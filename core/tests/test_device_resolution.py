"""Opening the capture device, and surviving its absence.

From the field: a container restart, and the engine died on its first line of
real work with `ValueError: No input device matching 'USB Audio CODEC: -
(hw:0,0)'`. The entrypoint had started it as a background job, so the container
stayed up and served a healthy-looking web UI attached to nothing for 38 hours,
with nothing in the diagnostics because the process that writes them was gone.

Two things can produce that error and the evidence cannot separate them: the
USB codec had not finished enumerating, or ALSA had renumbered the cards. The
device name PortAudio hands us — and that we store in config — embeds the card
number, and card numbers are assigned in registration order, so a working
device stops matching its own name when the onboard audio happens to register
first. Both are handled here.
"""
import asyncio
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CORE_DIR = os.path.dirname(HERE)
if CORE_DIR not in sys.path:
    sys.path.insert(0, CORE_DIR)

import core_engine  # noqa: E402

CODEC = "USB Audio CODEC: - (hw:0,0)"
CODEC_RENUMBERED = "USB Audio CODEC: - (hw:1,0)"
ONBOARD = "HDA Intel PCH: ALC233 Analog (hw:1,0)"


def dev(name, inputs=2):
    return {"name": name, "max_input_channels": inputs, "max_output_channels": 2}


class ResolveDeviceTest(unittest.TestCase):
    def test_an_exact_name_is_used_as_given(self):
        device, note = core_engine.resolve_device(CODEC, [dev(CODEC), dev(ONBOARD)])
        self.assertEqual(device, CODEC)
        self.assertIsNone(note)

    def test_no_configured_device_means_the_default(self):
        for configured in (None, "", "default"):
            with self.subTest(configured=configured):
                self.assertEqual(
                    core_engine.resolve_device(configured, [dev(CODEC)]), (None, None))

    def test_the_same_device_under_a_new_card_number_is_found(self):
        # The interface is present and working; ALSA just numbered it
        # differently on this boot. Refusing over that would be absurd.
        device, note = core_engine.resolve_device(
            CODEC, [dev(ONBOARD.replace("hw:1,0", "hw:0,0")), dev(CODEC_RENUMBERED)])
        self.assertEqual(device, 1)          # matched by index
        self.assertIsNotNone(note)
        self.assertIn("card number", note)

    def test_the_substitution_is_reported_not_silent(self):
        _device, note = core_engine.resolve_device(CODEC, [dev(CODEC_RENUMBERED)])
        self.assertIn("USB Audio CODEC", note)

    def test_a_genuinely_absent_device_raises(self):
        with self.assertRaises(LookupError):
            core_engine.resolve_device(CODEC, [dev(ONBOARD)])

    def test_an_empty_device_list_raises(self):
        # What a container that started before USB enumeration actually sees.
        with self.assertRaises(LookupError):
            core_engine.resolve_device(CODEC, [])

    def test_it_never_falls_back_to_the_default_input(self):
        # SpinSense exists to listen to one specific thing. Quietly recording
        # the onboard microphone would produce confident nonsense instead of an
        # obvious failure, and nothing downstream could tell the difference.
        with self.assertRaises(LookupError):
            core_engine.resolve_device(CODEC, [dev(ONBOARD), dev("default")])

    def test_output_only_devices_are_never_matched(self):
        with self.assertRaises(LookupError):
            core_engine.resolve_device(
                CODEC, [dev(CODEC_RENUMBERED, inputs=0)])

    def test_a_different_device_with_the_same_card_number_is_not_matched(self):
        # hw:0,0 is not an identity; the name is.
        with self.assertRaises(LookupError):
            core_engine.resolve_device(
                CODEC, [dev("HDA Intel PCH: ALC233 Analog (hw:0,0)")])

    def test_base_name_strips_only_the_card_coordinates(self):
        self.assertEqual(core_engine.device_base_name(CODEC),
                         core_engine.device_base_name(CODEC_RENUMBERED))
        self.assertEqual(core_engine.device_base_name("Scarlett 2i2 (plughw:2,0)"),
                         "scarlett 2i2")
        self.assertEqual(core_engine.device_base_name("Some Mic"), "some mic")
        self.assertEqual(core_engine.device_base_name(None), "")


class MissingDeviceIsRecoverableTest(unittest.IsolatedAsyncioTestCase):
    """A device that isn't there must pause the engine, not end it."""

    def setUp(self):
        self.events = []
        self.opens = 0

        async def fake_event(level, message):
            self.events.append((level, message))

        core_engine._orig_open = core_engine._open_input_stream
        core_engine._orig_event = core_engine.emit_event
        core_engine.emit_event = fake_event
        core_engine.state["device_missing"] = False

    def tearDown(self):
        core_engine._open_input_stream = core_engine._orig_open
        core_engine.emit_event = core_engine._orig_event
        core_engine.state["device_missing"] = False

    def fail_to_open(self, exc=LookupError("No input device matching 'x'")):
        def opener(_cb):
            self.opens += 1
            raise exc
        core_engine._open_input_stream = opener

    def open_fine(self):
        def opener(_cb):
            self.opens += 1
            return object(), None
        core_engine._open_input_stream = opener

    async def test_a_missing_device_returns_none_rather_than_raising(self):
        self.fail_to_open()
        stream = await core_engine._try_open_input_stream(lambda *a: None, was_open=True)
        self.assertIsNone(stream)

    async def test_it_says_so_in_the_diagnostics(self):
        self.fail_to_open()
        await core_engine._try_open_input_stream(lambda *a: None, was_open=True)
        self.assertTrue(any(lvl == "error" and "audio input" in m
                            for lvl, m in self.events))

    async def test_it_does_not_repeat_the_complaint_every_retry(self):
        # Retried every few seconds; one line per outage, not per attempt.
        self.fail_to_open()
        for _ in range(5):
            await core_engine._try_open_input_stream(lambda *a: None, was_open=False)
        self.assertEqual(len(self.events), 1)

    async def test_recovery_is_announced(self):
        self.fail_to_open()
        await core_engine._try_open_input_stream(lambda *a: None, was_open=True)
        self.open_fine()
        stream = await core_engine._try_open_input_stream(lambda *a: None, was_open=False)
        self.assertIsNotNone(stream)
        self.assertTrue(any(lvl == "info" and "listening again" in m
                            for lvl, m in self.events))

    async def test_a_card_renumbering_is_reported_when_it_happens(self):
        def opener(_cb):
            return object(), "'USB Audio CODEC: - (hw:0,0)' is not listed; the ALSA card number changed"
        core_engine._open_input_stream = opener
        await core_engine._try_open_input_stream(lambda *a: None, was_open=True)
        self.assertTrue(any(lvl == "warning" and "card number" in m
                            for lvl, m in self.events))


class RecognitionFailureIsSurvivableTest(unittest.IsolatedAsyncioTestCase):
    """sd.rec() raises if the interface disappears between opening the stream
    and capturing. That used to propagate out of the monitor loop and end the
    process."""

    def setUp(self):
        self.events = []

        async def fake_event(level, message):
            self.events.append((level, message))

        async def boom(**_kwargs):
            raise OSError("PortAudio: device unavailable")

        self._orig = (core_engine.emit_event, core_engine.recognize_audio)
        core_engine.emit_event = fake_event
        core_engine.recognize_audio = boom

    def tearDown(self):
        core_engine.emit_event, core_engine.recognize_audio = self._orig

    async def test_a_failed_recognition_is_reported_not_raised(self):
        await core_engine._safe_recognize()
        self.assertTrue(any(lvl == "error" and "Recognition failed" in m
                            for lvl, m in self.events))

    async def test_the_silence_counter_is_reset_so_the_loop_carries_on(self):
        core_engine.state["silence_counter"] = 9
        await core_engine._safe_recognize()
        self.assertEqual(core_engine.state["silence_counter"], 0)


if __name__ == "__main__":
    unittest.main()

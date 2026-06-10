# Source Menu + AcoustID + Back-off Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add AcoustID as a free backup recognizer behind a "backup recognizer" source menu (None/AudD/AcoustID, Shazam always primary), and fix the back-off rescan loop where an unidentifiable track that keeps playing re-scans forever.

**Architecture:** Reuses the normalized-adapter pattern from 1.5.0.0. A new AcoustID adapter (Chromaprint `fpcalc` fingerprint → AcoustID lookup → normalized mapping). The boolean `Fallback_Enabled` becomes a `Fallback_Provider` enum routed by an `_identify_fallback` dispatcher. The back-off bug is fixed by gating the back-off clear on a qualifying silence gap via a pure `_silence_step` helper.

**Tech Stack:** Python 3.12 (`unittest`, `asyncio`, `subprocess`, `tempfile`, `aiohttp`), Chromaprint `fpcalc` (Docker), Pydantic, vanilla JS + Jinja2.

Reference spec: `docs/superpowers/specs/2026-06-09-source-menu-acoustid-backoff.md`. Target version: **1.5.0.5**.

---

### Task 1: AcoustID adapter (additive)

Add the AcoustID recognition adapter. Purely additive — touches no existing config/wiring, so the suite stays green. Mirrors the AudD adapter's split (pure mapping + isolated I/O boundaries the tests stub).

**Files:**
- Modify: `core/core_engine.py` (imports; new functions after `_identify_audd`)
- Test: `core/tests/test_recognition_phases.py`

- [ ] **Step 1: Write tests for the mapping + adapter**

In `core/tests/test_recognition_phases.py`, add a new test class (after `AuddAdapterTest`):

```python
class AcoustidAdapterTest(unittest.TestCase):
    def setUp(self):
        self._orig = (core_engine._chromaprint_fingerprint, core_engine._acoustid_lookup,
                      core_engine.ACOUSTID_CLIENT_KEY)

    def tearDown(self):
        (core_engine._chromaprint_fingerprint, core_engine._acoustid_lookup,
         core_engine.ACOUSTID_CLIENT_KEY) = self._orig

    def test_maps_best_score_result(self):
        results = [
            {"score": 0.3, "recordings": [{"title": "Wrong", "artists": [{"name": "X"}]}]},
            {"score": 0.9, "recordings": [{
                "title": "Right", "artists": [{"name": "Tones and I"}],
                "releasegroups": [{"title": "The Album"}]}]},
        ]
        n = core_engine._acoustid_to_normalized(results)
        self.assertEqual(n["title"], "Right")
        self.assertEqual(n["artist"], "Tones and I")
        self.assertEqual(n["album"], "The Album")
        self.assertIsNone(n["art_url"])

    def test_joins_multiple_artists(self):
        results = [{"score": 1.0, "recordings": [{
            "title": "T", "artists": [{"name": "A"}, {"name": "B"}]}]}]
        self.assertEqual(core_engine._acoustid_to_normalized(results)["artist"], "A, B")

    def test_no_recordings_or_title_returns_none(self):
        self.assertIsNone(core_engine._acoustid_to_normalized([]))
        self.assertIsNone(core_engine._acoustid_to_normalized([{"score": 1.0, "recordings": []}]))
        self.assertIsNone(core_engine._acoustid_to_normalized(
            [{"score": 1.0, "recordings": [{"artists": [{"name": "A"}]}]}]))  # no title

    def test_identify_success(self):
        async def fake_fp(_w):
            return (180, "AQADtMk")
        async def fake_lookup(_d, _f):
            return {"status": "ok", "results": [
                {"score": 1.0, "recordings": [{"title": "T", "artists": [{"name": "A"}]}]}]}
        core_engine._chromaprint_fingerprint = fake_fp
        core_engine._acoustid_lookup = fake_lookup
        core_engine.ACOUSTID_CLIENT_KEY = "key"
        n = asyncio.run(core_engine._identify_acoustid(b""))
        self.assertEqual(n["title"], "T")

    def test_identify_no_results_returns_none(self):
        async def fake_fp(_w):
            return (180, "fp")
        async def fake_lookup(_d, _f):
            return {"status": "ok", "results": []}
        core_engine._chromaprint_fingerprint = fake_fp
        core_engine._acoustid_lookup = fake_lookup
        core_engine.ACOUSTID_CLIENT_KEY = "key"
        self.assertIsNone(asyncio.run(core_engine._identify_acoustid(b"")))

    def test_identify_fingerprint_failure_skips_lookup(self):
        self.lookup_called = False
        async def fake_fp(_w):
            return None
        async def fake_lookup(_d, _f):
            self.lookup_called = True
            return {}
        core_engine._chromaprint_fingerprint = fake_fp
        core_engine._acoustid_lookup = fake_lookup
        core_engine.ACOUSTID_CLIENT_KEY = "key"
        self.assertIsNone(asyncio.run(core_engine._identify_acoustid(b"")))
        self.assertFalse(self.lookup_called)

    def test_identify_no_key_skips_fingerprint(self):
        self.fp_called = False
        async def fake_fp(_w):
            self.fp_called = True
            return (1, "x")
        core_engine._chromaprint_fingerprint = fake_fp
        core_engine.ACOUSTID_CLIENT_KEY = ""
        self.assertIsNone(asyncio.run(core_engine._identify_acoustid(b"")))
        self.assertFalse(self.fp_called)
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest core/tests/test_recognition_phases.py::AcoustidAdapterTest -v`
Expected: FAIL — `_acoustid_to_normalized` / `_identify_acoustid` / `_chromaprint_fingerprint` / `_acoustid_lookup` / `ACOUSTID_CLIENT_KEY` don't exist.

- [ ] **Step 3: Add the imports**

In `core/core_engine.py`, after `import os` (line 3), add:
```python
import subprocess
import tempfile
```

- [ ] **Step 4: Implement the adapter**

In `core/core_engine.py`, add directly after the `_identify_audd` function:

```python
ACOUSTID_CLIENT_KEY = os.environ.get("SPINSENSE_ACOUSTID_KEY", "UGhMOSOjGb")
ACOUSTID_LOOKUP_URL = "https://api.acoustid.org/v2/lookup"


def _acoustid_to_normalized(results: list) -> dict | None:
    """Pure: map AcoustID lookup `results` to the normalized track shape, or None."""
    if not results:
        return None
    best = max(results, key=lambda r: r.get("score", 0) if isinstance(r, dict) else 0)
    recordings = best.get("recordings") or []
    if not recordings or not isinstance(recordings[0], dict):
        return None
    rec = recordings[0]
    title = rec.get("title")
    if not title:
        return None
    artists = rec.get("artists") or []
    names = [a.get("name", "") for a in artists if isinstance(a, dict) and a.get("name")]
    artist = ", ".join(names) or "Unknown Artist"
    album = None
    rgs = rec.get("releasegroups") or []
    if rgs and isinstance(rgs[0], dict):
        album = rgs[0].get("title") or None
    return {
        "title": title,
        "artist": artist,
        "album": album,
        "art_url": None,   # iTunes enrichment supplies art downstream
        "isrc": None,
        "genre": None,
        "release_year": None,
    }


def _run_fpcalc(wav_bytes: bytes) -> tuple[int, str] | None:
    """Blocking: write the WAV to a temp file, run `fpcalc -json`, return
    (duration_seconds, fingerprint). None on missing binary / error."""
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            f.write(wav_bytes)
            f.flush()
            out = subprocess.run(
                ["fpcalc", "-json", f.name],
                capture_output=True, text=True, timeout=15,
            )
        if out.returncode != 0:
            print(f"⚠️ fpcalc exited {out.returncode}: {out.stderr.strip()}")
            return None
        data = json.loads(out.stdout)
        return (int(round(float(data["duration"]))), data["fingerprint"])
    except FileNotFoundError:
        print("⚠️ fpcalc not installed — AcoustID unavailable")
        return None
    except Exception as e:
        print(f"⚠️ fpcalc failed: {e}")
        return None


async def _chromaprint_fingerprint(wav_bytes: bytes) -> tuple[int, str] | None:
    """Compute a Chromaprint fingerprint via fpcalc, off the event loop."""
    return await asyncio.to_thread(_run_fpcalc, wav_bytes)


async def _acoustid_lookup(duration: int, fingerprint: str) -> dict | None:
    """POST to the AcoustID lookup API; return parsed JSON, or None on any error."""
    try:
        data = aiohttp.FormData()
        data.add_field("client", ACOUSTID_CLIENT_KEY)
        data.add_field("duration", str(duration))
        data.add_field("fingerprint", fingerprint)
        data.add_field("meta", "recordings+releasegroups")
        data.add_field("format", "json")
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(ACOUSTID_LOOKUP_URL, data=data) as resp:
                if resp.status != 200:
                    print(f"⚠️ AcoustID HTTP {resp.status}")
                    return None
                return await resp.json(content_type=None)
    except Exception as e:
        print(f"⚠️ AcoustID request failed: {e}")
        return None


async def _identify_acoustid(wav_bytes: bytes) -> dict | None:
    """Recognize via AcoustID (Chromaprint fingerprint + lookup); normalized dict
    or None. No-ops without a client key; any error is a clean miss."""
    if not ACOUSTID_CLIENT_KEY:
        return None
    fp = await _chromaprint_fingerprint(wav_bytes)
    if not fp:
        return None
    duration, fingerprint = fp
    print("[!] Trying AcoustID fallback...")
    body = await _acoustid_lookup(duration, fingerprint)
    if not isinstance(body, dict) or body.get("status") != "ok":
        return None
    results = body.get("results") or []
    if not results:
        return None
    return _acoustid_to_normalized(results)
```

- [ ] **Step 5: Run to verify pass**

Run: `python -m pytest core/tests/test_recognition_phases.py::AcoustidAdapterTest -v`
Expected: PASS. Then the whole file: `python -m pytest core/tests/test_recognition_phases.py -q` → PASS (no regressions).

- [ ] **Step 6: Commit**

```bash
git add core/core_engine.py core/tests/test_recognition_phases.py
git commit -m "feat(audio): AcoustID recognition adapter (fpcalc + lookup + mapping)"
```

---

### Task 2: Source menu — Fallback_Provider + dispatcher + rewire

Replace the `Fallback_Enabled` boolean with a `Fallback_Provider` enum, add the `_identify_fallback` dispatcher, rewire `recognize_audio`, and update the 1.5.0.0 tests that reference the removed names.

**Files:**
- Test: `gui/tests/test_config_round_trip.py`, `core/tests/test_recognition_phases.py`
- Modify: `gui/config_manager.py`, `core/core_engine.py`

- [ ] **Step 1: Update config tests**

In `gui/tests/test_config_round_trip.py`, **replace** the two methods `test_fallback_defaults_off_and_empty_token` and `test_fallback_settings_round_trip` with:

```python
    def test_fallback_provider_defaults_none(self):
        defaults = config_manager.get_default_config()
        self.assertEqual(defaults["Audio"]["Fallback_Provider"], "none")
        self.assertEqual(defaults["Audio"]["AudD_API_Token"], "")

    def test_fallback_provider_round_trips(self):
        for provider in ("audd", "acoustid", "none"):
            cfg = config_manager.get_default_config()
            cfg["Audio"]["Fallback_Provider"] = provider
            self.assertTrue(config_manager.save_config(cfg), f"{provider} should validate")
            loaded = config_manager.load_config()
            self.assertEqual(loaded["Audio"]["Fallback_Provider"], provider)

    def test_fallback_provider_rejects_unknown(self):
        cfg = config_manager.get_default_config()
        cfg["Audio"]["Fallback_Provider"] = "spotify"
        self.assertFalse(config_manager.save_config(cfg))

    def test_audd_token_round_trips(self):
        cfg = config_manager.get_default_config()
        cfg["Audio"]["AudD_API_Token"] = "tok_abc123"
        self.assertTrue(config_manager.save_config(cfg))
        loaded = config_manager.load_config()
        self.assertEqual(loaded["Audio"]["AudD_API_Token"], "tok_abc123")
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest gui/tests/test_config_round_trip.py -v`
Expected: FAIL — `Fallback_Provider` doesn't exist (`KeyError`), and the unknown-value test fails (a `str` field accepts `"spotify"`).

- [ ] **Step 3: Swap the Pydantic field to a Literal enum**

In `gui/config_manager.py`, in `AudioConfig`, replace the line `Fallback_Enabled: bool = False` with:

```python
    Fallback_Provider: Literal["none", "audd", "acoustid"] = "none"
```

(`Literal` is already imported at the top of the file. `AudD_API_Token` stays.)

- [ ] **Step 4: Run config tests to verify pass**

Run: `python -m pytest gui/tests/test_config_round_trip.py -v`
Expected: PASS.

- [ ] **Step 5: Swap the engine default + runtime mirror**

In `core/core_engine.py`:

In `DEFAULT_CONFIG["Audio"]`, replace `"Fallback_Enabled": False,` with `"Fallback_Provider": "none",`.

In the `runtime` dict, replace `"fallback_enabled": False,` with `"fallback_provider": "none",`.

In `_populate_runtime`, replace the `runtime["fallback_enabled"] = ...` line with:
```python
    runtime["fallback_provider"] = cfg.get('Audio', {}).get('Fallback_Provider', 'none')
```

- [ ] **Step 6: Add the dispatcher + rewire recognize_audio**

In `core/core_engine.py`, add the dispatcher directly after `_identify_acoustid`:

```python
async def _identify_fallback(wav_bytes: bytes) -> dict | None:
    """Route the attempt-0 fallback to the configured backup recognizer."""
    provider = runtime["fallback_provider"]
    if provider == "audd":
        return await _identify_audd(wav_bytes)
    if provider == "acoustid":
        return await _identify_acoustid(wav_bytes)
    return None
```

In `recognize_audio`, replace the inline AudD gate:
```python
        track = await _identify_shazam(wav)
        if (track is None and attempt == 0
                and runtime["fallback_enabled"] and runtime["audd_token"]):
            track = await _identify_audd(wav)  # reuse the first sample
        if track:
            break
```
with:
```python
        track = await _identify_shazam(wav)
        if track is None and attempt == 0:
            track = await _identify_fallback(wav)  # reuse the first sample
        if track:
            break
```

- [ ] **Step 7: Update the flow tests to the provider model + add a dispatch test**

In `core/tests/test_recognition_phases.py`, in `AuddFallbackFlowTest`:
- In `setUp`, change `self._orig_flags = (core_engine.runtime["fallback_enabled"], ...)` to `core_engine.runtime["fallback_provider"]`.
- In `tearDown`, change the matching restore tuple's first element to `core_engine.runtime["fallback_provider"]`.
- Replace the `_set` helper with:
```python
    def _set(self, shazam_fn, audd_fn, provider="audd"):
        core_engine._identify_shazam = shazam_fn
        core_engine._identify_audd = audd_fn
        core_engine.runtime["fallback_provider"] = provider
        core_engine.runtime["audd_token"] = "tok"
```
- In `test_disabled_never_calls_audd`, change `self._set(shazam, audd, enabled=False)` to `self._set(shazam, audd, provider="none")`.
- **Delete** the `test_no_token_never_calls_audd` method entirely — the token gate now lives inside `_identify_audd` and is covered by `AuddAdapterTest.test_identify_no_token_skips_post`; with the provider dispatcher the flow test would stub past it.

Then add a dispatcher routing test class (after `AuddFallbackFlowTest`):

```python
class FallbackDispatchTest(unittest.TestCase):
    def setUp(self):
        self._orig = (core_engine._identify_audd, core_engine._identify_acoustid,
                      core_engine.runtime["fallback_provider"])
        self.calls = []
        async def fake_audd(_w):
            self.calls.append("audd"); return {"title": "AudD", "artist": "A"}
        async def fake_acoustid(_w):
            self.calls.append("acoustid"); return {"title": "AcoustID", "artist": "A"}
        core_engine._identify_audd = fake_audd
        core_engine._identify_acoustid = fake_acoustid

    def tearDown(self):
        (core_engine._identify_audd, core_engine._identify_acoustid,
         core_engine.runtime["fallback_provider"]) = self._orig

    def test_none_routes_to_nothing(self):
        core_engine.runtime["fallback_provider"] = "none"
        self.assertIsNone(asyncio.run(core_engine._identify_fallback(b"")))
        self.assertEqual(self.calls, [])

    def test_audd_routes_to_audd(self):
        core_engine.runtime["fallback_provider"] = "audd"
        n = asyncio.run(core_engine._identify_fallback(b""))
        self.assertEqual(n["title"], "AudD")
        self.assertEqual(self.calls, ["audd"])

    def test_acoustid_routes_to_acoustid(self):
        core_engine.runtime["fallback_provider"] = "acoustid"
        n = asyncio.run(core_engine._identify_fallback(b""))
        self.assertEqual(n["title"], "AcoustID")
        self.assertEqual(self.calls, ["acoustid"])
```

- [ ] **Step 8: Run the full suites**

Run: `python -m pytest core/tests/test_recognition_phases.py gui/tests/test_config_round_trip.py -q`
Expected: PASS (updated flow tests, new dispatch test, config enum tests).

Then confirm no dangling old names:
Run: `grep -rn "fallback_enabled\|Fallback_Enabled" core/ gui/ --include=*.py`
Expected: no matches.

- [ ] **Step 9: Commit**

```bash
git add core/core_engine.py gui/config_manager.py core/tests/test_recognition_phases.py gui/tests/test_config_round_trip.py
git commit -m "feat(audio): backup-recognizer source menu (Fallback_Provider: none/audd/acoustid)"
```

---

### Task 3: Fix the back-off rescan loop

Gate the back-off clear on a qualifying silence gap via a pure `_silence_step` helper, so the phantom post-scan zero (and momentary dips) can't re-arm scanning on an unidentifiable track.

**Files:**
- Modify: `core/core_engine.py` (add `_silence_step`; rewrite the `silence` branch of `audio_monitor_loop`)
- Test: `core/tests/test_recognition_phases.py`

- [ ] **Step 1: Write the `_silence_step` tests**

In `core/tests/test_recognition_phases.py`, add a new test class:

```python
class SilenceStepTest(unittest.TestCase):
    def s(self, sc, in_song, back_off, ns=3, stop=5):
        return core_engine._silence_step(sc, in_song, back_off, ns, stop)

    def test_backoff_survives_brief_dip(self):
        # The phantom post-scan zero / a 1-tick dip: below the interval -> stays armed.
        self.assertEqual(self.s(0, False, True), (1, True, False))

    def test_backoff_clears_after_qualifying_gap(self):
        # silence_counter reaches new_song_silence (3) -> back_off clears.
        self.assertEqual(self.s(2, False, True), (3, False, False))

    def test_in_song_stops_at_stopped_silence(self):
        self.assertEqual(self.s(4, True, False), (5, False, True))

    def test_in_song_below_stop_does_not_stop(self):
        self.assertEqual(self.s(3, True, False), (4, False, False))

    def test_not_in_song_never_stops(self):
        self.assertEqual(self.s(10, False, False), (11, False, False))
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest core/tests/test_recognition_phases.py::SilenceStepTest -v`
Expected: FAIL — `_silence_step` doesn't exist.

- [ ] **Step 3: Add `_silence_step`**

In `core/core_engine.py`, add directly above `_scan_decision`:

```python
def _silence_step(silence_counter, in_song, back_off, new_song_silence, stopped_silence):
    """Pure: process one below-threshold (silence) tick. Returns
    (silence_counter, back_off, stop). `stop` True => clear the track + publish 'stopped'.

    back_off clears only after a *qualifying* gap (>= new_song_silence), so a
    momentary dip or the phantom zero-RMS tick the loop injects right after a scan
    can't re-arm scanning on an unidentifiable track that keeps playing."""
    silence_counter += 1
    if back_off and silence_counter >= new_song_silence:
        back_off = False
    stop = in_song and silence_counter >= stopped_silence
    return silence_counter, back_off, stop
```

- [ ] **Step 4: Rewrite the `silence` branch of the monitor loop**

In `core/core_engine.py` `audio_monitor_loop`, replace the entire `else:  # silence` branch:
```python
        else:  # silence
            state["back_off"] = False  # gap observed → next onset is fair game
            if state["in_song"]:
                state["silence_counter"] += 1
                print("s", end="", flush=True)
                if state["silence_counter"] >= runtime["stopped_silence"]:
                    print(f"\n[ STOPPED ] {runtime['stopped_silence']}s silence limit reached.")
                    publish_state("stopped")
                    _clear_track_state(set_backoff=False)
                    state["silence_counter"] = 0
```
with:
```python
        else:  # silence
            new_sc, new_bo, stop = _silence_step(
                state["silence_counter"], state["in_song"], state.get("back_off", False),
                runtime["new_song_silence"], runtime["stopped_silence"],
            )
            if state["in_song"]:
                print("s", end="", flush=True)
            state["silence_counter"] = new_sc
            state["back_off"] = new_bo
            if stop:
                print(f"\n[ STOPPED ] {runtime['stopped_silence']}s silence limit reached.")
                publish_state("stopped")
                _clear_track_state(set_backoff=False)
                state["silence_counter"] = 0
```

(The `"s"` print stays gated on `in_song`, so idle/back-off console output is unchanged; only the back-off-clear logic changes.)

- [ ] **Step 5: Run the full file to verify pass**

Run: `python -m pytest core/tests/test_recognition_phases.py -q`
Expected: PASS (`SilenceStepTest` plus all prior classes — the monitor loop isn't directly unit-tested, so no regression).

- [ ] **Step 6: Commit**

```bash
git add core/core_engine.py core/tests/test_recognition_phases.py
git commit -m "fix(audio): hold back-off until a qualifying gap, ending the rescan loop"
```

---

### Task 4: Settings UI — backup recognizer dropdown

Replace the AudD enable toggle with a provider `<select>`, and make the dropdown mark the form dirty.

**Files:**
- Modify: `gui/templates/settings.html`, `gui/static/settings.js`

- [ ] **Step 1: Replace the toggle with a select**

In `gui/templates/settings.html`, replace the entire "Use AudD as a backup recognizer" toggle block (the `<label class="flex items-center justify-between gap-md cursor-pointer">` … `</label>` whose input is `name="Audio.Fallback_Enabled"`) with:

```html
        <label class="block">
          <span class="text-body-sm text-on-surface mb-1 block">Backup recognizer<span class="help-tip" tabindex="0" role="note" aria-label="Help: Backup recognizer"><span class="material-symbols-outlined help-icon">help</span><span class="help-bubble" role="tooltip">When Shazam can't identify a track on its first try, SpinSense retries the same sample against this backup before giving up. Shazam stays the primary. AudD needs an API token below; AcoustID is free and needs nothing.</span></span></span>
          <select name="Audio.Fallback_Provider" class="form-input">
            <option value="none">None (Shazam only)</option>
            <option value="audd">AudD</option>
            <option value="acoustid">AcoustID (free)</option>
          </select>
        </label>
```

Then update the AudD token field's help bubble (the `<input type="password" name="Audio.AudD_API_Token">` label) — replace its `help-bubble` text with:
```
Your API token from audd.io — only needed when "Backup recognizer" is set to AudD. Stored in config.json in plaintext (like the MQTT password), fine for a self-hosted LAN box.
```

- [ ] **Step 2: Make the dropdown mark the form dirty**

In `gui/static/settings.js`, find the change handler:
```javascript
  FORM.addEventListener("change", (ev) => {
    if (ev.target.type === "checkbox") setDirty(true);
  });
```
and replace it with:
```javascript
  FORM.addEventListener("change", (ev) => {
    if (ev.target.type === "checkbox" || ev.target.tagName === "SELECT") setDirty(true);
  });
```

- [ ] **Step 3: Verify**

Run: `grep -n "Audio.Fallback_Provider\|Audio.AudD_API_Token\|Audio.Fallback_Enabled" gui/templates/settings.html`
Expected: the provider `<select>` and the token input match; **no** `Fallback_Enabled` remains.

> Manual check (if running the app): load `/settings`, confirm the "Backup recognizer" dropdown renders (None/AudD/AcoustID), changing it enables Save, and a save round-trips (reload shows the chosen value).

- [ ] **Step 4: Commit**

```bash
git add gui/templates/settings.html gui/static/settings.js
git commit -m "feat(settings): backup-recognizer dropdown (None/AudD/AcoustID)"
```

---

### Task 5: Docker — add fpcalc

**Files:**
- Modify: `docker/Dockerfile`

- [ ] **Step 1: Add libchromaprint-tools to the apt install**

In `docker/Dockerfile`, in the `RUN apt-get update && apt-get install -y \` block, add `libchromaprint-tools \` to the package list (it provides `fpcalc`). The block becomes:

```dockerfile
RUN apt-get update && apt-get install -y \
    portaudio19-dev \
    alsa-utils \
    libsndfile1 \
    gcc \
    ffmpeg \
    libchromaprint-tools \
    && rm -rf /var/lib/apt/lists/*
```

- [ ] **Step 2: Verify**

Run: `grep -n "libchromaprint-tools" docker/Dockerfile`
Expected: one match inside the apt install block.

- [ ] **Step 3: Commit**

```bash
git add docker/Dockerfile
git commit -m "build: add libchromaprint-tools (fpcalc) for AcoustID"
```

---

### Task 6: Changelog

**Files:**
- Modify: `CHANGELOG.md` (add `## [Unreleased]` above the latest released heading)

- [ ] **Step 1: Add the Unreleased entry**

In `CHANGELOG.md`, insert after the intro paragraph and before the first `## [x.y.z.w]` heading:

```markdown
## [Unreleased]

### Added
- **AcoustID backup recognizer + a "Backup recognizer" menu in Settings.** Shazam stays the always-on primary; you can now pick a backup from **None / AudD / AcoustID**. AcoustID is free (no subscription, no signup — an app key ships embedded), using on-device Chromaprint fingerprints against the MusicBrainz-linked AcoustID database. Off by default.

### Fixed
- **Endless rescan loop on unidentifiable tracks.** A track that Shazam (and the backup) couldn't identify would re-scan over and over while it kept playing, because the post-scan RMS reset was misread as a silence gap that cleared the back-off. The back-off now holds until a genuine between-song gap, so a failed track settles quietly instead of looping.

### Changed
- The 1.5.0.0 `Audio.Fallback_Enabled` boolean is replaced by `Audio.Fallback_Provider` (`none`/`audd`/`acoustid`). Existing configs default to `none`.
```

- [ ] **Step 2: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: changelog for source menu + AcoustID + back-off fix"
```

---

### Final verification

- [ ] **Run all affected suites**

Run: `python -m pytest core/tests/test_recognition_phases.py gui/tests/test_config_round_trip.py -q`
Expected: all PASS.

Run (full, per-dir to avoid the cross-package collection collision): `python -m pytest core/tests/ -q` and `cd gui && python -m pytest tests/ -q`
Expected: all PASS.

- [ ] **Confirm no dangling old names**

Run: `grep -rn "fallback_enabled\|Fallback_Enabled" core/ gui/ --include=*.py`
Expected: no matches.

---

## Notes for the implementer

- **`aiohttp` is already a dependency** (iTunes/art/AudD). `subprocess`/`tempfile` are stdlib. The only new *runtime* dependency is the `fpcalc` binary (Task 5).
- **fpcalc-missing is graceful:** `_run_fpcalc` catches `FileNotFoundError` → AcoustID yields no match, never crashes. So the suite passes on a dev box without fpcalc (the tests stub `_chromaprint_fingerprint` anyway).
- **Testability pattern:** recognition is tested by swapping module-level functions on `core_engine` (e.g. `core_engine._acoustid_lookup = fake`). The I/O boundaries (`_chromaprint_fingerprint`, `_acoustid_lookup`, `_audd_post`) exist precisely so the flow can be stubbed without real network/binaries — keep them as separate module-level functions.
- **Art precedence unchanged:** the AcoustID adapter returns `art_url=None`; `_handle_match` still fetches iTunes art first. Don't add backend art handling.
- **The token gate moved:** with the provider dispatcher, "no AudD token" is handled *inside* `_identify_audd` (it self-gates), not in `recognize_audio`. That's why the flow test's `test_no_token_never_calls_audd` is removed and the adapter-level `test_identify_no_token_skips_post` is the canonical coverage.
- **VERSION bump to 1.5.0.5** happens at ship time (promote `[Unreleased]` → `[1.5.0.5]` + bump the `VERSION` file), consistent with prior releases — not part of these tasks.

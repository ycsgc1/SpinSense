# SpinSense Roadmap

Post-1.0 backlog — things intentionally deferred at the **1.0.0.0** launch (2026-06-01). Nothing here is scheduled; pick up whenever you're refreshed.

## Features
- **Wrapped story mode** — a swipeable year-in-review recap (big reveal cards) layered on the Stats API (`/api/stats?period=year&year=N`). The Stats page (shipped) is the data foundation; this is pure UI.
- **Database export / import** — clean backup and restore of the SQLite database (and the album-art cache) so the history can move between devices.

## Field reports (1.8.0.0-beta, 2026-08-29)

Found running the beta on real records. Ordered roughly by how much they hurt.

### Phono input mode (software RIAA + gain)

The rig feeds a **raw phono cartridge** into a UCA202, which is a line-level,
fixed-gain ADC — `amixer -c 0 scontrols` shows a single `PCM` control and no
capture gain to raise. Measured in the field: peaks of 50–90 out of 32767,
about **−55 dBFS**, roughly 45 dB below a healthy line input and using ~6 of 16
bits. Sample normalisation is pinned at its 30 dB ceiling on every capture and
still lands around −23 dBFS.

A hardware phono stage is ruled out by choice — the listener has a better one
in their speakers, and a UFO202's passthrough carries its *preamped* signal, so
it can't be bypassed. Splitters introduced hum on previous attempts.

So the software path:

- **RIAA de-emphasis.** The playback curve is a zero at 318 µs over poles at
  3180 µs and 75 µs. A bilinear-transformed biquad cascade was designed and
  checked against the published table: accurate to **0.05 dB below 2 kHz**, but
  drifting to −9 dB at 20 kHz from frequency warping, so the poles need
  prewarping (or oversampling) before it is honest.
- **Gain**, beyond the current 30 dB cap, once the spectrum is corrected.
- **A rumble filter.** RIAA lifts 20 Hz by +19 dB, which is also where mains
  hum and turntable rumble live; a high-pass around 20 Hz has to come with it.

**Set expectations honestly.** This corrects the *spectrum*, not the *bits*.
Un-corrected, the treble sits ~20 dB hot and the bass ~20 dB low, so bass
content is currently around −75 dBFS — under the quantisation floor of a 16-bit
converter at that level. Boosting it amplifies quantisation noise and hum, not
music. Expect a real improvement in recognition, not parity with 45 dB of
analogue gain.

Worth a bench test before committing: capture the same passage with and without
correction and compare Shazam hit rates.

### Tell a remaster from the original

Wanted: when Shazam matched the remaster rather than the original pressing, say
so. Currently both reduce to the same base title and are deliberately merged,
since for history purposes they are the same album.

The only hook already in hand is `isrc`, which we store per play and which is
per-recording — but remasters inconsistently reuse the original's ISRC, so it
identifies a remaster sometimes and silently fails the rest of the time. Worth
a spike against real records before designing anything on top of it.

## Docs
- **Home Assistant `media_player` screenshot** — the one missing "payoff" image; add it to the README's "In Home Assistant" subsection. Drop the file in `docs/images/` (e.g. `ha-entity.png`) and wire it in.

## Known limitations (noted, not bugs)
- The engine **hardcodes the MQTT topics** (`MQTT.Topics.*`): `config.json` exposes the fields but `core/core_engine.py` ignores them. Either wire them up or remove the dead config fields. *(The dead `MQTT.Discovery` config was removed in 1.4.0.0.)*

---

*See [CHANGELOG.md](CHANGELOG.md) for what shipped in 1.0.*

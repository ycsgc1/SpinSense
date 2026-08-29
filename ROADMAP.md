# SpinSense Roadmap

Post-1.0 backlog — things intentionally deferred at the **1.0.0.0** launch (2026-06-01). Nothing here is scheduled; pick up whenever you're refreshed.

## Features
- **Wrapped story mode** — a swipeable year-in-review recap (big reveal cards) layered on the Stats API (`/api/stats?period=year&year=N`). The Stats page (shipped) is the data foundation; this is pure UI.
- **Database export / import** — clean backup and restore of the SQLite database (and the album-art cache) so the history can move between devices.

## Field reports (1.8.0.0-beta, 2026-08-29)

Found running the beta on real records. Ordered roughly by how much they hurt.

### Recognition misses quiet songs
Slow, sad, low-level tracks fail to identify far more often than loud ones — consistently enough to look like a threshold-adjacent effect rather than bad luck, and the input clearly carries enough signal the rest of the time.

Worth separating two failure modes before picking a fix: the track never crosses `Volume_Threshold` so no scan is attempted at all, versus a scan happening but Shazam missing on a quiet sample. The engine can tell them apart today — the first logs nothing, the second logs `Could not identify`.

Most promising fix if it's the second: **peak-normalise the captured WAV before submitting it**. Fingerprinting is more reliable with a well-levelled sample, we already hold the buffer in memory in `_capture_sample()`, and it costs one numpy multiply. If it's the first, hysteresis (a lower threshold to *stay* in a song than to enter one) is the better tool than lowering the threshold globally, which is what we already established re-triggers on quiet passages.

### Audio input silently stalls
Twice in a month the engine stopped seeing audio: the input meter sat at exactly `0` where it normally jitters below the threshold. A TrueNAS reboot fixed it, and a container restart appeared to as well.

Nothing in `audio_monitor_loop` checks that the stream is still alive. `audio_callback` writes `state["current_rms"]` on every buffer (~22×/sec); if the callback stops firing, the last value simply persists, and if the device starts returning zeros the RMS is exactly `0.0`. Neither is noticed.

A watchdog is cheap and reuses machinery that already exists: record the time of the last `audio_callback`, and if nothing has arrived for N seconds, tear down and reopen the stream exactly as the mic-change path already does. Exactly-`0.0` RMS is a second, independent signal — a real analogue input essentially never produces it — so "stalled" can be surfaced in the status frame for the dashboard and Home Assistant rather than only being self-healed silently. Decide whether to root-cause the USB/ALSA side as well, or treat recovery as sufficient.

### Reconciliation merges different recordings, not just editions
Confirmed on Olivia Rodrigo's *SOUR*: `SOUR (Video Version)` normalises to base `sour`, merges with `SOUR`, and then wins because `pick_winner()` prefers the longest string.

Two separate faults:
- `version` sits in `_EDITION_MARKER_RE`, but "Video Version" denotes a different **rendition**, like live/acoustic/instrumental — which the original spec deliberately excluded. The marker list needs the same treatment for renditions generally (video, radio edit, instrumental, karaoke, sped up, slowed).
- "Most qualifiers wins" is the deeper problem. It assumes qualifiers stack toward a more complete edition, which holds for Deluxe → Super Deluxe and fails for anything that isn't a superset. Ranking known edition markers explicitly would beat measuring string length.

## Polish

- **Drop or rework the System Health panel.** The connection status conveys nothing actionable, and the input level neither responds properly nor adds anything over the meter already on the same page.

## Docs
- **Home Assistant `media_player` screenshot** — the one missing "payoff" image; add it to the README's "In Home Assistant" subsection. Drop the file in `docs/images/` (e.g. `ha-entity.png`) and wire it in.

## Known limitations (noted, not bugs)
- The engine **hardcodes the MQTT topics** (`MQTT.Topics.*`): `config.json` exposes the fields but `core/core_engine.py` ignores them. Either wire them up or remove the dead config fields. *(The dead `MQTT.Discovery` config was removed in 1.4.0.0.)*

---

*See [CHANGELOG.md](CHANGELOG.md) for what shipped in 1.0.*

# SpinSense Roadmap

Post-1.0 backlog — things intentionally deferred at the **1.0.0.0** launch (2026-06-01). Nothing here is scheduled; pick up whenever you're refreshed.

## Features
- **Listening analytics / "Wrapped"** — a stats view over play history (top artists/tracks, total listening time, trends over time), Spotify-Wrapped style. The `plays` table already captures `isrc`, `genre`, and `release_year` (added in 1.0), so richer data is accumulating now — this is mainly the queries + UI.
- **Database export / import** — clean backup and restore of the SQLite database (and the album-art cache) so the history can move between devices.

## UI / UX
- **Collapse / hide the MQTT section in Settings** — the MQTT broker block (host/port/credentials) is clutter for the majority who use mDNS discovery and never touch MQTT. Make it a collapsible section (collapsed by default), and/or gate it behind an "Enable MQTT" toggle so the broker fields only appear when MQTT is actually in use.

## Docs / polish
- **Home Assistant `media_player` screenshot** — the one missing "payoff" image; add it to the README's "In Home Assistant" subsection. Drop the file in `docs/images/` (e.g. `ha-entity.png`) and wire it in.
- **Re-grab two wizard screenshots** — `Connection_Selection.png` and `MQTT_Interface.png` still show the old intro line ("Enter your MQTT broker's address…"); the live wizard copy was corrected in 1.0, so these are slightly stale.

## Cleanup / tech debt
- **Remove build-time scaffolding & repo cruft.** `stitch/` (8 files — Stitch design mockups + screenshots used to prototype the UI; nothing at runtime references them, only the historical design specs under `docs/superpowers/` do) is dead weight — delete it. Also drop the committed macOS cruft (`.DS_Store`, `gui/.DS_Store`) and the empty `.env.example`, and confirm `.DS_Store` is in `.gitignore`. *(The root `DESIGN.md` — the original design doc — is already untracked; keep it locally or relocate into `docs/` if you want it versioned.)*

## Known limitations (noted, not bugs)
- The engine **hardcodes the MQTT topics** (`MQTT.Topics.*`): `config.json` exposes the fields but `core/core_engine.py` ignores them. Either wire them up or remove the dead config fields. *(The dead `MQTT.Discovery` config was removed in 1.4.0.0.)*

---

*See [CHANGELOG.md](CHANGELOG.md) for what shipped in 1.0.*

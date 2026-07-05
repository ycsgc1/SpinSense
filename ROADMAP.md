# SpinSense Roadmap

Post-1.0 backlog — things intentionally deferred at the **1.0.0.0** launch (2026-06-01). Nothing here is scheduled; pick up whenever you're refreshed.

## Features
- **Listening analytics / "Wrapped"** — a stats view over play history (top artists/tracks, total listening time, trends over time), Spotify-Wrapped style. The `plays` table already captures `isrc`, `genre`, and `release_year` (added in 1.0), so richer data is accumulating now — this is mainly the queries + UI.
- **Database export / import** — clean backup and restore of the SQLite database (and the album-art cache) so the history can move between devices.

## Docs / polish
- **Home Assistant `media_player` screenshot** — the one missing "payoff" image; add it to the README's "In Home Assistant" subsection. Drop the file in `docs/images/` (e.g. `ha-entity.png`) and wire it in.

## Known limitations (noted, not bugs)
- The engine **hardcodes the MQTT topics** (`MQTT.Topics.*`): `config.json` exposes the fields but `core/core_engine.py` ignores them. Either wire them up or remove the dead config fields. *(The dead `MQTT.Discovery` config was removed in 1.4.0.0.)*

---

*See [CHANGELOG.md](CHANGELOG.md) for what shipped in 1.0.*

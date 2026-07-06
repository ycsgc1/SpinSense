# SpinSense Roadmap

Post-1.0 backlog — things intentionally deferred at the **1.0.0.0** launch (2026-06-01). Nothing here is scheduled; pick up whenever you're refreshed.

## Features
- **Wrapped story mode** — a swipeable year-in-review recap (big reveal cards) layered on the Stats API (`/api/stats?period=year&year=N`). The Stats page (shipped) is the data foundation; this is pure UI.
- **Last.fm scrobbling** — the `plays` table now records everything track.scrobble needs (`played_at` start timestamp, `duration_secs`, `ended_at` for the ≥half-or-4-min eligibility rule); remaining work is auth + the submission client.
- **Database export / import** — clean backup and restore of the SQLite database (and the album-art cache) so the history can move between devices.

## Docs / polish
- **Home Assistant `media_player` screenshot** — the one missing "payoff" image; add it to the README's "In Home Assistant" subsection. Drop the file in `docs/images/` (e.g. `ha-entity.png`) and wire it in.

## Known limitations (noted, not bugs)
- The engine **hardcodes the MQTT topics** (`MQTT.Topics.*`): `config.json` exposes the fields but `core/core_engine.py` ignores them. Either wire them up or remove the dead config fields. *(The dead `MQTT.Discovery` config was removed in 1.4.0.0.)*

---

*See [CHANGELOG.md](CHANGELOG.md) for what shipped in 1.0.*

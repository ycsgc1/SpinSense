# SpinSense Roadmap

Post-1.0 backlog — things intentionally deferred at the **1.0.0.0** launch (2026-06-01). Nothing here is scheduled; pick up whenever you're refreshed.

## Features
- **Wrapped story mode** — a swipeable year-in-review recap (big reveal cards) layered on the Stats API (`/api/stats?period=year&year=N`). The Stats page (shipped) is the data foundation; this is pure UI.
- **Database export / import** — clean backup and restore of the SQLite database (and the album-art cache) so the history can move between devices.

## Field reports (1.8.0.0-beta, 2026-08-29)

Found running the beta on real records. Ordered roughly by how much they hurt.

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

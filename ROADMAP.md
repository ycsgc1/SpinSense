# SpinSense Roadmap

Post-1.0 backlog — things intentionally deferred at the **1.0.0.0** launch (2026-06-01). Nothing here is scheduled; pick up whenever you're refreshed.

## Features
- **Wrapped story mode** — a swipeable year-in-review recap (big reveal cards) layered on the Stats API (`/api/stats?period=year&year=N`). The Stats page (shipped) is the data foundation; this is pure UI.
- **Database export / import** — clean backup and restore of the SQLite database (and the album-art cache) so the history can move between devices.

## Field reports (1.8.0.0-beta, 2026-08-29)

Found running the beta on real records. Ordered roughly by how much they hurt.

### Reconciliation merges different recordings, not just editions
Confirmed on Olivia Rodrigo's *SOUR*: `SOUR (Video Version)` normalises to base `sour`, merges with `SOUR`, and then wins because `pick_winner()` prefers the longest string.

Two separate faults:
- `version` sits in `_EDITION_MARKER_RE`, but "Video Version" denotes a different **rendition**, like live/acoustic/instrumental — which the original spec deliberately excluded. The marker list needs the same treatment for renditions generally (video, radio edit, instrumental, karaoke, sped up, slowed).
- "Most qualifiers wins" is the deeper problem. It assumes qualifiers stack toward a more complete edition, which holds for Deluxe → Super Deluxe and fails for anything that isn't a superset. Ranking known edition markers explicitly would beat measuring string length.

## Docs
- **Home Assistant `media_player` screenshot** — the one missing "payoff" image; add it to the README's "In Home Assistant" subsection. Drop the file in `docs/images/` (e.g. `ha-entity.png`) and wire it in.

## Known limitations (noted, not bugs)
- The engine **hardcodes the MQTT topics** (`MQTT.Topics.*`): `config.json` exposes the fields but `core/core_engine.py` ignores them. Either wire them up or remove the dead config fields. *(The dead `MQTT.Discovery` config was removed in 1.4.0.0.)*

---

*See [CHANGELOG.md](CHANGELOG.md) for what shipped in 1.0.*

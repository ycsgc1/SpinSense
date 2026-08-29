# SpinSense Roadmap

Post-1.0 backlog — things intentionally deferred at the **1.0.0.0** launch (2026-06-01). Nothing here is scheduled; pick up whenever you're refreshed.

## Features
- **Wrapped story mode** — a swipeable year-in-review recap (big reveal cards) layered on the Stats API (`/api/stats?period=year&year=N`). The Stats page (shipped) is the data foundation; this is pure UI.
- **Database export / import** — clean backup and restore of the SQLite database (and the album-art cache) so the history can move between devices.

## Field reports (1.8.0.0-beta, 2026-08-29)

Found running the beta on real records. Ordered roughly by how much they hurt.

### Upgrade a run to the deluxe on evidence

The edition/rendition split shipped, and the winner rule is now "plainest title
wins" — so a run defaults to the base album, as originally intended. The other
half of that idea is still missing: **notice when a track can only exist on the
deluxe, and upgrade the whole run.**

The evidence is available. iTunes' song search returns several albums per
track, so for each identified track we can ask: does any result share this
album's base title *without* an edition qualifier? If yes, the track exists on
the base edition and proves nothing. If every result for that track is a
qualified edition, the track is exclusive to it — and the record on the platter
must be that edition, so the run should be upgraded.

Two things to settle before building it:

- **Where it runs.** `fetch_itunes_metadata()` currently asks for `limit=1` and
  lives in the engine, but `base_title()` and the marker lists live in
  `gui/reconcile.py`. Duplicating the marker vocabulary across two processes is
  exactly how the SOUR bug would come back, so either the decision moves to the
  GUI (which already has `_itunes_album_candidates`) or the vocabulary moves to
  a module both can import.
- **Storing the evidence.** Reconciliation runs later and can't re-derive it, so
  the exclusivity finding needs a column on `plays` — then `reconcile_album()`
  picks the qualified edition when any play in the group carries the flag, and
  the plain title otherwise.

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

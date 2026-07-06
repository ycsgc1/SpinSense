# Wrapped / Listening Stats — Design

**Date:** 2026-07-06
**Scope:** New "Stats" page over play history (top artists/tracks, plays, listening time, trends, genres/decades), plus the schema and capture changes that make listening time real — designed so (a) a story-style yearly "Wrapped" recap and (b) Last.fm scrobbling can be layered on later without another migration.

## Decisions made during brainstorming

- **Product shape:** permanent Stats page now; story-style yearly recap later reuses the same API (period=year). Not in this build.
- **Listening time:** record real durations from now on. **No estimation for old rows** — plays without an `ended_at` are simply excluded from listening-time totals (they still count everywhere else). Only two users exist; historical accuracy isn't worth heuristic complexity.
- **Last.fm forward-compatibility:** capture what scrobbling will need, don't implement scrobbling.
- **Modules (v1):** headline totals, top artists + tracks, plays-over-time chart, genres + decades.
- **Architecture:** server-computed stats (approach A) — pure SQL/Python aggregation module + one JSON endpoint + dependency-free charts (CSS bars / inline SVG). No chart library, no client-side aggregation.

## 1. Schema

Two nullable columns on `plays`, added in `play_history.init_db()` via the existing `PRAGMA table_info` migration pattern:

| Column | Type | Meaning |
|---|---|---|
| `ended_at` | INTEGER (nullable) | Unix seconds when the track stopped playing (next track started, or silence/stop). NULL = play predates this feature or the GUI restarted mid-play. |
| `duration_secs` | INTEGER (nullable) | Canonical track length from enrichment (iTunes `trackTimeMillis`, else AudD `durationInMillis`). NULL when no source had it. |

Existing rows keep NULLs. No backfill.

## 2. Capturing the data

### ended_at (gui/ipc_manager.py)

- New module-level `_last_play_id: int | None` alongside the existing dedupe key.
- In `_record_if_new`:
  - When a **new play is recorded**: first stamp the previous `_last_play_id`'s `ended_at` with `now`, then set `_last_play_id` to the new row id.
  - When the **title goes empty** (engine reported stopped or no-match): stamp `_last_play_id`'s `ended_at` with `now`, clear `_last_play_id`.
- New `play_history.set_ended_at(play_id, ts)` writes `ended_at` **only if currently NULL** (idempotent; first stamp wins).
- A GUI restart mid-play loses `_last_play_id` → that row stays NULL and is excluded from listening time. Accepted.
- Known imprecision: a failed re-identification of a still-playing track emits an empty-title frame (`no_match`), stamping `ended_at` early. Accepted — bounded by the read-time cap below and rare in practice.

### duration_secs (core/core_engine.py)

- `fetch_itunes_metadata()` returns `(album, art_url, duration_secs)` — it already receives `trackTimeMillis`; stop discarding it (`round(ms / 1000)`).
- `_audd_to_normalized()` maps `apple_music.durationInMillis` the same way. Shazam alone provides nothing → NULL.
- The normalized track dict, `state`, and `build_status_payload()`'s `track` object gain `duration_secs` (extra frame key is backward-compatible for the HACS integration); `_clear_track_state()` resets it.
- `ipc_manager._record_if_new` passes it to `record_play(..., duration_secs=...)`.

## 3. Listening time rule

- Per play: `listened = ended_at - played_at`, clamped to `[0, 2400]` (40-min cap guards clock skew and missed stop frames).
- Only rows with non-NULL `ended_at` contribute. The totals payload reports both `listening_secs` and how many of the period's plays were tracked, so the UI can caption the tile "across N of M plays" when they differ.

## 4. API

`GET /api/stats?period=month|year|all[&year=YYYY][&month=1-12]`

- Defaults: current month / current year (server-local timezone defines period boundaries and buckets). `year`/`month` select past periods — the hook the future story recap uses.
- Invalid params → 400 (mirror `/api/mqtt/test` error shape).
- Soft-deleted rows excluded everywhere (`deleted_at IS NULL`).

Response (single blob; all modules render from one fetch):

```json
{
  "period": {"kind": "year", "year": 2026, "start": 1767225600, "end": 1798761600},
  "totals": {"plays": 412, "unique_tracks": 180, "unique_artists": 74,
              "listening_secs": 91234, "listening_tracked_plays": 300},
  "top_artists": [{"artist": "M83", "plays": 31, "art_path": "art/123.jpg"}],
  "top_tracks":  [{"title": "Midnight City", "artist": "M83", "plays": 12, "art_path": "art/123.jpg"}],
  "plays_over_time": {"bucket": "day", "buckets": [{"key": "2026-07-01", "plays": 4}]},
  "genres":  {"covered": 350, "total": 412, "top": [{"genre": "Rock", "plays": 120}]},
  "decades": {"covered": 300, "total": 412, "buckets": [{"decade": 1970, "plays": 80}]}
}
```

- Top lists: top 5, ranked by play count; `art_path` is the most recent play's cached art (nullable).
- Buckets: `day` for month periods, `month` for year and all-time. Zero-count buckets included so charts don't lie by omission.
- Genres/decades: group by exact `genre` string / `(release_year/10)*10`; NULL-enrichment rows excluded, with `covered`/`total` so the UI can note coverage.

Implementation: new `gui/stats.py`, synchronous pure functions over SQLite in the `play_history.py` style (every function takes `db_path` for tests), called from `backend_main.py` via `asyncio.to_thread`.

## 5. UI

- **"Stats" becomes the 4th nav item** (`insights` icon) in `_layout.html`'s `nav_items`, with a `/stats` page route (setup-wizard gate applies, like every page).
- Layout, top to bottom: period toggle (This month / This year / All time) → four headline tiles (plays, unique artists, unique tracks, listening time with tracked-plays caption) → top artists + top tracks ranked lists side by side (art thumbnails, count bars) → plays-over-time chart → genres + decades row.
- `stats.js` page IIFE (existing pattern): fetch `/api/stats` on load and on toggle change; charts are CSS-width bars and tiles, no library; glass-panel styling throughout.
- Empty states: fresh install ("No plays yet"), zero-coverage genre/decade modules ("No genre data yet — it accrues as tracks are identified").

## 6. Last.fm forward-compatibility (not built now)

| Scrobble need | Covered by |
|---|---|
| `artist`, `track` (required) | existing columns |
| `timestamp` = track start, Unix UTC (required) | existing `played_at` |
| `album`, `duration` (optional) | existing `album`, new `duration_secs` |
| Eligibility: track > 30s, played ≥ half duration or ≥ 4 min | `duration_secs` + (`ended_at - played_at`) |

A future scrobbler is a pure consumer of the `plays` table; no further schema work anticipated.

## 7. Testing

- `gui/tests/test_stats.py`: seeded temp SQLite covering each aggregation, period boundaries, listening-time cap/exclusion rules, and NULL-enrichment coverage counts.
- Endpoint test (TestClient, existing pattern): param validation + response shape.
- `test_ipc_status.py`-style tests: ended_at stamped on track change and on empty-title; NULL preserved across restart simulation; idempotent stamping.
- Core test: `duration_secs` flows through the normalized shapes (`_identify_shazam` → None, `_audd_to_normalized` → mapped) and `_handle_match` into the status frame.

## Out of scope

- Story-style yearly recap UI (later; consumes `period=year&year=N`).
- Last.fm auth/scrobbling itself.
- Back-dating `played_at` to true needle-drop time (identification latency ~10–20 s is fine for scrobbles).
- Estimating listening time for pre-feature rows.

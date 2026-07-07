# Album / Edition Intelligence — Design

**Date:** 2026-07-07
**Scope:** Automatic album-edition reconciliation within listening sessions (regular vs deluxe), a manual album-correction picker on History, and a Top Albums stats module — all designed so the future Last.fm scrobbler needs no scrobble editing.

## Decisions made during brainstorming

- **Auto rule: superset wins, both directions.** Within a same-artist session run, the most-qualified edition variant (e.g. Deluxe) becomes the run's album; earlier AND later plays are rewritten live as evidence arrives.
- **Manual edit: iTunes picker, user picks scope.** Candidates fetched live from iTunes with cover art; free-text fallback; a checkbox applies the fix to this play or the whole run; manual edits are locked against auto-rewrites.
- **Last.fm compatibility (constraint only, nothing built):** there is no API to edit a submitted scrobble (web-only Pro feature), but submissions may be backdated up to 14 days. The future scrobbler therefore **submits a play only after its session closes**, reading the album at submission time. Submission-delay/backdating knobs will live on a future Last.fm config page. Corrections made after submission affect local stats only.
- Approach chosen: live in-pipeline reconciliation (no batch-at-session-end, no proof-by-containment iTunes verification — the latter is a possible future refinement).

## 1. Edition-variant matching (pure functions, `gui/reconcile.py`)

**Base title.** `base_title(album)` casefolds, collapses whitespace, then repeatedly strips a *trailing qualifier* until stable:

- a trailing bracketed group — `(…)` or `[…]` — whose text contains an edition marker, or
- a trailing dash suffix (` - …`, en/em dash too) whose text contains an edition marker.

**Edition markers** (case-insensitive, matched as whole words): `deluxe`, `super deluxe`, `expanded`, `remaster`, `remastered`, `anniversary`, `bonus track`, `bonus tracks`, `special edition`, `collector's edition`, `collectors edition`, `legacy edition`, `definitive edition`, `extended`, `reissue`, `re-issue`, `edition`, `version`. A four-digit year alone also qualifies (e.g. `(2019 Remaster)`, `[2014]`).

Deliberately **not** markers: `live`, `acoustic`, `demos`, `unplugged` — those are genuinely different albums and must never merge with the studio release. Likewise **possessive re-recording qualifiers** — any qualifier matching `…'s version` (regex `\w+['’]s\s+version`, e.g. `Taylor's Version`) — are different recordings, not editions, and never merge; this exception is checked BEFORE the generic `version` marker so it wins.

Examples: `Abbey Road` ≡ `Abbey Road (Super Deluxe Edition)` ≡ `Abbey Road - 50th Anniversary` ≡ `Abbey Road (Deluxe Edition) [2019 Remaster]`; `At Folsom Prison (Live)` ≢ `At Folsom Prison`; `1989 (Taylor's Version)` ≢ `1989` — but `1989 (Taylor's Version) [Deluxe]` ≡ `1989 (Taylor's Version)` (the deluxe qualifier still strips; the possessive one never does).

**Winner.** Among a run's plays sharing a base title, the winning album string is the one with the greatest raw length (most qualifiers = the superset edition); ties break to the most recently played. Deterministic; no network.

## 2. Session runs and the rewrite

- **Run** = the triggering play plus its same-artist neighbours (exact artist string) chained while the gap between CONSECUTIVE SAME-ARTIST plays is < `SESSION_GAP_SECS = 1800`. An interleaved play by another artist does not split the chain — deliberate, so a one-off misidentification mid-session can't fracture the run. Soft-deleted rows are ignored.
- `reconcile_album(play_id, db_path=None)` runs after each new play is recorded (`ipc_manager._record_if_new`, via `asyncio.to_thread`, wrapped in try/except — a reconcile failure must never block or crash recording).
- Within the run, plays are grouped by base title; for the triggering play's group, every play whose album differs from the winner is UPDATEd to it — **except locked rows** (`album_locked = 1`), which neither vote nor get rewritten.
- Different base titles never merge: two different albums by the same artist in one session stay separate groups.
- Auto-rewrites change the album string only; cached art is left alone (it is track-level and almost always the same cover family).

## 3. Schema

One new nullable column on `plays` via the existing `PRAGMA table_info` migration pattern: `album_locked INTEGER` (NULL/0 = auto-managed, 1 = manually set). New helper `play_history.set_album(play_id, album, locked, db_path=None)`.

## 4. Manual correction

**API:**
- `GET /api/plays/{play_id}/album-candidates` → 404 if the play doesn't exist; otherwise queries iTunes Search (`term=<artist> <title>`, `entity=song`, `limit=25`), deduplicates by `collectionName`, and returns up to 10 `{album, art_url}` (artwork upscaled to 1000x1000 like the engine does) plus `{"current": <album>}`. Network/timeout errors return an empty candidates list (the UI then offers free-text only). Endpoint isolated so tests can stub the iTunes call.
- `POST /api/plays/{play_id}/album` body `{album: str, art_url: str|null, apply_to_run: bool}` → 400 on empty album, 404 on unknown play. Sets `album` + `album_locked=1` on the play; when `apply_to_run`, the same album+lock is applied to **every** play in the contiguous same-artist run (any base title, including previously locked rows — an explicit user action outranks earlier locks; the run is computed by `reconcile.py`'s run detection). When `art_url` is provided, the existing `_download_and_store_art` refreshes the 64px cached art for each updated play. Response: `{"status": "ok", "updated": N}`.

**UI (History page):** a pencil icon next to the existing delete ✕ on row hover opens a glass-panel modal (same pattern as the wizard's MQTT popup): candidate list as selectable rows with cover thumbnails, a free-text input (choosing it means no art refresh), a checkbox "Apply to the whole session run", and Save/Cancel. On save: the edited row's album text updates in place and a toast reports "Album updated (N plays)"; sibling rows refresh on the next page load. No edit control on the dashboard's Recent list.

## 5. Stats: Top Albums

- `gui/stats.py` gains `top_albums`: group by `(album, artist)` where `album IS NOT NULL AND album != 'Unknown Album'`, top 5 by play count, `art_path` from the group's most recent play (same subquery pattern as top artists), plus `covered`/`total` so the UI can note "n of m plays have album data".
- UI: the ranked-lists grid becomes three columns on desktop (`md:grid-cols-3`): Top artists / Top albums / Top tracks, same row markup. Payload key `top_albums` sits alongside the existing modules.
- Because reconciliation unifies editions, deluxe and regular plays count as one album — which is what makes this module trustworthy.

## 6. Testing

- **Normalizer table tests** (pure): the example equivalences above, the live/acoustic non-merges, the `Taylor's Version` non-merge (including the curly-apostrophe form and the `[Deluxe]`-on-top-of-Taylor's-Version case), repeated-qualifier stripping, dash variants, year-only brackets.
- **Winner tests**: longest-wins, tie→most-recent.
- **Run/rewrite tests** (seeded SQLite): bidirectional rewrite in a run; gap > 30 min splits runs; artist change splits runs; locked rows neither vote nor rewrite; different base titles untouched; soft-deleted ignored; reconcile failure doesn't propagate (stubbed exception).
- **Endpoint tests**: candidates (iTunes stubbed: dedupe, cap at 10, error → empty), album POST (validation, single vs run scope, lock override on explicit run apply, art task fired when art_url given).
- **Stats tests**: top_albums grouping, Unknown-Album/NULL exclusion, coverage counts.
- **UI**: manual browser verification of the modal flow (candidates render, free-text path, run scope, toast).

## Out of scope

- Proof-by-containment edition verification (per-candidate track-list lookups).
- Editing artist/title (album only), MusicBrainz IDs.
- The Last.fm scrobbler itself and its config page (submission delay/backdating settings live there when built).
- Re-running reconciliation over pre-existing history (it engages for new plays; old rows can be fixed manually).

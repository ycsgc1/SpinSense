# Wave 1 — Recognition Status Indicators + Delete Scrobble

**Date:** 2026-06-07
**Status:** Approved design, pending implementation plan
**Scope:** Two post-1.0 fixes from a week of production use — surfacing recognition
phases on the dashboard (#2) and deleting a scrobble from history (#3, including the
art-file cleanup). Larger items (#4 HA track-change toggle, #5 Last.fm, #6 release
reconciliation, #7 metadata edit) are out of scope and tracked separately.

---

## Background / motivating problems

After a week running in production (remote access via Nginx Proxy Manager → Authentik;
live streaming fixed by enabling NPM's "Websockets Support" checkbox):

1. **Silent recognition failures looked like a freeze.** When Shazam returns no match,
   the engine only prints `❌ Could not identify track`
   ([core_engine.py:510](../../../core/core_engine.py)) — it does not clear state or
   publish anything. `in_song` stays `True` from the *previous* track, so the UI keeps the
   old song's art spinning while a new, unidentifiable song plays. The user thought the app
   was broken when in fact recognition had silently given up on one track.

2. **No way to remove a bad/duplicate scrobble** from the History tab.

---

## Feature #2 — Recognition status indicators

### Goal

Make the dashboard honestly reflect what the recognition pipeline is doing, including the
previously-invisible "couldn't identify" outcome, and give the user a manual rescan control
for stubborn tracks.

### Phase model

A single machine-readable `phase` enum drives the UI. Six **dashboard-visible** phases plus
one **backend-only** phase:

| phase | glow behind record | caption | emitted when |
|---|---|---|---|
| `listening` | grey **breathe** | "Waiting for the needle…" | idle, between tracks, after silence, after a failed ID |
| `scanning` | green **pulse** | "Listening to the track…" | recording the 5 s sample |
| `identifying` | blue (`#5fb4ff`) **pulse** | "Identifying…" | first Shazam attempt in flight |
| `playing` | steady purple | track title / artist / album | match found |
| `retrying` | yellow (`#fcd34d`) **flash** | "Couldn't catch it — retrying…" | during the 2 auto-retries |
| `no_match` | red (`#ff5451`) **flash** → reverts to `listening` | "Couldn't identify this one" | retries exhausted |
| `stopped` *(backend-only)* | — (renders as `listening`) | — | silence timeout; reserved for HA #4 |

### Behavior rules

- **Auto-retry:** on a missed first attempt, take up to **2 additional fresh 5 s samples**
  (`retrying`) before declaring `no_match`.
- **Recovery, not stop:** after `no_match`, revert to `listening` and keep monitoring — the
  next track is caught normally. A failed ID never goes to `stopped`.
- **Back-off:** after `no_match`, do **not** immediately re-scan the same unidentifiable
  audio. Wait for a fresh audio onset (RMS drops below threshold then rises again — the gap
  between vinyl tracks) before scanning again, so we don't hammer Shazam.
- **Stale-art fix:** the moment the engine leaves `playing`, the frontend clears the
  now-playing art and title. This is the core fix for problem #1.
- **`stopped` semantics:** SpinSense has only the mic — it cannot distinguish "turntable
  powered off" from "silence." The resting state is always `listening`. `stopped` remains a
  backend event (the existing silence timeout) used to feed the Home Assistant "stopped"
  notification (#4, later). The glow-off "Stopped" visual is designed but parked until a real
  "player off" signal exists (e.g. a smart-plug power sense).

### Manual "Scan again" control

- A pill button **directly under the vinyl** (chosen layout: Option A), always visible.
- Forces an immediate fresh recognition cycle, clearing the back-off gate.
- **Reuses the existing engine command socket** `/tmp/spinsense-cmd.sock` — the same channel
  calibration already uses via `_send_cmd()` ([backend_main.py:25](../../../gui/backend_main.py)).

### Implementation notes

**Engine — [core/core_engine.py](../../../core/core_engine.py):**
- Add a `phase` field to the `live_status` payload published over the UDS (currently built
  around lines 567–583). Thread the current phase through the publish helper so every frame
  carries it.
- Publish transitions in `audio_monitor_loop()` / `recognize_audio()`:
  - `scanning` immediately before recording the 5 s sample (~line 455).
  - `identifying` before the first `shazam.recognize()` call (~line 466).
  - On match (`if 'track' in out`, ~line 468) → existing track publish, phase `playing`.
  - On miss, loop up to 2 more samples with phase `retrying`; if all fail, replace the bare
    print at line 510 with: publish `no_match` (clears track), set a back-off flag, then
    revert to `listening`.
- Add a `rescan` case to the engine's command handler (alongside `start_calibration` /
  `get_calibration` / `clear_calibration`): clears the back-off gate and forces a scan.

**Backend — [gui/backend_main.py](../../../gui/backend_main.py):**
- New `POST /api/rescan` → `_send_cmd({"cmd": "rescan"})`, returning the engine reply; 503
  when the engine is unreachable (mirror the calibration endpoints, lines 241–281).
- No change to `/api/status` (HA polls `manager.last_status`); `phase` rides along.

**IPC — [gui/ipc_manager.py](../../../gui/ipc_manager.py):**
- `ConnectionManager.broadcast` already forwards the whole payload and caches `last_status`
  (line 42); `phase` is carried through with no structural change. A non-`playing` frame
  carries no track, so `_record_if_new` (line ~115) still records nothing on failure.

**Frontend shell — [gui/static/shell.js](../../../gui/static/shell.js):**
- `handleFrame` (lines 32–41): read `payload.phase`; map it to the engine-pill state/label
  (extend `LABELS`). Backward-compatible: if `phase` is absent, fall back to today's
  track-presence logic.

**Frontend dashboard — [gui/static/dashboard.js](../../../gui/static/dashboard.js)
+ [templates/dashboard.html](../../../gui/templates/dashboard.html)
+ [static/styles.css](../../../gui/static/styles.css):**
- `handleFrame` (lines 113–164): switch the vinyl glow class and the status caption on
  `payload.phase`; clear art/title when leaving `playing`.
- Add glow classes + `breathe`/`pulse`/`flash` keyframes to `styles.css` (values from the
  approved mockup). Apply to the vinyl element / its outer ring (dashboard.html line ~15).
- Add a small **status caption** element near the title (line ~30) for the phase text.
- Add the **"⟳ Scan again"** pill under the vinyl; click → `POST /api/rescan`, with brief
  disabled/feedback state.

---

## Feature #3 — Delete a scrobble (with Undo + art cleanup)

### Goal

Let the user remove a scrobble from the History tab, with a forgiving 5 s Undo, and reclaim
the deleted row's cached art without leaving orphaned files.

### Approach: soft-delete + purge sweep

Soft-delete is chosen over hard-delete because the approved UX is **immediate removal + Undo**.
It makes Undo trivial, preserves the cached art during the Undo window, and keeps row
ordering/identity intact. A purge sweep then reclaims storage so nothing dangles.

- **Schema:** add `deleted_at INTEGER` (nullable) to the `plays` table
  ([play_history.py:31](../../../gui/play_history.py)). Migration: `ALTER TABLE plays ADD
  COLUMN deleted_at INTEGER`, guarded for existing DBs via a one-time column check in
  `init_db` (the table uses `CREATE TABLE IF NOT EXISTS`).
- **Reads:** `recent_plays` / `count_plays` (lines 82–102) filter `WHERE deleted_at IS NULL`.
- **Delete:** `delete_play(id)` sets `deleted_at = now`.
- **Restore:** `restore_play(id)` sets `deleted_at = NULL`.

### Purge sweep (in scope — closes the art-cleanup loose end)

`purge_deleted(grace_seconds)` in [play_history.py](../../../gui/play_history.py):
1. Select rows where `deleted_at IS NOT NULL AND deleted_at < (now - grace_seconds)`.
2. Collect their `art_path` values.
3. Hard-`DELETE` those rows.
4. For each distinct `art_path`, unlink the file **only if no remaining row references it**
   (album art may be shared/deduped across plays — never unlink art still in use). Unlink only
   paths under `ART_DIR`.

Invocation:
- **On startup** in `lifespan` ([backend_main.py:60](../../../gui/backend_main.py)) — rows
  soft-deleted in a prior session are well past the Undo window.
- **Periodically** via a lightweight background task (~every 30 min) so a long-running
  instance reclaims art without a restart.
- **Grace window** comfortably larger than the 5 s Undo (default **120 s**) so an in-flight
  Undo is never purged.

### API — [gui/backend_main.py](../../../gui/backend_main.py)

- `DELETE /api/plays/{id}` → `to_thread(delete_play, id)`; 404 if no such row.
- `POST /api/plays/{id}/restore` → `to_thread(restore_play, id)`; 404 if no such row.

### UI — [gui/static/history.js](../../../gui/static/history.js) + [templates/history.html](../../../gui/templates/history.html)

- `rowHtml` (lines 49–63) gains a hover-revealed **✕** button carrying `data-id` (`row.id`
  already present).
- Click ✕ → optimistically remove the `<li>`, call `DELETE`, show a 5 s **"Undo"** toast.
  Undo → `POST …/restore` and re-insert the row in place. Toast timeout → nothing further
  (the row is soft-deleted and the purge sweep will reclaim it after the grace window).
- Scoped to the History tab. The dashboard "Recent" list does not get delete in Wave 1.

---

## Error handling

- `POST /api/rescan` when the engine is down → 503 (matches calibration endpoints).
- `DELETE` / `restore` on an unknown id → 404.
- Missing `phase` on a WS frame → frontend degrades to today's track-presence behavior.
- `purge_deleted` never unlinks an `art_path` still referenced by a live row, and tolerates a
  missing file (already gone) without error.

## Testing

- **Engine** ([core/tests](../../../core/tests)): a missed identification triggers exactly 2
  retries, then emits `no_match` and reverts to `listening`; back-off suppresses an immediate
  re-scan until a new audio onset; `rescan` command clears the gate.
- **History** ([gui/tests](../../../gui/tests)): `delete_play` soft-deletes and is excluded
  from `recent_plays`/`count_plays`; `restore_play` brings it back; migration adds the column
  on a pre-existing DB; `purge_deleted` hard-deletes only past-grace rows, unlinks orphaned
  art, and preserves art still referenced by a live row.
- **Frontend:** dogfood the six phase glows + captions and the Undo flow against the live app.

## Out of scope (tracked for later waves)

- #4 HA track-change re-trigger toggle (will consume the `stopped`/`playing` events).
- #5 Last.fm scrobbling (needs a listening-duration data model).
- #6 release/edition reconciliation; #7 manual metadata edit (a "pick the correct release"
  picker — the manual counterpart to #6).

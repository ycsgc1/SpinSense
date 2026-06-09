# Detection Tuning — dB floor, rescan-on-dip fix, escalating rescans

**Date:** 2026-06-09
**Status:** Approved (design)

## Problem

Field testing surfaced three issues with audio detection:

1. **The dB threshold floor (−80 dB) is too high.** Users run the same recommended
   line-level adapter we do, which has a low noise floor. Quiet music can sit around
   −80 dB while still being clearly above that noise floor, but the threshold slider
   hard-clamps at −80 dB so it can't be set low enough to distinguish quiet music from
   silence.

2. **Over-aggressive rescanning on brief dips (core bug).** When a song dips below the
   threshold for ~1 second and comes back, the track is re-identified almost
   immediately — even when the silence setting is configured much longer. Root cause:
   `_scan_decision` triggers a rescan whenever volume returns above threshold and
   `silence_counter > 0`, and the `New_Song_Silence_Interval` setting is loaded but
   **never read** anywhere in the engine. So the user's "make it wait longer" setting
   does nothing.

3. **No graceful escalation when identification fails.** Sample length is fixed. When
   Shazam can't identify a track, the engine retries 3 times immediately at the same
   length, then backs off. There is no wait-then-retry-with-a-longer-sample behavior.

## Goals

- Allow the threshold floor to go down to −120 dB.
- Make a brief dip below threshold *not* trigger a rescan; only a sustained gap should.
  Give `New_Song_Silence_Interval` its intended meaning.
- After a failed identification, wait and rescan with a progressively longer sample
  (base × 1, × 2, × 3) before giving up.

## Non-goals

- Changing the recognition backend (Shazam) or the metadata enrichment path.
- Across-time / non-blocking escalation (considered as Approach B, rejected — see below).
- Auto-migrating users' existing `config.json` values (their tuning is theirs; only
  code-level defaults are aligned).

---

## Design

### 1. Lower the dB floor to −120 (centralized)

Today the floor is a magic number duplicated across the codebase:

- `gui/static/db_utils.js:9-10` — the canonical `rmsToDb` clamp + zero-RMS return (−80)
- `gui/static/settings.js:16` — `const DB_MIN = -80`
- `gui/static/setup.js:60` — `const DB_MIN = -80`
- `gui/static/dashboard.js:40` — `const DB_MIN = -80`
- `gui/templates/settings.html:40,44` — `min="-80"` (2×)
- `gui/templates/setup.html:177,181,213,217` — `min="-80"` (4×)
- `gui/tests/test_db_utils.py` — pins the −80 contract

**Approach: centralize the floor.**

- Add `FLOOR_DB: -120` to `window.SpinSense.db` in `db_utils.js`.
- `rmsToDb` clamps to `FLOOR_DB` (both the `rms <= 0` early return and the
  `Math.max(...)` clamp).
- Replace each local `DB_MIN = -80` with a reference to `SpinSense.db.FLOOR_DB`
  (db_utils.js loads first via `_layout.html`, so it's always available).
- HTML `min` attributes can't read JS, so set them to `-120` directly **and** assign
  `input.min = SpinSense.db.FLOOR_DB` on page load so there's a single source of truth.
- Update `test_db_utils.py` to pin −120.

**Side effect (accepted):** the live level meters compute fill as
`(db − floor) / (0 − floor)`. Widening the floor to −120 stretches the meter — a
−80 dB signal moves from 0% to ~33% fill. This gives more resolution in the usable
range and is the desired behavior. The meter floor and the threshold floor stay
unified at −120 (no split floors).

### 2. Fix the rescan-on-dip bug

Replace the `silence_counter > 0` trigger with a gate on `New_Song_Silence_Interval`,
producing a three-tier silence model:

| Dip duration | Behavior |
|---|---|
| `< New_Song_Silence_Interval` | Ignore — same song keeps playing. Reset `silence_counter`, no rescan. |
| `New_Song_Silence_Interval ≤ dip < Stopped_Silence_Interval` | Real gap — rescan (re-identify) on the next onset. |
| `≥ Stopped_Silence_Interval` | Playback stopped — clear track, publish `stopped`. |

**`_scan_decision` change** (`core/core_engine.py:658-667`). New signature takes
`new_song_silence`:

```python
def _scan_decision(vol, threshold, in_song, silence_counter, new_song_silence, back_off):
    """Pure: decide what the monitor loop should do this tick.
    Returns 'scan' | 'tick' | 'wait_gap' | 'silence'."""
    if vol > threshold:
        if back_off:
            return "wait_gap"
        if not in_song:
            return "scan"
        if silence_counter >= new_song_silence:
            return "scan"
        return "tick"          # brief sub-threshold dip — resume same song
    return "silence"
```

**Monitor-loop change** (`core/core_engine.py:717-740`): pass
`runtime["new_song_silence"]` into `_scan_decision`. On a `"tick"` decision, reset
`state["silence_counter"] = 0` (clears a partial dip count when the song resumes
before the gap qualifies). The `"silence"` and `"scan"` branches are otherwise
unchanged; `recognize_audio()` already resets `silence_counter` at its end, and the
`stopped_silence` clear path is untouched.

**Default alignment:** `New_Song_Silence_Interval` now has teeth, so reconcile the
disagreeing code defaults — `core_engine.DEFAULT_CONFIG` (2.0) and the Pydantic
`config_manager` model (10.0) — to a single agreed default of **3.0 s**. Existing user
`config.json` values are left as-is.

### 3. Escalating rescans after a failed ID (Approach A — reuse the retry loop)

Keep escalation inside one `recognize_audio()` call (`core/core_engine.py:608-633`).
The existing `RECOGNIZE_ATTEMPTS = 3` loop becomes an escalating ladder:

- attempt 0 → `base × 1` seconds
- attempt 1 → `base × 2` seconds
- attempt 2 → `base × 3` seconds
- a configurable `Rescan_Wait_Interval` (default **5.0 s**) `await asyncio.sleep` runs
  *between* attempts (not before the first, not after the last).

`_capture_sample()` takes an explicit sample length (instead of reading
`runtime["sample_len"]` directly) so the loop can pass `base * (attempt + 1)`. A sane
upper clamp is applied so an unusually large base can't produce an absurd recording
length.

Behavior:

- Normal songs identify on attempt 0 at base length — **unchanged** path and timing.
- Only failures climb the ladder.
- If all three escalating attempts fail, the existing `_clear_track_state(set_backoff=True)`
  + `no_match` publish runs as today; the `back_off` gate then waits for a fresh onset.

**Accepted trade-off:** Approach A blocks the monitor loop longer on a *failing* track
(≈ `5 + 10 + 15` s recording + 2 × 5 s waits ≈ 40 s at base 5 s) because the input
stream is stopped during recognition. This was chosen over Approach B (across-time,
non-blocking escalation via monitor-loop state) for simplicity and because it directly
matches "consecutive scans with waits and growing samples." Approach B is rejected for
this iteration.

**New setting `Rescan_Wait_Interval`:** added to `DEFAULT_CONFIG`, the Pydantic model
(with validation bounds), and the settings UI (number input + help tooltip, matching
the existing interval inputs).

---

## Testing

- `_scan_decision` unit tests covering all three tiers: brief dip (< interval) →
  `tick`; qualifying gap (≥ interval) → `scan`; new onset (`not in_song`) → `scan`;
  `back_off` → `wait_gap`; sub-threshold → `silence`.
- Monitor-loop test: a sub-threshold dip shorter than `New_Song_Silence_Interval`
  resets `silence_counter` and does **not** rescan.
- Escalation test: the ladder produces sample lengths `base`, `2·base`, `3·base`, and
  `Rescan_Wait_Interval` is applied between attempts but not after the last.
- `test_db_utils.py`: updated to pin the −120 floor (clamp + zero-RMS return).

## Files touched (anticipated)

- `core/core_engine.py` — `_scan_decision`, monitor loop, `_capture_sample`,
  `recognize_audio`, `DEFAULT_CONFIG`, runtime mirror.
- `gui/config_manager.py` — Pydantic model: default alignment + new
  `Rescan_Wait_Interval`.
- `gui/static/db_utils.js` — `FLOOR_DB` constant + clamp.
- `gui/static/settings.js`, `setup.js`, `dashboard.js` — reference `FLOOR_DB`.
- `gui/templates/settings.html`, `setup.html` — `min` attributes + new setting input.
- `gui/tests/test_db_utils.py` — −120 contract.
- New/updated engine unit tests.
- `CHANGELOG.md` — `[Unreleased]` entry.

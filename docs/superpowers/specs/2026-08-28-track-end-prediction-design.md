# Track-End Prediction + the Play Clock (Last.fm foundation)

**Date:** 2026-08-28
**Status:** Approved (design)

## Problem

Silence-gap detection misses some track transitions. On records where the gap
between songs is very short or very quiet, the gap never reaches
`New_Song_Silence_Interval`, so `_scan_decision` returns `tick` ("same song
still playing") and SpinSense keeps reporting the previous track for the rest
of the side.

Lowering `New_Song_Silence_Interval` is not the answer: at the settings that
would catch these gaps, ordinary quiet passages *inside* songs start
triggering rescans. That burns Shazam calls on audio we already identified —
the one budget we are unwilling to spend.

## Insight

We already know how long the track is. `fetch_itunes_metadata()` returns
`trackTimeMillis` and `_audd_to_normalized()` maps `durationInMillis`; both
land in `state["duration_secs"]` and the status frame (shipped 1.6.0.0).

So instead of listening *harder* for a gap, we can **predict when the song
should be over** and, if that moment passes with no gap detected, spend one
deliberate rescan asking "what are we actually in right now?".

That is strictly cheaper than lowering the threshold: the extra calls are
bounded per play (see §4) and only happen on tracks whose gap we already
missed.

## Goals

- Catch missed transitions without touching the silence thresholds.
- Hard-bound the extra Shazam traffic.
- Produce a per-play **play clock** (true start, join offset, duration) so a
  Last.fm scrobbler is auth + POST and nothing else.

## Non-goals

- Last.fm auth or submission itself (next feature; this is its foundation).
- Changing `played_at`'s meaning. It stays "when we identified it"; the true
  start lands in a new column, so existing rows, stats and history ordering
  are untouched.
- Estimating anything for pre-feature rows.

---

## 1. The play clock

A new pure module, `core/track_clock.py`. No I/O, no clocks of its own —
every function takes `now` explicitly, so tests drive time directly. This
mirrors `_scan_decision` / `_silence_step`: the engine keeps the mutable
instance, the module keeps the arithmetic.

```
TrackClock(
    duration_secs,      # canonical length, or None => prediction disarmed
    position_secs,      # playhead at the anchor
    anchor_mono,        # monotonic clock at the START of the winning capture
    anchor_wall,        # unix secs at the same instant
    position_source,    # "shazam_offset" | "assumed_start"
    grace_secs,
    rescans,            # end-checks spent on this track
    deadline_mono,      # None => disarmed
)
```

Anchoring at the **start of the capture** (not at `_handle_match` time) keeps
network latency out of the estimate: `_capture_sample()` stamps both clocks,
and only the winning attempt's stamp is used.

### Where the playhead comes from

`shazamio.recognize()` returns the raw Shazam response, whose
`matches[0].offset` is the position in the reference recording that our
sample matched. `_identify_shazam` currently discards it.

We capture it as `match_offset_secs` and treat it as the playhead at the
start of the capture. **Its exact semantics are unverified** (the open
question from the 2026-07-12 lyrics design; the bench spike never ran). If
it turns out to mark the *end* of the sample instead, every prediction is
early by `Song_Sample_Length` — 5 s, comfortably inside a 20 s grace window.
Both readings are safe; the spike would only tighten accuracy.

Sanity gate: an offset that is negative, non-finite, or exceeds
`duration + 5 s` is discarded and the clock falls back to
`position_secs = 0.0, position_source = "assumed_start"` — i.e. "assume we
caught it from the top", which makes the prediction fire *late*. Late is the
safe direction: a late prediction wastes nothing, an early one wastes a call.

The offset matters most exactly where this feature is needed: after an
end-check finds a new track, we joined that track mid-song. Without the
offset we would assume it just started and mispredict its end by however
late we were. With it, the next prediction is right.

## 2. The prediction

```
remaining      = duration_secs - position_secs
expected_end   = anchor_mono + remaining
grace          = min(max(Track_End_Grace_Secs, 0.10 * duration_secs), 60)
deadline_mono  = expected_end + grace
```

`grace` takes the **larger** of a flat floor and 10 % of the track, capped at
60 s. The flat floor dominates short tracks (a 2:30 song: 20 s beats 15 s);
the percentage dominates long ones (a 9-minute side-long track gets 54 s
rather than 20 s). Taking the larger is the conservative choice — it fires
later, which spends fewer calls.

`duration_secs is None` => `deadline_mono is None` => permanently disarmed
for that play. No duration, no prediction.

## 3. Firing

In `audio_monitor_loop`, after `_scan_decision`:

| decision | behaviour |
|---|---|
| `scan` | unchanged — a real gap was detected, the normal path is better |
| `tick` | if the clock is armed and `now >= deadline_mono`, run an **end-check rescan** |
| `silence` | same, but only while the gap has *not* yet qualified |
| `wait_gap` | never (back-off means the audio is unidentifiable) |

Checking on both `tick` *and* `silence` is deliberate: the motivating case is
a gap too short to qualify, and it surfaces as either.

Standing down once the gap *has* qualified matters more than it looks. The
ordinary path will scan the instant audio returns, so an end-check there buys
nothing — and without the gate, the runout groove at the end of every side
(silence, against a track whose predicted end passed minutes ago) would spend
the entire rescan budget on the sound of nothing.

The end-check reuses the existing recognition path, with one change:

**`recognize_audio(preserve_on_miss=True)`.** Today a failed identification
calls `_clear_track_state(set_backoff=True)`, wiping "now playing". That is
right for a fresh onset — but an end-check runs mid-song against a track we
already have. A miss there must not destroy a good result. With
`preserve_on_miss`, a miss leaves the track intact and only defers the clock.

## 4. The call budget

This is the constraint the whole design serves. Four gates:

1. **Disarmed without a duration.** No `duration_secs`, no end-checks, ever.
2. **At most `MAX_END_RESCANS` (3) per track.** The counter lives on the
   clock and is *inherited* across same-track re-arms (see below). On
   exhaustion the clock disarms until real silence resets the track.
3. **Exponential deferral.** Each end-check that does not find a new track
   re-arms at `now + grace * 2^n`, capped at 300 s.
4. **Never during calibration, never while `back_off` is set, never
   concurrent** — it goes through the same single-recognition-at-a-time path
   as `force_scan`.

The counter-inheritance in (2) is the important one. `_handle_match` builds a
fresh clock on every match. If an end-check re-confirms the same track and we
built a naive fresh clock, `rescans` would reset to 0 and a track whose
duration metadata is simply wrong (iTunes returning the single edit for an
album version) would re-scan every `duration + grace` forever. So
`start_clock(previous=...)` inherits and increments the counter, but **only
when the match came from an end-check** — a same-track confirmation after a
real detected gap means detection is working and resets the budget.

Worst case for a track we keep failing to resolve: 3 extra calls, then
silence. Typical case: 0.

## 5. The ledger (what makes Last.fm cheap)

Frames gain a `play_clock` block beside `track` (additive; the HACS
integration ignores unknown keys):

```json
"play_clock": {
  "started_at": 1756338000,
  "join_offset_secs": 42,
  "duration_secs": 213,
  "position_source": "shazam_offset"
}
```

`started_at = anchor_wall - position_secs` — when the track actually began on
the platter. `join_offset_secs = position_secs` — how far in we joined.

Two new nullable columns on `plays` (same idempotent `ALTER TABLE` migration
as the 1.0 and 1.6 additions):

| Column | Meaning |
|---|---|
| `started_at` | true track start, unix secs. NULL => unknown, use `played_at` |
| `join_offset_secs` | seconds into the track when we started hearing it. NULL => unknown, treat as 0 |

`played_at` keeps its current meaning, so `stats.py`, `reconcile.py`, history
ordering and every existing row are untouched.

### The scrobbler seam

`play_history.scrobble_candidates(since, limit)` returns closed plays with
the eligibility maths already applied:

```
timestamp = COALESCE(started_at, played_at)          # Last.fm track.scrobble
listened  = (ended_at - played_at) - COALESCE(join_offset_secs, 0)
eligible  = duration_secs > 30 AND (listened >= duration_secs / 2 OR listened >= 240)
```

That is Last.fm's published rule verbatim. The remaining scrobbler work is an
auth flow and an HTTP POST — no further schema or engine work.

## 6. Config

Two new `Audio` fields (defaults mirrored in both `core_engine.DEFAULT_CONFIG`
and `config_manager.AudioConfig`, per the existing contract comment):

| Field | Default | Meaning |
|---|---|---|
| `Track_End_Detection` | `true` | master switch for the whole mechanism |
| `Track_End_Grace_Secs` | `20.0` | flat floor of the grace window |

`MAX_END_RESCANS`, the 10 % share and the 60 s / 300 s caps stay code
constants — they are safety rails, not preferences.

Both are hot-reloadable through the existing `runtime` dict and settings
form, like every other Audio field.

## 7. Testing

`core/tests/test_track_clock.py` — pure, no engine import:
offset extraction and every sanity-rejection path; the grace formula at both
ends of the max(); disarm on missing duration; arm/fire/defer transitions;
counter inheritance and exhaustion; the exponential ladder and its cap; the
`play_clock` payload shape.

`core/tests/test_track_end_engine.py` — wiring:
`_identify_shazam` carries the offset through; `_handle_match` builds a clock
anchored to the winning capture; `_clear_track_state` drops it;
`recognize_audio(preserve_on_miss=True)` leaves state intact on a miss.

`gui/tests/` — `started_at` / `join_offset_secs` round-trip, the migration on
a pre-existing table, `scrobble_candidates` against each limb of the
eligibility rule, and `_record_if_new` reading `play_clock` off the frame.

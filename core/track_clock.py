"""Track-end prediction and the per-play clock.

Two jobs, both pure:

1. **"Is this song over?"** Silence-gap detection misses transitions on records
   whose inter-track gaps are too short or too quiet to reach
   `New_Song_Silence_Interval`. We know how long the track is (iTunes
   `trackTimeMillis` / AudD `durationInMillis`), so we predict when it should
   end and, if that moment passes with no gap detected, spend one deliberate
   rescan instead of lowering the threshold and burning calls on every quiet
   passage.

2. **The play clock.** Where in the track we joined, and when the track really
   started — the ledger a Last.fm scrobbler needs (`gui/play_history.py`
   persists it; see `scrobble_candidates`).

No I/O, no module state, and no clock of its own: every function takes `now`
explicitly so tests drive time directly. `core_engine` owns the single mutable
`TrackClock` and supplies the readings. Same contract as `_scan_decision` and
`_silence_step`.
"""
import math
from dataclasses import dataclass

# Share of the track length used as the grace window, and the flat ceiling on
# it. The engine's configured floor and this share are combined with max() —
# the flat floor governs short tracks, the share governs long ones, and taking
# the larger always fires later, which spends fewer recognition calls.
GRACE_PERCENT = 0.10
GRACE_CAP_SECS = 60.0

# A Shazam offset may overshoot the duration slightly (different masters, our
# sample straddling the end). Beyond this margin we stop believing it.
OFFSET_SANITY_MARGIN_SECS = 5.0

# Hard ceiling on end-check rescans per track. Inherited across same-track
# re-arms so a track with wrong duration metadata can't loop forever.
MAX_END_RESCANS = 3

# Each end-check that doesn't find a new track re-arms further out.
BACKOFF_MULTIPLIER = 2.0
BACKOFF_CAP_SECS = 300.0

POSITION_FROM_OFFSET = "shazam_offset"
POSITION_ASSUMED = "assumed_start"


@dataclass
class TrackClock:
    """Where we are in the current track, and when to doubt that we still are.

    `anchor_mono` / `anchor_wall` are two readings of the same instant — the
    start of the audio capture that produced this match. Anchoring at capture
    start (rather than at match time) keeps recognition latency out of the
    estimate. `deadline_mono is None` means the prediction is disarmed.
    """

    duration_secs: float | None
    position_secs: float
    anchor_mono: float
    anchor_wall: int
    position_source: str
    grace_secs: float
    rescans: int = 0
    deadline_mono: float | None = None


def extract_match_offset(raw) -> float | None:
    """`matches[0].offset` from a raw Shazam response, or None.

    Every layer is optional — this runs against a third-party payload we don't
    control, and a missing offset is a normal outcome, not an error.
    """
    if not isinstance(raw, dict):
        return None
    matches = raw.get("matches")
    if not isinstance(matches, list) or not matches:
        return None
    first = matches[0]
    if not isinstance(first, dict):
        return None
    offset = first.get("offset")
    if isinstance(offset, bool) or not isinstance(offset, (int, float)):
        return None
    if not math.isfinite(offset):
        return None
    return float(offset)


def resolve_position(match_offset_secs, duration_secs) -> tuple[float, str]:
    """(playhead at capture start, how we know it).

    Shazam's `matches[0].offset` is the playhead in the track, measured at the
    start of the sample we submitted — verified on hardware 2026-08-28 by
    scanning one 220 s track twice, at ~1 s (reported 1) and ~184 s (reported
    184). The mid-song reading is the one that proves it: near the top of a
    track a real playhead is indistinguishable from a constant zero.

    Falls back to the top of the track whenever the offset is missing or fails
    the sanity check against the duration. That fallback makes the prediction
    fire *late*, which is the safe direction: a late prediction costs nothing,
    an early one costs a recognition call.
    """
    if match_offset_secs is None or match_offset_secs < 0:
        return 0.0, POSITION_ASSUMED
    if duration_secs is not None:
        if match_offset_secs > duration_secs + OFFSET_SANITY_MARGIN_SECS:
            return 0.0, POSITION_ASSUMED
        return min(float(match_offset_secs), float(duration_secs)), POSITION_FROM_OFFSET
    return float(match_offset_secs), POSITION_FROM_OFFSET


def grace_window(duration_secs, grace_floor_secs: float) -> float:
    """The slack we allow past the predicted end before doubting the track."""
    floor = max(0.0, float(grace_floor_secs))
    if duration_secs is None:
        return floor
    return min(max(floor, GRACE_PERCENT * float(duration_secs)), GRACE_CAP_SECS)


def start_clock(
    duration_secs,
    match_offset_secs,
    anchor_mono: float,
    anchor_wall: int,
    grace_floor_secs: float,
    previous: "TrackClock | None" = None,
) -> TrackClock:
    """Build the clock for a freshly matched track.

    `previous` is passed only when this match re-confirmed the *same* track
    from an end-check. The rescan budget is then inherited and incremented, so
    a track whose duration metadata is simply wrong (iTunes handing back the
    single edit for an album version) exhausts its budget and disarms instead
    of re-scanning every `duration + grace` for the rest of the side. A
    same-track confirmation that came from a genuinely detected gap passes no
    `previous`: detection is working, so the budget resets.
    """
    position, source = resolve_position(match_offset_secs, duration_secs)
    grace = grace_window(duration_secs, grace_floor_secs)
    rescans = previous.rescans + 1 if previous is not None else 0

    clock = TrackClock(
        duration_secs=float(duration_secs) if duration_secs is not None else None,
        position_secs=position,
        anchor_mono=float(anchor_mono),
        anchor_wall=int(anchor_wall),
        position_source=source,
        grace_secs=grace,
        rescans=rescans,
    )
    if duration_secs is not None and rescans <= MAX_END_RESCANS:
        remaining = max(0.0, float(duration_secs) - position)
        clock.deadline_mono = float(anchor_mono) + remaining + grace
    return clock


def should_check_end(
    clock: "TrackClock | None",
    now_mono: float,
    *,
    enabled: bool,
    in_song: bool,
    backing_off: bool,
    gap_qualified: bool,
) -> bool:
    """Whether this tick should spend a rescan asking what's actually playing.

    `backing_off` excludes audio we already failed to identify — re-asking
    about it is the one thing the back-off gate exists to prevent.

    `gap_qualified` (the silence has already lasted `New_Song_Silence_Interval`)
    stands us down: the ordinary path will scan the moment audio returns, so an
    end-check here buys nothing. Without this gate the runout groove at the end
    of a side — silence, but a track whose predicted end has long passed —
    would spend the whole rescan budget on nothing.
    """
    if not enabled or not in_song or backing_off or gap_qualified:
        return False
    if clock is None or clock.deadline_mono is None:
        return False
    return now_mono >= clock.deadline_mono


def defer(clock: "TrackClock | None", now_mono: float) -> None:
    """Push the deadline out after an end-check that didn't find a new track.

    Mutates in place — the engine holds one clock per play. Each deferral
    doubles the window; past `MAX_END_RESCANS` the clock disarms and only real
    silence (which clears the track) will arm a new one.
    """
    if clock is None or clock.deadline_mono is None:
        return
    clock.rescans += 1
    if clock.rescans > MAX_END_RESCANS:
        clock.deadline_mono = None
        return
    step = min(
        clock.grace_secs * (BACKOFF_MULTIPLIER ** clock.rescans),
        BACKOFF_CAP_SECS,
    )
    clock.deadline_mono = float(now_mono) + step


def play_clock_payload(clock: "TrackClock | None") -> dict | None:
    """The `play_clock` block for a live_status frame, or None.

    `started_at` is when the track began on the platter (our anchor, walked
    back by the playhead); `join_offset_secs` is how far in we started
    hearing it. Together with `ended_at` they give a scrobbler exact
    listened-time without it needing to know anything about the engine.
    """
    if clock is None:
        return None
    return {
        "started_at": clock.anchor_wall - int(round(clock.position_secs)),
        "join_offset_secs": int(round(clock.position_secs)),
        "duration_secs": int(clock.duration_secs) if clock.duration_secs else None,
        "position_source": clock.position_source,
    }

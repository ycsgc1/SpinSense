"""Album/edition reconciliation within listening sessions.

Two plays of the same base album ("Abbey Road" vs "Abbey Road (Deluxe
Edition)") inside one same-artist run are unified to a single title. The title
vocabulary and the choice of winner live in `spinsense.albums`, shared with the
engine; what remains here is the SQLite half — finding a run and rewriting it.

Synchronous SQLite (callers wrap in asyncio.to_thread), mirroring
play_history.py's contract.
"""
from play_history import _connect
from spinsense.albums import base_title, pick_winner, shares_credit  # noqa: F401

# A run = contiguous plays by the same artist with gaps under this.
SESSION_GAP_SECS = 1800
# Run detection looks at most this far around the triggering play; a single
# listening session never spans it.
_RUN_WINDOW_SECS = 86400


def _run_rows(conn, play_id: int) -> list[dict]:
    anchor = conn.execute(
        "SELECT id, artist, played_at FROM plays "
        "WHERE id = ? AND deleted_at IS NULL", (play_id,)).fetchone()
    if anchor is None:
        return []
    window = conn.execute(
        # art_path rides along because artwork has to follow whatever album the
        # run settles on; see ipc_manager._settle_run_art().
        "SELECT id, artist, album, played_at, album_locked, album_exclusive, art_path "
        "FROM plays "
        "WHERE deleted_at IS NULL AND played_at BETWEEN ? AND ? "
        "ORDER BY played_at, id",
        (anchor["played_at"] - _RUN_WINDOW_SECS,
         anchor["played_at"] + _RUN_WINDOW_SECS)).fetchall()
    # Not an exact string match: a record's own bonus track is often a duet, and
    # exact matching leaves that one play — the one carrying the evidence about
    # which edition is on the platter — in a session by itself. Filtered here
    # rather than in SQL because the comparison is a parsing question.
    rows = [r for r in window if shares_credit(r["artist"], anchor["artist"])]
    idx = next(i for i, r in enumerate(rows) if r["id"] == anchor["id"])
    lo = idx
    while lo > 0 and rows[lo]["played_at"] - rows[lo - 1]["played_at"] < SESSION_GAP_SECS:
        lo -= 1
    hi = idx
    while hi < len(rows) - 1 and rows[hi + 1]["played_at"] - rows[hi]["played_at"] < SESSION_GAP_SECS:
        hi += 1
    return [dict(r) for r in rows[lo:hi + 1]]


def find_run(play_id: int, db_path: str | None = None) -> list[dict]:
    """The contiguous same-artist session run containing play_id (gaps <
    SESSION_GAP_SECS), ordered by played_at. Empty if the play is missing."""
    with _connect(db_path) as conn:
        return _run_rows(conn, play_id)


UNKNOWN_ALBUM = "Unknown Album"


def _is_unknown(album: str | None) -> bool:
    return not album or album == UNKNOWN_ALBUM


def _adopt_run_album(conn, run: list[dict]) -> int:
    """Give the run's album to plays that never got one.

    The first track of a side has nothing to go on: no album is established
    yet, and iTunes' search fails outright for some titles — "OK Overture"
    returns no results at all. By the second track the record is known, but
    nothing was looking back at the first.

    A run is one record, so once the rest of it agrees, the odd play without an
    album belongs to it. Agreement is judged on `base_title`, not the raw
    string, so a run holding both "OK ORCHESTRA" and "OK ORCHESTRA (Deluxe)"
    still counts as one record — and the adopted name is whichever
    `pick_winner` would settle on, so the run stays uniform.

    Only acts when the run is unanimous, so a session spanning two records
    never has one bleed into the other.
    """
    known = [r for r in run
             if not r["album_locked"] and not _is_unknown(r["album"])]
    if not known or len({base_title(r["album"]) for r in known}) != 1:
        return 0
    album = pick_winner([
        (r["album"], r["played_at"], bool(r["album_exclusive"])) for r in known
    ])
    changed = 0
    for r in run:
        if r["album_locked"] or not _is_unknown(r["album"]):
            continue
        conn.execute(
            "UPDATE plays SET album = ? WHERE id = ? "
            "AND (album_locked IS NULL OR album_locked = 0)",
            (album, r["id"]))
        changed += 1
    return changed


def reconcile_album(play_id: int, db_path: str | None = None) -> int:
    """Unify edition variants of play_id's album across its run, and give the
    run's album to any play that never resolved one. Locked rows neither vote
    nor get rewritten. Returns the number of rows rewritten."""
    with _connect(db_path) as conn:
        run = _run_rows(conn, play_id)
        target = next((r for r in run if r["id"] == play_id), None)
        if target is None or target["album_locked"]:
            return 0

        changed = 0
        base = base_title(target["album"])
        if base:
            group = [r for r in run
                     if not r["album_locked"] and r["album"]
                     and base_title(r["album"]) == base]
            # The third element is the evidence: a play whose track could only
            # have come from a qualified edition upgrades the whole run to it.
            winner = pick_winner([
                (r["album"], r["played_at"], bool(r["album_exclusive"]))
                for r in group
            ])
            for r in group:
                if r["album"] != winner:
                    conn.execute(
                        "UPDATE plays SET album = ? WHERE id = ? "
                        "AND (album_locked IS NULL OR album_locked = 0)",
                        (winner, r["id"]))
                    changed += 1
            if changed:
                run = _run_rows(conn, play_id)   # re-read: albums just changed

        # After unification, so a run holding several editions of one record
        # reads as unanimous rather than as two different albums.
        return changed + _adopt_run_album(conn, run)


def apply_album_to_run(play_id: int, album: str,
                       db_path: str | None = None) -> list[int]:
    """Manual run-wide album set: every play in the run (any base title,
    including previously locked rows — an explicit user action outranks old
    locks) gets `album` and album_locked=1. Returns the updated ids."""
    with _connect(db_path) as conn:
        run = _run_rows(conn, play_id)
        ids = [r["id"] for r in run]
        conn.executemany(
            "UPDATE plays SET album = ?, album_locked = 1 WHERE id = ?",
            [(album, i) for i in ids])
        return ids

"""Album/edition reconciliation within listening sessions.

Two plays of the same base album ("Abbey Road" vs "Abbey Road (Deluxe
Edition)") inside one same-artist run are unified to a single title. The title
vocabulary and the choice of winner live in `spinsense.albums`, shared with the
engine; what remains here is the SQLite half — finding a run and rewriting it.

Synchronous SQLite (callers wrap in asyncio.to_thread), mirroring
play_history.py's contract.
"""
from play_history import _connect
from spinsense.albums import base_title, pick_winner  # noqa: F401  (re-exported)

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
    rows = conn.execute(
        "SELECT id, artist, album, played_at, album_locked, album_exclusive FROM plays "
        "WHERE deleted_at IS NULL AND artist = ? AND played_at BETWEEN ? AND ? "
        "ORDER BY played_at, id",
        (anchor["artist"], anchor["played_at"] - _RUN_WINDOW_SECS,
         anchor["played_at"] + _RUN_WINDOW_SECS)).fetchall()
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


def reconcile_album(play_id: int, db_path: str | None = None) -> int:
    """Unify edition variants of play_id's album across its run. Locked rows
    neither vote nor get rewritten. Returns the number of rows rewritten."""
    with _connect(db_path) as conn:
        run = _run_rows(conn, play_id)
        target = next((r for r in run if r["id"] == play_id), None)
        if target is None or target["album_locked"]:
            return 0
        base = base_title(target["album"])
        if not base:
            return 0
        group = [r for r in run
                 if not r["album_locked"] and r["album"]
                 and base_title(r["album"]) == base]
        # The third element is the evidence: a play whose track could only have
        # come from a qualified edition upgrades the whole run to it.
        winner = pick_winner([
            (r["album"], r["played_at"], bool(r["album_exclusive"]))
            for r in group
        ])
        changed = 0
        for r in group:
            if r["album"] != winner:
                conn.execute(
                    "UPDATE plays SET album = ? WHERE id = ? "
                    "AND (album_locked IS NULL OR album_locked = 0)",
                    (winner, r["id"]))
                changed += 1
        return changed


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

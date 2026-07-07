"""Album/edition reconciliation within listening sessions.

Two plays of the same base album ("Abbey Road" vs "Abbey Road (Deluxe
Edition)") inside one same-artist session run are unified to the most-
qualified edition — the deluxe is the release that must contain everything
heard. Pure string logic + synchronous SQLite (callers wrap in
asyncio.to_thread), mirroring play_history.py's contract.
"""
import re

from play_history import _connect

# A run = contiguous plays by the same artist with gaps under this.
SESSION_GAP_SECS = 1800
# Run detection looks at most this far around the triggering play; a single
# listening session never spans it.
_RUN_WINDOW_SECS = 86400

# Substrings that mark a strippable edition qualifier. Deliberately absent:
# live/acoustic/demos/unplugged — those are different albums, not editions.
_EDITION_MARKERS = (
    "super deluxe", "deluxe", "expanded", "remastered", "remaster",
    "anniversary", "bonus track", "special edition", "collector's edition",
    "collectors edition", "legacy edition", "definitive edition",
    "extended", "reissue", "re-issue", "edition", "version",
)
# Possessive re-recordings ("Taylor's Version") are different recordings,
# never editions — checked BEFORE the generic "version" marker.
_POSSESSIVE_VERSION_RE = re.compile(r"\w+['’]s\s+version", re.IGNORECASE)
_YEAR_RE = re.compile(r"(19|20)\d{2}")

_TRAILING_BRACKET_RE = re.compile(r"\s*[(\[]([^()\[\]]*)[)\]]\s*$")
_TRAILING_DASH_RE = re.compile(r"\s+[-–—]\s+([^-–—]+?)\s*$")


def _is_edition_qualifier(text: str) -> bool:
    t = " ".join(text.strip().lower().split())
    if not t:
        return False
    if _POSSESSIVE_VERSION_RE.search(t):
        return False
    if any(marker in t for marker in _EDITION_MARKERS):
        return True
    return _YEAR_RE.fullmatch(t) is not None


def base_title(album: str | None) -> str:
    """Normalized album title with trailing edition qualifiers stripped.
    Strips repeatedly, so stacked qualifiers all come off."""
    s = " ".join((album or "").split())
    while True:
        m = _TRAILING_BRACKET_RE.search(s)
        if m and _is_edition_qualifier(m.group(1)):
            s = s[: m.start()].rstrip()
            continue
        m = _TRAILING_DASH_RE.search(s)
        if m and _is_edition_qualifier(m.group(1)):
            s = s[: m.start()].rstrip()
            continue
        break
    return " ".join(s.casefold().split())


def pick_winner(albums: list[tuple[str, int]]) -> str:
    """The winning album string among (album, played_at) pairs: most
    qualifiers (longest raw string) wins; ties break to the most recent."""
    return max(albums, key=lambda pair: (len(pair[0]), pair[1]))[0]


def _run_rows(conn, play_id: int) -> list[dict]:
    anchor = conn.execute(
        "SELECT id, artist, played_at FROM plays "
        "WHERE id = ? AND deleted_at IS NULL", (play_id,)).fetchone()
    if anchor is None:
        return []
    rows = conn.execute(
        "SELECT id, artist, album, played_at, album_locked FROM plays "
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
        winner = pick_winner([(r["album"], r["played_at"]) for r in group])
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

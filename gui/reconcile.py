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

# Two vocabularies, because a trailing qualifier answers two different
# questions and conflating them is what mislabelled SOUR as "SOUR (Video
# Version)". Matched as whole words.
#
# EDITION: the same album, in a different edition or master. Strippable — the
# plays belong together, and the plain title is what we show.
_EDITION_MARKER_RE = re.compile(
    r"\b(super deluxe|deluxe|expanded|remastered|remaster|anniversary|"
    r"bonus tracks?|special edition|collector'?s edition|legacy edition|"
    r"definitive edition|reissue|re-issue|archive collection|"
    r"(19|20)\d{2} (remaster|mix))\b",
    re.IGNORECASE,
)
# RENDITION: a different recording of the same songs. Never strippable, never
# merged — a live album is not a pressing of the studio album.
#
# "version" used to sit in the edition list, which is backwards: across 6,215
# iTunes albums it appears overwhelmingly in rendition contexts (karaoke,
# instrumental, piano, acoustic, video, Taylor's) and as an edition only inside
# the fixed phrases "deluxe version" and "bonus track version" — both already
# caught above by "deluxe" and "bonus track".
_RENDITION_MARKER_RE = re.compile(
    r"\b(live|acoustic|unplugged|instrumental|karaoke|demos?|remix(es)?|"
    r"video|radio edit|single version|piano|orchestral|cover|tribute|"
    r"sped up|slowed|extended|dj mix|session|originally performed by|"
    r"in the style of)\b",
    re.IGNORECASE,
)
# Possessive re-recordings ("Taylor's Version") are a real, separately-pressed
# record, not an edition. Its own deluxe strips normally, so
# "1989 (Taylor's Version) [Deluxe]" reduces to "1989 (Taylor's Version)" and
# never to "1989".
_POSSESSIVE_VERSION_RE = re.compile(r"\w+['’]s\s+version", re.IGNORECASE)
_YEAR_RE = re.compile(r"(19|20)\d{2}")

_TRAILING_BRACKET_RE = re.compile(r"\s*[(\[]([^()\[\]]*)[)\]]\s*$")
_TRAILING_DASH_RE = re.compile(r"\s+[-–—]\s+([^-–—]+?)\s*$")


def _is_edition_qualifier(text: str) -> bool:
    """Whether a trailing qualifier means "same album, different edition".

    Order matters: a rendition marker wins over an edition marker, so
    "Video Version" and "Live (Deluxe Edition)" are judged on the part that
    makes them a different record.
    """
    t = " ".join(text.strip().lower().split())
    if not t:
        return False
    if _POSSESSIVE_VERSION_RE.search(t):
        return False
    if _RENDITION_MARKER_RE.search(t):
        return False
    if _EDITION_MARKER_RE.search(t):
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
    """The album to show for a merged group: the plainest form wins.

    "Most qualifiers wins" was the old rule, on the reasoning that a deluxe is
    the release containing everything you heard. But nothing here knows which
    pressing is on the platter — iTunes picks an edition per *track* lookup, so
    a qualifier is usually an artifact of which release happened to match, not
    evidence about your record. The plain title is the common denominator true
    of every edition, and it never asserts a deluxe you may not own.

    The other half of that idea — upgrade the whole run once a track appears
    that exists *only* on the deluxe — is the right way to earn the qualifier,
    and needs evidence this function does not have. See ROADMAP.

    Ties break to the most recent.
    """
    return min(albums, key=lambda pair: (len(pair[0]), -pair[1]))[0]


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

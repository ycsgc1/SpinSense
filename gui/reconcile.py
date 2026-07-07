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
_POSSESSIVE_VERSION_RE = re.compile(r"\w+['']s\s+version", re.IGNORECASE)
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

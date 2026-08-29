"""The iTunes Search API, in one place.

There were two clients before this: the engine asked for a single result to get
an album, cover and duration, while the backend asked for twenty-five to build
the manual album picker. Same endpoint, same parsing, two implementations free
to disagree — and neither could see the album vocabulary in `albums.py`, which
is what let a mislabelled edition through.

Network calls are isolated in `search_songs()` so everything above it is
testable without touching the network.
"""
import re
import urllib.parse

SEARCH_URL = "https://itunes.apple.com/search"

# Enough results to see whether a track appears on the base album as well as a
# deluxe — the evidence `albums.choose_edition()` needs — without asking for a
# page of unrelated compilations.
EDITION_LOOKUP_LIMIT = 10
# The picker shows up to ten distinct albums, and duplicates are common.
CANDIDATE_LOOKUP_LIMIT = 25


async def search_songs(artist: str, title: str, limit: int = EDITION_LOOKUP_LIMIT,
                       timeout_secs: float = 5.0) -> list[dict]:
    """Raw song results for a track, or an empty list on any failure.

    Never raises: enrichment is best-effort, and a play is worth recording even
    when iTunes is unreachable.
    """
    import aiohttp  # late: keeps the module importable without the dependency

    query = urllib.parse.quote_plus(f"{artist} {title}")
    url = f"{SEARCH_URL}?term={query}&entity=song&limit={int(limit)}"
    try:
        client_timeout = aiohttp.ClientTimeout(total=timeout_secs)
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    return []
                data = await response.json(content_type=None)
    except Exception as e:
        print(f"⚠️ iTunes API error: {e}")
        return []
    results = (data or {}).get("results")
    return results if isinstance(results, list) else []


_TRAILING_QUALIFIER_RE = re.compile(r"\s*[(\[][^()\[\]]*[)\]]\s*$|\s+[-\u2013\u2014]\s+.*$")
_NON_ALNUM_RE = re.compile(r"[^0-9a-z]+")


def track_key(title: str | None) -> str:
    """A comparison key for track titles across two different catalogues.

    Shazam and iTunes disagree constantly on punctuation and suffixes — curly
    versus straight apostrophes, "(feat. X)", "- 2019 Remaster" — so matching
    raw strings would reject the right result as often as the wrong one. One
    trailing qualifier comes off, then everything but letters and digits.
    """
    text = " ".join((title or "").split())
    text = _TRAILING_QUALIFIER_RE.sub("", text)
    return _NON_ALNUM_RE.sub("", text.casefold())


def results_for_track(results: list[dict], title: str) -> list[dict]:
    """Only the results that really are the track we asked about.

    iTunes' search is fuzzy and answers with *something* rather than nothing:
    a query for AJR's "3 O'Clock Things" comes back with "Yes I'm A Mess" and
    "3AM", from two albums the track is not on. Taking the top result on faith
    is how a play gets confidently labelled with the wrong record.

    An empty list is the honest answer when nothing matches. "Unknown Album"
    beats a wrong one, and it leaves the manual picker to sort out.
    """
    want = track_key(title)
    if not want:
        return []
    return [r for r in results or [] if track_key((r or {}).get("trackName")) == want]


def hi_res(artwork_url: str | None) -> str | None:
    """iTunes hands back a 100px thumbnail; the same URL serves 1000px."""
    if not artwork_url:
        return None
    return artwork_url.replace("100x100bb", "1000x1000bb")


def album_names(results: list[dict]) -> list[str]:
    """Album titles in relevance order, deduplicated, for edition analysis."""
    seen, out = set(), []
    for r in results or []:
        name = (r or {}).get("collectionName")
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def metadata_for(results: list[dict], album: str | None) -> tuple[str | None, int | None]:
    """(cover art, duration) from the result matching `album`.

    Falls back to the top result: the chosen album may be one we picked for its
    edition rather than its relevance, and every edition shares a duration.
    """
    chosen = None
    for r in results or []:
        if album and (r or {}).get("collectionName") == album:
            chosen = r
            break
    if chosen is None:
        chosen = (results or [{}])[0] if results else {}

    art = hi_res(chosen.get("artworkUrl100"))
    duration = None
    ms = chosen.get("trackTimeMillis")
    if isinstance(ms, (int, float)) and ms > 0:
        duration = int(round(ms / 1000))
    return art, duration


def album_candidates(results: list[dict], limit: int = 10) -> list[dict]:
    """Distinct `{album, art_url}` options for the manual album picker."""
    out, seen = [], set()
    for r in results or []:
        album = (r or {}).get("collectionName")
        if not album or album in seen:
            continue
        seen.add(album)
        out.append({"album": album, "art_url": hi_res(r.get("artworkUrl100"))})
        if len(out) >= limit:
            break
    return out

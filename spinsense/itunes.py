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


_FEAT_RE = re.compile(r"\s+(feat\.?|ft\.?|featuring|with)\s+.*$", re.IGNORECASE)


def artist_key(name: str | None) -> str:
    """A comparison key for artist names, tolerant of featured-credit noise."""
    text = " ".join((name or "").split())
    text = _TRAILING_QUALIFIER_RE.sub("", text)
    text = _FEAT_RE.sub("", text)
    return _NON_ALNUM_RE.sub("", text.casefold())


def results_for_track(results: list[dict], title: str,
                      artist: str | None = None) -> list[dict]:
    """Only the results that really are the recording we asked about.

    Two filters, because two different things went wrong in the field.

    The title catches iTunes answering a fuzzy query with *something* rather
    than nothing: asking for AJR's "3 O'Clock Things" returns "Yes I'm A Mess"
    and "3AM", from two albums the track is not on.

    The artist catches cover and lullaby records, which title their tracks
    identically and so sail past a title check — "My Play" came back from
    "Lullaby Versions of AJR", performed by The Cat and Owl. Matching on the
    album name would not have helped, since it contains the real artist's name;
    `artistName` is the field that actually distinguishes them.

    An empty list is the honest answer. "Unknown Album" beats a wrong one, and
    the manual picker is there for it.
    """
    want_title = track_key(title)
    if not want_title:
        return []
    want_artist = artist_key(artist) if artist else None
    hits = []
    for r in results or []:
        r = r or {}
        if track_key(r.get("trackName")) != want_title:
            continue
        if want_artist and artist_key(r.get("artistName")) != want_artist:
            continue
        hits.append(r)
    return hits


LOOKUP_URL = "https://itunes.apple.com/lookup"


async def album_tracks(collection_id: int, timeout_secs: float = 8.0) -> list[dict]:
    """Every track on an album, by its iTunes collection id.

    This is ground truth, unlike `search_songs()`, which is relevance-ranked and
    frequently unhelpful: searching for AJR's "World's Smallest Violin" returns
    a live album and a sped-up single but never the studio record it is track 11
    of, and searching for "OK Overture" returns nothing whatsoever. A lookup
    returns exactly what is on the release, with correct durations.

    The id comes free with any track that did resolve, so no extra search is
    needed to obtain it.
    """
    import aiohttp

    url = f"{LOOKUP_URL}?id={int(collection_id)}&entity=song&limit=200"
    try:
        client_timeout = aiohttp.ClientTimeout(total=timeout_secs)
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    return []
                data = await response.json(content_type=None)
    except Exception as e:
        print(f"⚠️ iTunes album lookup failed: {e}")
        return []
    results = (data or {}).get("results")
    if not isinstance(results, list):
        return []
    # The response leads with the album itself; only the tracks are wanted.
    return [r for r in results if isinstance(r, dict) and r.get("wrapperType") == "track"]


def find_track(tracks: list[dict], title: str,
               artist: str | None = None) -> dict | None:
    """The entry for `title` in a tracklist, or None if the record hasn't got it.

    `artist` matters far more than it looks, because a deluxe edition routinely
    carries two recordings of the same song. *Short n' Sweet (Deluxe)* has
    "Please Please Please" at track 2 by Sabrina Carpenter and again at track 14
    by Sabrina Carpenter & Dolly Parton. On a title-only match the first one
    wins, so asking about the duet answers about the solo — wrong duration,
    wrong artwork, and wrong album.

    The second consequence is the costly one. The duet is *not* on the standard
    pressing, and "this track is not on the record we thought was playing" is
    exactly the evidence that upgrades a whole listening session to the deluxe.
    Resolving it against the base album by title alone destroyed that evidence
    before anything could act on it.

    So a mismatched artist returns None rather than falling back to the title.
    None is not a wrong answer here — it sends the caller to search, which is
    where it would have gone had the tracklist not been consulted at all.
    """
    want = track_key(title)
    if not want:
        return None
    want_artist = artist_key(artist) if artist else None
    for t in tracks or []:
        if track_key((t or {}).get("trackName")) != want:
            continue
        if want_artist is not None and artist_key(t.get("artistName")) != want_artist:
            continue
        return t
    return None


def collection_id_of(results: list[dict], album: str | None) -> int | None:
    """The iTunes id of `album` among these results, for a tracklist lookup."""
    for r in results or []:
        if album and (r or {}).get("collectionName") == album:
            cid = r.get("collectionId")
            if isinstance(cid, int):
                return cid
    return None


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


def track_metadata(result: dict | None) -> tuple[str | None, int | None]:
    """(cover art, duration) from a single result or tracklist entry."""
    result = result or {}
    art = hi_res(result.get("artworkUrl100"))
    duration = None
    ms = result.get("trackTimeMillis")
    if isinstance(ms, (int, float)) and ms > 0:
        duration = int(round(ms / 1000))
    return art, duration


def metadata_for(results: list[dict], album: str | None) -> tuple[str | None, int | None]:
    """(cover art, duration) from the result matching `album`.

    Falls back to the top result: the chosen album may be one we picked for its
    edition rather than its relevance, and every edition shares a duration.
    """
    for r in results or []:
        if album and (r or {}).get("collectionName") == album:
            return track_metadata(r)
    return track_metadata(results[0] if results else None)


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

"""Album titles: what a trailing qualifier means, and which title to show.

Pure — no I/O, no database, no framework. Lives here rather than in `gui/`
because the engine needs the same vocabulary the moment it asks iTunes which
album a track belongs to, and a second copy of these regexes is precisely how
"SOUR (Video Version)" got treated as an edition of *SOUR*.
"""
import re


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


def normalized(album: str | None) -> str:
    """An album title reduced for comparison, with nothing stripped."""
    return " ".join((album or "").casefold().split())


def is_base_form(album: str | None) -> bool:
    """Whether a title carries no strippable edition qualifier of its own.

    "SOUR" and "1989 (Taylor's Version)" are base forms; "SOUR (Deluxe)" is
    not. A rendition qualifier does not disqualify a title — a live album is
    its own record, and its plain name is that record's base form.
    """
    return base_title(album) == normalized(album)


_SINGLE_OR_EP_RE = re.compile(r"\s+[-\u2013\u2014]\s+(single|ep)\s*$|\b(single|ep)\s*$",
                              re.IGNORECASE)


def is_single_or_ep(album: str | None) -> bool:
    """Whether a release is a single or EP rather than an album.

    iTunes ranks by relevance, so for a hit song the top result is usually the
    single: asking about "Espresso" leads with "Espresso EP" and "Espresso -
    Single" before "Short n' Sweet". Someone with a turntable is playing a
    record, so when an album is available it is the better anchor.
    """
    return bool(_SINGLE_OR_EP_RE.search(album or ""))


# A credit that *extends* another one: "&", "feat.", "with" and friends, in the
# position where a guest is appended to the artist whose record it is.
_GUEST_JOIN_RE = re.compile(
    r"\s*(?:&|\+|/|,|x|and|with|feat\.?|ft\.?|featuring|vs\.?)\s+",
    re.IGNORECASE,
)


def _credit(artist: str | None) -> str:
    return " ".join((artist or "").split()).casefold()


def shares_credit(a: str | None, b: str | None) -> bool:
    """Whether two track credits belong to the same artist's record.

    A listening session is one record, and its plays are found by matching
    artists — but a record's own bonus tracks are frequently credited to more
    than one person. *Short n' Sweet (Deluxe)* closes with "Please Please Please"
    by "Sabrina Carpenter & Dolly Parton", which matches none of the twelve plays
    around it by exact string, so the one play carrying proof of which edition is
    on the platter sat in a session of its own and could not upgrade anything.

    A guest is *appended* to the credit, so the test is whether one credit is the
    other plus a joined name — not whether they share a leading word. Reducing
    each credit to its first name would have worked here and turned
    "Simon & Garfunkel" into "Simon", "Florence + the Machine" into "Florence",
    and "Earth, Wind & Fire" into "Earth", collapsing bands into whoever else
    happens to share that word. Prefix matching leaves every one of those alone,
    since nothing precedes them.

    It also keeps a guest from capturing a session in the other direction:
    "Rowan Blanchard & Sabrina Carpenter" is Rowan Blanchard's record, and
    neither credit is a prefix of the other.
    """
    left, right = _credit(a), _credit(b)
    if left == right:
        return bool(left)
    if not left or not right:
        return False
    longer, shorter = (left, right) if len(left) > len(right) else (right, left)
    if not longer.startswith(shorter):
        return False
    return bool(_GUEST_JOIN_RE.match(longer[len(shorter):]))


def choose_edition(album_names: list[str]) -> tuple[str | None, bool]:
    """Pick which edition a track belongs to, and say whether it proves one.

    Returns `(album, exclusive)`. `exclusive` is True when every edition this
    track appears on carries a qualifier — meaning the track cannot be on the
    base album, so the record being played must be the qualified edition.

    This is the evidence half of the reconciliation rule. Assume the base album
    by default, because a qualifier is usually an artifact of which release
    happened to match rather than a fact about the record on the platter. But
    if a track exists *only* on the deluxe, that is not an artifact — it is
    proof, and the whole listening session can be upgraded on the strength of it.

    Only titles sharing the top result's base title are considered. A track also
    appearing on a greatest-hits or a soundtrack says nothing about which
    edition of *this* album is playing.
    """
    names = [n for n in (album_names or []) if n]
    if not names:
        return None, False

    # Anchor on an album where one exists, not on whichever single iTunes
    # happened to rank first. A lone single still resolves to itself.
    albums_only = [n for n in names if not is_single_or_ep(n)]
    anchor = (albums_only or names)[0]

    base = base_title(anchor)
    family = [n for n in (albums_only or names) if base_title(n) == base]
    plain = [n for n in family if is_base_form(n)]
    if plain:
        return plain[0], False          # the base edition exists; prove nothing
    return min(family, key=len), True   # every edition qualified: exclusive


def pick_winner(candidates) -> str:
    """The album to show for a merged group.

    `candidates` is an iterable of `(album, played_at)` or
    `(album, played_at, exclusive)`.

    Without evidence the plainest title wins: it is true of every edition and
    never asserts a deluxe the listener may not own. With evidence — some track
    in the run could only have come from a qualified edition — that edition
    wins for the whole run, which is the upgrade the original design called for.

    Ties break to the most recent.
    """
    rows = [(c[0], c[1], c[2] if len(c) > 2 else False) for c in candidates]
    proven = [r for r in rows if r[2] and not is_base_form(r[0])]
    if proven:
        return max(proven, key=lambda r: (len(r[0]), r[1]))[0]
    return min(rows, key=lambda r: (len(r[0]), -r[1]))[0]

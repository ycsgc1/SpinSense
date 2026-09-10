"""Album titles: what a trailing qualifier means, and which title to show.

**To fix a record that comes out wrong because of something in its title, edit
`EDITION_MARKERS` or `RENDITION_MARKERS` below.** They are plain lists for that
reason; see the comment above them for which is which, and
`tests/test_vocabulary.py` for the guards that keep an addition honest.

Pure — no I/O, no database, no framework. Lives here rather than in `gui/`
because the engine needs the same vocabulary the moment it asks iTunes which
album a track belongs to, and a second copy of these regexes is precisely how
"SOUR (Video Version)" got treated as an edition of *SOUR*.
"""
import re


# ---------------------------------------------------------------------------
# The qualifier vocabulary.
#
# THIS IS THE LIST TO EDIT when a record comes out wrong because of something in
# its title. Add the phrase in lowercase to whichever tuple below matches what
# it means; terms are escaped and word-bounded automatically, so no regex is
# involved and one term cannot break another. A phrase with a word that can
# vary ("bonus track" / "bonus tracks") needs both spellings listed.
#
# Which list a term goes in decides what SpinSense does with it, and the two
# answers are opposites — getting it wrong is how "SOUR (Video Version)" was
# once treated as an edition of *SOUR*:
#
#   EDITION   the same recordings, in a different edition or master. Strippable:
#             the plays belong together and the plain title is what we show, so
#             "Abbey Road (Deluxe)" is filed as "Abbey Road".
#
#   RENDITION a different recording of the same songs. Never strippable, never
#             merged — a live album is not a pressing of the studio album, and
#             "Blank Space (Taylor's Version)" is not the track on *1989*.
#
# When unsure, RENDITION is the safer choice: it keeps records apart, where a
# wrong EDITION entry silently merges two of them.
# ---------------------------------------------------------------------------

EDITION_MARKERS = (
    "super deluxe",
    "deluxe",
    "expanded",
    "remastered",
    "remaster",
    "anniversary",
    "bonus track",
    "bonus tracks",
    "special edition",
    "collectors edition",
    "collector's edition",
    "legacy edition",
    "definitive edition",
    "reissue",
    "re-issue",
    "archive collection",
)

RENDITION_MARKERS = (
    "live",
    "acoustic",
    "unplugged",
    "instrumental",
    "karaoke",
    "demo",
    "demos",
    "remix",
    "remixes",
    "video",
    "radio edit",
    "single version",
    "piano",
    "orchestral",
    "cover",
    "tribute",
    "sped up",
    "slowed",
    "extended",
    "dj mix",
    "session",
    "originally performed by",
    "in the style of",
)

# Shapes rather than fixed phrases, for qualifiers that vary by artist or year.
# Raw regex, so these are the entries to be careful with — prefer a plain term
# above unless the thing genuinely varies.
EDITION_PATTERNS = (
    # "2011 Remaster", "1987 Mix" — a year plus what was done to it.
    r"(?:19|20)\d{2}\s+(?:remaster|mix)",
)
RENDITION_PATTERNS = (
    # "Taylor's Version", and any other possessive re-recording. These are
    # separately pressed records, not editions: "1989 (Taylor's Version)
    # [Deluxe]" reduces to "1989 (Taylor's Version)" and never to "1989".
    r"\w+['\u2019]s\s+version",
)

# "version" is deliberately absent from both lists. Across 6,215 iTunes albums
# it appears overwhelmingly in rendition contexts (karaoke, instrumental,
# piano, acoustic, video, Taylor's) and as an edition only inside the fixed
# phrases "deluxe version" and "bonus track version" — both already covered.


_CURLY_APOSTROPHE = "\u2019"


def _fold(text: str) -> str:
    """Lowercase, collapse whitespace, and straighten curly apostrophes.

    The two catalogues disagree about apostrophes constantly, so "Collector's
    Edition" and "Collector\u2019s Edition" have to read the same or a term only
    matches half the time it should.
    """
    return " ".join(str(text or "").replace(_CURLY_APOSTROPHE, "'").lower().split())


def _compile_markers(terms, patterns=()) -> re.Pattern:
    """One word-bounded alternation from plain terms plus raw patterns.

    Longest first, so "super deluxe" is preferred over the "deluxe" inside it.
    Word boundaries already separate the pairs listed today, so this is really
    for terms added later — a "deluxe edition" alongside "deluxe" would
    otherwise report the wrong half of the phrase. Plain terms are escaped, so
    a phrase may contain regex characters without anyone needing to know that.
    """
    alts = [re.escape(t) for t in sorted(terms, key=len, reverse=True)]
    alts.extend(patterns)
    return re.compile(r"\b(" + "|".join(alts) + r")\b", re.IGNORECASE)


_EDITION_MARKER_RE = _compile_markers(EDITION_MARKERS, EDITION_PATTERNS)
_RENDITION_MARKER_RE = _compile_markers(RENDITION_MARKERS)
_POSSESSIVE_VERSION_RE = _compile_markers((), RENDITION_PATTERNS)
# Every way a title can announce it is a different recording, for reading the
# markers off a track title (see `rendition_markers`).
_RENDITION_RES = (_RENDITION_MARKER_RE, _POSSESSIVE_VERSION_RE)

_YEAR_RE = re.compile(r"(19|20)\d{2}")

_TRAILING_BRACKET_RE = re.compile(r"\s*[(\[]([^()\[\]]*)[)\]]\s*$")
# A dash inside a word is part of the qualifier ("re-issue"); a *spaced* dash
# starts a new one, so only the last segment is ever taken.
_TRAILING_DASH_RE = re.compile(
    r"\s+[-–—]\s+((?:[^-–—]|(?<=\S)[-–—](?=\S))+?)\s*$")


def _is_edition_qualifier(text: str) -> bool:
    """Whether a trailing qualifier means "same album, different edition".

    Order matters: a rendition marker wins over an edition marker, so
    "Video Version" and "Live (Deluxe Edition)" are judged on the part that
    makes them a different record.
    """
    t = _fold(text)
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


def trailing_qualifiers(title: str | None) -> list[str]:
    """Every trailing bracketed or dashed qualifier, outermost first.

    "Is It Over Now? (Taylor's Version) [From The Vault]" yields
    ["From The Vault", "Taylor's Version"]. Only trailing segments count, so a
    song actually called "Live and Let Die" carries no qualifier at all.
    """
    s = " ".join((title or "").split())
    out: list[str] = []
    while True:
        m = _TRAILING_BRACKET_RE.search(s)
        if m:
            out.append(m.group(1))
            s = s[: m.start()].rstrip()
            continue
        m = _TRAILING_DASH_RE.search(s)
        if m:
            out.append(m.group(1))
            s = s[: m.start()].rstrip()
            continue
        break
    return out


_MARKER_TOKEN_RE = re.compile(r"[^0-9a-z]+")


def rendition_markers(title: str | None) -> frozenset[str]:
    """Which *different recording* this title claims to be, if any.

    A rendition qualifier is not decoration — it names a separate performance.
    "Blank Space (Taylor's Version)" is not the recording on *1989*, it is the
    recording on *1989 (Taylor's Version)*, and treating the two as the same
    song is how a whole side of the re-recording came out filed under the
    original: the album context matched every track against the original's
    tracklist and never looked further.

    Read from trailing qualifiers only, so a song named "Live and Let Die"
    is not mistaken for a live recording.
    """
    found: set[str] = set()
    for qualifier in trailing_qualifiers(title):
        text = _fold(qualifier)
        for pattern in _RENDITION_RES:
            for m in pattern.finditer(text):
                found.add(_MARKER_TOKEN_RE.sub("", m.group(1)))
    return frozenset(t for t in found if t)


def same_recording(wanted: str | None, candidate: str | None) -> bool:
    """Whether `candidate` can be the recording `wanted` names.

    Deliberately one-directional. A qualifier the wanted title carries is
    positive evidence and the candidate must carry it too — "Blank Space
    (Taylor's Version)" cannot be answered by "Blank Space". A qualifier only
    the *candidate* carries is fine: recognisers frequently report the plain
    title for a re-recording, and refusing there would leave the right album
    unreachable.
    """
    return rendition_markers(wanted) <= rendition_markers(candidate)


def _is_any_qualifier(text: str) -> bool:
    """Whether a trailing qualifier is one we recognise at all."""
    t = _fold(text)
    if not t:
        return False
    return bool(_POSSESSIVE_VERSION_RE.search(t)
                or _RENDITION_MARKER_RE.search(t)
                or _EDITION_MARKER_RE.search(t)
                or _YEAR_RE.fullmatch(t))


def recording_base(album: str | None) -> str:
    """Album title with edition *and* rendition qualifiers stripped.

    `base_title` deliberately keeps a re-recording distinct, because "1989" and
    "1989 (Taylor's Version)" are two records and merging them would be wrong.
    This is the looser reading, used only to ask whether two titles name the
    same underlying record — which is the question when one of them is a
    mislabelled play of the other.
    """
    s = " ".join((album or "").split())
    while True:
        m = _TRAILING_BRACKET_RE.search(s)
        if m and _is_any_qualifier(m.group(1)):
            s = s[: m.start()].rstrip()
            continue
        m = _TRAILING_DASH_RE.search(s)
        if m and _is_any_qualifier(m.group(1)):
            s = s[: m.start()].rstrip()
            continue
        break
    return " ".join(s.casefold().split())


def is_recording_base_form(album: str | None) -> bool:
    """Whether a title names the plain record rather than a rendition of it."""
    return recording_base(album) == normalized(album)


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

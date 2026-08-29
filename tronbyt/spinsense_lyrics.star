"""SpinSense Lyrics — Tronbyt/Pixlet prototype.

Two-line karaoke layout on a 64x32 Tronbyt, lyrics only:
  - Top: the current line in a tall 6x10 font. Lines wider than the
    screen scroll horizontally with SMOOTH per-pixel motion
    (animation.Transformation keyframes), paced by playback position:
    the line holds its start for the first 15% of its time slot,
    scrolls linearly, and parks on its ending for the last 15% — so the
    scroll finishes as the line finishes being sung.
  - Bottom: the next line, greyed out (clipped preview). When the
    current line ends, it shifts up and the following line is revealed.

The window is a render.Sequence of per-line segments; each segment's
frame count matches that line's remaining time in the window, so the
whole 15s window plays back on the device's own frame clock.

Demo mode (default): fetches real AJR synced lyrics from LRCLIB at
render time (cached 1h) and simulates playback position from the wall
clock, so consecutive device pulls stay continuous — the same
self-clocking mechanic the real SpinSense integration will use. If
LRCLIB is unreachable, falls back to embedded public-domain songs.

SpinSense mode: set the `spinsense_url` config field. Each render then
fetches `GET {url}/api/lyrics/now` (contract in tronbyt/README.md) and
the window is computed from the real track position. Falls back to demo
mode on any error, so the display never goes blank.
"""

load("animation.star", "animation")
load("http.star", "http")
load("render.star", "render")
load("schema.star", "schema")
load("time.star", "time")

WIDTH = 64
HEIGHT = 32
ROW_H = 16  # height of each of the two visible text rows
YCENTER = 3  # vertical centering of 10px glyphs within a 16px row
WINDOW_SECS = 15  # seconds of lyrics compiled into one render window
TICK_MS = 100  # frame duration; 10 fps -> smooth pixel scroll

FONT = "6x10"  # tall monospace: 6px advance, 10px glyphs
PX_PER_CHAR = 6
SCROLL_HOLD = 0.15  # hold the start/end of a scrolling line 15% of its slot
VSHIFT_SECS = 0.5  # duration of the smooth line-change vertical slide

COLOR_ACTIVE = "#ffffff"
COLOR_NEXT = "#555555"
COLOR_REST = "#3a3a3a"

# Demo catalog: real tracks, lyrics fetched live from LRCLIB (line-level
# LRC). Nothing copyrighted is embedded in this file.
LRCLIB_GET = "https://lrclib.net/api/get"
AJR_TRACKS = [
    "Weak",
    "Burn the House Down",
    "Bang!",
    "World's Smallest Violin",
    "Sober Up",
]
TRACK_SLOT_SECS = 300  # each demo track owns a 5-minute wall-clock slot

# Offline fallback (public domain) so the screen never blanks.
FALLBACK_SONGS = [
    {
        "title": "Amazing Grace",
        "artist": "Trad. / John Newton",
        "duration": 95,
        "lines": [
            (0, ""),
            (4, "Amazing grace, how sweet the sound"),
            (12, "That saved a wretch like me"),
            (20, "I once was lost, but now am found"),
            (28, "Was blind, but now I see"),
            (36, ""),
            (40, "'Twas grace that taught my heart to fear"),
            (48, "And grace my fears relieved"),
            (56, "How precious did that grace appear"),
            (64, "The hour I first believed"),
            (72, ""),
            (78, "Through many dangers, toils and snares"),
            (86, "I have already come"),
        ],
    },
    {
        "title": "House of the Rising Sun",
        "artist": "Traditional",
        "duration": 110,
        "lines": [
            (0, ""),
            (5, "There is a house in New Orleans"),
            (13, "They call the Rising Sun"),
            (21, "And it's been the ruin of many a poor boy"),
            (30, "And God, I know I'm one"),
            (38, ""),
            (46, "My mother was a tailor"),
            (54, "She sewed my new blue jeans"),
            (62, "My father was a gamblin' man"),
            (70, "Down in New Orleans"),
        ],
    },
]

def _iround(v):
    """Round-half-away-from-zero (Starlark has no round())."""
    if v >= 0:
        return int(v + 0.5)
    return -int(-v + 0.5)

def _is_number(s):
    """True if s looks like a non-negative decimal number ('12', '12.34')."""
    if not s:
        return False
    dots = 0
    for i in range(len(s)):
        c = s[i]
        if c == ".":
            dots += 1
        elif not c.isdigit():
            return False
    return dots <= 1 and s != "."

def parse_lrc(text):
    """Parse standard LRC text into sorted [(seconds, line), ...].

    Handles multiple timestamps per line ('[00:12.00][00:40.00]text') and
    skips malformed lines. LRCLIB serves line-level LRC only.
    """
    out = []
    for raw in text.split("\n"):
        parts = raw.split("]")
        stamps = []
        rest_from = 0
        for i in range(len(parts)):
            p = parts[i].strip()
            if p.startswith("["):
                body = p[1:]
                pieces = body.split(":")
                if len(pieces) == 2 and _is_number(pieces[0]) and _is_number(pieces[1]):
                    stamps.append(int(pieces[0]) * 60 + float(pieces[1]))
                    rest_from = i + 1
                else:
                    break  # metadata tag like [ar:...] — skip whole line
            else:
                break
        if not stamps:
            continue
        line_text = "]".join(parts[rest_from:]).strip()
        for ts in stamps:
            out.append((ts, line_text))
    return sorted(out)

def ajr_now_playing(now_unix):
    """Demo: real AJR lyrics from LRCLIB, position simulated from clock."""
    track = AJR_TRACKS[(now_unix // TRACK_SLOT_SECS) % len(AJR_TRACKS)]
    rep = http.get(
        LRCLIB_GET,
        params = {"artist_name": "AJR", "track_name": track},
        headers = {"User-Agent": "SpinSense-Tronbyt-prototype (github.com/ycsgc1/SpinSense)"},
        ttl_seconds = 3600,
    )
    if rep.status_code != 200:
        return None
    body = rep.json()
    if not body:
        return None
    lines = parse_lrc(body.get("syncedLyrics") or "")
    if not lines:
        return None
    duration = body.get("duration") or (lines[-1][0] + 10)
    return {
        "title": body.get("trackName", track),
        "artist": body.get("artistName", "AJR"),
        "duration": duration,
        "position": now_unix % duration,
        "lines": lines,
    }

def fallback_now_playing(now_unix):
    """Offline radio over the embedded public-domain songs."""
    total = 0
    for s in FALLBACK_SONGS:
        total += s["duration"]
    t = now_unix % total
    for s in FALLBACK_SONGS:
        if t < s["duration"]:
            return {
                "title": s["title"],
                "artist": s["artist"],
                "duration": s["duration"],
                "position": t,
                "lines": s["lines"],
            }
        t -= s["duration"]
    return None  # unreachable: t < total by construction

def spinsense_now_playing(base_url):
    """Fetch real now-playing state from SpinSense (future endpoint).

    Expected JSON (see tronbyt/README.md):
      {"in_song": true, "title": "...", "artist": "...",
       "duration_secs": 213, "position_secs": 83.4,
       "lines": [[12.1, "First line"], [15.8, "Second line"], ...]}

    Returns None when not playing or on any error (caller falls back).
    """
    url = base_url.rstrip("/") + "/api/lyrics/now"
    rep = http.get(url, ttl_seconds = 0)
    if rep.status_code != 200:
        return None
    body = rep.json()
    if not body or not body.get("in_song"):
        return None
    lines = [(l[0], l[1]) for l in body.get("lines", [])]
    if not lines:
        return None
    return {
        "title": body.get("title", "Unknown"),
        "artist": body.get("artist", ""),
        "duration": body.get("duration_secs", 0),
        "position": body.get("position_secs", 0),
        "lines": lines,
    }

def line_index(lines, pos):
    """Index of the lyric line active at `pos`, or -1 before the first."""
    idx = -1
    for i in range(len(lines)):
        if lines[i][0] <= pos:
            idx = i
        else:
            break
    return idx

def next_nonempty(lines, idx):
    """Text of the next non-empty line after idx ('' if none)."""
    for i in range(idx + 1, len(lines)):
        if lines[i][1].strip() != "":
            return lines[i][1]
    return ""

def scroll_x(f, overhang):
    """Translate-x of the active line at fraction `f` through its slot.

    Geometry (verified against tronbyt/pixlet source): Transformation's
    canvas defaults to its parent Box bounds (64px) and the child text
    paints from the canvas origin — left-pinned, NOT centered. So the
    start of the line is pinned at translate 0, and pinning the END at
    the right edge needs translate -(text_width - 64) = -overhang.
    Holds at each end per SCROLL_HOLD.
    """
    if overhang <= 0:
        return 0.0
    if f <= SCROLL_HOLD:
        return 0.0
    if f >= 1.0 - SCROLL_HOLD:
        return -overhang * 1.0
    span = 1.0 - 2.0 * SCROLL_HOLD
    return -(f - SCROLL_HOLD) / span * overhang

def segment_keyframes(f0, f1, overhang):
    """Keyframe points [(percentage, x), ...] for the slot range [f0, f1].

    Maps the hold/scroll/hold profile onto a segment that may start or
    end mid-line (window boundaries), rescaled to 0..1 percentages.
    """
    pts = [(0.0, scroll_x(f0, overhang))]
    if f1 > f0:
        for b in (SCROLL_HOLD, 1.0 - SCROLL_HOLD):
            if f0 < b and b < f1:
                pts.append(((b - f0) / (f1 - f0), scroll_x(b, overhang)))
    pts.append((1.0, scroll_x(f1, overhang)))
    return pts

def window_segments(np):
    """Partition the render window into per-line segments.

    Each segment carries: text (active line), prev (line above, for the
    slide-in), next (preview below), slot-fraction range [f0, f1], frame
    count at TICK_MS, and `enter` — whether it begins at the line's true
    start (so it plays the smooth vertical line-change slide) versus being
    cut mid-line by a window boundary (shown already settled).
    """
    lines = np["lines"]
    pos = np["position"]
    end = min(pos + WINDOW_SECS, np["duration"])
    idx = line_index(lines, pos)
    segs = []
    prev_text = ""
    for i in range(-1, len(lines)):
        if i < idx:
            continue
        if i == -1:
            t0 = 0.0
            t1 = lines[0][0] if lines else np["duration"]
            text = ""
        else:
            t0 = lines[i][0]
            t1 = lines[i + 1][0] if i + 1 < len(lines) else max(np["duration"], t0 + 5)
            text = lines[i][1]
        seg_start = pos if i == -1 else max(t0, pos)
        seg_end = min(t1, end)
        if seg_start >= end:
            break
        if seg_end <= seg_start:
            if i >= 0:
                prev_text = text  # advance continuity past a zero-length slot
            continue
        span = t1 - t0
        in_line = i >= 0 and span > 0
        segs.append({
            "text": text,
            "prev": prev_text,
            "next": next_nonempty(lines, i),
            "f0": (seg_start - t0) / span if in_line else 0.0,
            "f1": (seg_end - t0) / span if in_line else 1.0,
            "frames": max(_iround((seg_end - seg_start) * 1000.0 / TICK_MS), 1),
            "enter": in_line and pos <= t0 + 1e-9,
        })
        prev_text = text if i >= 0 else ""
    if not segs:
        segs.append({"text": "", "prev": "", "next": "", "f0": 0.0, "f1": 1.0, "frames": 1, "enter": False})
    return segs

def static_points(text):
    """X-keyframes for a non-scrolling row: centered if it fits, else left."""
    w = len(text) * PX_PER_CHAR
    x = (WIDTH - w) // 2 if w < WIDTH else 0
    return [(0.0, x), (1.0, x)]

def active_points(text, f0, f1):
    """X-keyframes for the active line: centered if it fits, else scroll."""
    overhang = len(text) * PX_PER_CHAR - WIDTH
    if overhang <= 0:
        c = (WIDTH - len(text) * PX_PER_CHAR) // 2
        return [(0.0, c), (1.0, c)]
    return segment_keyframes(f0, f1, overhang)

def row_layer(text, color, x_pts, frames):
    """One 64x16 text row with a (possibly static) horizontal transform.

    The Transformation canvas fills the row Box and paints the text from
    its origin, so both the horizontal position and the +YCENTER vertical
    centering are applied via Translate offsets.
    """
    kfs = [
        animation.Keyframe(
            percentage = p,
            transforms = [animation.Translate(_iround(x), YCENTER)],
            curve = "linear",
        )
        for p, x in x_pts
    ]
    return render.Box(
        width = WIDTH,
        height = ROW_H,
        child = animation.Transformation(
            child = render.Text(content = text, font = FONT, color = color),
            duration = frames,
            delay = 0,
            keyframes = kfs,
        ),
    )

def vertical_points(enter, frames):
    """Y-keyframes for the 3-row stack.

    At Y=0 the viewport shows (prev, active); at Y=-ROW_H it shows
    (active, next). An `enter` segment slides 0 -> -ROW_H over VSHIFT_SECS
    then holds (the line-change animation); a mid-line segment stays
    settled at -ROW_H.
    """
    if not enter:
        return [(0.0, -ROW_H), (1.0, -ROW_H)]
    vframes = min(max(_iround(VSHIFT_SECS * 1000.0 / TICK_MS), 1), frames)
    if vframes >= frames:
        return [(0.0, 0.0), (1.0, -ROW_H)]
    p = vframes / float(frames)
    return [(0.0, 0.0), (p, -float(ROW_H)), (1.0, -float(ROW_H))]

def segment_widget(seg):
    """A window segment: a 3-row stack (prev / active / next) that slides
    up by one row at the line change, with the active row scrolling."""
    frames = seg["frames"]
    if seg["text"].strip() == "":
        cur_text = "♪  ♪  ♪"
        cur_color = COLOR_REST
    else:
        cur_text = seg["text"]
        cur_color = COLOR_ACTIVE
    stack = render.Column(children = [
        row_layer(seg["prev"], COLOR_NEXT, static_points(seg["prev"]), frames),
        row_layer(cur_text, cur_color, active_points(cur_text, seg["f0"], seg["f1"]), frames),
        row_layer(seg["next"], COLOR_NEXT, static_points(seg["next"]), frames),
    ])
    vkfs = [
        animation.Keyframe(
            percentage = p,
            transforms = [animation.Translate(0, _iround(y))],
            curve = "ease_in_out",
        )
        for p, y in vertical_points(seg["enter"], frames)
    ]
    return render.Box(
        width = WIDTH,
        height = HEIGHT,
        child = animation.Transformation(
            child = stack,
            duration = frames,
            delay = 0,
            keyframes = vkfs,
        ),
    )

def main(config):
    np = None
    base_url = config.get("spinsense_url") or ""
    if base_url:
        np = spinsense_now_playing(base_url)
    if np == None:
        np = ajr_now_playing(time.now().unix)
    if np == None:
        np = fallback_now_playing(time.now().unix)
    segs = window_segments(np)
    return render.Root(
        delay = TICK_MS,
        show_full_animation = True,
        child = render.Sequence(children = [segment_widget(s) for s in segs]),
    )

def get_schema():
    return schema.Schema(
        version = "1",
        fields = [
            schema.Text(
                id = "spinsense_url",
                name = "SpinSense URL",
                desc = "Base URL of your SpinSense instance (e.g. http://truenas:3313). Leave empty for demo mode.",
                icon = "music",
                default = "",
            ),
        ],
    )

# Tronbyt lyrics app (prototype)

`spinsense_lyrics.star` is a Pixlet/Starlark app for a Tronbyt (flashed Tidbyt,
64x32) that shows a two-line karaoke display — nothing but lyrics, exactly
two rows in a tall 6x10 monospace font:

- **Top line (current):** lines that fit are shown static. Longer lines
  scroll horizontally with **smooth per-pixel motion**
  (`animation.Transformation` keyframes — the same mechanism slick apps
  like the Tetris clock use), paced by playback position: the line holds
  its start for the first 15% of its time slot, scrolls linearly, and
  parks on its ending for the last 15%, so the scroll finishes as the
  line finishes being sung. (LRCLIB timestamps are line-level, so the
  pace within a line is interpolated.)
- **Bottom line (next):** greyed-out preview. When the current line ends,
  the whole stack slides up smoothly (karaoke style): the old line rises
  out the top, the next line rises into the active row, and a new preview
  rises in below. The slide lasts `VSHIFT_SECS` with an ease-in-out curve.
- Instrumental rests (empty LRC lines) render as dim `♪ ♪ ♪` with the
  upcoming line still previewed below.

Architecturally the window is a `render.Sequence` of per-line segments.
Each segment is a 3-row stack (prev / active / next) wrapped in a vertical
`Transformation` that slides the stack up one row at the line change,
while the active row has its own horizontal `Transformation` for the
scroll. Each segment's frame count (at 100ms/frame) matches that line's
remaining time in the window, so one 15s window ≈ 150 frames plays back on
the device's own clock.

## Modes

- **Demo mode (default):** fetches real AJR synced lyrics **live from
  LRCLIB** at render time (cached 1 hour) — Weak, Burn the House Down,
  Bang!, World's Smallest Violin, Sober Up — each owning a 5-minute
  wall-clock slot. Playback position is derived from the wall clock, so
  every device pull resumes exactly where the previous window ended: the
  demo rehearses the real sync mechanic *and* the real lyrics source.
  No copyrighted text is embedded in the repo.
  If LRCLIB is unreachable, falls back to embedded public-domain songs.
- **SpinSense mode:** set the `spinsense_url` config field (e.g.
  `http://truenas:3313`). Each render fetches `GET {url}/api/lyrics/now`
  and computes the window from the real track position. Any error or
  "not playing" falls back to demo mode so the display never blanks.

## Install on tronbyt-server

1. In the tronbyt-server web UI, add a custom app and upload
   `spinsense_lyrics.star` (or drop it in the server's custom-apps
   directory).
2. Set the app's display time (dwell) to **15 seconds** to match
   `WINDOW_SECS` — each pull plays one full window and immediately pulls
   the next, which keeps the lyrics continuous.
3. Leave `spinsense_url` empty for now: you should see AJR lyrics
   mid-song, the active line scrolling in time with its slot.

## Tuning knobs (top of the .star file)

These are source constants (Tronbyt custom-app config is clunky, so the
defaults are meant to be right as-is; edit the file only if needed):

| Knob | Default | Meaning |
|---|---|---|
| `WINDOW_SECS` | 15 | Seconds of lyrics compiled into one render. Match the app's dwell to this. |
| `TICK_MS` | 100 | Frame duration (10 fps) — the pixel-scroll smoothness. |
| `FONT` / `PX_PER_CHAR` | 6x10 / 6 | Tall monospace; exact geometry (6px advance/char). |
| `SCROLL_HOLD` | 0.15 | Fraction of the line's slot to hold at each end before/after scrolling. |
| `VSHIFT_SECS` | 0.5 | Duration of the smooth line-change slide-up. |
| `ROW_H` / `YCENTER` | 16 / 3 | Row height and vertical glyph centering. |
| `COLOR_ACTIVE` / `COLOR_NEXT` | white / grey | Active and preview line colors. |
| `AJR_TRACKS` / `TRACK_SLOT_SECS` | 5 tracks / 300s | Demo catalog and per-track wall-clock slot. |

## The SpinSense contract (future endpoint)

Pointing this at the live turntable = SpinSense serving one JSON route.
Per the approved design doc
(`~/.gstack/projects/ycsgc1-SpinSense/ubuntu-main-design-20260712-200205.md`),
position comes from the SyncClock (Shazam `matches[0].offset` + elapsed
wall clock, re-anchored by confirmation samples):

```
GET /api/lyrics/now

{
  "in_song": true,
  "title": "Song Title",
  "artist": "Artist",
  "duration_secs": 213,
  "position_secs": 83.4,          // SyncClock position at response time
  "lines": [[12.1, "First line"], // LRC timestamps in seconds
            [15.8, "Second line"], ...]
}
```

`in_song: false` (or 404/empty) means "nothing playing" — the app falls
back to demo mode.

## Known limitations (prototype)

- Scroll pace within a line is interpolated, not word-timed (line-level
  LRC — the data has one timestamp per line).
- Both motions were built on the tronbyt/pixlet source semantics: a
  `Transformation` canvas defaults to its parent `Box` bounds and paints
  its child from the origin, then translates, and the parent `Box` clips.
  Horizontal scroll runs 0 → -(text_width - 64); the vertical line-change
  slides a 48px 3-row stack from Y=0 (showing prev+active) to Y=-16
  (showing active+next), relying on that same translate-then-clip
  behavior (the exact thing your working horizontal scroll already
  proves). The nested vertical+horizontal `Transformation` per segment is
  the least device-verified part; if a render looks off, this is where.
- If a line freezes mid-scroll before finishing, check the app's display
  time (dwell) on tronbyt-server: it must be <= `WINDOW_SECS` (15s). A
  longer dwell holds the window's last frame until the next pull.
- Verified with a Python logic harness (45 checks: LRC parsing, scroll
  profile, vertical slide keyframes incl. enter/settled/tiny-slot, prev
  continuity across the line change, segment partitioning incl. rests and
  duplicate timestamps, truncation, fallbacks, SpinSense mode) — not yet
  rendered through a real `pixlet` binary.

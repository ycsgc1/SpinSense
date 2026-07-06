# Wrapped / Listening Stats Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A "Stats" page (4th nav item) computing top artists/tracks, plays-over-time, genres/decades, and real listening time from the plays table — with schema/capture changes that make a future Last.fm scrobbler a pure consumer.

**Architecture:** Two nullable columns (`ended_at`, `duration_secs`) are captured by the existing engine→GUI pipeline (iTunes enrichment supplies duration; `ipc_manager` stamps end times). A new synchronous `gui/stats.py` aggregates over SQLite (mirroring `play_history.py`), exposed as `GET /api/stats`, rendered by a vanilla-JS page with dependency-free CSS-bar charts.

**Tech Stack:** Python 3.11 / FastAPI / SQLite / Jinja2 templates / vanilla JS + Tailwind (CDN, existing M3 tokens). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-06-wrapped-stats-design.md`

## Global Constraints

- Run tests from inside the package dirs, venv at repo root: `cd gui && ../.venv/bin/python -m pytest tests -q` and `cd core && ../.venv/bin/python -m pytest tests -q`. Running from repo root fails collection (relative `static` path).
- No new runtime dependencies; no chart libraries; no client-side aggregation.
- Listening time: `clamp(ended_at - played_at, 0, 2400)`; rows with NULL `ended_at` are EXCLUDED (never estimated).
- Top lists are top 5. Soft-deleted rows (`deleted_at IS NOT NULL`) excluded from every query.
- Period boundaries and chart buckets use the server's local timezone.
- All new gui test files start with the `sys.path.insert(0, GUI_DIR)` header used by every existing test in `gui/tests/`.
- UI uses the existing design tokens/classes (`glass-panel`, `text-label-md`, `bg-primary`, etc.) — match `history.html`/`dashboard.html` idiom.
- Commit messages end with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

---

### Task 1: Schema columns + `set_ended_at` + `record_play(duration_secs=)`

**Files:**
- Modify: `gui/play_history.py` (init_db `_ENRICHMENT_COLUMNS`-style migration ~line 24-52; `record_play` ~line 55-73; new `set_ended_at` after `set_art_path`)
- Test: `gui/tests/test_play_history.py` (append a new TestCase)

**Interfaces:**
- Produces: `play_history.set_ended_at(play_id: int, ended_at: int, db_path: str | None = None) -> None` (writes only if currently NULL); `record_play(..., duration_secs: int | None = None)` kw-only.

- [ ] **Step 1: Write the failing tests** — append to `gui/tests/test_play_history.py`:

```python
class EndedAtAndDurationTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        play_history.init_db(db_path=self.db_path)

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def _row(self, pid):
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM plays WHERE id = ?", (pid,)).fetchone()
        conn.close()
        return row

    def test_new_columns_default_null(self):
        pid = play_history.record_play("T", "A", None, None, db_path=self.db_path)
        row = self._row(pid)
        self.assertIsNone(row["ended_at"])
        self.assertIsNone(row["duration_secs"])

    def test_record_play_stores_duration(self):
        pid = play_history.record_play("T", "A", None, None,
                                       db_path=self.db_path, duration_secs=245)
        self.assertEqual(self._row(pid)["duration_secs"], 245)

    def test_set_ended_at_first_write_wins(self):
        pid = play_history.record_play("T", "A", None, None, db_path=self.db_path)
        play_history.set_ended_at(pid, 1000, db_path=self.db_path)
        self.assertEqual(self._row(pid)["ended_at"], 1000)
        play_history.set_ended_at(pid, 2000, db_path=self.db_path)  # idempotent: no overwrite
        self.assertEqual(self._row(pid)["ended_at"], 1000)

    def test_migration_adds_columns_to_existing_db(self):
        # init_db twice must not fail, and columns exist exactly once.
        play_history.init_db(db_path=self.db_path)
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(plays)")]
        conn.close()
        self.assertEqual(cols.count("ended_at"), 1)
        self.assertEqual(cols.count("duration_secs"), 1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd gui && ../.venv/bin/python -m pytest tests/test_play_history.py::EndedAtAndDurationTest -v`
Expected: FAIL — `no such column: duration_secs` / `unexpected keyword argument 'duration_secs'` / `no attribute 'set_ended_at'`

- [ ] **Step 3: Implement** — in `gui/play_history.py`:

Extend the migration dict (keeps the existing `deleted_at` special-case unchanged):

```python
_ENRICHMENT_COLUMNS = {
    "isrc": "TEXT",
    "genre": "TEXT",
    "release_year": "INTEGER",
    # Listening-time / Last.fm-compat columns (2026-07 stats feature):
    "ended_at": "INTEGER",        # unix secs the track stopped; NULL = untracked
    "duration_secs": "INTEGER",   # canonical track length from enrichment
}
```

`record_play`: add `duration_secs: int | None = None` to the kw-only params and extend the INSERT:

```python
def record_play(
    title: str,
    artist: str,
    album: str | None,
    art_url: str | None,
    db_path: str | None = None,
    *,
    isrc: str | None = None,
    genre: str | None = None,
    release_year: int | None = None,
    duration_secs: int | None = None,
) -> int:
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO plays "
            "(title, artist, album, art_url, played_at, isrc, genre, release_year, duration_secs) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (title, artist, album, art_url, int(time.time()), isrc, genre,
             release_year, duration_secs),
        )
        return int(cur.lastrowid)
```

New function after `set_art_path`:

```python
def set_ended_at(play_id: int, ended_at: int, db_path: str | None = None) -> None:
    """Stamp when a play stopped. First write wins (ended_at must be NULL) so
    a late duplicate stop-frame can't stretch an already-closed play."""
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE plays SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
            (ended_at, play_id),
        )
```

- [ ] **Step 4: Run the full gui suite**

Run: `cd gui && ../.venv/bin/python -m pytest tests -q`
Expected: all pass (existing + 4 new)

- [ ] **Step 5: Commit**

```bash
git add gui/play_history.py gui/tests/test_play_history.py
git commit -m "feat(history): ended_at + duration_secs columns, set_ended_at helper

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: `ipc_manager` stamps `ended_at` and passes `duration_secs`

**Files:**
- Modify: `gui/ipc_manager.py` (`_record_if_new` ~line 102-137; module globals ~line 60)
- Test: `gui/tests/test_play_history.py` (append a new TestCase — the ipc dedupe tests already live in this file)

**Interfaces:**
- Consumes: `play_history.set_ended_at`, `record_play(duration_secs=)` from Task 1.
- Produces: `ipc_manager._last_play_id: int | None` module global (tests reset it in setUp like `_last_recorded_key`). Track dicts arriving over the UDS may carry `"duration_secs"` (Task 3 supplies it; absent/None is fine).

- [ ] **Step 1: Write the failing tests** — append to `gui/tests/test_play_history.py`:

```python
class EndedAtStampingTest(unittest.TestCase):
    """Feed frames through _record_if_new; assert ended_at stamping."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        play_history.init_db(db_path=self.db_path)
        self._orig_db = play_history.DB_PATH
        play_history.DB_PATH = self.db_path  # _record_if_new writes here
        ipc_manager._last_recorded_key = None
        ipc_manager._last_play_id = None

    def tearDown(self):
        play_history.DB_PATH = self._orig_db
        ipc_manager._last_recorded_key = None
        ipc_manager._last_play_id = None
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def _rows(self):
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM plays ORDER BY id").fetchall()
        conn.close()
        return rows

    def test_next_track_stamps_previous(self):
        asyncio.run(ipc_manager._record_if_new({"title": "One", "artist": "A"}))
        asyncio.run(ipc_manager._record_if_new({"title": "Two", "artist": "A"}))
        rows = self._rows()
        self.assertEqual(len(rows), 2)
        self.assertIsNotNone(rows[0]["ended_at"])   # closed by track change
        self.assertIsNone(rows[1]["ended_at"])      # still playing

    def test_silence_stamps_current(self):
        asyncio.run(ipc_manager._record_if_new({"title": "One", "artist": "A"}))
        asyncio.run(ipc_manager._record_if_new({"title": "", "artist": ""}))
        rows = self._rows()
        self.assertIsNotNone(rows[0]["ended_at"])
        self.assertIsNone(ipc_manager._last_play_id)

    def test_repeated_frames_do_not_stamp(self):
        asyncio.run(ipc_manager._record_if_new({"title": "One", "artist": "A"}))
        asyncio.run(ipc_manager._record_if_new({"title": "One", "artist": "A"}))
        self.assertIsNone(self._rows()[0]["ended_at"])

    def test_duration_passes_through(self):
        asyncio.run(ipc_manager._record_if_new(
            {"title": "One", "artist": "A", "duration_secs": 245}))
        self.assertEqual(self._rows()[0]["duration_secs"], 245)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd gui && ../.venv/bin/python -m pytest tests/test_play_history.py::EndedAtStampingTest -v`
Expected: FAIL — `module 'ipc_manager' has no attribute '_last_play_id'`

- [ ] **Step 3: Implement** — in `gui/ipc_manager.py`:

Add `import time` to the imports. Below `_last_recorded_key`, add:

```python
# The row id of the most recent play we recorded, still "open" (no ended_at).
# Stamped when the next track starts or the engine reports silence; a GUI
# restart mid-play simply leaves the row's ended_at NULL (excluded from
# listening-time stats — never estimated).
_last_play_id: int | None = None


async def _stamp_last_play_ended() -> None:
    global _last_play_id
    if _last_play_id is None:
        return
    try:
        await asyncio.to_thread(play_history.set_ended_at, _last_play_id, int(time.time()))
    except Exception as e:
        log.warning("failed to stamp ended_at for play %s: %s", _last_play_id, e)
    _last_play_id = None
```

Rework `_record_if_new` (whole function — the dedupe logic is unchanged, stamping is added):

```python
async def _record_if_new(track: dict) -> None:
    """Record a new identification if the title differs from the last one we
    saved. On silence (empty title) reset the dedupe state so the next play is
    treated as new, and close the open play's ended_at."""
    global _last_recorded_key, _last_play_id
    title = (track or {}).get("title", "") or ""

    if title == "":
        await _stamp_last_play_ended()
        _last_recorded_key = None
        return

    artist = track.get("artist", "") or ""
    key = (artist, title)
    if key == _last_recorded_key:
        return

    album = track.get("album") or None
    art_url = track.get("art_url") or None
    isrc = track.get("isrc") or None
    genre = track.get("genre") or None
    release_year = track.get("release_year") or None
    duration_secs = track.get("duration_secs") or None

    # A different track is starting: the previous one just ended.
    await _stamp_last_play_ended()

    try:
        play_id = await asyncio.to_thread(
            play_history.record_play, title, artist, album, art_url,
            isrc=isrc, genre=genre, release_year=release_year,
            duration_secs=duration_secs,
        )
    except Exception as e:
        log.error("failed to record play %s - %s: %s", artist, title, e)
        return

    _last_recorded_key = key
    _last_play_id = play_id

    if art_url:
        _art_tasks.add(task := asyncio.create_task(_download_and_store_art(play_id, art_url)))
        task.add_done_callback(_art_tasks.discard)
```

- [ ] **Step 4: Run the full gui suite**

Run: `cd gui && ../.venv/bin/python -m pytest tests -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add gui/ipc_manager.py gui/tests/test_play_history.py
git commit -m "feat(ipc): stamp ended_at on track change/silence; pass duration through

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Engine captures `duration_secs` (iTunes/AudD → state → frame)

**Files:**
- Modify: `core/core_engine.py` (`fetch_itunes_metadata` ~line 452; `_identify_shazam` return dict ~line 552; `_audd_to_normalized` ~line 563; `_acoustid_to_normalized` return ~line 662; `_handle_match` ~line 752; `state` dict ~line 382; `build_status_payload` ~line 399; `_clear_track_state` ~line 800)
- Modify: `core/tests/test_recognition_phases.py` (existing iTunes stubs return 2-tuples — update to 3-tuples)

**Interfaces:**
- Consumes: nothing new.
- Produces: normalized track dicts and the `live_status` frame's `track` object gain `"duration_secs": int | None`; `fetch_itunes_metadata(artist, title) -> (album, art_url, duration_secs)` (3-tuple).

- [ ] **Step 1: Update the existing stubs (they pin the old 2-tuple contract)** — in `core/tests/test_recognition_phases.py`:

In the class at ~line 140 (`setUp` with `self.events`): change
`async def fake_itunes(artist, title): return (None, None)` → `return (None, None, None)`.

In `HandleMatchArtTest` (~line 218): the stub `return self.itunes_return` stays; update every `self.itunes_return` assignment:
- `self.itunes_return = (None, None)` → `(None, None, None)` (three occurrences: setUp + two tests)
- `self.itunes_return = ("iTunes Album", "itunes_art.jpg")` → `("iTunes Album", "itunes_art.jpg", None)`

- [ ] **Step 2: Write the failing tests** — append to `core/tests/test_recognition_phases.py`:

```python
class DurationCaptureTest(unittest.TestCase):
    """duration_secs flows: enrichment -> state -> live_status frame."""

    def setUp(self):
        async def fake_img(url):
            return ""
        async def fake_phase(p):
            return None
        async def fake_blip():
            return None
        def fake_publish(status, artist="", title="", album="", art_url="", art_base64=""):
            return None
        self._orig = (core_engine.fetch_itunes_metadata, core_engine.fetch_image_base64,
                      core_engine._publish_phase, core_engine._publish_idle_blip,
                      core_engine.publish_state)
        core_engine.fetch_image_base64 = fake_img
        core_engine._publish_phase = fake_phase
        core_engine._publish_idle_blip = fake_blip
        core_engine.publish_state = fake_publish
        core_engine.state["last_song"] = ""

    def tearDown(self):
        (core_engine.fetch_itunes_metadata, core_engine.fetch_image_base64,
         core_engine._publish_phase, core_engine._publish_idle_blip,
         core_engine.publish_state) = self._orig

    def test_itunes_duration_reaches_state_and_frame(self):
        async def fake_itunes(artist, title):
            return ("Album", "art.jpg", 245)
        core_engine.fetch_itunes_metadata = fake_itunes
        n = {"title": "T", "artist": "A", "album": None, "art_url": None,
             "isrc": None, "genre": None, "release_year": None, "duration_secs": None}
        asyncio.run(core_engine._handle_match(n))
        self.assertEqual(core_engine.state["duration_secs"], 245)
        payload = core_engine.build_status_payload("playing", 0.0, core_engine.state)
        self.assertEqual(payload["payload"]["track"]["duration_secs"], 245)

    def test_backend_duration_used_when_itunes_has_none(self):
        async def fake_itunes(artist, title):
            return (None, None, None)
        core_engine.fetch_itunes_metadata = fake_itunes
        n = {"title": "T", "artist": "A", "album": None, "art_url": None,
             "isrc": None, "genre": None, "release_year": None, "duration_secs": 200}
        asyncio.run(core_engine._handle_match(n))
        self.assertEqual(core_engine.state["duration_secs"], 200)

    def test_clear_resets_duration(self):
        core_engine.state["duration_secs"] = 245
        core_engine._clear_track_state(set_backoff=False)
        self.assertIsNone(core_engine.state["duration_secs"])


class AuddDurationTest(unittest.TestCase):
    def test_maps_duration_in_millis(self):
        n = core_engine._audd_to_normalized(
            {"title": "T", "artist": "A", "apple_music": {"durationInMillis": 245500}})
        self.assertEqual(n["duration_secs"], 246)

    def test_missing_duration_is_none(self):
        n = core_engine._audd_to_normalized({"title": "T", "artist": "A"})
        self.assertIsNone(n["duration_secs"])
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd core && ../.venv/bin/python -m pytest tests/test_recognition_phases.py -v -k "Duration"`
Expected: FAIL — KeyError `'duration_secs'`

- [ ] **Step 4: Implement** — in `core/core_engine.py`:

`fetch_itunes_metadata` — return a 3-tuple (docstring: album, art_url, duration_secs):

```python
async def fetch_itunes_metadata(artist, title):
    query = urllib.parse.quote_plus(f"{artist} {title}")
    url = f"https://itunes.apple.com/search?term={query}&entity=song&limit=1"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json(content_type=None)
                    if data.get("resultCount", 0) > 0:
                        result = data["results"][0]
                        album = result.get("collectionName", "")
                        art_url = result.get("artworkUrl100", "").replace("100x100bb", "1000x1000bb")
                        duration_secs = None
                        ms = result.get("trackTimeMillis")
                        if isinstance(ms, (int, float)) and ms > 0:
                            duration_secs = int(round(ms / 1000))
                        return album, art_url, duration_secs
    except Exception as e:
        print(f"⚠️ iTunes API error: {e}")
    return None, None, None
```

Normalized shapes — add `"duration_secs"` to all three backends:
- `_identify_shazam` return dict: `"duration_secs": None,` (Shazam gives no reliable length)
- `_acoustid_to_normalized` return dict: `"duration_secs": None,`
- `_audd_to_normalized` — before the return, and in the return dict:

```python
    duration_secs = None
    dm = am.get("durationInMillis")
    if isinstance(dm, (int, float)) and dm > 0:
        duration_secs = int(round(dm / 1000))
```

`state` init (~line 382): add `"duration_secs": None,` after `"release_year": None,`.

`build_status_payload` track object: add `"duration_secs": st.get("duration_secs"),` after `"release_year"`.

`_handle_match`: change the unpack and store (iTunes wins, backend fallback — same precedence as album/art):

```python
    album, art_url, duration_secs = await fetch_itunes_metadata(artist, title)
    if not art_url:
        art_url = track.get('art_url') or ''   # backend-supplied fallback art
    if not album:
        album = track.get('album') or "Unknown Album"
    if not duration_secs:
        duration_secs = track.get('duration_secs')
```

and alongside the other `state[...] =` assignments: `state["duration_secs"] = duration_secs`.

`_clear_track_state`: add `state["duration_secs"] = None`.

- [ ] **Step 5: Run the full core suite**

Run: `cd core && ../.venv/bin/python -m pytest tests -q`
Expected: all pass (updated stubs + 5 new tests)

- [ ] **Step 6: Commit**

```bash
git add core/core_engine.py core/tests/test_recognition_phases.py
git commit -m "feat(engine): capture track duration from iTunes/AudD into state + frames

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: `gui/stats.py` aggregation module

**Files:**
- Create: `gui/stats.py`
- Test: `gui/tests/test_stats.py`

**Interfaces:**
- Consumes: `play_history._connect(db_path)` (same DB-access idiom), Task 1 columns.
- Produces: `stats.compute_stats(period: str, year: int | None = None, month: int | None = None, db_path: str | None = None, now: int | None = None) -> dict` returning the spec's JSON shape; `stats._period_bounds(period, year, month, now) -> (int, int)`; constants `LISTEN_CAP_SECS = 2400`, `TOP_N = 5`. Raises `ValueError` on bad `period`/`month` (endpoint maps to 400).

- [ ] **Step 1: Write the failing tests** — create `gui/tests/test_stats.py`:

```python
"""Aggregation tests for gui/stats.py. Seeded temp SQLite; timestamps are
built with datetime so they live in the server-local timezone the module
buckets by."""
import datetime
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

import play_history  # noqa: E402
import stats  # noqa: E402


def ts(y, m, d, hh=12):
    return int(datetime.datetime(y, m, d, hh).timestamp())


NOW = ts(2026, 7, 15)  # tests pin "now" so current-period defaults are stable


class StatsTestBase(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        play_history.init_db(db_path=self.db)

    def tearDown(self):
        try:
            os.remove(self.db)
        except OSError:
            pass

    def seed(self, title, artist, played_at, ended_at=None, genre=None,
             release_year=None, art_path=None, deleted_at=None):
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO plays (title, artist, played_at, ended_at, genre,"
            " release_year, art_path, deleted_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (title, artist, played_at, ended_at, genre, release_year,
             art_path, deleted_at))
        conn.commit()
        conn.close()


class PeriodBoundsTest(unittest.TestCase):
    def test_month_bounds(self):
        start, end = stats._period_bounds("month", 2026, 7, now=NOW)
        self.assertEqual(start, ts(2026, 7, 1, 0))
        self.assertEqual(end, ts(2026, 8, 1, 0))

    def test_december_rolls_year(self):
        start, end = stats._period_bounds("month", 2025, 12, now=NOW)
        self.assertEqual(end, ts(2026, 1, 1, 0))

    def test_year_bounds(self):
        start, end = stats._period_bounds("year", 2025, None, now=NOW)
        self.assertEqual(start, ts(2025, 1, 1, 0))
        self.assertEqual(end, ts(2026, 1, 1, 0))

    def test_defaults_to_current(self):
        start, end = stats._period_bounds("month", None, None, now=NOW)
        self.assertEqual(start, ts(2026, 7, 1, 0))

    def test_all_starts_at_zero(self):
        start, end = stats._period_bounds("all", None, None, now=NOW)
        self.assertEqual(start, 0)
        self.assertGreater(end, NOW)

    def test_invalid_period_raises(self):
        with self.assertRaises(ValueError):
            stats._period_bounds("week", None, None, now=NOW)
        with self.assertRaises(ValueError):
            stats._period_bounds("month", 2026, 13, now=NOW)


class TotalsTest(StatsTestBase):
    def test_counts_and_uniques(self):
        self.seed("One", "A", ts(2026, 7, 1))
        self.seed("One", "A", ts(2026, 7, 2))
        self.seed("Two", "B", ts(2026, 7, 3))
        self.seed("Old", "C", ts(2025, 1, 1))          # outside period
        self.seed("Gone", "D", ts(2026, 7, 4), deleted_at=1)  # soft-deleted
        out = stats.compute_stats("month", 2026, 7, db_path=self.db, now=NOW)
        self.assertEqual(out["totals"]["plays"], 3)
        self.assertEqual(out["totals"]["unique_tracks"], 2)
        self.assertEqual(out["totals"]["unique_artists"], 2)

    def test_listening_time_real_rows_only_and_capped(self):
        t = ts(2026, 7, 1)
        self.seed("A", "A", t, ended_at=t + 200)        # 200s tracked
        self.seed("B", "B", t, ended_at=t + 9999)       # capped to 2400
        self.seed("C", "C", t, ended_at=t - 50)         # clock skew -> 0
        self.seed("D", "D", t)                          # NULL: excluded
        out = stats.compute_stats("month", 2026, 7, db_path=self.db, now=NOW)
        self.assertEqual(out["totals"]["listening_secs"], 200 + 2400 + 0)
        self.assertEqual(out["totals"]["listening_tracked_plays"], 3)
        self.assertEqual(out["totals"]["plays"], 4)


class TopListsTest(StatsTestBase):
    def test_top_artists_ranked_with_latest_art(self):
        t = ts(2026, 7, 1)
        self.seed("S1", "Beatles", t, art_path="art/1.jpg")
        self.seed("S2", "Beatles", t + 100, art_path="art/2.jpg")
        self.seed("S3", "Doors", t)
        out = stats.compute_stats("month", 2026, 7, db_path=self.db, now=NOW)
        top = out["top_artists"]
        self.assertEqual(top[0]["artist"], "Beatles")
        self.assertEqual(top[0]["plays"], 2)
        self.assertEqual(top[0]["art_path"], "art/2.jpg")  # most recent art
        self.assertEqual(top[1]["artist"], "Doors")

    def test_top_tracks_keyed_by_title_and_artist(self):
        t = ts(2026, 7, 1)
        self.seed("Same", "A", t)
        self.seed("Same", "A", t + 1)
        self.seed("Same", "B", t)   # same title, different artist: separate
        out = stats.compute_stats("month", 2026, 7, db_path=self.db, now=NOW)
        self.assertEqual(out["top_tracks"][0]["plays"], 2)
        self.assertEqual(len(out["top_tracks"]), 2)

    def test_top_lists_capped_at_five(self):
        t = ts(2026, 7, 1)
        for i in range(7):
            self.seed(f"T{i}", f"Artist{i}", t)
        out = stats.compute_stats("month", 2026, 7, db_path=self.db, now=NOW)
        self.assertEqual(len(out["top_artists"]), 5)


class BucketsTest(StatsTestBase):
    def test_month_period_day_buckets_zero_filled(self):
        self.seed("A", "A", ts(2026, 7, 1))
        self.seed("B", "B", ts(2026, 7, 1, 20))
        self.seed("C", "C", ts(2026, 7, 3))
        out = stats.compute_stats("month", 2026, 7, db_path=self.db, now=NOW)
        b = out["plays_over_time"]
        self.assertEqual(b["bucket"], "day")
        self.assertEqual(b["buckets"][0], {"key": "2026-07-01", "plays": 2})
        self.assertEqual(b["buckets"][1], {"key": "2026-07-02", "plays": 0})
        self.assertEqual(b["buckets"][2], {"key": "2026-07-03", "plays": 1})
        # current month clamps at "now" (July 15), not month end
        self.assertEqual(b["buckets"][-1]["key"], "2026-07-15")

    def test_year_period_month_buckets(self):
        self.seed("A", "A", ts(2025, 2, 10))
        out = stats.compute_stats("year", 2025, None, db_path=self.db, now=NOW)
        b = out["plays_over_time"]
        self.assertEqual(b["bucket"], "month")
        self.assertEqual(len(b["buckets"]), 12)  # past year: full 12 months
        self.assertEqual(b["buckets"][1], {"key": "2025-02", "plays": 1})

    def test_all_period_starts_at_first_play(self):
        self.seed("A", "A", ts(2026, 5, 10))
        out = stats.compute_stats("all", None, None, db_path=self.db, now=NOW)
        b = out["plays_over_time"]
        self.assertEqual(b["buckets"][0]["key"], "2026-05")
        self.assertEqual(b["buckets"][-1]["key"], "2026-07")

    def test_all_period_no_plays_empty(self):
        out = stats.compute_stats("all", None, None, db_path=self.db, now=NOW)
        self.assertEqual(out["plays_over_time"]["buckets"], [])


class GenresDecadesTest(StatsTestBase):
    def test_genre_coverage_and_top(self):
        t = ts(2026, 7, 1)
        self.seed("A", "A", t, genre="Rock")
        self.seed("B", "B", t, genre="Rock")
        self.seed("C", "C", t, genre="Jazz")
        self.seed("D", "D", t)  # no genre
        out = stats.compute_stats("month", 2026, 7, db_path=self.db, now=NOW)
        self.assertEqual(out["genres"]["covered"], 3)
        self.assertEqual(out["genres"]["total"], 4)
        self.assertEqual(out["genres"]["top"][0], {"genre": "Rock", "plays": 2})

    def test_decades_from_release_year(self):
        t = ts(2026, 7, 1)
        self.seed("A", "A", t, release_year=1973)
        self.seed("B", "B", t, release_year=1979)
        self.seed("C", "C", t, release_year=1981)
        out = stats.compute_stats("month", 2026, 7, db_path=self.db, now=NOW)
        self.assertEqual(out["decades"]["buckets"][0], {"decade": 1970, "plays": 2})
        self.assertEqual(out["decades"]["buckets"][1], {"decade": 1980, "plays": 1})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd gui && ../.venv/bin/python -m pytest tests/test_stats.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'stats'`

- [ ] **Step 3: Implement** — create `gui/stats.py`:

```python
"""Aggregation queries for the /stats page ("Wrapped"). Synchronous on
purpose — backend_main wraps calls in asyncio.to_thread(), same contract as
play_history.py. Period boundaries and chart buckets use the server's local
timezone. Listening time counts only rows with a real ended_at (no
estimation for pre-feature plays — by design)."""
import datetime

from play_history import _connect

LISTEN_CAP_SECS = 2400  # 40 min: guards clock skew / missed stop frames
TOP_N = 5

_WHERE = "deleted_at IS NULL AND played_at >= ? AND played_at < ?"


def _period_bounds(period: str, year: int | None, month: int | None,
                   now: int | None = None) -> tuple[int, int]:
    """(start, end) unix seconds in server-local time; end is exclusive.
    'all' -> (0, just past now). Raises ValueError on bad period/month."""
    now_dt = (datetime.datetime.fromtimestamp(now) if now is not None
              else datetime.datetime.now())
    if period == "all":
        return 0, int(now_dt.timestamp()) + 1
    if period == "year":
        y = year if year is not None else now_dt.year
        return (int(datetime.datetime(y, 1, 1).timestamp()),
                int(datetime.datetime(y + 1, 1, 1).timestamp()))
    if period == "month":
        y = year if year is not None else now_dt.year
        m = month if month is not None else now_dt.month
        if not 1 <= m <= 12:
            raise ValueError(f"invalid month: {m!r}")
        start = datetime.datetime(y, m, 1)
        end = (datetime.datetime(y + 1, 1, 1) if m == 12
               else datetime.datetime(y, m + 1, 1))
        return int(start.timestamp()), int(end.timestamp())
    raise ValueError(f"invalid period: {period!r}")


def _totals(conn, start, end) -> dict:
    plays, artists = conn.execute(
        f"SELECT COUNT(*), COUNT(DISTINCT artist) FROM plays WHERE {_WHERE}",
        (start, end)).fetchone()
    (tracks,) = conn.execute(
        f"SELECT COUNT(*) FROM (SELECT 1 FROM plays WHERE {_WHERE}"
        " GROUP BY title, artist)", (start, end)).fetchone()
    secs, tracked = conn.execute(
        "SELECT COALESCE(SUM(MAX(0, MIN(ended_at - played_at, ?))), 0), COUNT(*)"
        f" FROM plays WHERE {_WHERE} AND ended_at IS NOT NULL",
        (LISTEN_CAP_SECS, start, end)).fetchone()
    return {"plays": plays, "unique_tracks": tracks, "unique_artists": artists,
            "listening_secs": int(secs), "listening_tracked_plays": tracked}


def _latest_art_subquery(match: str) -> str:
    return (f"(SELECT art_path FROM plays p2 WHERE {match}"
            " AND p2.deleted_at IS NULL AND p2.played_at >= ? AND p2.played_at < ?"
            " AND p2.art_path IS NOT NULL ORDER BY p2.played_at DESC, p2.id DESC"
            " LIMIT 1)")


def _top_artists(conn, start, end) -> list[dict]:
    art = _latest_art_subquery("p2.artist = p.artist")
    rows = conn.execute(
        f"SELECT p.artist, COUNT(*) AS plays, {art} AS art_path"
        f" FROM plays p WHERE {_WHERE}"
        " GROUP BY p.artist ORDER BY plays DESC, p.artist ASC LIMIT ?",
        (start, end, start, end, TOP_N)).fetchall()
    return [dict(r) for r in rows]


def _top_tracks(conn, start, end) -> list[dict]:
    art = _latest_art_subquery("p2.title = p.title AND p2.artist = p.artist")
    rows = conn.execute(
        f"SELECT p.title, p.artist, COUNT(*) AS plays, {art} AS art_path"
        f" FROM plays p WHERE {_WHERE}"
        " GROUP BY p.title, p.artist"
        " ORDER BY plays DESC, p.artist ASC, p.title ASC LIMIT ?",
        (start, end, start, end, TOP_N)).fetchall()
    return [dict(r) for r in rows]


def _bucket_starts(start_dt, last_dt, bucket):
    """All bucket keys from start_dt through last_dt inclusive."""
    keys = []
    cur = start_dt
    while cur <= last_dt:
        if bucket == "day":
            keys.append(cur.strftime("%Y-%m-%d"))
            cur = cur + datetime.timedelta(days=1)
        else:
            keys.append(cur.strftime("%Y-%m"))
            cur = (datetime.datetime(cur.year + 1, 1, 1) if cur.month == 12
                   else datetime.datetime(cur.year, cur.month + 1, 1))
    return keys


def _plays_over_time(conn, period, start, end, now_secs) -> dict:
    bucket = "day" if period == "month" else "month"
    fmt = "%Y-%m-%d" if bucket == "day" else "%Y-%m"
    counts = dict(conn.execute(
        f"SELECT strftime('{fmt}', played_at, 'unixepoch', 'localtime') AS k,"
        f" COUNT(*) FROM plays WHERE {_WHERE} GROUP BY k",
        (start, end)).fetchall())

    if period == "all":
        (first,) = conn.execute(
            "SELECT MIN(played_at) FROM plays WHERE deleted_at IS NULL"
        ).fetchone()
        if first is None:
            return {"bucket": bucket, "buckets": []}
        start_dt = datetime.datetime.fromtimestamp(first)
    else:
        start_dt = datetime.datetime.fromtimestamp(start)

    # Clamp the zero-filled range at "now" so a current period doesn't chart
    # the future; past periods run to their real end.
    last = min(end - 1, now_secs)
    last_dt = datetime.datetime.fromtimestamp(last)
    if bucket == "day":
        start_dt = datetime.datetime(start_dt.year, start_dt.month, start_dt.day)
        last_dt = datetime.datetime(last_dt.year, last_dt.month, last_dt.day)
    else:
        start_dt = datetime.datetime(start_dt.year, start_dt.month, 1)
        last_dt = datetime.datetime(last_dt.year, last_dt.month, 1)

    keys = _bucket_starts(start_dt, last_dt, bucket)
    return {"bucket": bucket,
            "buckets": [{"key": k, "plays": counts.get(k, 0)} for k in keys]}


def _genres(conn, start, end, total) -> dict:
    rows = conn.execute(
        f"SELECT genre, COUNT(*) AS plays FROM plays WHERE {_WHERE}"
        " AND genre IS NOT NULL GROUP BY genre"
        " ORDER BY plays DESC, genre ASC LIMIT ?",
        (start, end, TOP_N)).fetchall()
    (covered,) = conn.execute(
        f"SELECT COUNT(*) FROM plays WHERE {_WHERE} AND genre IS NOT NULL",
        (start, end)).fetchone()
    return {"covered": covered, "total": total,
            "top": [{"genre": r["genre"], "plays": r["plays"]} for r in rows]}


def _decades(conn, start, end, total) -> dict:
    rows = conn.execute(
        f"SELECT (release_year / 10) * 10 AS decade, COUNT(*) AS plays"
        f" FROM plays WHERE {_WHERE} AND release_year IS NOT NULL"
        " GROUP BY decade ORDER BY decade ASC",
        (start, end)).fetchall()
    (covered,) = conn.execute(
        f"SELECT COUNT(*) FROM plays WHERE {_WHERE} AND release_year IS NOT NULL",
        (start, end)).fetchone()
    return {"covered": covered, "total": total,
            "buckets": [{"decade": r["decade"], "plays": r["plays"]} for r in rows]}


def compute_stats(period: str, year: int | None = None, month: int | None = None,
                  db_path: str | None = None, now: int | None = None) -> dict:
    start, end = _period_bounds(period, year, month, now=now)
    now_secs = now if now is not None else int(datetime.datetime.now().timestamp())
    with _connect(db_path) as conn:
        totals = _totals(conn, start, end)
        return {
            "period": {"kind": period, "year": year, "month": month,
                        "start": start, "end": end},
            "totals": totals,
            "top_artists": _top_artists(conn, start, end),
            "top_tracks": _top_tracks(conn, start, end),
            "plays_over_time": _plays_over_time(conn, period, start, end, now_secs),
            "genres": _genres(conn, start, end, totals["plays"]),
            "decades": _decades(conn, start, end, totals["plays"]),
        }
```

SQLite's `MAX`/`MIN` two-argument scalar forms are used in `_totals`; both
are available in every SQLite bundled with Python 3.11.

- [ ] **Step 4: Run tests until green, then the full gui suite**

Run: `cd gui && ../.venv/bin/python -m pytest tests/test_stats.py -q` then `cd gui && ../.venv/bin/python -m pytest tests -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add gui/stats.py gui/tests/test_stats.py
git commit -m "feat(stats): aggregation module — totals, top lists, buckets, genres/decades

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: `GET /api/stats` endpoint + `/stats` page route + nav item

**Files:**
- Modify: `gui/backend_main.py` (import; page route after `/settings` ~line 160; API route after `/api/plays` routes ~line 348)
- Modify: `gui/templates/_layout.html` (nav_items list ~line 91)
- Test: `gui/tests/test_stats_api.py` (create)

**Interfaces:**
- Consumes: `stats.compute_stats(period, year, month)` (Task 4; raises ValueError on bad input).
- Produces: `GET /api/stats?period=&year=&month=` (JSON, 400 on invalid); `GET /stats` page rendering `stats.html` with `current_page="stats"` (template itself is Task 6; the route test only checks the API).

- [ ] **Step 1: Write the failing tests** — create `gui/tests/test_stats_api.py`:

```python
import os
import sys
import tempfile
import unittest

from fastapi.testclient import TestClient

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

import play_history  # noqa: E402
import backend_main  # noqa: E402


class StatsApiTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        play_history.init_db(db_path=self.db_path)
        self._orig = play_history.DB_PATH
        play_history.DB_PATH = self.db_path
        self.client = TestClient(backend_main.app)  # no `with`: lifespan stays off

    def tearDown(self):
        play_history.DB_PATH = self._orig
        self.client.close()
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def test_empty_db_shape(self):
        body = self.client.get("/api/stats?period=all").json()
        self.assertEqual(body["totals"]["plays"], 0)
        self.assertEqual(body["top_artists"], [])
        self.assertEqual(body["plays_over_time"]["buckets"], [])
        for key in ("period", "totals", "top_tracks", "genres", "decades"):
            self.assertIn(key, body)

    def test_counts_a_play(self):
        play_history.record_play("T", "A", None, None, db_path=self.db_path)
        body = self.client.get("/api/stats?period=all").json()
        self.assertEqual(body["totals"]["plays"], 1)
        self.assertEqual(body["top_artists"][0]["artist"], "A")

    def test_invalid_period_400(self):
        self.assertEqual(self.client.get("/api/stats?period=week").status_code, 400)

    def test_invalid_month_400(self):
        self.assertEqual(
            self.client.get("/api/stats?period=month&month=13").status_code, 400)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd gui && ../.venv/bin/python -m pytest tests/test_stats_api.py -q`
Expected: FAIL — 404s (route doesn't exist)

- [ ] **Step 3: Implement** — in `gui/backend_main.py`:

Add to the local imports (after `import play_history`): `import stats`.

Page route after the `/settings` route:

```python
@app.get("/stats")
async def stats_page(request: Request):
    return templates.TemplateResponse(
        "stats.html", {"request": request, "current_page": "stats"}
    )
```

API route after the `/api/plays/{play_id}/restore` route:

```python
@app.get("/api/stats")
async def get_stats(period: str = "month", year: int | None = None,
                    month: int | None = None):
    try:
        return await asyncio.to_thread(stats.compute_stats, period, year, month)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
```

In `gui/templates/_layout.html`, extend nav_items (both nav bars iterate it — one edit covers desktop + mobile):

```jinja
{% set nav_items = [
  ("dashboard", "/",         "Dashboard", "dashboard"),
  ("history",   "/history",  "History",   "history"),
  ("stats",     "/stats",    "Stats",     "insights"),
  ("settings",  "/settings", "Settings",  "settings")
] %}
```

Task 6 creates `stats.html`; until then `GET /stats` 500s — the API tests here don't touch it, so the suite stays green. (If you must run the page route before Task 6, skip — it's covered by Task 6's verification.)

- [ ] **Step 4: Run the full gui suite**

Run: `cd gui && ../.venv/bin/python -m pytest tests -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add gui/backend_main.py gui/templates/_layout.html gui/tests/test_stats_api.py
git commit -m "feat(api): /api/stats endpoint, /stats page route, Stats nav item

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Stats page UI (`stats.html` + `stats.js`)

**Files:**
- Create: `gui/templates/stats.html`
- Create: `gui/static/stats.js`

**Interfaces:**
- Consumes: `GET /api/stats?period=month|year|all` (Task 5 response shape). Existing globals: `window.SpinSense` shell (loaded by `_layout.html`), `asset_v` template global.
- Produces: user-facing page; no JS exports.

- [ ] **Step 1: Create `gui/templates/stats.html`:**

```html
{% extends "_layout.html" %}

{% block title %}SpinSense — Stats{% endblock %}

{% block content %}
<div class="max-w-4xl mx-auto flex flex-col gap-lg">

  <header class="flex flex-wrap items-center justify-between gap-md">
    <div>
      <h2 class="font-headline text-headline-lg text-on-background">Stats</h2>
      <p class="text-body-sm text-on-surface-variant mt-1">Your listening, by the numbers.</p>
    </div>
    <div id="stats-period" class="flex items-center gap-1 bg-surface-container/60 border border-outline-variant/40 rounded-full p-1" role="tablist" aria-label="Period">
      <button type="button" data-period="month" class="stats-period-btn px-4 py-1.5 rounded-full text-label-md transition-colors" role="tab">This month</button>
      <button type="button" data-period="year" class="stats-period-btn px-4 py-1.5 rounded-full text-label-md transition-colors" role="tab">This year</button>
      <button type="button" data-period="all" class="stats-period-btn px-4 py-1.5 rounded-full text-label-md transition-colors" role="tab">All time</button>
    </div>
  </header>

  <div id="stats-empty" class="hidden glass-panel rounded-xl p-lg text-center flex flex-col items-center gap-xs">
    <span class="material-symbols-outlined text-on-surface-variant" style="font-size: 48px;">insights</span>
    <p class="font-headline text-headline-md text-on-background">No plays in this period</p>
    <p class="text-body-sm text-on-surface-variant max-w-sm">Spin some records and your stats will build up here.</p>
  </div>

  <div id="stats-body" class="flex flex-col gap-lg">

    <!-- Headline tiles -->
    <div class="grid grid-cols-2 md:grid-cols-4 gap-gutter">
      <div class="glass-panel rounded-xl p-md">
        <p class="text-label-md text-on-surface-variant uppercase tracking-widest">Plays</p>
        <p id="stat-plays" class="font-headline text-headline-lg text-on-background mt-1 tabular-nums">—</p>
      </div>
      <div class="glass-panel rounded-xl p-md">
        <p class="text-label-md text-on-surface-variant uppercase tracking-widest">Artists</p>
        <p id="stat-artists" class="font-headline text-headline-lg text-on-background mt-1 tabular-nums">—</p>
      </div>
      <div class="glass-panel rounded-xl p-md">
        <p class="text-label-md text-on-surface-variant uppercase tracking-widest">Tracks</p>
        <p id="stat-tracks" class="font-headline text-headline-lg text-on-background mt-1 tabular-nums">—</p>
      </div>
      <div class="glass-panel rounded-xl p-md">
        <p class="text-label-md text-on-surface-variant uppercase tracking-widest">Listening</p>
        <p id="stat-listening" class="font-headline text-headline-lg text-on-background mt-1 tabular-nums">—</p>
        <p id="stat-listening-note" class="text-label-sm text-on-surface-variant mt-0.5"></p>
      </div>
    </div>

    <!-- Top artists / top tracks -->
    <div class="grid grid-cols-1 md:grid-cols-2 gap-gutter">
      <section class="glass-panel rounded-xl p-md">
        <h3 class="text-label-md text-on-surface-variant uppercase tracking-widest mb-md">Top artists</h3>
        <ol id="stats-top-artists" class="flex flex-col gap-2"></ol>
      </section>
      <section class="glass-panel rounded-xl p-md">
        <h3 class="text-label-md text-on-surface-variant uppercase tracking-widest mb-md">Top tracks</h3>
        <ol id="stats-top-tracks" class="flex flex-col gap-2"></ol>
      </section>
    </div>

    <!-- Plays over time -->
    <section class="glass-panel rounded-xl p-md">
      <h3 class="text-label-md text-on-surface-variant uppercase tracking-widest mb-md">Plays over time</h3>
      <div id="stats-chart" class="flex items-end gap-px h-40" role="img" aria-label="Plays per period"></div>
      <div class="flex justify-between mt-2 text-label-sm text-on-surface-variant">
        <span id="stats-chart-start"></span>
        <span id="stats-chart-end"></span>
      </div>
    </section>

    <!-- Genres + decades -->
    <div class="grid grid-cols-1 md:grid-cols-2 gap-gutter">
      <section class="glass-panel rounded-xl p-md">
        <h3 class="text-label-md text-on-surface-variant uppercase tracking-widest mb-md">Genres</h3>
        <div id="stats-genres" class="flex flex-col gap-2"></div>
        <p id="stats-genres-note" class="text-label-sm text-on-surface-variant mt-sm"></p>
      </section>
      <section class="glass-panel rounded-xl p-md">
        <h3 class="text-label-md text-on-surface-variant uppercase tracking-widest mb-md">Decades</h3>
        <div id="stats-decades" class="flex flex-col gap-2"></div>
        <p id="stats-decades-note" class="text-label-sm text-on-surface-variant mt-sm"></p>
      </section>
    </div>

  </div>
</div>
{% endblock %}

{% block scripts %}
<script src="/static/stats.js?v={{ asset_v }}"></script>
{% endblock %}
```

- [ ] **Step 2: Create `gui/static/stats.js`:**

```javascript
// stats.js — the /stats page. One fetch per period selection; every module
// renders from the single /api/stats blob. Charts are plain divs with
// percentage widths/heights — no chart library.
(function () {
  const $ = (id) => document.getElementById(id);
  const PERIOD_WRAP = $("stats-period");
  const EMPTY = $("stats-empty");
  const BODY = $("stats-body");

  let period = "month";

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;",
      '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function fmtListening(secs) {
    if (secs < 60) return `${secs}s`;
    const mins = Math.round(secs / 60);
    if (mins < 60) return `${mins}m`;
    return `${Math.floor(mins / 60)}h ${mins % 60}m`;
  }

  function setActiveButton() {
    PERIOD_WRAP.querySelectorAll(".stats-period-btn").forEach((b) => {
      const active = b.dataset.period === period;
      b.classList.toggle("bg-primary", active);
      b.classList.toggle("text-on-primary", active);
      b.classList.toggle("text-on-surface-variant", !active);
      b.setAttribute("aria-selected", active ? "true" : "false");
    });
  }

  function rankRow(rank, art, primary, secondary, plays, maxPlays) {
    const pct = maxPlays > 0 ? Math.max(4, (plays / maxPlays) * 100) : 0;
    const thumb = art
      ? `<img src="/${escapeHtml(art)}" alt="" class="w-10 h-10 rounded object-cover shrink-0 bg-surface-container-high" onerror="this.src='/static/placeholder.jpg'">`
      : `<span class="w-10 h-10 rounded shrink-0 bg-surface-container-high flex items-center justify-center"><span class="material-symbols-outlined text-outline" style="font-size:20px;">album</span></span>`;
    return `
      <li class="flex items-center gap-3">
        <span class="text-label-md text-outline tabular-nums w-4 text-right shrink-0">${rank}</span>
        ${thumb}
        <div class="flex-1 min-w-0">
          <p class="text-body-sm text-on-surface truncate">${primary}</p>
          ${secondary ? `<p class="text-label-sm text-on-surface-variant truncate">${secondary}</p>` : ""}
          <div class="h-1 mt-1 rounded-full bg-surface-container-highest overflow-hidden">
            <div class="h-full bg-primary/70" style="width:${pct}%"></div>
          </div>
        </div>
        <span class="text-label-sm text-on-surface-variant tabular-nums shrink-0">${plays}</span>
      </li>`;
  }

  function renderTopLists(data) {
    const artists = data.top_artists || [];
    const tracks = data.top_tracks || [];
    const maxA = artists.length ? artists[0].plays : 0;
    const maxT = tracks.length ? tracks[0].plays : 0;
    $("stats-top-artists").innerHTML = artists.length
      ? artists.map((a, i) => rankRow(i + 1, a.art_path, escapeHtml(a.artist), "", a.plays, maxA)).join("")
      : '<li class="text-body-sm text-on-surface-variant">No plays yet.</li>';
    $("stats-top-tracks").innerHTML = tracks.length
      ? tracks.map((t, i) => rankRow(i + 1, t.art_path, escapeHtml(t.title), escapeHtml(t.artist), t.plays, maxT)).join("")
      : '<li class="text-body-sm text-on-surface-variant">No plays yet.</li>';
  }

  function renderChart(data) {
    const buckets = (data.plays_over_time && data.plays_over_time.buckets) || [];
    const max = buckets.reduce((m, b) => Math.max(m, b.plays), 0);
    $("stats-chart").innerHTML = buckets.map((b) => {
      const pct = max > 0 ? (b.plays / max) * 100 : 0;
      return `<div class="flex-1 rounded-t bg-primary/70 hover:bg-primary transition-colors"
                   style="height:${Math.max(pct, b.plays > 0 ? 4 : 1)}%"
                   title="${escapeHtml(b.key)}: ${b.plays} ${b.plays === 1 ? "play" : "plays"}"></div>`;
    }).join("");
    $("stats-chart-start").textContent = buckets.length ? buckets[0].key : "";
    $("stats-chart-end").textContent = buckets.length ? buckets[buckets.length - 1].key : "";
  }

  function barList(el, rows, labelOf, noteEl, noteData, noun) {
    const max = rows.reduce((m, r) => Math.max(m, r.plays), 0);
    el.innerHTML = rows.length ? rows.map((r) => `
      <div class="flex items-center gap-3">
        <span class="text-body-sm text-on-surface w-24 truncate shrink-0">${escapeHtml(labelOf(r))}</span>
        <div class="flex-1 h-2 rounded-full bg-surface-container-highest overflow-hidden">
          <div class="h-full bg-secondary/70" style="width:${max > 0 ? (r.plays / max) * 100 : 0}%"></div>
        </div>
        <span class="text-label-sm text-on-surface-variant tabular-nums shrink-0">${r.plays}</span>
      </div>`).join("")
      : `<p class="text-body-sm text-on-surface-variant">No ${noun} data yet — it accrues as tracks are identified.</p>`;
    noteEl.textContent = (rows.length && noteData.covered < noteData.total)
      ? `${noteData.covered} of ${noteData.total} plays have ${noun} data.` : "";
  }

  function render(data) {
    const t = data.totals || {};
    const noPlays = !t.plays;
    EMPTY.classList.toggle("hidden", !noPlays);
    BODY.classList.toggle("hidden", noPlays);
    if (noPlays) return;

    $("stat-plays").textContent = t.plays;
    $("stat-artists").textContent = t.unique_artists;
    $("stat-tracks").textContent = t.unique_tracks;
    $("stat-listening").textContent = t.listening_tracked_plays > 0
      ? fmtListening(t.listening_secs) : "—";
    $("stat-listening-note").textContent =
      t.listening_tracked_plays < t.plays && t.listening_tracked_plays > 0
        ? `across ${t.listening_tracked_plays} of ${t.plays} plays`
        : (t.listening_tracked_plays === 0 ? "tracked from now on" : "");

    renderTopLists(data);
    renderChart(data);
    barList($("stats-genres"), (data.genres && data.genres.top) || [],
            (r) => r.genre, $("stats-genres-note"), data.genres || {covered: 0, total: 0}, "genre");
    barList($("stats-decades"), (data.decades && data.decades.buckets) || [],
            (r) => `${r.decade}s`, $("stats-decades-note"), data.decades || {covered: 0, total: 0}, "decade");
  }

  async function load() {
    setActiveButton();
    try {
      const res = await fetch(`/api/stats?period=${period}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      render(await res.json());
    } catch (e) {
      console.error("stats fetch failed:", e);
      EMPTY.classList.remove("hidden");
      BODY.classList.add("hidden");
    }
  }

  PERIOD_WRAP.addEventListener("click", (e) => {
    const btn = e.target.closest(".stats-period-btn");
    if (!btn || btn.dataset.period === period) return;
    period = btn.dataset.period;
    load();
  });

  load();
})();
```

- [ ] **Step 3: Verify in the running app** — start the GUI against a scratch data dir, seed a few plays, and drive the page:

```bash
SCRATCH=$(mktemp -d)
cd gui && SPINSENSE_DATA_DIR=$SCRATCH ../.venv/bin/python -m uvicorn backend_main:app --port 3399 &
sleep 3
curl -s -o /dev/null http://127.0.0.1:3399/api/setup-state   # forces config.json creation
../.venv/bin/python - <<EOF
import json, sys, time
sys.path.insert(0, ".")
import os
os.environ["SPINSENSE_DATA_DIR"] = "$SCRATCH"
# reload with env set
import importlib, play_history
importlib.reload(play_history)
play_history.init_db()
p1 = play_history.record_play("Midnight City", "M83", "Hurry Up, We're Dreaming", None, genre="Electronic", release_year=2011, duration_secs=244)
play_history.set_ended_at(p1, int(time.time()))
play_history.record_play("Riders on the Storm", "The Doors", "L.A. Woman", None, genre="Rock", release_year=1971)
cfg = json.load(open("$SCRATCH/config.json")); cfg["System"]["Setup_Wizard_State"] = "completed"
json.dump(cfg, open("$SCRATCH/config.json", "w"))
EOF
curl -s "http://127.0.0.1:3399/api/stats?period=all" | head -c 400
```

Then check `http://127.0.0.1:3399/stats` in a browser (or the gstack browse tool): nav shows a 4th "Stats" item; tiles populate; both period toggles refetch; "Listening" tile shows a value with the "across 1 of 2 plays" caption; genres/decades bars render. Kill the uvicorn process when done.

- [ ] **Step 4: Run both full suites** (guard against template/JS regressions in page-route tests)

Run: `cd gui && ../.venv/bin/python -m pytest tests -q && cd ../core && ../.venv/bin/python -m pytest tests -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add gui/templates/stats.html gui/static/stats.js
git commit -m "feat(stats): Stats page UI — tiles, top lists, plays chart, genres/decades

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Docs — CHANGELOG, ROADMAP, README

**Files:**
- Modify: `CHANGELOG.md` (top `## [Unreleased]` section — extend it if it still exists from the 1.5.1.1-era quick wins, else create it above the newest release heading)
- Modify: `ROADMAP.md` (remove the "Listening analytics / Wrapped" feature bullet; leave "Database export / import")
- Modify: `README.md` (add a features bullet)

**Interfaces:** none (docs only).

- [ ] **Step 1: CHANGELOG** — add under `## [Unreleased]` → `### Added`:

```markdown
- **Stats page ("Wrapped").** A new 4th nav page: top artists and tracks (with art), plays-over-time chart, genres and decades breakdowns, and headline totals — filterable by month / year / all-time. Listening time is measured for real from now on (new `ended_at`/`duration_secs` columns; old plays are counted everywhere except listening time — no estimates). The schema now also satisfies every future Last.fm scrobbling requirement (start timestamp, duration, played-length eligibility), so a scrobbler can be added without another migration.
```

- [ ] **Step 2: ROADMAP** — delete the `- **Listening analytics / "Wrapped"** …` bullet from `## Features`. Add under `## Features` (the story-mode follow-up we explicitly deferred):

```markdown
- **Wrapped story mode** — a swipeable year-in-review recap (big reveal cards) layered on the Stats API (`/api/stats?period=year&year=N`). The Stats page (shipped) is the data foundation; this is pure UI.
- **Last.fm scrobbling** — the `plays` table now records everything track.scrobble needs (`played_at` start timestamp, `duration_secs`, `ended_at` for the ≥half-or-4-min eligibility rule); remaining work is auth + the submission client.
```

- [ ] **Step 3: README** — after the "Zero-config Home Assistant discovery" features bullet, add:

```markdown
- **Listening stats** — a Wrapped-style Stats page: top artists and tracks, plays over time, genres and decades, filterable by month / year / all-time.
```

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md ROADMAP.md README.md
git commit -m "docs: changelog/roadmap/readme for the Stats page

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Final verification (after all tasks)

- [ ] Both suites green from their package dirs (`core/` 70 tests, `gui/` ~85 — counts approximate).
- [ ] Manual smoke per Task 6 Step 3 if not already done.
- [ ] `git log --oneline` shows the 7 task commits; working tree clean except the intentionally-untracked `DESIGN.md`.
- [ ] Do NOT push or release — the user decides when to ship (release will be 1.6.0.0-ish; VERSION bump happens at release time per repo convention).

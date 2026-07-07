# Album / Edition Intelligence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatic album-edition reconciliation within same-artist listening sessions, a manual album picker on History (iTunes candidates + free text, lockable, run-scoped), and a Top Albums stats module.

**Architecture:** A new pure/sync `gui/reconcile.py` owns title normalization, winner picking, session-run detection, and the rewrite; `ipc_manager` triggers it after each recorded play. Two new API routes expose iTunes album candidates and the album update. `gui/stats.py` gains a `top_albums` aggregate; the Stats and History pages get the corresponding UI.

**Tech Stack:** Python 3.11 / FastAPI / SQLite / aiohttp (existing dep) / Jinja2 + vanilla JS. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-07-album-edition-reconciliation-design.md`

## Global Constraints

- Tests run from inside the package dirs: `cd gui && ../.venv/bin/python -m pytest tests -q` (repo root fails collection by design). Venv at repo root.
- No new runtime dependencies.
- `SESSION_GAP_SECS = 1800` (a run = contiguous same-artist plays, consecutive gaps < 1800s). Soft-deleted rows invisible everywhere.
- Locked rows (`album_locked = 1`) neither vote nor get rewritten by auto-reconciliation; an explicit `apply_to_run` POST overrides locks.
- Edition markers never include `live`, `acoustic`, `demos`, `unplugged`; possessive re-recordings (`\w+['’]s\s+version`, e.g. "Taylor's Version") never merge and are checked BEFORE the generic `version` marker.
- A reconcile failure must never block or crash play recording.
- Commit messages end with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- Do NOT push or release; the user decides when to ship.

---

### Task 1: `album_locked` column + `set_album` + `get_play` helpers

**Files:**
- Modify: `gui/play_history.py` (`_ENRICHMENT_COLUMNS` ~line 24; new helpers after `set_ended_at`)
- Test: `gui/tests/test_play_history.py` (append a TestCase)

**Interfaces:**
- Produces: `play_history.set_album(play_id: int, album: str, locked: bool = True, db_path: str | None = None) -> bool` (False if no live row); `play_history.get_play(play_id: int, db_path: str | None = None) -> dict | None` (full row as dict, live rows only); migration column `album_locked INTEGER` (NULL = auto-managed).

- [ ] **Step 1: Write the failing tests** — append to `gui/tests/test_play_history.py`:

```python
class AlbumHelpersTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        play_history.init_db(db_path=self.db_path)

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def test_album_locked_defaults_null(self):
        pid = play_history.record_play("T", "A", "Alb", None, db_path=self.db_path)
        row = play_history.get_play(pid, db_path=self.db_path)
        self.assertIsNone(row["album_locked"])

    def test_set_album_updates_and_locks(self):
        pid = play_history.record_play("T", "A", "Old", None, db_path=self.db_path)
        ok = play_history.set_album(pid, "New (Deluxe)", db_path=self.db_path)
        self.assertTrue(ok)
        row = play_history.get_play(pid, db_path=self.db_path)
        self.assertEqual(row["album"], "New (Deluxe)")
        self.assertEqual(row["album_locked"], 1)

    def test_set_album_unknown_id_false(self):
        self.assertFalse(play_history.set_album(999, "X", db_path=self.db_path))

    def test_get_play_hides_soft_deleted(self):
        pid = play_history.record_play("T", "A", None, None, db_path=self.db_path)
        play_history.delete_play(pid, db_path=self.db_path)
        self.assertIsNone(play_history.get_play(pid, db_path=self.db_path))
```

- [ ] **Step 2: Run to verify failure**

Run: `cd gui && ../.venv/bin/python -m pytest tests/test_play_history.py::AlbumHelpersTest -v`
Expected: FAIL — `no attribute 'get_play'`

- [ ] **Step 3: Implement** — in `gui/play_history.py`:

Add to `_ENRICHMENT_COLUMNS` (after `duration_secs`):

```python
    # Album/edition reconciliation (2026-07): 1 = album set manually, never
    # auto-rewritten. NULL/0 = auto-managed.
    "album_locked": "INTEGER",
```

New helpers after `set_ended_at`:

```python
def get_play(play_id: int, db_path: str | None = None) -> dict | None:
    """One live (non-deleted) play row as a dict, or None."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM plays WHERE id = ? AND deleted_at IS NULL", (play_id,)
        ).fetchone()
        return dict(row) if row is not None else None


def set_album(play_id: int, album: str, locked: bool = True,
              db_path: str | None = None) -> bool:
    """Set a play's album. `locked` marks it manually-set so auto
    reconciliation leaves it alone. Returns True if a live row changed."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE plays SET album = ?, album_locked = ? "
            "WHERE id = ? AND deleted_at IS NULL",
            (album, 1 if locked else 0, play_id),
        )
        return cur.rowcount > 0
```

- [ ] **Step 4: Full gui suite**

Run: `cd gui && ../.venv/bin/python -m pytest tests -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add gui/play_history.py gui/tests/test_play_history.py
git commit -m "feat(history): album_locked column, set_album/get_play helpers

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: `reconcile.py` pure functions — `base_title` + `pick_winner`

**Files:**
- Create: `gui/reconcile.py` (pure functions only in this task)
- Test: `gui/tests/test_reconcile.py` (create)

**Interfaces:**
- Produces: `reconcile.base_title(album: str) -> str` (normalized base; empty string for empty/None-ish input); `reconcile.pick_winner(albums: list[tuple[str, int]]) -> str` (input `(album, played_at)` pairs, longest raw string wins, tie → most recent); constant `SESSION_GAP_SECS = 1800`.

- [ ] **Step 1: Write the failing tests** — create `gui/tests/test_reconcile.py`:

```python
"""Tests for album/edition reconciliation. Pure-function table tests here;
run/rewrite DB tests are added by a later task in this same file."""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

import play_history  # noqa: E402  (used by DB tests in a later task)
import reconcile  # noqa: E402


class BaseTitleTest(unittest.TestCase):
    def test_equivalences(self):
        base = reconcile.base_title("Abbey Road")
        for variant in [
            "Abbey Road (Super Deluxe Edition)",
            "Abbey Road - 50th Anniversary",
            "Abbey Road (Deluxe Edition) [2019 Remaster]",
            "Abbey Road [2014]",
            "Abbey Road (Bonus Track Version)",
            "abbey  road",
        ]:
            self.assertEqual(reconcile.base_title(variant), base, variant)

    def test_non_merges(self):
        studio = reconcile.base_title("At Folsom Prison")
        self.assertNotEqual(reconcile.base_title("At Folsom Prison (Live)"), studio)
        self.assertNotEqual(reconcile.base_title("Nevermind (Acoustic)"),
                            reconcile.base_title("Nevermind"))

    def test_taylors_version_never_merges(self):
        base = reconcile.base_title("1989")
        self.assertNotEqual(reconcile.base_title("1989 (Taylor's Version)"), base)
        # curly apostrophe form
        self.assertNotEqual(reconcile.base_title("1989 (Taylor’s Version)"), base)
        # but a deluxe qualifier ON TOP of Taylor's Version still strips
        self.assertEqual(
            reconcile.base_title("1989 (Taylor's Version) [Deluxe]"),
            reconcile.base_title("1989 (Taylor's Version)"),
        )

    def test_dash_only_strips_qualifiers(self):
        self.assertEqual(reconcile.base_title("Blood - Sugar - Deluxe Edition"),
                         reconcile.base_title("Blood - Sugar"))
        # a non-qualifier dash segment stays
        self.assertNotEqual(reconcile.base_title("First - Last"),
                            reconcile.base_title("First"))
        # hyphenated words (no spaces) never strip
        self.assertEqual(reconcile.base_title("X-Ray"), "x-ray")

    def test_empty_and_none_safe(self):
        self.assertEqual(reconcile.base_title(""), "")
        self.assertEqual(reconcile.base_title(None), "")


class PickWinnerTest(unittest.TestCase):
    def test_longest_wins(self):
        albums = [("Abbey Road", 100), ("Abbey Road (Super Deluxe Edition)", 50)]
        self.assertEqual(reconcile.pick_winner(albums),
                         "Abbey Road (Super Deluxe Edition)")

    def test_tie_breaks_most_recent(self):
        albums = [("Album (Deluxe A)", 100), ("Album (Deluxe B)", 200)]
        self.assertEqual(reconcile.pick_winner(albums), "Album (Deluxe B)")
```

- [ ] **Step 2: Run to verify failure**

Run: `cd gui && ../.venv/bin/python -m pytest tests/test_reconcile.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'reconcile'`

- [ ] **Step 3: Implement** — create `gui/reconcile.py`:

```python
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
```

- [ ] **Step 4: Run to verify pass, then full gui suite**

Run: `cd gui && ../.venv/bin/python -m pytest tests/test_reconcile.py -q` then `../.venv/bin/python -m pytest tests -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add gui/reconcile.py gui/tests/test_reconcile.py
git commit -m "feat(reconcile): base_title normalizer + edition winner picking

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Run detection + `reconcile_album` + `apply_album_to_run` + ipc trigger

**Files:**
- Modify: `gui/reconcile.py` (append DB functions)
- Modify: `gui/ipc_manager.py` (trigger after record; extract `spawn_art_download` helper)
- Test: `gui/tests/test_reconcile.py` (append TestCases)

**Interfaces:**
- Consumes: Task 1's `album_locked` column; Task 2's pure functions.
- Produces: `reconcile.find_run(play_id: int, db_path: str | None = None) -> list[dict]` (run rows as dicts: id, artist, album, played_at, album_locked; empty if play missing/deleted); `reconcile.reconcile_album(play_id: int, db_path: str | None = None) -> int` (rows rewritten); `reconcile.apply_album_to_run(play_id: int, album: str, db_path: str | None = None) -> list[int]` (all run ids updated+locked; empty if play missing); `ipc_manager.spawn_art_download(play_id: int, art_url: str) -> None`.

- [ ] **Step 1: Write the failing tests** — append to `gui/tests/test_reconcile.py`:

```python
import asyncio  # noqa: E402
import sqlite3  # noqa: E402

import ipc_manager  # noqa: E402


class ReconcileDbBase(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        play_history.init_db(db_path=self.db)

    def tearDown(self):
        try:
            os.remove(self.db)
        except OSError:
            pass

    def seed(self, artist, album, played_at, locked=None, deleted=None):
        conn = sqlite3.connect(self.db)
        cur = conn.execute(
            "INSERT INTO plays (title, artist, album, played_at, album_locked,"
            " deleted_at) VALUES (?, ?, ?, ?, ?, ?)",
            (f"t{played_at}", artist, album, played_at, locked, deleted))
        conn.commit()
        pid = cur.lastrowid
        conn.close()
        return pid

    def albums(self):
        conn = sqlite3.connect(self.db)
        rows = conn.execute("SELECT album FROM plays ORDER BY id").fetchall()
        conn.close()
        return [r[0] for r in rows]


class FindRunTest(ReconcileDbBase):
    def test_run_bounded_by_gap_and_artist(self):
        self.seed("A", "X", 1000)
        p2 = self.seed("A", "X", 2000)
        self.seed("A", "X", 2000 + reconcile.SESSION_GAP_SECS + 1)  # gap: new run
        self.seed("B", "Y", 2500)  # other artist: not in run
        run = reconcile.find_run(p2, db_path=self.db)
        self.assertEqual([r["played_at"] for r in run], [1000, 2000])

    def test_missing_play_empty(self):
        self.assertEqual(reconcile.find_run(999, db_path=self.db), [])


class ReconcileAlbumTest(ReconcileDbBase):
    def test_rewrites_backwards(self):
        self.seed("A", "Abbey Road", 1000)
        self.seed("A", "Abbey Road", 1100)
        p3 = self.seed("A", "Abbey Road (Super Deluxe Edition)", 1200)
        changed = reconcile.reconcile_album(p3, db_path=self.db)
        self.assertEqual(changed, 2)
        self.assertEqual(set(self.albums()), {"Abbey Road (Super Deluxe Edition)"})

    def test_rewrites_forwards(self):
        self.seed("A", "Abbey Road (Super Deluxe Edition)", 1000)
        p2 = self.seed("A", "Abbey Road", 1100)
        reconcile.reconcile_album(p2, db_path=self.db)
        self.assertEqual(set(self.albums()), {"Abbey Road (Super Deluxe Edition)"})

    def test_locked_rows_neither_vote_nor_rewrite(self):
        p1 = self.seed("A", "Abbey Road (My Special Long Locked Edition)", 1000,
                       locked=1)
        p2 = self.seed("A", "Abbey Road", 1100)
        reconcile.reconcile_album(p2, db_path=self.db)
        albums = self.albums()
        self.assertEqual(albums[0], "Abbey Road (My Special Long Locked Edition)")
        self.assertEqual(albums[1], "Abbey Road")  # locked row didn't drag it

    def test_locked_trigger_is_noop(self):
        self.seed("A", "Abbey Road", 1000)
        p2 = self.seed("A", "Abbey Road (Deluxe Edition)", 1100, locked=1)
        self.assertEqual(reconcile.reconcile_album(p2, db_path=self.db), 0)

    def test_different_base_titles_untouched(self):
        self.seed("A", "Revolver", 1000)
        p2 = self.seed("A", "Abbey Road (Deluxe Edition)", 1100)
        reconcile.reconcile_album(p2, db_path=self.db)
        self.assertIn("Revolver", self.albums())

    def test_gap_splits_runs(self):
        self.seed("A", "Abbey Road", 1000)
        p2 = self.seed("A", "Abbey Road (Deluxe Edition)",
                       1000 + reconcile.SESSION_GAP_SECS + 100)
        reconcile.reconcile_album(p2, db_path=self.db)
        self.assertIn("Abbey Road", self.albums())  # earlier run untouched

    def test_soft_deleted_ignored(self):
        self.seed("A", "Abbey Road", 1000, deleted=1)
        p2 = self.seed("A", "Abbey Road (Deluxe Edition)", 1100)
        reconcile.reconcile_album(p2, db_path=self.db)
        self.assertIn("Abbey Road", self.albums())

    def test_null_album_noop(self):
        p1 = self.seed("A", None, 1000)
        self.assertEqual(reconcile.reconcile_album(p1, db_path=self.db), 0)


class ApplyToRunTest(ReconcileDbBase):
    def test_overrides_locks_and_locks_all(self):
        p1 = self.seed("A", "Wrong", 1000, locked=1)
        p2 = self.seed("A", "Also Wrong", 1100)
        ids = reconcile.apply_album_to_run(p2, "Right (Deluxe)", db_path=self.db)
        self.assertEqual(sorted(ids), [p1, p2])
        conn = sqlite3.connect(self.db)
        rows = conn.execute("SELECT album, album_locked FROM plays").fetchall()
        conn.close()
        self.assertEqual(set(rows), {("Right (Deluxe)", 1)})

    def test_missing_play_empty(self):
        self.assertEqual(reconcile.apply_album_to_run(999, "X", db_path=self.db), [])


class IpcReconcileTriggerTest(ReconcileDbBase):
    def setUp(self):
        super().setUp()
        self._orig_db = play_history.DB_PATH
        play_history.DB_PATH = self.db
        ipc_manager._last_recorded_key = None
        ipc_manager._last_play_id = None

    def tearDown(self):
        play_history.DB_PATH = self._orig_db
        ipc_manager._last_recorded_key = None
        ipc_manager._last_play_id = None
        super().tearDown()

    def test_new_play_triggers_reconcile(self):
        asyncio.run(ipc_manager._record_if_new(
            {"title": "One", "artist": "A", "album": "Abbey Road"}))
        asyncio.run(ipc_manager._record_if_new(
            {"title": "Two", "artist": "A", "album": "Abbey Road (Deluxe Edition)"}))
        self.assertEqual(set(self.albums()), {"Abbey Road (Deluxe Edition)"})

    def test_reconcile_failure_does_not_block_recording(self):
        orig = reconcile.reconcile_album
        reconcile.reconcile_album = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            asyncio.run(ipc_manager._record_if_new(
                {"title": "One", "artist": "A", "album": "X"}))
        finally:
            reconcile.reconcile_album = orig
        self.assertEqual(self.albums(), ["X"])
```

- [ ] **Step 2: Run to verify failure**

Run: `cd gui && ../.venv/bin/python -m pytest tests/test_reconcile.py -q`
Expected: FAIL — `no attribute 'find_run'`

- [ ] **Step 3: Implement reconcile DB functions** — append to `gui/reconcile.py`:

```python
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
```

- [ ] **Step 4: Wire the ipc trigger** — in `gui/ipc_manager.py`:

Add `import reconcile` after `import play_history`. Extract the art-task idiom into a reusable helper (replacing the inline block at the end of `_record_if_new`):

```python
def spawn_art_download(play_id: int, art_url: str) -> None:
    """create_task + strong ref until done. Reused by the UDS record path and
    the album-edit API (art refresh)."""
    _art_tasks.add(task := asyncio.create_task(_download_and_store_art(play_id, art_url)))
    task.add_done_callback(_art_tasks.discard)
```

At the end of `_record_if_new`, replace the two inline art-task lines with, and add the reconcile call:

```python
    if art_url:
        spawn_art_download(play_id, art_url)

    # Unify edition variants across this play's session run. Best-effort:
    # a reconcile failure must never block or crash recording.
    try:
        await asyncio.to_thread(reconcile.reconcile_album, play_id)
    except Exception as e:
        log.warning("album reconcile failed for play %s: %s", play_id, e)
```

- [ ] **Step 5: Run to verify pass, then full gui suite**

Run: `cd gui && ../.venv/bin/python -m pytest tests/test_reconcile.py -q` then `../.venv/bin/python -m pytest tests -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add gui/reconcile.py gui/ipc_manager.py gui/tests/test_reconcile.py
git commit -m "feat(reconcile): session-run album unification, wired after each recorded play

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Album-candidates + album-update API routes

**Files:**
- Modify: `gui/backend_main.py` (imports; two routes after `/api/plays/{play_id}/restore`; one helper)
- Test: `gui/tests/test_album_api.py` (create)

**Interfaces:**
- Consumes: `play_history.get_play`/`set_album` (Task 1), `reconcile.apply_album_to_run` (Task 3), `ipc_manager.spawn_art_download` (Task 3).
- Produces: `GET /api/plays/{play_id}/album-candidates` → `{"current": str|null, "candidates": [{"album": str, "art_url": str|null}]}` (404 unknown play; network errors → empty candidates); `POST /api/plays/{play_id}/album` body `{album, art_url?, apply_to_run?}` → `{"status": "ok", "updated": N}` (400 empty album, 404 unknown play); `backend_main._itunes_album_candidates(artist, title) -> list[dict]` (isolated for stubbing).

- [ ] **Step 1: Write the failing tests** — create `gui/tests/test_album_api.py`:

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

import backend_main  # noqa: E402
import play_history  # noqa: E402


class AlbumApiBase(unittest.TestCase):
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


class CandidatesTest(AlbumApiBase):
    def setUp(self):
        super().setUp()
        self._orig_fetch = backend_main._itunes_album_candidates

    def tearDown(self):
        backend_main._itunes_album_candidates = self._orig_fetch
        super().tearDown()

    def test_unknown_play_404(self):
        self.assertEqual(
            self.client.get("/api/plays/999/album-candidates").status_code, 404)

    def test_candidates_shape(self):
        pid = play_history.record_play("Come Together", "The Beatles",
                                       "Abbey Road", None, db_path=self.db_path)
        async def fake(artist, title):
            return [{"album": "Abbey Road (Super Deluxe Edition)",
                     "art_url": "http://a/1000x1000bb.jpg"}]
        backend_main._itunes_album_candidates = fake
        body = self.client.get(f"/api/plays/{pid}/album-candidates").json()
        self.assertEqual(body["current"], "Abbey Road")
        self.assertEqual(body["candidates"][0]["album"],
                         "Abbey Road (Super Deluxe Edition)")


class SetAlbumTest(AlbumApiBase):
    def test_empty_album_400(self):
        pid = play_history.record_play("T", "A", None, None, db_path=self.db_path)
        r = self.client.post(f"/api/plays/{pid}/album", json={"album": "  "})
        self.assertEqual(r.status_code, 400)

    def test_unknown_play_404(self):
        r = self.client.post("/api/plays/999/album", json={"album": "X"})
        self.assertEqual(r.status_code, 404)

    def test_single_play_update_locks(self):
        pid = play_history.record_play("T", "A", "Old", None, db_path=self.db_path)
        r = self.client.post(f"/api/plays/{pid}/album", json={"album": "New"})
        self.assertEqual(r.json(), {"status": "ok", "updated": 1})
        row = play_history.get_play(pid, db_path=self.db_path)
        self.assertEqual((row["album"], row["album_locked"]), ("New", 1))

    def test_apply_to_run_updates_whole_run(self):
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO plays (title, artist, album, played_at, album_locked)"
                     " VALUES ('t1', 'A', 'Wrong', 1000, 1)")
        cur = conn.execute("INSERT INTO plays (title, artist, album, played_at)"
                           " VALUES ('t2', 'A', 'Also Wrong', 1100)")
        pid = cur.lastrowid
        conn.commit()
        conn.close()
        r = self.client.post(f"/api/plays/{pid}/album",
                             json={"album": "Right", "apply_to_run": True})
        self.assertEqual(r.json()["updated"], 2)

    def test_art_url_fires_download(self):
        # Patch the SYNCHRONOUS spawn helper as resolved inside backend_main —
        # patching the async downloader would race the response (create_task
        # may not have run by assertion time).
        pid = play_history.record_play("T", "A", "Old", None, db_path=self.db_path)
        calls = []
        orig = backend_main.spawn_art_download
        backend_main.spawn_art_download = lambda p, u: calls.append((p, u))
        try:
            self.client.post(f"/api/plays/{pid}/album",
                             json={"album": "New", "art_url": "http://a/x.jpg"})
        finally:
            backend_main.spawn_art_download = orig
        self.assertEqual(calls, [(pid, "http://a/x.jpg")])

    def test_no_art_url_no_download(self):
        pid = play_history.record_play("T", "A", "Old", None, db_path=self.db_path)
        calls = []
        orig = backend_main.spawn_art_download
        backend_main.spawn_art_download = lambda p, u: calls.append((p, u))
        try:
            self.client.post(f"/api/plays/{pid}/album", json={"album": "New"})
        finally:
            backend_main.spawn_art_download = orig
        self.assertEqual(calls, [])
```

- [ ] **Step 2: Run to verify failure**

Run: `cd gui && ../.venv/bin/python -m pytest tests/test_album_api.py -q`
Expected: FAIL — 404s from missing routes / missing `_itunes_album_candidates`

- [ ] **Step 3: Implement** — in `gui/backend_main.py`:

Add imports: `import urllib.parse` with the stdlib imports; `import aiohttp` after `import sounddevice as sd`; `import reconcile` after `import stats`; and extend the ipc_manager import line to `from ipc_manager import ART_DIR, manager, handle_uds_client, spawn_art_download`.

Helper + routes after the `/api/plays/{play_id}/restore` route:

```python
async def _itunes_album_candidates(artist: str, title: str) -> list[dict]:
    """Distinct candidate albums for a track from the iTunes Search API.
    Isolated so tests can stub it; any error is an empty list."""
    query = urllib.parse.quote_plus(f"{artist} {title}")
    url = f"https://itunes.apple.com/search?term={query}&entity=song&limit=25"
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json(content_type=None)
    except Exception as e:
        print(f"⚠️ iTunes candidates lookup failed: {e}")
        return []
    out, seen = [], set()
    for r in data.get("results", []):
        album = r.get("collectionName")
        if not album or album in seen:
            continue
        seen.add(album)
        art = (r.get("artworkUrl100") or "").replace("100x100bb", "1000x1000bb")
        out.append({"album": album, "art_url": art or None})
        if len(out) >= 10:
            break
    return out


@app.get("/api/plays/{play_id}/album-candidates")
async def album_candidates(play_id: int):
    play = await asyncio.to_thread(play_history.get_play, play_id)
    if play is None:
        return JSONResponse(status_code=404, content={"detail": "not found"})
    candidates = await _itunes_album_candidates(play["artist"], play["title"])
    return {"current": play["album"], "candidates": candidates}


@app.post("/api/plays/{play_id}/album")
async def set_album_route(play_id: int, request: Request):
    body = await request.json()
    album = str(body.get("album") or "").strip()
    art_url = body.get("art_url") or None
    if not album:
        return JSONResponse(status_code=400, content={"detail": "album is required"})
    if body.get("apply_to_run"):
        ids = await asyncio.to_thread(reconcile.apply_album_to_run, play_id, album)
        if not ids:
            return JSONResponse(status_code=404, content={"detail": "not found"})
    else:
        ok = await asyncio.to_thread(play_history.set_album, play_id, album)
        if not ok:
            return JSONResponse(status_code=404, content={"detail": "not found"})
        ids = [play_id]
    if art_url:
        for pid in ids:
            spawn_art_download(pid, art_url)
    return {"status": "ok", "updated": len(ids)}
```

- [ ] **Step 4: Run to verify pass, then full gui suite**

Run: `cd gui && ../.venv/bin/python -m pytest tests/test_album_api.py -q` then `../.venv/bin/python -m pytest tests -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add gui/backend_main.py gui/tests/test_album_api.py
git commit -m "feat(api): album candidates from iTunes + album update with run scope

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Stats — `top_albums` module + payload + UI

**Files:**
- Modify: `gui/stats.py` (new `_top_albums`; payload key in `compute_stats`)
- Modify: `gui/templates/stats.html` (ranked-lists grid → 3 columns, new section)
- Modify: `gui/static/stats.js` (render the new list)
- Test: `gui/tests/test_stats.py` (append a TestCase)

**Interfaces:**
- Consumes: existing `_WHERE`, `_latest_art_subquery`, `TOP_N` in stats.py.
- Produces: payload key `top_albums` = `{"covered": int, "total": int, "top": [{"album", "artist", "plays", "art_path"}]}`.

- [ ] **Step 1: Write the failing tests** — append to `gui/tests/test_stats.py`:

```python
class TopAlbumsTest(StatsTestBase):
    def seed_album(self, title, artist, album, played_at, art_path=None):
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO plays (title, artist, album, played_at, art_path)"
            " VALUES (?, ?, ?, ?, ?)", (title, artist, album, played_at, art_path))
        conn.commit()
        conn.close()

    def test_grouping_and_exclusions(self):
        t = ts(2026, 7, 1)
        self.seed_album("S1", "Beatles", "Abbey Road", t, art_path="art/1.jpg")
        self.seed_album("S2", "Beatles", "Abbey Road", t + 100, art_path="art/2.jpg")
        self.seed_album("S3", "Doors", "L.A. Woman", t)
        self.seed_album("S4", "X", None, t)                 # no album: excluded
        self.seed_album("S5", "Y", "Unknown Album", t)      # sentinel: excluded
        out = stats.compute_stats("month", 2026, 7, db_path=self.db, now=NOW)
        ta = out["top_albums"]
        self.assertEqual(ta["top"][0],
                         {"album": "Abbey Road", "artist": "Beatles",
                          "plays": 2, "art_path": "art/2.jpg"})
        self.assertEqual(len(ta["top"]), 2)
        self.assertEqual(ta["covered"], 3)
        self.assertEqual(ta["total"], 5)

    def test_same_album_name_different_artists_separate(self):
        t = ts(2026, 7, 1)
        self.seed_album("S1", "A", "Greatest Hits", t)
        self.seed_album("S2", "B", "Greatest Hits", t)
        out = stats.compute_stats("month", 2026, 7, db_path=self.db, now=NOW)
        self.assertEqual(len(out["top_albums"]["top"]), 2)
```

- [ ] **Step 2: Run to verify failure**

Run: `cd gui && ../.venv/bin/python -m pytest tests/test_stats.py::TopAlbumsTest -v`
Expected: FAIL — KeyError `'top_albums'`

- [ ] **Step 3: Implement stats module** — in `gui/stats.py`, after `_top_tracks`:

```python
def _top_albums(conn, start, end, total) -> dict:
    art = _latest_art_subquery("p2.album = p.album AND p2.artist = p.artist")
    where_album = "p.album IS NOT NULL AND p.album != 'Unknown Album'"
    rows = conn.execute(
        f"SELECT p.album, p.artist, COUNT(*) AS plays, {art} AS art_path"
        f" FROM plays p WHERE {_WHERE} AND {where_album}"
        " GROUP BY p.album, p.artist"
        " ORDER BY plays DESC, p.album ASC, p.artist ASC LIMIT ?",
        (start, end, start, end, TOP_N)).fetchall()
    (covered,) = conn.execute(
        f"SELECT COUNT(*) FROM plays WHERE {_WHERE}"
        " AND album IS NOT NULL AND album != 'Unknown Album'",
        (start, end)).fetchone()
    return {"covered": covered, "total": total, "top": [dict(r) for r in rows]}
```

In `compute_stats`'s return dict, after `"top_tracks": ...`:

```python
            "top_albums": _top_albums(conn, start, end, totals["plays"]),
```

- [ ] **Step 4: UI** — in `gui/templates/stats.html`, change the ranked-lists wrapper from `md:grid-cols-2` to `md:grid-cols-3` and insert a new section between the artists and tracks sections:

```html
      <section class="glass-panel rounded-xl p-md">
        <h3 class="text-label-md text-on-surface-variant uppercase tracking-widest mb-md">Top albums</h3>
        <ol id="stats-top-albums" class="flex flex-col gap-2"></ol>
        <p id="stats-top-albums-note" class="text-label-sm text-on-surface-variant mt-sm"></p>
      </section>
```

In `gui/static/stats.js`, inside `renderTopLists(data)`, after the tracks block:

```javascript
    const albums = (data.top_albums && data.top_albums.top) || [];
    const maxAl = albums.length ? albums[0].plays : 0;
    $("stats-top-albums").innerHTML = albums.length
      ? albums.map((a, i) => rankRow(i + 1, a.art_path, escapeHtml(a.album), escapeHtml(a.artist), a.plays, maxAl)).join("")
      : '<li class="text-body-sm text-on-surface-variant">No album data yet.</li>';
    const ta = data.top_albums || { covered: 0, total: 0 };
    $("stats-top-albums-note").textContent = (albums.length && ta.covered < ta.total)
      ? `${ta.covered} of ${ta.total} plays have album data.` : "";
```

- [ ] **Step 5: Run to verify pass, then full gui suite**

Run: `cd gui && ../.venv/bin/python -m pytest tests/test_stats.py -q` then `../.venv/bin/python -m pytest tests -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add gui/stats.py gui/templates/stats.html gui/static/stats.js gui/tests/test_stats.py
git commit -m "feat(stats): Top Albums module — grouped post-reconciliation, with coverage note

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: History UI — pencil + album modal

**Files:**
- Modify: `gui/templates/history.html` (modal markup before the toast div)
- Modify: `gui/static/history.js` (row data attrs + edit button in `rowHtml`; modal logic; toast refactor)

**Interfaces:**
- Consumes: `GET /api/plays/{id}/album-candidates`, `POST /api/plays/{id}/album` (Task 4 shapes).
- Produces: user-facing edit flow; no JS exports.

- [ ] **Step 1: Modal markup** — in `gui/templates/history.html`, insert before the `history-toast` div:

```html
  <div id="album-modal" class="hidden fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
    <div class="glass-panel rounded-xl p-md md:p-lg w-full max-w-md mx-md max-h-[80vh] flex flex-col">
      <p class="text-body-md text-on-surface font-medium">Change album</p>
      <p id="album-modal-track" class="text-body-sm text-on-surface-variant mt-1 truncate"></p>
      <div id="album-modal-list" class="flex flex-col gap-1 mt-md overflow-y-auto min-h-0"></div>
      <label class="block mt-md">
        <span class="text-label-sm text-on-surface-variant mb-1 block">Or type an album name</span>
        <input type="text" id="album-modal-text" class="form-input" autocomplete="off">
      </label>
      <label class="flex items-center gap-xs mt-md cursor-pointer">
        <input type="checkbox" id="album-modal-run" class="form-checkbox h-4 w-4">
        <span class="text-body-sm text-on-surface">Apply to the whole session run</span>
      </label>
      <div class="flex justify-end gap-md mt-md">
        <button type="button" id="album-modal-cancel"
                class="text-label-md text-on-surface-variant hover:text-on-surface transition-colors px-md py-2">Cancel</button>
        <button type="button" id="album-modal-save"
                class="bg-primary text-on-primary font-medium px-lg py-2 rounded-full hover:opacity-90 transition-opacity disabled:opacity-40">Save</button>
      </div>
    </div>
  </div>
```

- [ ] **Step 2: Row changes** — in `gui/static/history.js` `rowHtml(row)`:

Add data attributes to the `<li>` open tag and make the album line always-present and addressable; add the pencil button before the delete button. The full replacement `rowHtml`:

```javascript
  function rowHtml(row) {
    const artSrc = row.art_path ? `/${row.art_path}` : "/static/placeholder.jpg";
    return `
      <li class="group flex items-center gap-md py-2" data-id="${row.id}"
          data-title="${escapeHtml(row.title)}" data-artist="${escapeHtml(row.artist || "")}"
          data-album="${escapeHtml(row.album || "")}">
        <img src="${escapeHtml(artSrc)}" alt=""
             class="w-12 h-12 rounded shrink-0 bg-surface-container-high object-cover"
             onerror="this.src='/static/placeholder.jpg'">
        <div class="flex-1 min-w-0">
          <p class="text-body-md text-on-surface truncate">${escapeHtml(row.title)}</p>
          <p class="text-body-sm text-on-surface-variant truncate">${escapeHtml(row.artist || "Unknown artist")}</p>
          <p class="history-album text-label-sm text-on-surface-variant truncate">${escapeHtml(row.album || "")}</p>
        </div>
        <span class="text-label-sm text-on-surface-variant tabular-nums shrink-0">${escapeHtml(timeOfDay(row))}</span>
        <button type="button" class="history-edit shrink-0 ml-2 p-1 rounded text-on-surface-variant hover:text-primary
                       opacity-0 group-hover:opacity-100 focus:opacity-100 transition-opacity
                       focus-visible:ring-2 focus-visible:ring-primary"
                title="Change album for ${escapeHtml(row.title)}" aria-label="Change album for ${escapeHtml(row.title)}">
          <span class="material-symbols-outlined" style="font-size:20px;">edit</span>
        </button>
        <button type="button" class="history-del shrink-0 ml-2 p-1 rounded text-on-surface-variant hover:text-error
                       opacity-0 group-hover:opacity-100 focus:opacity-100 transition-opacity
                       focus-visible:ring-2 focus-visible:ring-primary"
                title="Remove ${escapeHtml(row.title)}" aria-label="Remove ${escapeHtml(row.title)}">
          <span class="material-symbols-outlined" style="font-size:20px;">close</span>
        </button>
      </li>
    `;
  }
```

(The album `<p>` was previously conditional; it is now always rendered — empty when no album — so in-place updates have a stable target.)

- [ ] **Step 3: Toast refactor + modal logic** — in `gui/static/history.js`:

Replace the toast-showing lines inside `removeRow` (the four lines from `const msgEl = ...` through `toastTimer = setTimeout(...)`) with `showToast(titleText ? \`Removed "${titleText}"\` : "Removed", true);` and add these below `hideToast()`:

```javascript
  function showToast(msg, withUndo) {
    const msgEl = document.getElementById("history-toast-msg");
    if (msgEl) msgEl.textContent = msg;
    TOAST_UNDO.classList.toggle("hidden", !withUndo);
    TOAST.classList.remove("hidden");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(hideToast, 5000);
  }
```

Append the modal wiring at the end of the IIFE (before the initial `fetchPage()` call):

```javascript
  // ---------- album edit modal ----------
  const MODAL = document.getElementById("album-modal");
  const MODAL_TRACK = document.getElementById("album-modal-track");
  const MODAL_LIST = document.getElementById("album-modal-list");
  const MODAL_TEXT = document.getElementById("album-modal-text");
  const MODAL_RUN = document.getElementById("album-modal-run");
  const MODAL_SAVE = document.getElementById("album-modal-save");
  const MODAL_CANCEL = document.getElementById("album-modal-cancel");
  let editLi = null;
  let candidates = [];

  function closeModal() {
    MODAL.classList.add("hidden");
    editLi = null;
    candidates = [];
  }

  function renderCandidates() {
    if (!candidates.length) {
      MODAL_LIST.innerHTML =
        '<p class="text-body-sm text-on-surface-variant">No suggestions found — type a name below.</p>';
      return;
    }
    MODAL_LIST.innerHTML = candidates.map((c, i) => `
      <label class="flex items-center gap-3 p-2 rounded-lg hover:bg-white/5 cursor-pointer">
        <input type="radio" name="album-candidate" value="${i}" class="shrink-0 accent-primary">
        <img src="${escapeHtml(c.art_url ? c.art_url.replace("1000x1000bb", "100x100bb") : "/static/placeholder.jpg")}"
             alt="" class="w-8 h-8 rounded object-cover shrink-0"
             onerror="this.src='/static/placeholder.jpg'">
        <span class="text-body-sm text-on-surface truncate">${escapeHtml(c.album)}</span>
      </label>`).join("");
  }

  async function openModal(li) {
    editLi = li;
    MODAL_TRACK.textContent = `${li.dataset.title} — ${li.dataset.artist}`;
    MODAL_TEXT.value = li.dataset.album || "";
    MODAL_RUN.checked = false;
    MODAL_SAVE.disabled = false;
    MODAL_LIST.innerHTML =
      '<p class="text-body-sm text-on-surface-variant">Loading suggestions…</p>';
    MODAL.classList.remove("hidden");
    try {
      const res = await fetch(`/api/plays/${li.dataset.id}/album-candidates`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const body = await res.json();
      candidates = body.candidates || [];
    } catch (_) {
      candidates = [];
    }
    renderCandidates();
  }

  MODAL_LIST.addEventListener("change", (e) => {
    if (e.target.name !== "album-candidate") return;
    const c = candidates[Number(e.target.value)];
    if (c) MODAL_TEXT.value = c.album;
  });

  async function saveAlbum() {
    if (!editLi) return;
    const album = MODAL_TEXT.value.trim();
    if (!album) return;
    const chosen = candidates.find((c) => c.album === album) || null;
    MODAL_SAVE.disabled = true;
    try {
      const res = await fetch(`/api/plays/${editLi.dataset.id}/album`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          album,
          art_url: chosen ? chosen.art_url : null,
          apply_to_run: MODAL_RUN.checked,
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const body = await res.json();
      const albumEl = editLi.querySelector(".history-album");
      if (albumEl) albumEl.textContent = album;
      editLi.dataset.album = album;
      const n = body.updated || 1;
      showToast(`Album updated (${n} ${n === 1 ? "play" : "plays"})`, false);
      closeModal();
    } catch (e) {
      MODAL_SAVE.disabled = false;
      showToast("Couldn't update album", false);
    }
  }

  MODAL_SAVE.addEventListener("click", saveAlbum);
  MODAL_CANCEL.addEventListener("click", closeModal);
  MODAL.addEventListener("click", (e) => { if (e.target === MODAL) closeModal(); });
```

And extend the existing `LIST` click handler to route the pencil (add before the `.history-del` branch):

```javascript
    const editBtn = e.target.closest(".history-edit");
    if (editBtn) {
      const editRow = editBtn.closest("li[data-id]");
      if (editRow) openModal(editRow);
      return;
    }
```

- [ ] **Step 4: Manual verification** — seeded scratch server, same recipe as the Stats feature:

```bash
SCRATCH=$(mktemp -d)
cd gui && SPINSENSE_DATA_DIR=$SCRATCH ../.venv/bin/python -m uvicorn backend_main:app --port 3399 &
sleep 3
curl -s -o /dev/null http://127.0.0.1:3399/api/setup-state
../.venv/bin/python - <<EOF
import json, os, time
os.environ["SPINSENSE_DATA_DIR"] = "$SCRATCH"
import sys; sys.path.insert(0, ".")
import importlib, play_history
importlib.reload(play_history)
p1 = play_history.record_play("Come Together", "The Beatles", "Abbey Road", None)
p2 = play_history.record_play("Something", "The Beatles", "Abbey Road", None)
cfg = json.load(open("$SCRATCH/config.json")); cfg["System"]["Setup_Wizard_State"] = "completed"
json.dump(cfg, open("$SCRATCH/config.json", "w"))
EOF
```

Then on `http://127.0.0.1:3399/history` (browser or gstack browse): hover shows pencil + ✕; pencil opens the modal with live iTunes suggestions for "The Beatles Come Together"; picking a candidate fills the text field; Save with "Apply to the whole session run" checked updates the row in place and toasts "Album updated (2 plays)"; `curl -s http://127.0.0.1:3399/api/plays | python3 -m json.tool` shows both rows with the new album. Also verify the delete flow's toast still shows Undo. Kill uvicorn when done.

- [ ] **Step 5: Full suites**

Run: `cd gui && ../.venv/bin/python -m pytest tests -q && cd ../core && ../.venv/bin/python -m pytest tests -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add gui/templates/history.html gui/static/history.js
git commit -m "feat(history): album edit modal — iTunes candidates, free text, run scope

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Docs — CHANGELOG

**Files:**
- Modify: `CHANGELOG.md` (new `## [Unreleased]` section above `## [1.6.0.0]`)

**Interfaces:** none (docs only).

- [ ] **Step 1: Add the section** — at the top of `CHANGELOG.md`, directly under the intro paragraph and above `## [1.6.0.0] - 2026-07-06`:

```markdown
## [Unreleased]

### Added
- **Album/edition intelligence.** When one listening session mixes edition variants of the same album ("Abbey Road" vs "Abbey Road (Super Deluxe Edition)"), SpinSense now unifies the whole same-artist run to the most-qualified edition — live, in both directions. Re-recordings like "(Taylor's Version)" and live/acoustic albums are deliberately never merged. Runs are bounded by the 30-minute session gap.
- **Manual album correction.** A pencil on each History row opens a picker with candidate albums from iTunes (plus free text), optional "apply to the whole session run", and album-art refresh. Manual choices are locked against future auto-rewrites.
- **Top Albums on the Stats page.** A third ranked list between Top Artists and Top Tracks, grouped post-reconciliation so deluxe and regular plays count as one album; plays without album data are excluded with a coverage note.
```

- [ ] **Step 2: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: changelog for album/edition intelligence

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Final verification (after all tasks)

- [ ] Both suites green from their package dirs.
- [ ] Manual smoke per Task 6 Step 4 if not already done.
- [ ] Working tree clean except the intentionally-untracked `DESIGN.md`.
- [ ] Do NOT push or release — the user decides when to ship.

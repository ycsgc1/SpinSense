# Wave 1 — Status Indicators + Delete Scrobble Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface the recognition state machine on the dashboard (with per-phase glow, 2 auto-retries, and a manual rescan) and let users delete a History scrobble with Undo, reclaiming orphaned art via a purge sweep.

**Architecture:** Two slices. (A) **Delete** is a soft-delete (`deleted_at` column) so Undo is trivial; a background purge sweep hard-deletes past-grace rows and unlinks unreferenced art. (B) **Status** adds a machine-readable `phase` field to the existing `live_status` WebSocket frame; the engine publishes phases from inside `recognize_audio()` (which blocks during sampling, so the monitor loop can't), retries twice before `no_match`, and gates re-scans behind a back-off flag. A new `rescan` engine command rides the existing `/tmp/spinsense-cmd.sock` channel.

**Tech Stack:** Python 3.12 · FastAPI · SQLite · `shazamio` · `sounddevice` · vanilla JS + Jinja2 + Tailwind (CDN). Tests are `unittest` run under `pytest`.

**Run all tests from the repo root** `/home/ubuntu/SpinSense/SpinSense`.

**Key invariant (do not break):** `ipc_manager._record_if_new` dedupes on `track.title` and *resets* its dedupe state on an empty title. Therefore phase frames during `scanning`/`identifying`/`retrying` MUST carry the **current** `state` track (not a blank one), or same-song re-scans will record duplicates. The frontend decides what to *display* from `phase`, not from track presence.

---

## File map

| File | Responsibility | Change |
|---|---|---|
| `gui/play_history.py` | SQLite layer | add `deleted_at` migration, `delete_play`, `restore_play`, `purge_deleted`, `_unlink_art`; filter reads |
| `gui/backend_main.py` | FastAPI routes + lifespan | `DELETE /api/plays/{id}`, `POST /api/plays/{id}/restore`, `POST /api/rescan`, startup + periodic purge |
| `gui/static/history.js` | History UI | per-row ✕, optimistic remove, 5 s Undo toast |
| `gui/templates/history.html` | History markup | toast element |
| `core/core_engine.py` | Recognition engine | `build_status_payload`, `_write_uds`, `_publish_phase`, retry loop in `recognize_audio`, `_scan_decision`, back-off + `force_scan`, `rescan` command, `phase` in monitor-loop frames |
| `gui/static/shell.js` | WS client + pill | map `phase` → pill state/label |
| `gui/static/dashboard.js` | Dashboard | glow + caption from `phase`, clear art when not `playing`, wire Scan-again |
| `gui/templates/dashboard.html` | Dashboard markup | `#vinyl-stage[data-phase]`, `#phase-caption`, `#scan-again` |
| `gui/static/styles.css` | Styles | glow classes + `glow-*` keyframes (NOT `pulse`) |
| `gui/tests/test_play_history.py` | DB tests | delete/restore/purge cases |
| `gui/tests/test_delete_api.py` | API tests | new file |
| `gui/tests/test_rescan_api.py` | API tests | new file |
| `core/tests/test_recognition_phases.py` | Engine tests | new file |

---

# PART A — Delete scrobble + art cleanup

## Task 1: Soft-delete column + delete/restore + filtered reads

**Files:**
- Modify: `gui/play_history.py`
- Test: `gui/tests/test_play_history.py`

- [ ] **Step 1: Write failing tests**

Append to `gui/tests/test_play_history.py` inside a new test class (after `PlayHistoryRoundTripTest`):

```python
class SoftDeleteTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        play_history.init_db(db_path=self.db_path)

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def test_delete_hides_from_reads(self):
        pid = play_history.record_play("Gone", "A", None, None, db_path=self.db_path)
        self.assertTrue(play_history.delete_play(pid, db_path=self.db_path))
        self.assertEqual(play_history.recent_plays(db_path=self.db_path), [])
        self.assertEqual(play_history.count_plays(db_path=self.db_path), 0)

    def test_delete_unknown_returns_false(self):
        self.assertFalse(play_history.delete_play(999, db_path=self.db_path))

    def test_delete_is_idempotent(self):
        pid = play_history.record_play("Once", "A", None, None, db_path=self.db_path)
        self.assertTrue(play_history.delete_play(pid, db_path=self.db_path))
        self.assertFalse(play_history.delete_play(pid, db_path=self.db_path))

    def test_restore_brings_it_back(self):
        pid = play_history.record_play("Back", "A", None, None, db_path=self.db_path)
        play_history.delete_play(pid, db_path=self.db_path)
        self.assertTrue(play_history.restore_play(pid, db_path=self.db_path))
        rows = play_history.recent_plays(db_path=self.db_path)
        self.assertEqual([r["title"] for r in rows], ["Back"])

    def test_restore_unknown_or_live_returns_false(self):
        pid = play_history.record_play("Live", "A", None, None, db_path=self.db_path)
        self.assertFalse(play_history.restore_play(pid, db_path=self.db_path))  # not deleted
        self.assertFalse(play_history.restore_play(999, db_path=self.db_path))

    def test_migration_adds_column_on_existing_db(self):
        play_history.init_db(db_path=self.db_path)  # run twice, must not error
        import sqlite3
        cols = {r[1] for r in sqlite3.connect(self.db_path).execute("PRAGMA table_info(plays)")}
        self.assertIn("deleted_at", cols)
```

- [ ] **Step 2: Run, verify they fail**

Run: `python -m pytest gui/tests/test_play_history.py::SoftDeleteTest -v`
Expected: FAIL — `AttributeError: module 'play_history' has no attribute 'delete_play'`.

- [ ] **Step 3: Implement**

In `gui/play_history.py`, extend `init_db` migration. After the existing enrichment loop (the `for name, sqltype in _ENRICHMENT_COLUMNS.items():` block), add:

```python
        if "deleted_at" not in existing:
            conn.execute("ALTER TABLE plays ADD COLUMN deleted_at INTEGER")
```

Change `recent_plays`'s SELECT to filter soft-deleted rows — replace `FROM plays ORDER BY` with:

```python
            "FROM plays WHERE deleted_at IS NULL ORDER BY played_at DESC, id DESC LIMIT ? OFFSET ?",
```

Change `count_plays` query to:

```python
        (n,) = conn.execute("SELECT COUNT(*) FROM plays WHERE deleted_at IS NULL").fetchone()
```

Add two functions after `set_art_path`:

```python
def delete_play(play_id: int, db_path: str | None = None) -> bool:
    """Soft-delete: stamp deleted_at. Returns True if a live row was hidden."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE plays SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
            (int(time.time()), play_id),
        )
        return cur.rowcount > 0


def restore_play(play_id: int, db_path: str | None = None) -> bool:
    """Clear deleted_at. Returns True if a soft-deleted row was restored."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE plays SET deleted_at = NULL WHERE id = ? AND deleted_at IS NOT NULL",
            (play_id,),
        )
        return cur.rowcount > 0
```

- [ ] **Step 4: Run, verify pass**

Run: `python -m pytest gui/tests/test_play_history.py -v`
Expected: PASS (all existing + new).

- [ ] **Step 5: Commit**

```bash
git add gui/play_history.py gui/tests/test_play_history.py
git commit -m "feat(history): soft-delete + restore for scrobbles"
```

---

## Task 2: Purge sweep with art-unlink guard

**Files:**
- Modify: `gui/play_history.py`
- Test: `gui/tests/test_play_history.py`

- [ ] **Step 1: Write failing tests**

Append a new class to `gui/tests/test_play_history.py`:

```python
class PurgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "t.db")
        os.makedirs(os.path.join(self.tmp, "art"), exist_ok=True)
        play_history.init_db(db_path=self.db_path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _age_deleted(self, pid, seconds_ago):
        import sqlite3
        with sqlite3.connect(self.db_path) as c:
            c.execute("UPDATE plays SET deleted_at = ? WHERE id = ?",
                      (int(time.time()) - seconds_ago, pid))

    def _art(self, name):
        p = os.path.join(self.tmp, "art", name)
        with open(p, "wb") as f:
            f.write(b"x")
        return p, f"art/{name}"

    def test_purges_past_grace_and_unlinks_art(self):
        path, rel = self._art("1.jpg")
        pid = play_history.record_play("Old", "A", None, None, db_path=self.db_path)
        play_history.set_art_path(pid, rel, db_path=self.db_path)
        play_history.delete_play(pid, db_path=self.db_path)
        self._age_deleted(pid, 300)
        n = play_history.purge_deleted(grace_seconds=120, data_dir=self.tmp, db_path=self.db_path)
        self.assertEqual(n, 1)
        self.assertFalse(os.path.exists(path))

    def test_keeps_rows_within_grace(self):
        pid = play_history.record_play("Recent", "A", None, None, db_path=self.db_path)
        play_history.delete_play(pid, db_path=self.db_path)  # deleted_at = now
        n = play_history.purge_deleted(grace_seconds=120, data_dir=self.tmp, db_path=self.db_path)
        self.assertEqual(n, 0)

    def test_keeps_art_still_referenced_by_live_row(self):
        path, rel = self._art("shared.jpg")
        live = play_history.record_play("Live", "A", None, None, db_path=self.db_path)
        dead = play_history.record_play("Dead", "A", None, None, db_path=self.db_path)
        play_history.set_art_path(live, rel, db_path=self.db_path)
        play_history.set_art_path(dead, rel, db_path=self.db_path)
        play_history.delete_play(dead, db_path=self.db_path)
        self._age_deleted(dead, 300)
        play_history.purge_deleted(grace_seconds=120, data_dir=self.tmp, db_path=self.db_path)
        self.assertTrue(os.path.exists(path))  # still used by the live row

    def test_tolerates_missing_art_file(self):
        pid = play_history.record_play("NoFile", "A", None, None, db_path=self.db_path)
        play_history.set_art_path(pid, "art/missing.jpg", db_path=self.db_path)
        play_history.delete_play(pid, db_path=self.db_path)
        self._age_deleted(pid, 300)
        n = play_history.purge_deleted(grace_seconds=120, data_dir=self.tmp, db_path=self.db_path)
        self.assertEqual(n, 1)  # no exception
```

- [ ] **Step 2: Run, verify they fail**

Run: `python -m pytest gui/tests/test_play_history.py::PurgeTest -v`
Expected: FAIL — `AttributeError: module 'play_history' has no attribute 'purge_deleted'`.

- [ ] **Step 3: Implement**

Add to `gui/play_history.py` (after `restore_play`):

```python
def _unlink_art(data_dir: str, art_path: str) -> None:
    """Unlink a cached art file, but only if it resolves inside data_dir/art.
    Tolerates an already-missing file."""
    full = os.path.normpath(os.path.join(data_dir, art_path))
    art_root = os.path.normpath(os.path.join(data_dir, "art"))
    if not (full == art_root or full.startswith(art_root + os.sep)):
        return
    try:
        os.remove(full)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def purge_deleted(
    grace_seconds: int = 120,
    data_dir: str | None = None,
    db_path: str | None = None,
) -> int:
    """Hard-delete soft-deleted rows whose deleted_at is older than grace_seconds,
    and unlink any art file no longer referenced by a remaining row. Returns the
    number of rows purged."""
    cutoff = int(time.time()) - int(grace_seconds)
    base = data_dir if data_dir is not None else DATA_DIR
    with _connect(db_path) as conn:
        victims = conn.execute(
            "SELECT id, art_path FROM plays "
            "WHERE deleted_at IS NOT NULL AND deleted_at < ?",
            (cutoff,),
        ).fetchall()
        if not victims:
            return 0
        conn.executemany(
            "DELETE FROM plays WHERE id = ?", [(r["id"],) for r in victims]
        )
        # Rows are gone now; unlink art only if nothing remaining references it.
        for art_path in {r["art_path"] for r in victims if r["art_path"]}:
            still = conn.execute(
                "SELECT 1 FROM plays WHERE art_path = ? LIMIT 1", (art_path,)
            ).fetchone()
            if still is None:
                _unlink_art(base, art_path)
    return len(victims)
```

- [ ] **Step 4: Run, verify pass**

Run: `python -m pytest gui/tests/test_play_history.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gui/play_history.py gui/tests/test_play_history.py
git commit -m "feat(history): purge_deleted sweep reclaims orphaned art"
```

---

## Task 3: Delete/restore API + startup & periodic purge

**Files:**
- Modify: `gui/backend_main.py`
- Test (create): `gui/tests/test_delete_api.py`

- [ ] **Step 1: Write failing tests**

Create `gui/tests/test_delete_api.py`:

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


class DeleteApiTest(unittest.TestCase):
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

    def test_delete_then_gone_from_list(self):
        pid = play_history.record_play("X", "A", None, None, db_path=self.db_path)
        r = self.client.delete(f"/api/plays/{pid}")
        self.assertEqual(r.status_code, 200)
        body = self.client.get("/api/plays").json()
        self.assertEqual(body["plays"], [])
        self.assertEqual(body["total"], 0)

    def test_delete_unknown_404(self):
        self.assertEqual(self.client.delete("/api/plays/999").status_code, 404)

    def test_restore_brings_back(self):
        pid = play_history.record_play("Y", "A", None, None, db_path=self.db_path)
        self.client.delete(f"/api/plays/{pid}")
        r = self.client.post(f"/api/plays/{pid}/restore")
        self.assertEqual(r.status_code, 200)
        titles = [p["title"] for p in self.client.get("/api/plays").json()["plays"]]
        self.assertEqual(titles, ["Y"])

    def test_restore_unknown_404(self):
        self.assertEqual(self.client.post("/api/plays/999/restore").status_code, 404)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run, verify they fail**

Run: `python -m pytest gui/tests/test_delete_api.py -v`
Expected: FAIL — 405/404 because the routes don't exist yet.

- [ ] **Step 3: Implement**

In `gui/backend_main.py`, add routes after the `get_plays` route (around line 294):

```python
@app.delete("/api/plays/{play_id}")
async def delete_play_route(play_id: int):
    ok = await asyncio.to_thread(play_history.delete_play, play_id)
    if not ok:
        return JSONResponse(status_code=404, content={"detail": "not found"})
    return {"status": "deleted", "id": play_id}


@app.post("/api/plays/{play_id}/restore")
async def restore_play_route(play_id: int):
    ok = await asyncio.to_thread(play_history.restore_play, play_id)
    if not ok:
        return JSONResponse(status_code=404, content={"detail": "not found"})
    return {"status": "restored", "id": play_id}
```

Add a purge loop above the `lifespan` function:

```python
async def _purge_loop():
    """Reclaim art for scrobbles soft-deleted beyond the Undo grace window."""
    while True:
        await asyncio.sleep(1800)  # 30 min
        try:
            await asyncio.to_thread(play_history.purge_deleted)
        except Exception as e:
            print(f"⚠️ purge sweep failed: {e}")
```

In `lifespan`, after `play_history.init_db()` (line 62), add a startup sweep and launch the loop; cancel it on shutdown. The body becomes:

```python
async def lifespan(app: FastAPI):
    play_history.init_db()
    os.makedirs(ART_DIR, exist_ok=True)
    try:
        await asyncio.to_thread(play_history.purge_deleted)
    except Exception as e:
        print(f"⚠️ startup purge failed: {e}")
    task = asyncio.create_task(start_uds_listener())
    purge_task = asyncio.create_task(_purge_loop())
    try:
        await advertiser.start(load_config())
    except Exception as e:
        print(f"⚠️ mDNS advertiser failed to start: {e}")
    yield
    await advertiser.stop()
    task.cancel()
    purge_task.cancel()
```

- [ ] **Step 4: Run, verify pass**

Run: `python -m pytest gui/tests/test_delete_api.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gui/backend_main.py gui/tests/test_delete_api.py
git commit -m "feat(api): delete/restore scrobble endpoints + purge on startup"
```

---

## Task 4: History UI — ✕ button + Undo toast

**Files:**
- Modify: `gui/static/history.js`, `gui/templates/history.html`

No JS unit-test harness exists in this repo; this task is verified by dogfooding.

- [ ] **Step 1: Add the toast markup**

In `gui/templates/history.html`, add before the closing `</div>` of the content block (after the `history-status` div, line 28):

```html
  <div id="history-toast"
       class="hidden fixed bottom-6 left-1/2 -translate-x-1/2 z-50 flex items-center gap-md
              glass-panel rounded-full px-5 py-3 shadow-lg">
    <span id="history-toast-msg" class="text-body-sm text-on-surface">Removed</span>
    <button id="history-toast-undo"
            class="text-label-md uppercase tracking-widest text-primary hover:text-primary-fixed-dim">
      Undo
    </button>
  </div>
```

- [ ] **Step 2: Add the ✕ button to each row**

In `gui/static/history.js`, change `rowHtml(row)` so the `<li>` becomes a Tailwind `group` and gains a delete button. Replace the whole `rowHtml` function with:

```javascript
  function rowHtml(row) {
    const artSrc = row.art_path ? `/${row.art_path}` : "/static/placeholder.jpg";
    return `
      <li class="group flex items-center gap-md py-2" data-id="${row.id}">
        <img src="${escapeHtml(artSrc)}" alt=""
             class="w-12 h-12 rounded shrink-0 bg-surface-container-high object-cover"
             onerror="this.src='/static/placeholder.jpg'">
        <div class="flex-1 min-w-0">
          <p class="text-body-md text-on-surface truncate">${escapeHtml(row.title)}</p>
          <p class="text-body-sm text-on-surface-variant truncate">${escapeHtml(row.artist || "Unknown artist")}</p>
          ${row.album ? `<p class="text-label-sm text-on-surface-variant truncate">${escapeHtml(row.album)}</p>` : ""}
        </div>
        <span class="text-label-sm text-on-surface-variant tabular-nums shrink-0">${escapeHtml(timeOfDay(row))}</span>
        <button class="history-del shrink-0 ml-2 p-1 rounded text-on-surface-variant hover:text-error
                       opacity-0 group-hover:opacity-100 focus:opacity-100 transition-opacity"
                title="Remove" aria-label="Remove this scrobble">
          <span class="material-symbols-outlined" style="font-size:20px;">close</span>
        </button>
      </li>
    `;
  }
```

- [ ] **Step 3: Add delete + Undo logic**

In `gui/static/history.js`, add inside the IIFE (e.g. just before the closing `fetchPage();` call at the bottom):

```javascript
  // ---------- delete + undo ----------
  const TOAST = document.getElementById("history-toast");
  const TOAST_UNDO = document.getElementById("history-toast-undo");
  let toastTimer = null;
  let pending = null; // { id, node, parent, next }

  function adjustTotal(delta) {
    const m = /(\d+)/.exec(TOTAL.textContent || "");
    if (!m) return;
    const n = Math.max(0, parseInt(m[1], 10) + delta);
    TOTAL.textContent = `${n} ${n === 1 ? "play" : "plays"}`;
  }

  function hideToast() {
    if (toastTimer) { clearTimeout(toastTimer); toastTimer = null; }
    TOAST.classList.add("hidden");
    pending = null;
  }

  async function removeRow(li) {
    const id = li.dataset.id;
    if (!id) return;
    const parent = li.parentNode;
    const next = li.nextSibling;
    li.remove();
    adjustTotal(-1);
    pending = { id, node: li, parent, next };
    try {
      const res = await fetch(`/api/plays/${id}`, { method: "DELETE" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (e) {
      // Roll the DOM back if the server rejected it.
      if (pending && pending.node === li) {
        parent.insertBefore(li, next);
        adjustTotal(1);
        pending = null;
      }
      return;
    }
    TOAST.classList.remove("hidden");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(hideToast, 5000);
  }

  async function undo() {
    if (!pending) return;
    const { id, node, parent, next } = pending;
    try {
      await fetch(`/api/plays/${id}/restore`, { method: "POST" });
    } catch (e) { /* best effort */ }
    parent.insertBefore(node, next);
    adjustTotal(1);
    hideToast();
  }

  LIST.addEventListener("click", (e) => {
    const btn = e.target.closest(".history-del");
    if (!btn) return;
    const li = btn.closest("li[data-id]");
    if (li) removeRow(li);
  });
  TOAST_UNDO.addEventListener("click", undo);
```

- [ ] **Step 4: Verify by dogfooding**

Run the app (`/run` skill or the project's normal launch). Then in a browser on `/history`:
1. Hover a row → the ✕ fades in.
2. Click ✕ → the row disappears, the total decrements, an "Undo" toast appears.
3. Click **Undo** within 5 s → the row returns in its original position and the total restores.
4. Delete again, let the toast expire (5 s), **reload** → the row stays gone.

Expected: all four behave as described; no console errors.

- [ ] **Step 5: Commit**

```bash
git add gui/static/history.js gui/templates/history.html
git commit -m "feat(history): per-row delete with 5s undo toast"
```

---

# PART B — Recognition status indicators

## Task 5: `phase` field + `build_status_payload` + UDS write helper

**Files:**
- Modify: `core/core_engine.py`
- Test (create): `core/tests/test_recognition_phases.py`

- [ ] **Step 1: Write failing test**

Create `core/tests/test_recognition_phases.py`:

```python
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CORE_DIR = os.path.dirname(HERE)
if CORE_DIR not in sys.path:
    sys.path.insert(0, CORE_DIR)

import core_engine  # noqa: E402


class BuildStatusPayloadTest(unittest.TestCase):
    def test_playing_carries_track_and_phase(self):
        st = {"in_song": True, "title": "T", "artist": "A", "album": "Al",
              "art_url": "u", "isrc": "i", "genre": "g", "release_year": 2001}
        msg = core_engine.build_status_payload("playing", 0.4, st)
        self.assertEqual(msg["type"], "live_status")
        p = msg["payload"]
        self.assertEqual(p["phase"], "playing")
        self.assertEqual(p["status_msg"], "Playing")
        self.assertEqual(p["rms_level"], 0.4)
        self.assertEqual(p["track"]["title"], "T")
        self.assertEqual(p["track"]["release_year"], 2001)

    def test_scanning_keeps_current_track_but_marks_phase(self):
        # Invariant: phase frames keep the existing track so dedupe is unaffected.
        st = {"in_song": True, "title": "T", "artist": "A", "album": "Al", "art_url": "u"}
        p = core_engine.build_status_payload("scanning", 0.2, st)["payload"]
        self.assertEqual(p["phase"], "scanning")
        self.assertEqual(p["track"]["title"], "T")

    def test_listening_when_not_in_song(self):
        p = core_engine.build_status_payload("listening", 0.0, {"in_song": False})["payload"]
        self.assertEqual(p["status_msg"], "Listening")
        self.assertEqual(p["track"]["title"], "")
```

- [ ] **Step 2: Run, verify it fails**

Run: `python -m pytest core/tests/test_recognition_phases.py::BuildStatusPayloadTest -v`
Expected: FAIL — `AttributeError: module 'core_engine' has no attribute 'build_status_payload'`.

- [ ] **Step 3: Implement**

In `core/core_engine.py`, add new keys to the `state` dict initializer (after `"current_rms": 0.0,`):

```python
    "isrc": None,
    "genre": None,
    "release_year": None,
    "back_off": False,
    "force_scan": False,
```

Add these functions right after the `state = {...}` block (before `fetch_itunes_metadata`):

```python
def build_status_payload(phase: str, rms: float, st: dict) -> dict:
    """Build a live_status frame. `phase` is the machine-readable recognition
    phase; the track always reflects current state so the GUI's dedupe hook is
    never reset mid-song. The frontend decides display from phase, not track."""
    return {
        "type": "live_status",
        "payload": {
            "rms_level": rms,
            "engine_active": True,
            "phase": phase,
            "status_msg": "Playing" if st.get("in_song") else "Listening",
            "track": {
                "title": st.get("title", "") or "",
                "artist": st.get("artist", "") or "",
                "album": st.get("album", "") or "",
                "art_url": st.get("art_url", "") or "",
                "isrc": st.get("isrc"),
                "genre": st.get("genre"),
                "release_year": st.get("release_year"),
            },
        },
    }


async def _write_uds(line: str) -> None:
    """Best-effort: write one newline-terminated frame to the GUI's UDS. Errors
    are swallowed (the GUI may not be up; the engine must not crash)."""
    try:
        if not os.path.exists('/tmp/spinsense.sock'):
            return
        reader, writer = await asyncio.open_unix_connection('/tmp/spinsense.sock')
        writer.write(line.encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass


async def _publish_phase(phase: str) -> None:
    """Publish a phase frame using current state + last RMS reading."""
    payload = build_status_payload(phase, state.get("current_rms", 0.0), state)
    await _write_uds(json.dumps(payload) + "\n")
```

- [ ] **Step 4: Run, verify pass**

Run: `python -m pytest core/tests/test_recognition_phases.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add core/core_engine.py core/tests/test_recognition_phases.py
git commit -m "feat(engine): phase-aware live_status payload + UDS write helper"
```

---

## Task 6: `rescan` engine command

**Files:**
- Modify: `core/core_engine.py`
- Test: `core/tests/test_recognition_phases.py`

- [ ] **Step 1: Write failing test**

Append to `core/tests/test_recognition_phases.py`:

```python
import asyncio


class RescanCommandTest(unittest.TestCase):
    def setUp(self):
        core_engine.state["force_scan"] = False
        core_engine.state["back_off"] = True

    def test_rescan_sets_force_and_clears_backoff(self):
        reply = asyncio.run(core_engine._handle_command({"cmd": "rescan"}))
        self.assertEqual(reply, {"ok": True})
        self.assertTrue(core_engine.state["force_scan"])
        self.assertFalse(core_engine.state["back_off"])

    def test_unknown_cmd_still_rejected(self):
        reply = asyncio.run(core_engine._handle_command({"cmd": "bogus"}))
        self.assertFalse(reply["ok"])
```

- [ ] **Step 2: Run, verify it fails**

Run: `python -m pytest core/tests/test_recognition_phases.py::RescanCommandTest -v`
Expected: FAIL — `test_rescan_sets_force_and_clears_backoff` returns the unknown-cmd error.

- [ ] **Step 3: Implement**

In `core/core_engine.py`, inside `_handle_command`, add before the final `return {"ok": False, "detail": f"unknown cmd: {cmd!r}"}`:

```python
    if cmd == "rescan":
        state["force_scan"] = True
        state["back_off"] = False
        return {"ok": True}
```

- [ ] **Step 4: Run, verify pass**

Run: `python -m pytest core/tests/test_recognition_phases.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add core/core_engine.py core/tests/test_recognition_phases.py
git commit -m "feat(engine): rescan command forces a fresh scan"
```

---

## Task 7: Retry loop in `recognize_audio` (2 retries → no_match → back-off)

**Files:**
- Modify: `core/core_engine.py`
- Test: `core/tests/test_recognition_phases.py`

- [ ] **Step 1: Write failing test**

Append to `core/tests/test_recognition_phases.py`:

```python
class RecognizeRetryTest(unittest.TestCase):
    def setUp(self):
        self.phases = []
        self.handled = []
        # Capture phase publishes instead of hitting the socket.
        async def fake_publish(phase):
            self.phases.append(phase)
        async def fake_capture():
            return b""
        async def fake_handle(track):
            self.handled.append(track)
            core_engine.state["in_song"] = True
            core_engine.state["back_off"] = False
        self._orig = (core_engine._publish_phase, core_engine._capture_sample,
                      core_engine._handle_match)
        core_engine._publish_phase = fake_publish
        core_engine._capture_sample = fake_capture
        core_engine._handle_match = fake_handle
        core_engine.state["back_off"] = False
        core_engine.state["in_song"] = False

    def tearDown(self):
        (core_engine._publish_phase, core_engine._capture_sample,
         core_engine._handle_match) = self._orig

    def test_all_miss_sets_no_match_and_backoff(self):
        async def always_none(_wav):
            return None
        core_engine._identify = always_none
        asyncio.run(core_engine.recognize_audio())
        self.assertEqual(
            self.phases,
            ["scanning", "identifying", "scanning", "retrying",
             "scanning", "retrying", "no_match"],
        )
        self.assertEqual(self.handled, [])
        self.assertTrue(core_engine.state["back_off"])
        self.assertFalse(core_engine.state["in_song"])

    def test_match_on_third_attempt_handles_and_no_backoff(self):
        self.calls = 0
        async def third(_wav):
            self.calls += 1
            return {"title": "Hit"} if self.calls == 3 else None
        core_engine._identify = third
        asyncio.run(core_engine.recognize_audio())
        self.assertEqual(self.handled, [{"title": "Hit"}])
        self.assertFalse(core_engine.state["back_off"])
        self.assertNotIn("no_match", self.phases)
```

- [ ] **Step 2: Run, verify it fails**

Run: `python -m pytest core/tests/test_recognition_phases.py::RecognizeRetryTest -v`
Expected: FAIL — `_capture_sample` / `_identify` / `_handle_match` don't exist yet.

- [ ] **Step 3: Implement**

In `core/core_engine.py`, add a module constant near the top of the Shazam section (after `shazam = Shazam()`):

```python
RECOGNIZE_ATTEMPTS = 3  # 1 initial + 2 auto-retries
```

Replace the entire `recognize_audio` function (lines ~451-512) with the following four functions:

```python
async def _capture_sample() -> bytes:
    """Record sample_len seconds from the mic and return WAV bytes."""
    sample_len = runtime["sample_len"]
    mic = runtime["mic_device"]
    print(f"\n[!] Music detected. Recording {sample_len}s for identification...")
    recording = sd.rec(int(sample_len * 48000), samplerate=48000, channels=1,
                       dtype='int16', device=mic)
    await asyncio.to_thread(sd.wait)
    wav_io = io.BytesIO()
    with wave.open(wav_io, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(48000)
        wf.writeframes(recording.tobytes())
    return wav_io.getvalue()


async def _identify(wav_bytes: bytes) -> dict | None:
    """Return the matched Shazam track dict, or None if no match."""
    print("[!] Analyzing with Shazam...")
    out = await shazam.recognize(wav_bytes)
    if isinstance(out, dict) and 'track' in out:
        return out['track']
    return None


async def _handle_match(track: dict) -> None:
    """Enrich, publish, and record a matched track (the old success branch)."""
    title = track.get('title', 'Unknown Title')
    artist = track.get('subtitle', 'Unknown Artist')

    print("[!] Fetching high-res metadata from iTunes...")
    album, art_url = await fetch_itunes_metadata(artist, title)
    if not art_url:
        art_url = track.get('images', {}).get('coverarthq',
                  track.get('images', {}).get('coverart', ''))
    if not album:
        album = "Unknown Album"

    art_base64 = ""
    if art_url:
        print("[!] Encoding album art to Base64 for Home Assistant...")
        art_base64 = await fetch_image_base64(art_url)

    result_str = f"{artist} - {title}"
    state["artist"] = artist
    state["title"] = title
    state["album"] = album
    state["art_url"] = art_url
    enrichment = _extract_enrichment(track)
    state["isrc"] = enrichment["isrc"]
    state["genre"] = enrichment["genre"]
    state["release_year"] = enrichment["release_year"]

    if result_str != state["last_song"]:
        print(f"🎵 NEW TRACK: {result_str}")
        publish_state("stopped")
        await asyncio.sleep(0.5)
        publish_state("playing", artist, title, album, art_url, art_base64)
        state["last_song"] = result_str
    else:
        print(f"      (Confirmed same track: {state['last_song']})")
        publish_state("playing", artist, title, album, art_url, art_base64)

    state["in_song"] = True
    state["back_off"] = False
    await _publish_phase("playing")


async def recognize_audio():
    """Sample + identify with up to 2 auto-retries. On total failure, publish
    no_match, clear the track, and set the back-off gate so the monitor loop
    waits for a fresh audio onset before scanning again."""
    track = None
    for attempt in range(RECOGNIZE_ATTEMPTS):
        await _publish_phase("scanning")
        wav = await _capture_sample()
        await _publish_phase("identifying" if attempt == 0 else "retrying")
        track = await _identify(wav)
        if track:
            break

    if track:
        await _handle_match(track)
    else:
        print("❌ Could not identify track (gave up).")
        state["in_song"] = False
        state["last_song"] = ""
        state["artist"] = ""
        state["title"] = ""
        state["album"] = ""
        state["art_url"] = ""
        state["isrc"] = None
        state["genre"] = None
        state["release_year"] = None
        state["back_off"] = True
        await _publish_phase("no_match")

    state["silence_counter"] = 0
```

- [ ] **Step 4: Run, verify pass**

Run: `python -m pytest core/tests/test_recognition_phases.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add core/core_engine.py core/tests/test_recognition_phases.py
git commit -m "feat(engine): 2 auto-retries, no_match phase, and re-scan back-off"
```

---

## Task 8: Monitor-loop integration — scan decision + phase frames + force_scan

**Files:**
- Modify: `core/core_engine.py`
- Test: `core/tests/test_recognition_phases.py`

- [ ] **Step 1: Write failing test (the pure decision function)**

Append to `core/tests/test_recognition_phases.py`:

```python
class ScanDecisionTest(unittest.TestCase):
    def d(self, vol, thr, in_song, sc, back_off):
        return core_engine._scan_decision(vol, thr, in_song, sc, back_off)

    def test_loud_idle_scans(self):
        self.assertEqual(self.d(0.5, 0.1, False, 0, False), "scan")

    def test_loud_in_song_steady_ticks(self):
        self.assertEqual(self.d(0.5, 0.1, True, 0, False), "tick")

    def test_loud_in_song_after_silence_rescans(self):
        self.assertEqual(self.d(0.5, 0.1, True, 1, False), "scan")

    def test_loud_but_backoff_waits_for_gap(self):
        self.assertEqual(self.d(0.5, 0.1, False, 0, True), "wait_gap")

    def test_quiet_is_silence(self):
        self.assertEqual(self.d(0.0, 0.1, True, 0, False), "silence")
```

- [ ] **Step 2: Run, verify it fails**

Run: `python -m pytest core/tests/test_recognition_phases.py::ScanDecisionTest -v`
Expected: FAIL — `_scan_decision` not defined.

- [ ] **Step 3: Implement**

In `core/core_engine.py`, add this pure function just before `audio_monitor_loop`:

```python
def _scan_decision(vol, threshold, in_song, silence_counter, back_off):
    """Pure: decide what the monitor loop should do this tick.
    Returns 'scan' | 'tick' | 'wait_gap' | 'silence'."""
    if vol > threshold:
        if back_off:
            return "wait_gap"
        if (not in_song) or silence_counter > 0:
            return "scan"
        return "tick"
    return "silence"
```

Now rewrite the body of `audio_monitor_loop`'s `while True:` loop. Replace the inline UDS-publish block (lines ~564-589) with a phase-aware publish:

```python
        phase = "playing" if state["in_song"] else "listening"
        await _write_uds(json.dumps(build_status_payload(phase, vol, state)) + "\n")
```

Immediately after the calibration-suppression `if` block (the `if calibration is not None ...: await asyncio.sleep(1); continue`), add force_scan handling:

```python
        if state.get("force_scan"):
            state["force_scan"] = False
            stream.stop()
            stream.close()
            await recognize_audio()
            stream = _open_input_stream(audio_callback)
            state["current_rms"] = 0.0
            await asyncio.sleep(1)
            continue
```

Replace the `if vol > runtime["threshold"]:` … `else:` detection block (lines ~598-620) with a `_scan_decision`-driven version:

```python
        decision = _scan_decision(
            vol, runtime["threshold"], state["in_song"],
            state["silence_counter"], state.get("back_off", False),
        )
        if decision == "scan":
            stream.stop()
            stream.close()
            await recognize_audio()
            stream = _open_input_stream(audio_callback)
            state["current_rms"] = 0.0
        elif decision == "wait_gap":
            print("b", end="", flush=True)
        elif decision == "tick":
            print(".", end="", flush=True)
        else:  # silence
            state["back_off"] = False  # gap observed → next onset is fair game
            if state["in_song"]:
                state["silence_counter"] += 1
                print("s", end="", flush=True)
                if state["silence_counter"] >= runtime["stopped_silence"]:
                    print(f"\n[ STOPPED ] {runtime['stopped_silence']}s silence limit reached.")
                    publish_state("stopped")
                    state["in_song"] = False
                    state["last_song"] = ""
                    state["artist"] = ""
                    state["title"] = ""
                    state["album"] = ""
                    state["art_url"] = ""
                    state["silence_counter"] = 0
```

> Note: the `phase` field is additive; `status_msg` still resolves to `"Playing"`/`"Listening"` so the Home Assistant `/api/status` poll is unchanged.

- [ ] **Step 4: Run, verify pass**

Run: `python -m pytest core/tests/test_recognition_phases.py -v`
Expected: PASS. Then run the whole engine suite to confirm no regression:
Run: `python -m pytest core/tests/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add core/core_engine.py core/tests/test_recognition_phases.py
git commit -m "feat(engine): back-off gating, force_scan, and phase frames in monitor loop"
```

---

## Task 9: `POST /api/rescan` backend endpoint

**Files:**
- Modify: `gui/backend_main.py`
- Test (create): `gui/tests/test_rescan_api.py`

- [ ] **Step 1: Write failing test**

Create `gui/tests/test_rescan_api.py`. It reuses the `FakeEngine` harness from `test_calibrate_api.py`:

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
from tests.test_calibrate_api import FakeEngine  # noqa: E402


class RescanApiTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.socket_path = os.path.join(self.tmpdir, "spinsense-cmd.sock")
        self._orig = backend_main.CMD_SOCKET_PATH
        backend_main.CMD_SOCKET_PATH = self.socket_path
        self.client = TestClient(backend_main.app)

    def tearDown(self):
        backend_main.CMD_SOCKET_PATH = self._orig
        self.client.close()
        try:
            os.rmdir(self.tmpdir)
        except OSError:
            pass

    def test_rescan_acks(self):
        fake = FakeEngine(self.socket_path)
        fake.queue({"ok": True})
        fake.start()
        try:
            res = self.client.post("/api/rescan")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json(), {"ok": True})
            self.assertEqual(fake.received[0]["cmd"], "rescan")
        finally:
            fake.stop()

    def test_rescan_503_when_engine_down(self):
        res = self.client.post("/api/rescan")
        self.assertEqual(res.status_code, 503)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run, verify it fails**

Run: `python -m pytest gui/tests/test_rescan_api.py -v`
Expected: FAIL — 405 (route missing).

- [ ] **Step 3: Implement**

In `gui/backend_main.py`, add after the `calibrate_clear` route (around line 281):

```python
@app.post("/api/rescan")
async def rescan():
    try:
        reply = await _send_cmd({"cmd": "rescan"})
    except (FileNotFoundError, ConnectionRefusedError, asyncio.TimeoutError):
        return JSONResponse(
            status_code=503,
            content={"ok": False, "detail": "Engine not reachable"},
        )
    return reply
```

- [ ] **Step 4: Run, verify pass**

Run: `python -m pytest gui/tests/test_rescan_api.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gui/backend_main.py gui/tests/test_rescan_api.py
git commit -m "feat(api): POST /api/rescan forwards to engine command socket"
```

---

## Task 10: Frontend status indicators — glow, caption, pill, Scan-again

**Files:**
- Modify: `gui/static/styles.css`, `gui/templates/dashboard.html`, `gui/static/dashboard.js`, `gui/static/shell.js`

Dogfood-verified (no JS test harness).

- [ ] **Step 1: Add glow styles (do NOT reuse the `pulse` keyframe)**

Append to `gui/static/styles.css`:

```css
/* Recognition-phase glow behind the vinyl. dashboard.js sets
   #vinyl-stage[data-phase]. Keyframes are namespaced glow-* so they don't
   collide with the engine-pill `pulse` (opacity) keyframe above. */
#vinyl-stage { border-radius: 9999px; transition: box-shadow 0.4s ease; }
#vinyl-stage[data-phase="listening"]   { box-shadow: 0 0 18px 2px rgba(152,141,159,0.30); animation: glow-breathe 3.2s ease-in-out infinite; }
#vinyl-stage[data-phase="scanning"]    { box-shadow: 0 0 40px 4px rgba(78,222,163,0.55);  animation: glow-pulse 1.1s ease-in-out infinite; }
#vinyl-stage[data-phase="identifying"] { box-shadow: 0 0 44px 5px rgba(95,180,255,0.62);  animation: glow-pulse 0.7s ease-in-out infinite; }
#vinyl-stage[data-phase="playing"]     { box-shadow: 0 0 48px 6px rgba(221,183,255,0.50); }
#vinyl-stage[data-phase="retrying"]    { box-shadow: 0 0 40px 4px rgba(252,211,77,0.55);  animation: glow-flash 1.0s ease-in-out infinite; }
#vinyl-stage[data-phase="no_match"]    { box-shadow: 0 0 42px 5px rgba(255,84,81,0.60);   animation: glow-flash 1.8s ease-in-out infinite; }

@keyframes glow-breathe { 0%,100% { box-shadow: 0 0 14px 1px rgba(152,141,159,0.22); } 50% { box-shadow: 0 0 22px 3px rgba(152,141,159,0.38); } }
@keyframes glow-pulse   { 0%,100% { filter: brightness(0.9); } 50% { filter: brightness(1.3); } }
@keyframes glow-flash   { 0%,100% { filter: brightness(0.85); } 50% { filter: brightness(1.35); } }

/* New engine-pill phase states (dots). */
.engine-pill[data-state="scanning"]    { color: #4edea3; }
.engine-pill[data-state="scanning"] .engine-pill-dot    { background: #4edea3; box-shadow: 0 0 6px rgba(78,222,163,0.6); }
.engine-pill[data-state="identifying"] { color: #5fb4ff; }
.engine-pill[data-state="identifying"] .engine-pill-dot { background: #5fb4ff; box-shadow: 0 0 6px rgba(95,180,255,0.6); }
.engine-pill[data-state="retrying"]    { color: #fcd34d; }
.engine-pill[data-state="retrying"] .engine-pill-dot    { background: #fcd34d; box-shadow: 0 0 6px rgba(252,211,77,0.6); }
.engine-pill[data-state="no_match"]    { color: #ff5451; }
.engine-pill[data-state="no_match"] .engine-pill-dot    { background: #ff5451; box-shadow: 0 0 6px rgba(255,84,81,0.6); }
```

- [ ] **Step 2: Restructure the dashboard markup**

In `gui/templates/dashboard.html`, replace the **entire** Now-Playing flex container — from `<div class="flex flex-col md:flex-row gap-lg items-center">` at line 12 through its matching closing `</div>` at line 47 (it wraps both the vinyl and the metadata column). The vinyl becomes `#vinyl-stage`, gains a Scan-again button beneath it, and the metadata column gets a phase caption:

```html
    <div class="flex flex-col md:flex-row gap-lg items-center">
      <!-- Vinyl + Scan again -->
      <div class="flex flex-col items-center gap-md flex-shrink-0">
        <div id="vinyl-stage" data-phase="listening" class="relative w-48 h-48 md:w-64 md:h-64">
          <div class="absolute inset-0 rounded-full bg-black/60 shadow-[0_0_50px_rgba(0,0,0,0.8)] border border-surface-variant"></div>
          <div id="vinyl" class="vinyl-disc absolute inset-2 rounded-full bg-[#111] border border-[#222]"
               style="background: repeating-radial-gradient(#111 0, #111 2px, #0a0a0a 3px, #0a0a0a 4px);">
            <div class="absolute inset-0 m-auto w-1/3 h-1/3 rounded-full overflow-hidden border-2 border-primary/40 shadow-[0_0_20px_rgba(221,183,255,0.25)] bg-surface-container-high flex items-center justify-center">
              <img id="vinyl-art" src="/static/placeholder.jpg" alt=""
                   class="w-full h-full object-cover hidden">
              <span id="vinyl-logo" class="material-symbols-outlined text-primary text-3xl">album</span>
            </div>
            <div class="absolute inset-0 m-auto w-2 h-2 bg-background rounded-full"></div>
          </div>
        </div>
        <button id="scan-again"
                class="inline-flex items-center gap-2 text-label-md text-on-surface-variant
                       hover:text-primary bg-surface-container/55 border border-outline-variant/40
                       rounded-full px-4 py-2 backdrop-blur-md transition-colors
                       disabled:opacity-50 disabled:cursor-default">
          <span class="material-symbols-outlined" style="font-size:16px;">refresh</span>
          Scan again
        </button>
      </div>

      <!-- Track metadata + RMS meter -->
      <div class="flex-1 flex flex-col justify-center text-center md:text-left w-full">
        <div class="mb-md">
          <p id="phase-caption" class="text-label-md text-on-surface-variant uppercase tracking-widest mb-1">Idle</p>
          <h3 id="track-title" class="font-headline text-headline-lg text-on-background tracking-tight">Waiting for the needle&hellip;</h3>
          <p id="track-artist" class="text-body-lg text-primary mt-1">&nbsp;</p>
          <p id="track-album" class="text-body-sm text-on-surface-variant mt-0.5">&nbsp;</p>
        </div>

        <div>
          <div class="flex items-center justify-between mb-1">
            <span class="text-label-sm text-outline tracking-widest uppercase">Input</span>
            <span id="input-meter-text" class="text-label-sm text-outline tabular-nums">&minus;&infin; dB</span>
          </div>
          <div class="relative h-2 bg-surface-container-highest rounded-full overflow-hidden border border-outline-variant/30">
            <div id="input-meter" class="h-full bg-primary/80 rounded-r-full shadow-[0_0_10px_rgba(221,183,255,0.35)] transition-[width] duration-200 linear"
                 style="width: 0%;"></div>
            <div id="input-meter-threshold" class="absolute inset-y-0 w-0.5 bg-tertiary shadow-[0_0_4px_rgba(255,180,171,0.8)]" style="left: 50%;"></div>
          </div>
        </div>
      </div>
    </div>
```

- [ ] **Step 3: Drive glow + caption + art from `phase` in dashboard.js**

In `gui/static/dashboard.js`, add element refs near the top (after `const meterThreshold = $("input-meter-threshold");`):

```javascript
  const vinylStage = $("vinyl-stage");
  const phaseCaption = $("phase-caption");
  const scanBtn = $("scan-again");

  // Eyebrow = short label; headline = descriptive line (when not playing) or
  // the track title (when playing). Filling both avoids an empty headline.
  const PHASE_EYEBROW = {
    listening: "Idle", scanning: "Scanning", identifying: "Identifying",
    retrying: "Retrying", no_match: "No match", playing: "Now playing",
  };
  const PHASE_HEADLINE = {
    listening:   "Waiting for the needle…",
    scanning:    "Listening to the track…",
    identifying: "Identifying…",
    retrying:    "Couldn't catch it — retrying…",
    no_match:    "Couldn't identify this one",
  };
  const SPIN_PHASES = new Set(["playing", "scanning", "identifying", "retrying"]);
```

Replace the track/vinyl section of `handleFrame` (the `if (title) { … } else { … }` block, lines ~117-130) with a phase-driven version:

```javascript
    // Phase drives display. Frames carry the prior track during scanning/etc.,
    // so we key everything off phase (falling back to title presence if the
    // engine hasn't sent a phase — e.g. the mock stream).
    const phase = payload.phase || (title ? "playing" : "listening");
    const uiPhase = phase === "stopped" ? "listening" : phase;
    if (vinylStage) vinylStage.dataset.phase = uiPhase;
    if (phaseCaption) phaseCaption.textContent = PHASE_EYEBROW[uiPhase] || "Idle";
    setVinylSpinning(SPIN_PHASES.has(uiPhase));

    if (uiPhase === "playing" && title) {
      titleEl.textContent  = title;
      artistEl.textContent = track.artist || "Unknown Artist";
      albumEl.textContent  = track.album  || "";
      setVinylArt(track.art_url || null);
    } else {
      titleEl.textContent  = PHASE_HEADLINE[uiPhase] || "Waiting for the needle…";
      artistEl.innerHTML   = "&nbsp;";
      albumEl.innerHTML    = "&nbsp;";
      setVinylArt(null);
    }
```

Wire the Scan-again button — add inside the `DOMContentLoaded` handler (after `window.SpinSense.onFrame(handleFrame);`):

```javascript
    if (scanBtn) {
      scanBtn.addEventListener("click", async () => {
        scanBtn.disabled = true;
        try { await fetch("/api/rescan", { method: "POST" }); }
        catch (_) { /* engine may be down; ignore */ }
        setTimeout(() => { scanBtn.disabled = false; }, 1500);
      });
    }
```

- [ ] **Step 4: Map `phase` → pill in shell.js**

In `gui/static/shell.js`, extend `LABELS`:

```javascript
  const LABELS = {
    idle:         "Idle",
    listening:    "Listening",
    scanning:     "Scanning",
    identifying:  "Identifying",
    retrying:     "Retrying",
    no_match:     "No match",
    playing:      "Playing",
    disconnected: "Disconnected",
  };
```

Replace `handleFrame` so phase wins when present:

```javascript
  function handleFrame(payload) {
    const phase = payload && payload.phase;
    if (phase) {
      setPillState(phase === "stopped" ? "listening" : phase);
    } else {
      const track = (payload && payload.track) || {};
      setPillState(track.title ? "playing" : "listening");
    }
    notify(payload);
  }
```

- [ ] **Step 5: Verify by dogfooding**

Run the app. On the dashboard:
1. Idle → grey breathing glow, caption "Waiting for the needle…".
2. Drop a record → glow goes **green** (scanning) → **blue** (identifying) → **steady purple** (playing) with the track + art; pill follows.
3. Force a fail: play something Shazam won't know (or disconnect briefly) → **yellow** retrying, then **red** no_match, then back to grey breathing while the next track is still catchable.
4. Click **Scan again** → an immediate green→blue cycle; button disables ~1.5 s.
5. Confirm the engine pill (bottom-left) is NOT stuck on "Playing" during scanning.

Expected: all transitions render; no console errors; the previously-stale art clears the instant it leaves `playing`.

- [ ] **Step 6: Commit**

```bash
git add gui/static/styles.css gui/templates/dashboard.html gui/static/dashboard.js gui/static/shell.js
git commit -m "feat(dashboard): per-phase vinyl glow, status caption, and Scan again"
```

---

## Final verification

- [ ] **Full suite:** `python -m pytest -v` from `/home/ubuntu/SpinSense/SpinSense` → all green.
- [ ] **Dogfood end-to-end:** spin a record through identify → play → a deliberate miss → delete a scrobble → Undo → let a delete expire and confirm the purge reclaims its art after the grace window (or call `play_history.purge_deleted(grace_seconds=0)` in a shell to force it).
- [ ] **HA unaffected:** `GET /api/status` still returns `status_msg` of `Playing`/`Listening`/`stopped` (the `phase` field is purely additive).

## Spec traceability

- #2 phase model + 6 glows → Tasks 5, 7, 8, 10
- #2 two auto-retries → Task 7
- #2 back-off after no_match → Tasks 7, 8
- #2 stale-art fix → Task 10 (display keyed on phase)
- #2 manual Scan again over existing command socket → Tasks 6, 9, 10
- #2 `stopped` stays backend-only, renders as listening → Tasks 8, 10
- #3 soft-delete + Undo → Tasks 1, 3, 4
- #3 art purge (loose end closed) → Tasks 2, 3
- Error handling (503/404, missing phase, missing art) → Tasks 3, 5, 9, 10

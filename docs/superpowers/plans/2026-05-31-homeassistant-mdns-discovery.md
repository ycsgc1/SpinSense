# Home Assistant mDNS Discovery + History Schema Future-Proofing — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make SpinSense advertise itself over mDNS so the companion Home Assistant integration auto-discovers it, expose the HTTP status the integration polls, make the listen port configurable (default `3313`), and add nullable history columns for future analytics.

**Architecture:** A new zeroconf advertiser runs inside the FastAPI (GUI) process, bound to its lifespan; a new `GET /api/status` serves the last `live_status` payload cached in the IPC manager; the core engine enriches each recognition with ISRC/genre/year and includes them in the status frame; the setup wizard gains independent mDNS + MQTT toggles.

**Tech Stack:** Python 3.11, FastAPI/uvicorn, `python-zeroconf`, SQLite (`sqlite3`), Pydantic v1-style `.dict()`, vanilla JS wizard, Docker.

**Spec:** `docs/superpowers/specs/2026-05-31-homeassistant-mdns-discovery-design.md`

**Test command convention:** run from repo root with `python -m pytest <path> -v`. Test files add `gui/` or `core/` to `sys.path` themselves (existing pattern), so no package prefix is needed.

---

## File Structure

**App repo (`ycsgc1/SpinSense`, branch `feat/homeassistant-mdns-discovery`):**

- Create: `gui/discovery.py` — zeroconf advertiser (start/stop/reconcile, ServiceInfo builder).
- Create: `gui/tests/test_discovery.py` — unit tests for the ServiceInfo builder + reconcile logic.
- Create: `gui/tests/test_status_api.py` — tests for `GET /api/status`.
- Modify: `requirements.txt` — add `zeroconf`.
- Modify: `docker/entrypoint.sh` — configurable `--port`.
- Modify: `docker/Dockerfile` — `EXPOSE 3313`.
- Modify: `docker-compose.yml` — reference example: data volume already present; add `SPINSENSE_PORT`, document host-networking option.
- Modify: `gui/config_manager.py` — add `Discovery` section + `MQTT.Enabled`.
- Modify: `gui/play_history.py` — additive nullable columns + `record_play` params.
- Modify: `gui/ipc_manager.py` — `last_status` cache + enrichment passthrough.
- Modify: `gui/backend_main.py` — `GET /api/status`, lifespan wiring, reconcile on config save.
- Modify: `core/core_engine.py` — enrichment extraction + include in status frame + honor `MQTT.Enabled`.
- Modify: `gui/templates/setup.html` — mDNS + MQTT toggles in step 3.
- Modify: `gui/static/setup.js` — toggle state, gate MQTT payload, write `Discovery.mDNS.Enabled`.
- Modify: `gui/tests/test_config_round_trip.py` — cover new config fields.
- Modify: `gui/tests/test_play_history.py` — cover new columns.

**Integration repo (`ycsgc1/homeassistant-spinsense`, NEW, separate deliverable):** see Task 12.

**Deployment (user action on Dockge):** see Task 13.

---

## Task 1: Add zeroconf dependency and configurable listen port

**Files:**
- Modify: `requirements.txt`
- Modify: `docker/entrypoint.sh:16`
- Modify: `docker/Dockerfile`

- [ ] **Step 1: Add the dependency**

In `requirements.txt`, add this line after the `uvicorn[standard]==0.29.0` line:

```
zeroconf==0.131.0
```

- [ ] **Step 2: Make the uvicorn port configurable, default 3313**

In `docker/entrypoint.sh`, replace line 16:

```bash
exec uvicorn backend_main:app --host 0.0.0.0 --port 8000
```

with:

```bash
exec uvicorn backend_main:app --host 0.0.0.0 --port "${SPINSENSE_PORT:-3313}"
```

Also update the comment on line 15 from `port 8000` to `the configured port (SPINSENSE_PORT, default 3313)`.

- [ ] **Step 3: Document the port in the Dockerfile**

In `docker/Dockerfile`, add this line immediately before the `ENTRYPOINT` line (line 24):

```dockerfile
EXPOSE 3313
```

- [ ] **Step 4: Verify the entrypoint honors the env var**

Run:

```bash
SPINSENSE_PORT=3313 bash -n docker/entrypoint.sh && echo "syntax ok"
grep -n 'SPINSENSE_PORT' docker/entrypoint.sh
```

Expected: `syntax ok` and the grep shows the parameter-expansion line.

- [ ] **Step 5: Commit**

```bash
git add requirements.txt docker/entrypoint.sh docker/Dockerfile
git commit -m "feat: configurable listen port (default 3313) + zeroconf dep"
```

---

## Task 2: Config schema — Discovery section + MQTT.Enabled

**Files:**
- Modify: `gui/config_manager.py:31-50`
- Test: `gui/tests/test_config_round_trip.py`

- [ ] **Step 1: Write the failing test**

Append to `gui/tests/test_config_round_trip.py` (inside the existing test class, or as new test functions matching the file's style — it uses `unittest`):

```python
class TestDiscoveryConfig(unittest.TestCase):
    def test_defaults_include_discovery_and_mqtt_enabled(self):
        from config_manager import SpinSenseConfig
        cfg = SpinSenseConfig().dict()
        self.assertEqual(cfg["Discovery"]["mDNS"]["Enabled"], True)
        self.assertEqual(cfg["Discovery"]["mDNS"]["Service_Name"], "")
        self.assertEqual(cfg["MQTT"]["Enabled"], False)

    def test_roundtrip_preserves_discovery(self):
        from config_manager import SpinSenseConfig
        data = SpinSenseConfig().dict()
        data["Discovery"]["mDNS"]["Enabled"] = False
        data["Discovery"]["mDNS"]["Service_Name"] = "Living Room"
        data["MQTT"]["Enabled"] = True
        out = SpinSenseConfig(**data).dict()
        self.assertEqual(out["Discovery"]["mDNS"]["Enabled"], False)
        self.assertEqual(out["Discovery"]["mDNS"]["Service_Name"], "Living Room")
        self.assertEqual(out["MQTT"]["Enabled"], True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest gui/tests/test_config_round_trip.py -v -k Discovery`
Expected: FAIL — `KeyError: 'Discovery'` (and `'Enabled'` missing from MQTT).

- [ ] **Step 3: Add the models**

In `gui/config_manager.py`, add `Enabled` to `MQTTConfig` (line 41-44) so it becomes:

```python
class MQTTConfig(BaseModel):
    Enabled: bool = False
    Broker: MQTTBrokerConfig = MQTTBrokerConfig()
    Discovery: MQTTDiscoveryConfig = MQTTDiscoveryConfig()
    Topics: MQTTTopicsConfig = MQTTTopicsConfig()
```

Add these two new models immediately before `class SpinSenseConfig` (line 46):

```python
class MDNSConfig(BaseModel):
    Enabled: bool = True
    Service_Name: str = ""  # empty => derive from hostname at runtime

class DiscoveryConfig(BaseModel):
    mDNS: MDNSConfig = MDNSConfig()
```

Add `Discovery` to `SpinSenseConfig`:

```python
class SpinSenseConfig(BaseModel):
    System: SystemConfig = SystemConfig()
    Hardware: HardwareConfig = HardwareConfig()
    Audio: AudioConfig = AudioConfig()
    MQTT: MQTTConfig = MQTTConfig()
    Discovery: DiscoveryConfig = DiscoveryConfig()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest gui/tests/test_config_round_trip.py -v -k Discovery`
Expected: PASS (both tests).

- [ ] **Step 5: Run the full config test file to confirm no regression**

Run: `python -m pytest gui/tests/test_config_round_trip.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add gui/config_manager.py gui/tests/test_config_round_trip.py
git commit -m "feat(config): add Discovery.mDNS settings and explicit MQTT.Enabled"
```

---

## Task 3: History schema — nullable enrichment columns

**Files:**
- Modify: `gui/play_history.py:24-39` (init_db), `42-55` (record_play), `66-79` (recent_plays SELECT)
- Test: `gui/tests/test_play_history.py`

- [ ] **Step 1: Write the failing test**

Append to `gui/tests/test_play_history.py` (it uses a temp db_path; mirror the existing pattern — check the top of the file for how it builds `db_path`):

```python
class TestEnrichmentColumns(unittest.TestCase):
    def setUp(self):
        import tempfile, os
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        import play_history
        play_history.init_db(self.db)

    def test_init_db_is_idempotent_with_new_columns(self):
        import play_history
        # Running init_db twice must not error and must leave columns present.
        play_history.init_db(self.db)
        import sqlite3
        cols = {r[1] for r in sqlite3.connect(self.db).execute("PRAGMA table_info(plays)")}
        self.assertTrue({"isrc", "genre", "release_year"} <= cols)

    def test_record_play_stores_enrichment(self):
        import play_history
        pid = play_history.record_play(
            "Title", "Artist", "Album", "http://art",
            isrc="USRC12345678", genre="Rock", release_year=1977,
            db_path=self.db,
        )
        rows = play_history.recent_plays(10, 0, db_path=self.db)
        self.assertEqual(rows[0]["id"], pid)
        self.assertEqual(rows[0]["isrc"], "USRC12345678")
        self.assertEqual(rows[0]["genre"], "Rock")
        self.assertEqual(rows[0]["release_year"], 1977)

    def test_record_play_enrichment_optional(self):
        import play_history
        pid = play_history.record_play("T", "A", None, None, db_path=self.db)
        rows = play_history.recent_plays(10, 0, db_path=self.db)
        self.assertIsNone(rows[0]["isrc"])
        self.assertIsNone(rows[0]["genre"])
        self.assertIsNone(rows[0]["release_year"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest gui/tests/test_play_history.py -v -k Enrichment`
Expected: FAIL — `record_play() got an unexpected keyword argument 'isrc'` / missing columns.

- [ ] **Step 3: Add idempotent column migration in init_db**

In `gui/play_history.py`, replace the `init_db` function (lines 24-39) with:

```python
_ENRICHMENT_COLUMNS = {
    "isrc": "TEXT",
    "genre": "TEXT",
    "release_year": "INTEGER",
}


def init_db(db_path: str | None = None) -> None:
    with _connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS plays (
              id          INTEGER PRIMARY KEY AUTOINCREMENT,
              title       TEXT    NOT NULL,
              artist      TEXT    NOT NULL,
              album       TEXT,
              art_url     TEXT,
              art_path    TEXT,
              played_at   INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_plays_played_at ON plays (played_at DESC);
            """
        )
        existing = {row[1] for row in conn.execute("PRAGMA table_info(plays)")}
        for name, sqltype in _ENRICHMENT_COLUMNS.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE plays ADD COLUMN {name} {sqltype}")
```

- [ ] **Step 4: Add enrichment params to record_play**

Replace `record_play` (lines 42-55) with:

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
) -> int:
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO plays "
            "(title, artist, album, art_url, played_at, isrc, genre, release_year) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (title, artist, album, art_url, int(time.time()), isrc, genre, release_year),
        )
        return int(cur.lastrowid)
```

- [ ] **Step 5: Return the new columns from recent_plays**

In `recent_plays` (line 75), replace the SELECT column list so it reads:

```python
        rows = conn.execute(
            "SELECT id, title, artist, album, art_url, art_path, played_at, "
            "isrc, genre, release_year "
            "FROM plays ORDER BY played_at DESC, id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest gui/tests/test_play_history.py -v`
Expected: all PASS (new + existing).

- [ ] **Step 7: Commit**

```bash
git add gui/play_history.py gui/tests/test_play_history.py
git commit -m "feat(history): add nullable isrc/genre/release_year columns"
```

---

## Task 4: Engine — enrichment extraction + status-frame fields + MQTT.Enabled

**Files:**
- Modify: `core/core_engine.py` (recognition path ~414-450, status frame ~515-520, MQTT enable)
- Test: `core/tests/test_enrichment.py` (new)

- [ ] **Step 1: Write the failing test for the extraction helper**

Create `core/tests/test_enrichment.py`:

```python
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CORE_DIR = os.path.dirname(HERE)
if CORE_DIR not in sys.path:
    sys.path.insert(0, CORE_DIR)

import unittest


class TestExtractEnrichment(unittest.TestCase):
    def test_pulls_isrc_genre_year_when_present(self):
        from core_engine import _extract_enrichment
        track = {
            "isrc": "USRC17607839",
            "genres": {"primary": "Rock"},
            "sections": [
                {"metadata": [{"title": "Released", "text": "1977"}]},
            ],
        }
        out = _extract_enrichment(track)
        self.assertEqual(out["isrc"], "USRC17607839")
        self.assertEqual(out["genre"], "Rock")
        self.assertEqual(out["release_year"], 1977)

    def test_missing_fields_are_none(self):
        from core_engine import _extract_enrichment
        out = _extract_enrichment({})
        self.assertIsNone(out["isrc"])
        self.assertIsNone(out["genre"])
        self.assertIsNone(out["release_year"])

    def test_non_numeric_year_is_none(self):
        from core_engine import _extract_enrichment
        track = {"sections": [{"metadata": [{"title": "Released", "text": "n/a"}]}]}
        self.assertIsNone(_extract_enrichment(track)["release_year"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest core/tests/test_enrichment.py -v`
Expected: FAIL — `cannot import name '_extract_enrichment'`.

- [ ] **Step 3: Implement the helper**

In `core/core_engine.py`, add this pure function just above `async def recognize_audio():` (line 397):

```python
def _extract_enrichment(track: dict) -> dict:
    """Best-effort pull of stable-id/genre/year from a Shazam track object.
    Every field is optional; anything missing or unparseable is None so it
    never blocks a play from being recorded."""
    track = track or {}
    isrc = track.get("isrc") or None

    genre = None
    genres = track.get("genres")
    if isinstance(genres, dict):
        genre = genres.get("primary") or None

    release_year = None
    for section in track.get("sections", []) or []:
        for item in (section or {}).get("metadata", []) or []:
            if (item or {}).get("title") == "Released":
                text = str(item.get("text", "")).strip()
                # text is sometimes "1977" and sometimes a full date; take any
                # leading 4-digit run.
                digits = ""
                for ch in text:
                    if ch.isdigit():
                        digits += ch
                        if len(digits) == 4:
                            break
                    elif digits:
                        break
                if len(digits) == 4:
                    release_year = int(digits)
                break
        if release_year is not None:
            break

    return {"isrc": isrc, "genre": genre, "release_year": release_year}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest core/tests/test_enrichment.py -v`
Expected: all PASS.

- [ ] **Step 5: Populate enrichment into engine state and the status frame**

In `recognize_audio()`, after line 436 (`state["art_url"] = art_url`), add:

```python
        enrichment = _extract_enrichment(track)
        state["isrc"] = enrichment["isrc"]
        state["genre"] = enrichment["genre"]
        state["release_year"] = enrichment["release_year"]
```

Then in `audio_monitor_loop()` extend the `"track"` object in the status frame (lines 515-520) to:

```python
                        "track": {
                            "title": state.get("title", ""),
                            "artist": state.get("artist", ""),
                            "album": state.get("album", ""),
                            "art_url": state.get("art_url", ""),
                            "isrc": state.get("isrc"),
                            "genre": state.get("genre"),
                            "release_year": state.get("release_year"),
                        },
```

- [ ] **Step 6: Honor MQTT.Enabled when deciding to connect**

Find where `MQTT_ENABLED` is set in `core/core_engine.py` (search `MQTT_ENABLED`). In the config-loading path (`_populate_runtime` / `_load_config` region near line 100), set the module flag from config. Add, right after `_populate_runtime(_initial_cfg)` (line 103):

```python
MQTT_ENABLED = bool(_initial_cfg.get("MQTT", {}).get("Enabled", False))
```

and delete the later hardcoded `MQTT_ENABLED = False` on line 123 (the assignment above now owns it). Leave the `mqtt_client = ...` line on 122 intact.

> Note: if the engine has a config-watch loop that should re-read this on change, that is a follow-up; for 1.0 the flag is read at startup, consistent with how other MQTT settings are read today.

- [ ] **Step 7: Run the engine test suite to confirm nothing broke**

Run: `python -m pytest core/tests/ -v`
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add core/core_engine.py core/tests/test_enrichment.py
git commit -m "feat(engine): extract isrc/genre/year, surface in status frame, honor MQTT.Enabled"
```

---

## Task 5: IPC manager — last-status cache + enrichment passthrough

**Files:**
- Modify: `gui/ipc_manager.py:19-39` (cache), `82-111` (passthrough)
- Test: `gui/tests/test_ipc_status.py` (new)

- [ ] **Step 1: Write the failing test**

Create `gui/tests/test_ipc_status.py`:

```python
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

import asyncio
import unittest


class TestLastStatusCache(unittest.TestCase):
    def test_broadcast_caches_live_status_payload(self):
        from ipc_manager import ConnectionManager
        mgr = ConnectionManager()
        frame = {"type": "live_status", "payload": {"engine_active": True, "status_msg": "Playing"}}
        asyncio.run(mgr.broadcast(frame))
        self.assertEqual(mgr.last_status["status_msg"], "Playing")

    def test_default_last_status_is_stopped(self):
        from ipc_manager import ConnectionManager
        mgr = ConnectionManager()
        self.assertEqual(mgr.last_status["status_msg"], "stopped")
        self.assertEqual(mgr.last_status["engine_active"], False)
        self.assertIn("track", mgr.last_status)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest gui/tests/test_ipc_status.py -v`
Expected: FAIL — `AttributeError: 'ConnectionManager' object has no attribute 'last_status'`.

- [ ] **Step 3: Add the cache to ConnectionManager**

In `gui/ipc_manager.py`, add a module constant above `class ConnectionManager` (line 19):

```python
DEFAULT_STATUS = {
    "engine_active": False,
    "status_msg": "stopped",
    "rms_level": 0.0,
    "track": {"title": "", "artist": "", "album": "", "art_url": ""},
}
```

Change `__init__` (lines 20-21) to:

```python
    def __init__(self):
        self.active_connections: list["WebSocket"] = []
        self.last_status: dict = dict(DEFAULT_STATUS)
```

Change `broadcast` (lines 31-36) to update the cache:

```python
    async def broadcast(self, message: dict):
        if message.get("type") == "live_status" and isinstance(message.get("payload"), dict):
            self.last_status = message["payload"]
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                pass
```

- [ ] **Step 4: Pass enrichment through to record_play**

In `_record_if_new` (lines 96-103), after `art_url = track.get("art_url") or None` (line 98) add:

```python
    isrc = track.get("isrc") or None
    genre = track.get("genre") or None
    release_year = track.get("release_year") or None
```

and change the `record_play` call (lines 101-103) to:

```python
        play_id = await asyncio.to_thread(
            play_history.record_play, title, artist, album, art_url,
            isrc=isrc, genre=genre, release_year=release_year,
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest gui/tests/test_ipc_status.py gui/tests/test_play_history.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add gui/ipc_manager.py gui/tests/test_ipc_status.py
git commit -m "feat(ipc): cache last live_status, pass enrichment to record_play"
```

---

## Task 6: GET /api/status endpoint

**Files:**
- Modify: `gui/backend_main.py` (add route near line 264)
- Test: `gui/tests/test_status_api.py` (new)

- [ ] **Step 1: Write the failing test**

Create `gui/tests/test_status_api.py`:

```python
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

import unittest
from fastapi.testclient import TestClient


class TestStatusApi(unittest.TestCase):
    def test_status_default_is_stopped(self):
        import importlib, ipc_manager
        importlib.reload(ipc_manager)
        import backend_main
        importlib.reload(backend_main)
        client = TestClient(backend_main.app)
        r = client.get("/api/status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status_msg"], "stopped")
        self.assertEqual(body["engine_active"], False)
        self.assertIn("track", body)

    def test_status_reflects_last_broadcast(self):
        import backend_main
        backend_main.manager.last_status = {
            "engine_active": True, "status_msg": "Playing", "rms_level": 0.2,
            "track": {"title": "X", "artist": "Y", "album": "", "art_url": "http://a"},
        }
        client = TestClient(backend_main.app)
        body = client.get("/api/status").json()
        self.assertEqual(body["status_msg"], "Playing")
        self.assertEqual(body["track"]["title"], "X")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest gui/tests/test_status_api.py -v`
Expected: FAIL — 404 for `/api/status`.

- [ ] **Step 3: Add the route**

In `gui/backend_main.py`, after the `/api/plays` route (line 271) and before the `# --- WebSocket ---` comment (line 274), add:

```python
@app.get("/api/status")
def get_status():
    """Last-known engine status, in the shape the Home Assistant integration
    polls. Defaults to a 'stopped' payload when the engine hasn't reported."""
    return manager.last_status
```

The name `manager` is already imported on line 15.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest gui/tests/test_status_api.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add gui/backend_main.py gui/tests/test_status_api.py
git commit -m "feat(api): add GET /api/status for Home Assistant polling"
```

---

## Task 7: mDNS advertiser module + FastAPI wiring + reconcile on config save

**Files:**
- Create: `gui/discovery.py`
- Create: `gui/tests/test_discovery.py`
- Modify: `gui/backend_main.py` (lifespan + reconcile on POST /api/config)

- [ ] **Step 1: Write the failing test for the ServiceInfo builder**

Create `gui/tests/test_discovery.py`:

```python
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

import unittest


class TestServiceInfoBuilder(unittest.TestCase):
    def test_build_service_info_uses_type_and_port(self):
        import discovery
        info = discovery.build_service_info(port=3313, service_name="Living Room", version="1.0")
        self.assertEqual(info.type, "_spinsense._tcp.local.")
        self.assertTrue(info.name.endswith("._spinsense._tcp.local."))
        self.assertIn("Living Room", info.name)
        self.assertEqual(info.port, 3313)
        # TXT properties are bytes-keyed/bytes-valued in zeroconf
        self.assertEqual(info.properties.get(b"version"), b"1.0")

    def test_build_service_info_defaults_name_from_hostname(self):
        import discovery
        info = discovery.build_service_info(port=3313, service_name="", version="1.0")
        # Empty service name => a non-empty instance name derived from hostname
        self.assertTrue(len(info.name) > len("._spinsense._tcp.local."))


class TestEnabledFlag(unittest.TestCase):
    def test_is_enabled_reads_config(self):
        import discovery
        self.assertTrue(discovery.is_enabled({"Discovery": {"mDNS": {"Enabled": True}}}))
        self.assertFalse(discovery.is_enabled({"Discovery": {"mDNS": {"Enabled": False}}}))
        self.assertTrue(discovery.is_enabled({}))  # default on
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest gui/tests/test_discovery.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'discovery'`.

- [ ] **Step 3: Implement gui/discovery.py**

Create `gui/discovery.py`:

```python
"""mDNS/zeroconf advertisement of the SpinSense HTTP service so the companion
Home Assistant integration can auto-discover it on the LAN.

Runs inside the FastAPI (GUI) process. All failures are non-fatal: if zeroconf
cannot bind (no network, UDP 5353 in use), we log and carry on serving HTTP.
"""
import logging
import os
import socket

from zeroconf import ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

log = logging.getLogger(__name__)

SERVICE_TYPE = "_spinsense._tcp.local."


def get_port() -> int:
    try:
        return int(os.environ.get("SPINSENSE_PORT", "3313"))
    except (TypeError, ValueError):
        return 3313


def is_enabled(config: dict) -> bool:
    return bool(
        (config or {}).get("Discovery", {}).get("mDNS", {}).get("Enabled", True)
    )


def _instance_name(service_name: str) -> str:
    name = (service_name or "").strip()
    if not name:
        host = socket.gethostname().split(".")[0] or "spinsense"
        name = f"SpinSense ({host})"
    return f"{name}.{SERVICE_TYPE}"


def build_service_info(port: int, service_name: str, version: str) -> ServiceInfo:
    return ServiceInfo(
        type_=SERVICE_TYPE,
        name=_instance_name(service_name),
        port=port,
        properties={"version": version, "path": "/"},
        server=f"{socket.gethostname().split('.')[0] or 'spinsense'}.local.",
    )


class Advertiser:
    """Owns the AsyncZeroconf instance and the currently-registered service."""

    def __init__(self, version: str = "1.0"):
        self._azc: AsyncZeroconf | None = None
        self._info: ServiceInfo | None = None
        self._version = version

    async def reconcile(self, config: dict) -> None:
        """Make the live advertisement match config: register if enabled and
        not yet registered, unregister if disabled."""
        try:
            if is_enabled(config):
                await self._ensure_registered(config)
            else:
                await self.stop()
        except Exception as exc:  # never let discovery break the app
            log.warning("mDNS reconcile failed: %s", exc)

    async def _ensure_registered(self, config: dict) -> None:
        if self._info is not None:
            return  # already advertising
        service_name = (
            (config or {}).get("Discovery", {}).get("mDNS", {}).get("Service_Name", "")
        )
        info = build_service_info(get_port(), service_name, self._version)
        if self._azc is None:
            self._azc = AsyncZeroconf()
        await self._azc.async_register_service(info)
        self._info = info
        log.info("mDNS advertising %s on port %s", info.name, info.port)

    async def start(self, config: dict) -> None:
        await self.reconcile(config)

    async def stop(self) -> None:
        try:
            if self._azc is not None and self._info is not None:
                await self._azc.async_unregister_service(self._info)
            if self._azc is not None:
                await self._azc.async_close()
        except Exception as exc:
            log.warning("mDNS stop failed: %s", exc)
        finally:
            self._azc = None
            self._info = None


advertiser = Advertiser()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest gui/tests/test_discovery.py -v`
Expected: all PASS.

- [ ] **Step 5: Wire the advertiser into the FastAPI lifespan**

In `gui/backend_main.py`, add to the imports near line 15:

```python
from discovery import advertiser
```

Replace the `lifespan` function (lines 59-65) with:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    play_history.init_db()
    os.makedirs(ART_DIR, exist_ok=True)
    task = asyncio.create_task(start_uds_listener())
    try:
        await advertiser.start(load_config())
    except Exception as e:
        print(f"⚠️ mDNS advertiser failed to start: {e}")
    yield
    await advertiser.stop()
    task.cancel()
```

`load_config` is already imported on line 14.

- [ ] **Step 6: Reconcile the advertiser when config is saved**

In `update_config` (lines 132-151), after the successful `save_config` branch — i.e. replace the final `return {"status": "success"}` (line 151) with:

```python
    try:
        await advertiser.reconcile(new_config)
    except Exception as e:
        print(f"⚠️ mDNS reconcile after config save failed: {e}")
    return {"status": "success"}
```

- [ ] **Step 7: Verify the app still imports and the status/discovery tests pass**

Run: `python -m pytest gui/tests/test_discovery.py gui/tests/test_status_api.py -v`
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add gui/discovery.py gui/tests/test_discovery.py gui/backend_main.py
git commit -m "feat(discovery): advertise _spinsense._tcp via zeroconf, reconcile on config save"
```

---

## Task 8: Setup wizard — independent mDNS + MQTT toggles

The current step 3 ("Connect to Home Assistant") only configures MQTT. We add two toggles at the top: **mDNS auto-discovery** (default ON) and **Enable MQTT** (default OFF) that shows/hides the existing broker fields. No step-machine changes — this is additive within step 3.

**Files:**
- Modify: `gui/templates/setup.html:238-289`
- Modify: `gui/static/setup.js` (field refs ~42-53, load ~169-178, buildPayload ~186-199, listeners ~280)

- [ ] **Step 1: Add the toggles markup**

In `gui/templates/setup.html`, immediately after the step-3 intro paragraph `</p>` (line 244) and before the `<div class="grid ...">` (line 246), insert:

```html
      <div class="mt-md space-y-3">
        <label class="flex items-center justify-between gap-md p-3 rounded-lg bg-surface-container-high">
          <span>
            <span class="text-body-md text-on-surface block">Home Assistant auto-discovery (mDNS)</span>
            <span class="text-body-sm text-on-surface-variant">Zero-config. Install the SpinSense HACS integration and it finds this device automatically. Recommended.</span>
          </span>
          <input type="checkbox" id="wizard-mdns-enabled" class="form-checkbox h-5 w-5" checked>
        </label>
        <label class="flex items-center justify-between gap-md p-3 rounded-lg bg-surface-container-high">
          <span>
            <span class="text-body-md text-on-surface block">MQTT (advanced)</span>
            <span class="text-body-sm text-on-surface-variant">Publish to your own MQTT broker instead of (or in addition to) mDNS.</span>
          </span>
          <input type="checkbox" id="wizard-mqtt-enabled" class="form-checkbox h-5 w-5">
        </label>
      </div>
```

Then wrap the existing broker `<div class="grid ...">` (lines 246-263) and the test-connection row (lines 265-271) in a container that can be hidden. Change the opening `<div class="grid grid-cols-1 md:grid-cols-2 gap-md mt-md">` on line 246 to be preceded by:

```html
      <div id="wizard-mqtt-fields" class="hidden">
```

and add a closing `</div>` immediately after the test-connection row's closing `</div>` (after line 271).

- [ ] **Step 2: Reference the toggles in setup.js**

In `gui/static/setup.js`, add to the element-refs block (after line 48, near the other MQTT refs):

```javascript
  const MDNS_ENABLED = document.getElementById("wizard-mdns-enabled");
  const MQTT_ENABLED = document.getElementById("wizard-mqtt-enabled");
  const MQTT_FIELDS = document.getElementById("wizard-mqtt-fields");
```

- [ ] **Step 3: Initialize toggle state from config on load**

In the load block (after line 178, where MQTT fields are populated), add:

```javascript
    MDNS_ENABLED.checked = getNested(initialConfig, "Discovery.mDNS.Enabled") ?? true;
    MQTT_ENABLED.checked = getNested(initialConfig, "MQTT.Enabled") ?? false;
    MQTT_FIELDS.classList.toggle("hidden", !MQTT_ENABLED.checked);
```

- [ ] **Step 4: Toggle field visibility on change**

Near the other `addEventListener` calls (around line 280), add:

```javascript
  MQTT_ENABLED.addEventListener("change", () => {
    MQTT_FIELDS.classList.toggle("hidden", !MQTT_ENABLED.checked);
  });
```

- [ ] **Step 5: Write the toggles into the saved payload**

In `buildPayload` (lines 186-199), replace the MQTT broker assignment block (lines 192-197, currently always writing the broker fields) so it is gated on the MQTT toggle and also writes the two enable flags + mDNS setting:

```javascript
    setNested(payload, "Discovery.mDNS.Enabled", !!MDNS_ENABLED.checked);
    setNested(payload, "MQTT.Enabled", !!MQTT_ENABLED.checked);
    if (MQTT_ENABLED.checked) {
      setNested(payload, "MQTT.Broker.Host", MQTT_HOST.value);
      setNested(payload, "MQTT.Broker.Port", Number(MQTT_PORT.value || 1883));
      setNested(payload, "MQTT.Broker.User", MQTT_USER.value);
      setNested(payload, "MQTT.Broker.Password", MQTT_PASS.value);
    }
```

(If the original lines 192-197 were wrapped in an existing `if`, replace that whole block with the above.)

- [ ] **Step 6: Manual verification in the browser**

Run the app locally (or in the test container) and open `/setup`. Verify:
1. Step 3 shows both toggles; mDNS is checked, MQTT unchecked, broker fields hidden.
2. Toggling MQTT on reveals the broker fields; off hides them.
3. Finishing the wizard then `GET /api/config` shows `Discovery.mDNS.Enabled` and `MQTT.Enabled` reflecting the toggles.

Command to fetch config after finishing:

```bash
curl -s http://localhost:3313/api/config | python -m json.tool | grep -A2 -E 'Discovery|"Enabled"'
```

Expected: `Discovery.mDNS.Enabled` present; `MQTT.Enabled` matches what you set.

- [ ] **Step 7: Commit**

```bash
git add gui/templates/setup.html gui/static/setup.js
git commit -m "feat(wizard): independent mDNS + MQTT toggles on the Home Assistant step"
```

---

## Task 9: Update in-repo docker-compose.yml reference + README deployment notes

**Files:**
- Modify: `docker-compose.yml`
- Modify: `README.md` (deployment/Home Assistant section)

- [ ] **Step 1: Add SPINSENSE_PORT to the reference compose**

In `docker-compose.yml`, in the `environment:` list (the one already containing `SPINSENSE_DATA_DIR=/app/data`), add:

```yaml
      - SPINSENSE_PORT=3313
```

If the file maps `ports: - "8000:8000"`, change it to `- "3313:3313"`. Add a comment above the service noting that mDNS Home Assistant discovery requires `network_mode: host` (and that under host mode the `ports:` mapping is dropped).

- [ ] **Step 2: Document the Home Assistant integration in README**

Add a "Home Assistant" section to `README.md` covering: install the SpinSense HACS integration (link to `ycsgc1/homeassistant-spinsense`), run the container with `network_mode: host`, the device is auto-discovered on `:3313`, and MQTT remains available as an alternative. Include the host-network test-stack compose from the spec's Deployment section.

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml README.md
git commit -m "docs: HA discovery deployment notes + 3313 in reference compose"
```

---

## Task 10: Full test sweep + branch verification

- [ ] **Step 1: Run the entire suite**

Run: `python -m pytest gui/tests core/tests -v`
Expected: all PASS, no errors.

- [ ] **Step 2: Import smoke-check the GUI app**

Run:

```bash
cd gui && python -c "import backend_main; print('app import ok')" && cd ..
```

Expected: `app import ok` (confirms discovery import + lifespan wiring load cleanly).

- [ ] **Step 3: Confirm the dependency installs / resolves**

Run: `python -c "import zeroconf; print(zeroconf.__version__)"`
Expected: prints a version (install `zeroconf` first if missing).

---

## Task 11 (separate deliverable): First-party HA integration repo

This is a **new repository**, not part of the app branch. Create it after the app changes land.

- [ ] **Step 1:** Create `ycsgc1/homeassistant-spinsense` (public). Seed it from `fwump38/SpinSense`'s root: copy `custom_components/spinsense/`, `hacs.json`, and the integration `README` into the new repo root.

- [ ] **Step 2:** In `custom_components/spinsense/manifest.json`, keep `"zeroconf": ["_spinsense._tcp.local."]`; add `ycsgc1` to `codeowners` (keep `fwump38` credited); repoint `documentation` and `issue_tracker` to the new repo.

- [ ] **Step 3:** In `custom_components/spinsense/const.py`, set `DEFAULT_PORT = 3313`.

- [ ] **Step 4:** Add an attribution line to the README crediting `fwump38` as the original author.

- [ ] **Step 5:** Verify against a running SpinSense (host-network, port 3313): in Home Assistant, the device should appear under Settings → Devices → Discovered; adding it creates a `media_player.turn_table` entity that reflects now-playing. Manual end-to-end check.

---

## Task 12 (user action): Dockge deployment

Hand these to the user; not code.

- [ ] **Step 1: Rescue existing main-stack history before any rebuild**

```bash
docker cp spinsense:/app/spinsense.db ./data/spinsense.db
docker cp spinsense:/app/art ./data/art   # if present
```

- [ ] **Step 2:** Update the main stack's compose to include `environment: [SPINSENSE_DATA_DIR=/app/data]` and `volumes: [./data:/app/data]` (persistence fix) — independent of mDNS.

- [ ] **Step 3:** For mDNS discovery, run the test stack with `network_mode: host`, `SPINSENSE_PORT=3313`, the data volume, and `SPINSENSE_DATA_DIR=/app/data` (full compose in the spec's Deployment section). Rebuild `--no-cache` to pull the feature branch.

- [ ] **Step 4:** Confirm Home Assistant discovers the device and the `media_player` entity tracks playback.

---

## Self-Review

**Spec coverage:**
- mDNS advertiser (spec §2/Components 1) → Task 7. ✓
- `GET /api/status` (Components 2) → Task 6. ✓
- Configurable port / 3313 (Components 3) → Task 1. ✓
- Wizard toggles (Components 4) → Task 8. ✓
- Config schema Discovery + MQTT.Enabled (Components 5) → Task 2. ✓
- Schema enrichment columns (Components 6) → Task 3 (+ engine populate Task 4, passthrough Task 5). ✓
- In-house integration repo (Components 7) → Task 11. ✓
- WS payload "no change" → confirmed; engine adds extra track keys (Task 4) which the integration ignores. ✓
- Deployment: volume fix + host network + DB rescue (spec Deployment) → Tasks 9, 12. ✓
- Error handling: mDNS non-fatal (Task 7 try/except), `/api/status` default (Task 6 + Task 5 DEFAULT_STATUS), nullable enrichment (Tasks 3/4). ✓
- Testing matrix (spec Testing) → Tasks 2,3,4,5,6,7,8 each carry tests. ✓
- `zeroconf` dependency → Task 1. ✓

**Placeholder scan:** No TBD/TODO in code steps. The one explicit deferral (engine re-reading `MQTT.Enabled` on live config change) is called out as a follow-up, consistent with the spec's "read at startup like other MQTT settings."

**Type/name consistency:** `manager.last_status` (Task 5) is read by `/api/status` (Task 6) and tested in both. `record_play(..., isrc=, genre=, release_year=)` keyword signature (Task 3) matches the call site (Task 5) and the engine-sourced track keys (Task 4). `advertiser` singleton + `start/stop/reconcile/build_service_info/is_enabled` (Task 7) match the tests and the backend wiring. Wizard ids `wizard-mdns-enabled` / `wizard-mqtt-enabled` / `wizard-mqtt-fields` match between HTML (Task 8 Step 1) and JS (Task 8 Steps 2-5).

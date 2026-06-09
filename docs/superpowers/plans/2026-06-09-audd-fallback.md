# AudD Fallback Recognizer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add AudD as a backup recognizer that fires on the first Shazam miss (reusing the first sample), behind a normalized-track refactor so backends are pluggable, with the album-art enrichment preserved exactly.

**Architecture:** Recognition is refactored so each backend produces a single **normalized track** dict (`title, artist, album?, art_url?, isrc?, genre?, release_year?`) that `_handle_match` consumes. `_identify_shazam` wraps Shazam; a new `_identify_audd` wraps AudD's HTTP API. In `recognize_audio`, an attempt-0 Shazam miss tries AudD on the *same* sample (gated on an enable toggle + API token); if AudD also misses, the existing Shazam 2×/3× escalation continues unchanged.

**Tech Stack:** Python 3.12 (`unittest`, `asyncio`, `aiohttp` — already a dependency), Pydantic config, vanilla JS + Jinja2 templates.

Reference spec: `docs/superpowers/specs/2026-06-09-audd-fallback-design.md`

---

### Task 1: Normalized-track refactor (no behavior change)

Split recognition so Shazam parsing lives in `_identify_shazam` (producing a normalized dict) and `_handle_match` consumes the normalized shape. Pure refactor — output behavior is identical; this just changes the internal interface and is the foundation for plugging in AudD.

**Files:**
- Modify: `core/core_engine.py` (`_identify` → `_identify_shazam` ~535-541; `_handle_match` ~544-590; the `recognize_audio` call site ~632)
- Test: `core/tests/test_recognition_phases.py`

**The normalized track shape** (a plain dict, used as the interface between backends and `_handle_match`):
```python
{"title": str, "artist": str, "album": str | None, "art_url": str | None,
 "isrc": str | None, "genre": str | None, "release_year": int | None}
```

- [ ] **Step 1: Write tests for the Shazam adapter + art precedence**

In `core/tests/test_recognition_phases.py`, add two new test classes (after `RecognizeRetryTest`):

```python
class ShazamNormalizeTest(unittest.TestCase):
    def setUp(self):
        self._orig = core_engine.shazam.recognize

    def tearDown(self):
        core_engine.shazam.recognize = self._orig

    def test_maps_shazam_dict_to_normalized(self):
        async def fake_recognize(_wav):
            return {"track": {
                "title": "Song", "subtitle": "Band",
                "images": {"coverarthq": "hq.jpg", "coverart": "lq.jpg"},
                "isrc": "US1234567890",
                "genres": {"primary": "Rock"},
                "sections": [{"metadata": [{"title": "Released", "text": "1979"}]}],
            }}
        core_engine.shazam.recognize = fake_recognize
        n = asyncio.run(core_engine._identify_shazam(b""))
        self.assertEqual(n["title"], "Song")
        self.assertEqual(n["artist"], "Band")
        self.assertEqual(n["art_url"], "hq.jpg")     # coverarthq preferred
        self.assertEqual(n["isrc"], "US1234567890")
        self.assertEqual(n["genre"], "Rock")
        self.assertEqual(n["release_year"], 1979)
        self.assertIsNone(n["album"])                # Shazam gives no reliable album

    def test_no_track_returns_none(self):
        async def fake_recognize(_wav):
            return {"matches": []}
        core_engine.shazam.recognize = fake_recognize
        self.assertIsNone(asyncio.run(core_engine._identify_shazam(b"")))


class HandleMatchArtTest(unittest.TestCase):
    def setUp(self):
        self.published = []
        async def fake_itunes(artist, title):
            return self.itunes_return
        async def fake_img(url):
            return "b64" if url else ""
        async def fake_phase(p):
            return None
        async def fake_blip():
            return None
        def fake_publish(status, artist="", title="", album="", art_url="", art_base64=""):
            self.published.append(dict(status=status, album=album, art_url=art_url))
        self._orig = (core_engine.fetch_itunes_metadata, core_engine.fetch_image_base64,
                      core_engine._publish_phase, core_engine._publish_idle_blip,
                      core_engine.publish_state)
        core_engine.fetch_itunes_metadata = fake_itunes
        core_engine.fetch_image_base64 = fake_img
        core_engine._publish_phase = fake_phase
        core_engine._publish_idle_blip = fake_blip
        core_engine.publish_state = fake_publish
        core_engine.state["last_song"] = ""
        self.itunes_return = (None, None)

    def tearDown(self):
        (core_engine.fetch_itunes_metadata, core_engine.fetch_image_base64,
         core_engine._publish_phase, core_engine._publish_idle_blip,
         core_engine.publish_state) = self._orig

    def test_itunes_art_is_primary(self):
        self.itunes_return = ("iTunes Album", "itunes_art.jpg")
        n = {"title": "T", "artist": "A", "album": "Backend Album",
             "art_url": "backend_art.jpg", "isrc": None, "genre": None, "release_year": None}
        asyncio.run(core_engine._handle_match(n))
        self.assertEqual(core_engine.state["art_url"], "itunes_art.jpg")
        self.assertEqual(core_engine.state["album"], "iTunes Album")

    def test_backend_art_used_when_itunes_has_none(self):
        self.itunes_return = (None, None)
        n = {"title": "T", "artist": "A", "album": "Backend Album",
             "art_url": "backend_art.jpg", "isrc": None, "genre": None, "release_year": None}
        asyncio.run(core_engine._handle_match(n))
        self.assertEqual(core_engine.state["art_url"], "backend_art.jpg")
        self.assertEqual(core_engine.state["album"], "Backend Album")

    def test_falls_back_to_unknown_album_and_empty_art(self):
        self.itunes_return = (None, None)
        n = {"title": "T", "artist": "A", "album": None,
             "art_url": None, "isrc": None, "genre": None, "release_year": None}
        asyncio.run(core_engine._handle_match(n))
        self.assertEqual(core_engine.state["art_url"], "")
        self.assertEqual(core_engine.state["album"], "Unknown Album")
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `python -m pytest core/tests/test_recognition_phases.py::ShazamNormalizeTest core/tests/test_recognition_phases.py::HandleMatchArtTest -v`
Expected: FAIL — `_identify_shazam` doesn't exist (`AttributeError`), and `_handle_match` reads `subtitle`/`images` (not the normalized keys) so the art assertions fail.

- [ ] **Step 3: Add `_identify_shazam` (replaces `_identify`)**

In `core/core_engine.py`, replace `_identify` (lines 535-541) with:

```python
async def _identify_shazam(wav_bytes: bytes) -> dict | None:
    """Recognize via Shazam; return a normalized track dict, or None on no match."""
    print("[!] Analyzing with Shazam...")
    out = await shazam.recognize(wav_bytes)
    if not (isinstance(out, dict) and 'track' in out):
        return None
    track = out['track'] or {}
    images = track.get('images', {}) if isinstance(track, dict) else {}
    enr = _extract_enrichment(track)
    return {
        "title": track.get('title', 'Unknown Title'),
        "artist": track.get('subtitle', 'Unknown Artist'),
        "album": None,  # Shazam has no reliable album; iTunes supplies it downstream
        "art_url": images.get('coverarthq') or images.get('coverart') or None,
        "isrc": enr["isrc"],
        "genre": enr["genre"],
        "release_year": enr["release_year"],
    }
```

(`_extract_enrichment` stays as-is and is now called only from here.)

- [ ] **Step 4: Refactor `_handle_match` to consume the normalized shape**

In `core/core_engine.py`, replace the head of `_handle_match` (lines 544-570) — through the enrichment assignments — with:

```python
async def _handle_match(track: dict) -> None:
    """Enrich, publish, and record a matched track. `track` is the NORMALIZED shape
    produced by a backend (_identify_shazam / _identify_audd)."""
    title = track.get('title') or 'Unknown Title'
    artist = track.get('artist') or 'Unknown Artist'

    print("[!] Fetching high-res metadata from iTunes...")
    album, art_url = await fetch_itunes_metadata(artist, title)
    if not art_url:
        art_url = track.get('art_url') or ''   # backend-supplied fallback art
    if not album:
        album = track.get('album') or "Unknown Album"

    art_base64 = ""
    if art_url:
        print("[!] Encoding album art to Base64 for Home Assistant...")
        art_base64 = await fetch_image_base64(art_url)

    result_str = f"{artist} - {title}"
    state["artist"] = artist
    state["title"] = title
    state["album"] = album
    state["art_url"] = art_url
    state["isrc"] = track.get('isrc')
    state["genre"] = track.get('genre')
    state["release_year"] = track.get('release_year')
```

Leave everything from `if result_str != state["last_song"]:` onward (lines 572-590) unchanged.

- [ ] **Step 5: Update the `recognize_audio` call site**

In `core/core_engine.py`, in `recognize_audio` change `track = await _identify(wav)` (line 632) to:

```python
        track = await _identify_shazam(wav)
```

- [ ] **Step 6: Update the existing tests to the new names/shapes**

In `core/tests/test_recognition_phases.py`, `RecognizeRetryTest`: replace every `core_engine._identify` with `core_engine._identify_shazam` (in the `_orig` tuple at setUp, the two assignments, and the tearDown tuple — 4 occurrences).

In `IdleBlipTest`, the two `_handle_match` calls pass Shazam-shaped dicts; change the `"subtitle"` key to `"artist"`:
- `core_engine._handle_match({"title": "T", "subtitle": "A"})` → `({"title": "T", "artist": "A"})`
- `core_engine._handle_match({"title": "T2", "subtitle": "A"})` → `({"title": "T2", "artist": "A"})`

- [ ] **Step 7: Run the full recognition test file**

Run: `python -m pytest core/tests/test_recognition_phases.py -v`
Expected: PASS (all classes — the renamed/updated existing tests plus the two new ones).

- [ ] **Step 8: Commit**

```bash
git add core/core_engine.py core/tests/test_recognition_phases.py
git commit -m "refactor(audio): normalized track shape behind backend adapters"
```

---

### Task 2: Config keys for the fallback

Add `Fallback_Enabled` (default off) and `AudD_API_Token` (default empty) to both default tables + the runtime mirror.

**Files:**
- Test: `gui/tests/test_config_round_trip.py`
- Modify: `gui/config_manager.py` (`AudioConfig`), `core/core_engine.py` (`DEFAULT_CONFIG`, `runtime`, `_populate_runtime`)

- [ ] **Step 1: Write failing config tests**

In `gui/tests/test_config_round_trip.py`, add to `ConfigRoundTripTest` (after `test_rescan_wait_interval_default_and_round_trips`):

```python
    def test_fallback_defaults_off_and_empty_token(self):
        defaults = config_manager.get_default_config()
        self.assertEqual(defaults["Audio"]["Fallback_Enabled"], False)
        self.assertEqual(defaults["Audio"]["AudD_API_Token"], "")

    def test_fallback_settings_round_trip(self):
        cfg = config_manager.get_default_config()
        cfg["Audio"]["Fallback_Enabled"] = True
        cfg["Audio"]["AudD_API_Token"] = "tok_abc123"
        self.assertTrue(config_manager.save_config(cfg))
        loaded = config_manager.load_config()
        self.assertEqual(loaded["Audio"]["Fallback_Enabled"], True)
        self.assertEqual(loaded["Audio"]["AudD_API_Token"], "tok_abc123")
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest gui/tests/test_config_round_trip.py -v`
Expected: FAIL — `KeyError: 'Fallback_Enabled'`.

- [ ] **Step 3: Add the keys to the Pydantic model**

In `gui/config_manager.py`, add two fields to `AudioConfig` (after `Retrigger_On_Track_Change`):

```python
    Fallback_Enabled: bool = False
    AudD_API_Token: str = ""
```

- [ ] **Step 4: Run to verify the config tests pass**

Run: `python -m pytest gui/tests/test_config_round_trip.py -v`
Expected: PASS.

- [ ] **Step 5: Add the keys to the engine defaults + runtime**

In `core/core_engine.py` `DEFAULT_CONFIG["Audio"]`, add (after `Rescan_Wait_Interval`):
```python
        "Fallback_Enabled": False,
        "AudD_API_Token": "",
```

In the `runtime` dict, add (after `"rescan_wait": 5.0,`):
```python
    "fallback_enabled": False,
    "audd_token": "",
```

In `_populate_runtime`, add (after the `rescan_wait` line):
```python
    runtime["fallback_enabled"] = cfg.get('Audio', {}).get('Fallback_Enabled', False)
    runtime["audd_token"]       = cfg.get('Audio', {}).get('AudD_API_Token', '')
```

- [ ] **Step 6: Verify the engine imports**

Run: `python -c "import sys; sys.path.insert(0,'core'); import core_engine; print(core_engine.runtime['fallback_enabled'], repr(core_engine.runtime['audd_token']))"`
Expected: prints `False ''` (or the on-disk config values) with no exception.

- [ ] **Step 7: Commit**

```bash
git add gui/tests/test_config_round_trip.py gui/config_manager.py core/core_engine.py
git commit -m "feat(config): add Fallback_Enabled + AudD_API_Token (default off)"
```

---

### Task 3: AudD adapter (`_identify_audd`)

Add the AudD HTTP call + the pure response→normalized mapping. The network call is isolated in `_audd_post` so the flow is testable without mocking aiohttp internals.

**Files:**
- Modify: `core/core_engine.py` (add near `_identify_shazam`)
- Test: `core/tests/test_recognition_phases.py`

- [ ] **Step 1: Write tests for the mapping + adapter**

In `core/tests/test_recognition_phases.py`, add a test class:

```python
class AuddAdapterTest(unittest.TestCase):
    def setUp(self):
        self._orig_post = core_engine._audd_post
        self._orig_token = core_engine.runtime["audd_token"]
        core_engine.runtime["audd_token"] = "tok"

    def tearDown(self):
        core_engine._audd_post = self._orig_post
        core_engine.runtime["audd_token"] = self._orig_token

    def test_maps_audd_result_to_normalized(self):
        result = {
            "artist": "Band", "title": "Song", "album": "The Album",
            "release_date": "1979-10-12",
            "apple_music": {
                "genreNames": ["Rock", "Pop"], "isrc": "US1234567890",
                "artwork": {"url": "https://art/{w}x{h}bb.jpg"},
            },
        }
        n = core_engine._audd_to_normalized(result)
        self.assertEqual(n["title"], "Song")
        self.assertEqual(n["artist"], "Band")
        self.assertEqual(n["album"], "The Album")
        self.assertEqual(n["release_year"], 1979)
        self.assertEqual(n["isrc"], "US1234567890")
        self.assertEqual(n["genre"], "Rock")
        self.assertEqual(n["art_url"], "https://art/600x600bb.jpg")  # {w}/{h} resolved

    def test_maps_missing_fields_to_none(self):
        n = core_engine._audd_to_normalized({"artist": "A", "title": "T"})
        self.assertIsNone(n["album"])
        self.assertIsNone(n["isrc"])
        self.assertIsNone(n["genre"])
        self.assertIsNone(n["release_year"])
        self.assertIsNone(n["art_url"])

    def test_identify_success(self):
        async def fake_post(_wav, _token):
            return {"status": "success", "result": {"artist": "A", "title": "T"}}
        core_engine._audd_post = fake_post
        n = asyncio.run(core_engine._identify_audd(b""))
        self.assertEqual(n["title"], "T")

    def test_identify_no_match_returns_none(self):
        async def fake_post(_wav, _token):
            return {"status": "success", "result": None}
        core_engine._audd_post = fake_post
        self.assertIsNone(asyncio.run(core_engine._identify_audd(b"")))

    def test_identify_network_error_returns_none(self):
        async def fake_post(_wav, _token):
            return None  # _audd_post swallows errors -> None
        core_engine._audd_post = fake_post
        self.assertIsNone(asyncio.run(core_engine._identify_audd(b"")))

    def test_identify_no_token_skips_post(self):
        core_engine.runtime["audd_token"] = ""
        self.called = False
        async def fake_post(_wav, _token):
            self.called = True
            return None
        core_engine._audd_post = fake_post
        self.assertIsNone(asyncio.run(core_engine._identify_audd(b"")))
        self.assertFalse(self.called)
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest core/tests/test_recognition_phases.py::AuddAdapterTest -v`
Expected: FAIL — `_audd_to_normalized` / `_identify_audd` / `_audd_post` don't exist.

- [ ] **Step 3: Implement the adapter**

In `core/core_engine.py`, add directly after `_identify_shazam`:

```python
def _audd_to_normalized(result: dict) -> dict:
    """Pure: map an AudD `result` object to the normalized track shape."""
    result = result or {}
    am = result.get("apple_music") or {}
    sp = result.get("spotify") or {}

    release_year = None
    rd = str(result.get("release_date") or "")
    if len(rd) >= 4 and rd[:4].isdigit():
        release_year = int(rd[:4])

    genre = None
    genres = am.get("genreNames")
    if isinstance(genres, list) and genres:
        genre = genres[0] or None

    isrc = am.get("isrc") or result.get("isrc") or None

    # Fallback art only (iTunes is primary downstream): resolve Apple's {w}x{h}
    # artwork template, else a Spotify album image.
    art_url = None
    art = am.get("artwork")
    if isinstance(art, dict) and art.get("url"):
        art_url = str(art["url"]).replace("{w}", "600").replace("{h}", "600")
    elif isinstance(sp.get("album"), dict):
        imgs = sp["album"].get("images")
        if isinstance(imgs, list) and imgs and isinstance(imgs[0], dict):
            art_url = imgs[0].get("url") or None

    return {
        "title": result.get("title", "Unknown Title"),
        "artist": result.get("artist", "Unknown Artist"),
        "album": result.get("album") or None,
        "art_url": art_url,
        "isrc": isrc,
        "genre": genre,
        "release_year": release_year,
    }


async def _audd_post(wav_bytes: bytes, token: str) -> dict | None:
    """POST the sample to AudD; return the parsed JSON body, or None on any
    HTTP/timeout/parse error. Isolated so the recognize-flow tests can stub it."""
    try:
        data = aiohttp.FormData()
        data.add_field("api_token", token)
        data.add_field("return", "apple_music,spotify")
        data.add_field("file", wav_bytes, filename="sample.wav", content_type="audio/wav")
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post("https://api.audd.io/", data=data) as resp:
                if resp.status != 200:
                    print(f"⚠️ AudD HTTP {resp.status}")
                    return None
                return await resp.json(content_type=None)
    except Exception as e:
        print(f"⚠️ AudD request failed: {e}")
        return None


async def _identify_audd(wav_bytes: bytes) -> dict | None:
    """Recognize via AudD; return a normalized track dict, or None. No-ops without
    a configured token. Any error is treated as a clean miss."""
    token = runtime["audd_token"]
    if not token:
        return None
    print("[!] Trying AudD fallback...")
    body = await _audd_post(wav_bytes, token)
    if not isinstance(body, dict) or body.get("status") != "success":
        return None
    result = body.get("result")
    if not result:
        return None
    return _audd_to_normalized(result)
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest core/tests/test_recognition_phases.py::AuddAdapterTest -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add core/core_engine.py core/tests/test_recognition_phases.py
git commit -m "feat(audio): AudD recognition adapter (HTTP + normalized mapping)"
```

---

### Task 4: Wire AudD into `recognize_audio`

On an attempt-0 Shazam miss, try AudD with the same sample, gated on `fallback_enabled` + a token.

**Files:**
- Modify: `core/core_engine.py` (`recognize_audio` loop ~625-634)
- Test: `core/tests/test_recognition_phases.py`

- [ ] **Step 1: Write the flow tests**

In `core/tests/test_recognition_phases.py`, add:

```python
class AuddFallbackFlowTest(unittest.TestCase):
    def setUp(self):
        self.handled = []
        self.audd_calls = 0
        self.shazam_calls = 0
        async def fake_phase(p):
            return None
        async def fake_capture(sample_len):
            return b""
        async def fake_pause(seconds):
            return None
        async def fake_handle(track):
            self.handled.append(track)
            core_engine.state["in_song"] = True
            core_engine.state["back_off"] = False
        self._orig = (core_engine._publish_phase, core_engine._capture_sample,
                      core_engine._rescan_pause, core_engine._handle_match,
                      core_engine._identify_shazam, core_engine._identify_audd)
        self._orig_flags = (core_engine.runtime["fallback_enabled"],
                            core_engine.runtime["audd_token"],
                            core_engine.runtime["rescan_wait"])
        core_engine._publish_phase = fake_phase
        core_engine._capture_sample = fake_capture
        core_engine._rescan_pause = fake_pause
        core_engine._handle_match = fake_handle
        core_engine.runtime["rescan_wait"] = 0
        core_engine.state["back_off"] = False
        core_engine.state["in_song"] = False

    def tearDown(self):
        (core_engine._publish_phase, core_engine._capture_sample,
         core_engine._rescan_pause, core_engine._handle_match,
         core_engine._identify_shazam, core_engine._identify_audd) = self._orig
        (core_engine.runtime["fallback_enabled"], core_engine.runtime["audd_token"],
         core_engine.runtime["rescan_wait"]) = self._orig_flags

    def _set(self, shazam_fn, audd_fn, enabled=True, token="tok"):
        core_engine._identify_shazam = shazam_fn
        core_engine._identify_audd = audd_fn
        core_engine.runtime["fallback_enabled"] = enabled
        core_engine.runtime["audd_token"] = token

    def test_audd_rescues_after_first_shazam_miss(self):
        async def shazam(_w):
            self.shazam_calls += 1
            return None
        async def audd(_w):
            self.audd_calls += 1
            return {"title": "AudD Hit", "artist": "A"}
        self._set(shazam, audd)
        asyncio.run(core_engine.recognize_audio())
        self.assertEqual(self.handled, [{"title": "AudD Hit", "artist": "A"}])
        self.assertEqual(self.shazam_calls, 1)   # broke out after attempt 0
        self.assertEqual(self.audd_calls, 1)
        self.assertFalse(core_engine.state["back_off"])

    def test_audd_miss_continues_shazam_escalation(self):
        async def shazam(_w):
            self.shazam_calls += 1
            return None
        async def audd(_w):
            self.audd_calls += 1
            return None
        self._set(shazam, audd)
        asyncio.run(core_engine.recognize_audio())
        self.assertEqual(self.handled, [])
        self.assertEqual(self.shazam_calls, 3)   # full ladder ran
        self.assertEqual(self.audd_calls, 1)     # AudD only on attempt 0
        self.assertTrue(core_engine.state["back_off"])

    def test_disabled_never_calls_audd(self):
        async def shazam(_w):
            self.shazam_calls += 1
            return None
        async def audd(_w):
            self.audd_calls += 1
            return {"title": "X", "artist": "Y"}
        self._set(shazam, audd, enabled=False)
        asyncio.run(core_engine.recognize_audio())
        self.assertEqual(self.audd_calls, 0)
        self.assertEqual(self.handled, [])

    def test_no_token_never_calls_audd(self):
        async def shazam(_w):
            self.shazam_calls += 1
            return None
        async def audd(_w):
            self.audd_calls += 1
            return {"title": "X", "artist": "Y"}
        self._set(shazam, audd, enabled=True, token="")
        asyncio.run(core_engine.recognize_audio())
        self.assertEqual(self.audd_calls, 0)
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest core/tests/test_recognition_phases.py::AuddFallbackFlowTest -v`
Expected: FAIL — AudD is never called yet, so `test_audd_rescues_after_first_shazam_miss` fails (no match handled, `audd_calls == 0`).

- [ ] **Step 3: Wire the fallback into the loop**

In `core/core_engine.py` `recognize_audio`, replace the identify line + break (currently `track = await _identify_shazam(wav)` then `if track: break`) with:

```python
        track = await _identify_shazam(wav)
        if (track is None and attempt == 0
                and runtime["fallback_enabled"] and runtime["audd_token"]):
            track = await _identify_audd(wav)  # reuse the first sample
        if track:
            break
```

- [ ] **Step 4: Run the full file to verify pass**

Run: `python -m pytest core/tests/test_recognition_phases.py -v`
Expected: PASS — `AuddFallbackFlowTest` plus all prior classes (the existing `RecognizeRetryTest` still passes because `fallback_enabled` defaults False in those tests' runtime).

> Note: `RecognizeRetryTest` doesn't set `fallback_enabled`, so it uses whatever the module loaded (default `False`) — AudD stays out of its path. If that test is flaky due to global runtime state, set `core_engine.runtime["fallback_enabled"] = False` in its `setUp`.

- [ ] **Step 5: Commit**

```bash
git add core/core_engine.py core/tests/test_recognition_phases.py
git commit -m "feat(audio): try AudD on first Shazam miss, reusing the first sample"
```

---

### Task 5: Settings UI — enable toggle + API token field

**Files:**
- Modify: `gui/templates/settings.html` (Audio section, after the re-announce toggle ~line 74)

- [ ] **Step 1: Add the fallback block**

In `gui/templates/settings.html`, immediately after the closing `</label>` of the "Re-announce each track to Home Assistant" toggle block (the one whose input is `name="Audio.Retrigger_On_Track_Change"`) and before the `</div>` that closes `class="flex flex-col gap-lg"`, insert:

```html
        <label class="flex items-center justify-between gap-md cursor-pointer">
          <span class="text-body-sm text-on-surface">Use AudD as a backup recognizer<span class="help-tip" tabindex="0" role="note" aria-label="Help: AudD backup recognizer"><span class="material-symbols-outlined help-icon">help</span><span class="help-bubble" role="tooltip">When Shazam can't identify a track, SpinSense retries the same sample against AudD (a second online music database) before giving up. Off by default. Requires an AudD API token below.</span></span></span>
          <input type="checkbox" name="Audio.Fallback_Enabled" class="sr-only peer">
          <span class="relative shrink-0 w-11 h-6 rounded-full bg-surface-container-highest border border-outline-variant/40 peer-checked:bg-primary peer-focus-visible:ring-2 peer-focus-visible:ring-primary transition-colors after:content-[''] after:absolute after:top-0.5 after:left-0.5 after:w-5 after:h-5 after:rounded-full after:bg-on-surface after:transition-transform peer-checked:after:translate-x-5"></span>
        </label>

        <label class="block">
          <span class="text-body-sm text-on-surface mb-1 block">AudD API token<span class="help-tip" tabindex="0" role="note" aria-label="Help: AudD API token"><span class="material-symbols-outlined help-icon">help</span><span class="help-bubble" role="tooltip">Your API token from audd.io, used only for the backup lookup above. Stored in config.json in plaintext (like the MQTT password) — fine for a self-hosted LAN box. Leave blank to disable the fallback.</span></span></span>
          <input type="password" name="Audio.AudD_API_Token" autocomplete="off" class="form-input">
        </label>
```

- [ ] **Step 2: Verify the fields are present + named correctly**

Run: `grep -n "Audio.Fallback_Enabled\|Audio.AudD_API_Token" gui/templates/settings.html`
Expected: exactly two matches (the checkbox and the password input).

> Manual check (if running the app): load `/settings`, confirm the toggle + token field render under the Audio section, editing either enables Save, and a save round-trips (reload shows the saved values; `settings.js` auto-binds any `[name]` input, so no JS change is needed).

- [ ] **Step 3: Commit**

```bash
git add gui/templates/settings.html
git commit -m "feat(settings): AudD fallback toggle + API token field"
```

---

### Task 6: Changelog

**Files:**
- Modify: `CHANGELOG.md` (add a new `## [Unreleased]` section at the top, above the latest released version)

- [ ] **Step 1: Add the Unreleased entry**

In `CHANGELOG.md`, insert after the intro paragraph and before the first `## [x.y.z.w]` heading:

```markdown
## [Unreleased]

### Added
- **AudD backup recognizer.** When Shazam can't identify a track on its first try, SpinSense now retries the *same* sample against [AudD](https://audd.io) before falling back to its longer-sample retries — improving the hard-to-identify tail. Opt-in: enable it and paste an API token in Settings (off by default; nothing changes until configured). Album art is still sourced iTunes-first, so artwork quality is unchanged.

### Changed
- Recognition is refactored behind a normalized track shape so backends (Shazam, AudD, future ones) are pluggable. No behavior change to the Shazam path.
```

- [ ] **Step 2: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: changelog for AudD fallback recognizer"
```

---

### Final verification

- [ ] **Run all affected suites**

Run: `python -m pytest core/tests/test_recognition_phases.py gui/tests/test_config_round_trip.py -v`
Expected: all PASS.

Run (full, from each dir to avoid the cross-package collection collision): `python -m pytest core/tests/ -q` and `cd gui && python -m pytest tests/ -q`
Expected: all PASS.

---

## Notes for the implementer

- **`aiohttp` is already a dependency** (used for iTunes + art). No new packages.
- **Testability pattern:** the codebase tests recognition by swapping module-level functions on `core_engine` (e.g. `core_engine._identify_shazam = fake`). `_audd_post` exists purely so the HTTP boundary can be stubbed the same way — don't inline it back into `_identify_audd`.
- **Art precedence is load-bearing** (the user explicitly called it out): iTunes-first, backend `art_url` fallback, then `""`. The `HandleMatchArtTest` cases lock this in — don't weaken them.
- **Global runtime state:** tests mutate `core_engine.runtime[...]`; always restore in `tearDown` (the provided tests do). The fallback is gated on `fallback_enabled and audd_token`, so a stray enabled flag left in global state could leak across tests — restore it.
- **Out of scope:** no new dashboard "phase" for AudD (it runs within the existing `identifying` window); no escalation of AudD (first sample only).

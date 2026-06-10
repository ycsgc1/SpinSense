# Recognition source menu (AcoustID) + back-off gate fix

**Date:** 2026-06-09
**Status:** Approved (design)
**Target version:** 1.5.0.5

Two related pieces of recognition-state-machine work, shipped together:
1. A **source menu** — Shazam stays the always-on primary; one selectable backup (None / AudD / AcoustID), with AcoustID added as a free, no-subscription option.
2. A **fix for the back-off rescan loop** — an unidentifiable track that keeps playing re-scans forever.

---

## Part A — Recognition source menu + AcoustID

### Problem / goals

AudD (added in 1.5.0.0) is good but its pricing (300 free/user then subscription) isn't friendly to distribute. AcoustID is genuinely free for non-commercial use with an embeddable app key — no per-user signup. We want users to **pick** their backup recognizer from a small menu, with Shazam always primary and the default being no fallback.

- Shazam remains the always-on primary (free, default, no config).
- One selectable fallback: `none` (default) | `audd` | `acoustid`.
- AcoustID embedded with a single SpinSense application key (no user signup).
- Reuse the normalized-adapter architecture from 1.5.0.0 — adding AcoustID is "one more adapter."

**Non-goals:** chaining multiple fallbacks / priority ordering; escalating the fallback (still fires once, on the first sample); per-user AcoustID keys.

### A1. Config — provider selector replaces the boolean

Replace `Audio.Fallback_Enabled` (bool, from 1.5.0.0) with:

- `Audio.Fallback_Provider: Literal["none", "audd", "acoustid"] = "none"`

`Audio.AudD_API_Token` (str) stays — used only when the provider is `audd`. A stale `Fallback_Enabled` left in an existing `config.json` is harmless (Pydantic ignores unknown fields; the provider defaults to `none`). Mirror into `runtime["fallback_provider"]` (replacing `runtime["fallback_enabled"]`).

### A2. Dispatch (the menu mechanism)

A single dispatcher routes the attempt-0 fallback:

```python
async def _identify_fallback(wav_bytes: bytes) -> dict | None:
    provider = runtime["fallback_provider"]
    if provider == "audd":
        return await _identify_audd(wav_bytes)
    if provider == "acoustid":
        return await _identify_acoustid(wav_bytes)
    return None
```

In `recognize_audio`, the attempt-0 Shazam-miss branch becomes:

```python
        track = await _identify_shazam(wav)
        if track is None and attempt == 0:
            track = await _identify_fallback(wav)  # reuse first sample
        if track:
            break
```

Same "fires once, on the first sample; if it misses, Shazam escalation continues" behavior — just provider-routed. `_identify_audd` already self-gates on an empty token (returns None), and `_identify_acoustid` self-gates on a missing key, so `none`/unconfigured is always safe.

### A3. AcoustID adapter

AcoustID needs a **Chromaprint fingerprint**, not raw audio. Three isolated, testable pieces (mirroring the AudD split so the recognize-flow tests can stub each):

- `_chromaprint_fingerprint(wav_bytes) -> tuple[int, str] | None` — writes the WAV to a `tempfile.NamedTemporaryFile(suffix=".wav")`, runs `fpcalc -json <file>` via `subprocess.run(..., timeout=15)` inside `asyncio.to_thread`, parses `{"duration": float, "fingerprint": str}` → returns `(int(round(duration)), fingerprint)`. Missing binary (`FileNotFoundError`), non-zero exit, timeout, or parse error → `None` (logged).
- `_acoustid_lookup(duration, fingerprint) -> dict | None` — POST (form-encoded, to avoid URL-length limits on the long fingerprint) to `https://api.acoustid.org/v2/lookup` with `client=ACOUSTID_CLIENT_KEY`, `duration`, `fingerprint`, `meta=recordings+releasegroups`, `format=json`, via the existing `aiohttp` (timeout 10s). Any HTTP/timeout/parse error → `None`.
- `_acoustid_to_normalized(results) -> dict | None` — pure: pick the highest-`score` result; from its first `recordings[0]` take `title`, artist = `", ".join(a["name"] for a in recordings[0].get("artists", []))` (all credited artists; empty → `"Unknown Artist"`), album = `recordings[0].releasegroups[0].title` if present. Returns `None` if there's no usable recording with a title. `art_url`/`isrc`/`genre`/`release_year` stay `None` — **iTunes enrichment fills art + album downstream by artist+title, so art behavior is unchanged.**

Orchestrator:

```python
async def _identify_acoustid(wav_bytes: bytes) -> dict | None:
    if not ACOUSTID_CLIENT_KEY:
        return None
    fp = await _chromaprint_fingerprint(wav_bytes)
    if not fp:
        return None
    duration, fingerprint = fp
    print("[!] Trying AcoustID fallback...")
    body = await _acoustid_lookup(duration, fingerprint)
    if not isinstance(body, dict) or body.get("status") != "ok":
        return None
    results = body.get("results") or []
    if not results:
        return None
    return _acoustid_to_normalized(results)
```

**Embedded key:** `ACOUSTID_CLIENT_KEY = os.environ.get("SPINSENSE_ACOUSTID_KEY", "UGhMOSOjGb")` — baked-in default (the registered SpinSense app key), overridable via env. Empty → AcoustID no-ops gracefully.

**Docker dependency:** add `libchromaprint-tools` (provides `fpcalc`) to the `apt-get install` line in `docker/Dockerfile`. If `fpcalc` is absent at runtime (e.g. local dev), `_chromaprint_fingerprint` returns `None` and AcoustID simply yields no match — never crashes.

### A4. Settings UI

Replace the AudD enable toggle with a `<select name="Audio.Fallback_Provider">` — **Backup recognizer**: `None` / `AudD` / `AcoustID` — with a help tooltip. Keep the `AudD_API_Token` password field below it, with a caption noting it's only needed for the AudD option. `settings.js` binds `[name]` generically (a `<select>`'s `.value` is read/written by the existing else-branch), but its dirty-tracking `change` handler only fires for checkboxes today — broaden it to also set dirty for `<select>` (`ev.target.tagName === "SELECT"`) so changing the dropdown enables Save.

---

## Part B — Back-off gate fix (rescan loop)

### Root cause (confirmed by code trace)

When recognition fails, `_clear_track_state(set_backoff=True)` sets `back_off=True`, `in_song=False`. The monitor loop then resets `state["current_rms"] = 0.0` after every scan ([core_engine.py:846](core/core_engine.py#L846), and `:832` for manual rescan). On the **next tick**, `vol == 0.0` → `_scan_decision` returns `"silence"` → the silence branch runs `state["back_off"] = False` **unconditionally after a single tick** ([:852-853](core/core_engine.py#L852-L853)). The **following** tick sees the refreshed (loud) RMS and `not in_song` → returns `"scan"` → `recognize_audio()` runs again. → endless rescan loop for any track that fails to ID while it keeps playing.

Two compounding flaws: back-off clears on **one** silence tick (not a qualifying gap), and the post-scan `current_rms = 0.0` reset **guarantees** such a tick after every scan. Pre-existing since the back-off mechanism (1.1.0); the escalating retries just made the loop longer and more visible.

### The fix

Clear `back_off` only after a **qualifying silence gap** (`>= new_song_silence`), consistent with the in-song dip gate — so a momentary dip *or* the phantom post-scan zero (both single ticks) can't defeat it; only a real between-song gap re-arms scanning. Extract the silence transition into a pure, unit-testable helper (mirroring `_scan_decision`):

```python
def _silence_step(silence_counter, in_song, back_off, new_song_silence, stopped_silence):
    """Pure: process one silence tick. Returns (silence_counter, back_off, stop).
    `stop` True => clear the track and publish 'stopped'.

    back_off clears only after a *qualifying* gap (>= new_song_silence), so a
    momentary dip or the phantom zero-RMS tick injected right after a scan can't
    re-arm scanning on an unidentifiable track that keeps playing."""
    silence_counter += 1
    if back_off and silence_counter >= new_song_silence:
        back_off = False
    stop = in_song and silence_counter >= stopped_silence
    return silence_counter, back_off, stop
```

The silence branch becomes:

```python
        else:  # silence
            print("s", end="", flush=True)
            new_sc, new_bo, stop = _silence_step(
                state["silence_counter"], state["in_song"], state.get("back_off", False),
                runtime["new_song_silence"], runtime["stopped_silence"],
            )
            state["silence_counter"] = new_sc
            state["back_off"] = new_bo
            if stop:
                print(f"\n[ STOPPED ] {runtime['stopped_silence']}s silence limit reached.")
                publish_state("stopped")
                _clear_track_state(set_backoff=False)
                state["silence_counter"] = 0
```

Note `silence_counter` now increments on **every** silence tick (previously only while `in_song`). This is safe: when `not in_song`, `_scan_decision` short-circuits on `not in_song` before reading `silence_counter`, and `recognize_audio` resets it to 0 at the end of every scan, and the `tick` branch resets it to 0 — so the counter can't leak stale values into a scan decision. The `current_rms = 0.0` resets are left as-is (the gate now neutralizes the phantom tick: 1 < `new_song_silence`).

### Behavior after the fix

- Unidentifiable track that keeps playing: after no_match, the phantom zero is one silence tick (1 < 3) → back_off stays True → loud ticks return `"wait_gap"` (`b`), **no rescan**. Only a real ≥`new_song_silence` gap clears back_off, then the next onset scans the next song.
- Successful match path is unchanged (the existing dip gate already absorbs the phantom zero via `tick`).
- A genuine between-song gap still re-arms scanning exactly as intended.

---

## Testing

**Part A:**
- Config round-trip: `Fallback_Provider` default `"none"`; accepts `"audd"`/`"acoustid"`; rejects an unknown value (Pydantic `Literal`); `AudD_API_Token` still round-trips.
- `_acoustid_to_normalized`: best-score pick, title/artist/album mapping, artist join, no-recording → None.
- `_identify_acoustid` (stub `_chromaprint_fingerprint` + `_acoustid_lookup`): hit → normalized; `status != ok` / empty results / fp-None / lookup-None → None; empty `ACOUSTID_CLIENT_KEY` → None without calling fingerprint.
- `_identify_fallback` routing: `none` → None (neither adapter called); `audd` → `_identify_audd`; `acoustid` → `_identify_acoustid`.
- Flow: provider `acoustid`, Shazam miss on attempt 0 → AcoustID hit → handled, no escalation; AcoustID miss → Shazam escalation continues.

**Part B:**
- `_silence_step`: back_off True + counter below `new_song_silence` → back_off stays True (the regression test for the loop, incl. the phantom-zero single tick); counter reaching `new_song_silence` → back_off clears; `in_song` + counter reaching `stopped_silence` → `stop=True`; not in_song never stops.

## Files touched (anticipated)

- `core/core_engine.py` — `Fallback_Provider` in `DEFAULT_CONFIG`/runtime/`_populate_runtime` (replacing `Fallback_Enabled`); `_identify_fallback`; AcoustID adapter trio + `ACOUSTID_CLIENT_KEY`; `recognize_audio` dispatch; `_silence_step` + silence branch.
- `gui/config_manager.py` — `AudioConfig`: `Fallback_Provider` Literal (replace `Fallback_Enabled`).
- `gui/templates/settings.html` — provider `<select>` (replace toggle) + AudD token caption.
- `gui/static/settings.js` — broaden dirty-tracking to include `<select>`.
- `docker/Dockerfile` — add `libchromaprint-tools`.
- `core/tests/test_recognition_phases.py`, `gui/tests/test_config_round_trip.py` — new tests, **plus updating the 1.5.0.0 fallback tests** that reference the removed names: `AuddFallbackFlowTest` (uses `runtime["fallback_enabled"]` → switch to `runtime["fallback_provider"] = "audd"`/`"none"`) and the config tests `test_fallback_defaults_off_and_empty_token` / `test_fallback_settings_round_trip` (assert `Fallback_Enabled` → switch to `Fallback_Provider`).
- `CHANGELOG.md`, `VERSION` (1.5.0.5).

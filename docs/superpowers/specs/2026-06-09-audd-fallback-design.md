# AudD fallback recognizer + normalized track refactor

**Date:** 2026-06-09
**Status:** Approved (design)

## Problem

Shazam (via the unofficial `shazamio`) identifies ~98% of tracks, but a repeatable
~2% — typically sparse/ambient, heavily compressed, or obscure pressings — never get
a good match, even with the existing 1×/2×/3× escalating-sample retry ladder. These
same tracks are also slow/hard on phone Shazam, suggesting inherent fingerprint
difficulty rather than a capture-level problem. A second online recognizer with a
different catalog is the most effective lever for that tail.

## Goals

- Add **AudD** (`https://api.audd.io/`) as a backup recognizer.
- On the **first** Shazam miss, immediately try AudD with **that same first sample**
  (no re-record). If AudD also misses, fall back into Shazam's existing 2×/3×
  escalation. AudD fires **once**, on the first sample only.
- Opt-in: off by default, gated on an enable toggle **and** a configured API token.
  When disabled/unconfigured, recognition behavior is byte-for-byte unchanged.
- Refactor recognition behind a **normalized track** shape so backends are pluggable.
- **Preserve the album-art enrichment** exactly (iTunes-first, backend-art fallback).

## Non-goals

- Local/offline recognition (separate future idea).
- Escalating AudD (it runs only on sample₁; the longer-sample retries stay Shazam-only).
- Parallel Shazam+AudD calls (sequential: Shazam first, AudD on miss).
- Any change to the silence/rescan/escalation timing logic.

---

## Design

### 1. Recognition flow (`recognize_audio`)

```
capture sample₁ (base length)
 ├─ Shazam(sample₁) ── hit ─→ _handle_match ✅
 └─ miss → fallback enabled AND token set?
            ├─ AudD(sample₁) ── hit ─→ _handle_match ✅
            └─ miss / disabled / error → Shazam escalation (unchanged):
                 Shazam(sample₂ = 2×) ── hit ─→ ✅
                 Shazam(sample₃ = 3×) ── hit ─→ ✅
                 all miss → _clear_track_state(set_backoff=True) + no_match (unchanged)
```

Concretely, inside the existing `for attempt in range(RECOGNIZE_ATTEMPTS)` loop: on
`attempt == 0`, if Shazam returns `None` and the fallback is active, call
`_identify_audd(wav)` with the **already-captured** sample₁ bytes before proceeding to
`attempt 1`. The escalation/`_rescan_pause`/`_MAX_SAMPLE_SECONDS` logic is untouched.

Normal tracks hit on Shazam `attempt 0` and never touch AudD → zero added latency or
quota cost on the happy path.

### 2. Normalized track shape (the refactor)

Introduce a normalized dict that every backend produces and `_handle_match` consumes:

```python
# normalized track
{
    "title": str,
    "artist": str,
    "album": str | None,        # backend-supplied; iTunes is still primary
    "art_url": str | None,      # backend-supplied FALLBACK art (iTunes is primary)
    "isrc": str | None,
    "genre": str | None,
    "release_year": int | None,
}
```

Recognition splits into per-backend adapters, each returning `normalized | None`:

- `_identify_shazam(wav) → normalized | None` — wraps `shazam.recognize`; moves
  today's Shazam-shape parsing (title/`subtitle`→artist, `images.coverarthq/coverart`
  → `art_url`, and the current `_extract_enrichment` for isrc/genre/release_year) here.
- `_identify_audd(wav) → normalized | None` — new (see §3).

`_handle_match(normalized)` consumes the normalized shape. **The `attempt==0 ? "identifying" : "retrying"` phase publishing, dedupe on `last_song`, `retrigger_on_track_change` blip, and `publish_state` all stay as-is** — only the field
access changes from Shazam's raw dict to the normalized keys.

This isolates each backend behind one interface. (The rejected alternative — having
the AudD adapter fabricate a Shazam-shaped nested dict so `_handle_match` is untouched —
is leaky and fragile.)

### 3. AudD adapter (`_identify_audd`)

- POST WAV bytes to `https://api.audd.io/` as multipart form-data via the
  **already-present `aiohttp`** (no new dependency), fields: `api_token=<token>`,
  `file=<wav bytes>`, `return=apple_music,spotify` (richer metadata).
- Apply an explicit timeout (e.g. `aiohttp.ClientTimeout(total=10)`).
- Response handling:
  - `{"status":"success","result":{...}}` → map to normalized:
    `title`←`result.title`, `artist`←`result.artist`, `album`←`result.album`,
    `release_year`←year parsed from `result.release_date` (`YYYY-MM-DD`),
    `isrc`←`result.apple_music.isrc` if present else `None`,
    `genre`←first of `result.apple_music.genreNames` if present else `None`,
    `art_url`←`result.apple_music.artwork.url` (or `spotify` album image) if present
    else `None` (used only as the iTunes-fallback art).
  - `{"status":"success","result":null}` → `None` (clean miss).
  - `{"status":"error",...}`, non-200, malformed JSON, timeout, any exception →
    log a warning and return `None` (treated as a clean miss; recognition continues
    to the Shazam escalation and never crashes).

### 4. Album-art enrichment — PRESERVED EXACTLY

`_handle_match` keeps the current precedence, now reading the backend fallback from the
normalized field instead of Shazam's raw dict:

1. `album, art_url = await fetch_itunes_metadata(artist, title)` — **iTunes is the
   primary source** of high-res art + album (unchanged).
2. `if not art_url: art_url = normalized.get("art_url") or ""` — fall back to the
   **backend-supplied** art (Shazam `images` today; AudD `apple_music/spotify` art now).
   *(Previously this fell back to `track['images'][...]`; the normalized `art_url`
   carries exactly that value, so behavior is identical for Shazam and now also works
   for AudD.)*
3. `if not album: album = normalized.get("album") or "Unknown Album"` — prefer the
   backend album over the literal `"Unknown Album"` when iTunes has none (a small
   improvement; today it jumps straight to `"Unknown Album"`).
4. `art_base64 = await fetch_image_base64(art_url)` if `art_url` — unchanged.

Net: the "Shazam doesn't send album art, so we fetch it" behavior is fully maintained —
iTunes-first, backend-art fallback — for both backends.

### 5. Config + Settings UI

New opt-in keys, default **off**, added to **both** `core_engine.DEFAULT_CONFIG`
(+ runtime mirror + `_populate_runtime`) **and** the Pydantic `AudioConfig` (keeping the
"defaults must match" invariant):

- `Fallback_Enabled: bool = False`
- `AudD_API_Token: str = ""`  (secret)

Runtime mirror: `runtime["fallback_enabled"]`, `runtime["audd_token"]`. The fallback is
active only when `fallback_enabled and audd_token` (non-empty). Settings page gets a new
block: an enable toggle (matching the re-announce toggle pattern) and a token input
(`type="password"`, like the MQTT password), each with a help tooltip.

### 6. Error handling

- AudD network/timeout/quota/parse failures → logged, treated as a miss, recognition
  proceeds. Never fatal.
- No token / disabled → AudD silently skipped.

---

## Testing

- `_identify_audd` (mocked HTTP): success JSON → correct normalized mapping
  (incl. `release_date`→`release_year`, isrc/genre/art extraction); `result: null`,
  HTTP error, timeout/exception → `None`.
- `_identify_shazam` (mocked `shazam.recognize`): Shazam dict → normalized; miss → `None`.
- `_handle_match` art precedence: iTunes art present → used; iTunes art absent +
  normalized `art_url` present → backend art used; both absent → `""` (no crash);
  album fallback to normalized then `"Unknown Album"`.
- `recognize_audio` flow: Shazam-miss-then-AudD-hit on sample₁ → match + no escalation;
  AudD miss → Shazam 2×/3× escalation continues; fallback disabled / no token → AudD
  never called (assert `_identify_audd` not invoked).
- Config round-trip for `Fallback_Enabled` + `AudD_API_Token` (defaults + persistence).

## Files touched (anticipated)

- `core/core_engine.py` — `recognize_audio`, new `_identify_shazam`/`_identify_audd`,
  `_handle_match` (normalized + art precedence), `_extract_enrichment` (folded into
  `_identify_shazam`), `DEFAULT_CONFIG`/runtime/`_populate_runtime`.
- `gui/config_manager.py` — `AudioConfig`: `Fallback_Enabled`, `AudD_API_Token`.
- `gui/templates/settings.html` — fallback toggle + token field + tooltips.
- `core/tests/test_recognition_phases.py` — updated for new adapter names/shapes + new
  flow/adapter/art tests.
- `gui/tests/test_config_round_trip.py` — new config keys.
- `CHANGELOG.md` — `[Unreleased]`.

# SpinSense — Design Source of Truth

**Version:** 1.0.0.0 (2026-06-01)
**Status:** Live. This document describes what SpinSense **is today**, not what we want it to be next. Per-feature design specs live under `docs/superpowers/specs/`; the post-1.0 backlog lives in `ROADMAP.md`. Update this file when an architectural decision changes.

---

## 1. Product Overview

SpinSense bridges an analogue turntable and a digital smart home. It listens to the turntable's audio, identifies the track that's playing using a Shazam-compatible recognizer, and surfaces the result in Home Assistant as a `media_player` entity — alongside lights, thermostats, and the rest of the household.

**Primary user:** a home-vinyl-and-home-automation enthusiast who wants what's spinning on their deck to be visible to (and queryable by) Home Assistant, dashboards, voice assistants, and automations. Typically running a Raspberry Pi or x64 NAS next to or near the turntable.

**Design priorities, in order:**
1. **Local & private.** Recognition results, history, and album art stay on the user's hardware. The only network call to a third party is the Shazam audio query (the recognizer it wraps) and the iTunes metadata lookup.
2. **Zero-config when possible.** mDNS discovery means a fresh install is reachable from Home Assistant with no IP, port, or broker entered anywhere.
3. **Docker-first.** A single container, a single compose file, a prebuilt multi-arch image. The setup-wizard handles everything beyond that in a browser.
4. **Hot-reloadable.** Configuration changes (mic, threshold, recognition tuning, mDNS toggle) take effect on the running engine without a restart.

---

## 2. Top-Level Architecture

```
┌─────────────────────────────┐         ┌──────────────────────────┐
│  Turntable → audio interface│         │   Home Assistant LAN     │
│         │                   │         │                          │
│  /dev/snd                   │         │  HACS integration:       │
│  passthrough                │         │  ycsgc1/homeassistant-   │
│         ▼                   │         │     spinsense            │
│  ┌───────────────────────┐  │   mDNS  │  ┌────────────────────┐  │
│  │  Engine process       │──┼─────────┼─▶│ media_player entity│  │
│  │  core/core_engine.py  │  │  HTTP   │  └────────────────────┘  │
│  │                       │  │  WS     │                          │
│  │  ┌─audio loop         │  │         │   (or, optional)         │
│  │  ├─Shazam recognition │  │         │   ┌──────────────────┐   │
│  │  ├─iTunes metadata    │  │         │                          │
│  │  ├─track-end clock    │  │         │                          │
│  │  └─config watcher     │  │         │                          │
│  └───────────────────────┘  │         └──────────────────────────┘
│           │ UDS             │
│           │ /tmp/spinsense* │
│           ▼                 │
│  ┌───────────────────────┐  │       ┌──────────────────────┐
│  │  GUI / backend        │──┼───────┤  Browser             │
│  │  gui/backend_main.py  │  │  HTTP │  http://host:3313/   │
│  │  FastAPI + uvicorn    │  │  WS   │  setup wizard,       │
│  │                       │  │       │  dashboard, history, │
│  │  ┌─templates/         │  │       │  settings            │
│  │  ├─static/            │  │       └──────────────────────┘
│  │  ├─SQLite history     │
│  │  ├─album-art cache    │
│  │  └─mDNS advertiser    │
│  └───────────────────────┘
│                             │
│  Single Docker container    │
└─────────────────────────────┘
```

**Two processes inside one container** (`docker/entrypoint.sh`):
1. The **engine** (`python core/core_engine.py`) runs in the background. It owns audio capture, detection, recognition, metadata enrichment, and the `config.json` file watcher.
2. The **GUI/backend** (`uvicorn backend_main:app`) runs in the foreground (PID 1 by container convention). It owns the web UI, the integration HTTP/WebSocket contract, the SQLite history database, the album-art cache, and the mDNS advertiser.

They communicate over **two named UDS sockets** under `/tmp` (see §3). This split lets the engine focus on real-time audio work without any HTTP framework overhead, and lets the GUI restart independently for hot reloads during development.

---

## 3. Inter-Process Communication (UDS)

| Socket | Direction | Used for | Owned by |
| --- | --- | --- | --- |
| `/tmp/spinsense.sock` | engine → backend | Live status frames (RMS, current track, engine status) | Backend listens (`gui/ipc_manager.py`); engine writes (`core_engine.py` audio loop) |
| `/tmp/spinsense-cmd.sock` | backend → engine | Command channel: `start_calibration`, `get_calibration`, `clear_calibration`, `rescan` | Engine listens (`command_listener_loop()` in `core_engine.py`); backend writes (`_send_cmd` in `backend_main.py`) |

Both use **JSON-per-line**, short-lived connections. The status socket has a long-lived listener with reconnects on either side; the command socket is one connection per command.

The status frame schema is the single source of truth for what the GUI and the HA integration both render:

```json
{
  "type": "live_status",
  "payload": {
    "engine_active": true,
    "status_msg": "Playing",
    "rms_level": 0.0042,
    "track": {
      "title": "Sing Sing Sing",
      "artist": "Benny Goodman",
      "album": "The Famous 1938 Carnegie Hall Jazz Concert",
      "art_url": "https://..."
    }
  }
}
```

Fields are **additive** — a consumer that doesn't know a key skips it, which is
how an older HACS integration keeps working against a newer engine. The sample
above shows the core; the frame also carries `phase`, `input_ok`, the
enrichment fields on `track` (`isrc`, `genre`, `release_year`, `duration_secs`,
`album_exclusive`), the `play_clock` block (§6.1), and `supersedes_previous`
(§6.2), which is true on exactly one frame per manual rescan.

The backend caches the most recent payload (`ipc_manager.last_status`) and serves it on `GET /api/status` so the HA integration can poll between WebSocket frames.

---

## 4. Configuration Model

A single `config.json` under `SPINSENSE_DATA_DIR` (default `/app/data`). Validated by pydantic on every read and write (`gui/config_manager.py`). Schema (live as of 1.0):

```python
class SpinSenseConfig(BaseModel):
    System: SystemConfig         # Auto_Start, Engine_Status, Setup_Wizard_State
    Hardware: HardwareConfig     # Mic_Device
    Audio: AudioConfig           # Volume_Threshold, Song_Sample_Length, *_Silence_Interval
    Discovery: DiscoveryConfig   # mDNS{Enabled, Service_Name}
```

**Defaults that matter:**
- `Volume_Threshold = 0.01` (linear RMS, = −40 dBFS). Internal storage stays linear; the UI converts to dB.
- `Discovery.mDNS.Enabled = True`. Zero-config out of the box.
- `Setup_Wizard_State = "pending"`. The middleware redirects to `/setup` on first run.

**Hot-reload model.** The engine watches `config.json`'s mtime every 2 seconds. On change, `_apply_config_diff()` re-populates the `runtime` dict and dispatches side effects per category:

| Category changed | Side effect |
| --- | --- |
| Audio thresholds | Picked up on next audio-loop iteration |
| Mic device | `mic_change_event.set()` → audio loop tears down + reopens the InputStream |
| `Discovery.mDNS.Enabled` | Backend's advertiser starts / stops in place |

**Why a file watcher and not in-process pub/sub?** The GUI process writes config.json (validated, via `POST /api/config`); the engine reads it. Two processes, one file. mtime polling at 2 s is the simplest possible boundary that doesn't require RPC.

---

## 5. Storage

Two stores, both under `SPINSENSE_DATA_DIR`:

**`history.db`** — SQLite (`gui/play_history.py`). One table:

```
plays(
  id INTEGER PRIMARY KEY,
  played_at INTEGER,      -- unix epoch seconds
  title TEXT, artist TEXT, album TEXT,
  art_path TEXT,          -- relative to ART_DIR; rendered via /art/...
  isrc TEXT,              -- nullable; future analytics
  genre TEXT,             -- nullable
  release_year INTEGER    -- nullable
)
```

Later columns (`ended_at`, `duration_secs`, `album_locked`, `started_at`, `join_offset_secs`) arrived the same way and are documented where the feature that added them is described (§6.1, §14).

The three nullable columns (`isrc`, `genre`, `release_year`) were added in 1.0 with an idempotent `ALTER TABLE` migration. They're populated best-effort from the recognition result; old rows stay valid as NULLs. They exist now so a future "listening Wrapped"-style feature has real data to mine — you cannot retroactively backfill listening history.

**`art_cache/`** — downloaded album art, served under `/art/...`. The dashboard and history pages reference these via local URLs; the HA integration sees the **remote** `art_url` from the recognition pipeline (so it works without going through the SpinSense host as a proxy).

---

## 6. Audio Pipeline

The engine's main loop (`audio_monitor_loop` in `core/core_engine.py`):

```
sounddevice InputStream
    └─audio_callback()  ── RMS computed every buffer (~22 Hz at 48 kHz / 1024 frames)
         │
         ├─state["current_rms"] = rms          ← drives live meter (UDS frames)
         └─if calibration["status"]=="running":
              calibration["samples"].append(rms)   ← Step 7

main loop @ 1 Hz:
    if calibration is running:  skip detection branch (suppression)
    elif rms > runtime["threshold"]:
        if not in_song or just-came-out-of-silence:
            recognize_audio()  ── Shazam → iTunes metadata → publish + persist
    elif in_song:
        silence_counter++
        if silence_counter >= stopped_silence:
            publish_state("stopped")
```

**Recognition (`recognize_audio`):**
1. Capture `Song_Sample_Length` seconds (default 5 s) into an in-memory WAV buffer.
2. Send to `shazamio.Shazam.recognize()`.
3. On hit: fetch high-res album art from iTunes (`fetch_itunes_metadata`).
4. Publish the result over the UDS status frame, which the backend broadcasts to the dashboard and Home Assistant, and writes to SQLite.

**Why the `rms > threshold` model and not a continuous Shazam stream?** API budget, and silence-after-side handling. The engine spends most of its time idle, watching one float. Recognition only runs when the needle drops.

**Engine state machine — high level:**

```
LISTENING ──audio above threshold──▶ RECOGNIZING ──result──▶ PLAYING(track)
   ▲                                     ▲                        │
   │                                     │ predicted end passed   │
   │                                     └────────────────────────┤
   │                                                              │
   └──────────silence_counter ≥ stopped_silence ──────────────────┘
```

`status_msg` in UDS frames maps directly to this state: `"Listening"`, recognition-in-progress (no public name; the WebSocket pauses), `"Playing"`, `"stopped"`.

### 6.1 Track-end prediction and the play clock

Silence detection alone misses transitions on records whose inter-track gaps are too short or too quiet to reach `New_Song_Silence_Interval` — and no threshold setting fixes that, because at the sensitivity needed to catch those gaps, quiet passages *inside* songs start triggering rescans.

So there is a second, independent way out of a track: **we know how long it is.** `core/track_clock.py` is a pure module (no I/O, `now` always passed in — same contract as `_scan_decision`) that turns the duration plus Shazam's `matches[0].offset` into a deadline:

```
position   ← matches[0].offset, or 0 if missing / implausible
remaining  ← duration - position
grace      ← min(max(Track_End_Grace_Secs, 0.10 * duration), 60)
deadline   ← capture_start + remaining + grace
```

Past the deadline with no gap detected, the engine spends **one** recognition asking what is actually playing. Anchoring at capture start (not match time) keeps network latency out of the estimate; falling back to `position = 0` when the offset looks wrong makes the prediction fire *late*, which costs nothing.

**The call budget is the design constraint.** Four gates keep it bounded: no duration means permanently disarmed; at most 3 end-checks per track, with the counter *inherited* across same-track re-arms so bad duration metadata can't loop forever; exponential deferral between them; and a stand-down once the gap has qualified anyway (without which the runout groove at the end of every side would drain the budget). Worst case is 3 extra calls on a track we keep failing to place; typical case is zero.

One behavioural exception: an end-check calls `recognize_audio(preserve_on_miss=True)`. The ordinary no-match path tears down "now playing" and arms the back-off gate, which is right for a fresh onset — but an end-check runs against a track we are still playing, so a miss there means "we could not tell", not "there is nothing here".

**The play clock** rides along in every frame as a `play_clock` block beside `track` (additive; unknown keys are ignored by the HACS integration):

```json
"play_clock": {"started_at": 1756338000, "join_offset_secs": 42,
               "duration_secs": 213, "position_source": "shazam_offset"}
```

`started_at` is when the track actually began on the platter; `join_offset_secs` is how far in we started hearing it. Both persist to `plays`. `played_at` deliberately keeps its old meaning ("when we identified it") so every existing row, every stats query and history ordering stay valid.

**Offset semantics — measured, 2026-08-28.** This was the open question carried
over from the 2026-07-12 lyrics design, whose bench spike never ran. Settled on
real hardware instead, against AJR's "Maybe Man" (220 s):

| Scan at | `join_offset_secs` | `position_source` |
|---|---|---|
| needle drop, ~1 s in | `1` | `shazam_offset` |
| forced rescan, ~184 s in | `184` | `shazam_offset` |

So `matches[0].offset` is the playhead in the track, measured at the **start** of
the submitted sample — exactly what `resolve_position()` assumes. Two readings
that would have broken the design are ruled out: it is not the position of the
sample's *end* (that would have reported ~6 s and ~189 s), and it is not an
offset within the submitted buffer (that would have reported ~0 in both cases,
and would have made every mid-song join predict the end a full track too late).

The second measurement is the load-bearing one. A single reading near the top of
a track cannot distinguish a real playhead from a constant zero; only a scan
taken deliberately mid-song can. `_log_clock()` still prints position and source
on every match, which is how this was measured and how a regression would show.

---

### 6.2 The needle drop, and the play it invents

Lowering the needle makes a thump. It is loud — comfortably past
`Volume_Threshold` — and it is the *only* thing that is: behind it sits the
lead-in groove, which is silent, and the music does not start for another second
or three. To the 1 Hz monitor loop, which sees one instantaneous RMS reading per
tick, that thump is indistinguishable from a song starting.

So a scan fires, and it samples the lead-in groove. Three things can come of it:

| what the sample held | what happens |
|---|---|
| nothing but the thump | no match — harmless, back-off arms, the music triggers a real scan |
| the thump and a fragment of the song | **a confident wrong answer** |
| the song, properly | correct, by luck of timing |

The middle row is the problem, and it is worse than a plain miss. Half a second
of audio is enough for the recognizer to answer, and enough for it to answer
with the wrong track. That wrong track then owns the entire first song of the
side, because the thing that would normally correct it — a detected gap — does
not come until the song is *over*.

**The guard.** A needle drop and a song do not look alike, once you ask the
right question. Music *fills* a sample; a needle drop is a spike in a field of
silence. `active_audio_ratio()` splits the capture into 50 ms frames and reports
what fraction of them clear the same `Volume_Threshold` the monitor loop uses.
A needle drop reads a few percent; a song reads near 1.0, and still reads well
above the 0.4 cutoff with two full seconds of near-silence in the middle of it.
Below the cutoff, `recognize_audio()` returns before it ever calls out —
so this costs no API budget to enforce, and saves the call it rejects.

Three properties make it safe to have on by default:

- **It only runs before a track is known** (`not in_song`). Mid-play, a quiet
  sample is a quiet passage, and the track-end path handles that.
- **A manual rescan is never guarded.** The listener asked; refusing to look
  because the room is quiet would make the button appear broken.
- **It changes no gate on the way out.** The lead-in groove reads as silence, so
  the ordinary path already waits and then scans the moment audio returns —
  which is when the music starts. Arming the back-off here looks tempting and is
  a trap: if the song had *already* begun during the rejected capture, the audio
  never goes quiet again, the back-off never clears, and the engine sits out the
  entire first track. That is worse than the bug being fixed.
- **It can only refuse `MAX_NEEDLE_DROP_ABORTS` times in a row.** This is a
  heuristic about a mechanical event, and a heuristic that can refuse
  indefinitely is one that can go deaf. An intermittent input — a click once per
  revolution, a dusty groove — eventually gets handed to the recognizer anyway,
  which either identifies it or gives up through the ordinary no-match path and
  arms the back-off there. Any accepted capture clears the streak.

Measurement happens on the **raw** capture, before normalisation. Peak
normalisation would drag the silence up along with the thump and make a needle
drop look like music.

**The cleanup: a rescan replaces what it corrects.** The guard cannot catch
every misfire, and when one gets through the listener sees a wrong title and
presses Rescan. Filing the correction as a *second* play would be the opposite
of what pressing the button meant — the wrong one would stay in History and in
the scrobble queue.

So a manual rescan that lands on a *different* track raises
`supersedes_previous` on exactly one status frame, and the backend soft-deletes
the open play instead of stamping its `ended_at`. Four things bound it:

- **Only a manual rescan.** An automatic re-identification never replaces
  anything, which is what protects the case where the engine simply sat through
  a transition it never heard.
- **Only inside `SUPERSEDE_WINDOW_SECS` (90 s).** Later, a rescan means that
  missed transition, and the play being corrected is a real one that really did
  play. The window is shorter than any track that could have finished inside it.
- **Never a scrobbled play.** Last.fm has no API to take a scrobble back, so
  deleting our copy would only make the two records disagree.
- **Soft-delete, never a hard one.** This is an inference about intent, and
  inferences about intent should be reversible; the row restores like any other
  deleted play.

The flag is raised immediately before its publish and cleared in a `finally`
immediately after. A flag that outlived its frame would tell the backend to drop
a play that had nothing to do with the rescan. For the same reason the
`Retrigger_On_Track_Change` idle blip is skipped when superseding: that blip is
an empty-track frame, which the backend reads as the end of the very play the
rescan is about to replace.

---

## 7. Calibration

Two paths, both produce the same artifact: a single `Audio.Volume_Threshold` float in linear RMS.

**Manual:** drag a dB slider while watching a live dBFS meter. Internally the slider operates on −80 to 0 dB at 0.5 dB resolution; the linked number input mirrors it. On save the GUI converts dB → linear RMS via `gui/static/db_utils.js` and POSTs to `/api/config`.

**Auto-calibrate (the wizard's recommended path):**

```
User taps "Auto-calibrate"
   │
   ▼ "Drop needle on runout" → 5s capture
   │   Engine: start_calibration noise_floor
   │   audio_callback appends per-buffer RMS to deque
   │   _finish_calibration timer flips status to "done"
   │
   ▼ "Drop needle on a song, tap when it starts" → 5s capture
   │   (same path, phase=music)
   │
   ▼ Frontend reads stats for both phases:
       threshold_dB = noise_p99_dB + 0.25 * (music_p10_dB - noise_p99_dB)
       if threshold_dB < noise_p99_dB + 2:  threshold_dB = noise_p99_dB + 2
       (clamped to [-80, 0])
```

**Why this formula?** `noise_p99` is the loudest rumble blip during silence (must clear it). `music_p10` is the *quiet* parts of music (threshold must sit below these so quiet intros still trigger). The 0.25 weighting biases toward sensitivity — closer to noise — because real-world vinyl has very quiet intros and we'd rather catch them than miss them. The 2 dB safety floor prevents the formula from spitting out a value too close to noise when the gap is small.

**dB everywhere:** wizard, Settings, Dashboard. Internal storage stays linear so existing installs keep their saved value without migration. The conversion helper has a Python mirror under `gui/tests/test_db_utils.py` that pins the contract so JS↔Python math drift gets caught at CI time.

---

## 8. Setup Wizard

State machine: `System.Setup_Wizard_State ∈ {"pending", "skipped", "completed"}`.

Routing middleware (`backend_main.setup_wizard_gate`):

| Wizard state | Visiting `/`, `/history`, `/settings` | Visiting `/setup` |
|--------------|----------------------------------------|-------------------|
| `pending`    | 307-redirect to `/setup`               | Render wizard     |
| `skipped`    | Render normally                        | Render wizard     |
| `completed`  | Render normally                        | Render wizard     |

`/api/*`, `/static/*`, `/art/*`, `/ws/*` always pass through.

**Five steps:**

1. **Welcome** — intro, "Get started" or "Skip setup".
2. **Microphone** — dropdown populated from `/api/devices`.
3. **Calibrate threshold** — chooser sub-flow (Auto vs Manual; see §7).
4. **Home Assistant** — a single mDNS toggle, on by default. Zero-config with the companion HACS integration.
5. **Done** — "Save and finish" writes everything to `config.json`. The engine's file watcher picks it up within ~2 s. No restart.

**Three exits:**
- **X** (close) — leaves state as-is. If it was `pending`, the redirect fires again on the next page hit.
- **Skip setup** (footer link) — sets state to `"skipped"`. Auto-redirect stops.
- **Save and finish** (final step) — sets state to `"completed"`.

Re-entry is always available via **Settings → Re-run setup wizard**.

---

## 9. Discovery & Integrations

One integration path, toggleable in §8 step 4.

### 9.1 mDNS

`gui/discovery.py` advertises `_spinsense._tcp.local.` on `SPINSENSE_PORT` whenever the GUI process is running and `Discovery.mDNS.Enabled` is true. The companion HACS integration ([ycsgc1/homeassistant-spinsense](https://github.com/ycsgc1/homeassistant-spinsense)) declares the same service type in its `manifest.json` `zeroconf` key, so Home Assistant's discovery surfaces SpinSense automatically under Settings → Devices & Services → Discovered.

The integration's config flow reads `discovery_info.host` + `.port` (no IP or port typed anywhere). It then validates by calling `GET /api/status` and subscribes to `WS /ws/live-status` for ongoing state.

**Why mDNS requires `network_mode: host`.** Multicast does not cross Docker's bridge network. Under host mode the container binds `SPINSENSE_PORT` directly on the host; there is no `ports:` mapping (and adding one would do nothing). This is why the default port is **3313** (a nod to 33⅓ RPM) instead of 8000 — colliding with every other "default 8000" container under host networking would be a fresh-install footgun.

**Failure mode.** mDNS bind failures (UDP 5353 already in use, no network) are non-fatal. The GUI logs and carries on serving HTTP; the dashboard still works, and the integration can be added by hand with the host and port.

## 10. HTTP / WebSocket Contract

The HA integration depends on this surface. Breaking changes here ripple to a separate repo and a HACS install base.

| Endpoint | Purpose |
| --- | --- |
| `GET /` | Dashboard HTML |
| `GET /history` | History page HTML |
| `GET /settings` | Settings page HTML |
| `GET /setup` | Setup wizard HTML (always renders regardless of state) |
| `GET /api/config` | Current config (linear-RMS values stored, dB-display done client-side) |
| `POST /api/config` | Pydantic-validated config write; 400 on validation failure with `{"detail": "..."}` |
| `GET /api/devices` | Audio input devices visible to the container |
| `GET /api/setup-state` | `{"state": "pending\|skipped\|completed"}` |
| `POST /api/calibrate/start` body `{phase}` | Forwarded to engine; 503 if engine unreachable |
| `GET /api/calibrate/status` | Forwarded to engine; returns running/done/none + stats blob |
| `POST /api/calibrate/clear` | Forwarded to engine |
| `GET /api/recent?limit=N` | Recent plays for dashboard |
| `GET /api/plays?limit=N&offset=M` | Paginated history; capped at 100 |
| `GET /api/lastfm/status` | Connection state, username, pending queue depth |
| `POST /api/lastfm/auth/start` body `{api_key, api_secret, origin}` | Validates credentials; returns the redirect URL (built from `origin`) and a manual fallback URL |
| `GET /api/lastfm/callback?token=` | Where Last.fm returns the user; 303s to `/settings` with the outcome |
| `POST /api/lastfm/auth/complete` | Manual fallback: trades the pre-minted token for a session key |
| `POST /api/lastfm/disconnect` | Forgets the session; keeps the API key + secret |
| `POST /api/lastfm/flush` | Submits the pending queue now instead of on the timer |
| **`GET /api/status`** | Last cached engine status frame; the HA integration's poll endpoint |
| **`WS /ws/live-status`** | Push-only stream of status frames |

**Cache policy.** HTML and `/static/*` are served `Cache-Control: no-cache` so a rebuild can never leave a browser executing stale JS against fresh markup. `/art/*` and `/api/*` stay cacheable.

---

## 11. Deployment

**Distribution:** prebuilt multi-arch image at `ghcr.io/ycsgc1/spinsense`, built by a GitHub Actions workflow.

| Tag | Tracks |
| --- | --- |
| `:latest` | Each release |
| `<version>` | The specific release (e.g. `:1.0.0.0`) |
| `:main` | Every commit to `main` |

`docker compose pull && docker compose up -d` (and Dockge's Update button) just works. Building from source is supported via the commented `build:` block in the reference compose.

**Reference compose** (`docker-compose.yml`, host networking required for mDNS):

```yaml
services:
  spinsense:
    image: ghcr.io/ycsgc1/spinsense:latest
    container_name: spinsense_engine
    restart: unless-stopped
    devices:
      - "/dev/snd:/dev/snd"
    group_add: ["29"]
    ipc: host
    network_mode: host
    environment:
      - SPINSENSE_DATA_DIR=/app/data
      - SPINSENSE_PORT=3313
    volumes:
      - ./data:/app/data
      - /tmp:/tmp
```

**Critical knobs:**
- `/dev/snd` passthrough — without it the container has no audio devices.
- `group_add: ["29"]` (or `audio`) — owns the right ALSA permissions.
- `network_mode: host` — required for mDNS to reach the LAN.
- `volumes: ./data:/app/data` — persistence for `config.json`, `history.db`, and `art_cache/`. Without it, every rebuild starts from an empty database. **This is the single most-easily-missed config item.**

**Image build (`docker/Dockerfile`):** Python 3.11-slim base; installs `portaudio19-dev`, `alsa-utils`, `libsndfile1`, `ffmpeg`; pip-installs `requirements.txt`; copies the source; runs `docker/entrypoint.sh` (starts engine in background, uvicorn in foreground). Exposes 3313 documentationally — under host networking the EXPOSE is informational only.

---

## 11.1 Last.fm scrobbling

`gui/lastfm.py`, in the GUI process — that is where the play history lives. The engine knows nothing about it.

**Credentials: SpinSense ships its own application key.** The alternative — every user registering their own — was tried first and is worse: it front-loads a five-minute detour onto a button labelled "Connect to Last.fm", for a benefit (a private rate limit) that a vinyl-scrobbling workload will never need. Pano Scrobbler and Web Scrobbler both ship a key; so do we.

The key and secret are **public by construction**. They sit in a public repository and inside every published image, and no obfuscation changes that — it would only manufacture false confidence. Last.fm has no public-client flow (no PKCE equivalent) and `auth.getSession` cannot be signed without the secret, so an installed application either ships one or asks every user to register.

What it does not grant is access to anyone's account: scrobbling still requires a per-user session key from the approval flow. The realistic blast radius is someone burning the shared rate limit or getting the key revoked, which is exactly why bring-your-own is kept as an override rather than deleted — it is the escape hatch.

`credentials()` resolves most-specific-first: the user's own from config, then `SPINSENSE_LASTFM_KEY`/`_SECRET`, then the built-in pair. Each tier is all-or-nothing — a lone key paired with the built-in secret would sign a request Last.fm rejects with an error pointing nowhere useful. The built-in pair is never written into `config.json`, so a later build can replace a revoked key without every install pinning the dead one.

**How the handshake works.** Last.fm's web flow accepts a per-request `cb` parameter that overrides the callback registered on the API account. That is what makes a redirect viable for a box with no fixed address: the browser tells us the origin it is *already* reaching SpinSense on, we validate it is a bare `http(s)://host` (it goes into a URL we hand to a third party, so it is checked, not trusted), and Last.fm returns the user to `/api/lastfm/callback?token=…`. One click, Last.fm's own login page, and the user's password never touches SpinSense.

The manual desktop flow is kept as a fallback for when the redirect cannot return — approval on a different device, or a proxy that rewrites the origin. It mints a token up front; that token lives in a module-level variable between its two calls, because it is single-use and expires in 60 minutes, and persisting it would only leave a stale key to clean up. `auth.getToken` doubles as the credential check for *both* paths: it is a signed call, so a wrong key or secret fails at the Settings page rather than confusing the user on last.fm.

`/api/lastfm/callback` is a page the user lands on, not an API call, so every outcome — success, denial, error — is a 303 back to `/settings` with the result in the query string. It is unauthenticated, like everything else here; someone on the LAN could link their own account instead, which is the same exposure the rest of the app already carries and not made worse by this route.

**The queue is the interesting part.** Plays are marked with `scrobbled_at` once submitted, and the mark is what makes submission exactly-once. The rule for *when* to mark is the one that matters:

| Outcome | Marked? | Why |
|---|---|---|
| Accepted | yes | obviously |
| Accepted but ignored by Last.fm | yes | it will be ignored identically next time; retrying forever is a slow leak |
| Network / timeout | **no** | nothing reached Last.fm; retry next sweep |
| Retryable service error (8, 11, 16, 29) | **no** | Last.fm asked us to come back later |
| Other API error | yes | resubmitting would fail the same way |
| Session revoked (error 9) | **no** | the user can fix this; keep the queue for when they do, and disable scrobbling so we stop asking |
| Older than 14 days | yes, unsent | Last.fm refuses these outright — never offered to the API at all |

Two further bounds: `Scrobble_Since` is stamped at connect time and nothing before it is ever submitted (connecting an account must not upload months of back catalogue), and batches are capped at Last.fm's 50.

`play_history.scrobble_candidates(since, limit, pending_only)` is the read side — closed plays oldest-first (the order `track.scrobble` batches want) with the maths applied:

```
timestamp     = started_at or played_at         # true track start where known
listened_secs = ended_at - played_at            # never estimated; NULL stays NULL
eligible      = duration > 30s AND listened >= min(duration/2, 240s)
```

That last line is Last.fm's published rule verbatim. Ineligible rows come back **flagged, not dropped** — the ledger reports, the caller decides.

`track.updateNowPlaying` is fired from `ipc_manager` as each play is recorded, as a detached task: it is decorative, and it must never delay or fail the act of recording a play.

---

## 11.1.1 A side is one record

Metadata enrichment asks the record it already believes is playing before it
asks iTunes' search.

iTunes' song search is relevance-ranked, not authoritative, and in the field it
was wrong for five plays of a single OK ORCHESTRA side: nothing at all for "OK
Overture", two unrelated songs for "3 O'Clock Things", a lullaby cover for "My
Play", and only a live album for "World's Smallest Violin". Filtering cannot
fix a result that is absent from the response.

But a side is one album, and any track that *does* resolve carries the album's
`collectionId`. One `lookup` call then returns the real tracklist — every song
with its correct duration — and all five of those tracks are on it.

So the engine keeps an `album_context`: the collection id and name of the
record it believes is on the platter, refreshed by every track that confirms
it. Subsequent tracks are answered from that tracklist, which beats search
because nine other tracks already established what is playing.

- **A track absent from the album falls through to search.** That is what lets
  a different record take over, and it re-points the context when it does.
- **The context expires after 30 minutes** without a confirming track, matching
  `reconcile.SESSION_GAP_SECS` — long enough to span flipping a side, short
  enough that tomorrow's listening starts clean.
- **Tracklists are cached per album for the engine's lifetime, misses
  included.** Caching the miss matters: an album iTunes cannot expand would
  otherwise be re-requested for every track for half an hour, and the cost of
  caching it is only the fallback to search that existed before.

Net API traffic goes *down*: one lookup per record instead of one search per
track.

This also repairs durations, which matters beyond the label — "World's Smallest
Violin" is 180 s, and the 229 s taken from the live album is why a track-end
check (§6.1) fired some fifty seconds late on it.

---

## 11.1.2 Two cheaper oracles than search

Resolving a track's album asks three sources, most specific first.

**The record on the platter** (§11.1.1) — the current run's tracklist.

**The listener's own history.** A vinyl collection is small and repetitive: most
people own one or two pressings of any given record, and a track identified
today almost certainly belongs to the album it belonged to last time.
`play_history.album_for_track()` answers instantly, with no API call, and it
reflects what the listener actually *owns* — if they have only ever played the
deluxe, the deluxe is the right answer for them. An album they set by hand
(`album_locked`) outranks any later guess, because that is them telling us.

**iTunes search**, last, because it is relevance-ranked rather than
authoritative — and because "relevance" for a hit song means the single.
Asking about "Espresso" leads with "Espresso EP" and "Espresso - Single"
before "Short n' Sweet", so `choose_edition()` anchors on an album whenever one
is present. A lone single still resolves to itself; someone may genuinely be
playing a 7-inch.

### Backfilling the first track of a side

A side's first track has none of the above: no run established, and iTunes'
search returns nothing at all for some titles — "OK Overture" among them. By
the second track the record is known, but nothing was looking backwards, so the
first play stayed "Unknown Album" for good.

`reconcile_album()` now adopts the run's album for any play that never resolved
one. Two constraints keep it honest:

- It runs **after** edition unification, and judges agreement on `base_title`,
  so a run holding both "OK ORCHESTRA" and "OK ORCHESTRA (Deluxe)" reads as one
  record rather than two.
- It acts **only when the run is unanimous**. A session that genuinely spans two
  records leaves the unresolved play alone rather than letting one album bleed
  into the other.

Together these mean a track only has to be identified correctly *once* — from
then on the listener's own history answers for it.

### The bonus track that has to be heard

The upgrade rule above is only as good as the evidence reaching it, and on a
real side of *Short n' Sweet* none did. Three separate things stood in the way,
each of which looked correct in isolation.

**1. The tracklist shortcut answered about the wrong recording.** Once a record
is playing, tracks resolve from its tracklist rather than from search
(§11.1.2) — and that lookup matched on title alone. A deluxe edition routinely
carries two recordings of one song: *Short n' Sweet (Deluxe)* has "Please Please
Please" at track 2 by Sabrina Carpenter and again at track 14 by Sabrina
Carpenter & Dolly Parton. The duet therefore resolved against the *standard*
album's track 2 — wrong duration, wrong artwork, and, worst of all, the evidence
destroyed. "This track is not on the record we thought was playing" is exactly
what proves an edition, and the shortcut answered before anything could notice.

`find_track()` now takes the artist, and a mismatch returns None rather than
falling back to the title. None is not a wrong answer: it sends the caller to
search, which is where it would have gone had the tracklist never been consulted.

**2. The run stopped at the credit.** A session run is one artist's contiguous
plays, matched on the artist string — but a record's own bonus tracks are
frequently credited to more than one person. "Sabrina Carpenter & Dolly Parton"
matched none of the twelve plays around it, so the one play carrying the proof
sat in a run of its own, where there was nothing to upgrade.

`shares_credit()` treats one credit as the same record's as another when it is
that credit *plus a joined name* — a guest is appended, so the test is a prefix
at a join boundary, not a shared first word. Reducing each credit to its leading
name would also have worked here, and would have turned "Simon & Garfunkel" into
"Simon", "Florence + the Machine" into "Florence" and "Earth, Wind & Fire" into
"Earth". Prefix matching leaves all of those alone, and keeps a guest from
capturing a session in the other direction: "Rowan Blanchard & Sabrina
Carpenter" is Rowan Blanchard's record.

**3. Artwork did not follow the album.** Reconciliation rewrites album *titles*;
artwork is a separate file per play. A session that upgraded still read
"Short n' Sweet (Deluxe)" underneath twelve copies of the standard cover — the
album right and the record still visibly wrong.

`_settle_run_art()` closes that, deciding from what happened to the play
reconciliation just ran on: if its album survived and the run moved to meet it,
that play's cover is the run's cover; if the play was itself rewritten, its cover
is the one now wrong and it takes the run's. Only plays sharing the settled album
are touched, since a run can span two records by one artist.

It is also **serialized**, which is not optional. A side spawns a dozen of these
a few minutes apart, they write to overlapping rows, and `_replace_art_file()`
unlinks whatever it supersedes — interleaved, an older settle finishes last and
restores the very cover the newest one just corrected. Every artwork write for a
play happens inside that lock, including the plain single-play download: a
fire-and-forget `create_task` there escapes the lock and reintroduces the same
race, which is what a flaky test caught before this shipped.

**4. Search does not know some bonus tracks at all.** Three tracks of the
seventeen — "15 Minutes", "Busy Woman", "Couldn't Make It Any Harder" — return
nothing usable from a song search, and an album-entity search for
"Short n' Sweet" does not list the deluxe either. They were filed as
*Unknown Album*, and the session upgraded only when it happened to reach
"Bad Reviews", the one bonus track search does know — nine tracks and forty
minutes later.

Listed under the **artist**, the deluxe is right there. So `_edition_carrying()`
is the last resort, and it states the original rule outright: *a song that is not
on the standard pressing means the pressing on the platter is not the standard
one*. Take the editions of the record we believe is playing, and ask which of
them has this track. Reached only once search has already failed, so it costs
nothing on the ordinary path and replaces an "Unknown Album" when it works;
cached per artist per engine run, since it returns a whole career.

Three constraints on which releases may answer:

- **Same base title as the record playing.** An artist re-records and reuses
  titles, so "this artist has a song by that name" is not evidence about the
  pressing. Singles and EPs need no separate exclusion — "- Single" and "EP" are
  not edition qualifiers, so they survive `base_title()` and this comparison
  already refuses them.
- **Plainest edition first**, matching `choose_edition()`: never claim a super
  deluxe when an ordinary deluxe accounts for the track just as well.
- **`exclusive` is `not is_base_form(name)`** — the track is on a qualified
  edition and demonstrably not on the base, which is exactly the definition.

With all four, the reported session resolves against the live API end to end.
The first deluxe-only track upgrades the record rather than the ninth, so
everything after it resolves from the seventeen-track list with the right
durations and the right cover; the twelve plays before it are upgraded by the
reconciler; and every cover follows.

---

### Memory never out-argues the record

Owning two pressings is the case this has to get right. History is consulted
**only when the lookup resolved nothing**, so putting on the standard pressing
files as standard even for a listener who has only ever played the deluxe.

And the one path where memory does apply — the lookup failed, so the remembered
deluxe is filled in — corrects itself. Reconciliation sees a run whose other
tracks resolved to the base and no track proving the deluxe, so the plainest
title wins. A qualifier is only ever kept when something proves it, which is
also why the reverse still holds: a genuine bonus track upgrades the whole side.

---

## 11.2 Why there is a shared package

`core/` and `gui/` are two processes and were two import roots, so anything
both needed got written twice — and the two copies drifted. Album-title
vocabulary lived only in `gui/reconcile.py`, invisible to the engine, which is
the process that actually asks iTunes which album a track belongs to; that is
what let "SOUR (Video Version)" be treated as an edition of *SOUR*. Separately,
each side had grown its own iTunes client against the same endpoint.

`spinsense/` holds what both need: the album vocabulary and one search client.
Everything in it is pure or purely-network, with no framework dependency, so
either process can import it and it is testable on its own.

Import resolution is the only cost. Neither process runs from the repository
root — the engine is a script in `core/`, the backend a uvicorn app in `gui/` —
so the root is put on the path by `PYTHONPATH=/app` in the image and by a
`conftest.py` in each suite under test. A `pip install -e .` with packaging
metadata would be the heavier alternative; for a single-image application this
buys the same thing with less to maintain.

---

## 12. Operational Notes

- **Tier 3 hot-reload.** Every field in `config.json` that the engine reads is mirrored into a mutable `runtime` dict at startup and re-applied by the file watcher. No engine restart for any setting change. (Old behavior — needing a restart — was a 0.3-era bug; the Settings page banner that warned about it is gone.)
- **mtime polling cadence.** 2 s. Fast enough that the UI feels live; slow enough that the engine's normal audio loop is unbothered.
- **Track-end checks are budgeted, not throttled.** The cap is per *track*, not per unit time, and it resets only when the track changes or real silence clears it. This is deliberate: a rate limit would still let one badly-tagged record scan all afternoon, where a budget cannot.
- **Detection suppression during calibration.** While a 5 s capture is `"running"`, the engine's audio loop skips the threshold-comparison branch entirely (samples still accumulate; the live meter still publishes). This prevents recognition firing on the calibration audio itself.
- **mDNS advertiser reconcile.** Lives in the GUI process. Stopping requires un-registering the `ServiceInfo`; starting binds a fresh one. Bind failures log and continue serving HTTP.

---

## 13. Project Structure

```
spinsense/                 # domain logic shared by BOTH processes
  albums.py                # album-title vocabulary, edition vs rendition,
                           #   choose_edition() and pick_winner()
  itunes.py                # the one iTunes Search client
  tests/
core/
  core_engine.py           # the engine process: audio + recognition + enrichment
  track_clock.py           # pure: track-end prediction + the play clock (§6.1)
  tests/                   # unittest; runs without audio hardware (mocks indata)
gui/
  backend_main.py          # FastAPI app, routes, middleware
  config_manager.py        # pydantic schema + load/save
  ipc_manager.py           # UDS listener for engine→backend frames; last-status cache
  discovery.py             # mDNS advertiser
  lastfm.py                # Last.fm auth handshake, scrobble queue, now-playing
  play_history.py          # SQLite history + migrations + the scrobble ledger
  audio_utils.py           # device enumeration for /api/devices
  templates/               # Jinja2: _layout, dashboard, history, settings, setup
  static/                  # JS, CSS, db_utils.js (the shared dB conversion)
  tests/                   # unittest; covers config round-trip, db_utils,
                           # play_history, calibrate API (with fake UDS listener)
docker/
  Dockerfile, entrypoint.sh
docs/
  superpowers/specs/       # per-feature design docs (one per phase)
  images/                  # README screenshots
docker-compose.yml         # the reference compose (image-based)
VERSION                    # 1.0.0.0
CHANGELOG.md               # Keep-a-Changelog format
README.md                  # install + setup + usage walkthrough
ROADMAP.md                 # post-1.0 backlog
DESIGN.md                  # ← you are here
```

---

## 14. What's Deliberately Not Here

- **Listening analytics / "Wrapped".** The history schema has the nullable columns ready (`isrc`, `genre`, `release_year`) but no surface yet. Deferred post-1.0.
- **Clean DB export/import** for device migration. Backup is "copy the `data/` volume." Tracked in `ROADMAP.md`.
- **Schema normalization** (separate `artists` / `tracks` tables). Same data, different shape — punt until analytics actually need joins.
- **A JS test runner.** The frontend is hand-verified against the manual test plan in the spec. The Python mirror of `db_utils.js` is the closest thing to a unit test the JS gets.
- **Engine restart on failure.** If hot-reload fails mid-flight, the engine logs and continues with the old value rather than crashing. No supervisor, no auto-restart loop inside the container — Docker's `restart: unless-stopped` is the only safety net, and it should rarely need to fire.

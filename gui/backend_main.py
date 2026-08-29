import asyncio
import hashlib
import json
import os
import urllib.parse
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
import sounddevice as sd

import lastfm
import play_history
import stats
import reconcile
from config_manager import SpinSenseConfig, load_config, save_config
from ipc_manager import ART_DIR, events, manager, handle_uds_client, unify_art
from discovery import advertiser
from spinsense import itunes

# Paths that the setup-wizard redirect must let through. Everything outside
# this list is gated when Setup_Wizard_State == "pending".
_SETUP_ALLOWED_PREFIXES = ("/setup", "/api/", "/static/", "/art/", "/ws/")

CMD_SOCKET_PATH = '/tmp/spinsense-cmd.sock'


async def _send_cmd(payload: dict, timeout: float = 2.0) -> dict:
    """Open a short-lived connection to the engine's command socket, write
    one JSON line, read one JSON line, close. Returns the parsed reply.

    Raises FileNotFoundError if the socket doesn't exist, ConnectionRefusedError
    if the engine isn't listening, asyncio.TimeoutError on either side."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_unix_connection(CMD_SOCKET_PATH),
        timeout=timeout,
    )
    try:
        writer.write((json.dumps(payload) + '\n').encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        return json.loads(line.decode())
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def start_uds_listener():
    socket_path = '/tmp/spinsense.sock'
    if os.path.exists(socket_path):
        os.remove(socket_path)

    server = await asyncio.start_unix_server(handle_uds_client, path=socket_path)
    print(f"🎧 Now listening for Core Engine on {socket_path}")

    async with server:
        await server.serve_forever()


async def _purge_loop():
    """Reclaim art for scrobbles soft-deleted beyond the Undo grace window."""
    while True:
        await asyncio.sleep(1800)  # 30 min
        try:
            await asyncio.to_thread(play_history.purge_deleted)
        except Exception as e:
            print(f"⚠️ purge sweep failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    play_history.init_db()
    os.makedirs(ART_DIR, exist_ok=True)
    try:
        await asyncio.to_thread(play_history.purge_deleted)
    except Exception as e:
        print(f"⚠️ startup purge failed: {e}")
    task = asyncio.create_task(start_uds_listener())
    purge_task = asyncio.create_task(_purge_loop())
    scrobble_task = asyncio.create_task(lastfm.flush_loop())
    try:
        await advertiser.start(load_config())
    except Exception as e:
        print(f"⚠️ mDNS advertiser failed to start: {e}")
    yield
    await advertiser.stop()
    task.cancel()
    purge_task.cancel()
    scrobble_task.cancel()


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def setup_wizard_gate(request: Request, call_next):
    """Redirect to /setup whenever Setup_Wizard_State is "pending" and the
    user is hitting a normal page route. API + static + the wizard itself are
    always allowed through."""
    path = request.url.path
    if not any(path.startswith(p) for p in _SETUP_ALLOWED_PREFIXES):
        try:
            cfg = load_config()
            state = cfg.get("System", {}).get("Setup_Wizard_State", "pending")
        except Exception:
            state = "pending"
        if state == "pending":
            return RedirectResponse(url="/setup", status_code=307)
    return await call_next(request)


@app.middleware("http")
async def no_cache_app_assets(request: Request, call_next):
    """Force revalidation of anything whose URL is stable but whose bytes move.

    That's the app's HTML and static JS/CSS: a rebuild must never serve a stale
    asset against fresh markup.

    `/art/` is included as a belt-and-braces measure rather than the real fix.
    Artwork filenames are content-addressed now, so changed art is a changed
    URL and no cache can serve the old one — but a caching layer that ignores
    `Cache-Control` was strongly suspected here, and revalidation costs one 304
    on a file measured in kilobytes.

    `no-cache` is not `no-store`: the browser still keeps the file. `/api` stays
    cacheable — its responses are generated fresh anyway."""
    response = await call_next(request)
    ctype = response.headers.get("content-type", "")
    path = request.url.path
    if (path.startswith("/static/") or path.startswith("/art/")
            or ctype.startswith("text/html")):
        response.headers["Cache-Control"] = "no-cache"
    return response


# StaticFiles asserts these directories exist at construction time.
os.makedirs(ART_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/art", StaticFiles(directory=ART_DIR), name="art")
templates = Jinja2Templates(directory="templates")

# Version-stamp static asset URLs so a rebuild busts browser/proxy caches
# (kills the 'new HTML, stale CSS/JS' class of bug). VERSION lives at repo root.


def _static_digest(static_dir: str) -> str:
    """Short digest of everything under `static_dir`, by content.

    VERSION on its own is not enough, and shipping a beta proved it: VERSION
    only moves at a release, so every build of a rolling tag like :beta reused
    one `?v=` while the JavaScript underneath changed five times. A browser or
    an upstream proxy then serves yesterday's script against today's markup —
    exactly the bug the stamp exists to prevent.

    Hashing content rather than mtimes matters: a git checkout restamps mtimes
    on every build, so an mtime-based digest would change on every rebuild and
    needlessly discard caches that are still perfectly good.
    """
    digest = hashlib.sha256()
    for root, dirs, files in os.walk(static_dir):
        dirs.sort()  # stable walk order, or the digest is nondeterministic
        for name in sorted(files):
            path = os.path.join(root, name)
            digest.update(os.path.relpath(path, static_dir).encode())
            try:
                with open(path, "rb") as handle:
                    for chunk in iter(lambda: handle.read(65536), b""):
                        digest.update(chunk)
            except OSError:
                continue  # unreadable file: skip it rather than lose the stamp
    return digest.hexdigest()[:8]


def _asset_version() -> str:
    """`<version>.<digest>`, or a bare version if the assets can't be read."""
    try:
        with open(os.path.join(os.path.dirname(__file__), "..", "VERSION")) as _vf:
            version = _vf.read().strip() or "dev"
    except OSError:
        version = "dev"
    try:
        return f"{version}.{_static_digest('static')}"
    except Exception as e:
        print(f"⚠️ Could not digest static assets, using bare version: {e}")
        return version


ASSET_VERSION = _asset_version()
templates.env.globals["asset_v"] = ASSET_VERSION


# --- Page routes ---

@app.get("/")
async def dashboard(request: Request):
    return templates.TemplateResponse(
        request, "dashboard.html", {"current_page": "dashboard"}
    )


@app.get("/history")
async def history(request: Request):
    return templates.TemplateResponse(
        request, "history.html", {"current_page": "history"}
    )


@app.get("/settings")
async def settings(request: Request):
    return templates.TemplateResponse(
        request, "settings.html", {"current_page": "settings"}
    )


@app.get("/stats")
async def stats_page(request: Request):
    return templates.TemplateResponse(
        request, "stats.html", {"current_page": "stats"}
    )


@app.get("/setup")
async def setup(request: Request):
    return templates.TemplateResponse(
        request, "setup.html", {"current_page": "setup"}
    )


# --- API routes ---

@app.get("/api/config")
def get_config():
    return load_config()


@app.post("/api/config")
async def update_config(request: Request):
    new_config = await request.json()
    try:
        SpinSenseConfig(**new_config)
    except ValidationError as e:
        errs = e.errors()
        first = errs[0] if errs else {}
        loc = ".".join(str(p) for p in first.get("loc", []))
        msg = first.get("msg", "Validation failed")
        return JSONResponse(
            status_code=400,
            content={"status": "error", "detail": f"{loc}: {msg}" if loc else msg},
        )
    if not save_config(new_config):
        return JSONResponse(
            status_code=500,
            content={"status": "error", "detail": "Failed to write config.json"},
        )
    try:
        await advertiser.reconcile(new_config)
    except Exception as e:
        print(f"⚠️ mDNS reconcile after config save failed: {e}")
    return {"status": "success"}


@app.get("/api/devices")
def get_audio_devices():
    try:
        devices = sd.query_devices()
        mics = [{"name": d['name']} for d in devices if d['max_input_channels'] > 0]
        unique_mics = list({m['name']: m for m in mics}.values())
        return {"devices": unique_mics}
    except Exception as e:
        print(f"Error querying devices: {e}")
        return {"devices": []}


@app.get("/api/setup-state")
def get_setup_state():
    cfg = load_config()
    return {"state": cfg.get("System", {}).get("Setup_Wizard_State", "pending")}


@app.post("/api/calibrate/start")
async def calibrate_start(request: Request):
    body = await request.json()
    phase = body.get("phase")
    if phase not in ("noise_floor", "music"):
        return JSONResponse(
            status_code=400,
            content={"ok": False, "detail": f"invalid phase: {phase!r}"},
        )
    try:
        reply = await _send_cmd({"cmd": "start_calibration", "phase": phase})
    except (FileNotFoundError, ConnectionRefusedError, asyncio.TimeoutError):
        return JSONResponse(
            status_code=503,
            content={"ok": False, "detail": "Engine not reachable"},
        )
    return reply


@app.get("/api/calibrate/status")
async def calibrate_status():
    try:
        reply = await _send_cmd({"cmd": "get_calibration"})
    except (FileNotFoundError, ConnectionRefusedError, asyncio.TimeoutError):
        return JSONResponse(
            status_code=503,
            content={"status": "none", "samples_count": 0, "stats": None, "detail": "Engine not reachable"},
        )
    return reply


@app.post("/api/calibrate/clear")
async def calibrate_clear():
    try:
        reply = await _send_cmd({"cmd": "clear_calibration"})
    except (FileNotFoundError, ConnectionRefusedError, asyncio.TimeoutError):
        return JSONResponse(
            status_code=503,
            content={"ok": False, "detail": "Engine not reachable"},
        )
    return reply


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


@app.get("/api/recent")
async def get_recent(limit: int = 10):
    rows = await asyncio.to_thread(play_history.recent_plays, limit)
    return {"plays": rows}


@app.get("/api/plays")
async def get_plays(limit: int = 50, offset: int = 0):
    rows = await asyncio.to_thread(play_history.recent_plays, limit, offset)
    total = await asyncio.to_thread(play_history.count_plays)
    return {"plays": rows, "total": total}


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


async def _itunes_album_candidates(artist: str, title: str) -> list[dict]:
    """Distinct candidate albums for a track, for the manual picker."""
    results = await itunes.search_songs(
        artist, title, limit=itunes.CANDIDATE_LOOKUP_LIMIT)
    # Same filter the engine applies: offering albums belonging to some other
    # song is worse than offering none, since the picker looks authoritative.
    return itunes.album_candidates(
        itunes.results_for_track(results, title, artist))


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

    # Awaited, not fired and forgotten: the caller is a person who clicked Save
    # and is about to redraw those rows, so the response must not claim the art
    # is in place before it is. Falls back to the edited play's own artwork when
    # no URL was chosen, so "apply to the whole session" always ends up uniform.
    arted = await unify_art(ids, play_id, art_url)

    rows = await asyncio.to_thread(_rows_for, ids)
    # No cache-busting parameter needed: artwork filenames are content-addressed,
    # so new art arrives as a genuinely new URL in each row's art_path.
    return {"status": "ok", "updated": len(ids), "arted": len(arted), "rows": rows}


def _rows_for(ids: list[int]) -> list[dict]:
    """The post-edit state of the rows we touched, so the page can redraw them
    in place instead of reloading and losing the reader's position."""
    out = []
    for pid in ids:
        play = play_history.get_play(pid)
        if play is not None:
            out.append({"id": play["id"], "album": play["album"],
                        "art_path": play["art_path"]})
    return out


# --- Last.fm ---
#
# Two ways to reach the same session key (see gui/lastfm.py). The default is
# Last.fm's redirect flow: the user clicks once, approves on last.fm's own page,
# and is redirected to /api/lastfm/callback with a token. The fallback is the
# manual flow, for when that redirect can't come back — approving on a phone, or
# reaching SpinSense through something that rewrites the origin.
#
# The manual flow's token lives here between its two calls only: it is
# single-use, expires in 60 minutes, and persisting it would just leave a stale
# key to clean up.
_pending_auth_token: str | None = None


@app.get("/api/lastfm/status")
async def lastfm_status():
    return await asyncio.to_thread(lastfm.status)


@app.post("/api/lastfm/auth/start")
async def lastfm_auth_start(request: Request):
    """Resolve the credentials, then hand back both auth URLs.

    The user only supplies a key and secret when overriding the application
    SpinSense ships with; the ordinary path sends neither and gets the built-in
    pair. A partial override is rejected rather than silently half-applied,
    because mixing one half with the built-in other half produces a signature
    Last.fm rejects with an error that points nowhere useful.

    `auth.getToken` doubles as the credential check: it's a signed call, so a
    wrong key *or* a wrong secret fails here rather than confusing the user on
    last.fm's page. The token it returns is what powers the manual fallback, so
    nothing is wasted either way.
    """
    global _pending_auth_token
    body = await request.json()
    api_key = str(body.get("api_key") or "").strip()
    api_secret = str(body.get("api_secret") or "").strip()
    supplied = bool(api_key or api_secret)
    if supplied and not (api_key and api_secret):
        return JSONResponse(
            status_code=400,
            content={"ok": False,
                     "detail": "Supply both the API key and the shared secret, or neither"})

    if not supplied:
        api_key, api_secret = await asyncio.to_thread(lastfm.credentials)
        if not api_key or not api_secret:
            return JSONResponse(
                status_code=400,
                content={"ok": False,
                         "detail": "This build has no Last.fm application key — "
                                   "supply your own under Advanced"})

    token, err = await lastfm.request_token(api_key, api_secret)
    if err:
        return JSONResponse(status_code=502, content={"ok": False, "detail": err})

    # Persist an override only after Last.fm confirms the pair works, so a typo
    # can't overwrite working credentials. The built-in pair is never written to
    # config — it must stay resolved at run time so a later build can replace it.
    if supplied and not await asyncio.to_thread(
            lastfm._store, API_Key=api_key, API_Secret=api_secret):
        return JSONResponse(
            status_code=500,
            content={"ok": False, "detail": "Failed to write config.json"})
    _pending_auth_token = token

    # The redirect flow needs somewhere to come back to. The browser tells us
    # which address it is actually reaching SpinSense on — a LAN box has no
    # fixed one, and this is the only party that knows.
    callback = lastfm.callback_url(str(body.get("origin") or ""))
    return {
        "ok": True,
        "auth_url": lastfm.web_auth_url(api_key, callback) if callback else None,
        "manual_url": lastfm.auth_url(api_key, token),
    }


@app.get("/api/lastfm/callback")
async def lastfm_callback(token: str = "", request: Request = None):
    """Where Last.fm returns the user after they approve the redirect flow.

    Redirects back to Settings either way, with the outcome in the query string
    — this is a page the user lands on, not an API call, so it must never answer
    with raw JSON.

    Anyone on the LAN could call this with a token of their own and link their
    account instead. That is the same exposure as every other endpoint here (the
    whole app is unauthenticated by design, on the assumption of a trusted home
    network) and is not made worse by this route.
    """
    if not token:
        return RedirectResponse(url="/settings?lastfm=denied", status_code=303)
    api_key, api_secret = await asyncio.to_thread(lastfm.credentials)
    username, err = await lastfm.complete_auth(api_key, api_secret, token)
    if err:
        return RedirectResponse(
            url=f"/settings?lastfm=error&detail={urllib.parse.quote(err)}",
            status_code=303)
    return RedirectResponse(
        url=f"/settings?lastfm=connected&user={urllib.parse.quote(username)}",
        status_code=303)


@app.post("/api/lastfm/auth/complete")
async def lastfm_auth_complete():
    """Called once the user has approved the token on last.fm."""
    global _pending_auth_token
    api_key, api_secret = await asyncio.to_thread(lastfm.credentials)
    username, err = await lastfm.complete_auth(
        api_key, api_secret, _pending_auth_token or "")
    if err:
        return JSONResponse(status_code=400, content={"ok": False, "detail": err})
    _pending_auth_token = None
    return {"ok": True, "username": username}


@app.post("/api/lastfm/disconnect")
async def lastfm_disconnect():
    global _pending_auth_token
    _pending_auth_token = None
    if not await asyncio.to_thread(lastfm.disconnect):
        return JSONResponse(
            status_code=500,
            content={"ok": False, "detail": "Failed to write config.json"})
    return {"ok": True}


@app.post("/api/lastfm/flush")
async def lastfm_flush():
    """Release the queue by hand, review window and all.

    This is a person saying "I have looked at these, send them" — so it
    deliberately submits plays still inside the hold that the background sweep
    would leave alone.
    """
    return await lastfm.flush(release_held=True)


@app.get("/api/stats")
async def get_stats(period: str = "month", year: int | None = None,
                    month: int | None = None):
    try:
        return await asyncio.to_thread(stats.compute_stats, period, year, month)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})


@app.get("/api/events")
def get_events(limit: int = 100):
    """Recent engine diagnostics, newest first — what the Settings page shows
    so a stalled input or a run of failed identifications is visible without
    shell access to the host."""
    limit = max(1, min(int(limit), 200))
    return {"events": list(events)[-limit:][::-1]}


@app.get("/api/status")
def get_status():
    """Last-known engine status, in the shape the Home Assistant integration
    polls. Defaults to a 'stopped' payload when the engine hasn't reported."""
    return manager.last_status


# --- WebSocket ---

@app.websocket("/ws/live-status")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

// shell.js — loaded on every page from _layout.html.
// Owns the single WebSocket connection, the engine-status pill, and a tiny
// pub/sub so page-specific scripts can subscribe to live frames.
(function () {
  const BACKOFF_MS = [1000, 2000, 5000, 10000];
  // One vocabulary for the whole app. The pill used to say "Listening" while
  // the dashboard said "Idle" for the very same state, which read as if the
  // engine could hear something when nothing was on the platter.
  //   waiting   - nothing playing, watching the input level
  //   listening - capturing a sample from the record
  //   analyzing - the sample is with the recognizer
  const LABELS = {
    idle:         "Waiting",
    listening:    "Waiting",
    scanning:     "Listening",
    identifying:  "Analyzing",
    retrying:     "Retrying",
    no_match:     "No match",
    playing:      "Playing",
    disconnected: "Disconnected",
  };

  const subscribers = new Set();
  let attempt = 0;
  let ws = null;

  function setPillState(state) {
    const label = LABELS[state] || LABELS.idle;
    document.querySelectorAll(".engine-pill").forEach((el) => {
      el.dataset.state = state;
      const labelEl = el.querySelector(".engine-pill-label");
      if (labelEl) labelEl.textContent = label;
    });
  }

  // What's playing, in text, for the pages that don't show the record. The
  // dashboard has the whole vinyl and metadata block, so it opts out via
  // data-now-playing="off" rather than saying the same thing twice.
  function setNowPlaying(track, playing) {
    document.querySelectorAll(".engine-now-playing").forEach((el) => {
      if (el.dataset.nowPlaying === "off") return;
      if (!playing || !track || !track.title) {
        el.textContent = "";
        el.classList.add("hidden");
        return;
      }
      const parts = [track.title, track.artist, track.album].filter(Boolean);
      el.textContent = parts.join(" — ");
      el.classList.remove("hidden");
    });
  }

  function notify(payload) {
    subscribers.forEach((cb) => {
      try { cb(payload); } catch (e) { console.error("frame subscriber error:", e); }
    });
  }

  function handleFrame(payload) {
    const track = (payload && payload.track) || {};
    const phase = payload && payload.phase;
    const state = phase
      ? (phase === "stopped" ? "listening" : phase)
      : (track.title ? "playing" : "listening");
    setPillState(state);
    // Keep the caption up during scanning and retries: the record is still on
    // the platter, and blanking it every time we re-identify would flicker.
    setNowPlaying(track, state !== "listening" && state !== "no_match");
    notify(payload);
  }

  function scheduleReconnect() {
    const delay = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)];
    attempt += 1;
    setTimeout(connect, delay);
  }

  function connect() {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${window.location.host}/ws/live-status`;

    ws = new WebSocket(url);

    ws.addEventListener("open", () => {
      attempt = 0;
      // Stay in "idle" until the first frame; the engine may not have started yet.
    });

    ws.addEventListener("message", (event) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg.type === "live_status") {
          handleFrame(msg.payload || {});
        }
      } catch (e) {
        console.error("WS payload error:", e);
      }
    });

    ws.addEventListener("close", () => {
      setPillState("disconnected");
      setNowPlaying(null, false);
      scheduleReconnect();
    });

    ws.addEventListener("error", () => {
      // The "close" event will fire right after; let it handle reconnect.
      try { ws.close(); } catch (_) {}
    });
  }

  if (!window.SpinSense) window.SpinSense = {};
  window.SpinSense.onFrame  = (cb) => { subscribers.add(cb); };
  window.SpinSense.offFrame = (cb) => { subscribers.delete(cb); };

  document.addEventListener("DOMContentLoaded", () => {
    setPillState("idle");
    connect();
  });
})();

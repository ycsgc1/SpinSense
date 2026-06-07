// shell.js — loaded on every page from _layout.html.
// Owns the single WebSocket connection, the engine-status pill, and a tiny
// pub/sub so page-specific scripts can subscribe to live frames.
(function () {
  const BACKOFF_MS = [1000, 2000, 5000, 10000];
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

  function notify(payload) {
    subscribers.forEach((cb) => {
      try { cb(payload); } catch (e) { console.error("frame subscriber error:", e); }
    });
  }

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

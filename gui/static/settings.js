// settings.js — owns the /settings form: loads config + mic list, binds dirty
// state, posts changes back to /api/config, and renders the live RMS preview
// alongside the volume-threshold slider via shell.js's pub/sub.
(function () {
  const FORM = document.getElementById("settings-form");
  const TOAST = document.getElementById("settings-toast");
  const SAVE_BTN = document.getElementById("save-button");
  const MIC_SELECT = document.getElementById("mic-device");
  const THRESHOLD_SLIDER = document.getElementById("volume-threshold");
  const RMS_BAR = document.getElementById("rms-preview-bar");
  const RMS_TICK = document.getElementById("rms-threshold-tick");
  const MQTT_ENABLED = document.getElementById("mqtt-enabled");
  const MQTT_FIELDS = document.getElementById("mqtt-fields");

  // dB display range. Threshold value posted to the backend is linear RMS;
  // the slider + number input both operate in dB and we convert on the way
  // in/out.
  const DB_MIN = window.SpinSense.db.FLOOR_DB;
  const DB_MAX = 0;
  const dbUtil = window.SpinSense.db;
  const THRESHOLD_NUMBER = document.getElementById("volume-threshold-number");
  THRESHOLD_NUMBER.min = String(DB_MIN);
  THRESHOLD_SLIDER.min = String(DB_MIN);

  let dirty = false;
  let initialConfig = {};

  function getNested(obj, path) {
    return path.split(".").reduce(
      (o, k) => (o == null ? undefined : o[k]),
      obj
    );
  }

  function setNested(obj, path, value) {
    const parts = path.split(".");
    let cur = obj;
    for (let i = 0; i < parts.length - 1; i++) {
      if (cur[parts[i]] == null || typeof cur[parts[i]] !== "object") {
        cur[parts[i]] = {};
      }
      cur = cur[parts[i]];
    }
    cur[parts[parts.length - 1]] = value;
  }

  function mergeDeep(target, source) {
    for (const key of Object.keys(source)) {
      const v = source[key];
      if (v && typeof v === "object" && !Array.isArray(v)) {
        if (target[key] == null || typeof target[key] !== "object") {
          target[key] = {};
        }
        mergeDeep(target[key], v);
      } else {
        target[key] = v;
      }
    }
    return target;
  }

  function setDirty(value) {
    dirty = value;
    SAVE_BTN.disabled = !value;
  }

  function setToast(text, kind) {
    TOAST.textContent = text;
    TOAST.dataset.kind = kind || "";
  }

  // Broker fields are clutter for the mDNS-discovery majority: keep them
  // collapsed unless MQTT is actually enabled (mirrors the setup wizard).
  function syncMqttFieldsVisibility() {
    if (MQTT_ENABLED && MQTT_FIELDS) {
      MQTT_FIELDS.classList.toggle("hidden", !MQTT_ENABLED.checked);
    }
  }

  function updateThresholdTick() {
    const db = Number(THRESHOLD_SLIDER.value);
    const pct = ((db - DB_MIN) / (DB_MAX - DB_MIN)) * 100;
    RMS_TICK.style.left = pct + "%";
    if (document.activeElement !== THRESHOLD_NUMBER) {
      THRESHOLD_NUMBER.value = db.toFixed(1);
    }
  }

  function populateForm(config) {
    initialConfig = JSON.parse(JSON.stringify(config));
    FORM.querySelectorAll("[name]").forEach((el) => {
      const value = getNested(config, el.name);
      if (value === undefined || value === null) return;
      if (el === MIC_SELECT) return;
      if (el.type === "checkbox") {
        el.checked = Boolean(value);
      } else {
        el.value = value;
      }
    });
    // Threshold is stored linear; display as dB.
    const storedRms = getNested(config, "Audio.Volume_Threshold");
    if (typeof storedRms === "number") {
      const db = dbUtil.rmsToDb(storedRms);
      THRESHOLD_SLIDER.value = db.toFixed(1);
      THRESHOLD_NUMBER.value = db.toFixed(1);
    }
    updateThresholdTick();
    syncMqttFieldsVisibility();
    setDirty(false);
    setToast("");
  }

  function readForm() {
    const formObj = {};
    FORM.querySelectorAll("[name]").forEach((el) => {
      let value;
      if (el.type === "checkbox") {
        value = el.checked;
      } else if (el.type === "number" || el.type === "range") {
        value = el.value === "" ? 0 : Number(el.value);
      } else {
        value = el.value;
      }
      setNested(formObj, el.name, value);
    });
    // The threshold inputs aren't part of FORM iteration (no name attribute).
    // Convert dB back to linear RMS and write it explicitly.
    const db = Number(THRESHOLD_SLIDER.value);
    setNested(formObj, "Audio.Volume_Threshold", dbUtil.dbToRms(db));
    return mergeDeep(JSON.parse(JSON.stringify(initialConfig)), formObj);
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;",
      '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  async function loadDevices() {
    let devices = [];
    try {
      const res = await fetch("/api/devices");
      const data = await res.json();
      devices = (data && data.devices) || [];
    } catch (e) {
      // Fall through to default-only.
    }
    const current = getNested(initialConfig, "Hardware.Mic_Device") || "default";
    const options = ['<option value="default">System default</option>'];
    let currentInList = current === "default";
    for (const d of devices) {
      const name = String(d.name);
      const escaped = escapeHtml(name);
      options.push(`<option value="${escaped}">${escaped}</option>`);
      if (name === current) currentInList = true;
    }
    // If the saved mic isn't in the live device list (e.g. unplugged), show it
    // anyway so the user can see what's currently set without us silently
    // mutating their config to "default".
    if (!currentInList) {
      options.push(`<option value="${escapeHtml(current)}">${escapeHtml(current)} (not connected)</option>`);
    }
    MIC_SELECT.innerHTML = options.join("");
    MIC_SELECT.value = current;
  }

  async function loadConfig() {
    try {
      const res = await fetch("/api/config");
      const cfg = await res.json();
      populateForm(cfg);
      await loadDevices();
    } catch (e) {
      setToast("Failed to load config: " + e.message, "error");
    }
  }

  async function onSubmit(ev) {
    ev.preventDefault();
    if (!dirty) return;
    SAVE_BTN.disabled = true;
    setToast("Saving…");
    const payload = readForm();
    try {
      const res = await fetch("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        setToast(body.detail || `Save failed (${res.status})`, "error");
        SAVE_BTN.disabled = false;
        return;
      }
      initialConfig = JSON.parse(JSON.stringify(payload));
      setDirty(false);
      setToast("Saved and applied", "ok");
      setTimeout(() => {
        if (!dirty && TOAST.dataset.kind === "ok") setToast("");
      }, 2500);
    } catch (e) {
      setToast("Network error: " + e.message, "error");
      SAVE_BTN.disabled = false;
    }
  }

  // ---------- Last.fm ----------
  //
  // Default path: "Connect" sends the browser to Last.fm, which signs the user
  // in, takes their approval, and redirects to /api/lastfm/callback — the server
  // finishes there and bounces back to /settings?lastfm=... So this page is
  // *left* mid-flow and re-entered fresh; nothing needs to survive in memory.
  //
  // Fallback path, for setups where that redirect can't return: the server also
  // hands back a manual approval URL backed by a token it holds, and
  // "I've approved it" trades it in. Same auth.getSession at the end.

  const LF_STATE = document.getElementById("lastfm-state");
  const LF_SETUP = document.getElementById("lastfm-setup");
  const LF_OWN_KEY = document.getElementById("lastfm-own-key");
  const LF_MANUAL = document.getElementById("lastfm-manual");
  const LF_MANUAL_LINK = document.getElementById("lastfm-manual-link");
  const LF_KEY = document.getElementById("lastfm-api-key");
  const LF_SECRET = document.getElementById("lastfm-api-secret");
  const LF_CONNECT = document.getElementById("lastfm-connect");
  const LF_FINISH = document.getElementById("lastfm-finish");
  const LF_TOAST = document.getElementById("lastfm-toast");

  // The server flips LastFM.Enabled during connect/disconnect, so the form is
  // stale afterwards. Repopulating it would discard any unsaved edits elsewhere
  // on the page, so when the form is dirty we say so instead of stomping it.
  async function reloadConfigIfClean(warning) {
    if (dirty) {
      lfToast(warning, "ok");
      return;
    }
    await loadConfig();
  }

  function lfToast(text, kind) {
    if (!LF_TOAST) return;
    LF_TOAST.textContent = text || "";
    LF_TOAST.dataset.kind = kind || "";
  }

  function renderLastFm(status) {
    if (!LF_STATE) return;
    if (status.connected) {
      const pending = status.pending || 0;
      const queued = pending === 0
        ? "Nothing waiting to be sent."
        : `${pending} play${pending === 1 ? "" : "s"} waiting to be sent.`;
      LF_STATE.innerHTML = `
        <div class="flex items-center justify-between gap-md flex-wrap">
          <div>
            <p class="text-body-md text-on-surface">Connected as
              <strong>${escapeHtml(status.username || "?")}</strong></p>
            <p class="text-body-sm text-on-surface-variant">${queued}</p>
          </div>
          <div class="flex gap-2">
            <button type="button" id="lastfm-flush"
                    class="bg-surface-container-high border border-outline-variant/40 text-on-surface font-medium px-md py-2 rounded-full hover:bg-surface-container-highest transition-colors">
              Send now
            </button>
            <button type="button" id="lastfm-disconnect"
                    class="bg-surface-container-high border border-outline-variant/40 text-on-surface font-medium px-md py-2 rounded-full hover:bg-surface-container-highest transition-colors">
              Disconnect
            </button>
          </div>
        </div>`;
      LF_SETUP.classList.add("hidden");
      document.getElementById("lastfm-flush").addEventListener("click", flushLastFm);
      document.getElementById("lastfm-disconnect").addEventListener("click", disconnectLastFm);
    } else {
      LF_STATE.innerHTML =
        '<p class="text-body-sm text-on-surface-variant">Not connected. ' +
        'Scrobbling is off until you link an account.</p>';
      LF_SETUP.classList.remove("hidden");
      if (status.using_own_key) {
        // Their own keys are already saved; don't make them hunt them down
        // again, but show the section so it's clear an override is in effect.
        LF_KEY.placeholder = "(saved)";
        LF_SECRET.placeholder = "(saved)";
        if (LF_OWN_KEY) LF_OWN_KEY.open = true;
      }
      if (status.can_connect === false) {
        // A build with no bundled application key. Supplying one isn't
        // optional here, so lead with it rather than hiding it away.
        if (LF_OWN_KEY) LF_OWN_KEY.open = true;
        lfToast("This build has no Last.fm application key — supply your own below.",
                "error");
      }
    }
  }

  async function refreshLastFm() {
    try {
      const res = await fetch("/api/lastfm/status");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      renderLastFm(await res.json());
    } catch (e) {
      if (LF_STATE) {
        LF_STATE.innerHTML =
          '<p class="text-body-sm text-on-surface-variant">Couldn&rsquo;t read Last.fm status.</p>';
      }
    }
  }

  async function connectLastFm() {
    // Both blank is the normal path: the server uses the application key
    // SpinSense ships with. Only a deliberate override sends anything.
    const api_key = LF_KEY.value.trim();
    const api_secret = LF_SECRET.value.trim();
    if ((api_key || api_secret) && !(api_key && api_secret)) {
      lfToast("Supply both the API key and the shared secret, or neither.", "error");
      if (LF_OWN_KEY) LF_OWN_KEY.open = true;
      return;
    }
    LF_CONNECT.disabled = true;
    lfToast("Checking your credentials with Last.fm…");
    try {
      const res = await fetch("/api/lastfm/auth/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // The server can't know which address we reached it on — a LAN box has
        // no fixed one — and Last.fm needs somewhere to send us back to.
        body: JSON.stringify({ api_key, api_secret, origin: window.location.origin }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        lfToast(body.detail || `Failed (${res.status})`, "error");
        LF_CONNECT.disabled = false;
        return;
      }

      // Reveal the fallback before leaving, so it's already there if the user
      // comes back by hand rather than by redirect.
      if (body.manual_url && LF_MANUAL && LF_MANUAL_LINK) {
        LF_MANUAL_LINK.href = body.manual_url;
        LF_MANUAL.classList.remove("hidden");
      }

      if (body.auth_url) {
        lfToast("Taking you to Last.fm…");
        window.location.href = body.auth_url;   // we'll be redirected back
        return;
      }

      // No usable callback (unrecognisable origin) — manual is the only route.
      if (LF_MANUAL) LF_MANUAL.open = true;
      lfToast("Couldn't build a return address for this URL — approve manually below.",
              "error");
      LF_CONNECT.disabled = false;
    } catch (e) {
      lfToast("Network error: " + e.message, "error");
      LF_CONNECT.disabled = false;
    }
  }

  async function finishLastFm() {
    LF_FINISH.disabled = true;
    lfToast("Finishing…");
    try {
      const res = await fetch("/api/lastfm/auth/complete", { method: "POST" });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        // Usually "Unauthorized Token" — they haven't clicked Allow yet.
        lfToast(body.detail || `Failed (${res.status})`, "error");
        return;
      }
      if (LF_MANUAL) LF_MANUAL.classList.add("hidden");
      LF_KEY.value = "";
      LF_SECRET.value = "";
      lfToast(`Connected as ${body.username}.`, "ok");
      await refreshLastFm();
      await reloadConfigIfClean("Connected — reload the page to see the scrobbling toggles update.");
    } catch (e) {
      lfToast("Network error: " + e.message, "error");
    } finally {
      LF_FINISH.disabled = false;
    }
  }

  async function disconnectLastFm() {
    if (!window.confirm("Disconnect this Last.fm account? Your API key stays saved.")) return;
    try {
      await fetch("/api/lastfm/disconnect", { method: "POST" });
      lfToast("Disconnected.", "ok");
      await refreshLastFm();
      await reloadConfigIfClean("Disconnected — reload the page to see the scrobbling toggles update.");
    } catch (e) {
      lfToast("Network error: " + e.message, "error");
    }
  }

  async function flushLastFm() {
    lfToast("Sending…");
    try {
      const res = await fetch("/api/lastfm/flush", { method: "POST" });
      const body = await res.json().catch(() => ({}));
      lfToast(body.detail || (body.ok ? "Sent." : "Failed."), body.ok ? "ok" : "error");
      await refreshLastFm();
    } catch (e) {
      lfToast("Network error: " + e.message, "error");
    }
  }

  // The redirect flow finishes server-side and bounces back here with the
  // outcome in the query string. Report it, then strip it so a refresh doesn't
  // re-announce a connection that happened minutes ago.
  function reportRedirectOutcome() {
    const params = new URLSearchParams(window.location.search);
    const outcome = params.get("lastfm");
    if (!outcome) return;
    if (outcome === "connected") {
      lfToast(`Connected as ${params.get("user") || "your account"}.`, "ok");
    } else if (outcome === "denied") {
      lfToast("Last.fm authorisation was declined.", "error");
    } else {
      lfToast(params.get("detail") || "Last.fm authorisation failed.", "error");
    }
    window.history.replaceState({}, "", window.location.pathname);
  }

  if (LF_CONNECT) LF_CONNECT.addEventListener("click", connectLastFm);
  if (LF_FINISH) LF_FINISH.addEventListener("click", finishLastFm);

  // Wire up RMS preview off the shell's WS pub/sub.
  if (window.SpinSense && typeof window.SpinSense.onFrame === "function") {
    window.SpinSense.onFrame((payload) => {
      const rms = payload && typeof payload.rms_level === "number" ? payload.rms_level : 0;
      const db = dbUtil.rmsToDb(rms);
      const pct = Math.max(0, Math.min(100, ((db - DB_MIN) / (DB_MAX - DB_MIN)) * 100));
      RMS_BAR.style.width = pct + "%";
    });
  }

  THRESHOLD_SLIDER.addEventListener("input", () => {
    updateThresholdTick();
    setDirty(true);
  });

  THRESHOLD_NUMBER.addEventListener("input", () => {
    const v = Math.max(DB_MIN, Math.min(DB_MAX, Number(THRESHOLD_NUMBER.value)));
    THRESHOLD_SLIDER.value = v;
    updateThresholdTick();
    setDirty(true);
  });

  FORM.addEventListener("input", (ev) => {
    // Don't double-fire dirty for the slider's input (handled above) or for
    // events that didn't actually change a value.
    if (ev.target === THRESHOLD_SLIDER) return;
    if (ev.target === THRESHOLD_NUMBER) return;
    setDirty(true);
  });

  // Checkboxes emit `change`, not `input` — wire dirty tracking for them.
  FORM.addEventListener("change", (ev) => {
    if (ev.target.type === "checkbox" || ev.target.tagName === "SELECT") setDirty(true);
    if (ev.target === MQTT_ENABLED) syncMqttFieldsVisibility();
  });

  FORM.addEventListener("submit", onSubmit);

  // MQTT test connection
  const MQTT_TEST_BTN = document.getElementById("mqtt-test");
  const MQTT_TEST_STATUS = document.getElementById("mqtt-test-status");
  if (MQTT_TEST_BTN && MQTT_TEST_STATUS) {
    MQTT_TEST_BTN.addEventListener("click", async () => {
      MQTT_TEST_BTN.disabled = true;
      MQTT_TEST_STATUS.textContent = "Testing…";
      MQTT_TEST_STATUS.dataset.kind = "";
      const formData = readForm();
      const broker = (formData.MQTT && formData.MQTT.Broker) || {};
      try {
        const res = await fetch("/api/mqtt/test", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            host: broker.Host || "",
            port: broker.Port || 1883,
            user: broker.User || "",
            password: broker.Password || "",
          }),
        });
        const body = await res.json().catch(() => ({}));
        if (res.ok && body.ok) {
          MQTT_TEST_STATUS.textContent = "Connected ✓";
          MQTT_TEST_STATUS.dataset.kind = "ok";
        } else {
          MQTT_TEST_STATUS.textContent = body.detail || `Failed (${res.status})`;
          MQTT_TEST_STATUS.dataset.kind = "error";
        }
      } catch (e) {
        MQTT_TEST_STATUS.textContent = "Network error: " + e.message;
        MQTT_TEST_STATUS.dataset.kind = "error";
      } finally {
        MQTT_TEST_BTN.disabled = false;
      }
    });
  }

  window.addEventListener("beforeunload", (e) => {
    if (dirty) {
      e.preventDefault();
      e.returnValue = "";
    }
  });

  loadConfig();
  refreshLastFm();
  reportRedirectOutcome();
})();

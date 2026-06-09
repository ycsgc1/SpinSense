// setup.js — multi-step wizard. Owns step navigation, the mic + threshold +
// MQTT form fields, MQTT test-connection, skip/finish/close flow. Saves via
// POST /api/config like Settings does.
(function () {
  const STEPS = Array.from(document.querySelectorAll(".wizard-step"));
  const DOTS = Array.from(document.querySelectorAll(".wizard-dot"));
  const CLOSE_BTN = document.getElementById("wizard-close");

  const MIC = document.getElementById("wizard-mic");

  // Step 2 — auto path elements
  const AUTO_BTN = document.getElementById("calibrate-auto-btn");
  const MANUAL_BTN = document.getElementById("calibrate-manual-btn");
  const AUTO_WARNING = document.getElementById("calibrate-auto-warning");

  const NOISE_START = document.getElementById("calibrate-noise-start");
  const NOISE_STATUS = document.getElementById("calibrate-noise-status");
  const NOISE_BAR = document.getElementById("calibrate-noise-bar");

  const MUSIC_START = document.getElementById("calibrate-music-start");
  const MUSIC_STATUS = document.getElementById("calibrate-music-status");
  const MUSIC_BAR = document.getElementById("calibrate-music-bar");

  const RESULT_HEADLINE = document.getElementById("calibrate-result-headline");
  const RESULT_NOISE = document.getElementById("calibrate-result-noise");
  const RESULT_MUSIC = document.getElementById("calibrate-result-music");
  const RESULT_THRESHOLD = document.getElementById("calibrate-result-threshold");
  const RERUN_BTN = document.getElementById("calibrate-rerun");

  // Step 2 — result-screen slider (auto path) and manual-screen slider.
  // Both operate in dB; the engine value in config is linear RMS.
  const THRESHOLD = document.getElementById("wizard-threshold");
  const THRESHOLD_NUMBER = document.getElementById("wizard-threshold-number");
  const RMS_BAR = document.getElementById("wizard-rms-bar");
  const RMS_TICK = document.getElementById("wizard-rms-tick");

  const THRESHOLD_MANUAL = document.getElementById("wizard-threshold-manual");
  const THRESHOLD_MANUAL_NUMBER = document.getElementById("wizard-threshold-manual-number");
  const RMS_BAR_MANUAL = document.getElementById("wizard-rms-bar-manual");
  const RMS_TICK_MANUAL = document.getElementById("wizard-rms-tick-manual");

  const MQTT_HOST = document.getElementById("wizard-mqtt-host");
  const MQTT_PORT = document.getElementById("wizard-mqtt-port");
  const MQTT_USER = document.getElementById("wizard-mqtt-user");
  const MQTT_PASS = document.getElementById("wizard-mqtt-pass");
  const MQTT_TEST = document.getElementById("wizard-mqtt-test");
  const MQTT_STATUS = document.getElementById("wizard-mqtt-status");
  const MQTT_SKIP = document.getElementById("wizard-mqtt-skip");
  const MDNS_ENABLED = document.getElementById("wizard-mdns-enabled");
  const MQTT_ENABLED = document.getElementById("wizard-mqtt-enabled");
  const MQTT_FIELDS = document.getElementById("wizard-mqtt-fields");

  const POPUP = document.getElementById("wizard-mqtt-popup");
  const POPUP_DETAIL = document.getElementById("wizard-mqtt-popup-detail");
  const POPUP_RETRY = document.getElementById("wizard-mqtt-popup-retry");
  const POPUP_SKIP = document.getElementById("wizard-mqtt-popup-skip");

  const FINISH_BTN = document.getElementById("wizard-finish");

  const DB_MIN = window.SpinSense.db.FLOOR_DB;
  const DB_MAX = 0;
  const dbUtil = window.SpinSense.db;
  // Which slider holds the canonical threshold value for save. "result" = auto
  // path slider on Screen 2D; "manual" = Screen 2E slider.
  let activeSlider = "result";
  let captures = { noise: null, music: null };
  let currentSubstep = "choose";
  let captureAbortKey = 0; // bumped on cancel to invalidate in-flight polls

  let step = 0;
  let initialConfig = {};

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;",
      '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function showStep(n) {
    STEPS.forEach((el) => {
      const i = Number(el.dataset.step);
      el.classList.toggle("hidden", i !== n);
    });
    DOTS.forEach((el) => {
      const i = Number(el.dataset.step);
      el.dataset.active = i === n ? "1" : "0";
    });
    step = n;
  }

  function placeTick(tickEl, db) {
    if (!tickEl) return;
    const pct = ((db - DB_MIN) / (DB_MAX - DB_MIN)) * 100;
    tickEl.style.left = Math.max(0, Math.min(100, pct)) + "%";
  }

  function syncThresholdControls(which, db) {
    const slider = which === "manual" ? THRESHOLD_MANUAL : THRESHOLD;
    const number = which === "manual" ? THRESHOLD_MANUAL_NUMBER : THRESHOLD_NUMBER;
    const tick = which === "manual" ? RMS_TICK_MANUAL : RMS_TICK;
    const clamped = Math.max(DB_MIN, Math.min(DB_MAX, db));
    slider.value = clamped.toFixed(1);
    if (document.activeElement !== number) {
      number.value = clamped.toFixed(1);
    }
    placeTick(tick, clamped);
  }

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
    if (!currentInList) {
      options.push(`<option value="${escapeHtml(current)}">${escapeHtml(current)} (not connected)</option>`);
    }
    MIC.innerHTML = options.join("");
    MIC.value = current;
  }

  async function loadConfig() {
    try {
      const res = await fetch("/api/config");
      initialConfig = await res.json();
    } catch (e) {
      console.error("Wizard: failed to fetch config", e);
    }
    // Apply config to the form defensively. A failure here (e.g. a missing
    // element from a stale/cached asset) must never block the mic list below,
    // so this is isolated from loadDevices().
    try {
      const storedRms = getNested(initialConfig, "Audio.Volume_Threshold") ?? 0.01;
      const storedDb = dbUtil.rmsToDb(storedRms);
      syncThresholdControls("result", storedDb);
      syncThresholdControls("manual", storedDb);
      MQTT_HOST.value = getNested(initialConfig, "MQTT.Broker.Host") ?? "";
      MQTT_PORT.value = getNested(initialConfig, "MQTT.Broker.Port") ?? 1883;
      MQTT_USER.value = getNested(initialConfig, "MQTT.Broker.User") ?? "";
      MQTT_PASS.value = getNested(initialConfig, "MQTT.Broker.Password") ?? "";
      if (MDNS_ENABLED) MDNS_ENABLED.checked = getNested(initialConfig, "Discovery.mDNS.Enabled") ?? true;
      if (MQTT_ENABLED) MQTT_ENABLED.checked = getNested(initialConfig, "MQTT.Enabled") ?? false;
      if (MQTT_FIELDS) MQTT_FIELDS.classList.toggle("hidden", !(MQTT_ENABLED && MQTT_ENABLED.checked));
    } catch (e) {
      console.error("Wizard: failed to apply config to form", e);
    }
    await loadDevices();
  }

  function buildPayload({ state }) {
    const payload = JSON.parse(JSON.stringify(initialConfig || {}));
    setNested(payload, "Hardware.Mic_Device", MIC.value || "default");
    const sliderDb = Number(
      activeSlider === "manual" ? THRESHOLD_MANUAL.value : THRESHOLD.value
    );
    setNested(payload, "Audio.Volume_Threshold", dbUtil.dbToRms(sliderDb));
    setNested(payload, "Discovery.mDNS.Enabled", !!(MDNS_ENABLED && MDNS_ENABLED.checked));
    const mqttOn = !!(MQTT_ENABLED && MQTT_ENABLED.checked);
    setNested(payload, "MQTT.Enabled", mqttOn);
    if (mqttOn) {
      setNested(payload, "MQTT.Broker.Host", MQTT_HOST.value);
      setNested(payload, "MQTT.Broker.Port", Number(MQTT_PORT.value || 1883));
      setNested(payload, "MQTT.Broker.User", MQTT_USER.value);
      setNested(payload, "MQTT.Broker.Password", MQTT_PASS.value);
    }
    setNested(payload, "System.Setup_Wizard_State", state);
    return payload;
  }

  async function saveAndNavigate(state) {
    const payload = buildPayload({ state });
    const res = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      alert("Save failed: " + (body.detail || res.status));
      return false;
    }
    return true;
  }

  function setMqttStatus(text, kind) {
    MQTT_STATUS.textContent = text;
    MQTT_STATUS.dataset.kind = kind || "";
  }

  function openPopup(detail) {
    POPUP_DETAIL.textContent = detail;
    POPUP.classList.remove("hidden");
  }
  function closePopup() {
    POPUP.classList.add("hidden");
  }

  async function testMqtt() {
    setMqttStatus("Testing…", "");
    MQTT_TEST.disabled = true;
    try {
      const res = await fetch("/api/mqtt/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          host: MQTT_HOST.value,
          port: Number(MQTT_PORT.value || 1883),
          user: MQTT_USER.value,
          password: MQTT_PASS.value,
        }),
      });
      const body = await res.json().catch(() => ({}));
      if (res.ok && body.ok) {
        setMqttStatus("Connected ✓", "ok");
      } else {
        setMqttStatus("", "");
        openPopup(body.detail || `HTTP ${res.status}`);
      }
    } catch (e) {
      setMqttStatus("", "");
      openPopup("Network error: " + e.message);
    } finally {
      MQTT_TEST.disabled = false;
    }
  }

  // --- Event wiring ---
  document.querySelectorAll("[data-wizard-next]").forEach((b) => {
    b.addEventListener("click", () => showStep(Math.min(step + 1, STEPS.length - 1)));
  });
  document.querySelectorAll("[data-wizard-back]").forEach((b) => {
    b.addEventListener("click", () => showStep(Math.max(step - 1, 0)));
  });
  document.querySelectorAll("[data-wizard-skip]").forEach((b) => {
    b.addEventListener("click", async () => {
      captureAbortKey++;
      clearCalibrationBestEffort();
      if (await saveAndNavigate("skipped")) window.location.href = "/";
    });
  });

  CLOSE_BTN.addEventListener("click", () => {
    captureAbortKey++;
    clearCalibrationBestEffort();
    window.location.href = "/";
  });

  if (MQTT_ENABLED && MQTT_FIELDS) {
    MQTT_ENABLED.addEventListener("change", () => {
      MQTT_FIELDS.classList.toggle("hidden", !MQTT_ENABLED.checked);
    });
  }

  MQTT_TEST.addEventListener("click", testMqtt);
  MQTT_SKIP.addEventListener("click", () => {
    showStep(step + 1);
  });

  POPUP_RETRY.addEventListener("click", () => {
    closePopup();
    testMqtt();
  });
  POPUP_SKIP.addEventListener("click", () => {
    closePopup();
    showStep(step + 1);
  });

  FINISH_BTN.addEventListener("click", async () => {
    FINISH_BTN.disabled = true;
    if (await saveAndNavigate("completed")) {
      window.location.href = "/";
    } else {
      FINISH_BTN.disabled = false;
    }
  });

  // Live RMS preview on the threshold step.
  if (window.SpinSense && typeof window.SpinSense.onFrame === "function") {
    window.SpinSense.onFrame((payload) => {
      const rms = payload && typeof payload.rms_level === "number" ? payload.rms_level : 0;
      const db = dbUtil.rmsToDb(rms);
      const pct = Math.max(0, Math.min(100, ((db - DB_MIN) / (DB_MAX - DB_MIN)) * 100));
      const widthStr = pct + "%";
      if (NOISE_BAR) NOISE_BAR.style.width = widthStr;
      if (MUSIC_BAR) MUSIC_BAR.style.width = widthStr;
      if (RMS_BAR) RMS_BAR.style.width = widthStr;
      if (RMS_BAR_MANUAL) RMS_BAR_MANUAL.style.width = widthStr;
    });
  }

  function showSubstep(name) {
    currentSubstep = name;
    document.querySelectorAll(".wizard-substep").forEach((el) => {
      el.classList.toggle("hidden", el.dataset.substep !== name);
    });
    if (name === "manual") activeSlider = "manual";
    if (name === "result") activeSlider = "result";
  }

  async function checkEngineReachable() {
    try {
      const res = await fetch("/api/calibrate/status");
      if (res.status === 503) return false;
      return res.ok;
    } catch (_) {
      return false;
    }
  }

  async function clearCalibrationBestEffort() {
    try { await fetch("/api/calibrate/clear", { method: "POST" }); } catch (_) {}
  }

  function applyThresholdFormula(noiseStats, musicStats) {
    // Threshold = noise_p99 + 0.25 * (music_p10 - noise_p99), all in dB.
    // Safety: 2 dB minimum gap above noise; clamp to display range.
    const noiseDb = dbUtil.rmsToDb(noiseStats.p99);
    const musicDb = dbUtil.rmsToDb(musicStats.p10);
    if (noiseDb >= musicDb) {
      return { ok: false, noiseDb, musicDb };
    }
    let thresholdDb = noiseDb + 0.25 * (musicDb - noiseDb);
    if (thresholdDb < noiseDb + 2) thresholdDb = noiseDb + 2;
    thresholdDb = Math.max(DB_MIN, Math.min(DB_MAX, thresholdDb));
    return { ok: true, noiseDb, musicDb, thresholdDb };
  }

  async function runCapture(phase, statusEl, startBtn) {
    const myKey = ++captureAbortKey;
    startBtn.disabled = true;
    statusEl.textContent = "Starting…";

    let startReply;
    try {
      const res = await fetch("/api/calibrate/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ phase }),
      });
      startReply = await res.json().catch(() => ({}));
      if (!res.ok || !startReply.ok) {
        statusEl.textContent = (startReply && startReply.detail) || `Start failed (${res.status})`;
        startBtn.disabled = false;
        return null;
      }
    } catch (e) {
      statusEl.textContent = "Network error: " + e.message;
      startBtn.disabled = false;
      return null;
    }

    const duration = (startReply.duration_s || 5) * 1000;
    const startedAt = Date.now();
    while (Date.now() - startedAt < duration) {
      if (captureAbortKey !== myKey) return null;
      const remaining = Math.ceil((duration - (Date.now() - startedAt)) / 1000);
      statusEl.textContent = `Capturing… ${remaining}s`;
      await new Promise((r) => setTimeout(r, 250));
    }

    // Poll status up to 3s extra slack for the engine's timer task to finish.
    statusEl.textContent = "Finishing…";
    const deadline = Date.now() + 3000;
    while (Date.now() < deadline) {
      if (captureAbortKey !== myKey) return null;
      try {
        const res = await fetch("/api/calibrate/status");
        const body = await res.json().catch(() => ({}));
        if (body.status === "done" && body.stats) {
          startBtn.disabled = false;
          statusEl.textContent = `Captured ${body.samples_count} samples.`;
          return body.stats;
        }
      } catch (_) {}
      await new Promise((r) => setTimeout(r, 250));
    }
    statusEl.textContent = "Capture timed out — try again.";
    startBtn.disabled = false;
    return null;
  }

  async function runNoiseCapture() {
    const stats = await runCapture("noise_floor", NOISE_STATUS, NOISE_START);
    if (!stats) return;
    captures.noise = stats;
    showSubstep("music_capture");
  }

  async function runMusicCapture() {
    const stats = await runCapture("music", MUSIC_STATUS, MUSIC_START);
    if (!stats) return;
    captures.music = stats;

    const result = applyThresholdFormula(captures.noise, captures.music);
    if (!result.ok) {
      MUSIC_STATUS.textContent = "Your silence sample was as loud as your music sample. Try again — make sure a song is actually playing.";
      return; // user can click the capture button again to retry music phase only
    }

    RESULT_HEADLINE.textContent = dbUtil.formatDb(result.thresholdDb);
    RESULT_NOISE.textContent = dbUtil.formatDb(result.noiseDb);
    RESULT_MUSIC.textContent = dbUtil.formatDb(result.musicDb);
    RESULT_THRESHOLD.textContent = dbUtil.formatDb(result.thresholdDb);
    syncThresholdControls("result", result.thresholdDb);
    showSubstep("result");
    clearCalibrationBestEffort();
  }

  AUTO_BTN.addEventListener("click", async () => {
    AUTO_WARNING.classList.add("hidden");
    const reachable = await checkEngineReachable();
    if (!reachable) {
      AUTO_WARNING.textContent = "Audio engine not running — restart the container or use 'Set manually'.";
      AUTO_WARNING.classList.remove("hidden");
      return;
    }
    captures = { noise: null, music: null };
    showSubstep("noise_capture");
  });

  MANUAL_BTN.addEventListener("click", () => {
    activeSlider = "manual";
    showSubstep("manual");
  });

  NOISE_START.addEventListener("click", runNoiseCapture);
  MUSIC_START.addEventListener("click", runMusicCapture);

  RERUN_BTN.addEventListener("click", () => {
    captures = { noise: null, music: null };
    showSubstep("noise_capture");
  });

  document.querySelectorAll("[data-substep-back]").forEach((b) => {
    b.addEventListener("click", () => {
      captureAbortKey++;
      clearCalibrationBestEffort();
      showSubstep(b.dataset.substepBack);
    });
  });
  document.querySelectorAll("[data-substep-goto]").forEach((b) => {
    b.addEventListener("click", () => showSubstep(b.dataset.substepGoto));
  });

  // Threshold control wiring for both sliders.
  THRESHOLD.addEventListener("input", () => {
    syncThresholdControls("result", Number(THRESHOLD.value));
  });
  THRESHOLD_NUMBER.addEventListener("input", () => {
    syncThresholdControls("result", Number(THRESHOLD_NUMBER.value));
  });
  THRESHOLD_MANUAL.addEventListener("input", () => {
    syncThresholdControls("manual", Number(THRESHOLD_MANUAL.value));
  });
  THRESHOLD_MANUAL_NUMBER.addEventListener("input", () => {
    syncThresholdControls("manual", Number(THRESHOLD_MANUAL_NUMBER.value));
  });

  // Always start Step 2 on the chooser.
  showSubstep("choose");

  showStep(0);
  loadConfig();
})();

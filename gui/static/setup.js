// setup.js — multi-step wizard. Owns step navigation, the mic + threshold +
// MQTT form fields, MQTT test-connection, skip/finish/close flow. Saves via
// POST /api/config like Settings does.
(function () {
  const STEPS = Array.from(document.querySelectorAll(".wizard-step"));
  const DOTS = Array.from(document.querySelectorAll(".wizard-dot"));
  const CLOSE_BTN = document.getElementById("wizard-close");

  const MIC = document.getElementById("wizard-mic");
  // const THRESHOLD = document.getElementById("wizard-threshold");
  // const THRESHOLD_VALUE = document.getElementById("wizard-threshold-value");
  // const RMS_BAR = document.getElementById("wizard-rms-bar");
  // const RMS_TICK = document.getElementById("wizard-rms-tick");

  const MQTT_HOST = document.getElementById("wizard-mqtt-host");
  const MQTT_PORT = document.getElementById("wizard-mqtt-port");
  const MQTT_USER = document.getElementById("wizard-mqtt-user");
  const MQTT_PASS = document.getElementById("wizard-mqtt-pass");
  const MQTT_TEST = document.getElementById("wizard-mqtt-test");
  const MQTT_STATUS = document.getElementById("wizard-mqtt-status");
  const MQTT_SKIP = document.getElementById("wizard-mqtt-skip");

  const POPUP = document.getElementById("wizard-mqtt-popup");
  const POPUP_DETAIL = document.getElementById("wizard-mqtt-popup-detail");
  const POPUP_RETRY = document.getElementById("wizard-mqtt-popup-retry");
  const POPUP_SKIP = document.getElementById("wizard-mqtt-popup-skip");

  const FINISH_BTN = document.getElementById("wizard-finish");

  const RMS_CEILING = 0.05;

  let step = 0;
  let initialConfig = {};
  let skipMqtt = false;

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

  function updateThresholdTick() {
    // const t = Number(THRESHOLD.value);
    // const pct = Math.min(100, (t / RMS_CEILING) * 100);
    // RMS_TICK.style.left = pct + "%";
    // THRESHOLD_VALUE.textContent = t.toFixed(4);
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
      // THRESHOLD.value = getNested(initialConfig, "Audio.Volume_Threshold") ?? 0.0062;
      MQTT_HOST.value = getNested(initialConfig, "MQTT.Broker.Host") ?? "";
      MQTT_PORT.value = getNested(initialConfig, "MQTT.Broker.Port") ?? 1883;
      MQTT_USER.value = getNested(initialConfig, "MQTT.Broker.User") ?? "";
      MQTT_PASS.value = getNested(initialConfig, "MQTT.Broker.Password") ?? "";
      updateThresholdTick();
      await loadDevices();
    } catch (e) {
      console.error("Wizard: failed to load config", e);
    }
  }

  function buildPayload({ state }) {
    const payload = JSON.parse(JSON.stringify(initialConfig || {}));
    setNested(payload, "Hardware.Mic_Device", MIC.value || "default");
    // setNested(payload, "Audio.Volume_Threshold", Number(THRESHOLD.value));
    if (!skipMqtt) {
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
      if (await saveAndNavigate("skipped")) window.location.href = "/";
    });
  });

  CLOSE_BTN.addEventListener("click", () => {
    // X = leave state as-is; just navigate away. If state was "pending" the
    // middleware will redirect back to /setup on the next page hit.
    window.location.href = "/";
  });

  // THRESHOLD.addEventListener("input", updateThresholdTick);

  MQTT_TEST.addEventListener("click", testMqtt);
  MQTT_SKIP.addEventListener("click", () => {
    skipMqtt = true;
    showStep(step + 1);
  });

  POPUP_RETRY.addEventListener("click", () => {
    closePopup();
    testMqtt();
  });
  POPUP_SKIP.addEventListener("click", () => {
    closePopup();
    skipMqtt = true;
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
  // if (window.SpinSense && typeof window.SpinSense.onFrame === "function") {
  //   window.SpinSense.onFrame((payload) => {
  //     const rms = payload && typeof payload.rms_level === "number" ? payload.rms_level : 0;
  //     const pct = Math.min(100, Math.max(0, (rms / RMS_CEILING) * 100));
  //     RMS_BAR.style.width = pct + "%";
  //   });
  // }

  // Step 2 substep router. Full capture orchestration lands in the next task.
  function showSubstep(name) {
    document.querySelectorAll(".wizard-substep").forEach((el) => {
      el.classList.toggle("hidden", el.dataset.substep !== name);
    });
  }
  document.querySelectorAll("[data-substep-back]").forEach((b) => {
    b.addEventListener("click", () => showSubstep(b.dataset.substepBack));
  });
  document.querySelectorAll("[data-substep-goto]").forEach((b) => {
    b.addEventListener("click", () => showSubstep(b.dataset.substepGoto));
  });
  document.getElementById("calibrate-auto-btn").addEventListener("click", () => {
    showSubstep("noise_capture");
  });
  document.getElementById("calibrate-manual-btn").addEventListener("click", () => {
    showSubstep("manual");
  });
  showSubstep("choose");

  showStep(0);
  loadConfig();
})();

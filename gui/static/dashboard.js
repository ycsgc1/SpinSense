// dashboard.js — page-specific. Subscribes to shell.js for live frames,
// drives the vinyl, track metadata, input meter, system-health input level,
// and refreshes Recent Plays whenever a new identification arrives.
(function () {
  const $ = (id) => document.getElementById(id);

  const vinyl       = $("vinyl");
  const vinylArt    = $("vinyl-art");
  const vinylLogo   = $("vinyl-logo");
  const titleEl     = $("track-title");
  const artistEl    = $("track-artist");
  const albumEl     = $("track-album");
  const meterBar    = $("input-meter");
  const meterText   = $("input-meter-text");
  const levelBar    = $("input-level-bar");
  const levelText   = $("input-level-text");
  const recentList  = $("recent-plays-list");
  const meterThreshold = $("input-meter-threshold");
  const vinylStage = $("vinyl-stage");
  const phaseCaption = $("phase-caption");
  const scanBtn = $("scan-again");

  // Eyebrow = short label; headline = descriptive line (when not playing) or
  // the track title (when playing). Filling both avoids an empty headline.
  const PHASE_EYEBROW = {
    listening: "Idle", scanning: "Scanning", identifying: "Identifying",
    retrying: "Retrying", no_match: "No match", playing: "Now playing",
  };
  const PHASE_HEADLINE = {
    listening:   "Waiting for the needle…",
    scanning:    "Listening to the track…",
    identifying: "Identifying…",
    retrying:    "Couldn't catch it — retrying…",
    no_match:    "Couldn't identify this one",
  };
  const SPIN_PHASES = new Set(["playing", "scanning", "identifying", "retrying"]);

  // dB display window for the input meter on this page. Threshold tick is
  // computed from Audio.Volume_Threshold (linear RMS in config -> dB here).
  const DB_MIN = -80;
  const DB_MAX = 0;
  const dbUtil = window.SpinSense.db;
  let volumeThresholdDb = -40;  // overwritten by /api/config on load
  let lastSeenTitle   = "";

  // ---------- helpers ----------

  function fmtRelative(seconds) {
    const delta = Math.max(0, Math.floor(Date.now() / 1000 - seconds));
    if (delta < 60)     return `${delta}s ago`;
    if (delta < 3600)   return `${Math.floor(delta / 60)}m ago`;
    if (delta < 86400)  return `${Math.floor(delta / 3600)}h ago`;
    return `${Math.floor(delta / 86400)}d ago`;
  }

  function setVinylSpinning(spinning) {
    if (!vinyl) return;
    vinyl.classList.toggle("spin-slow", spinning);
  }

  function setVinylArt(url) {
    if (!vinylArt || !vinylLogo) return;
    if (url) {
      vinylArt.src = url;
      vinylArt.classList.remove("hidden");
      vinylLogo.classList.add("hidden");
    } else {
      vinylArt.classList.add("hidden");
      vinylLogo.classList.remove("hidden");
    }
  }

  // ---------- recent plays ----------

  function renderRecent(plays) {
    if (!recentList) return;
    if (!plays || plays.length === 0) {
      recentList.innerHTML =
        '<li class="text-body-sm text-on-surface-variant py-2">No plays yet — drop a record to begin.</li>';
      return;
    }

    recentList.innerHTML = plays.map((p) => {
      const art = p.art_path
        ? `<img src="/${p.art_path}" alt="" class="w-full h-full object-cover">`
        : `<span class="material-symbols-outlined text-outline" style="font-size: 20px;">album</span>`;
      return `
        <li class="flex items-center gap-3 p-2 rounded-lg hover:bg-white/5 transition-colors">
          <div class="w-10 h-10 rounded bg-surface-container-high border border-outline-variant overflow-hidden flex items-center justify-center">
            ${art}
          </div>
          <div class="flex-1 min-w-0">
            <p class="text-label-sm text-on-surface truncate">${escapeHtml(p.title || "")}</p>
            <p class="text-body-sm text-on-surface-variant truncate" style="font-size: 11px;">${escapeHtml(p.artist || "")}</p>
          </div>
          <span class="text-label-sm text-outline" style="font-size: 11px;">${fmtRelative(p.played_at)}</span>
        </li>
      `;
    }).join("");
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function placeThresholdTick() {
    if (!meterThreshold) return;
    const pct = ((volumeThresholdDb - DB_MIN) / (DB_MAX - DB_MIN)) * 100;
    meterThreshold.style.left = `${Math.max(0, Math.min(100, pct))}%`;
  }

  async function refreshRecent() {
    try {
      const res = await fetch("/api/recent?limit=5");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      renderRecent(data.plays || []);
    } catch (e) {
      console.error("recent plays fetch failed:", e);
      if (recentList) {
        recentList.innerHTML =
          '<li class="text-body-sm text-on-surface-variant py-2">Couldn&rsquo;t load recent plays.</li>';
      }
    }
  }

  // ---------- live frame handling ----------

  function handleFrame(payload) {
    const track = payload.track || {};
    const title = track.title || "";

    // Phase drives display. Frames carry the prior track during scanning/etc.,
    // so we key everything off phase (falling back to title presence if the
    // engine hasn't sent a phase — e.g. the mock stream).
    const phase = payload.phase || (title ? "playing" : "listening");
    const uiPhase = phase === "stopped" ? "listening" : phase;
    if (vinylStage) vinylStage.dataset.phase = uiPhase;
    if (phaseCaption) phaseCaption.textContent = PHASE_EYEBROW[uiPhase] || "Idle";
    setVinylSpinning(SPIN_PHASES.has(uiPhase));

    if (uiPhase === "playing" && title) {
      titleEl.textContent  = title;
      artistEl.textContent = track.artist || "Unknown Artist";
      albumEl.textContent  = track.album  || "";
      setVinylArt(track.art_url || null);
    } else {
      titleEl.textContent  = PHASE_HEADLINE[uiPhase] || "Waiting for the needle…";
      artistEl.innerHTML   = "&nbsp;";
      albumEl.innerHTML    = "&nbsp;";
      setVinylArt(null);
    }

    // Input meter in dB, with a tick at the configured threshold.
    const rms = typeof payload.rms_level === "number" ? payload.rms_level : 0;
    if (meterBar && meterText) {
      const db = dbUtil.rmsToDb(rms);
      const pct = Math.max(0, Math.min(100, ((db - DB_MIN) / (DB_MAX - DB_MIN)) * 100));
      meterBar.style.width = `${pct}%`;
      meterText.textContent = rms <= 0 ? "−∞ dB" : `${db.toFixed(1)} dB`;
    }

    // System Health: Input Level (dB) — narrower visual window (-60..0)
    // because that's where useful signal lives on this widget.
    if (levelBar && levelText) {
      if (rms <= 0) {
        levelBar.style.width = "0%";
        levelText.innerHTML  = "&minus;&infin; dB";
      } else {
        const db = dbUtil.rmsToDb(rms);
        const clamped = Math.max(-60, Math.min(0, db));
        levelBar.style.width = `${((clamped + 60) / 60) * 100}%`;
        levelText.textContent = `${Math.round(clamped)} dB`;
      }
    }

    // Recent-plays refresh on title transition
    if (title !== lastSeenTitle) {
      const wasNew = title !== "" && title !== lastSeenTitle;
      lastSeenTitle = title;
      if (wasNew) {
        // Give the server a beat to write the row, then refetch.
        setTimeout(refreshRecent, 250);
      }
    }
  }

  // ---------- boot ----------

  async function loadConfig() {
    try {
      const res = await fetch("/api/config");
      if (!res.ok) return;
      const cfg = await res.json();
      const v = cfg && cfg.Audio && cfg.Audio.Volume_Threshold;
      if (typeof v === "number" && v > 0) {
        volumeThresholdDb = dbUtil.rmsToDb(v);
        placeThresholdTick();
      }
    } catch (_) { /* fallback default already set */ }
  }

  document.addEventListener("DOMContentLoaded", () => {
    loadConfig();
    refreshRecent();
    window.SpinSense.onFrame(handleFrame);

    if (scanBtn) {
      scanBtn.addEventListener("click", async () => {
        scanBtn.disabled = true;
        try { await fetch("/api/rescan", { method: "POST" }); }
        catch (_) { /* engine may be down; ignore */ }
        setTimeout(() => { scanBtn.disabled = false; }, 1500);
      });
    }
  });
})();

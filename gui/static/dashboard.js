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

  let volumeThreshold = 0.05;  // overwritten by /api/config on load
  let lastSeenTitle   = "";

  // ---------- helpers ----------

  function fmtRelative(seconds) {
    const delta = Math.max(0, Math.floor(Date.now() / 1000 - seconds));
    if (delta < 60)     return `${delta}s ago`;
    if (delta < 3600)   return `${Math.floor(delta / 60)}m ago`;
    if (delta < 86400)  return `${Math.floor(delta / 3600)}h ago`;
    return `${Math.floor(delta / 86400)}d ago`;
  }

  function rmsToDb(rms) {
    if (!rms || rms <= 0) return null; // -infinity
    const db = 20 * Math.log10(rms);
    return Math.max(-60, Math.min(0, db));
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

    // Vinyl + track metadata
    if (title) {
      titleEl.textContent  = title;
      artistEl.textContent = track.artist || "Unknown Artist";
      albumEl.textContent  = track.album  || "";
      setVinylSpinning(true);
      setVinylArt(track.art_url || null);
    } else {
      titleEl.textContent  = "Waiting for drop…";
      artistEl.innerHTML   = "&nbsp;";
      albumEl.innerHTML    = "&nbsp;";
      setVinylSpinning(false);
      setVinylArt(null);
    }

    // RMS input meter (against configured threshold)
    const rms = typeof payload.rms_level === "number" ? payload.rms_level : 0;
    if (meterBar && meterText) {
      const pct = Math.max(0, Math.min(100, (rms / volumeThreshold) * 100));
      meterBar.style.width = `${pct}%`;
      meterText.textContent = rms.toFixed(4);
    }

    // System Health: Input Level (dB)
    const db = rmsToDb(rms);
    if (levelBar && levelText) {
      if (db === null) {
        levelBar.style.width = "0%";
        levelText.innerHTML  = "&minus;&infin; dB";
      } else {
        levelBar.style.width = `${((db + 60) / 60) * 100}%`;
        levelText.textContent = `${Math.round(db)} dB`;
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
      if (typeof v === "number" && v > 0) volumeThreshold = v;
    } catch (_) { /* fallback default already set */ }
  }

  document.addEventListener("DOMContentLoaded", () => {
    loadConfig();
    refreshRecent();
    window.SpinSense.onFrame(handleFrame);
  });
})();

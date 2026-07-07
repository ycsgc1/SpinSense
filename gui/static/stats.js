// stats.js — the /stats page. One fetch per period selection; every module
// renders from the single /api/stats blob. Charts are plain divs with
// percentage widths/heights — no chart library.
(function () {
  const $ = (id) => document.getElementById(id);
  const PERIOD_WRAP = $("stats-period");
  const EMPTY = $("stats-empty");
  const BODY = $("stats-body");

  let period = "month";

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;",
      '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function fmtListening(secs) {
    if (secs < 60) return `${secs}s`;
    const mins = Math.round(secs / 60);
    if (mins < 60) return `${mins}m`;
    return `${Math.floor(mins / 60)}h ${mins % 60}m`;
  }

  function setActiveButton() {
    PERIOD_WRAP.querySelectorAll(".stats-period-btn").forEach((b) => {
      const active = b.dataset.period === period;
      b.classList.toggle("bg-primary", active);
      b.classList.toggle("text-on-primary", active);
      b.classList.toggle("text-on-surface-variant", !active);
      b.setAttribute("aria-selected", active ? "true" : "false");
    });
  }

  function rankRow(rank, art, primary, secondary, plays, maxPlays) {
    const pct = maxPlays > 0 ? Math.max(4, (plays / maxPlays) * 100) : 0;
    const thumb = art
      ? `<img src="/${escapeHtml(art)}" alt="" class="w-10 h-10 rounded object-cover shrink-0 bg-surface-container-high" onerror="this.src='/static/placeholder.jpg'">`
      : `<span class="w-10 h-10 rounded shrink-0 bg-surface-container-high flex items-center justify-center"><span class="material-symbols-outlined text-outline" style="font-size:20px;">album</span></span>`;
    return `
      <li class="flex items-center gap-3">
        <span class="text-label-md text-outline tabular-nums w-4 text-right shrink-0">${rank}</span>
        ${thumb}
        <div class="flex-1 min-w-0">
          <p class="text-body-sm text-on-surface truncate">${primary}</p>
          ${secondary ? `<p class="text-label-sm text-on-surface-variant truncate">${secondary}</p>` : ""}
          <div class="h-1 mt-1 rounded-full bg-surface-container-highest overflow-hidden">
            <div class="h-full bg-primary/70" style="width:${pct}%"></div>
          </div>
        </div>
        <span class="text-label-sm text-on-surface-variant tabular-nums shrink-0">${plays}</span>
      </li>`;
  }

  function renderTopLists(data) {
    const artists = data.top_artists || [];
    const tracks = data.top_tracks || [];
    const maxA = artists.length ? artists[0].plays : 0;
    const maxT = tracks.length ? tracks[0].plays : 0;
    $("stats-top-artists").innerHTML = artists.length
      ? artists.map((a, i) => rankRow(i + 1, a.art_path, escapeHtml(a.artist), "", a.plays, maxA)).join("")
      : '<li class="text-body-sm text-on-surface-variant">No plays yet.</li>';
    const albums = (data.top_albums && data.top_albums.top) || [];
    const maxAl = albums.length ? albums[0].plays : 0;
    $("stats-top-albums").innerHTML = albums.length
      ? albums.map((a, i) => rankRow(i + 1, a.art_path, escapeHtml(a.album), escapeHtml(a.artist), a.plays, maxAl)).join("")
      : '<li class="text-body-sm text-on-surface-variant">No album data yet.</li>';
    const ta = data.top_albums || { covered: 0, total: 0 };
    $("stats-top-albums-note").textContent = (albums.length && ta.covered < ta.total)
      ? `${ta.covered} of ${ta.total} plays have album data.` : "";
    $("stats-top-tracks").innerHTML = tracks.length
      ? tracks.map((t, i) => rankRow(i + 1, t.art_path, escapeHtml(t.title), escapeHtml(t.artist), t.plays, maxT)).join("")
      : '<li class="text-body-sm text-on-surface-variant">No plays yet.</li>';
  }

  function renderChart(data) {
    const buckets = (data.plays_over_time && data.plays_over_time.buckets) || [];
    const max = buckets.reduce((m, b) => Math.max(m, b.plays), 0);
    $("stats-chart").innerHTML = buckets.map((b) => {
      const pct = max > 0 ? (b.plays / max) * 100 : 0;
      return `<div class="flex-1 rounded-t bg-primary/70 hover:bg-primary transition-colors"
                   style="height:${Math.max(pct, b.plays > 0 ? 4 : 1)}%"
                   title="${escapeHtml(b.key)}: ${b.plays} ${b.plays === 1 ? "play" : "plays"}"></div>`;
    }).join("");
    $("stats-chart-start").textContent = buckets.length ? buckets[0].key : "";
    $("stats-chart-end").textContent = buckets.length ? buckets[buckets.length - 1].key : "";
  }

  function barList(el, rows, labelOf, noteEl, noteData, noun) {
    const max = rows.reduce((m, r) => Math.max(m, r.plays), 0);
    el.innerHTML = rows.length ? rows.map((r) => `
      <div class="flex items-center gap-3">
        <span class="text-body-sm text-on-surface w-24 truncate shrink-0">${escapeHtml(labelOf(r))}</span>
        <div class="flex-1 h-2 rounded-full bg-surface-container-highest overflow-hidden">
          <div class="h-full bg-secondary/70" style="width:${max > 0 ? (r.plays / max) * 100 : 0}%"></div>
        </div>
        <span class="text-label-sm text-on-surface-variant tabular-nums shrink-0">${r.plays}</span>
      </div>`).join("")
      : `<p class="text-body-sm text-on-surface-variant">No ${noun} data yet — it accrues as tracks are identified.</p>`;
    noteEl.textContent = (rows.length && noteData.covered < noteData.total)
      ? `${noteData.covered} of ${noteData.total} plays have ${noun} data.` : "";
  }

  function render(data) {
    const t = data.totals || {};
    const noPlays = !t.plays;
    EMPTY.classList.toggle("hidden", !noPlays);
    BODY.classList.toggle("hidden", noPlays);
    if (noPlays) return;

    $("stat-plays").textContent = t.plays;
    $("stat-artists").textContent = t.unique_artists;
    $("stat-tracks").textContent = t.unique_tracks;
    $("stat-listening").textContent = t.listening_tracked_plays > 0
      ? fmtListening(t.listening_secs) : "—";
    $("stat-listening-note").textContent =
      t.listening_tracked_plays < t.plays && t.listening_tracked_plays > 0
        ? `across ${t.listening_tracked_plays} of ${t.plays} plays`
        : (t.listening_tracked_plays === 0 ? "tracked from now on" : "");

    renderTopLists(data);
    renderChart(data);
    barList($("stats-genres"), (data.genres && data.genres.top) || [],
            (r) => r.genre, $("stats-genres-note"), data.genres || {covered: 0, total: 0}, "genre");
    barList($("stats-decades"), (data.decades && data.decades.buckets) || [],
            (r) => `${r.decade}s`, $("stats-decades-note"), data.decades || {covered: 0, total: 0}, "decade");
  }

  async function load() {
    setActiveButton();
    try {
      const res = await fetch(`/api/stats?period=${period}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      render(await res.json());
    } catch (e) {
      console.error("stats fetch failed:", e);
      EMPTY.classList.remove("hidden");
      BODY.classList.add("hidden");
    }
  }

  PERIOD_WRAP.addEventListener("click", (e) => {
    const btn = e.target.closest(".stats-period-btn");
    if (!btn || btn.dataset.period === period) return;
    period = btn.dataset.period;
    load();
  });

  load();
})();

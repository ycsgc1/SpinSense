// history.js — paginated, date-grouped play history. Initial 50 rows on load;
// IntersectionObserver on the sentinel fetches the next page until the server
// returns fewer than `limit` rows, at which point we stop observing.
(function () {
  const LIST = document.getElementById("history-list");
  const EMPTY = document.getElementById("history-empty");
  const STATUS = document.getElementById("history-status");
  const SENTINEL = document.getElementById("history-sentinel");
  const TOTAL = document.getElementById("history-total");

  const LIMIT = 50;
  let offset = 0;
  let exhausted = false;
  let loading = false;
  let lastDateLabel = null;

  function setStatus(text) {
    STATUS.textContent = text;
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;",
      '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function dateLabelForRow(row) {
    const d = new Date((row.played_at || 0) * 1000);
    const today = new Date();
    const y = new Date(today);
    y.setDate(today.getDate() - 1);
    const sameDay = (a, b) =>
      a.getFullYear() === b.getFullYear() &&
      a.getMonth() === b.getMonth() &&
      a.getDate() === b.getDate();
    if (sameDay(d, today)) return "Today";
    if (sameDay(d, y)) return "Yesterday";
    return d.toLocaleDateString(undefined, {
      year: "numeric", month: "long", day: "numeric",
    });
  }

  function timeOfDay(row) {
    const d = new Date((row.played_at || 0) * 1000);
    return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  }

  function rowHtml(row) {
    const artSrc = row.art_path ? `/${row.art_path}` : "/static/placeholder.jpg";
    return `
      <li class="group flex items-center gap-md py-2" data-id="${row.id}"
          data-title="${escapeHtml(row.title)}" data-artist="${escapeHtml(row.artist || "")}"
          data-album="${escapeHtml(row.album || "")}">
        <img src="${escapeHtml(artSrc)}" alt=""
             class="w-12 h-12 rounded shrink-0 bg-surface-container-high object-cover"
             onerror="this.src='/static/placeholder.jpg'">
        <div class="flex-1 min-w-0">
          <p class="text-body-md text-on-surface truncate">${escapeHtml(row.title)}</p>
          <p class="text-body-sm text-on-surface-variant truncate">${escapeHtml(row.artist || "Unknown artist")}</p>
          <p class="history-album text-label-sm text-on-surface-variant truncate">${escapeHtml(row.album || "")}</p>
        </div>
        <span class="text-label-sm text-on-surface-variant tabular-nums shrink-0">${escapeHtml(timeOfDay(row))}</span>
        <button type="button" class="history-edit shrink-0 ml-2 p-1 rounded text-on-surface-variant hover:text-primary
                       opacity-0 group-hover:opacity-100 focus:opacity-100 transition-opacity
                       focus-visible:ring-2 focus-visible:ring-primary"
                title="Change album for ${escapeHtml(row.title)}" aria-label="Change album for ${escapeHtml(row.title)}">
          <span class="material-symbols-outlined" style="font-size:20px;">edit</span>
        </button>
        <button type="button" class="history-del shrink-0 ml-2 p-1 rounded text-on-surface-variant hover:text-error
                       opacity-0 group-hover:opacity-100 focus:opacity-100 transition-opacity
                       focus-visible:ring-2 focus-visible:ring-primary"
                title="Remove ${escapeHtml(row.title)}" aria-label="Remove ${escapeHtml(row.title)}">
          <span class="material-symbols-outlined" style="font-size:20px;">close</span>
        </button>
      </li>
    `;
  }

  function renderPage(rows) {
    if (rows.length === 0) return;
    // Group by date as we go. Each date header gets its own section so styling
    // is simple and re-rendering doesn't need to know about prior pages.
    //
    // Rows that continue the date group already rendered by the previous page
    // are appended into that section's <ul> directly — string-stripping the
    // closing tags off LIST.innerHTML can't reopen them (the parser rebalances
    // the markup on assignment, so the new rows would land outside the list).
    let i = 0;
    if (lastDateLabel != null) {
      const lastUl = LIST.querySelector("section:last-of-type ul");
      if (lastUl) {
        let continuation = "";
        while (i < rows.length && dateLabelForRow(rows[i]) === lastDateLabel) {
          continuation += rowHtml(rows[i]);
          i++;
        }
        if (continuation) lastUl.insertAdjacentHTML("beforeend", continuation);
      }
    }

    let buffer = "";
    let currentDate = lastDateLabel;
    let listOpen = false;

    for (; i < rows.length; i++) {
      const row = rows[i];
      const label = dateLabelForRow(row);
      if (label !== currentDate) {
        if (listOpen) buffer += "</ul></section>";
        buffer += `
          <section class="glass-panel rounded-xl p-md">
            <h3 class="text-label-md text-on-surface-variant uppercase tracking-widest mb-2">${escapeHtml(label)}</h3>
            <ul class="flex flex-col divide-y divide-outline-variant/20">
        `;
        currentDate = label;
        listOpen = true;
      }
      buffer += rowHtml(row);
    }
    if (listOpen) buffer += "</ul></section>";

    if (buffer) LIST.insertAdjacentHTML("beforeend", buffer);
    lastDateLabel = currentDate;
  }

  async function fetchPage() {
    if (loading || exhausted) return;
    loading = true;
    setStatus(offset === 0 ? "Loading…" : "Loading more…");
    try {
      const res = await fetch(`/api/plays?limit=${LIMIT}&offset=${offset}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const rows = (data && data.plays) || [];
      const total = data && typeof data.total === "number" ? data.total : null;

      if (offset === 0 && rows.length === 0) {
        EMPTY.classList.remove("hidden");
        TOTAL.textContent = "0 plays";
        observer.disconnect();
        setStatus("");
        return;
      }

      renderPage(rows);
      offset += rows.length;
      if (total != null) {
        TOTAL.textContent = `${total} ${total === 1 ? "play" : "plays"}`;
      }
      if (rows.length < LIMIT) {
        exhausted = true;
        observer.disconnect();
        setStatus(offset > 0 ? "End of history." : "");
      } else {
        setStatus("");
      }
    } catch (e) {
      setStatus("Failed to load: " + e.message);
    } finally {
      loading = false;
    }
  }

  const observer = new IntersectionObserver((entries) => {
    if (entries.some((e) => e.isIntersecting)) fetchPage();
  }, { rootMargin: "200px" });

  observer.observe(SENTINEL);

  // ---------- delete + undo ----------
  const TOAST = document.getElementById("history-toast");
  const TOAST_UNDO = document.getElementById("history-toast-undo");
  let toastTimer = null;
  let pending = null; // { id, node, parent, next }

  function adjustTotal(delta) {
    const m = /(\d+)/.exec(TOTAL.textContent || "");
    if (!m) return;
    const n = Math.max(0, parseInt(m[1], 10) + delta);
    TOTAL.textContent = `${n} ${n === 1 ? "play" : "plays"}`;
  }

  function hideToast() {
    if (toastTimer) { clearTimeout(toastTimer); toastTimer = null; }
    TOAST.classList.add("hidden");
    pending = null;
  }

  function showToast(msg, withUndo) {
    const msgEl = document.getElementById("history-toast-msg");
    if (msgEl) msgEl.textContent = msg;
    TOAST_UNDO.classList.toggle("hidden", !withUndo);
    TOAST.classList.remove("hidden");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(hideToast, 5000);
  }

  async function removeRow(li) {
    const id = li.dataset.id;
    if (!id) return;
    // Only one undo slot: finalize any prior pending delete before starting a new one.
    if (pending) hideToast();
    const titleEl = li.querySelector("p");
    const titleText = titleEl ? titleEl.textContent : "";
    const parent = li.parentNode;
    const next = li.nextSibling;
    li.remove();
    adjustTotal(-1);
    pending = { id, node: li, parent, next };
    try {
      const res = await fetch(`/api/plays/${id}`, { method: "DELETE" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (e) {
      // Roll the DOM back if the server rejected it.
      if (pending && pending.node === li) {
        parent.insertBefore(li, next);
        adjustTotal(1);
        pending = null;
      }
      return;
    }
    showToast(titleText ? `Removed "${titleText}"` : "Removed", true);
  }

  async function undo() {
    if (!pending) return;
    const { id, node, parent, next } = pending;
    try {
      await fetch(`/api/plays/${id}/restore`, { method: "POST" });
    } catch (e) { /* best effort */ }
    parent.insertBefore(node, next);
    adjustTotal(1);
    hideToast();
  }

  LIST.addEventListener("click", (e) => {
    const editBtn = e.target.closest(".history-edit");
    if (editBtn) {
      const editRow = editBtn.closest("li[data-id]");
      if (editRow) openModal(editRow);
      return;
    }
    const btn = e.target.closest(".history-del");
    if (!btn) return;
    const li = btn.closest("li[data-id]");
    if (li) removeRow(li);
  });
  TOAST_UNDO.addEventListener("click", undo);

  // ---------- album edit modal ----------
  const MODAL = document.getElementById("album-modal");
  const MODAL_TRACK = document.getElementById("album-modal-track");
  const MODAL_LIST = document.getElementById("album-modal-list");
  const MODAL_TEXT = document.getElementById("album-modal-text");
  const MODAL_RUN = document.getElementById("album-modal-run");
  const MODAL_SAVE = document.getElementById("album-modal-save");
  const MODAL_CANCEL = document.getElementById("album-modal-cancel");
  let editLi = null;
  let candidates = [];

  function closeModal() {
    MODAL.classList.add("hidden");
    editLi = null;
    candidates = [];
  }

  function renderCandidates() {
    if (!candidates.length) {
      MODAL_LIST.innerHTML =
        '<p class="text-body-sm text-on-surface-variant">No suggestions found — type a name below.</p>';
      return;
    }
    MODAL_LIST.innerHTML = candidates.map((c, i) => `
      <label class="flex items-center gap-3 p-2 rounded-lg hover:bg-white/5 cursor-pointer">
        <input type="radio" name="album-candidate" value="${i}" class="shrink-0 accent-primary">
        <img src="${escapeHtml(c.art_url ? c.art_url.replace("1000x1000bb", "100x100bb") : "/static/placeholder.jpg")}"
             alt="" class="w-8 h-8 rounded object-cover shrink-0"
             onerror="this.src='/static/placeholder.jpg'">
        <span class="text-body-sm text-on-surface truncate">${escapeHtml(c.album)}</span>
      </label>`).join("");
  }

  async function openModal(li) {
    editLi = li;
    MODAL_TRACK.textContent = `${li.dataset.title} — ${li.dataset.artist}`;
    MODAL_TEXT.value = li.dataset.album || "";
    MODAL_RUN.checked = false;
    MODAL_SAVE.disabled = !MODAL_TEXT.value.trim();
    MODAL_LIST.innerHTML =
      '<p class="text-body-sm text-on-surface-variant">Loading suggestions…</p>';
    MODAL.classList.remove("hidden");
    try {
      const res = await fetch(`/api/plays/${li.dataset.id}/album-candidates`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const body = await res.json();
      candidates = body.candidates || [];
    } catch (_) {
      candidates = [];
    }
    renderCandidates();
  }

  MODAL_LIST.addEventListener("change", (e) => {
    if (e.target.name !== "album-candidate") return;
    const c = candidates[Number(e.target.value)];
    if (c) MODAL_TEXT.value = c.album;
    MODAL_SAVE.disabled = false;
  });

  MODAL_TEXT.addEventListener("input", () => {
    MODAL_SAVE.disabled = !MODAL_TEXT.value.trim();
  });

  async function saveAlbum() {
    if (!editLi) return;
    const album = MODAL_TEXT.value.trim();
    if (!album) return;
    const chosen = candidates.find((c) => c.album === album) || null;
    MODAL_SAVE.disabled = true;
    try {
      const res = await fetch(`/api/plays/${editLi.dataset.id}/album`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          album,
          art_url: chosen ? chosen.art_url : null,
          apply_to_run: MODAL_RUN.checked,
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const body = await res.json();
      const albumEl = editLi.querySelector(".history-album");
      if (albumEl) albumEl.textContent = album;
      editLi.dataset.album = album;
      const n = body.updated || 1;
      showToast(`Album updated (${n} ${n === 1 ? "play" : "plays"})`, false);
      closeModal();
    } catch (e) {
      MODAL_SAVE.disabled = false;
      showToast("Couldn't update album", false);
    }
  }

  MODAL_SAVE.addEventListener("click", saveAlbum);
  MODAL_CANCEL.addEventListener("click", closeModal);
  MODAL.addEventListener("click", (e) => { if (e.target === MODAL) closeModal(); });

  // Initial load. Without this, an empty list never scrolls and the observer
  // would never fire.
  fetchPage();
})();

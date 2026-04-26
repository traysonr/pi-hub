(() => {
  "use strict";

  const els = {
    form: document.getElementById("download-form"),
    url: document.getElementById("url"),
    audioOnly: document.getElementById("audio-only"),
    downloadBtn: document.getElementById("download-btn"),
    downloadStatus: document.getElementById("download-status"),
    refreshBtn: document.getElementById("refresh-btn"),
    musicRefreshBtn: document.getElementById("music-refresh-btn"),
    shuffleBtn: document.getElementById("shuffle-btn"),
    shuffleLabel: document.getElementById("shuffle-label"),
    videoList: document.getElementById("video-list"),
    musicList: document.getElementById("music-list"),
    playerStatus: document.getElementById("player-status"),
    nowPlaying: document.getElementById("now-playing"),
    pauseBtn: document.getElementById("pause-btn"),
    pauseGlyph: document.getElementById("pause-glyph"),
    pauseLabel: document.getElementById("pause-label"),
    stopBtnRemote: document.getElementById("stop-btn-remote"),
    trackRow: document.getElementById("track-row"),
    prevTrackBtn: document.getElementById("prev-track-btn"),
    nextTrackBtn: document.getElementById("next-track-btn"),
    volReadout: document.getElementById("vol-readout"),
    remoteStatus: document.getElementById("remote-status"),
    tvWakeBtn: document.getElementById("tv-wake-btn"),
    tvSleepBtn: document.getElementById("tv-sleep-btn"),
    tvStatus: document.getElementById("tv-status"),
    ssEnabled: document.getElementById("ss-enabled"),
    ssStatus: document.getElementById("ss-status"),
    ssStartBtn: document.getElementById("ss-start-btn"),
    ssStopBtn: document.getElementById("ss-stop-btn"),
    ssRefreshBtn: document.getElementById("ss-refresh-btn"),
    ssRotateBtn: document.getElementById("ss-rotate-btn"),
    ssDeleteCurrentBtn: document.getElementById("ss-delete-current-btn"),
    ssReloadBtn: document.getElementById("ss-reload-btn"),
    ssThemes: document.getElementById("ss-themes"),
    ssMeta: document.getElementById("ss-meta"),
    ssAddForm: document.getElementById("ss-add-form"),
    ssAddInput: document.getElementById("ss-add-input"),
    ssAddBtn: document.getElementById("ss-add-btn"),
    tabs: {
      add: document.getElementById("tab-add"),
      video: document.getElementById("tab-video"),
      music: document.getElementById("tab-music"),
      screensaver: document.getElementById("tab-screensaver"),
      remote: document.getElementById("tab-remote"),
    },
    tabBtns: {
      add: document.getElementById("tabbtn-add"),
      video: document.getElementById("tabbtn-video"),
      music: document.getElementById("tabbtn-music"),
      screensaver: document.getElementById("tabbtn-screensaver"),
      remote: document.getElementById("tabbtn-remote"),
    },
    shuffleModal: document.getElementById("shuffle-modal"),
    shuffleModalOptions: document.getElementById("shuffle-modal-options"),
    shuffleModalCancel: document.getElementById("shuffle-modal-cancel"),
  };

  let activeJobId = null;
  let activeJobAudioOnly = false;
  let pollHandle = null;
  let statusPollHandle = null;
  let currentTab = "add";
  let lastKnownPlaying = false;
  let shuffleActive = false;

  // ---- Helpers ------------------------------------------------------------

  function setStatus(node, text, kind) {
    node.textContent = text;
    node.classList.remove("error", "success");
    if (kind === "error") node.classList.add("error");
    if (kind === "success") node.classList.add("success");
  }

  function fmtSize(bytes) {
    if (!Number.isFinite(bytes) || bytes <= 0) return "";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    let n = bytes;
    while (n >= 1024 && i < units.length - 1) {
      n /= 1024;
      i++;
    }
    return `${n.toFixed(n >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
  }

  function fmtDate(epochSec) {
    if (!epochSec) return "";
    try {
      return new Date(epochSec * 1000).toLocaleString();
    } catch (_) {
      return "";
    }
  }

  async function api(path, options) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    let body = null;
    try {
      body = await res.json();
    } catch (_) {
      body = null;
    }
    if (!res.ok) {
      const detail = (body && (body.detail || body.message)) || res.statusText;
      throw new Error(typeof detail === "string" ? detail : "Request failed");
    }
    return body;
  }

  // ---- Tabs ---------------------------------------------------------------

  function showTab(name) {
    if (!Object.prototype.hasOwnProperty.call(els.tabs, name)) return;
    currentTab = name;
    for (const key of Object.keys(els.tabs)) {
      const isActive = key === name;
      els.tabs[key].hidden = !isActive;
      const btn = els.tabBtns[key];
      btn.classList.toggle("active", isActive);
      btn.setAttribute("aria-selected", isActive ? "true" : "false");
    }
    if (name === "remote") {
      // Refresh the snapshot so the readout matches reality on entry.
      refreshPlayerStatus();
    }
    if (name === "screensaver") {
      refreshScreensaver();
    }
    if (name === "video") {
      loadVideos();
    }
    if (name === "music") {
      loadMusic();
    }
  }

  for (const [name, btn] of Object.entries(els.tabBtns)) {
    btn.addEventListener("click", () => showTab(name));
  }

  // ---- Catalogue (videos + music) -----------------------------------------

  // Both libraries share the same item shape ({filename, title, size_bytes,
  // modified}) and the same Play/Delete UX. The only differences are the
  // API paths, the items array key in the response, and the empty-state
  // copy, so we drive everything from a single config object.
  const LIBRARIES = {
    videos: {
      api: "/api/videos",
      itemsKey: "videos",
      deleteApi: (filename) => `/api/videos/${encodeURIComponent(filename)}`,
      empty: "No videos yet. Add one from the Add tab.",
      listEl: () => els.videoList,
      playLibrary: "videos",
    },
    music: {
      api: "/api/music",
      itemsKey: "tracks",
      deleteApi: (filename) => `/api/music/${encodeURIComponent(filename)}`,
      empty: "No music yet. Add one from the Add tab with Audio only on.",
      listEl: () => els.musicList,
      playLibrary: "music",
    },
  };

  // The catalog API decorates every item with all metadata fields
  // (currently `category` and `play_count`). Filter/sort presets are
  // declarative so adding a future attribute (favourite, duration, tag
  // list, …) is a single config entry here plus a new <option>.
  const FILTER_STATE = {
    videos: { category: "", sort: "recent" },
    music: { category: "", sort: "recent" },
  };

  // libraryCache keeps the last fetched payload so re-applying a filter
  // doesn't require another network round-trip.
  const libraryCache = { videos: null, music: null };

  const SORTERS = {
    recent: (a, b) => (b.modified || 0) - (a.modified || 0),
    plays: (a, b) =>
      (b.play_count || 0) - (a.play_count || 0) ||
      (b.modified || 0) - (a.modified || 0),
    title: (a, b) =>
      (a.title || a.filename || "").localeCompare(
        b.title || b.filename || "",
        undefined,
        { sensitivity: "base" }
      ),
  };

  function applyFilter(kind, items) {
    const state = FILTER_STATE[kind];
    let out = items.slice();
    if (state.category) {
      out = out.filter((it) => (it.category || "") === state.category);
    }
    const sorter = SORTERS[state.sort] || SORTERS.recent;
    out.sort(sorter);
    return out;
  }

  function renderCategoryDropdown(kind, categories, total) {
    const select = document.querySelector(
      `.filter-category[data-library="${kind}"]`
    );
    if (!select) return;
    const current = FILTER_STATE[kind].category;
    select.innerHTML = "";

    const all = document.createElement("option");
    all.value = "";
    all.textContent = `All (${total})`;
    select.appendChild(all);

    for (const cat of categories) {
      const opt = document.createElement("option");
      // "" is a legitimate stored category (default), but the UI labels
      // it explicitly so users understand it means "no category set".
      opt.value = cat.name;
      opt.textContent = `${cat.name || "(uncategorized)"} (${cat.count})`;
      select.appendChild(opt);
    }

    // Preserve the user's selection if it still exists; otherwise fall
    // back to "All".
    const stillExists =
      current === "" || categories.some((c) => c.name === current);
    select.value = stillExists ? current : "";
    if (!stillExists) FILTER_STATE[kind].category = "";
  }

  function updateFilterCount(kind, shown, total) {
    const el = document.querySelector(`.filter-count[data-library="${kind}"]`);
    if (!el) return;
    el.textContent =
      shown === total ? `${total} items` : `${shown} of ${total}`;
  }

  for (const select of document.querySelectorAll(".filter-category")) {
    select.addEventListener("change", () => {
      const kind = select.dataset.library;
      FILTER_STATE[kind].category = select.value;
      rerenderLibrary(kind);
    });
  }
  for (const select of document.querySelectorAll(".filter-sort")) {
    select.addEventListener("change", () => {
      const kind = select.dataset.library;
      FILTER_STATE[kind].sort = select.value;
      rerenderLibrary(kind);
    });
  }

  function rerenderLibrary(kind) {
    const cached = libraryCache[kind];
    if (!cached) return;
    const filtered = applyFilter(kind, cached.items);
    renderLibrary(kind, filtered);
    updateFilterCount(kind, filtered.length, cached.items.length);
  }

  async function loadLibrary(kind) {
    const cfg = LIBRARIES[kind];
    if (!cfg) return;
    const listEl = cfg.listEl();
    if (!listEl) return;
    try {
      const data = await api(cfg.api);
      const items = data[cfg.itemsKey] || [];
      const categories = data.categories || [];
      libraryCache[kind] = { items, categories };
      renderCategoryDropdown(kind, categories, items.length);
      const filtered = applyFilter(kind, items);
      renderLibrary(kind, filtered);
      updateFilterCount(kind, filtered.length, items.length);
    } catch (err) {
      listEl.innerHTML = "";
      const li = document.createElement("li");
      li.className = "empty";
      li.textContent = `Failed to load: ${err.message}`;
      listEl.appendChild(li);
    }
  }

  function renderLibrary(kind, items) {
    const cfg = LIBRARIES[kind];
    const listEl = cfg.listEl();
    if (!listEl) return;
    listEl.innerHTML = "";
    if (items.length === 0) {
      const li = document.createElement("li");
      li.className = "empty";
      li.textContent = cfg.empty;
      listEl.appendChild(li);
      return;
    }

    for (const item of items) {
      const li = document.createElement("li");
      li.className = "video";

      const meta = document.createElement("div");
      meta.className = "meta";

      const title = document.createElement("span");
      title.className = "title";
      title.textContent = item.title || item.filename;
      title.title = item.filename;

      const sub = document.createElement("span");
      sub.className = "sub";
      const parts = [];
      if (item.category) parts.push(item.category);
      if (Number.isFinite(item.play_count) && item.play_count > 0) {
        parts.push(
          `${item.play_count} play${item.play_count === 1 ? "" : "s"}`
        );
      }
      if (item.size_bytes) parts.push(fmtSize(item.size_bytes));
      if (item.modified) parts.push(fmtDate(item.modified));
      sub.textContent = parts.join(" · ");

      meta.appendChild(title);
      meta.appendChild(sub);

      const actions = document.createElement("div");
      actions.className = "actions";

      const playBtn = document.createElement("button");
      playBtn.type = "button";
      playBtn.className = "play-btn";
      playBtn.textContent = "Play";
      playBtn.addEventListener("click", () =>
        playMedia(kind, item.filename, item.title || item.filename, playBtn)
      );

      const deleteBtn = document.createElement("button");
      deleteBtn.type = "button";
      deleteBtn.className = "ghost danger delete-btn";
      deleteBtn.textContent = "Delete";
      deleteBtn.addEventListener("click", () =>
        deleteMedia(kind, item.filename, item.title || item.filename, deleteBtn)
      );

      actions.appendChild(playBtn);
      actions.appendChild(deleteBtn);

      li.appendChild(meta);
      li.appendChild(actions);
      listEl.appendChild(li);
    }
  }

  function loadVideos() {
    return loadLibrary("videos");
  }

  function loadMusic() {
    return loadLibrary("music");
  }

  async function deleteMedia(kind, filename, label, btn) {
    const confirmed = window.confirm(`Delete "${label}"? This cannot be undone.`);
    if (!confirmed) return;

    if (btn) {
      btn.disabled = true;
      btn.textContent = "Deleting…";
    }
    try {
      await api(LIBRARIES[kind].deleteApi(filename), { method: "DELETE" });
      setStatus(els.downloadStatus, `Deleted: ${label}`, "success");
      loadLibrary(kind);
      refreshPlayerStatus();
    } catch (err) {
      setStatus(els.downloadStatus, `Delete failed: ${err.message}`, "error");
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Delete";
      }
    }
  }

  // ---- Playback / Remote --------------------------------------------------

  async function playMedia(kind, filename, label, btn) {
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Starting…";
    }
    try {
      await api("/api/play", {
        method: "POST",
        body: JSON.stringify({
          filename,
          library: LIBRARIES[kind].playLibrary,
        }),
      });
      setPlayerStatus({
        playing: true,
        paused: false,
        title: label || filename,
        kind: LIBRARIES[kind].playLibrary === "music" ? "audio" : "video",
      });
      // Force-switch the user to the remote so they can immediately control
      // whatever just started playing.
      showTab("remote");
      schedulePolling();
    } catch (err) {
      setStatus(els.downloadStatus, `Playback failed: ${err.message}`, "error");
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Play";
      }
    }
  }

  function setPlayerStatus(state) {
    const playing = !!(state && state.playing);
    const paused = !!(state && state.paused);
    const kind = state && state.kind;
    lastKnownPlaying = playing;

    els.playerStatus.classList.remove("playing", "paused");
    if (playing && paused) {
      els.playerStatus.classList.add("paused");
      els.playerStatus.textContent = "paused";
    } else if (playing) {
      els.playerStatus.classList.add("playing");
      els.playerStatus.textContent = kind === "audio" ? "playing audio" : "playing";
    } else {
      els.playerStatus.textContent = "idle";
    }

    const label =
      (state && (state.title || state.filename)) || (playing ? "Playing" : "");
    const prefix = playing && kind === "audio" ? "♪ " : "";
    els.nowPlaying.textContent = playing
      ? `${prefix}${label || "Playing"}`
      : "Nothing playing";

    if (playing && paused) {
      els.pauseGlyph.innerHTML = "&#9654;";
      els.pauseLabel.textContent = "Play";
    } else {
      els.pauseGlyph.innerHTML = "&#10074;&#10074;";
      els.pauseLabel.textContent = "Pause";
    }

    if (state && Number.isFinite(state.volume)) {
      els.volReadout.textContent = `${Math.round(state.volume)}%`;
    } else if (!playing) {
      els.volReadout.textContent = "--";
    }

    setRemoteEnabled(playing);
    els.tabBtns.remote.classList.toggle("has-badge", playing);

    if (state && typeof state.shuffle_active === "boolean") {
      setShuffleUi(state.shuffle_active);
    }
  }

  function setShuffleUi(active) {
    shuffleActive = !!active;
    if (els.shuffleBtn) {
      els.shuffleBtn.classList.toggle("active", shuffleActive);
      els.shuffleBtn.setAttribute(
        "aria-pressed",
        shuffleActive ? "true" : "false"
      );
      if (els.shuffleLabel) {
        els.shuffleLabel.textContent = shuffleActive ? "Stop" : "Shuffle";
      }
    }
    if (els.trackRow) {
      // Only show track navigation when shuffle mode is active.
      els.trackRow.hidden = !shuffleActive;
    }
  }

  function setRemoteEnabled(enabled) {
    const buttons = document.querySelectorAll(".remote-btn");
    for (const btn of buttons) {
      btn.disabled = !enabled;
    }
  }

  async function refreshPlayerStatus() {
    try {
      const data = await api("/api/status");
      setPlayerStatus(data);
      return data;
    } catch (_) {
      return null;
    }
  }

  function schedulePolling() {
    if (statusPollHandle) clearInterval(statusPollHandle);
    statusPollHandle = setInterval(async () => {
      const state = await refreshPlayerStatus();
      // Slow down once the player goes idle so we aren't hitting the API
      // every second forever.
      if (state && !state.playing) {
        clearInterval(statusPollHandle);
        statusPollHandle = setInterval(refreshPlayerStatus, 5000);
      }
    }, 2000);
  }

  // Wire the remote skip buttons.
  for (const btn of document.querySelectorAll(".remote-btn[data-seek]")) {
    btn.addEventListener("click", async () => {
      const seconds = Number(btn.dataset.seek);
      if (!Number.isFinite(seconds)) return;
      try {
        await api("/api/control/seek", {
          method: "POST",
          body: JSON.stringify({ seconds }),
        });
        flashRemote(`${seconds > 0 ? "+" : ""}${seconds}s`);
      } catch (err) {
        flashRemote(`Seek failed: ${err.message}`, "error");
      }
    });
  }

  for (const btn of document.querySelectorAll(".remote-btn[data-volume]")) {
    btn.addEventListener("click", async () => {
      const delta = Number(btn.dataset.volume);
      if (!Number.isFinite(delta)) return;
      try {
        const data = await api("/api/control/volume", {
          method: "POST",
          body: JSON.stringify({ delta }),
        });
        if (Number.isFinite(data.volume)) {
          els.volReadout.textContent = `${Math.round(data.volume)}%`;
        }
        flashRemote(`Volume ${delta > 0 ? "up" : "down"}`);
      } catch (err) {
        flashRemote(`Volume failed: ${err.message}`, "error");
      }
    });
  }

  els.pauseBtn.addEventListener("click", async () => {
    try {
      const data = await api("/api/control/pause", {
        method: "POST",
        body: JSON.stringify({}),
      });
      // Optimistic update; full status refresh will overwrite shortly.
      setPlayerStatus({
        playing: true,
        paused: !!data.paused,
        title: els.nowPlaying.textContent,
      });
      flashRemote(data.paused ? "Paused" : "Playing");
    } catch (err) {
      flashRemote(`Pause failed: ${err.message}`, "error");
    }
  });

  if (els.prevTrackBtn) {
    els.prevTrackBtn.addEventListener("click", async () => {
      try {
        await api("/api/music/shuffle/prev", { method: "POST" });
        flashRemote("Prev track", "success");
        refreshPlayerStatus();
      } catch (err) {
        flashRemote(`Prev failed: ${err.message}`, "error");
      }
    });
  }

  if (els.nextTrackBtn) {
    els.nextTrackBtn.addEventListener("click", async () => {
      try {
        await api("/api/music/shuffle/next", { method: "POST" });
        flashRemote("Next track", "success");
        refreshPlayerStatus();
      } catch (err) {
        flashRemote(`Next failed: ${err.message}`, "error");
      }
    });
  }

  els.stopBtnRemote.addEventListener("click", async () => {
    els.stopBtnRemote.disabled = true;
    try {
      await api("/api/stop", { method: "POST" });
      setPlayerStatus({ playing: false });
      flashRemote("Stopped", "success");
    } catch (err) {
      flashRemote(`Stop failed: ${err.message}`, "error");
    } finally {
      // Re-enabled by setRemoteEnabled when state changes; explicitly
      // restore here in case the state didn't change.
      els.stopBtnRemote.disabled = !lastKnownPlaying ? true : false;
    }
  });

  async function startShuffleWithCategory(category) {
    closeShuffleModal();
    els.shuffleBtn.disabled = true;
    try {
      const body = category ? { category } : {};
      const data = await api("/api/music/shuffle/start", {
        method: "POST",
        body: JSON.stringify(body),
      });
      setShuffleUi(!!data.active);
      const label = category
        ? `Shuffling ${category}…`
        : data.current || "Shuffling…";
      setPlayerStatus({
        playing: true,
        paused: false,
        kind: "audio",
        title: data.current || label,
        shuffle_active: true,
      });
      showTab("remote");
      schedulePolling();
    } catch (err) {
      setStatus(els.downloadStatus, `Shuffle failed: ${err.message}`, "error");
    } finally {
      els.shuffleBtn.disabled = false;
      refreshPlayerStatus();
    }
  }

  function openShuffleModal() {
    if (!els.shuffleModal || !els.shuffleModalOptions) return;
    // Pull the freshest category list available — prefer the cached
    // music payload, fall back to a one-shot fetch so the modal is
    // useful even if the Music tab hasn't been opened yet this session.
    const cached = libraryCache.music;
    const renderOptions = (categories, total) => {
      els.shuffleModalOptions.innerHTML = "";

      const allLi = document.createElement("li");
      const allBtn = document.createElement("button");
      allBtn.type = "button";
      allBtn.className = "opt-all";
      allBtn.innerHTML = `<span>All</span><span class="opt-count">${total} tracks</span>`;
      allBtn.addEventListener("click", () => startShuffleWithCategory(null));
      allLi.appendChild(allBtn);
      els.shuffleModalOptions.appendChild(allLi);

      for (const cat of categories) {
        const li = document.createElement("li");
        const btn = document.createElement("button");
        btn.type = "button";
        const label = cat.name || "(uncategorized)";
        btn.innerHTML = `<span>${label}</span><span class="opt-count">${cat.count}</span>`;
        btn.addEventListener("click", () =>
          startShuffleWithCategory(cat.name)
        );
        li.appendChild(btn);
        els.shuffleModalOptions.appendChild(li);
      }

      els.shuffleModal.hidden = false;
      els.shuffleModal.setAttribute("aria-hidden", "false");
    };

    if (cached) {
      renderOptions(cached.categories || [], cached.items.length);
      return;
    }
    api("/api/music/shuffle")
      .then((data) =>
        renderOptions(data.categories || [], (data.categories || []).reduce((s, c) => s + (c.count || 0), 0))
      )
      .catch(() => renderOptions([], 0));
  }

  function closeShuffleModal() {
    if (!els.shuffleModal) return;
    els.shuffleModal.hidden = true;
    els.shuffleModal.setAttribute("aria-hidden", "true");
  }

  if (els.shuffleModalCancel) {
    els.shuffleModalCancel.addEventListener("click", closeShuffleModal);
  }
  if (els.shuffleModal) {
    els.shuffleModal.addEventListener("click", (event) => {
      if (event.target === els.shuffleModal) closeShuffleModal();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !els.shuffleModal.hidden) {
        closeShuffleModal();
      }
    });
  }

  if (els.shuffleBtn) {
    els.shuffleBtn.addEventListener("click", async () => {
      if (shuffleActive) {
        els.shuffleBtn.disabled = true;
        try {
          await api("/api/music/shuffle/stop", { method: "POST" });
          setShuffleUi(false);
          setPlayerStatus({ playing: false, shuffle_active: false });
        } catch (err) {
          setStatus(els.downloadStatus, `Shuffle failed: ${err.message}`, "error");
        } finally {
          els.shuffleBtn.disabled = false;
          refreshPlayerStatus();
        }
        return;
      }
      openShuffleModal();
    });
  }

  let flashHandle = null;
  function flashRemote(text, kind) {
    setStatus(els.remoteStatus, text, kind);
    if (flashHandle) clearTimeout(flashHandle);
    flashHandle = setTimeout(() => setStatus(els.remoteStatus, ""), 2500);
  }

  // ---- TV (HDMI-CEC) ------------------------------------------------------

  async function sendTvCommand(action, btn, busyText, okText) {
    if (!btn) return;
    btn.disabled = true;
    const originalText = btn.textContent;
    btn.textContent = busyText;
    setStatus(els.tvStatus, `${busyText}…`);
    try {
      await api(`/api/tv/${action}`, { method: "POST" });
      setStatus(els.tvStatus, okText, "success");
    } catch (err) {
      setStatus(els.tvStatus, `${originalText} failed: ${err.message}`, "error");
    } finally {
      btn.disabled = false;
      btn.textContent = originalText;
    }
  }

  if (els.tvWakeBtn) {
    els.tvWakeBtn.addEventListener("click", () =>
      sendTvCommand("wake", els.tvWakeBtn, "Waking", "TV woken")
    );
  }
  if (els.tvSleepBtn) {
    els.tvSleepBtn.addEventListener("click", () =>
      sendTvCommand("sleep", els.tvSleepBtn, "Sleeping", "TV asleep")
    );
  }

  // ---- Downloads ----------------------------------------------------------

  if (els.refreshBtn) {
    els.refreshBtn.addEventListener("click", () => {
      loadVideos();
      refreshPlayerStatus();
    });
  }

  if (els.musicRefreshBtn) {
    els.musicRefreshBtn.addEventListener("click", () => {
      loadMusic();
      refreshPlayerStatus();
    });
  }

  els.form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const url = els.url.value.trim();
    if (!url) return;

    // Prefer FormData so we reflect the actual form state; also read the
    // checkbox directly in case the cached ref is stale.
    const fd = new FormData(els.form);
    const audioOnly =
      fd.get("audio_only") === "on" ||
      !!(els.audioOnly && els.audioOnly.checked);

    els.downloadBtn.disabled = true;
    setStatus(
      els.downloadStatus,
      audioOnly ? "Queueing audio download…" : "Queueing download…"
    );

    try {
      const data = await api("/api/download", {
        method: "POST",
        body: JSON.stringify({ url, audio_only: audioOnly }),
      });
      activeJobId = data.job.id;
      activeJobAudioOnly = !!data.job.audio_only;
      setStatus(
        els.downloadStatus,
        activeJobAudioOnly ? "Audio download started…" : "Download started…"
      );
      els.url.value = "";
      pollJob();
    } catch (err) {
      setStatus(els.downloadStatus, `Download failed: ${err.message}`, "error");
    } finally {
      els.downloadBtn.disabled = false;
    }
  });

  async function pollJob() {
    if (!activeJobId) return;
    if (pollHandle) clearTimeout(pollHandle);

    try {
      const data = await api(`/api/downloads/${activeJobId}`);
      const job = data.job;
      if (job.status === "downloading" || job.status === "queued") {
        setStatus(els.downloadStatus, `Downloading… (${job.status})`);
        pollHandle = setTimeout(pollJob, 2500);
        return;
      }
      if (job.status === "success") {
        const targetTab = activeJobAudioOnly ? "Music" : "Video";
        setStatus(
          els.downloadStatus,
          job.filename
            ? `Saved to ${targetTab}: ${job.filename}`
            : `Download complete (saved to ${targetTab})`,
          "success"
        );
        activeJobId = null;
        if (activeJobAudioOnly) {
          loadMusic();
        } else {
          loadVideos();
        }
        activeJobAudioOnly = false;
        return;
      }
      if (job.status === "error") {
        setStatus(els.downloadStatus, `Failed: ${job.message || "unknown error"}`, "error");
        activeJobId = null;
        activeJobAudioOnly = false;
        return;
      }
    } catch (err) {
      setStatus(els.downloadStatus, `Lost track of job: ${err.message}`, "error");
      activeJobId = null;
      activeJobAudioOnly = false;
    }
  }

  // ---- Screensaver --------------------------------------------------------

  function fmtRelative(epochSec) {
    if (!epochSec) return "never";
    const seconds = Math.max(0, Math.floor(Date.now() / 1000 - epochSec));
    if (seconds < 60) return `${seconds}s ago`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    return `${Math.floor(seconds / 86400)}d ago`;
  }

  function renderScreensaver(state) {
    if (!state) return;

    if (els.ssEnabled) els.ssEnabled.checked = !!state.enabled;

    const bits = [];
    if (state.video_playing) {
      bits.push("video playing");
    } else if (state.running) {
      bits.push("slideshow running");
    } else if (state.enabled) {
      // Enabled + nothing on screen means we're showing the yellow
      // fallback (likely no cached images yet). Surface that explicitly
      // so the user understands why the TV isn't slideshowing.
      bits.push("yellow (no images cached)");
    } else {
      bits.push("disabled (showing yellow)");
    }
    if (state.image_seconds) bits.push(`${state.image_seconds}s/slide`);
    if (state.last_refresh_at) {
      bits.push(`refreshed ${fmtRelative(state.last_refresh_at)}`);
    }
    setStatus(els.ssStatus, bits.join(" \u00b7 "));
    if (state.last_error) {
      setStatus(els.ssStatus, state.last_error, "error");
    }

    if (els.ssDeleteCurrentBtn) {
      const canDelete = !!state.can_delete_current_image;
      els.ssDeleteCurrentBtn.disabled = !canDelete;
      const label = state.current_image ? `Delete current image (${state.current_image})` : "Delete current image";
      els.ssDeleteCurrentBtn.textContent = label;
    }

    if (els.ssMeta) {
      const totalCached = (state.themes || []).reduce(
        (sum, t) => sum + (t.cached_images || 0),
        0
      );
      els.ssMeta.textContent = `${totalCached} images cached`;
    }

    els.ssStartBtn.disabled = !state.enabled || state.running || state.video_playing;
    els.ssStopBtn.disabled = !state.running;

    renderThemes(state.themes || []);
  }

  function renderThemes(themes) {
    els.ssThemes.innerHTML = "";
    if (themes.length === 0) {
      const li = document.createElement("li");
      li.className = "empty";
      li.textContent = "No themes configured.";
      els.ssThemes.appendChild(li);
      return;
    }
    for (const theme of themes) {
      const li = document.createElement("li");
      li.className = "theme";

      const meta = document.createElement("div");
      meta.className = "meta";
      const title = document.createElement("span");
      title.className = "title";
      title.textContent = theme.name;
      const sub = document.createElement("span");
      sub.className = "sub";
      sub.textContent = `r/${theme.subreddit} \u00b7 ${theme.cached_images || 0} cached`;
      meta.appendChild(title);
      meta.appendChild(sub);

      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = theme.enabled ? "play-btn" : "ghost";
      toggle.textContent = theme.enabled ? "On" : "Off";
      toggle.addEventListener("click", () => toggleTheme(theme.name, toggle));

      const del = document.createElement("button");
      del.type = "button";
      del.className = "ghost danger";
      del.textContent = "Delete";
      del.addEventListener("click", () => deleteTheme(theme, del));

      const actions = document.createElement("div");
      actions.className = "actions";
      actions.appendChild(toggle);
      actions.appendChild(del);

      li.appendChild(meta);
      li.appendChild(actions);
      els.ssThemes.appendChild(li);
    }
  }

  async function addTheme(subreddit) {
    if (!subreddit) return;
    if (els.ssAddBtn) els.ssAddBtn.disabled = true;
    setStatus(els.ssStatus, `Adding r/${subreddit}…`);
    try {
      const data = await api("/api/screensaver/themes", {
        method: "POST",
        body: JSON.stringify({ subreddit }),
      });
      renderScreensaver(data);
      setStatus(
        els.ssStatus,
        `Added r/${subreddit}. Fetching images in the background…`,
        "success"
      );
      if (els.ssAddInput) els.ssAddInput.value = "";
    } catch (err) {
      setStatus(els.ssStatus, err.message, "error");
    } finally {
      if (els.ssAddBtn) els.ssAddBtn.disabled = false;
    }
  }

  async function deleteTheme(theme, btn) {
    const label = `r/${theme.subreddit}`;
    const cached = theme.cached_images || 0;
    const suffix = cached > 0 ? ` and ${cached} cached image${cached === 1 ? "" : "s"}` : "";
    if (!window.confirm(`Delete ${label}${suffix}? This cannot be undone.`)) {
      return;
    }
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Deleting…";
    }
    try {
      const data = await api(
        `/api/screensaver/themes/${encodeURIComponent(theme.name)}`,
        { method: "DELETE" }
      );
      renderScreensaver(data);
      setStatus(els.ssStatus, `Removed ${label}.`, "success");
    } catch (err) {
      setStatus(els.ssStatus, `Delete failed: ${err.message}`, "error");
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Delete";
      }
    }
  }

  if (els.ssAddForm) {
    els.ssAddForm.addEventListener("submit", (event) => {
      event.preventDefault();
      const raw = (els.ssAddInput && els.ssAddInput.value) || "";
      addTheme(raw.trim());
    });
  }

  async function refreshScreensaver() {
    try {
      const data = await api("/api/screensaver");
      renderScreensaver(data);
    } catch (err) {
      setStatus(els.ssStatus, `Failed to load: ${err.message}`, "error");
    }
  }

  async function toggleTheme(name, btn) {
    if (btn) btn.disabled = true;
    try {
      const data = await api(
        `/api/screensaver/themes/${encodeURIComponent(name)}/toggle`,
        { method: "POST" }
      );
      renderScreensaver(data);
    } catch (err) {
      setStatus(els.ssStatus, `Toggle failed: ${err.message}`, "error");
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  if (els.ssEnabled) {
    els.ssEnabled.addEventListener("change", async () => {
      const enabled = els.ssEnabled.checked;
      try {
        const data = await api("/api/screensaver/enabled", {
          method: "POST",
          body: JSON.stringify({ enabled }),
        });
        renderScreensaver(data);
      } catch (err) {
        els.ssEnabled.checked = !enabled;
        setStatus(els.ssStatus, `Failed: ${err.message}`, "error");
      }
    });
  }

  if (els.ssStartBtn) {
    els.ssStartBtn.addEventListener("click", async () => {
      els.ssStartBtn.disabled = true;
      setStatus(els.ssStatus, "Starting…");
      try {
        const data = await api("/api/screensaver/start", { method: "POST" });
        renderScreensaver(data);
      } catch (err) {
        setStatus(els.ssStatus, err.message, "error");
        refreshScreensaver();
      }
    });
  }

  if (els.ssStopBtn) {
    els.ssStopBtn.addEventListener("click", async () => {
      els.ssStopBtn.disabled = true;
      try {
        const data = await api("/api/screensaver/stop", { method: "POST" });
        renderScreensaver(data);
      } catch (err) {
        setStatus(els.ssStatus, `Stop failed: ${err.message}`, "error");
        refreshScreensaver();
      }
    });
  }

  if (els.ssRefreshBtn) {
    els.ssRefreshBtn.addEventListener("click", async () => {
      els.ssRefreshBtn.disabled = true;
      const originalText = els.ssRefreshBtn.textContent;
      els.ssRefreshBtn.textContent = "Refreshing…";
      setStatus(els.ssStatus, "Fetching images from Reddit…");
      try {
        const data = await api("/api/screensaver/refresh", { method: "POST" });
        renderScreensaver(data);
      } catch (err) {
        setStatus(els.ssStatus, `Refresh failed: ${err.message}`, "error");
      } finally {
        els.ssRefreshBtn.disabled = false;
        els.ssRefreshBtn.textContent = originalText;
      }
    });
  }

  if (els.ssRotateBtn) {
    els.ssRotateBtn.addEventListener("click", async () => {
      if (
        !window.confirm(
          "Rotate now? This keeps ~25% of cached images and replaces the rest."
        )
      ) {
        return;
      }
      els.ssRotateBtn.disabled = true;
      const originalText = els.ssRotateBtn.textContent;
      els.ssRotateBtn.textContent = "Rotating…";
      setStatus(els.ssStatus, "Rotating cache…");
      try {
        const data = await api("/api/screensaver/rotate", { method: "POST" });
        if (data.status) renderScreensaver(data.status);
        if (data.rotation && data.rotation.summary) {
          setStatus(els.ssStatus, `Rotated: ${data.rotation.summary}`, "success");
        }
      } catch (err) {
        setStatus(els.ssStatus, `Rotate failed: ${err.message}`, "error");
      } finally {
        els.ssRotateBtn.disabled = false;
        els.ssRotateBtn.textContent = originalText;
      }
    });
  }

  if (els.ssDeleteCurrentBtn) {
    els.ssDeleteCurrentBtn.addEventListener("click", async () => {
      if (
        !window.confirm(
          "Delete the image currently being displayed? This removes it from the cache."
        )
      ) {
        return;
      }
      els.ssDeleteCurrentBtn.disabled = true;
      try {
        const data = await api("/api/screensaver/current/delete", { method: "POST" });
        renderScreensaver(data);
        setStatus(els.ssStatus, "Deleted current image.", "success");
      } catch (err) {
        setStatus(els.ssStatus, `Delete failed: ${err.message}`, "error");
        refreshScreensaver();
      }
    });
  }

  if (els.ssReloadBtn) {
    els.ssReloadBtn.addEventListener("click", async () => {
      els.ssReloadBtn.disabled = true;
      try {
        const data = await api("/api/screensaver/reload", { method: "POST" });
        renderScreensaver(data);
        setStatus(els.ssStatus, "Config reloaded", "success");
      } catch (err) {
        setStatus(els.ssStatus, `Reload failed: ${err.message}`, "error");
      } finally {
        els.ssReloadBtn.disabled = false;
      }
    });
  }

  // ---- Boot ---------------------------------------------------------------

  // Resume tracking any download still in flight when the page reloaded,
  // so the "Saved to Music/Video" indicator and auto-refresh fire even
  // if the user navigated away during a long extraction.
  async function resumeActiveDownload() {
    try {
      const data = await api("/api/downloads");
      const jobs = data.jobs || [];
      // Newest first (the API already sorts by created_at desc).
      const inflight = jobs.find(
        (j) => j.status === "downloading" || j.status === "queued"
      );
      if (!inflight) return;
      activeJobId = inflight.id;
      activeJobAudioOnly = !!inflight.audio_only;
      setStatus(
        els.downloadStatus,
        activeJobAudioOnly
          ? "Resuming audio download in progress…"
          : "Resuming download in progress…"
      );
      pollJob();
    } catch (_) {
      // Non-fatal; resume is best-effort.
    }
  }

  loadVideos();
  loadMusic();
  resumeActiveDownload();
  refreshPlayerStatus().then((state) => {
    // If the server is already mid-playback when the page opens, drop the
    // user straight onto the remote so they don't have to hunt for it.
    if (state && state.playing) {
      showTab("remote");
      schedulePolling();
    } else {
      // Light idle polling so the UI catches state changes (e.g. the video
      // ends naturally) without hammering the server.
      statusPollHandle = setInterval(refreshPlayerStatus, 5000);
    }
  });
})();

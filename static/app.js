(() => {
  "use strict";

  const els = {
    form: document.getElementById("download-form"),
    url: document.getElementById("url"),
    downloadBtn: document.getElementById("download-btn"),
    downloadStatus: document.getElementById("download-status"),
    refreshBtn: document.getElementById("refresh-btn"),
    list: document.getElementById("video-list"),
    playerStatus: document.getElementById("player-status"),
    nowPlaying: document.getElementById("now-playing"),
    pauseBtn: document.getElementById("pause-btn"),
    pauseGlyph: document.getElementById("pause-glyph"),
    pauseLabel: document.getElementById("pause-label"),
    stopBtnRemote: document.getElementById("stop-btn-remote"),
    volReadout: document.getElementById("vol-readout"),
    remoteStatus: document.getElementById("remote-status"),
    tvWakeBtn: document.getElementById("tv-wake-btn"),
    tvSleepBtn: document.getElementById("tv-sleep-btn"),
    tvStatus: document.getElementById("tv-status"),
    tabs: {
      home: document.getElementById("tab-home"),
      remote: document.getElementById("tab-remote"),
    },
    tabBtns: {
      home: document.getElementById("tabbtn-home"),
      remote: document.getElementById("tabbtn-remote"),
    },
  };

  let activeJobId = null;
  let pollHandle = null;
  let statusPollHandle = null;
  let currentTab = "home";
  let lastKnownPlaying = false;

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
    if (name !== "home" && name !== "remote") return;
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
  }

  for (const [name, btn] of Object.entries(els.tabBtns)) {
    btn.addEventListener("click", () => showTab(name));
  }

  // ---- Catalogue ----------------------------------------------------------

  async function loadVideos() {
    try {
      const data = await api("/api/videos");
      renderVideos(data.videos || []);
    } catch (err) {
      els.list.innerHTML = "";
      const li = document.createElement("li");
      li.className = "empty";
      li.textContent = `Failed to load: ${err.message}`;
      els.list.appendChild(li);
    }
  }

  function renderVideos(videos) {
    els.list.innerHTML = "";
    if (videos.length === 0) {
      const li = document.createElement("li");
      li.className = "empty";
      li.textContent = "No videos yet. Download one above.";
      els.list.appendChild(li);
      return;
    }

    for (const v of videos) {
      const li = document.createElement("li");
      li.className = "video";

      const meta = document.createElement("div");
      meta.className = "meta";

      const title = document.createElement("span");
      title.className = "title";
      title.textContent = v.title || v.filename;
      title.title = v.filename;

      const sub = document.createElement("span");
      sub.className = "sub";
      const parts = [];
      if (v.size_bytes) parts.push(fmtSize(v.size_bytes));
      if (v.modified) parts.push(fmtDate(v.modified));
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
        playVideo(v.filename, v.title || v.filename, playBtn)
      );

      const deleteBtn = document.createElement("button");
      deleteBtn.type = "button";
      deleteBtn.className = "ghost danger delete-btn";
      deleteBtn.textContent = "Delete";
      deleteBtn.addEventListener("click", () =>
        deleteVideo(v.filename, v.title || v.filename, deleteBtn)
      );

      actions.appendChild(playBtn);
      actions.appendChild(deleteBtn);

      li.appendChild(meta);
      li.appendChild(actions);
      els.list.appendChild(li);
    }
  }

  async function deleteVideo(filename, label, btn) {
    const confirmed = window.confirm(`Delete "${label}"? This cannot be undone.`);
    if (!confirmed) return;

    if (btn) {
      btn.disabled = true;
      btn.textContent = "Deleting…";
    }
    try {
      await api(`/api/videos/${encodeURIComponent(filename)}`, {
        method: "DELETE",
      });
      setStatus(els.downloadStatus, `Deleted: ${label}`, "success");
      loadVideos();
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

  async function playVideo(filename, label, btn) {
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Starting…";
    }
    try {
      await api("/api/play", {
        method: "POST",
        body: JSON.stringify({ filename }),
      });
      setPlayerStatus({ playing: true, paused: false, title: label || filename });
      // Force-switch the user to the remote so they can immediately control
      // what's now playing on the TV.
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
    lastKnownPlaying = playing;

    els.playerStatus.classList.remove("playing", "paused");
    if (playing && paused) {
      els.playerStatus.classList.add("paused");
      els.playerStatus.textContent = "paused";
    } else if (playing) {
      els.playerStatus.classList.add("playing");
      els.playerStatus.textContent = "playing";
    } else {
      els.playerStatus.textContent = "idle";
    }

    const label =
      (state && (state.title || state.filename)) || (playing ? "Playing" : "");
    els.nowPlaying.textContent = playing
      ? label || "Playing"
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

  els.refreshBtn.addEventListener("click", () => {
    loadVideos();
    refreshPlayerStatus();
  });

  els.form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const url = els.url.value.trim();
    if (!url) return;

    els.downloadBtn.disabled = true;
    setStatus(els.downloadStatus, "Queueing download…");

    try {
      const data = await api("/api/download", {
        method: "POST",
        body: JSON.stringify({ url }),
      });
      activeJobId = data.job.id;
      setStatus(els.downloadStatus, "Download started…");
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
        setStatus(
          els.downloadStatus,
          job.filename ? `Saved: ${job.filename}` : "Download complete",
          "success"
        );
        activeJobId = null;
        loadVideos();
        return;
      }
      if (job.status === "error") {
        setStatus(els.downloadStatus, `Failed: ${job.message || "unknown error"}`, "error");
        activeJobId = null;
        return;
      }
    } catch (err) {
      setStatus(els.downloadStatus, `Lost track of job: ${err.message}`, "error");
      activeJobId = null;
    }
  }

  // ---- Boot ---------------------------------------------------------------

  loadVideos();
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

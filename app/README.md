STATUS: CANONICAL
OWNER: trays
LAST UPDATED: 2026-04-26
SCOPE: FastAPI application package — entrypoint, HTTP routes, and backing services for Pi Hub.
RELATED: ../README.md, ../docs/README.md, ../docs/INDEX.md, ../AGENTS.md

# Application package (`app/`)

## Entry

- `main.py` — FastAPI app, static/template mounts, startup initialization for the persistent display controller and screensaver subsystem.

## HTTP routes

- `routes/media.py` — Video and music catalogue, downloads (video + audio-only), playback, TV HDMI-CEC, and remote-style controls.
- `routes/screensaver.py` — Screensaver state, themes, refresh, and master toggle.

## Core services

- `services/display.py` — Single long-lived `mpv` process and JSON IPC; owns HDMI transitions between slideshow, video, and yellow idle modes.
- `services/audio_player.py` — Headless `mpv` backend for music playback over HDMI/ALSA without disturbing whatever the display controller is showing.
- `services/player.py` — Playback facade dispatching between the video (display) and audio backends; also surfaces unified `/api/status` and remote controls.
- `services/shuffle.py` — Continuous shuffle mode for the music library (plays random tracks end-to-end). Provides next/prev track controls for the Remote tab while shuffle is active.
- `services/screensaver.py` — Theme configuration, Reddit-backed image cache, and idle-mode coordination with `display`.
- `services/reddit.py` — Subreddit listing and on-disk image cache helpers.
- `services/scheduler.py` — In-app daily/weekly job scheduler. Hosts the 05:00 cache rotation and is the hook point for future recurring tasks (scripts, reports, emails) without adding systemd timers or cron.
- `services/catalogue.py`, `services/downloader.py`, `services/cec.py` — Video + music catalogue listings, `yt-dlp` jobs, and CEC helpers respectively.
- `services/metadata.py` — Per-file JSON catalog (`config/video-catalog.json`, `config/audio-catalog.json`) tracking `category` and `play_count` for every video/audio file. Auto-maintained: downloader registers new entries on success, delete routes prune entries, the play route increments `play_count`, and `sync_all()` runs at boot to reconcile with the on-disk media directories. `GET /api/videos` and `GET /api/music` decorate every item with the full metadata blob plus a `categories` summary so the UI can filter/sort generically (extending to a new attribute is one entry in the frontend filter config). `POST /api/music/shuffle/start` accepts an optional `{category}` so the Music tab's shuffle button can scope the pool to a single category.

## Configuration

Runtime paths and environment overrides are defined in `config.py`. Screensaver theme JSON lives at `config/screensaver-themes.json` on the Pi (gitignored); copy from `config/screensaver-themes.json.example` to get started.

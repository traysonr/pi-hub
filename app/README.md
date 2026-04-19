STATUS: CANONICAL
OWNER: trays
LAST UPDATED: 2026-04-19
SCOPE: FastAPI application package — entrypoint, HTTP routes, and backing services for Pi Hub.
RELATED: ../README.md, ../docs/README.md, ../docs/INDEX.md, ../AGENTS.md

# Application package (`app/`)

## Entry

- `main.py` — FastAPI app, static/template mounts, startup initialization for the persistent display controller and screensaver subsystem.

## HTTP routes

- `routes/media.py` — Video catalogue, downloads, playback, TV HDMI-CEC, and remote-style controls.
- `routes/screensaver.py` — Screensaver state, themes, refresh, and master toggle.

## Core services

- `services/display.py` — Single long-lived `mpv` process and JSON IPC; owns HDMI transitions between slideshow, video, and yellow idle modes.
- `services/player.py` — Thin playback facade over `display` for legacy call sites and `/api/status`.
- `services/screensaver.py` — Theme configuration, Reddit-backed image cache, and idle-mode coordination with `display`.
- `services/reddit.py` — Subreddit listing and on-disk image cache helpers.
- `services/catalogue.py`, `services/downloader.py`, `services/cec.py` — Catalogue, `yt-dlp` jobs, and CEC helpers respectively.

## Configuration

Runtime paths and environment overrides are defined in `config.py`. Screensaver theme JSON lives at `config/screensaver-themes.json` on the Pi (gitignored); copy from `config/screensaver-themes.json.example` to get started.

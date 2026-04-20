STATUS: CANONICAL
OWNER: trays
LAST UPDATED: 2026-04-20
SCOPE: Pi Hub network-controlled media server for Raspberry Pi — setup, API, screensaver, and operations.
RELATED: docs/README.md, docs/INDEX.md, app/README.md, AGENTS.md

# Pi Hub

A lightweight, network-controlled media server for a Raspberry Pi 3 B+.

Documentation map and contributor rules: [docs/README.md](docs/README.md), [docs/INDEX.md](docs/INDEX.md), [AGENTS.md](AGENTS.md).

The first milestone provides a mobile-friendly web UI to:

- Download videos from YouTube using `yt-dlp` into a local catalogue.
- Browse the catalogue from any device on the LAN.
- Play a selected video fullscreen on the TV connected to the Pi via HDMI using `mpv`.

## Requirements

System packages (Raspberry Pi OS Lite):

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip mpv ffmpeg
```

`yt-dlp` is installed via pip (in the project venv) along with the Python deps below. `ffmpeg` is needed by `yt-dlp` to merge best video and audio streams.

## Setup

```bash
cd ~/pi-hub
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## Development

From the project root (optional venv):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

## Run

### Manual (foreground, useful for development)

From the project root:

```bash
./scripts/run.sh
```

Or manually:

```bash
source .venv/bin/activate
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Auto-start on boot (recommended for the Pi)

Install the bundled `systemd` service. It starts on boot, restarts on failure,
and runs as the `gilberto` user so `mpv` can drive HDMI output.

```bash
./scripts/install-service.sh
```

Common operations:

```bash
sudo systemctl status pi-hub        # current state
sudo systemctl restart pi-hub       # apply code changes
sudo systemctl stop pi-hub          # stop without disabling
sudo systemctl start pi-hub
sudo systemctl disable pi-hub       # don't start on next boot
journalctl -u pi-hub -f             # live logs
journalctl -u pi-hub -n 200         # last 200 log lines
```

To uninstall the service:

```bash
./scripts/install-service.sh --uninstall
```

The service file lives at [`scripts/pi-hub.service`](scripts/pi-hub.service); edit it and rerun the installer (or `sudo systemctl daemon-reload && sudo systemctl restart pi-hub`) to apply changes.

### Access

Open the UI from any device on the LAN:

```
http://<pi-ip>:8000
```

Environment overrides (set in the systemd unit or your shell):

- `PI_HUB_HOST` (default `0.0.0.0`)
- `PI_HUB_PORT` (default `8000`)
- `PI_HUB_MEDIA_DIR` (default `<project>/media`)
- `PI_HUB_YT_COOKIES` (default `<project>/secrets/youtube-cookies.txt`)

## YouTube authentication (cookies)

YouTube increasingly blocks unauthenticated downloads with a "Sign in to
confirm you're not a bot" error. To work around this, `yt-dlp` is given a
cookies file exported from a **dedicated throwaway Google account** (do NOT
use your real account).

Setup:

1. Create a new Google account in a private browser profile and watch a
   video on YouTube while signed in (so the account looks "warm").
2. Install a cookie exporter extension such as **Get cookies.txt LOCALLY**
   (Chrome) or **cookies.txt** (Firefox).
3. With YouTube open and signed in, export cookies for `youtube.com` to a
   `cookies.txt` file (Netscape format).
4. Copy it to the Pi:

   ```bash
   scp cookies.txt gilberto@<pi-ip>:/tmp/youtube-cookies.txt
   ssh gilberto@<pi-ip> 'mkdir -p ~/pi-hub/secrets && \
     mv /tmp/youtube-cookies.txt ~/pi-hub/secrets/youtube-cookies.txt && \
     chmod 600 ~/pi-hub/secrets/youtube-cookies.txt && \
     chmod 700 ~/pi-hub/secrets && \
     sudo systemctl restart pi-hub'
   ```

5. **Close the browser tab without signing out** — signing out invalidates
   the cookies you just exported.

Cookies expire periodically (weeks to months). When downloads start failing
with a "YouTube rejected the cookies" error, repeat the export step. The
`secrets/` directory is gitignored.

**Age-restricted videos and Shorts:** YouTube may return "Sign in to confirm
your age" for some clips. That is not the same as expired anti-bot cookies:
open youtube.com in the same throwaway account, complete any age prompt
YouTube shows, then export a new `cookies.txt` and replace the file on the Pi.

## API

- `GET /` — Web UI.
- `GET /api/videos` — List video catalogue entries.
- `DELETE /api/videos/{filename}` — Remove a video from the catalogue (stops playback first if needed).
- `GET /api/music` — List audio catalogue entries.
- `DELETE /api/music/{filename}` — Remove a music track from the catalogue (stops playback first if needed).
- `POST /api/download` — Body: `{ "url": "https://...", "audio_only": false }`. Starts a background download. With `audio_only=true` the file is extracted into the Music tab instead of the Video tab.
- `GET /api/downloads` — List recent download jobs.
- `GET /api/downloads/{id}` — Status of a single download job.
- `POST /api/play` — Body: `{ "filename": "video.mp4", "library": "video" }`. Set `"library": "music"` to play an audio track headlessly over HDMI/ALSA without disturbing the slideshow on screen.
- `POST /api/stop` — Stops any current playback.
- `GET /api/status` — Whether something is currently playing.
- `POST /api/control/pause` — Toggle (or set) pause for the current video.
- `POST /api/control/seek` — Body: `{ "seconds": 30 }`. Relative seek.
- `POST /api/control/volume` — Body: `{ "delta": 10 }`. Adjust mpv volume.
- `POST /api/tv/wake` — Wake TV, switch to Pi input (HDMI-CEC).
- `POST /api/tv/sleep` — Send TV to standby (HDMI-CEC).
- `GET /api/screensaver` — Current screensaver state, themes, and cache counts.
- `POST /api/screensaver/enabled` — Body: `{ "enabled": true|false }`. Master toggle.
- `POST /api/screensaver/start` — Start the slideshow now (409 if a video is playing or the master toggle is off; falls back to the yellow placeholder if no images are cached yet).
- `POST /api/screensaver/stop` — Stop the slideshow.
- `POST /api/screensaver/refresh` — Re-fetch images from all enabled themes.
- `POST /api/screensaver/themes/{name}/toggle` — Toggle a single theme on/off.
- `POST /api/screensaver/themes` — Body: `{ "subreddit": "robotics" }`. Add a new theme. Accepts bare names, `r/name`, or full Reddit URLs. 400 on invalid input, 409 if the subreddit is already configured.
- `DELETE /api/screensaver/themes/{name}` — Remove a theme and delete its cached images on disk.
- `POST /api/screensaver/reload` — Reload `config/screensaver-themes.json` from disk.
- `GET /healthz` — Health check.

## Project Layout

```
pi-hub/
  app/
    README.md          Application component portal
    main.py            FastAPI entrypoint
    config.py          Paths, logging, runtime dirs
    routes/
      media.py         HTTP API
      screensaver.py   Screensaver HTTP API
    services/
      catalogue.py     Filesystem-backed video + music listing
      display.py       Persistent mpv HDMI controller (slideshow / video / idle)
      audio_player.py  Headless mpv backend for music playback over HDMI/ALSA
      downloader.py    yt-dlp background jobs (video and audio-only)
      player.py        Playback facade dispatching between video and audio backends
      cec.py           HDMI-CEC TV wake/sleep
      reddit.py        Subreddit image listing + cache
      screensaver.py   Slideshow lifecycle and theme management
  docs/
    README.md          Documentation portal
    INDEX.md           Documentation navigation map
  config/
    screensaver-themes.json.example   Starter themes file (copy to .json)
  media/
    videos/            Downloaded videos live here
    music/             Audio-only downloads live here
    screensaver-cache/ Per-theme cached images
  secrets/
    youtube-cookies.txt  yt-dlp auth cookies (gitignored)
  static/              UI assets (CSS, JS)
  templates/           Jinja2 HTML templates
  scripts/
    run.sh                 Convenience foreground launcher
    pi-hub.service         systemd unit (auto-start on boot)
    install-service.sh     Installs/enables/starts the systemd service
    bulk_download.py       CLI to enqueue many YouTube URLs from a list file
  requirements.txt
  requirements-dev.txt  Dev/test dependencies (pytest)
```

## Notes

- Playback happens on the Pi's HDMI output; nothing is streamed to the browser.
- Downloads run in a background thread so the API responds immediately. Poll `/api/downloads/{id}` for status.
- Filenames are sanitized via `yt-dlp --restrict-filenames`. Filenames passed to `/api/play` are resolved against the media directory and rejected if they escape it.

## Screensaver

The TV connected to the Pi never falls back to the Linux console. A
single long-lived `mpv` process owns the HDMI output continuously and
switches between three "modes" via IPC, so transitions are seamless:

1. **Slideshow** — fullscreen rotation of images pulled from
   configurable subreddit "themes" (e.g. `r/Watercolor`, `r/EarthPorn`).
2. **Video** — whatever you pressed Play on.
3. **Yellow fallback** — a solid yellow placeholder, used when the
   slideshow is disabled (or enabled but has no cached images yet).

Behavior:

- **Enabled by default.** A fresh boot lands on the slideshow as the
  idle screen. Toggle the master switch off in the Screensaver tab to
  fall back to the solid-yellow placeholder instead.
- **Manual start/stop.** "Start now" forces the slideshow on screen
  immediately. "Stop" swaps it for the yellow fallback without changing
  the master toggle.
- **Never wakes the TV.** If the TV is off, neither the slideshow nor
  the yellow fallback push CEC wake commands.
- **Seamless video handoff.** Pressing Play on any video swaps the
  slideshow/yellow content for the video over IPC -- the same `mpv`
  process keeps owning the framebuffer, so the Linux console never
  flashes through. When the video ends (manually or naturally), the TV
  returns immediately to whichever idle mode is configured (slideshow
  if enabled, otherwise yellow).

Themes live in `config/screensaver-themes.json` (gitignored; an example
file is committed alongside it). The easiest way to manage them is from
the Screensaver tab: type a subreddit name (or paste an `r/...` link)
to add one, press **Delete** to remove a theme and its cached images,
or use the per-theme on/off buttons to keep a theme configured but
silenced. Hand edits still work -- press **Reload config** in the UI
after editing the file. Images are cached under
`media/screensaver-cache/<subreddit>/`,
so the slideshow keeps working even if Reddit is briefly unreachable.
The yellow placeholder image is generated at startup into the same
cache directory and can be safely deleted (it'll be regenerated).

System dependency: `mpv` (already required for video playback) handles
all three rendering modes.

## Future Work (designed for, not implemented)

- Dashboard / kiosk mode
- Automation rules engine
- Sensor data ingestion
- Remote access outside the LAN

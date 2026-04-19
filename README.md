# Pi Hub

A lightweight, network-controlled media server for a Raspberry Pi 3 B+.

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

## API

- `GET /` — Web UI.
- `GET /api/videos` — List catalogue entries.
- `DELETE /api/videos/{filename}` — Remove a video from the catalogue (stops playback first if needed).
- `POST /api/download` — Body: `{ "url": "https://..." }`. Starts a background download.
- `GET /api/downloads` — List recent download jobs.
- `GET /api/downloads/{id}` — Status of a single download job.
- `POST /api/play` — Body: `{ "filename": "video.mp4" }`. Plays fullscreen on HDMI.
- `POST /api/stop` — Stops any current playback.
- `GET /api/status` — Whether something is currently playing.
- `GET /healthz` — Health check.

## Project Layout

```
pi-hub/
  app/
    main.py            FastAPI entrypoint
    config.py          Paths, logging, runtime dirs
    routes/
      media.py         HTTP API
    services/
      catalogue.py     Filesystem-backed video listing
      downloader.py    yt-dlp background jobs
      player.py        mpv subprocess controller
  media/
    videos/            Downloaded videos live here
  static/              UI assets (CSS, JS)
  templates/           Jinja2 HTML templates
  scripts/
    run.sh                 Convenience foreground launcher
    pi-hub.service         systemd unit (auto-start on boot)
    install-service.sh     Installs/enables/starts the systemd service
  requirements.txt
```

## Notes

- Playback happens on the Pi's HDMI output; nothing is streamed to the browser.
- Downloads run in a background thread so the API responds immediately. Poll `/api/downloads/{id}` for status.
- Filenames are sanitized via `yt-dlp --restrict-filenames`. Filenames passed to `/api/play` are resolved against the media directory and rejected if they escape it.

## Future Work (designed for, not implemented)

- HDMI-CEC TV control
- Music playback
- Dashboard / kiosk mode
- Automation rules engine
- Sensor data ingestion
- Remote access outside the LAN

"""Shared configuration and filesystem paths for Pi Hub."""

from __future__ import annotations

import logging
import os
from pathlib import Path

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

MEDIA_DIR: Path = Path(
    os.environ.get("PI_HUB_MEDIA_DIR", PROJECT_ROOT / "media")
).resolve()
VIDEO_DIR: Path = (MEDIA_DIR / "videos").resolve()
MUSIC_DIR: Path = (MEDIA_DIR / "music").resolve()
SCREENSAVER_CACHE_DIR: Path = (MEDIA_DIR / "screensaver-cache").resolve()

TEMPLATES_DIR: Path = PROJECT_ROOT / "templates"
STATIC_DIR: Path = PROJECT_ROOT / "static"

CONFIG_DIR: Path = Path(
    os.environ.get("PI_HUB_CONFIG_DIR", PROJECT_ROOT / "config")
).resolve()
SCREENSAVER_THEMES_FILE: Path = Path(
    os.environ.get(
        "PI_HUB_SCREENSAVER_THEMES",
        str(CONFIG_DIR / "screensaver-themes.json"),
    )
).resolve()
SCREENSAVER_THEMES_EXAMPLE: Path = (
    CONFIG_DIR / "screensaver-themes.json.example"
).resolve()

# Extensions that the catalogue treats as playable video files.
VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}
)

# Extensions that the catalogue treats as playable audio files. mpv plays
# all of these through the shared ALSA output (HDMI when a display is
# connected, otherwise the 3.5mm headphone/aux jack — see
# ``resolve_audio_device``).
AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {".m4a", ".mp3", ".opus", ".ogg", ".flac", ".wav", ".aac"}
)

_DRM_CLASS_DIR = Path("/sys/class/drm")
_ALSA_CARDS_PATH = Path("/proc/asound/cards")


def hdmi_display_connected() -> bool:
    """True when any HDMI connector reports ``connected`` in sysfs."""

    try:
        for status_path in _DRM_CLASS_DIR.glob("card*-HDMI-*/status"):
            try:
                if status_path.read_text().strip().lower() == "connected":
                    return True
            except OSError:
                continue
    except OSError:
        return False
    return False


def _alsa_card_ids() -> list[str]:
    """Return ALSA card short names in index order (e.g. ``Headphones``)."""

    try:
        text = _ALSA_CARDS_PATH.read_text()
    except OSError:
        return []

    ids: list[str] = []
    for line in text.splitlines():
        # " 0 [Headphones     ]: bcm2835_headpho - bcm2835 Headphones"
        if "[" not in line or "]" not in line:
            continue
        inner = line.split("[", 1)[1].split("]", 1)[0].strip()
        if inner:
            ids.append(inner)
    return ids


def _pick_card_id(cards: list[str], *, want_hdmi: bool) -> str | None:
    if want_hdmi:
        for cid in cards:
            if "hdmi" in cid.lower():
                return cid
        return None
    for cid in cards:
        lower = cid.lower()
        if "hdmi" in lower:
            continue
        if "headphone" in lower or cid == "Headphones":
            return cid
    for cid in cards:
        if "hdmi" not in cid.lower():
            return cid
    return None


def resolve_audio_device() -> str:
    """Choose the mpv ``--audio-device`` target.

    - Explicit ``PI_HUB_AUDIO_DEVICE`` (anything other than ``auto``) wins.
    - ``auto`` / unset: prefer HDMI ALSA when a display is connected,
      otherwise the 3.5mm headphone jack (aux). Uses ``CARD=`` names so
      card index reshuffles don't break routing.
    """

    raw = os.environ.get("PI_HUB_AUDIO_DEVICE", "auto").strip()
    if raw and raw.lower() != "auto":
        return raw

    cards = _alsa_card_ids()
    hdmi_id = _pick_card_id(cards, want_hdmi=True)
    aux_id = _pick_card_id(cards, want_hdmi=False)

    if hdmi_display_connected() and hdmi_id is not None:
        return f"alsa/plughw:CARD={hdmi_id},DEV=0"
    if aux_id is not None:
        return f"alsa/plughw:CARD={aux_id},DEV=0"
    if hdmi_id is not None:
        return f"alsa/plughw:CARD={hdmi_id},DEV=0"
    return "alsa/default"



def ensure_runtime_dirs() -> None:
    """Create directories that the app expects to exist at runtime."""

    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSAVER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def configure_logging() -> None:
    """Configure a simple, readable log format for the app."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

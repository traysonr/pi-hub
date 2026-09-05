"""Tests for HDMI-vs-aux audio device resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from app import config


@pytest.fixture(autouse=True)
def _clear_audio_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PI_HUB_AUDIO_DEVICE", raising=False)


def test_explicit_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PI_HUB_AUDIO_DEVICE", "alsa/plughw:CARD=Headphones,DEV=0")
    assert config.resolve_audio_device() == "alsa/plughw:CARD=Headphones,DEV=0"


def test_auto_prefers_hdmi_when_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PI_HUB_AUDIO_DEVICE", "auto")
    monkeypatch.setattr(config, "hdmi_display_connected", lambda: True)
    monkeypatch.setattr(
        config,
        "_alsa_card_ids",
        lambda: ["Headphones", "vc4hdmi"],
    )
    assert config.resolve_audio_device() == "alsa/plughw:CARD=vc4hdmi,DEV=0"


def test_auto_falls_back_to_aux_when_hdmi_disconnected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "hdmi_display_connected", lambda: False)
    monkeypatch.setattr(
        config,
        "_alsa_card_ids",
        lambda: ["Headphones", "vc4hdmi"],
    )
    assert config.resolve_audio_device() == "alsa/plughw:CARD=Headphones,DEV=0"


def test_auto_uses_hdmi_card_if_no_aux_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "hdmi_display_connected", lambda: False)
    monkeypatch.setattr(config, "_alsa_card_ids", lambda: ["vc4hdmi"])
    assert config.resolve_audio_device() == "alsa/plughw:CARD=vc4hdmi,DEV=0"


def test_alsa_card_ids_parses_proc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cards = tmp_path / "cards"
    cards.write_text(
        " 0 [Headphones     ]: bcm2835_headpho - bcm2835 Headphones\n"
        " 1 [vc4hdmi        ]: vc4-hdmi - vc4-hdmi\n"
    )
    monkeypatch.setattr(config, "_ALSA_CARDS_PATH", cards)
    assert config._alsa_card_ids() == ["Headphones", "vc4hdmi"]


def test_hdmi_display_connected_reads_sysfs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drm = tmp_path / "drm"
    conn = drm / "card0-HDMI-A-1"
    conn.mkdir(parents=True)
    (conn / "status").write_text("connected\n")
    monkeypatch.setattr(config, "_DRM_CLASS_DIR", drm)
    assert config.hdmi_display_connected() is True
    (conn / "status").write_text("disconnected\n")
    assert config.hdmi_display_connected() is False

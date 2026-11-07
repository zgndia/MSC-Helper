"""App constants, Settings model, and settings persistence."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

APP_NAME = "MSC Helper"
APP_VERSION = "0.3.0"
# Package: msc-helper-py/msc_helper/; project root is three levels up.
BASE_DIR = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "MSCHelper"
CONFIG_PATH = CONFIG_DIR / "settings.json"
CAPTURE_PATH = CONFIG_DIR / "last_capture.json"
LOGS_DIR = CONFIG_DIR / "logs"
VERSION_MARK_PATH = CONFIG_DIR / "VERSION"
GITHUB_VERSION_URL = "https://raw.githubusercontent.com/zgndia/MSC-Helper/main/VERSION"
GITHUB_RELEASES_API = "https://api.github.com/repos/zgndia/MSC-Helper/releases/latest"
GITHUB_REPO = "zgndia/MSC-Helper"


@dataclass(frozen=True)
class Settings:
    """User-configurable options. Field names describe the job they control."""
    window_title: str = "MapleStory"
    max_hp: int = 652
    max_mp: int = 516
    hp_threshold: float = 100
    mp_threshold: float = 200
    hp_threshold_mode: str = "points"
    mp_threshold_mode: str = "points"
    hp_random_offset: int = 75
    mp_random_offset: int = 75
    hp_enabled: bool = True
    mp_enabled: bool = True
    hp_key: str = "v"
    mp_key: str = "t"
    potion_cooldown_ms: int = 1000
    hud_scan_interval_ms: int = 500
    key_hold_ms: int = 75
    window_id: int = 0
    auto_detect_max_stats: bool = True
    hud_monitoring_enabled: bool = True
    passive_hud_reading: bool = False
    min_focus_hold_ms: int = 1000
    ocr_interval_ms: int = 1800
    ability_spam_enabled: bool = False
    ability_keys: str = "q, e, r"
    movement_keys: str = "w, a, s, d"
    ability_interval_ms: int = 250
    ability_set_delay_ms: int = 800
    movement_hold_ms: int = 1250
    pause_spam_while_typing: bool = True
    overlay_enabled: bool = True
    overlay_x_offset: int = 18
    overlay_y_offset: int = 42
    overlay_width: int = 390
    overlay_height: int = 315
    auto_update_enabled: bool = True
    last_window_title: str = ""
    last_window_pid: int = 0
    last_window_hwnd: int = 0


def load_settings() -> Settings:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.is_file():
        defaults = Settings()
        save_settings(defaults)
        return defaults
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return Settings()
    if not isinstance(raw, dict):
        return Settings()
    allowed = Settings.__dataclass_fields__.keys()
    cleaned = {k: v for k, v in raw.items() if k in allowed}
    try:
        return Settings(**cleaned)
    except TypeError:
        return Settings()


def save_settings(settings: Settings) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")

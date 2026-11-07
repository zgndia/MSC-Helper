"""Rotating file logging under %APPDATA%/MSCHelper/logs."""
from __future__ import annotations

import logging
import logging.handlers
from datetime import datetime

from msc_helper.config import APP_VERSION, LOGS_DIR

_file_logger: logging.Logger | None = None


def setup_file_logging() -> logging.Logger:
    global _file_logger
    if _file_logger is not None:
        return _file_logger
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("msc_helper")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    log_path = LOGS_DIR / f"msc_helper_{datetime.now().strftime('%Y%m%d')}.log"
    dated = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    dated.setFormatter(fmt)
    logger.addHandler(dated)
    latest_path = LOGS_DIR / "latest.log"
    try:
        latest_path.write_text("", encoding="utf-8")
    except OSError:
        pass
    latest = logging.FileHandler(latest_path, mode="a", encoding="utf-8")
    latest.setFormatter(fmt)
    logger.addHandler(latest)
    logger.propagate = False
    _file_logger = logger
    logger.info("MSC Helper %s started", APP_VERSION)
    return logger


def get_logger() -> logging.Logger:
    return _file_logger or setup_file_logging()

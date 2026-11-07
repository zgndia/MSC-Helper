"""Version checks and update download helpers."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from msc_helper.config import (
    APP_VERSION,
    BASE_DIR,
    GITHUB_RELEASES_API,
    GITHUB_VERSION_URL,
    VERSION_MARK_PATH,
    CONFIG_DIR,
)

def parse_version(value: str) -> tuple[int, ...]:
    """Parse a dotted version string into a comparable integer tuple."""
    parts = re.findall(r"\d+", (value or "").strip().lstrip("vV"))
    return tuple(int(part) for part in parts) if parts else (0,)


def is_newer_version(remote: str, local: str) -> bool:
    """True only when remote is strictly greater than local (semver-ish)."""
    return parse_version(remote) > parse_version(local)


def read_bundled_version() -> str:
    for candidate in (BASE_DIR / "VERSION", Path(getattr(sys, "_MEIPASS", BASE_DIR)) / "VERSION"):
        try:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8").strip() or APP_VERSION
        except OSError:
            continue
    return APP_VERSION


def fetch_remote_version(timeout: float = 8.0) -> str | None:
    try:
        req = urllib.request.Request(
            GITHUB_VERSION_URL,
            headers={"User-Agent": f"MSC-Helper/{APP_VERSION}", "Accept": "text/plain"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace").strip()
    except Exception:
        return None


def fetch_latest_release_asset(timeout: float = 12.0) -> tuple[str, str] | None:
    """Return (download_url, tag_name) for the latest .exe asset, or None."""
    try:
        req = urllib.request.Request(
            GITHUB_RELEASES_API,
            headers={
                "User-Agent": f"MSC-Helper/{APP_VERSION}",
                "Accept": "application/vnd.github+json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tag = str(data.get("tag_name") or data.get("name") or "").lstrip("v").strip()
        assets = data.get("assets") or []
        for asset in assets:
            name = str(asset.get("name") or "").lower()
            url = asset.get("browser_download_url")
            if url and name.endswith(".exe"):
                return str(url), tag or "latest"
        # Fallback: any asset that looks like the main binary
        for asset in assets:
            name = str(asset.get("name") or "").lower()
            url = asset.get("browser_download_url")
            if url and "msc" in name:
                return str(url), tag or "latest"
    except Exception:
        return None
    return None


def download_file(url: str, dest: Path, timeout: float = 120.0) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    req = urllib.request.Request(url, headers={"User-Agent": f"MSC-Helper/{APP_VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as out:
        shutil.copyfileobj(resp, out)
    tmp.replace(dest)


def mark_installed_version(version: str) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        VERSION_MARK_PATH.write_text(version.strip() + "\n", encoding="utf-8")
        # Remove legacy marker name if present.
        legacy = CONFIG_DIR / "installed_version.txt"
        if legacy.is_file() and legacy != VERSION_MARK_PATH:
            try:
                legacy.unlink()
            except OSError:
                pass
    except OSError:
        pass


def read_installed_version_mark() -> str | None:
    try:
        if VERSION_MARK_PATH.is_file():
            return VERSION_MARK_PATH.read_text(encoding="utf-8").strip() or None
        legacy = CONFIG_DIR / "installed_version.txt"
        if legacy.is_file():
            value = legacy.read_text(encoding="utf-8").strip() or None
            if value:
                mark_installed_version(value)
            return value
    except OSError:
        pass
    return None



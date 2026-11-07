"""Keyboard key name parsing and held-key checks."""
from __future__ import annotations

import ctypes
import re
import sys

try:
    from native_keyboard_inputs import KEY_NAMES
    from native_keyboard_inputs.keys import KEY_ALIASES
except ImportError:
    KEY_NAMES = ()
    KEY_ALIASES = {}

from msc_helper.platform_win import _USER32

def key_to_virtual_key(name: str) -> int:
    """Map a supported physical key name to its Windows virtual-key code."""
    key = canonical_key(name)
    if len(key) == 1 and key.isalnum():
        return ord(key.upper())
    if key in MOVEMENT_VK_NAMES:
        return MOVEMENT_VK_NAMES[key]
    if key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        return 0x70 + int(key[1:]) - 1
    raise ValueError(f"{name!r} cannot be monitored as a physical key. Use letters, digits, arrows, Space, Shift, Ctrl, Alt, or F1-F24.")


def parse_monitor_keys(value: str) -> list[tuple[str, int]]:
    raw_items = [item.strip() for item in value.split(",") if item.strip()]
    if not raw_items:
        raise ValueError("Enter at least one movement/listener key, separated by commas.")
    result: list[tuple[str, int]] = []
    seen: set[str] = set()
    for item in raw_items:
        key = canonical_key(item)
        if key not in seen:
            result.append((key, key_to_virtual_key(key)))
            seen.add(key)
    return result


def _held_virtual_keys(codes: list[int] | tuple[int, ...] | set[int]) -> bool:
    if sys.platform != "win32" or _USER32 is None:
        return False
    return any(_USER32.GetAsyncKeyState(vk) & 0x8000 for vk in codes)


def is_movement_key_held(movement_keys: str = "w, a, s, d") -> bool:
    """Return whether a configured movement/listener key is physically held."""
    try:
        codes = [vk for _name, vk in parse_monitor_keys(movement_keys)]
    except ValueError:
        codes = list(VK_MOVEMENT_KEYS[:4])
    return _held_virtual_keys(codes)


def is_space_key_held() -> bool:
    return _held_virtual_keys((0x20,))


def is_typing_key_held(movement_keys: str = "w, a, s, d") -> bool:
    """Detect likely in-game typing without installing a hook or reading text."""
    try:
        ignored = {vk for _name, vk in parse_monitor_keys(movement_keys)} | {0x20}
    except ValueError:
        ignored = set(VK_MOVEMENT_KEYS)
    return _held_virtual_keys(tuple(vk for vk in VK_TYPING_KEYS if vk not in ignored))


def canonical_key(name: str) -> str:
    value = re.sub(r"[\s-]+", "_", name.strip().lower())
    canonical = KEY_ALIASES.get(value, value)
    if canonical not in KEY_NAMES:
        raise ValueError(f"Unknown key {name!r}. Use a supported key name from Native keyboard inputs.")
    return canonical


def parse_key_list(value: str) -> list[str]:
    raw_items = [item.strip() for item in value.split(",") if item.strip()]
    keys: list[str] = []
    for raw in raw_items:
        key = canonical_key(raw)
        if key not in keys:
            keys.append(key)
    if not keys:
        raise ValueError("Enter at least one ability/pickup key, separated by commas.")
    return keys


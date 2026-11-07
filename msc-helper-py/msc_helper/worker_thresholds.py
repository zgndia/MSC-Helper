"""HP/MP threshold and reading helpers for MonitorWorker."""
from __future__ import annotations

import random
from typing import Any


class ThresholdMixin:
    def _update_reading(self, kind: str, reading: BarReading | None, now: float) -> None:
        self._fresh_flags[kind] = reading is not None
        if reading is not None:
            self._last_reading[kind] = reading
            self._last_seen_mono[kind] = now
            self._last_seen_wall[kind] = datetime.now().strftime("%H:%M:%S")

    def _current_points(self, kind: str, maximum: int, ratio: float, now: float) -> int:
        """Prefer a recent exact OCR reading; use the bar ratio between OCR samples."""
        stat_tuple = self._last_stats.get(kind)
        if stat_tuple is not None:
            stat, captured_at = stat_tuple
            age = now - captured_at
            if stat.maximum == maximum and -0.30 <= age <= 1.5:
                return int(stat.current)
        return round(maximum * ratio)

    def _randomized_threshold(self, kind: str, settings: Settings) -> float | int:
        """Roll once per potion cycle; the threshold stays fixed until a pot is sent."""
        maximum = settings.max_hp if kind == "hp" else settings.max_mp
        base = settings.hp_threshold if kind == "hp" else settings.mp_threshold
        offset = settings.hp_random_offset if kind == "hp" else settings.mp_random_offset
        mode = settings.hp_threshold_mode if kind == "hp" else settings.mp_threshold_mode
        if mode == "percent":
            # Keep offsets configured in resource points. Convert them into a
            # percentage-point jitter so the same 75-point setting remains
            # proportionate when the player changes level/max stats.
            delta = (float(offset) / max(1, maximum)) * 100.0
            return round(max(30.0, min(100.0, random.uniform(float(base) - delta, float(base) + delta))), 2)
        rolled = int(round(float(base) + random.randint(-int(offset), int(offset))))
        # In points/missing mode, 70% missing is the latest safe trigger:
        # the pot must be used while at least 30% of the resource remains.
        latest_safe_missing = max(1, int(maximum * 0.70))
        return max(1, min(latest_safe_missing, rolled))

    def _threshold_for(self, kind: str, settings: Settings) -> float | int:
        signature = (
            settings.max_hp, settings.max_mp, settings.hp_threshold, settings.mp_threshold,
            settings.hp_threshold_mode, settings.mp_threshold_mode,
            settings.hp_random_offset, settings.mp_random_offset,
        )
        if signature != self._threshold_signature:
            self._active_thresholds = {"hp": None, "mp": None}
            self._threshold_signature = signature
        value = self._active_thresholds[kind]
        if value is None:
            value = self._randomized_threshold(kind, settings)
            self._active_thresholds[kind] = value
            self.emit_log(f"{kind.upper()} randomized potion threshold set to {value}{'%' if getattr(settings, kind + '_threshold_mode') == 'percent' else ' missing points'}.")
        return value

    @staticmethod
    def _threshold_reached(kind: str, settings: Settings, current: int, maximum: int, threshold: float | int) -> bool:
        mode = settings.hp_threshold_mode if kind == "hp" else settings.mp_threshold_mode
        if mode == "percent":
            return (current / max(1, maximum)) * 100.0 <= float(threshold)
        return (maximum - current) >= int(threshold)


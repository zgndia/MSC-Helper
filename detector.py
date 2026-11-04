"""MapleStory Classic HP/MP HUD detection and numeric OCR.

The supplied HUD images anchor detection. Colored fill provides a fast
fallback estimate; Tesseract OCR reads the displayed current/max numbers so
the live readout and potion checks can use the actual on-screen values.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal
import re
import shutil

import cv2
import numpy as np

try:
    import pytesseract
except ImportError:  # OCR is optional; bar-ratio detection still works.
    pytesseract = None

BarKind = Literal["hp", "mp"]


@dataclass(frozen=True)
class BarReading:
    kind: BarKind
    ratio: float
    confidence: float
    x: int
    y: int
    scale: float


@dataclass(frozen=True)
class StatReading:
    kind: BarKind
    current: int
    maximum: int
    confidence: float
    text: str


@dataclass(frozen=True)
class _Template:
    image: np.ndarray
    width: int
    height: int
    bar_x: int = 1
    bar_y: int = 24
    bar_width: int = 148
    bar_height: int = 17
    text_width: int = 148
    text_height: int = 21


class BarDetector:
    """Template-anchor detector for the supplied MapleStory Classic HUD."""

    SCALES = (1.0, 0.9, 1.1, 0.8, 1.2, 1.25, 0.75)
    MIN_CONFIDENCE = 0.76
    OCR_RATIO_TOLERANCE = 0.014
    OCR_RATIO_VOTE_TOLERANCE = 0.014

    def __init__(self, assets_dir: str | Path | None = None) -> None:
        if assets_dir is None:
            assets_dir = Path(__file__).resolve().parent / "assets"
        assets_dir = Path(assets_dir)
        self.templates: dict[BarKind, _Template] = {
            "hp": self._load_template(assets_dir / "hp_reference.png", "hp"),
            "mp": self._load_template(assets_dir / "mp_reference.png", "mp"),
        }
        self._ocr_available: bool | None = None
        # HUD labels are stationary inside the game client. Cache their last
        # client-area coordinates and scale so the steady-state scan only
        # searches a small neighbourhood instead of matching nine scales
        # across the entire game frame on every poll.
        self._last_locations: dict[BarKind, tuple[int, int, float, tuple[int, ...]]] = {}
        self._configure_tesseract_path()

    @staticmethod
    def _configure_tesseract_path() -> None:
        if pytesseract is None:
            return
        if shutil.which("tesseract"):
            return
        candidates = [
            Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
            Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
            Path.home() / "AppData/Local/Programs/Tesseract-OCR/tesseract.exe",
        ]
        for candidate in candidates:
            if candidate.is_file():
                pytesseract.pytesseract.tesseract_cmd = str(candidate)
                return

    @staticmethod
    def _load_template(path: Path, kind: BarKind) -> _Template:
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is None:
            raise FileNotFoundError(f"Could not load reference image: {path}")

        label_width = 32 if kind == "hp" else 34
        label_height = 20
        label = image[:label_height, :label_width].copy()
        gray = cv2.cvtColor(label, cv2.COLOR_BGR2GRAY)
        if cv2.countNonZero((gray >= 120).astype(np.uint8)) < 30:
            raise ValueError(f"Reference label for {kind.upper()} is unexpectedly blank")
        return _Template(image=label, width=image.shape[1], height=image.shape[0])

    @property
    def ocr_available(self) -> bool:
        """Whether pytesseract and the Tesseract executable are both usable."""
        if self._ocr_available is not None:
            return self._ocr_available
        if pytesseract is None:
            self._ocr_available = False
            return False
        try:
            pytesseract.get_tesseract_version()
        except Exception:
            self._ocr_available = False
        else:
            self._ocr_available = True
        return self._ocr_available

    def locate(self, frame_bgr: np.ndarray, kind: BarKind) -> tuple[int, int, float, float] | None:
        """Return (x, y, confidence, scale) for a detected bar label.

        Once a bar is found, subsequent frames search only a small local region
        at its previous scale. A full multi-scale search is used for the first
        frame, after a resolution change, or when the cached location is lost.
        """
        if frame_bgr is None or frame_bgr.ndim != 3 or frame_bgr.shape[2] != 3:
            raise ValueError("frame_bgr must be a BGR image with 3 channels")

        template = self.templates[kind]
        frame_shape = tuple(int(value) for value in frame_bgr.shape)
        cached = self._last_locations.get(kind)

        if cached is not None and cached[3] == frame_shape:
            cached_x, cached_y, cached_scale, _shape = cached
            width = max(8, round(template.image.shape[1] * cached_scale))
            height = max(8, round(template.image.shape[0] * cached_scale))
            radius = max(32, round(44 * cached_scale))
            left = max(0, cached_x - radius)
            top = max(0, cached_y - radius)
            right = min(frame_bgr.shape[1], cached_x + width + radius)
            bottom = min(frame_bgr.shape[0], cached_y + height + radius)
            local = frame_bgr[top:bottom, left:right]
            if local.shape[0] >= height and local.shape[1] >= width:
                local_gray = cv2.cvtColor(local, cv2.COLOR_BGR2GRAY)
                interpolation = cv2.INTER_AREA if cached_scale < 1.0 else cv2.INTER_CUBIC
                scaled_template = cv2.resize(
                    template.image, (width, height), interpolation=interpolation
                )
                template_gray = cv2.cvtColor(scaled_template, cv2.COLOR_BGR2GRAY)
                response = cv2.matchTemplate(local_gray, template_gray, cv2.TM_CCOEFF_NORMED)
                _, confidence, _, location = cv2.minMaxLoc(response)
                if np.isfinite(confidence) and confidence >= self.MIN_CONFIDENCE:
                    found = (
                        int(left + location[0]), int(top + location[1]),
                        float(confidence), float(cached_scale),
                    )
                    self._last_locations[kind] = (found[0], found[1], found[3], frame_shape)
                    return found

        frame_gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        best: tuple[int, int, float, float] | None = None

        for scale in self.SCALES:
            width = max(8, round(template.image.shape[1] * scale))
            height = max(8, round(template.image.shape[0] * scale))
            if height > frame_gray.shape[0] or width > frame_gray.shape[1]:
                continue

            interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC
            image = cv2.resize(template.image, (width, height), interpolation=interpolation)
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            response = cv2.matchTemplate(frame_gray, gray, cv2.TM_CCOEFF_NORMED)
            _, confidence, _, location = cv2.minMaxLoc(response)
            if not np.isfinite(confidence):
                continue
            if best is None or confidence > best[2]:
                best = (location[0], location[1], float(confidence), scale)
            # The common 1:1 HUD scale is usually a near-exact label match.
            if confidence >= 0.995:
                best = (location[0], location[1], float(confidence), scale)
                break

        if best is None or best[2] < self.MIN_CONFIDENCE:
            self._last_locations.pop(kind, None)
            return None
        self._last_locations[kind] = (best[0], best[1], best[3], frame_shape)
        return best

    def read(self, frame_bgr: np.ndarray, kind: BarKind) -> BarReading | None:
        """Find a bar and estimate its fill ratio from its red/blue pixels."""
        found = self.locate(frame_bgr, kind)
        if found is None:
            return None
        return self.read_at(frame_bgr, kind, found)

    def read_at(
        self,
        frame_bgr: np.ndarray,
        kind: BarKind,
        found: tuple[int, int, float, float],
    ) -> BarReading | None:
        """Measure a bar using an already-located label anchor."""
        x, y, confidence, scale = found
        template = self.templates[kind]
        x1 = max(0, x + round(template.bar_x * scale))
        y1 = max(0, y + round(template.bar_y * scale))
        x2 = min(frame_bgr.shape[1], x + round((template.bar_x + template.bar_width) * scale))
        y2 = min(frame_bgr.shape[0], y + round((template.bar_y + template.bar_height) * scale))
        roi = frame_bgr[y1:y2, x1:x2]
        if roi.size == 0 or roi.shape[1] < 10 or roi.shape[0] < 5:
            return None

        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        if kind == "hp":
            color_mask = cv2.inRange(
                hsv, np.array([0, 70, 40], np.uint8), np.array([12, 255, 255], np.uint8)
            ) | cv2.inRange(
                hsv, np.array([168, 70, 40], np.uint8), np.array([179, 255, 255], np.uint8)
            )
        else:
            color_mask = cv2.inRange(
                hsv, np.array([82, 70, 40], np.uint8), np.array([130, 255, 255], np.uint8)
            )

        column_fill = (color_mask > 0).mean(axis=0) >= 0.35
        ratio = float(np.count_nonzero(column_fill) / len(column_fill))
        return BarReading(kind, max(0.0, min(1.0, ratio)), confidence, x, y, scale)

    @staticmethod
    def _choose_numbers(text: str, visual_ratio: float) -> tuple[int, int, float] | None:
        """Choose OCR digit groups that best agree with the bar's visual ratio.

        Tesseract sometimes reads the HUD's closing bracket as an extra '1'.
        We test suffixes of the current value and prefixes of the maximum value,
        then accept a pair only when its ratio is close to the measured fill.
        """
        groups = re.findall(r"\d+", text)
        if len(groups) < 2 or not 0.015 <= visual_ratio <= 1.0:
            return None
        current_group, maximum_group = groups[0], groups[1]
        best: tuple[int, int, float] | None = None
        best_score = float("inf")

        for current_start in range(max(0, len(current_group) - 7), len(current_group)):
            current_text = current_group[current_start:]
            if not current_text:
                continue
            current = int(current_text)
            for maximum_end in range(1, min(8, len(maximum_group)) + 1):
                maximum = int(maximum_group[:maximum_end])
                if maximum <= 0 or current > maximum:
                    continue
                ratio = current / maximum
                error = abs(ratio - visual_ratio)
                removed = current_start + (len(maximum_group) - maximum_end)
                score = error + removed * 0.002
                if score < best_score:
                    best_score = score
                    best = (current, maximum, error)

        if best is None or best[2] > BarDetector.OCR_RATIO_TOLERANCE:
            return None
        return best

    def read_stats(
        self,
        frame_bgr: np.ndarray,
        kind: BarKind,
        bar_reading: BarReading,
    ) -> StatReading | None:
        """Read the numeric HUD text and cross-check it against the bar fill.

        The full-label crop included the HP/MP glyphs and closing bracket,
        which could be mistaken for digits. This reads only the number field.
        A clean primary OCR result returns immediately; alternate contrast
        passes are reserved for ambiguous or ratio-inconsistent readings.
        """
        if not self.ocr_available:
            return None

        template = self.templates[kind]
        scale = bar_reading.scale
        x1 = max(0, bar_reading.x + round(23 * scale))
        y1 = max(0, bar_reading.y + round(1 * scale))
        x2 = min(frame_bgr.shape[1], bar_reading.x + round(template.text_width * scale))
        y2 = min(frame_bgr.shape[0], bar_reading.y + round(21 * scale))
        crop = frame_bgr[y1:y2, x1:x2]
        if crop.size == 0 or crop.shape[1] < 20 or crop.shape[0] < 6:
            return None

        enlarged = cv2.resize(crop, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
        gray = cv2.cvtColor(enlarged, cv2.COLOR_BGR2GRAY)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 2)).apply(gray)
        thresholded = cv2.threshold(
            clahe, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
        )[1]
        config = (
            "--oem 3 --psm 7 "
            "-c tessedit_char_whitelist=0123456789/ "
            "-c load_system_dawg=0 -c load_freq_dawg=0"
        )

        candidates: list[tuple[int, int, float, str]] = []

        def add_candidate(image: np.ndarray) -> tuple[int, int, float] | None:
            text = pytesseract.image_to_string(image, config=config).strip()
            selected = self._choose_numbers(text, bar_reading.ratio)
            if selected is None:
                return None
            current, maximum, error = selected
            candidates.append((current, maximum, error, text))
            return selected

        def finish(selected: tuple[int, int, float], text: str, consensus: bool) -> StatReading:
            current, maximum, error = selected
            confidence = max(0.0, 1.0 - error / self.OCR_RATIO_TOLERANCE)
            if consensus:
                confidence = min(1.0, confidence + 0.15)
            return StatReading(kind, current, maximum, confidence, text)

        try:
            primary = add_candidate(gray)
            # Prefer a single Tesseract pass. Extra passes are the main source of
            # 500ms+ spikes and are only worth it when the first read is unusable.
            if primary is not None and primary[2] <= 0.012:
                return finish(primary, candidates[-1][3], consensus=False)

            # One fallback pass only (CLAHE). Skip the third threshold pass.
            selected = add_candidate(clahe)
            if selected is not None:
                return finish(selected, candidates[-1][3], consensus=False)
        except Exception:
            return None

        if not candidates:
            return None

        counts = {}
        for current, maximum, _error, _text in candidates:
            pair = (current, maximum)
            counts[pair] = counts.get(pair, 0) + 1
        candidates.sort(
            key=lambda item: (
                counts[(item[0], item[1])],
                -item[2],
                len(item[3]),
            ),
            reverse=True,
        )
        current, maximum, error, text = candidates[0]
        if error > self.OCR_RATIO_TOLERANCE:
            return None
        return finish((current, maximum, error), text, consensus=counts[(current, maximum)] >= 2)

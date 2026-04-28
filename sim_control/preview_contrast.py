from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

import numpy as np


@dataclass
class AutoContrastState:
    low_percentile: float = 0.5
    high_percentile: float = 99.5
    smoothing_alpha: float = 0.25
    max_sample_pixels: int = 262_144
    lo: float | None = None
    hi: float | None = None
    frame_shape: tuple[int, ...] | None = None

    def reset(self) -> None:
        self.lo = None
        self.hi = None
        self.frame_shape = None


def manual_uint16_to_uint8(frame: Any, gray_max: int | float) -> np.ndarray:
    gray_max_value = max(1.0, float(gray_max))
    frame_array = np.asarray(frame, dtype=np.float32)
    scaled = frame_array * (255.0 / gray_max_value)
    return np.clip(scaled, 0, 255).astype(np.uint8)


def auto_uint16_to_uint8(frame: Any, state: AutoContrastState) -> np.ndarray:
    frame_array = np.asarray(frame)
    if frame_array.size == 0:
        return np.zeros(frame_array.shape, dtype=np.uint8)

    if tuple(frame_array.shape) != state.frame_shape:
        state.reset()
        state.frame_shape = tuple(frame_array.shape)

    sample = _sample_pixels(frame_array, state.max_sample_pixels)
    lo, hi = _percentile_limits(sample, state.low_percentile, state.high_percentile)
    if not math.isfinite(lo) or not math.isfinite(hi) or hi <= lo:
        state.lo = lo if math.isfinite(lo) else state.lo
        state.hi = hi if math.isfinite(hi) else state.hi
        return np.zeros(frame_array.shape, dtype=np.uint8)

    if state.lo is None or state.hi is None:
        state.lo = lo
        state.hi = hi
    else:
        alpha = min(1.0, max(0.0, float(state.smoothing_alpha)))
        state.lo = ((1.0 - alpha) * state.lo) + (alpha * lo)
        state.hi = ((1.0 - alpha) * state.hi) + (alpha * hi)

    if state.hi <= state.lo:
        return np.zeros(frame_array.shape, dtype=np.uint8)

    display = (np.asarray(frame_array, dtype=np.float32) - float(state.lo)) * (255.0 / (float(state.hi) - float(state.lo)))
    return np.clip(display, 0, 255).astype(np.uint8)


def _sample_pixels(frame: np.ndarray, max_sample_pixels: int) -> np.ndarray:
    flat = np.ravel(frame)
    max_pixels = max(1, int(max_sample_pixels))
    if flat.size <= max_pixels:
        return flat
    stride = int(math.ceil(flat.size / max_pixels))
    return flat[::stride]


def _percentile_limits(sample: np.ndarray, low_percentile: float, high_percentile: float) -> tuple[float, float]:
    low = min(100.0, max(0.0, float(low_percentile)))
    high = min(100.0, max(0.0, float(high_percentile)))
    if high < low:
        low, high = high, low
    values = np.percentile(sample.astype(np.float32, copy=False), [low, high])
    return float(values[0]), float(values[1])

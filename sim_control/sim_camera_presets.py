from __future__ import annotations

from collections.abc import Sequence
from typing import Final


SIM_CAMERA_SIZE_PRESETS: Final[tuple[tuple[int, int], ...]] = (
    (2304, 2304),
    (1152, 1152),
    (576, 576),
)
DEFAULT_SIM_CAMERA_SIZE: Final[tuple[int, int]] = SIM_CAMERA_SIZE_PRESETS[0]
SIM_CAMERA_SENSOR_SIZE: Final[tuple[int, int]] = DEFAULT_SIM_CAMERA_SIZE
SIM_CAMERA_ROI_STEP_PX: Final[int] = 4
SIM_CAMERA_SIZE_PRESET_LABELS: Final[tuple[str, ...]] = tuple(
    f"{width} x {height}" for width, height in SIM_CAMERA_SIZE_PRESETS
)

_LABEL_TO_SIZE: Final[dict[str, tuple[int, int]]] = {
    label: size for label, size in zip(SIM_CAMERA_SIZE_PRESET_LABELS, SIM_CAMERA_SIZE_PRESETS)
}
_SIZE_TO_LABEL: Final[dict[tuple[int, int], str]] = {
    size: label for label, size in _LABEL_TO_SIZE.items()
}


def _normalize_size_presets(
    presets: Sequence[tuple[int, int]] | None = None,
) -> tuple[tuple[int, int], ...]:
    normalized = tuple((int(width), int(height)) for width, height in (presets or SIM_CAMERA_SIZE_PRESETS))
    return normalized or SIM_CAMERA_SIZE_PRESETS


def build_sim_camera_size_presets(sensor_width: int, sensor_height: int) -> tuple[tuple[int, int], ...]:
    sensor_width = max(1, int(sensor_width))
    sensor_height = max(1, int(sensor_height))
    presets: list[tuple[int, int]] = []
    for divisor in (1, 2, 4):
        size = (max(1, sensor_width // divisor), max(1, sensor_height // divisor))
        if size not in presets:
            presets.append(size)
    return tuple(presets)


def labels_for_sim_camera_size_presets(
    presets: Sequence[tuple[int, int]] | None = None,
) -> tuple[str, ...]:
    return tuple(f"{width} x {height}" for width, height in _normalize_size_presets(presets))


def coerce_sim_camera_size(
    width: int,
    height: int,
    presets: Sequence[tuple[int, int]] | None = None,
) -> tuple[int, int]:
    normalized_presets = _normalize_size_presets(presets)
    size = (int(width), int(height))
    if size in normalized_presets:
        return size
    return normalized_presets[0]


def label_for_sim_camera_size(
    width: int,
    height: int,
    presets: Sequence[tuple[int, int]] | None = None,
) -> str:
    roi_width, roi_height = coerce_sim_camera_size(width, height, presets)
    return f"{roi_width} x {roi_height}"


def size_from_sim_camera_label(
    label: str,
    presets: Sequence[tuple[int, int]] | None = None,
) -> tuple[int, int]:
    normalized_presets = _normalize_size_presets(presets)
    label_to_size = {
        f"{width} x {height}": (width, height)
        for width, height in normalized_presets
    }
    return label_to_size.get(str(label).strip(), normalized_presets[0])


def preset_index_for_sim_camera_size(
    width: int,
    height: int,
    presets: Sequence[tuple[int, int]] | None = None,
) -> int:
    normalized_presets = _normalize_size_presets(presets)
    coerced = coerce_sim_camera_size(width, height, normalized_presets)
    return normalized_presets.index(coerced)


def is_full_frame_sim_camera_size(
    width: int,
    height: int,
    sensor_size: tuple[int, int] | None = None,
    presets: Sequence[tuple[int, int]] | None = None,
) -> bool:
    return coerce_sim_camera_size(width, height, presets) == tuple(sensor_size or SIM_CAMERA_SENSOR_SIZE)


def sim_camera_roi_origin_bounds(
    width: int,
    height: int,
    sensor_size: tuple[int, int] | None = None,
    presets: Sequence[tuple[int, int]] | None = None,
) -> tuple[int, int]:
    roi_width, roi_height = coerce_sim_camera_size(width, height, presets)
    sensor_width, sensor_height = sensor_size or SIM_CAMERA_SENSOR_SIZE
    return (
        max(0, sensor_width - roi_width),
        max(0, sensor_height - roi_height),
    )


def _align_sim_camera_roi_origin(value: int, max_value: int, step_px: int = SIM_CAMERA_ROI_STEP_PX) -> int:
    clamped = min(max(int(value), 0), int(max_value))
    step_px = max(1, int(step_px))
    return int(clamped // step_px) * step_px


def normalize_sim_camera_roi(
    width: int,
    height: int,
    roi_x: int,
    roi_y: int,
    sensor_size: tuple[int, int] | None = None,
    step_px: int = SIM_CAMERA_ROI_STEP_PX,
    presets: Sequence[tuple[int, int]] | None = None,
) -> tuple[int, int, int, int]:
    roi_width, roi_height = coerce_sim_camera_size(width, height, presets)
    if is_full_frame_sim_camera_size(roi_width, roi_height, sensor_size=sensor_size, presets=presets):
        return roi_width, roi_height, 0, 0

    max_x, max_y = sim_camera_roi_origin_bounds(roi_width, roi_height, sensor_size=sensor_size, presets=presets)
    normalized_x = _align_sim_camera_roi_origin(roi_x, max_x, step_px=step_px)
    normalized_y = _align_sim_camera_roi_origin(roi_y, max_y, step_px=step_px)
    return roi_width, roi_height, normalized_x, normalized_y


def fit_image_size_to_bounds(
    image_width: int,
    image_height: int,
    bounds_width: int,
    bounds_height: int,
) -> tuple[int, int]:
    image_width = max(int(image_width), 1)
    image_height = max(int(image_height), 1)
    bounds_width = max(int(bounds_width), 1)
    bounds_height = max(int(bounds_height), 1)

    scale = min(bounds_width / image_width, bounds_height / image_height)
    scaled_width = min(bounds_width, max(1, int(round(image_width * scale))))
    scaled_height = min(bounds_height, max(1, int(round(image_height * scale))))
    return scaled_width, scaled_height

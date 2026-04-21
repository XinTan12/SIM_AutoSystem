from __future__ import annotations

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


def coerce_sim_camera_size(width: int, height: int) -> tuple[int, int]:
    size = (int(width), int(height))
    if size in _SIZE_TO_LABEL:
        return size
    return DEFAULT_SIM_CAMERA_SIZE


def label_for_sim_camera_size(width: int, height: int) -> str:
    return _SIZE_TO_LABEL[coerce_sim_camera_size(width, height)]


def size_from_sim_camera_label(label: str) -> tuple[int, int]:
    return _LABEL_TO_SIZE.get(str(label).strip(), DEFAULT_SIM_CAMERA_SIZE)


def preset_index_for_sim_camera_size(width: int, height: int) -> int:
    coerced = coerce_sim_camera_size(width, height)
    return SIM_CAMERA_SIZE_PRESETS.index(coerced)


def is_full_frame_sim_camera_size(width: int, height: int) -> bool:
    return coerce_sim_camera_size(width, height) == SIM_CAMERA_SENSOR_SIZE


def sim_camera_roi_origin_bounds(width: int, height: int) -> tuple[int, int]:
    roi_width, roi_height = coerce_sim_camera_size(width, height)
    sensor_width, sensor_height = SIM_CAMERA_SENSOR_SIZE
    return (
        max(0, sensor_width - roi_width),
        max(0, sensor_height - roi_height),
    )


def _align_sim_camera_roi_origin(value: int, max_value: int) -> int:
    clamped = min(max(int(value), 0), int(max_value))
    return int(clamped // SIM_CAMERA_ROI_STEP_PX) * SIM_CAMERA_ROI_STEP_PX


def normalize_sim_camera_roi(
    width: int,
    height: int,
    roi_x: int,
    roi_y: int,
) -> tuple[int, int, int, int]:
    roi_width, roi_height = coerce_sim_camera_size(width, height)
    if is_full_frame_sim_camera_size(roi_width, roi_height):
        return roi_width, roi_height, 0, 0

    max_x, max_y = sim_camera_roi_origin_bounds(roi_width, roi_height)
    normalized_x = _align_sim_camera_roi_origin(roi_x, max_x)
    normalized_y = _align_sim_camera_roi_origin(roi_y, max_y)
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

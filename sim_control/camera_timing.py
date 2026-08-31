"""Pure helpers for deriving safe DCAM external-trigger timing."""

from __future__ import annotations

import math
from collections.abc import Mapping
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any


CAMERA_TRIGGER_GAP_SAFETY_MARGIN_US = 500

_CAMERA_CONFIG_SIGNATURE_FIELDS = (
    "device_index",
    "device_label",
    "roi_x",
    "roi_y",
    "roi_width",
    "roi_height",
    "exposure_us",
    "bit_depth",
    "trigger_mode",
)
_CAMERA_CONFIG_INTEGER_FIELDS = {
    "device_index",
    "roi_x",
    "roi_y",
    "roi_width",
    "roi_height",
    "exposure_us",
    "bit_depth",
}


def camera_config_signature(config_or_mapping: Any) -> tuple[int | str, ...]:
    """Return the timing-relevant identity of a camera config or status payload.

    A mapping may either contain the camera fields directly or expose them under
    ``camera_config``, matching controller status payloads.  Fields unrelated to
    camera timing, such as ``timeout_ms``, deliberately do not affect the result.
    """

    source = config_or_mapping
    if isinstance(source, Mapping) and "camera_config" in source:
        source = source["camera_config"]

    values: list[int | str] = []
    for field_name in _CAMERA_CONFIG_SIGNATURE_FIELDS:
        if isinstance(source, Mapping):
            value = source[field_name]
        else:
            value = getattr(source, field_name)
        if field_name in _CAMERA_CONFIG_INTEGER_FIELDS:
            values.append(int(value))
        else:
            values.append(str(value))
    return tuple(values)


def calculate_camera_trigger_gap(
    *,
    exposure_us: int | float,
    readout_s: float | None,
    cyclic_s: float | None,
    min_tb_s: float | None,
) -> dict[str, int | bool | str | None]:
    """Calculate the minimum and recommended exposure-to-trigger gap.

    DCAM defines ``Tb`` as the interval from the end of one exposure to the
    next trigger.  When ``Tx < Tc``, it requires both ``Tb > Tc - Tx`` and
    ``Tb >= MinTB``; otherwise only the latter condition applies.  The return
    value is expressed in whole microseconds and includes a fixed safety margin
    only in ``recommended_inter_frame_gap_us``.

    ``TIMING_READOUTTIME`` is validated and reported by the caller, but is not
    added to the formal DCAM gap formula.
    """

    invalid_reason = _validate_inputs(
        exposure_us=exposure_us,
        readout_s=readout_s,
        cyclic_s=cyclic_s,
        min_tb_s=min_tb_s,
    )
    if invalid_reason is not None:
        return _fallback_metadata(invalid_reason)

    exposure_decimal_us = Decimal(str(exposure_us))
    cyclic_decimal_us = Decimal(str(cyclic_s)) * Decimal(1_000_000)
    min_tb_decimal_us = Decimal(str(min_tb_s)) * Decimal(1_000_000)

    min_tb_requirement_us = int(min_tb_decimal_us.to_integral_value(rounding=ROUND_CEILING))
    cyclic_requirement_us = 0
    if cyclic_decimal_us > 0 and exposure_decimal_us < cyclic_decimal_us:
        strict_boundary_us = cyclic_decimal_us - exposure_decimal_us
        cyclic_requirement_us = int(strict_boundary_us.to_integral_value(rounding=ROUND_FLOOR)) + 1

    theoretical_gap_us = max(min_tb_requirement_us, cyclic_requirement_us)
    return {
        "theoretical_min_inter_frame_gap_us": theoretical_gap_us,
        "inter_frame_gap_safety_margin_us": CAMERA_TRIGGER_GAP_SAFETY_MARGIN_US,
        "recommended_inter_frame_gap_us": theoretical_gap_us + CAMERA_TRIGGER_GAP_SAFETY_MARGIN_US,
        "timing_fallback_used": False,
        "timing_fallback_reason": None,
    }


def _validate_inputs(
    *,
    exposure_us: int | float,
    readout_s: float | None,
    cyclic_s: float | None,
    min_tb_s: float | None,
) -> str | None:
    try:
        exposure_value = float(exposure_us)
    except (TypeError, ValueError, OverflowError):
        return "exposure_us is invalid; expected a finite value greater than 0."
    if not math.isfinite(exposure_value) or exposure_value <= 0:
        return "exposure_us is invalid; expected a finite value greater than 0."

    timing_inputs = (
        ("TIMING_READOUTTIME", readout_s, True),
        ("TIMING_CYCLICTRIGGERPERIOD", cyclic_s, False),
        ("TIMING_MINTRIGGERBLANKING", min_tb_s, False),
    )
    for property_name, value, strictly_positive in timing_inputs:
        if value is None:
            return f"{property_name} is unavailable."
        try:
            numeric_value = float(value)
        except (TypeError, ValueError, OverflowError):
            return f"{property_name} is invalid; expected a finite value in seconds."
        lower_bound_is_invalid = numeric_value <= 0 if strictly_positive else numeric_value < 0
        if not math.isfinite(numeric_value) or lower_bound_is_invalid:
            comparison = "> 0" if strictly_positive else ">= 0"
            return f"{property_name} is invalid; expected a finite value {comparison} seconds."
    return None


def _fallback_metadata(reason: str) -> dict[str, int | bool | str | None]:
    return {
        "theoretical_min_inter_frame_gap_us": None,
        "inter_frame_gap_safety_margin_us": CAMERA_TRIGGER_GAP_SAFETY_MARGIN_US,
        "recommended_inter_frame_gap_us": None,
        "timing_fallback_used": True,
        "timing_fallback_reason": reason,
    }

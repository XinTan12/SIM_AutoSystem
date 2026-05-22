"""Qt-free z-scan autofocus routine used before formal SIM9 acquisition."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable

import numpy as np

from .focus_metrics import sum_modified_laplacian
from .models import CameraConfig, DaqLineConfig, TimingConfig, ZScanConfig
from .waveform import NIDaqWaveformBuilder


class ZScanCancelled(RuntimeError):
    """Raised when the z-scan stop event is set."""


@dataclass(frozen=True)
class ZFocusPoint:
    z_um: float
    focus_score: float


@dataclass(frozen=True)
class ZScanResult:
    best_z_um: float
    focus_curve: list[ZFocusPoint]
    exposure_actual_us: int
    captured_stack: np.ndarray | None = None


StatusCallback = Callable[[str, dict[str, Any]], None]


def _noop_status(event: str, payload: dict[str, Any]) -> None:
    return None


def _raise_if_cancelled(stop_event: Any | None) -> None:
    if stop_event is not None and stop_event.is_set():
        raise ZScanCancelled("Z-scan cancelled.")


def scan_positions(config: ZScanConfig, stage_position_um: float) -> list[float]:
    start_um = stage_position_um if config.start_um is None else float(config.start_um)
    direction_sign = 1.0 if config.direction == "positive_z" else -1.0
    move_count = int(config.num_steps)
    return [start_um + (direction_sign * float(config.step_um) * index) for index in range(move_count + 1)]


_scan_positions = scan_positions


def run_z_scan(
    *,
    stage_adapter: Any,
    camera_adapter: Any,
    slm_adapter: Any,
    daq_adapter: Any,
    daq_config: DaqLineConfig,
    camera_config: CameraConfig,
    timing: TimingConfig,
    z_scan_config: ZScanConfig,
    waveform_builder: NIDaqWaveformBuilder | None = None,
    stop_event: Any | None = None,
    on_status: StatusCallback | None = None,
    keep_captured_stack: bool = False,
) -> ZScanResult:
    """Run a single-frame-per-position z-scan and move to the best focus plane."""
    if z_scan_config.focus_metric != "sml":
        raise ValueError(f"Unsupported z-scan focus metric: {z_scan_config.focus_metric}")

    callback = on_status or _noop_status
    builder = waveform_builder or NIDaqWaveformBuilder()
    original_z_um = float(stage_adapter.get_position_um())
    positions = _scan_positions(z_scan_config, original_z_um)
    if len(positions) < 2:
        raise ValueError("z_scan_config.num_steps must be >= 1.")

    actual_exposure_us = int(z_scan_config.actual_exposure_us)
    z_camera_config = replace(camera_config, exposure_us=actual_exposure_us)
    camera_adapter.apply_config(z_camera_config)
    focus_curve: list[ZFocusPoint] = []
    captured_frames: list[np.ndarray] | None = [] if keep_captured_stack else None
    cancel_return_z_um = positions[0]

    try:
        for index, z_um in enumerate(positions, start=1):
            _raise_if_cancelled(stop_event)
            stage_adapter.move_z_um(z_um)
            callback(
                "z_scan_stage_positioned",
                {
                    "step_index": index,
                    "total_steps": len(positions),
                    "z_um": float(z_um),
                },
            )
            _raise_if_cancelled(stop_event)

            armed = False
            try:
                camera_adapter.arm(1)
                armed = True
                slm_adapter.activate_prepared_patterns()
                plan = builder.build_z_scan(
                    daq_config=daq_config,
                    timing=timing,
                    exposure_us=actual_exposure_us,
                    include_role_matrix=False,
                )
                if plan.warnings:
                    callback("z_scan_waveform_warning", {"warnings": plan.warnings})
                daq_adapter.play_waveform(daq_config.device_name, plan, stop_event=stop_event)
                stack, _timestamps = camera_adapter.read_frame_sequence(
                    1,
                    pattern_files=["zscan3p"],
                    laser_wavelength_nm=488,
                    stop_event=stop_event,
                )
            finally:
                if armed:
                    camera_adapter.disarm()
                daq_adapter.set_all_low(daq_config.device_name)

            _raise_if_cancelled(stop_event)
            array = np.asarray(stack)
            if array.ndim != 3 or array.shape[0] < 1:
                raise RuntimeError(f"Z-scan camera returned invalid stack shape: {array.shape!r}")
            frame = np.asarray(array[0], dtype=np.uint16)
            score = sum_modified_laplacian(frame)
            point = ZFocusPoint(z_um=float(z_um), focus_score=float(score))
            focus_curve.append(point)
            if captured_frames is not None:
                captured_frames.append(np.array(frame, dtype=np.uint16, copy=True))
            callback(
                "z_scan_progress",
                {
                    "step_index": index,
                    "total_steps": len(positions),
                    "z_um": float(z_um),
                    "focus_score": float(score),
                },
            )

        best_point = max(focus_curve, key=lambda point: point.focus_score)
        stage_adapter.move_z_um(best_point.z_um)
        captured_stack = (
            np.stack(captured_frames, axis=0).astype(np.uint16, copy=False)
            if captured_frames is not None
            else None
        )
        result = ZScanResult(
            best_z_um=best_point.z_um,
            focus_curve=focus_curve,
            exposure_actual_us=actual_exposure_us,
            captured_stack=captured_stack,
        )
        callback(
            "z_scan_complete",
            {
                "best_z_um": result.best_z_um,
                "focus_curve": [(point.z_um, point.focus_score) for point in result.focus_curve],
                "exposure_actual_us": result.exposure_actual_us,
            },
        )
        return result
    except ZScanCancelled:
        if z_scan_config.return_to_start_on_cancel:
            stage_adapter.move_z_um(cancel_return_z_um)
        raise
    except Exception:
        if stop_event is not None and stop_event.is_set() and z_scan_config.return_to_start_on_cancel:
            stage_adapter.move_z_um(cancel_return_z_um)
        raise


__all__ = ["ZFocusPoint", "ZScanResult", "ZScanCancelled", "scan_positions", "run_z_scan"]

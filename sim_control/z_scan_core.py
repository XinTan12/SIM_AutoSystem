"""Qt-free z-scan autofocus routine used before formal SIM9 acquisition."""

from __future__ import annotations

from dataclasses import dataclass, replace
import logging
import time
from typing import Any, Callable

import numpy as np

from .errors import HardwareError
from .focus_metrics import sum_modified_laplacian
from .models import CameraConfig, DaqLineConfig, TimingConfig, ZScanConfig, new_task_id
from .waveform import NIDaqWaveformBuilder


class ZScanCancelled(RuntimeError):
    """Raised when the z-scan stop event is set."""


@dataclass(frozen=True)
class ZFocusPoint:
    z_um: float
    focus_score: float


@dataclass(frozen=True)
class FocusFrameRecord:
    """One autofocus diagnostic frame with commanded and measured Z metadata."""

    task_id: str
    plane_index: int
    total_layers: int
    requested_z_um: float
    measured_z_um: float
    wavelength_nm: int
    exposure_actual_us: int
    timestamp: float
    score: float
    frame: np.ndarray


@dataclass(frozen=True)
class ZScanResult:
    best_z_um: float
    focus_curve: list[ZFocusPoint]
    exposure_actual_us: int
    captured_stack: np.ndarray | None = None


StatusCallback = Callable[[str, dict[str, Any]], None]
FocusFrameCallback = Callable[[FocusFrameRecord], None]

logger = logging.getLogger(__name__)


def _noop_status(event: str, payload: dict[str, Any]) -> None:
    return None


def _report_restore_warning(
    callback: StatusCallback,
    event: str,
    payload: dict[str, Any],
) -> None:
    try:
        callback(event, payload)
    except Exception:
        logger.warning(
            "Failed to report Z-scan restore warning %s without masking the scan error.",
            event,
            exc_info=True,
        )


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
    laser_wavelength_nm: int = 488,
    task_id: str | None = None,
    waveform_builder: NIDaqWaveformBuilder | None = None,
    stop_event: Any | None = None,
    on_status: StatusCallback | None = None,
    on_focus_frame: FocusFrameCallback | None = None,
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
    laser_wavelength_nm = int(laser_wavelength_nm)
    task_id = str(task_id or new_task_id())
    z_camera_config = replace(camera_config, exposure_us=actual_exposure_us)
    focus_curve: list[ZFocusPoint] = []
    captured_frames: list[np.ndarray] | None = [] if keep_captured_stack else None
    # Build the complete Z-scan waveform before applying camera state or moving
    # the stage.  A bad DAQ mapping/timing combination must fail closed while
    # the specimen is still at the starting position.
    waveform_plan = builder.build_z_scan(
        daq_config=daq_config,
        timing=timing,
        exposure_us=actual_exposure_us,
        laser_wavelength_nm=laser_wavelength_nm,
        include_role_matrix=False,
    )
    waveform_warnings = list(getattr(waveform_plan, "warnings", []))

    try:
        camera_adapter.apply_config(z_camera_config)
        for plane_index, requested_z_um in enumerate(positions):
            index = plane_index + 1
            _raise_if_cancelled(stop_event)
            cycle_started_s = time.perf_counter()
            from_z_um = float(stage_adapter.get_position_um())
            move_started_s = time.perf_counter()
            stage_adapter.move_z_um(requested_z_um)
            measured_z_um = float(stage_adapter.get_position_um())
            move_ms = (time.perf_counter() - move_started_s) * 1000.0
            callback(
                "z_scan_stage_positioned",
                {
                    "task_id": task_id,
                    "plane_index": plane_index,
                    "step_index": index,
                    "total_steps": len(positions),
                    "from_z_um": from_z_um,
                    "z_um": measured_z_um,
                    "requested_z_um": float(requested_z_um),
                    "measured_z_um": measured_z_um,
                    "distance_um": abs(measured_z_um - from_z_um),
                    "move_ms": float(move_ms),
                },
            )
            _raise_if_cancelled(stop_event)

            armed = False
            try:
                camera_adapter.arm(1)
                armed = True
                slm_adapter.activate_prepared_patterns()
                if waveform_warnings:
                    callback("z_scan_waveform_warning", {"warnings": list(waveform_warnings)})
                daq_adapter.play_waveform(daq_config.device_name, waveform_plan, stop_event=stop_event)
                stack, timestamps = camera_adapter.read_frame_sequence(
                    1,
                    pattern_files=["zscan3p"],
                    laser_wavelength_nm=laser_wavelength_nm,
                    stop_event=stop_event,
                )
            finally:
                if armed:
                    camera_adapter.disarm()
                daq_adapter.set_all_low(daq_config.device_name)

            _raise_if_cancelled(stop_event)
            array = np.asarray(stack)
            if array.ndim != 3 or array.shape[0] != 1 or array.shape[1] < 1 or array.shape[2] < 1:
                raise HardwareError(f"Z-scan camera returned invalid stack shape: {array.shape!r}")
            if array.dtype != np.uint16:
                raise HardwareError(
                    f"Z-scan camera returned frame dtype {array.dtype}; expected uint16."
                )
            try:
                timestamp_count = len(timestamps)
            except TypeError as exc:
                raise HardwareError("Z-scan camera returned invalid timestamps; expected exactly 1.") from exc
            if timestamp_count != 1:
                raise HardwareError(
                    f"Z-scan camera returned {timestamp_count} timestamps; expected exactly 1."
                )
            try:
                timestamp = float(timestamps[0])
            except (TypeError, ValueError, IndexError) as exc:
                raise HardwareError("Z-scan camera returned a non-numeric timestamp.") from exc
            if not np.isfinite(timestamp):
                raise HardwareError("Z-scan camera returned a non-finite timestamp.")
            frame = np.array(array[0], copy=True)
            frame.setflags(write=False)
            score = sum_modified_laplacian(frame)
            record = FocusFrameRecord(
                task_id=task_id,
                plane_index=plane_index,
                total_layers=len(positions),
                requested_z_um=float(requested_z_um),
                measured_z_um=measured_z_um,
                wavelength_nm=laser_wavelength_nm,
                exposure_actual_us=actual_exposure_us,
                timestamp=timestamp,
                score=float(score),
                frame=frame,
            )
            if on_focus_frame is not None:
                on_focus_frame(record)
            point = ZFocusPoint(z_um=measured_z_um, focus_score=float(score))
            focus_curve.append(point)
            if captured_frames is not None:
                captured_frames.append(frame)
            callback(
                "z_scan_progress",
                {
                    "task_id": task_id,
                    "plane_index": plane_index,
                    "step_index": index,
                    "total_steps": len(positions),
                    "z_um": measured_z_um,
                    "requested_z_um": float(requested_z_um),
                    "measured_z_um": measured_z_um,
                    "timestamp": timestamp,
                    "layer_shape": list(frame.shape),
                    "focus_score": float(score),
                    "move_ms": float(move_ms),
                    "cycle_ms": float((time.perf_counter() - cycle_started_s) * 1000.0),
                },
            )

        best_point = max(focus_curve, key=lambda point: point.focus_score)
        best_from_z_um = float(stage_adapter.get_position_um())
        best_move_started_s = time.perf_counter()
        stage_adapter.move_z_um(best_point.z_um)
        best_move_ms = (time.perf_counter() - best_move_started_s) * 1000.0
        callback(
            "z_scan_best_focus_positioned",
            {
                "task_id": task_id,
                "from_z_um": best_from_z_um,
                "z_um": float(best_point.z_um),
                "distance_um": abs(float(best_point.z_um) - best_from_z_um),
                "move_ms": float(best_move_ms),
            },
        )
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
                "task_id": task_id,
                "best_z_um": result.best_z_um,
                "focus_curve": [(point.z_um, point.focus_score) for point in result.focus_curve],
                "exposure_actual_us": result.exposure_actual_us,
            },
        )
        return result
    finally:
        try:
            daq_adapter.set_all_low(daq_config.device_name)
        except Exception:
            logger.warning("Failed to set DAQ outputs low during z-scan cleanup.", exc_info=True)


@dataclass(frozen=True)
class ZScanStageMoveResult:
    """Result of a stage-only z-scan (no camera capture / focus scoring)."""

    positions_visited: list[float]
    move_latencies_ms: list[float]
    started_from_um: float


def preflight_z_scan_positions(stage_adapter: Any, z_scan_config: ZScanConfig) -> list[float]:
    """Compute and range-check z-scan target positions before any stage motion.

    Qt-free port of ``SimControlWindow._zscan_positions_checked``: ensures the
    stage adapter is connected, derives the scan positions from the *current*
    stage position and raises before moving if any target is out of range.
    """
    if stage_adapter is None:
        raise HardwareError("No Z stage adapter is available.")
    if not getattr(stage_adapter, "is_connected", False):
        stage_adapter.connect()
    positions = scan_positions(z_scan_config, stage_position_um=float(stage_adapter.get_position_um()))
    if len(positions) < 2:
        raise ValueError("z_scan_config.num_steps must be >= 1.")
    min_um, max_um = stage_adapter.get_z_ranges_um()
    out_of_range = [z for z in positions if not (float(min_um) <= float(z) <= float(max_um))]
    if out_of_range:
        preview = ", ".join(f"{z:.3f}" for z in out_of_range[:5])
        suffix = f" ... (共 {len(out_of_range)} 个越界)" if len(out_of_range) > 5 else ""
        raise HardwareError(
            f"Z-Scan 目标位置超出位移台量程 [{float(min_um):.3f}, {float(max_um):.3f}] um："
            f"{preview}{suffix}"
        )
    return [float(z) for z in positions]


def run_z_scan_stage_only(
    *,
    stage_adapter: Any,
    z_scan_config: ZScanConfig,
    stop_event: Any | None = None,
    on_status: StatusCallback | None = None,
) -> ZScanStageMoveResult:
    """Move the Z stage through the scan positions without any camera capture.

    Used to time / validate stage motion in isolation. Performs a full
    out-of-range preflight before any motion, is cancellable between moves and
    stays at the final position (does not return to the start).
    """
    callback = on_status or _noop_status
    positions = preflight_z_scan_positions(stage_adapter, z_scan_config)
    latencies: list[float] = []
    prev_z = positions[0]
    total_steps = len(positions) - 1
    for index, z_um in enumerate(positions):
        _raise_if_cancelled(stop_event)
        from_z_um = positions[0] if index == 0 else prev_z
        move_started_s = time.perf_counter()
        stage_adapter.move_z_um(z_um)
        move_ms = (time.perf_counter() - move_started_s) * 1000.0
        latencies.append(float(move_ms))
        callback(
            "z_scan_stage_positioned",
            {
                "step_index": index,
                "total_steps": total_steps,
                "from_z_um": float(from_z_um),
                "z_um": float(z_um),
                "distance_um": abs(float(z_um) - float(from_z_um)),
                "move_ms": float(move_ms),
            },
        )
        prev_z = z_um
    return ZScanStageMoveResult(
        positions_visited=positions,
        move_latencies_ms=latencies,
        started_from_um=positions[0],
    )


def run_z_scan_autofocus(
    *,
    stage_adapter: Any,
    camera_adapter: Any,
    slm_adapter: Any,
    daq_adapter: Any,
    daq_config: DaqLineConfig,
    camera_config: CameraConfig,
    timing: TimingConfig,
    z_scan_config: ZScanConfig,
    laser_wavelength_nm: int = 488,
    task_id: str | None = None,
    waveform_builder: NIDaqWaveformBuilder | None = None,
    stop_event: Any | None = None,
    on_status: StatusCallback | None = None,
    on_focus_frame: FocusFrameCallback | None = None,
    keep_captured_stack: bool = False,
) -> ZScanResult:
    """Range-check, connect-check, select the z-scan RO, then run ``run_z_scan``.

    Wraps :func:`run_z_scan` with the preconditions the GUI normally enforces:
    a full out-of-range preflight (raises before any motion), camera/SLM
    connection checks and automatic selection of the current wavelength's
    three-phase z-scan Running Order matching the configured exposure preset.
    """
    # Running Order helpers 局部 import：``adapters`` 体量大且含 ctypes /
    # SDK import 副作用，而本模块刻意保持 Qt-free 轻量；局部 import 同时避免
    # ``z_scan_core`` 经 ``adapters`` 形成潜在 import 环。
    from sim_control.adapters import find_best_running_order, find_z_scan_running_order

    preflight_z_scan_positions(stage_adapter, z_scan_config)
    if not camera_adapter.is_connected():
        raise HardwareError("Z-Scan 拍图需要相机已连接。")
    if not slm_adapter.is_connected():
        raise HardwareError("Z-Scan 拍图需要 SLM 已连接。")
    running_orders = slm_adapter.list_running_orders()
    formal_idx, formal_name, formal_warnings = find_best_running_order(
        running_orders,
        wavelength_nm=int(laser_wavelength_nm),
        exposure_us=int(camera_config.exposure_us),
    )
    if formal_idx is None:
        raise HardwareError(
            "; ".join(formal_warnings) or "No matching formal SIM running order found."
        )
    z_scan_idx, z_scan_name, z_scan_warnings = find_z_scan_running_order(
        running_orders,
        wavelength_nm=int(laser_wavelength_nm),
        exposure_preset_ms=int(z_scan_config.exposure_preset_ms),
    )
    if z_scan_idx is None:
        raise HardwareError(
            "; ".join(z_scan_warnings) or "No matching z-scan running order found."
        )

    callback = on_status or _noop_status
    for warning in formal_warnings:
        callback("formal_running_order_warning", {"message": warning})
    for warning in z_scan_warnings:
        callback("z_scan_running_order_warning", {"message": warning})

    result: ZScanResult | None = None
    operation_error: Exception | None = None
    try:
        slm_adapter.select_running_order(z_scan_idx)
        callback(
            "z_scan_running_order_selected",
            {
                "running_order_index": int(z_scan_idx),
                "running_order_name": z_scan_name,
            },
        )
        result = run_z_scan(
            stage_adapter=stage_adapter,
            camera_adapter=camera_adapter,
            slm_adapter=slm_adapter,
            daq_adapter=daq_adapter,
            daq_config=daq_config,
            camera_config=camera_config,
            timing=timing,
            z_scan_config=z_scan_config,
            laser_wavelength_nm=int(laser_wavelength_nm),
            task_id=task_id,
            waveform_builder=waveform_builder,
            stop_event=stop_event,
            on_status=on_status,
            on_focus_frame=on_focus_frame,
            keep_captured_stack=keep_captured_stack,
        )
    except Exception as exc:
        operation_error = exc
        raise
    finally:
        restore_errors: list[tuple[str, Exception]] = []
        try:
            slm_adapter.select_running_order(formal_idx)
            callback(
                "running_order_restored",
                {
                    "running_order_index": int(formal_idx),
                    "running_order_name": formal_name,
                },
            )
        except Exception as restore_exc:
            restore_message = (
                "Failed to restore formal SIM running order "
                f"{formal_name!r} (index {int(formal_idx)}): {restore_exc}"
            )
            _report_restore_warning(
                callback,
                "running_order_restore_warning",
                {"message": restore_message},
            )
            restore_errors.append((restore_message, restore_exc))
        try:
            camera_adapter.apply_config(camera_config)
        except Exception as restore_exc:
            restore_message = (
                "Failed to restore formal camera configuration after Z-scan: "
                f"{restore_exc}"
            )
            _report_restore_warning(
                callback,
                "camera_config_restore_warning",
                {"message": restore_message},
            )
            restore_errors.append((restore_message, restore_exc))
        if restore_errors:
            restore_message = "\n".join(message for message, _error in restore_errors)
            if operation_error is not None:
                raise HardwareError(
                    f"{operation_error}\n{restore_message}"
                ) from operation_error
            raise HardwareError(restore_message) from restore_errors[0][1]
    if result is None:  # pragma: no cover - defensive type narrowing
        raise HardwareError("Z-scan completed without a result.")
    return result


__all__ = [
    "FocusFrameCallback",
    "FocusFrameRecord",
    "ZFocusPoint",
    "ZScanResult",
    "ZScanStageMoveResult",
    "ZScanCancelled",
    "scan_positions",
    "run_z_scan",
    "preflight_z_scan_positions",
    "run_z_scan_stage_only",
    "run_z_scan_autofocus",
]

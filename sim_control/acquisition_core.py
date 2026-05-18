"""脱离 Qt 的单次 SIM 采集核心。

这个文件把一次 9 帧采集拆成纯 Python 流程：校验取消状态，应用相机配置，准备相机/SLM/DAQ，播放 USB-6423 波形，读取图像栈，并在 finally 中确保相机 disarm 与 DAQ 全低。controller 和 GUI 只负责调度它，硬件动作通过 adapter 协议注入。
"""

from __future__ import annotations

from typing import Any, Callable

import numpy as np

from .adapters import HardwareError
from .models import (
    AcquisitionBatch,
    CameraConfig,
    DaqLineConfig,
    PatternPreparationResult,
    SimTaskConfig,
    new_task_id,
)
from .protocols import CameraAdapter, DaqAdapter, SlmAdapter
from .waveform import NIDaqWaveformBuilder, validate_daq_line_config

StatusCallback = Callable[[str, dict[str, Any]], None]
FrameCallback = Callable[[int, float], None]


class AcquisitionCancelled(RuntimeError):
    """表示采集流程被用户或 stop_event 主动取消，区别于硬件错误。"""
    pass


def _noop_status(status: str, payload: dict[str, Any]) -> None:
    """默认状态回调占位，让采集核心可在无 UI 时复用。"""
    pass


def _stop_requested(stop_event: Any | None) -> bool:
    """集中检查停止信号，兼容 threading.Event 和测试里的轻量替身。"""
    return bool(stop_event is not None and stop_event.is_set())


def _raise_if_cancelled(stop_event: Any | None) -> None:
    """把停止请求转换为采集取消异常，统一交给上层控制器收尾。"""
    if _stop_requested(stop_event):
        raise AcquisitionCancelled("Acquisition cancelled.")


def _validate_acquisition_result(stack: Any, timestamps: list[float], expected_frames: int) -> np.ndarray:
    """集中校验输入条件，把错误尽早转成可报告的问题。"""
    if not isinstance(stack, np.ndarray):
        raise HardwareError("Camera returned a non-NumPy acquisition stack.")
    if stack.ndim != 3:
        raise HardwareError(f"Camera returned stack with ndim={stack.ndim}; expected 3.")
    if stack.shape[0] != expected_frames:
        raise HardwareError(f"Camera returned {stack.shape[0]} frames; expected {expected_frames}.")
    if stack.shape[1] <= 0 or stack.shape[2] <= 0:
        raise HardwareError(f"Camera returned invalid frame shape {stack.shape[1:]}.")
    if stack.dtype != np.uint16:
        raise HardwareError(f"Camera returned stack dtype {stack.dtype}; expected uint16.")
    if len(timestamps) != expected_frames:
        raise HardwareError(f"Camera returned {len(timestamps)} timestamps; expected {expected_frames}.")
    return stack


# 核心采集流程保持脱离 Qt，便于 GUI worker、测试和未来自动化入口复用同一套硬件顺序。
def run_single_acquisition(
    task: SimTaskConfig,
    daq_config: DaqLineConfig,
    pattern_result: PatternPreparationResult,
    camera: CameraAdapter,
    slm: SlmAdapter,
    daq: DaqAdapter,
    waveform_builder: NIDaqWaveformBuilder | None = None,
    task_id: str | None = None,
    on_status: StatusCallback = _noop_status,
    stop_event: Any | None = None,
) -> AcquisitionBatch:
    """Run a synchronous 9-frame SIM acquisition. No Qt dependency.

    Raises on failure; always disarms camera and resets DAQ in finally block.
    """
    if waveform_builder is None:
        waveform_builder = NIDaqWaveformBuilder()
    if task_id is None:
        task_id = new_task_id()

    on_status("acquisition_starting", {"task_id": task_id, "laser_wavelength_nm": task.laser_wavelength_nm})

    plan = waveform_builder.build(
        daq_config=daq_config,
        timing=task.timing,
        laser_wavelength_nm=task.laser_wavelength_nm,
        exposure_us=task.camera.exposure_us,
        frame_count=9,
        include_role_matrix=False,
    )
    on_status("waveform_ready", {"task_id": task_id, "sample_count": plan.sample_count, "duration_s": plan.duration_s})
    for warning in getattr(plan, "warnings", []):
        on_status("waveform_warning", {"task_id": task_id, "message": warning})

    emitted_frames: set[int] = set()

    def emit_frame_captured(frame_index: int, timestamp: float) -> None:
        emitted_frames.add(int(frame_index))
        on_status("frame_captured", {"task_id": task_id, "frame_index": int(frame_index), "timestamp": float(timestamp)})

    # 从 arm 到 DAQ 播放再到读帧必须保证 finally 清理，避免异常后硬件停在触发或高电平状态。
    try:
        _raise_if_cancelled(stop_event)
        camera.apply_config(task.camera)
        _raise_if_cancelled(stop_event)
        camera.arm(frame_count=9)
        _raise_if_cancelled(stop_event)
        slm.activate_prepared_patterns()
        _raise_if_cancelled(stop_event)
        try:
            daq.play_waveform(daq_config.device_name, plan, stop_event=stop_event)
        except Exception as exc:
            if _stop_requested(stop_event):
                raise AcquisitionCancelled("Acquisition cancelled.") from exc
            raise
        _raise_if_cancelled(stop_event)
        try:
            stack, timestamps = camera.read_frame_sequence(
                frame_count=9,
                pattern_files=pattern_result.pattern_files,
                laser_wavelength_nm=task.laser_wavelength_nm,
                frame_callback=emit_frame_captured,
                stop_event=stop_event,
            )
        except Exception as exc:
            if _stop_requested(stop_event):
                raise AcquisitionCancelled("Acquisition cancelled.") from exc
            raise
        _raise_if_cancelled(stop_event)
    finally:
        try:
            camera.disarm()
        except Exception:
            pass
        try:
            daq.set_all_low(daq_config.device_name)
        except Exception:
            pass

    stack = _validate_acquisition_result(stack, timestamps, expected_frames=9)

    for index, timestamp in enumerate(timestamps, start=1):
        if index not in emitted_frames:
            emit_frame_captured(index, timestamp)

    on_status("acquisition_complete", {"task_id": task_id, "stack_shape": list(stack.shape)})

    return AcquisitionBatch(
        task_id=task_id,
        stack=stack,
        timestamps=timestamps,
        laser_wavelength_nm=task.laser_wavelength_nm,
        exposure_us=task.camera.exposure_us,
        pattern_files=list(pattern_result.pattern_files),
        metadata={
            "pattern_handles": list(pattern_result.handles),
            "pattern_mode": pattern_result.metadata.get("mode", ""),
            "running_order_name": task.running_order_name
            or str(pattern_result.metadata.get("running_order_name", "")),
            "waveform": plan.metadata,
            "daq_device": daq_config.device_name,
        },
    )

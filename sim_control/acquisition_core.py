from __future__ import annotations

from typing import Any, Callable

import numpy as np

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


def _noop_status(status: str, payload: dict[str, Any]) -> None:
    pass


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

    try:
        camera.apply_config(task.camera)
        camera.arm(frame_count=9)
        slm.activate_prepared_patterns()
        daq.play_waveform(daq_config.device_name, plan)
        stack, timestamps = camera.read_frame_sequence(
            frame_count=9,
            pattern_files=pattern_result.pattern_files,
            laser_wavelength_nm=task.laser_wavelength_nm,
            frame_callback=emit_frame_captured,
        )
    finally:
        try:
            camera.disarm()
        except Exception:
            pass
        try:
            daq.set_all_low(daq_config.device_name)
        except Exception:
            pass

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

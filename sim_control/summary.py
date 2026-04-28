from __future__ import annotations

from typing import Any

from .models import AppConfig


def _sim_exposure_us_to_ms(exposure_us: int) -> int:
    return max(1, int(round(float(exposure_us) / 1000.0)))


def _format_readout_time_ms(runtime_timing: dict[str, Any] | None) -> str:
    if not runtime_timing:
        return "-"
    value_s = runtime_timing.get("timing_readout_time_s")
    if value_s in (None, ""):
        return "-"
    return f"{float(value_s) * 1000.0:.3f} ms"


def _actual_inter_frame_gap_us(sim_config: AppConfig, runtime_timing: dict[str, Any] | None) -> int:
    if runtime_timing and runtime_timing.get("recommended_inter_frame_gap_us") is not None:
        return int(runtime_timing["recommended_inter_frame_gap_us"])
    return int(sim_config.timing.inter_frame_gap_us)


def build_sim_settings_summary(sim_config: AppConfig, runtime_timing: dict[str, Any] | None = None) -> str:
    daq = sim_config.daq
    camera = sim_config.camera
    selected_device = camera.device_label or f"#{camera.device_index}"
    selected_ro = sim_config.selected_running_order or "(SLM 未连接)"
    summary_lines = [
        f"Laser: {sim_config.selected_laser_nm} nm",
        f"Pattern RO: {selected_ro}",
        f"Selected SIM Camera: {selected_device}",
        f"Exposure: {_sim_exposure_us_to_ms(camera.exposure_us)} ms",
        f"Bit Depth: {int(camera.bit_depth)}-bit",
        f"ROI: x={camera.roi_x}, y={camera.roi_y}, w={camera.roi_width}, h={camera.roi_height}",
        f"TIMING_READOUTTIME: {_format_readout_time_ms(runtime_timing)}",
        f"Actual Inter Frame Gap: {_actual_inter_frame_gap_us(sim_config, runtime_timing)} us",
        "",
        "DAQ:",
        f"  device_name: {daq.device_name}",
        f"  slm_enable_line: {daq.slm_enable_line}",
        f"  slm_trigger_line: {daq.slm_trigger_line}",
        f"  slm_finish_line: {daq.slm_finish_line}",
        f"  cam_trigger_line: {daq.camera_trigger_line}",
        f"  laser_405_line: {daq.laser_405_line}",
        f"  laser_488_line: {daq.laser_488_line}",
        f"  laser_561_line: {daq.laser_561_line}",
        f"  laser_647_line: {daq.laser_647_line}",
    ]
    return "\n".join(summary_lines)

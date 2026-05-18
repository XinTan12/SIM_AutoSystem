"""主界面 SIM 设置摘要文本生成器。

这个模块把 AppConfig 和运行时相机 timing 信息格式化成用户可读的多行摘要，供 control_wangbo 主界面展示当前相机、激光、DAQ 线位、Running Order 和 Timing 状态。
"""

from __future__ import annotations

from typing import Any

from .models import AppConfig, effective_inter_frame_gap_us


def _sim_exposure_us_to_ms(exposure_us: int) -> int:
    """把配置中的曝光微秒值转换成摘要里更易读的毫秒文本。"""
    return max(1, int(round(float(exposure_us) / 1000.0)))


def _format_readout_time_ms(runtime_timing: dict[str, Any] | None) -> str:
    """格式化相机读出时间，缺失时保持未知语义。"""
    if not runtime_timing:
        return "-"
    value_s = runtime_timing.get("timing_readout_time_s")
    if value_s in (None, ""):
        return "-"
    return f"{float(value_s) * 1000.0:.3f} ms"


def _summary_inter_frame_gap_us(runtime_timing: dict[str, Any] | None) -> int:
    """生成帧间隔摘要，区分默认值和相机建议值。"""
    recommended_gap_us = None
    if runtime_timing:
        recommended_gap_us = runtime_timing.get("recommended_inter_frame_gap_us")
    return effective_inter_frame_gap_us(recommended_gap_us)


def build_sim_settings_summary(sim_config: AppConfig, runtime_timing: dict[str, Any] | None = None) -> str:
    """把当前 SIM 配置和运行时相机 timing 信息格式化为主界面摘要。"""
    daq = sim_config.daq
    camera = sim_config.camera
    timing = sim_config.timing
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
        "",
        "Timing:",
        f"  sample_rate_hz: {timing.sample_rate_hz}",
        f"  edge_pulse_us: {timing.edge_pulse_us}",
        f"  inter_frame_gap_us: {_summary_inter_frame_gap_us(runtime_timing)}",
        f"  slm_enable_guard_us: {timing.slm_enable_guard_us}",
    ]
    return "\n".join(summary_lines)

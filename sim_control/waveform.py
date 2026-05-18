"""NI USB-6423 SIM9 同步 TTL 波形生成。

本文件把配置中的 port0/lineN 映射解析成整数线号，校验角色不冲突，并按相机曝光、SLM enable/trigger/finish、激光线和帧间间隔生成 packed uint32 端口波形。NIDaqAdapter 直接播放这里生成的 WaveformPlan。
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Any

import numpy as np

from .models import DAQ_ROLE_ORDER, DaqLineConfig, LASER_ROLE_MAP, TimingConfig


LINE_PATTERN = re.compile(r"^(?P<device>[^/]+)/port(?P<port>\d+)/line(?P<line>\d+)$")


@dataclass
class WaveformPlan:
    """DAQ 波形构建后的结果对象，包含 packed port 数据、采样率、时长和诊断信息。"""
    line_order: list[str]
    packed_port_values: np.ndarray
    role_matrix: dict[str, np.ndarray]
    sample_rate_hz: int
    sample_count: int
    duration_s: float
    metadata: dict[str, Any]
    warnings: list[str]


def parse_line_name(line_name: str) -> tuple[str, int, int]:
    """把用户或配置中的文本形式解析成后续流程可直接使用的结构化值。"""
    match = LINE_PATTERN.match(line_name)
    if not match:
        raise ValueError(f"Invalid NI line name: {line_name}")
    return match.group("device"), int(match.group("port")), int(match.group("line"))


def validate_daq_line_config(config: DaqLineConfig) -> None:
    """集中校验输入条件，把错误尽早转成可报告的问题。"""
    devices = set()
    ports = set()
    seen_lines = set()
    for role in DAQ_ROLE_ORDER:
        line_name = getattr(config, role)
        device, port, line_index = parse_line_name(line_name)
        devices.add(device)
        ports.add(port)
        if line_name in seen_lines:
            raise ValueError(f"DAQ line reused: {line_name}")
        seen_lines.add(line_name)
        if not 0 <= line_index <= 15:
            raise ValueError(f"Line index must be between 0 and 15: {line_name}")
    if len(devices) != 1:
        raise ValueError("All DAQ lines must belong to the same device.")
    if ports != {0}:
        raise ValueError("All DAQ lines must belong to port0.")
    if config.device_name not in devices:
        raise ValueError("DAQ device_name does not match selected lines.")


# 波形 builder 只负责生成数据计划，不直接操作 NI 任务；真实播放在 NIDaqAdapter。
class NIDaqWaveformBuilder:
    """根据 SIM9 时序和线位配置生成 USB-6423 可播放的数字端口波形。"""
    def build(
        self,
        daq_config: DaqLineConfig,
        timing: TimingConfig,
        laser_wavelength_nm: int,
        exposure_us: int,
        frame_count: int = 9,
        include_role_matrix: bool = True,
    ) -> WaveformPlan:
        """生成一轮 SIM9 采集的 packed port 波形和诊断元数据。"""
        validate_daq_line_config(daq_config)
        if laser_wavelength_nm not in LASER_ROLE_MAP:
            raise ValueError(f"Unsupported laser wavelength: {laser_wavelength_nm}")
        if frame_count <= 0:
            raise ValueError("frame_count must be positive.")
        if exposure_us <= 0:
            raise ValueError("exposure_us must be positive.")
        if timing.sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be positive.")

        edge_pulse_samples = self._us_to_samples(timing.edge_pulse_us, timing.sample_rate_hz)
        gap_samples = self._us_to_samples(timing.inter_frame_gap_us, timing.sample_rate_hz)
        exposure_samples = self._us_to_samples(exposure_us, timing.sample_rate_hz)
        guard_samples = self._us_to_samples(timing.slm_enable_guard_us, timing.sample_rate_hz)
        warnings: list[str] = []

        if exposure_samples <= 0:
            raise ValueError("Exposure converts to zero samples. Increase exposure or sample rate.")
        if exposure_samples < 10:
            warnings.append(
                f"exposure_us={exposure_us} converts to only {exposure_samples} samples at {timing.sample_rate_hz} Hz."
            )
        if timing.inter_frame_gap_us < 1000:
            warnings.append(
                f"inter_frame_gap_us={timing.inter_frame_gap_us} is below the conservative 1000 us readout margin."
            )

        per_frame_span = exposure_samples + gap_samples
        sample_count = (guard_samples * 2) + (frame_count * per_frame_span)
        matrix = (
            {role: np.zeros(sample_count, dtype=np.uint8) for role in DAQ_ROLE_ORDER}
            if include_role_matrix
            else {}
        )
        packed = np.zeros(sample_count, dtype=np.uint32)
        line_bits = {
            role: np.uint32(1 << parse_line_name(getattr(daq_config, role))[2])
            for role in DAQ_ROLE_ORDER
        }

        active_laser_role = LASER_ROLE_MAP[laser_wavelength_nm]
        if include_role_matrix:
            matrix["slm_enable_line"][:] = 1
        else:
            packed[:] |= line_bits["slm_enable_line"]

        frame_starts = []
        frame_ends = []

        for frame_index in range(frame_count):
            frame_start = guard_samples + (frame_index * per_frame_span)
            frame_end = frame_start + exposure_samples
            frame_starts.append(frame_start)
            frame_ends.append(frame_end)

            if include_role_matrix:
                matrix["camera_trigger_line"][frame_start:frame_end] = 1
                matrix[active_laser_role][frame_start:frame_end] = 1
            else:
                packed[frame_start:frame_end] |= (
                    line_bits["camera_trigger_line"] | line_bits[active_laser_role]
                )

            trigger_end = min(frame_start + edge_pulse_samples, sample_count)
            finish_end = min(frame_end + edge_pulse_samples, sample_count)
            if frame_index < frame_count - 1 and edge_pulse_samples > gap_samples:
                warnings.append(
                    "slm_finish pulse extends beyond the inter-frame gap before the next frame."
                )
            if frame_end + edge_pulse_samples > sample_count:
                warnings.append("slm_finish pulse is clipped at the end of the waveform.")
            if include_role_matrix:
                matrix["slm_trigger_line"][frame_start:trigger_end] = 1
                matrix["slm_finish_line"][frame_end:finish_end] = 1
            else:
                packed[frame_start:trigger_end] |= line_bits["slm_trigger_line"]
                packed[frame_end:finish_end] |= line_bits["slm_finish_line"]

        if include_role_matrix:
            packed = self._pack_port_values(daq_config, matrix, sample_count)
        duration_s = sample_count / float(timing.sample_rate_hz)
        metadata = {
            "frame_count": frame_count,
            "exposure_us": exposure_us,
            "sample_rate_hz": timing.sample_rate_hz,
            "edge_pulse_samples": edge_pulse_samples,
            "inter_frame_gap_samples": gap_samples,
            "slm_enable_guard_samples": guard_samples,
            "frame_start_samples": frame_starts,
            "frame_end_samples": frame_ends,
            "active_laser_role": active_laser_role,
        }
        return WaveformPlan(
            line_order=list(DAQ_ROLE_ORDER),
            packed_port_values=packed,
            role_matrix=matrix,
            sample_rate_hz=timing.sample_rate_hz,
            sample_count=sample_count,
            duration_s=duration_s,
            metadata=metadata,
            warnings=sorted(set(warnings)),
        )

    @staticmethod
    def _us_to_samples(microseconds: int, sample_rate_hz: int) -> int:
        """把微秒时长换算成 DAQ 采样点数，并保证非零脉冲至少占一个点。"""
        return max(1, int(math.ceil((microseconds / 1_000_000.0) * sample_rate_hz)))

    @staticmethod
    def _pack_port_values(
        daq_config: DaqLineConfig,
        matrix: dict[str, np.ndarray],
        sample_count: int,
    ) -> np.ndarray:
        """把每个时间片的角色高低电平打包成 USB-6423 port0 的 uint32 值。"""
        packed = np.zeros(sample_count, dtype=np.uint32)
        for role in DAQ_ROLE_ORDER:
            _, _, line_index = parse_line_name(getattr(daq_config, role))
            packed[matrix[role] != 0] |= np.uint32(1 << line_index)
        return packed

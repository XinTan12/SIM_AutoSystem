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
    line_order: list[str]
    packed_port_values: np.ndarray
    role_matrix: dict[str, np.ndarray]
    sample_rate_hz: int
    sample_count: int
    duration_s: float
    metadata: dict[str, Any]
    warnings: list[str]


def parse_line_name(line_name: str) -> tuple[str, int, int]:
    match = LINE_PATTERN.match(line_name)
    if not match:
        raise ValueError(f"Invalid NI line name: {line_name}")
    return match.group("device"), int(match.group("port")), int(match.group("line"))


def validate_daq_line_config(config: DaqLineConfig) -> None:
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


class NIDaqWaveformBuilder:
    def build(
        self,
        daq_config: DaqLineConfig,
        timing: TimingConfig,
        laser_wavelength_nm: int,
        exposure_us: int,
        frame_count: int = 9,
    ) -> WaveformPlan:
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
        matrix = {
            role: np.zeros(sample_count, dtype=np.uint8)
            for role in DAQ_ROLE_ORDER
        }

        active_laser_role = LASER_ROLE_MAP[laser_wavelength_nm]
        matrix["slm_enable_line"][:] = 1

        frame_starts = []
        frame_ends = []

        for frame_index in range(frame_count):
            frame_start = guard_samples + (frame_index * per_frame_span)
            frame_end = frame_start + exposure_samples
            frame_starts.append(frame_start)
            frame_ends.append(frame_end)

            matrix["camera_trigger_line"][frame_start:frame_end] = 1
            matrix[active_laser_role][frame_start:frame_end] = 1

            trigger_end = min(frame_start + edge_pulse_samples, sample_count)
            finish_end = min(frame_end + edge_pulse_samples, sample_count)
            if frame_index < frame_count - 1 and edge_pulse_samples > gap_samples:
                warnings.append(
                    "slm_finish pulse extends beyond the inter-frame gap before the next frame."
                )
            if frame_end + edge_pulse_samples > sample_count:
                warnings.append("slm_finish pulse is clipped at the end of the waveform.")
            matrix["slm_trigger_line"][frame_start:trigger_end] = 1
            matrix["slm_finish_line"][frame_end:finish_end] = 1

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
        return max(1, int(math.ceil((microseconds / 1_000_000.0) * sample_rate_hz)))

    @staticmethod
    def _pack_port_values(
        daq_config: DaqLineConfig,
        matrix: dict[str, np.ndarray],
        sample_count: int,
    ) -> np.ndarray:
        packed = np.zeros(sample_count, dtype=np.uint32)
        for role in DAQ_ROLE_ORDER:
            _, _, line_index = parse_line_name(getattr(daq_config, role))
            packed |= (matrix[role].astype(np.uint32) << np.uint32(line_index))
        return packed

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import time
import uuid


DAQ_ROLE_ORDER = [
    "slm_enable_line",
    "slm_trigger_line",
    "slm_finish_line",
    "camera_trigger_line",
    "laser_405_line",
    "laser_488_line",
    "laser_561_line",
    "laser_647_line",
]

DEFAULT_DAQ_LINE_INDICES = {
    "slm_enable_line": 0,
    "slm_trigger_line": 1,
    "slm_finish_line": 2,
    "camera_trigger_line": 5,
    "laser_405_line": 8,
    "laser_488_line": 6,
    "laser_561_line": 7,
    "laser_647_line": 9,
}


def default_daq_line_name(device_name: str, role: str) -> str:
    return f"{device_name}/port0/line{DEFAULT_DAQ_LINE_INDICES[role]}"


def default_daq_line_map(device_name: str) -> dict[str, str]:
    return {role: default_daq_line_name(device_name, role) for role in DAQ_ROLE_ORDER}


LASER_ROLE_MAP = {
    405: "laser_405_line",
    488: "laser_488_line",
    561: "laser_561_line",
    647: "laser_647_line",
}

SUPPORTED_LASERS = tuple(LASER_ROLE_MAP.keys())


def _default_pattern_files() -> list[str]:
    return [""] * 9


def new_task_id(prefix: str = "sim") -> str:
    ts = time.strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{ts}_{uuid.uuid4().hex[:8]}"


@dataclass
class DaqLineConfig:
    device_name: str = "Dev1"
    slm_enable_line: str = default_daq_line_name("Dev1", "slm_enable_line")
    slm_trigger_line: str = default_daq_line_name("Dev1", "slm_trigger_line")
    slm_finish_line: str = default_daq_line_name("Dev1", "slm_finish_line")
    camera_trigger_line: str = default_daq_line_name("Dev1", "camera_trigger_line")
    laser_405_line: str = default_daq_line_name("Dev1", "laser_405_line")
    laser_488_line: str = default_daq_line_name("Dev1", "laser_488_line")
    laser_561_line: str = default_daq_line_name("Dev1", "laser_561_line")
    laser_647_line: str = default_daq_line_name("Dev1", "laser_647_line")

    def line_map(self) -> dict[str, str]:
        return {role: getattr(self, role) for role in DAQ_ROLE_ORDER}


@dataclass
class CameraConfig:
    device_index: int = 0
    device_label: str = ""
    roi_x: int = 0
    roi_y: int = 0
    roi_width: int = 512
    roi_height: int = 512
    exposure_us: int = 10_000
    bit_depth: int = 16
    timeout_ms: int = 5_000
    trigger_mode: str = "external_level"


@dataclass
class TimingConfig:
    sample_rate_hz: int = 1_000_000
    edge_pulse_us: int = 50
    inter_frame_gap_us: int = 10_000
    slm_enable_guard_us: int = 50


@dataclass
class BackendConfig:
    fusion_bt_sdk_path: str = ""
    slm_sdk_path: str = ""
    simulation_mode: bool = False


@dataclass
class SimTaskConfig:
    laser_wavelength_nm: int = 488
    pattern_files: list[str] = field(default_factory=_default_pattern_files)
    running_order_name: str = ""
    camera: CameraConfig = field(default_factory=CameraConfig)
    timing: TimingConfig = field(default_factory=TimingConfig)


@dataclass
class PatternPreparationResult:
    pattern_files: list[str] = field(default_factory=_default_pattern_files)
    handles: list[int] = field(default_factory=list)
    prepared_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AcquisitionBatch:
    task_id: str
    stack: Any
    timestamps: list[float]
    laser_wavelength_nm: int
    exposure_us: int
    pattern_files: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReconstructionResult:
    task_id: str
    preview_image: Any
    metadata: dict[str, Any] = field(default_factory=dict)
    succeeded: bool = True


@dataclass
class FeatureResult:
    task_id: str
    features: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    succeeded: bool = True


@dataclass
class DecisionResult:
    task_id: str
    decision: str
    reason: str
    score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AppConfig:
    daq: DaqLineConfig = field(default_factory=DaqLineConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)
    timing: TimingConfig = field(default_factory=TimingConfig)
    backend: BackendConfig = field(default_factory=BackendConfig)
    pattern_files: list[str] = field(default_factory=_default_pattern_files)
    selected_running_order: str = ""
    selected_laser_nm: int = 488
    config_version: int = 3
    config_path: str = ""

    def resolved_config_path(self) -> Path | None:
        if not self.config_path:
            return None
        return Path(self.config_path)

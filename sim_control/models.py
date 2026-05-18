"""SIM 控制链路共享的数据模型。

所有配置、采集任务、采集结果、重建结果、特征结果和决策结果都在这里用 dataclass 定义。其它模块通过这些数据类传递结构化信息，避免用松散 dict 在 GUI、controller、adapter 和 pipeline 之间漂移。
"""

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
    """按照项目固定的 USB-6423 端口约定生成单条 TTL 线名。"""
    return f"{device_name}/port0/line{DEFAULT_DAQ_LINE_INDICES[role]}"


def default_daq_line_map(device_name: str) -> dict[str, str]:
    """生成默认 DAQ 角色到物理 TTL 线的完整映射。"""
    return {role: default_daq_line_name(device_name, role) for role in DAQ_ROLE_ORDER}


LASER_ROLE_MAP = {
    405: "laser_405_line",
    488: "laser_488_line",
    561: "laser_561_line",
    647: "laser_647_line",
}

SUPPORTED_LASERS = tuple(LASER_ROLE_MAP.keys())
# 50ms 是当前正式 SIM9 默认帧间隔；只有更短的相机推荐值才允许覆盖。
DEFAULT_INTER_FRAME_GAP_US = 50_000


def _default_pattern_files() -> list[str]:
    """为 SIM9 采集准备 9 个图案槽位，Running Order 模式会用元数据覆盖。"""
    return [""] * 9


def new_task_id(prefix: str = "sim") -> str:
    """用时间戳和短 UUID 生成采集任务目录/记录可读且低冲突的标识。"""
    ts = time.strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{ts}_{uuid.uuid4().hex[:8]}"


def effective_inter_frame_gap_us(recommended_gap_us: int | float | None = None) -> int:
    """在手动间隔和相机建议读出时间之间取可用帧间隔，默认保持 50 ms。"""
    if recommended_gap_us is None:
        return DEFAULT_INTER_FRAME_GAP_US
    gap_us = int(recommended_gap_us)
    if 0 <= gap_us < DEFAULT_INTER_FRAME_GAP_US:
        return gap_us
    return DEFAULT_INTER_FRAME_GAP_US


# 以下数据类是跨 GUI、controller、adapter、pipeline 传递状态的稳定结构。
@dataclass
class DaqLineConfig:
    """保存 USB-6423 设备名和各 SIM TTL 角色对应的物理线位。"""
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
        """把 dataclass 字段重新组织成采集核心需要的角色到线名字典。"""
        return {role: getattr(self, role) for role in DAQ_ROLE_ORDER}


@dataclass
class CameraConfig:
    """保存相机选择、ROI、曝光、位深、超时和触发模式等采集参数。"""
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
    """保存 DAQ 波形采样率、脉冲宽度、帧间隔和 SLM 保护时间。"""
    sample_rate_hz: int = 1_000_000
    edge_pulse_us: int = 50
    inter_frame_gap_us: int = DEFAULT_INTER_FRAME_GAP_US
    slm_enable_guard_us: int = 50


@dataclass
class BackendConfig:
    """保存真实 SDK 路径和仿真模式开关，决定 controller 创建哪类 adapter。"""
    fusion_bt_sdk_path: str = ""
    slm_sdk_path: str = ""
    simulation_mode: bool = False


@dataclass
class SimTaskConfig:
    """描述一次 SIM9 采集任务，包括激光、图案/RO、相机参数和时序参数。"""
    laser_wavelength_nm: int = 488
    pattern_files: list[str] = field(default_factory=_default_pattern_files)
    running_order_name: str = ""
    camera: CameraConfig = field(default_factory=CameraConfig)
    timing: TimingConfig = field(default_factory=TimingConfig)


@dataclass
class PatternPreparationResult:
    """记录 SLM 图案文件、句柄和 Running Order 元数据，供采集核心激活。"""
    pattern_files: list[str] = field(default_factory=_default_pattern_files)
    handles: list[int] = field(default_factory=list)
    prepared_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AcquisitionBatch:
    """承载一次采集完成后的图像 stack、时间戳和采集元数据。"""
    task_id: str
    stack: Any
    timestamps: list[float]
    laser_wavelength_nm: int
    exposure_us: int
    pattern_files: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReconstructionResult:
    """承载占位重建阶段输出的预览图、元数据和成功状态。"""
    task_id: str
    preview_image: Any
    metadata: dict[str, Any] = field(default_factory=dict)
    succeeded: bool = True


@dataclass
class FeatureResult:
    """承载特征提取阶段输出的特征字典、元数据和成功状态。"""
    task_id: str
    features: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    succeeded: bool = True


@dataclass
class DecisionResult:
    """承载 release/sort 决策、原因、评分和后续回传所需元数据。"""
    task_id: str
    decision: str
    reason: str
    score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AppConfig:
    """聚合 SIM GUI 的完整配置，是 JSON 配置读写和界面同步的根对象。"""
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
        """把可选字符串配置路径规范化为 Path，便于保存和重载时复用。"""
        if not self.config_path:
            return None
        return Path(self.config_path)

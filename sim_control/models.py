"""SIM 控制链路共享的数据模型与常量。

作用：
    本文件用 ``dataclass`` 集中定义**整个 SIM 控制链路**会跨模块流转的所有
    结构化数据：硬件配置（``DaqLineConfig``/``CameraConfig``/``TimingConfig``/
    ``BackendConfig``）、采集任务（``SimTaskConfig``）、采集中间产物
    （``PatternPreparationResult``/``AcquisitionBatch``）以及后续重建/特征/
    决策的结果对象。所有其它模块通过这些数据类传递信息，从而避免在 GUI、
    controller、adapter、pipeline 之间用松散 dict 漂移。
    同时这里也定义了 USB-6423 DAQ 线位的项目固定约定（DAQ_ROLE_ORDER/
    DEFAULT_DAQ_LINE_INDICES）、波长 → DAQ 角色映射（LASER_ROLE_MAP）和
    默认帧间隔（DEFAULT_INTER_FRAME_GAP_US）。

协作关系：
    上游：``config_store.py``（JSON 互转）、``sim_control/gui.py``（GUI 绑定）、
          ``controller.py``（采集流程）。
    下游：被 ``adapters.py``、``sim_adapters.py``、``waveform.py``、
          ``acquisition_core.py``、``summary.py`` 等模块读取使用。
    相关：所有 ``tests/test_*`` 用例都从这里 import 数据类来构造夹具。

关键概念：
    - ``DAQ_ROLE_ORDER``：DAQ 8 路角色的固定排列，决定波形构建器中 role →
      line 索引的对应顺序，**绝对不可改顺序**，否则会和真实接线错位。
    - ``DEFAULT_DAQ_LINE_INDICES``：项目唯一固定的 USB-6423 端口/线位映射，
      与硬件实际接线一致（slm_enable=0/trigger=1/finish=2/cam=8/
      405=9/488=10/561=11/red=12）。第四路红光线 ``laser_red_line``（line 12）
      承载机器红光（638 或 647），物理接线与波长数字无关。
    - ``DEFAULT_INTER_FRAME_GAP_US = 50_000``：SIM9 正式采集帧间隔默认 50 ms，
      只有相机推荐值（recommended_inter_frame_gap_us）小于该值时才能覆盖。
    - ``SUPPORTED_LASERS = (405, 488, 561, 638)``：默认机器四档波长常量；按机器红光
      （``AppConfig.red_laser_nm`` 638/647）取四档用 ``supported_lasers_for``。
    - 9 帧 pattern：``pattern_files`` 列表始终 9 槽位长度，Running Order
      模式下槽位值由元数据覆盖（实际由 SLM 端 RO 控制）。

维护要点：
    - dataclass 字段顺序与默认值是 JSON schema 的事实定义，新增字段必须同步
      更新 ``config_store.py`` 的迁移逻辑，避免老配置无法加载。
    - 不要在数据类里写硬件 IO、SDK 调用或耗时计算；它们必须保持纯数据形态。
    - ``DAQ_ROLE_ORDER`` 与 ``DEFAULT_DAQ_LINE_INDICES`` 的修改会影响波形
      builder、GUI 摘要和真机接线之间的协议，必须经过硬件验证才能改。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import time
import uuid


# DAQ 角色名顺序：波形构建、配置文件 schema、UI 摘要都按此顺序枚举 8 路 TTL。
# 顺序固定为「SLM 三线 + 相机触发 + 四档激光」，与项目接线约定对齐。
DAQ_ROLE_ORDER = [
    "slm_enable_line",
    "slm_trigger_line",
    "slm_finish_line",
    "camera_trigger_line",
    "laser_405_line",
    "laser_488_line",
    "laser_561_line",
    "laser_red_line",
]

# 项目唯一规范化的 USB-6423 port0 线位号。任何修改都必须经过硬件验收。
# 来源：AGENTS.md「关键约束」与 PROJECT_MEMORY.md「当前硬件与集成状态」。
DEFAULT_DAQ_LINE_INDICES = {
    "slm_enable_line": 0,
    "slm_trigger_line": 1,
    "slm_finish_line": 2,
    "camera_trigger_line": 8,
    "laser_405_line": 9,
    "laser_488_line": 10,
    "laser_561_line": 11,
    "laser_red_line": 12,
}


def default_daq_line_name(device_name: str, role: str) -> str:
    """按项目固定的 USB-6423 端口约定生成一条 TTL 线名。

    返回：
        形如 ``"Dev1/port0/line5"``，由 ``device_name`` 与
        ``DEFAULT_DAQ_LINE_INDICES[role]`` 拼接而成。

    参数：
        device_name: NI MAX 中的设备名（默认 ``"Dev1"``）。
        role: ``DAQ_ROLE_ORDER`` 中的某个角色键；不在表中会触发 ``KeyError``。
    """
    # 拼接结果用作 nidaqmx 的 channel 名，必须符合 ``<device>/port<N>/line<N>``。
    return f"{device_name}/port0/line{DEFAULT_DAQ_LINE_INDICES[role]}"


def default_daq_line_map(device_name: str) -> dict[str, str]:
    """生成默认 DAQ 角色→物理 TTL 线的完整 8 项映射。

    用途：
        ``DaqLineConfig`` 字段默认值、新建配置和 GUI 复位都用这个函数生成基线，
        避免在多处复制硬编码。
    """
    # 字典推导一次性生成 8 个键值对，保持与 ``DAQ_ROLE_ORDER`` 同步。
    return {role: default_daq_line_name(device_name, role) for role in DAQ_ROLE_ORDER}


# 激光波长（nm）→ DAQ 角色键的映射。controller 选定波长后用它定位要拉高的 TTL。
# 第四路红光（638 或 647）是"每台机器固定、机器间不同"的波长身份，但物理上共用同一条
# 红光 DAQ 线 ``laser_red_line``（line 12）；故 638 与 647 都映射到 ``laser_red_line``。
LASER_ROLE_MAP = {
    405: "laser_405_line",
    488: "laser_488_line",
    561: "laser_561_line",
    638: "laser_red_line",
    647: "laser_red_line",
}

# 前三档固定波长；第四档红光由各机器的 ``AppConfig.red_laser_nm`` 决定（638/647）。
BASE_LASERS = (405, 488, 561)
# 第四路红光的两种合法波长身份（两台机器各一）。
RED_LASER_CHOICES = (638, 647)
# 默认机器红光波长（与历史"统一 638"兼容）。
DEFAULT_RED_LASER_NM = 638
# 638/647 是同一条物理红光路（line 12）的两种激光器身份，RO 命名可能用任一数字，
# 互为 fallback；用于 RO 选择与 immediate 波长匹配的红光等价判定。
RED_EQUIVALENT_WAVELENGTHS = frozenset({638, 647})
# 项目默认支持的四档波长（默认机器/legacy 语义）。必须是显式字面量，**不能**由
# ``LASER_ROLE_MAP.keys()`` 派生——map 现含 638 与 647 两个红光键会让它变 5 元组、
# 破坏全项目"四档"假设。按机器红光取波长请用 ``supported_lasers_for``。
SUPPORTED_LASERS = (405, 488, 561, 638)


def supported_lasers_for(red_laser_nm: int) -> tuple[int, int, int, int]:
    """返回某台机器实际支持的四档波长 ``(405, 488, 561, red_laser_nm)``。

    第四档由机器红光波长决定；非法 ``red_laser_nm``（不在 ``RED_LASER_CHOICES``）回落
    到 ``DEFAULT_RED_LASER_NM``。UI 波长下拉、validate 等"按机器"的点都应调它，不要
    直接用模块级 ``SUPPORTED_LASERS``（那是默认 638 机器的常量）。
    """
    try:
        red = int(red_laser_nm)
    except (TypeError, ValueError):
        red = DEFAULT_RED_LASER_NM
    if red not in RED_LASER_CHOICES:
        red = DEFAULT_RED_LASER_NM
    return (*BASE_LASERS, red)
# 50 ms 是正式 SIM9 默认帧间隔。仅当相机针对当前 ROI/接口推荐的
# ``recommended_inter_frame_gap_us`` < 50_000 时才允许覆盖此默认。
DEFAULT_INTER_FRAME_GAP_US = 50_000
Z_SCAN_EXPOSURE_PRESETS_US = {
    5: 4_884,
    8: 7_884,
    14: 13_884,
    20: 19_884,
}
Z_SCAN_EXPOSURE_PRESETS_MS = tuple(Z_SCAN_EXPOSURE_PRESETS_US.keys())
Z_SCAN_DIRECTIONS = ("positive_z", "negative_z")
Z_SCAN_FOCUS_METRICS = ("sml",)


def _default_pattern_files() -> list[str]:
    """为 SIM9 采集准备 9 个图案文件槽位。

    用途：
        SIM 任务必须有 9 个图案路径槽位（与 Running Order 9 帧对齐）；
        Running Order 模式下槽位值由 ``PatternPreparationResult.metadata``
        覆盖，实际并不读取这些字符串，但 9 的长度仍是协议级约束。

    返回：
        长度为 9 的空字符串列表。
    """
    # 9 个空字符串槽位：上层负责按需填入文件路径或留空（Running Order 模式）。
    return [""] * 9


def new_task_id(prefix: str = "sim") -> str:
    """生成「时间戳 + 短 UUID」组合的采集任务 ID。

    用途：
        采集结果目录、日志、TIFF 文件名都用同一个 task_id 串起来，便于事后排查。
        时间戳保证可读、UUID 前缀保证同一秒内多次启动也不冲突。

    参数：
        prefix: 业务前缀（默认 ``"sim"``），便于在其它生成器中复用此函数。
    """
    # 1) 取本地时间精确到秒，避免微秒造成不必要的文件名长度。
    ts = time.strftime("%Y%m%d_%H%M%S")
    # 2) 拼接 UUID4 的前 8 个 hex 字符作为去重后缀，足够区分同秒多次启动。
    return f"{prefix}_{ts}_{uuid.uuid4().hex[:8]}"


def effective_inter_frame_gap_us(recommended_gap_us: int | float | None = None) -> int:
    """在默认 50 ms 与相机推荐间隔之间选出实际生效的帧间隔。

    规则：
        - 推荐值 None / 缺失：使用 ``DEFAULT_INTER_FRAME_GAP_US`` = 50 ms。
        - 推荐值 ≥ 50 ms：仍使用 50 ms（项目策略不允许放慢默认时序）。
        - 推荐值 < 50 ms 且 ≥ 0：使用推荐值（更快读出时缩短帧间）。

    返回：
        实际写入波形 builder 的 ``inter_frame_gap_us``（整数微秒）。
    """
    # 1) 没有相机推荐：直接回落默认值。
    if recommended_gap_us is None:
        return DEFAULT_INTER_FRAME_GAP_US
    # 2) 强转 int（兼容 numpy 浮点等类型），保持后续比较确定。
    gap_us = int(recommended_gap_us)
    # 3) 推荐值在 [0, 50_000) 之间才有"更快"的意义，超过默认则继续使用默认。
    if 0 <= gap_us < DEFAULT_INTER_FRAME_GAP_US:
        return gap_us
    return DEFAULT_INTER_FRAME_GAP_US


# ============================================================================
# 跨模块流转的数据类（以下全部是纯数据载体，不应包含硬件 IO / 阻塞调用）
# ============================================================================
@dataclass
class DaqLineConfig:
    """保存 USB-6423 设备名和各 SIM TTL 角色对应的物理线位。

    职责：
        - 8 个角色字段（``slm_enable_line`` ... ``laser_red_line``）记录线名。
        - 提供 ``line_map()`` 把字段重新组织为角色→线名字典，供波形 builder 与
          adapter 直接使用。

    协作：
        - 由 ``AppConfig`` 持有，作为 SIM 配置 JSON 的一部分被读写。
        - 被 ``waveform.NIDaqWaveformBuilder`` 校验后转成 packed port 波形。
    """
    device_name: str = "Dev1"
    slm_enable_line: str = default_daq_line_name("Dev1", "slm_enable_line")
    slm_trigger_line: str = default_daq_line_name("Dev1", "slm_trigger_line")
    slm_finish_line: str = default_daq_line_name("Dev1", "slm_finish_line")
    camera_trigger_line: str = default_daq_line_name("Dev1", "camera_trigger_line")
    laser_405_line: str = default_daq_line_name("Dev1", "laser_405_line")
    laser_488_line: str = default_daq_line_name("Dev1", "laser_488_line")
    laser_561_line: str = default_daq_line_name("Dev1", "laser_561_line")
    laser_red_line: str = default_daq_line_name("Dev1", "laser_red_line")

    def line_map(self) -> dict[str, str]:
        """把 dataclass 字段重新组织成采集核心需要的角色→线名字典。

        返回：
            8 项字典，键来自 ``DAQ_ROLE_ORDER``、值是配置里实际填写的线名。
        """
        # 使用 ``getattr`` 按 DAQ_ROLE_ORDER 顺序读字段，避免硬编码键名。
        return {role: getattr(self, role) for role in DAQ_ROLE_ORDER}


@dataclass
class CameraConfig:
    """保存相机选择、ROI、曝光、位深、超时和触发模式等采集参数。

    职责：
        是 GUI / Worker / Adapter 之间「相机怎么用」的唯一配置载体；
        被 ``apply_config()`` 翻译成 DCAM 属性写入硬件。

    维护要点：
        - ``trigger_mode`` 默认 ``external_level``：与 NI USB-6423 输出的电平触发匹配。
        - ROI 必须经过 ``sim_camera_presets.normalize_sim_camera_roi`` 校准后再下发。
    """
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


# R11 数据手册 PD0011CA Table 7-1 (p.26)：EXT_RUN 拉高后系统进入 Active Mode
# 的硬件激活时间 tHWAT 最大为 500 µs；guard 不超过该值时首个 SLM trigger
# 可能在 [HWA h] Running Order 激活完成前被丢弃（表现为第一帧黑帧 + 图案错位）。
R11_HARDWARE_ACTIVATION_MAX_US = 500
# slm_enable guard 推荐最小值：tHWAT 上限的 2 倍余量。
SLM_ENABLE_GUARD_RECOMMENDED_US = 1_000


@dataclass
class TimingConfig:
    """保存 DAQ 波形采样率、脉冲宽度、帧间隔和 SLM 保护时间。

    职责：
        提供波形 builder 必需的四个时序量；DAQ 页面已不再向用户暴露这些字段，
        但它们仍作为内部可调参数保留，便于实验时修改。

    维护要点：
        - ``inter_frame_gap_us`` 默认 50_000 µs，与 ``DEFAULT_INTER_FRAME_GAP_US`` 一致；
          运行时实际生效值由 ``effective_inter_frame_gap_us`` 决定。
        - ``edge_pulse_us`` 控制 SLM trigger / camera trigger / laser 的上升沿持续时间，
          一般不需要改。
        - ``slm_enable_guard_us`` 应大于 ``R11_HARDWARE_ACTIVATION_MAX_US``（500 µs），
          否则首个 trigger 可能落在 RO 硬件激活窗口内被丢弃（waveform builder 会
          告警但不硬拦截）；默认取 ``SLM_ENABLE_GUARD_RECOMMENDED_US``。
    """
    sample_rate_hz: int = 1_000_000
    edge_pulse_us: int = 50
    inter_frame_gap_us: int = DEFAULT_INTER_FRAME_GAP_US
    slm_enable_guard_us: int = SLM_ENABLE_GUARD_RECOMMENDED_US


@dataclass
class BackendConfig:
    """保存真实 SDK 路径和仿真模式开关，决定 controller 创建哪类 adapter。

    维护要点：
        - ``simulation_mode=True`` 时 controller 优先创建 ``sim_adapters.*Sim*``。
        - SDK 路径为空时 adapter 会用其内部默认搜索路径回落。
        - ``ti2_dll_path`` / ``ti2_sdk_module_path`` 透传给 ``Ti2ZStageAdapter``；
          空字符串表示沿用 adapter 内部默认推导路径（保证默认行为不变）。
    """
    fusion_bt_sdk_path: str = ""
    slm_sdk_path: str = ""
    ti2_dll_path: str = ""
    ti2_sdk_module_path: str = ""
    simulation_mode: bool = False


@dataclass
class SimTaskConfig:
    """描述一次 SIM9 采集任务的全部输入参数。

    职责：
        把"波长、9 帧 pattern / Running Order、相机参数、时序参数"打包成一次性
        交给 ``acquisition_core.run_single_acquisition()`` 的请求结构。

    维护要点：
        - ``pattern_files`` 始终 9 项，长度由 ``_default_pattern_files`` 锁定。
        - Running Order 模式下 ``running_order_name`` 非空且 ``pattern_files`` 内容不被读取。
    """
    laser_wavelength_nm: int = 488
    pattern_files: list[str] = field(default_factory=_default_pattern_files)
    running_order_name: str = ""
    camera: CameraConfig = field(default_factory=CameraConfig)
    timing: TimingConfig = field(default_factory=TimingConfig)


@dataclass
class PatternPreparationResult:
    """记录 SLM 图案准备结果（文件、句柄、模式元数据）。

    职责：
        ``SlmAdapter.program_patterns()`` 完成后返回此结构，``activate_prepared_patterns()``
        会据此把图案/RO 真正写到 SLM。

    维护要点：
        - Running Order 模式：``handles=[-1]``、``metadata["mode"]="running_order"``。
        - 普通 pattern 模式：``handles`` 为 SDK 返回的图案句柄列表。
    """
    pattern_files: list[str] = field(default_factory=_default_pattern_files)
    handles: list[int] = field(default_factory=list)
    prepared_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AcquisitionBatch:
    """承载一次 SIM9 采集完成后的图像 stack、时间戳和元数据。

    职责：
        ``acquisition_core.run_single_acquisition()`` 的返回结构；下游重建/特征
        pipeline 直接消费 ``stack``（``(9, H, W)`` ``numpy.uint16``）。

    维护要点：
        - ``stack`` 形状固定 ``(frame_count, H, W)``，dtype 必须 ``uint16``，
          否则后续 pipeline 接口约束失败。
        - ``timestamps`` 长度与 ``frame_count`` 一致，单位为秒。
    """
    task_id: str
    stack: Any
    timestamps: list[float]
    laser_wavelength_nm: int
    exposure_us: int
    pattern_files: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReconstructionResult:
    """承载占位重建阶段输出的预览图、元数据和成功状态。

    维护要点：
        - ``preview_image`` 当前是占位 numpy 2D 数组；实际重建接入后将替换为
          同学的算法产物（高分辨重建图）。
        - 出错时 ``succeeded=False`` 并在 ``metadata`` 中写入原因。
    """
    task_id: str
    preview_image: Any
    metadata: dict[str, Any] = field(default_factory=dict)
    succeeded: bool = True


@dataclass
class FeatureResult:
    """承载特征提取阶段输出的特征字典、元数据和成功状态。

    维护要点：
        ``features`` 字典键名应与下游决策器约定，例如
        ``{"intensity_mean": ..., "intensity_std": ..., "max_xy": ...}``。
    """
    task_id: str
    features: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    succeeded: bool = True


@dataclass
class DecisionResult:
    """承载 release/sort 决策、原因、评分和回传所需元数据。

    维护要点：
        - ``decision`` 字符串应当只取自约定集合（如 ``"release"``、``"sort"``、
          ``"discard"``）。
        - ``score`` 用于让上层做阈值或比较，0.0 表示尚未评分。
    """
    task_id: str
    decision: str
    reason: str
    score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ZScanConfig:
    """Configuration for the pre-SIM z-stack autofocus pass."""

    enabled: bool = True
    start_um: float | None = None
    direction: str = "positive_z"
    step_um: float = 0.3
    num_steps: int = 10
    exposure_preset_ms: int = 8
    focus_metric: str = "sml"

    @property
    def actual_exposure_us(self) -> int:
        return Z_SCAN_EXPOSURE_PRESETS_US[int(self.exposure_preset_ms)]


@dataclass
class ReconstructionConfig:
    """Configuration for the SIM9 reconstruction stage."""

    enabled: bool = True
    backend: str = "sim_wiener_gpu"
    device: str = "cuda"
    dtype: str = "single"
    otf_405_path: str = ""
    otf_488_path: str = ""
    otf_561_path: str = ""
    otf_638_path: str = ""
    otf_647_path: str = ""
    background_path: str = ""
    output_path: str = "data/reconstruction"
    wiener: float = 2.0
    pixel_size_nm: float = 65.0
    excitation_na: float = 1.49
    theta_ratio: tuple[int, int, int] = (1, 1, 1)
    recon_group_batch: int = 1
    # 参数策略：默认每帧重新估计（~287ms 热，开箱即用，遵守"仿真优先"——无 .mat
    # 也能跑通）。真实实验现场标定好每波长 .mat 后置 use_saved_params=True 走 ~45ms
    # 热路径（见 docs/reconstruction_saved_params.md 与 production 模板）。
    use_saved_params: bool = False
    saved_params_fallback: str = "fail"  # fail | estimate
    estimated_params_405_path: str = ""
    estimated_params_488_path: str = ""
    estimated_params_561_path: str = ""
    estimated_params_638_path: str = ""
    estimated_params_647_path: str = ""
    # 异步落盘：先发结果再后台有界单 writer 写盘，避免拖慢 重建→特征→决策 回传。
    save_reconstruction_output: bool = True
    async_save_reconstruction_output: bool = True
    save_queue_maxsize: int = 4

    def otf_path_for_wavelength(self, wavelength_nm: int) -> str:
        """Return the configured OTF path for a supported laser wavelength."""
        return str(getattr(self, f"otf_{int(wavelength_nm)}_path", ""))

    def estimated_params_path_for_wavelength(self, wavelength_nm: int) -> str:
        """Return the configured saved-parameter (.mat) path for a laser wavelength."""
        return str(getattr(self, f"estimated_params_{int(wavelength_nm)}_path", ""))

    def snapshot(self) -> "ReconstructionConfig":
        """Return a deep copy for safe cross-thread hand-off via Qt queued signals."""
        return copy.deepcopy(self)


@dataclass
class AppConfig:
    """聚合 SIM GUI 的完整配置，是 JSON 配置读写和界面同步的根对象。

    职责：
        - 持有四个子配置（``daq`` / ``camera`` / ``timing`` / ``backend``）。
        - 记录用户级选择（``selected_laser_nm`` / ``selected_running_order`` /
          ``pattern_files``）和元信息（``config_version`` / ``config_path``）。
        - 提供 ``resolved_config_path()`` 帮调用方拿到合法 ``Path``。

    维护要点：
        - ``config_version`` 升一档时务必在 ``config_store._MIGRATIONS`` 加迁移函数。
        - ``config_path`` 用作"上次保存的位置"提示，不参与 JSON schema 校验。
    """
    daq: DaqLineConfig = field(default_factory=DaqLineConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)
    timing: TimingConfig = field(default_factory=TimingConfig)
    backend: BackendConfig = field(default_factory=BackendConfig)
    z_scan: ZScanConfig = field(default_factory=ZScanConfig)
    reconstruction: ReconstructionConfig = field(default_factory=ReconstructionConfig)
    pattern_files: list[str] = field(default_factory=_default_pattern_files)
    selected_running_order: str = ""
    selected_laser_nm: int = 488
    # 机器档案：第四路红光的真实波长（638 或 647）。每台机器固定、机器间不同，决定
    # ``supported_lasers_for`` 的第四档、SLM RO 选择波长身份与重建 OTF 取用。默认 638
    # 兼容历史"统一 638"配置。
    red_laser_nm: int = DEFAULT_RED_LASER_NM
    # 默认值必须与 ``config_store.CURRENT_CONFIG_VERSION`` 保持一致（由
    # ``test_legacy_sim_config_migration`` 钉死）；写盘路径 ``app_config_to_dict``
    # 另有强制兜底，即使此处漂移也不会写出过期版本号。
    config_version: int = 13
    config_path: str = ""

    def resolved_config_path(self) -> Path | None:
        """把可选字符串 ``config_path`` 规范化为 ``Path``。

        返回：
            ``Path`` 对象；当 ``config_path`` 为空字符串或 ``None`` 时返回 ``None``，
            调用方据此决定回落到默认配置路径。
        """
        # 空字符串视为"未指定"，避免向 ``Path("")`` 后续做相对路径解析。
        if not self.config_path:
            return None
        return Path(self.config_path)

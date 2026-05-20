"""无真实硬件时使用的仿真 adapter 三件套。

作用：
    本文件实现与真实 ``adapters.py`` 完全相同接口的仿真版本：
        - ``SimulatedCameraAdapter``：生成可重复的 ``uint16`` 9 帧 stack 与预览帧。
        - ``SimulatedSlmAdapter``：列出虚构的 36 个 Running Order，并支持普通
          pattern 编程，让正式 SIM 采集流程在无 R11 硬件时仍可端到端跑通。
        - ``SimulatedDaqAdapter``：记录 ``play_waveform`` / ``pulse_line``
          调用，但不向真实 USB-6423 输出。
    所有方法签名与 ``protocols.py`` 中的 Protocol 一致；调用方注入哪个版本
    取决于 ``BackendConfig.simulation_mode``。

协作关系：
    上游：``controller.SimAcquisitionController._create_adapters`` 在
          ``backend.simulation_mode=True`` 时构造这三个类。
    下游：``models``、``waveform.parse_line_name``、``waveform.WaveformPlan``。
    相关：``tests/`` 中绝大多数测试都使用本文件的仿真 adapter。

关键概念：
    - ``np.random.default_rng(12345)``：固定随机数种子保证 ``_generate_frame``
      在同一进程内可重复（便于 visual diff 与回归测试）。
    - ``SIMULATED_RUNNING_ORDERS``：覆盖 4 波长 × 3 曝光 × {normal, _ang0}
      共 24 个名字（注意 ``647/3.5/2d/1ms`` 这类）。它们的命名格式与真实
      R11 repertoire 保持一致，因此 ``adapters.find_best_running_order`` 在
      仿真路径下也能正常工作。

维护要点：
    - 仿真生成的帧 dtype 必须保持 ``uint16``，否则 ``acquisition_core``
      校验会失败。
    - ``SimulatedDaqAdapter.play_waveform`` 故意短暂阻塞（最多 100 ms），
      模拟真实 DAQ 播放期间 GUI 不可立即返回的体验，便于 stop_event 测试。
"""

from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any

import numpy as np

from .models import CameraConfig, PatternPreparationResult
from .waveform import WaveformPlan, parse_line_name


# 仿真用 Running Order 名称：覆盖 4 波长 × 3 曝光 × {normal, _ang0}。
# 顺序刻意先按波长聚类，再按曝光、再按 angle，便于在 GUI 下拉中分组浏览。
SIMULATED_RUNNING_ORDERS = [
    f"{wavelength}_3.5_2d_{exposure}{suffix}"
    for wavelength in (405, 488, 561, 647)
    for exposure in ("10ms", "1ms", "50ms")
    for suffix in ("", "_ang0")
]
# 仿真相机支持的 bit depth；与真实 Hamamatsu Fusion BT 的常用集合保持一致。
SIMULATED_CAMERA_BIT_DEPTHS = [8, 12, 16]


# 仿真相机提供稳定可重复的帧数据，让测试不依赖 Hamamatsu 真机。
class SimulatedCameraAdapter:
    """离线相机仿真，生成预览帧和 9 帧 ``uint16`` stack。

    职责：
        - 维护连接/arm/preview 三个状态。
        - 用固定种子的 ``np.random.default_rng`` 生成可重复随机噪声帧。
        - 在 ``apply_config`` 中报告 ``recommended_inter_frame_gap_us``，让
          上层走与真机一致的"有效帧间隔"路径。

    协作：
        被 ``SimAcquisitionController`` 通过 ``protocols.CameraAdapter`` 注入；
        测试夹具中也通过此类直接构造。
    """
    def __init__(self):
        # 内部状态字段：连接、arm、preview 三态分别独立，避免互相干扰。
        self._initialized = False
        self._connected = False
        self._armed = False
        self._frame_count = 0
        self._camera_config = CameraConfig()
        self._connection_info: dict[str, Any] = {}
        self._preview_active = False
        # 固定种子的 RNG：保证仿真帧在不同会话间也可重复。
        self._rng = np.random.default_rng(12345)

    def initialize(self) -> None:
        # 仿真模式下 ``initialize`` 仅置位 flag；真实 adapter 在这里会加载 DCAM 模块。
        self._initialized = True

    def list_devices(self) -> list[dict[str, Any]]:
        # 始终返回一台虚构相机；GUI 下拉据此显示「Simulated ORCA-Fusion BT」。
        self.initialize()
        return [
            {
                "index": 0,
                "model": "Simulated ORCA-Fusion BT",
                "camera_id": "SIM-CAMERA-001",
                "driver_version": "simulation",
                "display": "0: Simulated ORCA-Fusion BT [SIM-CAMERA-001]",
            }
        ]

    def is_connected(self) -> bool:
        return self._connected

    def connection_info(self) -> dict[str, Any]:
        # 返回浅拷贝，避免上层修改 dict 影响仿真状态。
        return dict(self._connection_info)

    def connect(self, device_index: int | None = None, device_label: str = "") -> dict[str, Any]:
        # 1) 确保 initialize 已执行；显式做幂等。
        self.initialize()
        selected_index = 0 if device_index is None else int(device_index)
        display = device_label or f"{selected_index}: Simulated ORCA-Fusion BT [SIM-CAMERA-001]"
        # 2) 标记已连接并写入 connection_info；后续 ``connection_info()`` 返回浅拷贝。
        self._connected = True
        self._connection_info = {
            "index": selected_index,
            "model": "Simulated ORCA-Fusion BT",
            "camera_id": "SIM-CAMERA-001",
            "driver_version": "simulation",
            "display": display,
            "supported_bit_depths": list(SIMULATED_CAMERA_BIT_DEPTHS),
            "simulation": True,
        }
        return dict(self._connection_info)

    def disconnect(self) -> None:
        # 清空所有三态，模拟相机彻底掉电后的状态。
        self._connected = False
        self._preview_active = False
        self._armed = False
        self._connection_info = {}

    def apply_config(self, config: CameraConfig) -> dict[str, Any]:
        # 1) 未连接时自动连接：方便单测一行 ``apply_config(...)`` 跑通。
        self.initialize()
        if not self._connected:
            self.connect(device_index=config.device_index, device_label=config.device_label)
        # 2) 保存配置；非法 bit_depth 回落到 16，避免下游 ``_generate_frame`` 算出非法 max_value。
        self._camera_config = config
        if int(config.bit_depth) not in SIMULATED_CAMERA_BIT_DEPTHS:
            config.bit_depth = 16
        # 3) 返回"相机实际接受"的报告，包括建议帧间隔；让上层走和真机一致的有效帧间隔路径。
        return {
            "camera_model": "Simulated ORCA-Fusion BT",
            "camera_id": "SIM-CAMERA-001",
            "supported_bit_depths": list(SIMULATED_CAMERA_BIT_DEPTHS),
            "applied_bit_depth": int(config.bit_depth),
            "recommended_inter_frame_gap_us": max(1000, int(config.exposure_us // 2)),
            "simulation": True,
        }

    def get_supported_bit_depths(self) -> list[int]:
        return list(SIMULATED_CAMERA_BIT_DEPTHS)

    @property
    def preview_active(self) -> bool:
        return self._preview_active

    def start_preview(self, config: CameraConfig, frame_buffer_count: int = 3) -> None:
        # 依赖 ``apply_config`` 把传入的 CameraConfig 锁定到内部状态，再切预览 flag。
        self.apply_config(config)
        self._preview_active = True

    def read_preview_frame(self, timeout_ms: int = 100) -> np.ndarray:
        # 1) preview 未启动直接抛错，避免上层把 stale 数据当作真实预览帧。
        if not self._preview_active:
            raise RuntimeError("Simulated preview is not active.")
        # 2) 模拟读出延迟：把 timeout_ms 限制在 [1, 10] ms，避免 CPU 空转。
        time.sleep(min(max(timeout_ms, 1) / 1000.0, 0.01))
        # 3) 生成一帧仿真图像并返回；调用方一般立即送 LUT 显示。
        return self._generate_frame()

    def stop_preview(self) -> None:
        self._preview_active = False

    def arm(self, frame_count: int) -> None:
        # 记录帧数并切到 armed；read_frame_sequence 严格检查这个状态。
        self._frame_count = int(frame_count)
        self._armed = True

    def disarm(self) -> None:
        self._armed = False

    def read_frame_sequence(
        self,
        frame_count: int,
        pattern_files: list[str],
        laser_wavelength_nm: int,
        frame_callback: Any | None = None,
        stop_event: Any | None = None,
    ) -> tuple[np.ndarray, list[float]]:
        # 1) 未 arm 直接抛错；与真实 DCAM 行为一致：必须先 ``arm`` 才能拿到帧。
        if not self._armed:
            raise RuntimeError("Simulated camera must be armed before reading frames.")
        # 2) 预分配 stack 数组：ROI 大小 + 帧数 → ``(frame_count, H, W)`` uint16。
        height = int(self._camera_config.roi_height)
        width = int(self._camera_config.roi_width)
        frames = np.empty((int(frame_count), height, width), dtype=np.uint16)
        timestamps: list[float] = []
        # 3) 逐帧生成 + 回调；遇到 stop_event 立即抛错让上层走 AcquisitionCancelled 路径。
        for index in range(1, int(frame_count) + 1):
            if stop_event is not None and stop_event.is_set():
                raise RuntimeError("Acquisition cancelled.")
            frame = self._generate_frame()
            frames[index - 1] = frame
            timestamp = time.time()
            timestamps.append(timestamp)
            if frame_callback is not None:
                frame_callback(index, timestamp)
        return frames, timestamps

    def _generate_frame(self) -> np.ndarray:
        # 1) 取当前配置 ROI 尺寸；不允许负数（防御调用方 bug）。
        height = int(self._camera_config.roi_height)
        width = int(self._camera_config.roi_width)
        # 2) 根据 bit_depth 计算 max_value，再夹到 uint16 极值，保证生成值合法。
        max_value = min((1 << int(self._camera_config.bit_depth)) - 1, np.iinfo(np.uint16).max)
        # 3) 设定随机区间 [low, high)：在低位深下避免 high<=low，否则 randint 抛错。
        low = min(500, max_value // 8)
        high = min(4000, max_value + 1)
        if high <= low:
            low = 0
            high = max(1, max_value + 1)
        # 4) 用固定 RNG 生成 uint16 随机噪声帧；可被 visual 与统计断言重复检查。
        signal = self._rng.integers(low, high, size=(height, width), dtype=np.uint16)
        return signal


# 仿真 SLM 模拟 Running Order 和 pattern 准备，保持正式流程可离线跑通。
class SimulatedSlmAdapter:
    """离线 SLM 仿真，模拟连接、Running Order 列举 / 选择和普通 pattern 编程。

    协作：
        与 ``SimulatedCameraAdapter`` 一道支撑 ``simulation_mode=True`` 下的端到端
        SIM 采集；正式采集走 ``select_running_order``，旧调试路径走 ``program_patterns``。
    """
    def __init__(self):
        self._initialized = False
        self._connected = False
        # 默认空 pattern 准备结果：``activate_prepared_patterns`` 会拒绝它。
        self._prepared = PatternPreparationResult()
        self._connection_info: dict[str, Any] = {}

    def initialize(self) -> None:
        self._initialized = True

    def list_devices(self) -> list[dict[str, str]]:
        # 返回固定一台虚构 SLM；display/path/id 字段保持与真实 adapter 风格一致。
        self.initialize()
        return [{"display": "Simulated Kopin QXGA-R11", "path": "simulated-r11", "id": "SIM-SLM-001", "serial": "SIM-SLM-001"}]

    def is_connected(self) -> bool:
        return self._connected

    def connection_info(self) -> dict[str, Any]:
        return dict(self._connection_info)

    def connect(self, device_path: str | None = None) -> dict[str, Any]:
        # 1) 标记 connected 并填入 connection_info；接受任意 device_path 字符串。
        self.initialize()
        self._connected = True
        self._connection_info = {
            "device_id": "SIM-SLM-001",
            "device_path": device_path or "simulated-r11",
            "device_serial_hint": "SIM-SLM-001",
            "serial_number": "SIM-SLM-001",
            "repertoire_name": "simulation",
            "simulation": True,
        }
        return dict(self._connection_info)

    def disconnect(self) -> None:
        self._connected = False
        self._connection_info = {}

    def list_running_orders(self) -> list[tuple[int, str]]:
        # 返回 (index, name) 元组列表；命名与真实 R11 repertoire 同模式，可被 find_best_running_order 解析。
        self.initialize()
        return list(enumerate(SIMULATED_RUNNING_ORDERS))

    def select_running_order(self, ro_index: int) -> dict[str, Any]:
        # 1) 未连接自动连接，与真实 adapter 不同（真实路径要求显式 connect），让单测更省事。
        self.initialize()
        if not self._connected:
            self.connect()
        ro_index = int(ro_index)
        # 2) 越界直接抛错；上层 GUI 会把异常翻译成对话框文本。
        try:
            ro_name = SIMULATED_RUNNING_ORDERS[ro_index]
        except IndexError as exc:
            raise RuntimeError(f"Simulated running order index out of range: {ro_index}") from exc
        # 3) 把"RO 已选中"打包成 PatternPreparationResult，统一接口给 acquisition_core。
        self._prepared = PatternPreparationResult(
            pattern_files=[ro_name] * 9,
            handles=[-1],
            prepared_at=time.time(),
            metadata={
                "mode": "running_order",
                "running_order_index": ro_index,
                "running_order_name": ro_name,
                "simulation": True,
                **self._connection_info,
            },
        )
        return {
            "running_order_index": ro_index,
            "running_order_name": ro_name,
            "pattern_result": self._prepared,
            "simulation": True,
        }

    def program_patterns(self, pattern_files: list[str], device_path: str | None = None) -> PatternPreparationResult:
        # 1) 必须传入 9 帧 pattern；任何长度不等于 9 都拒绝。
        if len(pattern_files) != 9:
            raise RuntimeError("Exactly 9 pattern files are required.")
        # 2) 未连接时按需连接，方便测试一行写完。
        if not self._connected:
            self.connect(device_path=device_path)
        # 3) handles 用 0..8 顺序号代替真实 SDK 句柄；元数据标记仿真模式。
        self._prepared = PatternPreparationResult(
            pattern_files=list(pattern_files),
            handles=list(range(len(pattern_files))),
            prepared_at=time.time(),
            metadata={"mode": "simulation", **self._connection_info},
        )
        return self._prepared

    def activate_prepared_patterns(self) -> None:
        # 1) handles 为空表示从未准备过 patterns / RO，拒绝激活。
        if not self._prepared.handles:
            raise RuntimeError("No simulated patterns or running order prepared on SLM.")
        # 2) 自动连接；真实 adapter 不允许这样做，但仿真路径优先减少测试样板代码。
        if not self._connected:
            self.connect()

    def prepared_summary(self) -> dict[str, Any]:
        # ``asdict`` 把 dataclass 展平，让 GUI 摘要可以直接 json.dumps 查看。
        return asdict(self._prepared)


# 仿真 DAQ 只记录调用和短暂停顿，不向真实 USB-6423 输出信号。
class SimulatedDaqAdapter:
    """离线 DAQ 仿真，记录波形播放和脉冲调用但不触发硬件。

    维护要点：
        - ``play_waveform`` 故意短暂阻塞，模拟真实 DAQ 播放无法立即返回，方便
          stop_event 测试观察取消语义。
        - ``pulse_line`` 仍调 ``parse_line_name``，以便在仿真路径下也能尽早发现非法线名。
    """
    def list_devices(self, default_device: str = "Dev1") -> list[str]:
        # 仿真模式下永远返回一个默认设备名，GUI 下拉据此渲染。
        return [default_device or "Dev1"]

    def list_port0_lines(self, device_name: str | None = None, default_device: str | None = None) -> list[str]:
        # 1) 解析最终设备名（参数 → 默认参数 → ``"Dev1"``）。
        selected_device = device_name or default_device or "Dev1"
        # 2) 生成 16 条 port0 line 名；GUI 下拉据此构建。
        return [f"{selected_device}/port0/line{index}" for index in range(16)]

    def play_waveform(self, device_name: str, plan: WaveformPlan, stop_event: Any | None = None) -> None:
        # 1) 计算一个最长 100 ms 的等待 deadline，模拟真实 DAQ 不会立即返回。
        deadline = time.monotonic() + min(max(plan.duration_s, 0.0), 0.1)
        # 2) 循环短睡眠（10 ms 一片），让 stop_event 设置后能尽快响应。
        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                return
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))

    def set_all_low(self, device_name: str) -> None:
        # 仿真路径无副作用；保留方法签名与真实 adapter 对齐。
        return None

    def pulse_line(self, device_name: str, line_index: int, duration_s: float) -> None:
        # 1) 把虚构线名喂给真实解析器，让上层在仿真路径下也能发现非法 line index。
        parse_line_name(f"{device_name}/port0/line{int(line_index)}")
        # 2) 模拟脉冲长度（最长 100 ms），避免 UI 测试中按下"测试"按钮即时返回造成误以为成功。
        time.sleep(min(max(float(duration_s), 0.0), 0.1))

"""相机、SLM 和 DAQ adapter 的协议（Protocol）边界。

作用：
    本文件定义三个 ``typing.Protocol``，列出 controller 需要从硬件层得到的
    **最小**能力集合：
        - ``CameraAdapter``：枚举/连接相机、应用配置、预览、武装/采集 9 帧。
        - ``SlmAdapter``：枚举/连接 SLM、列出 Running Order、激活预烧录 RO 或
          普通 9 帧 pattern。
        - ``DaqAdapter``：枚举设备/线位、播放 ``WaveformPlan``、把所有线置低、
          测试单线脉冲。
    真实 adapter（``adapters.py``）和仿真 adapter（``sim_adapters.py``）都按
    这些方法实现，从而让 controller 与测试可以面向同一个抽象编程。

协作关系：
    上游：``controller.py`` 与 ``tests/`` 中的 mock adapter。
    下游：``adapters.py`` 提供真实硬件实现；``sim_adapters.py`` 提供仿真实现。
    相关：``models.py`` 提供参数与返回值类型；``waveform.py`` 提供
          ``WaveformPlan``。

关键概念：
    - ``@runtime_checkable`` 让 ``isinstance(x, CameraAdapter)`` 仅做"方法名
      存在性"检查，不验证签名；测试里用得到。
    - Protocol 体内方法只写 ``...``（无实现），形式上是抽象签名声明。

维护要点：
    - 修改 Protocol 方法签名前，必须先确保 ``adapters.py``、``sim_adapters.py``、
      ``controller.py``、相关测试都已同步；任何一处漏改都会让运行期失败。
    - 不要在 Protocol 中堆砌可选 helper；Protocol 应只保留 controller 真正
      调用的方法，其它工具方法放具体实现类即可。
    - Protocol 方法 docstring 不放在 ``...`` 上方会有解析歧义，**保持
      ``def xxx(self, ...) -> T: ...`` 同一行结尾**，docstring 放在类签名下方。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import numpy as np

from .models import CameraConfig, PatternPreparationResult
from .waveform import WaveformPlan


@runtime_checkable
class CameraAdapter(Protocol):
    """controller 期望的相机最小能力集合。

    职责：
        - 设备枚举/连接（``list_devices`` / ``connect`` / ``disconnect``）。
        - 配置下发（``apply_config`` 把 ``CameraConfig`` 翻译成 DCAM 属性）。
        - 实时预览（``start_preview`` / ``read_preview_frame`` / ``stop_preview``），
          采用 latest-frame-wins 模型。
        - SIM9 采集（``arm`` / ``read_frame_sequence`` / ``disarm``），返回
          ``(9, H, W)`` ``uint16`` stack 与时间戳。

    协作：
        被 ``SimAcquisitionController`` 直接调用；真实实现在
        ``FusionBtCameraAdapter``，仿真实现在 ``SimulatedCameraAdapter``。
    """

    def initialize(self) -> None: ...

    def list_devices(self) -> list[dict[str, Any]]: ...

    def is_connected(self) -> bool: ...

    def connection_info(self) -> dict[str, Any]: ...

    def connect(self, device_index: int | None = None, device_label: str = "") -> dict[str, Any]: ...

    def disconnect(self) -> None: ...

    def apply_config(self, config: CameraConfig) -> dict[str, Any]: ...

    def get_supported_bit_depths(self) -> list[int]: ...

    @property
    def preview_active(self) -> bool: ...

    def start_preview(self, config: CameraConfig, frame_buffer_count: int = 3) -> None: ...

    def read_preview_frame(self, timeout_ms: int = 100) -> np.ndarray: ...

    def stop_preview(self) -> None: ...

    def arm(self, frame_count: int) -> None: ...

    def disarm(self) -> None: ...

    def read_frame_sequence(
        self,
        frame_count: int,
        pattern_files: list[str],
        laser_wavelength_nm: int,
        frame_callback: Any | None = None,
        stop_event: Any | None = None,
    ) -> tuple[np.ndarray, list[float]]: ...


@runtime_checkable
class SlmAdapter(Protocol):
    """controller 期望的 SLM 最小能力集合。

    职责：
        - 设备枚举/连接。
        - Running Order 列举 / 选择（正式 SIM 采集走 RO 路径）。
        - 普通图案准备 / 激活（保留兼容旧调试路径）。
        - 暴露 ``prepared_summary`` 供 GUI 摘要使用。

    协作：
        真实实现在 ``KopinSlmAdapter``，仿真实现在 ``SimulatedSlmAdapter``。
        ``SimSettingsDialog`` 不应持有 SLM 生命周期，必须共享 controller 的
        ``slm_adapter`` 实例，避免 R11 WinUSB 设备被重复打开。
    """

    def initialize(self) -> None: ...

    def list_devices(self) -> list[dict[str, str]]: ...

    def is_connected(self) -> bool: ...

    def connection_info(self) -> dict[str, Any]: ...

    def connect(self, device_path: str | None = None) -> dict[str, Any]: ...

    def disconnect(self) -> None: ...

    def list_running_orders(self) -> list[tuple[int, str]]: ...

    def select_running_order(self, ro_index: int) -> dict[str, Any]: ...

    def get_running_order_activation_state(self) -> dict[str, Any]: ...

    def program_patterns(self, pattern_files: list[str], device_path: str | None = None) -> PatternPreparationResult: ...

    def activate_prepared_patterns(self) -> None: ...

    def prepared_summary(self) -> dict[str, Any]: ...


@runtime_checkable
class DaqAdapter(Protocol):
    """controller 期望的 DAQ 最小能力集合。

    职责：
        - 设备/线位枚举（GUI 下拉用）。
        - 播放 ``WaveformPlan``（一次 SIM9 采集的核心动作）。
        - 把所有 SIM 相关线置低（采集结束或停止后做安全归位）。
        - 单线脉冲测试（DAQ 设置弹窗的诊断按钮使用）。

    协作：
        真实实现在 ``NIDaqAdapter``，仿真实现在 ``SimulatedDaqAdapter``。
    """

    def list_devices(self, default_device: str = "Dev1") -> list[str]: ...

    def list_port0_lines(self, device_name: str | None = None, default_device: str | None = None) -> list[str]: ...

    def play_waveform(self, device_name: str, plan: WaveformPlan, stop_event: Any | None = None) -> None: ...

    def set_all_low(self, device_name: str) -> None: ...

    def set_line(self, device_name: str, line_index: int, high: bool) -> None: ...

    def pulse_line(self, device_name: str, line_index: int, duration_s: float) -> None: ...

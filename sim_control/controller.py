"""SIM 采集控制器与后台 worker。

作用：
    本文件是 GUI 与 SIM 硬件 / 采集核心之间的"调度门面"：
        - ``SimAcquisitionWorker``：``QObject``，运行在独立 ``QThread`` 中，
          负责"初始化硬件 → 应用 DAQ 与相机配置 → 选择 SLM Running Order →
          调用 ``run_single_acquisition`` 或仅做 prepare-only"。所有可能阻塞的硬件
          调用都放在这里，避免阻塞 GUI 主线程。
        - ``SimAcquisitionController``：``QObject``，由 GUI 直接持有；
          组装 worker payload、管理 ``stop_event``、转发 worker 的状态/结果/失败/
          取消信号给 GUI。

协作关系：
    上游：``sim_control/gui.py``、``control_wangbo/main.py`` 的 SIM 集成模块。
    下游：``acquisition_core.run_single_acquisition``、``adapters.*``、
          ``sim_adapters.*``、``waveform.NIDaqWaveformBuilder``。
    相关：``config_store.validate_app_config`` 在每次启动 worker 前校验配置。

关键概念：
    - ``stop_event``（``threading.Event``）：每次启动一个 worker task 都建一个；
      ``stop()`` 把它置位；worker 与 ``acquisition_core`` 内部都会检查它，最大程度
      缩短取消响应延迟。
    - ``signal_start_worker``：跨线程 Qt 信号，把 payload 投递到 worker。
    - ``effective_inter_frame_gap_us``：每次启动前用相机最新回报的
      ``recommended_inter_frame_gap_us`` 调一次有效帧间隔，让 50 ms 默认与"更短
      读出"两种场景一致。

维护要点：
    - 任何新增硬件分支（如多相机）都必须保证：worker payload 中所有 adapter
      引用都来自 controller 实例（避免 SLM/相机重复打开）。
    - ``stop_event`` 在 worker 完成/失败/取消三种路径里都要清理；本文件通过
      ``_clear_stop_event_for_*`` 集中处理。
    - GUI 不可直接持有 ``SimSettingsDialog`` 之外的 SLM/相机生命周期，必须共
      享本 controller 的 ``camera_adapter`` / ``slm_adapter``（决策日志
      2026-04-26 的硬约束）。
"""

from __future__ import annotations

import logging
import threading
import traceback
from typing import Any

from PyQt5.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

from .acquisition_core import AcquisitionCancelled, run_single_acquisition
from .adapter_factory import create_adapter_bundle
from .adapters import (
    HardwareError,
    find_best_running_order,
    find_z_scan_running_order,
)
from .config_store import validate_app_config
from .models import (
    AppConfig,
    BackendConfig,
    CameraConfig,
    DaqLineConfig,
    PatternPreparationResult,
    ReconstructionConfig,
    SimTaskConfig,
    ZScanConfig,
    effective_inter_frame_gap_us,
    new_task_id,
)
from .waveform import NIDaqWaveformBuilder, validate_daq_line_config


logger = logging.getLogger(__name__)


def _acquisition_summary_payload(batch: Any) -> dict[str, Any]:
    """Build the GUI-facing acquisition summary without copying image data."""
    stack = getattr(batch, "stack", None)
    shape = getattr(stack, "shape", ())
    dtype = getattr(stack, "dtype", "")
    metadata = getattr(batch, "metadata", {}) or {}
    return {
        "task_id": str(getattr(batch, "task_id", "")),
        "stack_shape": list(shape),
        "stack_dtype": str(dtype),
        "metadata": dict(metadata),
    }


# Worker 运行在 QThread 中，所有可能阻塞的硬件初始化和采集动作都从 GUI 线程移出。
class SimAcquisitionWorker(QObject):
    """后台采集 worker，在 QThread 中执行硬件准备、RO 选择和正式采集。

    线程模型：
        - ``__init__`` 在 GUI 线程构造，``moveToThread(QThread)`` 之后所有 slot
          都在工作线程执行。
        - 通过 ``signal_start_worker`` 接收 payload；处理过程同步阻塞。

    状态信号：
        - ``signal_status_changed(status_name, payload_dict)``：阶段性进度。
        - ``signal_acquisition_ready(batch)``：正式采集成功。
        - ``signal_acquisition_summary_ready(payload)``：正式采集成功后的 GUI 轻量摘要。
        - ``signal_acquisition_failed(task_id, traceback)``：硬件/逻辑失败。
        - ``signal_acquisition_cancelled(task_id, message)``：用户/stop_event 取消。
        - ``signal_prepare_ready(task_id, payload)``：prepare_only 路径完成。
    """
    signal_status_changed = pyqtSignal(str, dict)
    signal_acquisition_ready = pyqtSignal(object)
    signal_acquisition_summary_ready = pyqtSignal(dict)
    signal_acquisition_failed = pyqtSignal(str, str)
    signal_acquisition_cancelled = pyqtSignal(str, str)
    signal_prepare_ready = pyqtSignal(str, dict)
    signal_z_scan_progress = pyqtSignal(int, int, float, float)
    signal_z_scan_complete = pyqtSignal(float, object)

    @pyqtSlot(object)
    def slot_start(self, payload: dict[str, Any]) -> None:
        """Qt 信号入口：在工作线程执行一整套 SIM 采集流程。

        payload 字典字段（``_start_worker_task`` 中组装）：
            - task / task_id / daq_config / pattern_result：核心数据。
            - waveform_builder / daq_adapter / camera_adapter / slm_adapter：注入对象。
            - stop_event：可选取消标志。
            - initialize_hardware / apply_daq_config / apply_camera_config /
              prepare_running_order / prepare_only：开关，控制本次跑哪些阶段。
        """
        # 1) 拆解 payload，命中类型注解便于 IDE 检查（运行时不强校验）。
        task: SimTaskConfig = payload["task"]
        task_id: str = payload["task_id"]
        daq_config: DaqLineConfig = payload["daq_config"]
        pattern_result: PatternPreparationResult = payload["pattern_result"]
        waveform_builder: NIDaqWaveformBuilder = payload["waveform_builder"]
        daq = payload["daq_adapter"]
        camera = payload["camera_adapter"]
        slm = payload["slm_adapter"]
        stage = payload.get("stage_adapter")
        z_scan_config: ZScanConfig | None = payload.get("z_scan_config")
        z_scan_enabled = bool(payload.get("z_scan_enabled")) and z_scan_config is not None
        z_scan_pattern_result: PatternPreparationResult | None = None
        stop_event = payload.get("stop_event")

        def emit_status(status: str, data: dict[str, Any]) -> None:
            self.signal_status_changed.emit(status, data)
            if status == "z_scan_progress":
                self.signal_z_scan_progress.emit(
                    int(data.get("step_index", 0)),
                    int(data.get("total_steps", 0)),
                    float(data.get("z_um", 0.0)),
                    float(data.get("focus_score", 0.0)),
                )
            elif status == "z_scan_complete":
                self.signal_z_scan_complete.emit(
                    float(data.get("best_z_um", 0.0)),
                    data.get("focus_curve", []),
                )

        try:
            _raise_if_cancelled(stop_event)
            # 2) 可选阶段 1：硬件初始化。仅当 controller 显式请求时执行；多次启动可跳过。
            if payload.get("initialize_hardware"):
                emit_status("hardware_initializing", {})
                camera.initialize()
                _raise_if_cancelled(stop_event)
                slm.initialize()
                emit_status("hardware_initialized", {})
            # 3) 可选阶段 2：DAQ 配置校验（不直接打开 NI 任务，只确认线位合法）。
            if payload.get("apply_daq_config"):
                validate_daq_line_config(daq_config)
                emit_status("daq_config_applied", {"device_name": daq_config.device_name})
            # 4) 可选阶段 3：把相机配置下发到 DCAM，并把相机回报反写到 task 与时序。
            if payload.get("apply_camera_config"):
                if task.camera.trigger_mode != "external_level":
                    raise HardwareError("Only external_level trigger mode is supported.")
                result = camera.apply_config(task.camera) or {}
                _apply_camera_result_to_config(task.camera, result)
                # 4a) 相机推荐间隔小于 50 ms 时使用推荐值；否则保留 50 ms 默认。
                task.timing.inter_frame_gap_us = effective_inter_frame_gap_us(
                    result.get("recommended_inter_frame_gap_us")
                )
                # 4b) 把整段相机配置 + 相机回报合并广播给 GUI，便于显示实际生效参数。
                payload_data = {"camera_config": dict(task.camera.__dict__), **dict(result)}
                emit_status("camera_config_applied", payload_data)
            # 5) 可选阶段 4：自动选择匹配波长 + 曝光的 Running Order。
            if payload.get("prepare_running_order"):
                running_orders = slm.list_running_orders()
                ro_index, ro_name, warnings = find_best_running_order(
                    running_orders,
                    wavelength_nm=int(task.laser_wavelength_nm),
                    exposure_us=int(task.camera.exposure_us),
                )
                if ro_index is None:
                    # 没匹配到 RO → 抛错让 GUI 提示用户检查 SLM 烧录内容。
                    raise HardwareError("; ".join(warnings) or "No matching SLM running order found.")
                result = slm.select_running_order(ro_index)
                # 5a) 规整成统一 PatternPreparationResult，下游 acquisition_core 才能消费。
                pattern_result = _coerce_running_order_pattern_result(result, ro_index, ro_name)
                task.running_order_name = ro_name
                task.pattern_files = list(pattern_result.pattern_files)
                emit_status(
                    "running_order_selected",
                    _running_order_payload(result, pattern_result, ro_index, ro_name, warnings),
                )
            if z_scan_enabled:
                if stage is None:
                    raise HardwareError("Z-scan is enabled but no Z stage adapter is available.")
                if not getattr(stage, "is_connected", False):
                    info = stage.connect()
                    emit_status("z_stage_connected", dict(info or {}))
                running_orders = slm.list_running_orders()
                z_ro_index, z_ro_name, z_warnings = find_z_scan_running_order(
                    running_orders,
                    exposure_preset_ms=int(z_scan_config.exposure_preset_ms),
                )
                if z_ro_index is None:
                    raise HardwareError("; ".join(z_warnings) or "No matching z-scan running order found.")
                z_result = slm.select_running_order(z_ro_index)
                z_scan_pattern_result = _coerce_running_order_pattern_result(z_result, z_ro_index, z_ro_name)
                emit_status(
                    "z_scan_running_order_selected",
                    _running_order_payload(z_result, z_scan_pattern_result, z_ro_index, z_ro_name, z_warnings),
                )
            _raise_if_cancelled(stop_event)
            # 6) prepare-only 路径：到这里就算完成；返回前广播"patterns_prepared"事件。
            if payload.get("prepare_only"):
                prepared_payload = {
                    "task_id": task_id,
                    "running_order_name": task.running_order_name,
                    "pattern_files": list(task.pattern_files),
                    "handles": list(pattern_result.handles),
                    "metadata": dict(pattern_result.metadata),
                }
                emit_status("patterns_prepared", prepared_payload)
                self.signal_prepare_ready.emit(task_id, prepared_payload)
                return
            # 7) 正式采集路径：调 ``run_single_acquisition``，由它负责 9 帧硬件流程。
            batch = run_single_acquisition(
                task=task,
                daq_config=daq_config,
                pattern_result=pattern_result,
                camera=camera,
                slm=slm,
                daq=daq,
                waveform_builder=waveform_builder,
                task_id=task_id,
                on_status=emit_status,
                stop_event=stop_event,
                stage_adapter=stage,
                z_scan_config=z_scan_config if z_scan_enabled else None,
                z_scan_pattern_result=z_scan_pattern_result,
            )
            self.signal_acquisition_ready.emit(batch)
            self.signal_acquisition_summary_ready.emit(_acquisition_summary_payload(batch))
        except AcquisitionCancelled as exc:
            # 8a) 取消路径：广播 ``acquisition_cancelled``，不发 failed 信号，
            #     便于 GUI 区分"用户主动停"和"硬件出错"。
            message = str(exc) or "Acquisition cancelled."
            emit_status("acquisition_cancelled", {"task_id": task_id, "message": message})
            self.signal_acquisition_cancelled.emit(task_id, message)
        except Exception as exc:
            # 8b) 失败路径：附上完整 traceback，让 GUI 弹错误对话框时可让用户复制。
            self.signal_acquisition_failed.emit(task_id, f"{exc}\n{traceback.format_exc()}")


def _raise_if_cancelled(stop_event: Any | None) -> None:
    """worker 内部"快速取消"助手；与 ``acquisition_core._raise_if_cancelled`` 语义相同。"""
    if stop_event is not None and stop_event.is_set():
        raise AcquisitionCancelled("Acquisition cancelled.")


def _apply_camera_result_to_config(config: CameraConfig, result: dict[str, Any]) -> None:
    """把相机实际接受的位深和 ROI 回写到任务配置。

    用途：
        DCAM 可能根据传感器边界把 ROI 调整到对齐位置；如果不回写 task.camera，
        后续摘要/UI 显示的会是用户原始请求，与硬件实际生效不一致。
    """
    # 1) bit_depth：DCAM 可能 fallback 到接近的合法值（如 12 → 16）。
    if result.get("applied_bit_depth") is not None:
        config.bit_depth = int(result["applied_bit_depth"])
    # 2) applied_roi：相机回报实际接受的 (x, y, width, height)；逐字段回写。
    applied_roi = result.get("applied_roi")
    if isinstance(applied_roi, dict):
        config.roi_x = int(applied_roi.get("x", config.roi_x))
        config.roi_y = int(applied_roi.get("y", config.roi_y))
        config.roi_width = int(applied_roi.get("width", config.roi_width))
        config.roi_height = int(applied_roi.get("height", config.roi_height))


def _coerce_running_order_pattern_result(
    result: dict[str, Any],
    ro_index: int,
    ro_name: str,
) -> PatternPreparationResult:
    """把 SLM RO 选择结果规整成统一的 ``PatternPreparationResult``。

    用途：
        ``select_running_order`` 不同 adapter 实现可能直接返回
        ``PatternPreparationResult``，也可能只返回元数据字典；本函数把后者补全
        为标准 PatternPreparationResult，下游 acquisition_core 才能直接消费。
    """
    # 1) 如果 adapter 已直接返回 PatternPreparationResult，直接转交。
    pattern_result = result.get("pattern_result")
    if isinstance(pattern_result, PatternPreparationResult):
        return pattern_result
    # 2) 否则用 (ro_index, ro_name) 自行构造一个：handles=[-1] 表示 RO 模式无显式句柄。
    return PatternPreparationResult(
        pattern_files=[ro_name] * 9,
        handles=[-1],
        prepared_at=0.0,
        metadata={
            "mode": "running_order",
            "running_order_index": ro_index,
            "running_order_name": ro_name,
        },
    )


def _running_order_payload(
    result: dict[str, Any],
    pattern_result: PatternPreparationResult,
    ro_index: int,
    ro_name: str,
    warnings: list[str],
) -> dict[str, Any]:
    """把 Running Order 选择结果整理成 GUI 状态信号可直接显示的 payload 字典。"""
    return {
        **dict(result),
        "running_order_index": ro_index,
        "running_order_name": ro_name,
        "warnings": list(warnings),
        "handles": list(pattern_result.handles),
        "prepared_at": pattern_result.prepared_at,
    }


# Controller 是 GUI 与 worker 的桥：负责组装 payload、管理 stop_event 和转发结果信号。
class SimAcquisitionController(QObject):
    """采集控制门面，向 GUI 提供 prepare、run、stop 等异步操作。

    职责：
        - 创建并管理一对 ``QThread`` + ``SimAcquisitionWorker``。
        - 持有三个 adapter（相机/SLM/DAQ），让 SimSettingsDialog 与集成主界面
          共享同一份硬件连接。
        - 维护 ``stop_event`` 字典，把 ``stop()`` 转发到所有活跃 task。
        - 把 worker 的状态/结果/失败/取消信号转发到 GUI。
        - 提供 prepare-only 与正式采集两套入口。

    协作：
        - 由 GUI 顶层 SimControlWindow 或 ``control_wangbo/main.py`` 单例持有。
        - 通过 ``backend.simulation_mode`` 决定 ``_create_adapters`` 走真实
          还是仿真路径。
    """
    signal_status_changed = pyqtSignal(str, dict)
    signal_acquisition_ready = pyqtSignal(object)
    signal_acquisition_summary_ready = pyqtSignal(dict)
    signal_acquisition_failed = pyqtSignal(str, str)
    signal_acquisition_cancelled = pyqtSignal(str, str)
    signal_z_scan_progress = pyqtSignal(int, int, float, float)
    signal_z_scan_complete = pyqtSignal(float, object)
    # 跨线程信号：把 payload 投递给 worker 的 ``slot_start``。
    signal_start_worker = pyqtSignal(object)

    def __init__(self, backend: BackendConfig | None = None, parent: QObject | None = None):
        # 1) 调父类构造；让 controller 挂在 parent 上便于自动清理。
        super().__init__(parent)
        # 2) backend 缺省 → 用默认配置（非仿真模式 + 空 SDK 路径）。
        self.backend = backend or BackendConfig()
        # 3) 单实例波形 builder，避免每次采集都重建。
        self.waveform_builder = NIDaqWaveformBuilder()
        # 4) 创建 3 个 adapter（真实 or 仿真，由 backend.simulation_mode 决定）。
        (
            self.camera_adapter,
            self.slm_adapter,
            self.daq_adapter,
            self.stage_adapter,
        ) = self._create_adapters(self.backend)
        # 5) 默认 DAQ/相机/图案配置；后续 GUI 会通过 ``apply_*`` 覆盖。
        self.daq_config = DaqLineConfig()
        self.camera_config = CameraConfig()
        self.z_scan_config = ZScanConfig()
        self.reconstruction_config = ReconstructionConfig()
        self.pattern_result = PatternPreparationResult()
        # 6) 记录相机最近一次回报的 timing 信息（含 recommended_inter_frame_gap_us）。
        self._latest_camera_timing: dict[str, Any] = {}
        # 7) shutdown 与 stop_event 簿记：``_active_stop_events`` 按 task_id 索引，
        #    ``_current_stop_event`` 是最近一次启动的 event 引用。
        self._shutdown_requested = False
        self._active_stop_events: dict[str, threading.Event] = {}
        self._current_stop_event: threading.Event | None = None

        # 8) 创建独立 QThread + worker，绑定一组信号路由，再启动线程。
        self._thread = QThread(self)
        self._worker = SimAcquisitionWorker()
        self._worker.moveToThread(self._thread)
        self._worker.signal_status_changed.connect(self.signal_status_changed)
        self._worker.signal_acquisition_ready.connect(self.signal_acquisition_ready)
        self._worker.signal_acquisition_summary_ready.connect(self.signal_acquisition_summary_ready)
        self._worker.signal_acquisition_failed.connect(self.signal_acquisition_failed)
        self._worker.signal_acquisition_cancelled.connect(self.signal_acquisition_cancelled)
        self._worker.signal_z_scan_progress.connect(self.signal_z_scan_progress)
        self._worker.signal_z_scan_complete.connect(self.signal_z_scan_complete)
        # 8a) 三个清理钩子：prepare_ready/ready/failed/cancelled 都要从字典里移除 stop_event。
        self._worker.signal_prepare_ready.connect(self._clear_stop_event_for_task)
        self._worker.signal_acquisition_summary_ready.connect(self._clear_stop_event_for_summary)
        self._worker.signal_acquisition_failed.connect(self._clear_stop_event_for_task)
        self._worker.signal_acquisition_cancelled.connect(self._clear_stop_event_for_task)
        # 8b) ``signal_start_worker`` 触发 worker.slot_start；跨线程投递。
        self.signal_start_worker.connect(self._worker.slot_start)
        self._thread.start()

    @staticmethod
    def _create_adapters(backend: BackendConfig):
        """根据 ``backend.simulation_mode`` 创建真实或仿真 adapter 四件套。

        返回：
            ``(camera_adapter, slm_adapter, daq_adapter, stage_adapter)`` 元组。
        """
        bundle = create_adapter_bundle(backend)
        return bundle.camera, bundle.slm, bundle.daq, bundle.stage

    def shutdown(self) -> None:
        """停止后台 worker、按 Qt 生命周期顺序断开相机、SLM 与 DAQ。"""
        # 1) 标记 shutdown，使其它槽函数知道不要再启动新任务。
        self._shutdown_requested = True
        # 2) 取消所有活跃 stop_event；忽略异常，确保后续清理动作不被中断。
        try:
            self.stop()
        except Exception:
            logger.warning("Failed to request stop during controller shutdown.", exc_info=True)
        # 3) 退出 QThread。``wait(2000)`` 给 worker 2 秒优雅退出窗口。
        self._thread.quit()
        if not self._thread.wait(2000):
            self.signal_status_changed.emit("shutdown_timeout", {})
            return
        # 4) 主动断相机和 SLM；DAQ 没有显式 disconnect 接口。
        try:
            self.camera_adapter.disconnect()
        except Exception:
            logger.warning("Failed to disconnect camera during controller shutdown.", exc_info=True)
        try:
            self.slm_adapter.disconnect()
        except Exception:
            logger.warning("Failed to disconnect SLM during controller shutdown.", exc_info=True)
        try:
            self.stage_adapter.disconnect()
        except Exception:
            logger.warning("Failed to disconnect stage during controller shutdown.", exc_info=True)

    def initialize_hardware(self) -> None:
        """同步初始化相机和 SLM，并广播状态。注：真实硬件可能阻塞数秒。"""
        self.signal_status_changed.emit("hardware_initializing", {})
        self.camera_adapter.initialize()
        self.slm_adapter.initialize()
        self.signal_status_changed.emit("hardware_initialized", {})

    def initialize_camera(self) -> None:
        """仅初始化相机（用于 GUI 中"重连相机"按钮的轻量入口）。"""
        self.camera_adapter.initialize()
        self.signal_status_changed.emit("camera_initialized", {})

    def refresh_available_camera_devices(self) -> list[dict[str, Any]]:
        """刷新 GUI 相机下拉框：返回当前可见的相机列表（不更改连接状态）。"""
        return self.camera_adapter.list_devices()

    def connect_camera(self, device_index: int | None = None, device_label: str = "") -> dict[str, Any]:
        """连接相机并在成功后广播 ``camera_connected`` 状态。"""
        info = self.camera_adapter.connect(device_index=device_index, device_label=device_label)
        self.signal_status_changed.emit("camera_connected", info)
        return info

    def disconnect_camera(self) -> None:
        """断开相机并广播 ``camera_disconnected`` 状态，GUI 据此切换按钮可用性。"""
        self.camera_adapter.disconnect()
        self.signal_status_changed.emit("camera_disconnected", {})

    def camera_connection_info(self) -> dict[str, Any]:
        return self.camera_adapter.connection_info()

    def arm_camera(self, frame_count: int = 9) -> None:
        """把相机切到 ``armed`` 状态；正式 SIM9 采集前必须为 9 帧。"""
        self.camera_adapter.arm(frame_count=frame_count)
        self.signal_status_changed.emit("camera_armed", {"frame_count": frame_count})

    def disarm_camera(self) -> None:
        """从 ``armed`` 状态退出；GUI 调试或采集结束后调用。"""
        self.camera_adapter.disarm()
        self.signal_status_changed.emit("camera_disarmed", {})

    def refresh_available_daq_devices(self) -> list[str]:
        """刷新 DAQ 设备下拉：返回 NI MAX 中识别到的设备名列表。"""
        return self.daq_adapter.list_devices(default_device=self.daq_config.device_name)

    def refresh_available_lines(self, device_name: str | None = None) -> list[str]:
        """刷新某 DAQ 设备 port0 的 16 条 line 名，GUI 用于线位下拉的候选项。"""
        selected_device = device_name or self.daq_config.device_name
        return self.daq_adapter.list_port0_lines(device_name=selected_device, default_device=self.daq_config.device_name)

    def apply_daq_config(self, config: DaqLineConfig) -> None:
        """校验并保存 DAQ 线位配置，广播 ``daq_config_applied``。

        副作用：
            ``validate_daq_line_config`` 失败时直接抛 ValueError，GUI 应当捕获并提示用户。
        """
        validate_daq_line_config(config)
        self.daq_config = config
        self.signal_status_changed.emit("daq_config_applied", {"device_name": config.device_name})

    def apply_camera_config(self, config: CameraConfig) -> dict[str, Any]:
        """把当前 UI 相机参数下发到 adapter，并回传"实际生效"的配置 dict。

        副作用：
            - 调 ``camera.apply_config`` 触发 DCAM 写属性，可能阻塞数十 ms。
            - 把 adapter 回报的 timing 缓存到 ``_latest_camera_timing``，供下次启动 worker 计算有效帧间隔。
        """
        # 1) 触发模式限制：项目目前只支持 external_level。
        if config.trigger_mode != "external_level":
            raise HardwareError("Only external_level trigger mode is supported.")
        # 2) 缓存配置 → 下发 → 回写 → 广播。
        self.camera_config = config
        result = self.camera_adapter.apply_config(config) or {}
        _apply_camera_result_to_config(config, result)
        self._latest_camera_timing = dict(result)
        payload = {"camera_config": dict(config.__dict__), **dict(result)}
        self.signal_status_changed.emit("camera_config_applied", payload)
        return dict(result)

    def refresh_available_slm_devices(self) -> list[dict[str, str]]:
        """刷新 SLM 设备下拉：返回 WinUSB 总线上可见的 R11 设备列表。"""
        return self.slm_adapter.list_devices()

    def connect_slm(self, device_path: str | None = None) -> dict[str, Any]:
        """连接 SLM；成功后广播 ``slm_connected`` 状态。"""
        info = self.slm_adapter.connect(device_path=device_path)
        self.signal_status_changed.emit("slm_connected", info)
        return info

    def disconnect_slm(self) -> None:
        """断开 SLM；GUI 据此切按钮颜色 + 阻止后续 RO 选择。"""
        self.slm_adapter.disconnect()
        self.signal_status_changed.emit("slm_disconnected", {})

    def slm_connection_info(self) -> dict[str, Any]:
        return self.slm_adapter.connection_info()

    def connect_stage(self) -> dict[str, Any]:
        """Connect the Nikon Ti2 ZDrive adapter and broadcast the current position."""
        info = self.stage_adapter.connect()
        self.signal_status_changed.emit("z_stage_connected", dict(info or {}))
        return info

    def disconnect_stage(self) -> None:
        self.stage_adapter.disconnect()
        self.signal_status_changed.emit("z_stage_disconnected", {})

    def get_stage_position_um(self) -> float:
        return float(self.stage_adapter.get_position_um())

    def move_stage_to_um(self, target_um: float) -> None:
        self.stage_adapter.move_z_um(float(target_um))
        self.signal_status_changed.emit("z_stage_moved", {"z_um": float(target_um)})

    def prepare_patterns(self, pattern_files: list[str], device_path: str | None = None) -> PatternPreparationResult:
        """把文件型 9 帧 pattern 编程到 SLM；正式采集应优先用 Running Order 路径。"""
        # 1) 调 adapter.program_patterns，得到包含 handles 的 PatternPreparationResult。
        self.pattern_result = self.slm_adapter.program_patterns(pattern_files, device_path=device_path)
        # 2) 广播状态，便于 GUI 显示 handle 数与准备时间。
        self.signal_status_changed.emit(
            "patterns_prepared",
            {
                "handles": list(self.pattern_result.handles),
                "prepared_at": self.pattern_result.prepared_at,
            },
        )
        return self.pattern_result

    def select_running_order_for_task(self, wavelength_nm: int, exposure_us: int) -> dict[str, Any]:
        """按任务波长 + 曝光自动选择预烧录 Running Order 并写入 controller。

        抛出：
            ``HardwareError``：没匹配到 RO 或 SLM 未连接时；GUI 据此弹错误对话框。
        """
        # 1) 枚举 SLM 上烧录的 RO 列表 → 调命名解析器找出最佳匹配。
        running_orders = self.slm_adapter.list_running_orders()
        ro_index, ro_name, warnings = find_best_running_order(
            running_orders,
            wavelength_nm=int(wavelength_nm),
            exposure_us=int(exposure_us),
        )
        if ro_index is None:
            raise HardwareError("; ".join(warnings) or "No matching SLM running order found.")
        # 2) 把选中的 RO 写到 SLM；返回值可能包含 PatternPreparationResult 或仅元数据。
        result = self.slm_adapter.select_running_order(ro_index)
        # 3) 规整成统一 PatternPreparationResult；下游 acquisition_core 才能消费。
        pattern_result = _coerce_running_order_pattern_result(result, ro_index, ro_name)
        self.pattern_result = pattern_result
        # 4) 打包 GUI 显示用 payload 并广播。
        payload = _running_order_payload(result, self.pattern_result, ro_index, ro_name, warnings)
        self.signal_status_changed.emit("running_order_selected", payload)
        return payload

    def start_single_acquisition(
        self,
        task: SimTaskConfig,
        *,
        prepare_running_order: bool = False,
        initialize_hardware: bool = False,
        apply_daq_config: bool = False,
        apply_camera_config: bool = False,
        z_scan_config: ZScanConfig | None = None,
        z_scan_enabled: bool | None = None,
    ) -> str:
        """启动单次 SIM9 正式采集；返回新分配的 ``task_id``。"""
        return self._start_worker_task(
            task,
            prepare_running_order=prepare_running_order,
            initialize_hardware=initialize_hardware,
            apply_daq_config=apply_daq_config,
            apply_camera_config=apply_camera_config,
            prepare_only=False,
            z_scan_config=z_scan_config,
            z_scan_enabled=z_scan_enabled,
        )

    def start_prepare_experiment(
        self,
        task: SimTaskConfig,
        *,
        prepare_running_order: bool = True,
        initialize_hardware: bool = True,
        apply_daq_config: bool = True,
        apply_camera_config: bool = True,
    ) -> str:
        """启动"准备实验"路径：默认开启全部预备阶段，不真正触发 9 帧采集。"""
        return self._start_worker_task(
            task,
            prepare_running_order=prepare_running_order,
            initialize_hardware=initialize_hardware,
            apply_daq_config=apply_daq_config,
            apply_camera_config=apply_camera_config,
            prepare_only=True,
        )

    def _start_worker_task(
        self,
        task: SimTaskConfig,
        *,
        prepare_running_order: bool,
        initialize_hardware: bool,
        apply_daq_config: bool,
        apply_camera_config: bool,
        prepare_only: bool,
        z_scan_config: ZScanConfig | None = None,
        z_scan_enabled: bool | None = None,
    ) -> str:
        """``start_*`` 系列的统一实现：组装 payload、做配置校验、生成 task_id、投递信号。"""
        # 1) 防御：非 RO 路径要求 controller.pattern_result.handles 已就绪。
        if not prepare_running_order and not self.pattern_result.handles:
            raise HardwareError("Patterns must be prepared before acquisition.")
        # 2) 用最近一次相机回报刷新有效帧间隔；保证 GUI 与 worker 用同一规则。
        task.timing.inter_frame_gap_us = effective_inter_frame_gap_us(
            self._latest_camera_timing.get("recommended_inter_frame_gap_us")
        )
        selected_z_scan_config = z_scan_config or self.z_scan_config
        selected_z_scan_enabled = (
            bool(selected_z_scan_config.enabled) if z_scan_enabled is None else bool(z_scan_enabled)
        )
        if prepare_only:
            selected_z_scan_enabled = False
        # 3) 选定的 pattern_result：RO 路径稍后由 worker 重新生成；非 RO 路径用 controller 持有的。
        pattern_result = self.pattern_result if not prepare_running_order else PatternPreparationResult()
        pattern_files = list(task.pattern_files)
        selected_running_order = task.running_order_name
        # 3a) 如果 controller 现持有的就是 RO 模式 pattern_result，则把它的 RO 名/pattern 同步到 task。
        if pattern_result.metadata.get("mode") == "running_order":
            selected_running_order = selected_running_order or str(
                pattern_result.metadata.get("running_order_name", "")
            )
            pattern_files = list(pattern_result.pattern_files)
            task.running_order_name = selected_running_order
        # 4) 把所有相关字段拼成 AppConfig，并跑通用配置校验；任何失败立即拒绝启动。
        validation_config = AppConfig(
            daq=self.daq_config,
            camera=task.camera,
            timing=task.timing,
            backend=self.backend,
            z_scan=selected_z_scan_config,
            reconstruction=self.reconstruction_config,
            pattern_files=pattern_files,
            selected_running_order=selected_running_order,
            selected_laser_nm=task.laser_wavelength_nm,
        )
        validation_errors = validate_app_config(validation_config)
        if validation_errors:
            raise ValueError("Invalid SIM acquisition config: " + "; ".join(validation_errors))
        # 5) 生成 task_id 与 stop_event，把 stop_event 写入两个簿记字段。
        task_id = new_task_id()
        stop_event = threading.Event()
        self._active_stop_events[task_id] = stop_event
        self._current_stop_event = stop_event
        # 6) 组装 payload；adapter 引用从 controller 自身取，保证 SLM/相机共享同一连接。
        payload = {
            "task_id": task_id,
            "task": task,
            "daq_config": self.daq_config,
            "pattern_result": pattern_result,
            "waveform_builder": self.waveform_builder,
            "daq_adapter": self.daq_adapter,
            "camera_adapter": self.camera_adapter,
            "slm_adapter": self.slm_adapter,
            "stage_adapter": self.stage_adapter,
            "z_scan_config": selected_z_scan_config,
            "z_scan_enabled": selected_z_scan_enabled,
            "stop_event": stop_event,
            "prepare_running_order": bool(prepare_running_order),
            "initialize_hardware": bool(initialize_hardware),
            "apply_daq_config": bool(apply_daq_config),
            "apply_camera_config": bool(apply_camera_config),
            "prepare_only": bool(prepare_only),
        }
        # 7) 通过跨线程信号投递给 worker；本函数立即返回，GUI 不会被阻塞。
        self.signal_start_worker.emit(payload)
        return task_id

    def stop(self) -> None:
        """取消当前 / 所有活跃 worker；置位 stop_event 让采集核心尽快退出。"""
        # 1) 当前 task 优先：先置位最近一次的 stop_event，提示 acquisition_core 立即取消。
        if self._current_stop_event is not None:
            self._current_stop_event.set()
        # 2) 兜底：遍历所有活跃 stop_event 全部置位（防御多 task 排队的情况）。
        for stop_event in list(self._active_stop_events.values()):
            stop_event.set()
        self.signal_status_changed.emit("stop_requested", {})

    def _clear_stop_event_for_summary(self, payload: dict[str, Any]) -> None:
        """signal_acquisition_summary_ready 的清理回调；避免 controller 接收 raw stack。"""
        task_id = str((payload or {}).get("task_id", ""))
        self._clear_stop_event_for_task(task_id, "")

    def _clear_stop_event_for_task(self, task_id: str, _message: str = "") -> None:
        """从 ``_active_stop_events`` 字典里移除已完成的 task；并在字典空时清 _current。"""
        # 1) 已知 task_id 直接 pop；缺失也安全。
        if task_id:
            self._active_stop_events.pop(str(task_id), None)
        # 2) 字典空 → 没有 active task；清 ``_current_stop_event`` 防止误用。
        if not self._active_stop_events:
            self._current_stop_event = None

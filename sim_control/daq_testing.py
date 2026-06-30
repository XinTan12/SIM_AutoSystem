"""DAQ 诊断测试的可复用核心（与 QDialog 解耦）。

作用：
    把原 ``SimSettingsDialog`` 内的 DAQ 测试逻辑抽到 :class:`DaqTestRunner`，使
    其不再依赖任何 Qt 对话框，可供以下三方复用：
        - ``control_wangbo`` 主界面的一级 DAQ 模块（经 :class:`_PulseTestWorker`
          在后台线程调用，详见 ``control_wangbo/main.py``）；
        - ``sim_control.gui.SimControlWindow``（独立 SIM 调试 GUI，仍把
          :class:`_PulseTestWorker` 当通用后台 worker 用）；
        - 回归测试（直接构造 runner 或以 host stub 调未绑定方法）。

线程契约（B5）：
    - runner 由调用方在 GUI 线程构造，注入**共享 adapter**（与 controller 同一
      ``slm_adapter``，避免重复打开 R11 WinUSB 设备）与**配置快照**；worker 线程内
      只读 ``self.config`` 等普通对象，**绝不触碰 Qt 控件**。
    - 每次测试持调用方传入的 ``stop_event``，分片可取消。
    - 所有 成功/异常/取消 路径都在 ``finally`` 执行相机收尾 + ``daq.set_all_low``。
    - SIM 采集 / SLM 激活时序测试选 RO 时复用正式采集的过滤语义：传入
      ``immediate_ro_indices`` 后经 ``exclude_indices`` 显式排除 ``ACT_IMMEDIATE`` RO。

注意：
    ``sim_control/gui.py`` 从本模块重导出 :class:`_PulseTestWorker`、结果 dataclass、
    测试常量与 :func:`build_daq_test_target_items`，以保持 ``SimControlWindow`` 与既有
    ``from sim_control.gui import ...`` 调用点不破（决策：删弹窗但不删测试逻辑）。
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import tifffile

from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot

from .adapters import HardwareError, R11_ACTIVATION_STATE_ACTIVE, find_best_running_order
from .config_store import app_config_from_dict, app_config_to_dict
from .models import AppConfig, CameraConfig, DaqLineConfig
from .waveform import NIDaqWaveformBuilder, parse_line_name

logger = logging.getLogger(__name__)


# 8 个 DAQ 角色的可读标签；GUI 错误提示、测试下拉等地方使用。
ROLE_LABELS = {
    "slm_enable_line": "SLM Enable",
    "slm_trigger_line": "SLM Trigger",
    "slm_finish_line": "SLM Finish",
    "camera_trigger_line": "Camera Trigger",
    "laser_405_line": "Laser 405",
    "laser_488_line": "Laser 488",
    "laser_561_line": "Laser 561",
    # 第四路红光（638 或 647）共用中性线 ``laser_red_line``；标签不写死具体波长。
    "laser_red_line": "Laser red",
}

# "SIM 采集测试"的下拉项目 ID 常量。
SIM_ACQUISITION_TEST_ID = "sim_acquisition"
# SIM 采集测试默认参数：500ms 曝光 + 50ms 帧间隔，便于真机调试时观察是否触发。
SIM_ACQUISITION_TEST_EXPOSURE_US = 500_000
SIM_ACQUISITION_TEST_INTER_FRAME_GAP_US = 50_000
# "SLM 激活时序测试"下拉项目 ID：DAQ 拉高 slm_enable 后轮询 R11 激活状态，
# 实测 EXT_RUN -> ACT 的延迟上界（数据手册 tHWAT 规格为 5~500 µs）。
SLM_ACTIVATION_TIMING_TEST_ID = "slm_activation_timing"
# 激活轮询超时：tHWAT 上限 500 µs + USB 轮询粒度（毫秒级），2 秒足够分辨异常。
SLM_ACTIVATION_TIMING_TIMEOUT_S = 2.0
# 测试下拉中可单独发短脉冲的 4 个激光角色（不含 SLM enable/trigger/finish 与相机触发线，避免误触发）。
DAQ_PULSE_TEST_ROLES = (
    "laser_405_line",
    "laser_488_line",
    "laser_561_line",
    "laser_red_line",
)
# 测试采集的 TIFF 保存目录；按子目录分类相机/SIM9 采集，便于事后审阅。
TEST_CAPTURE_ROOT = Path(__file__).resolve().parent.parent / "data" / "test_captures"


@dataclass(frozen=True)
class SimAcquisitionTestResult:
    """``DaqTestRunner._run_sim_acquisition_test`` 的返回结构。

    职责：
        - ``output_path``：保存到磁盘的 9 帧 uint16 TIFF 路径。
        - ``actual_acquisition_duration_s``：从发起测试到 9 帧 stack 入内存的实际耗时（秒），
          **不含**写盘开销，便于真机评估端到端时延。
        - ``daq_waveform_duration_s``：DAQ 完整波形播放时长（秒）。
    """

    output_path: Path
    actual_acquisition_duration_s: float
    daq_waveform_duration_s: float


@dataclass(frozen=True)
class SlmActivationTimingTestResult:
    """``DaqTestRunner._run_slm_activation_timing_test`` 的返回结构。

    职责：
        - ``running_order_name``：本次测试选中的 RO 名。
        - ``initial_state``：软件 activate 后、slm_enable 拉高前的状态 ``{"code","name"}``。
        - ``transitions``：``(elapsed_ms, state)`` 列表，记录 enable 拉高后观察到的状态变迁。
        - ``reached_active`` / ``enable_to_active_ms``：是否达到 0x56 ACT 及对应耗时；
          受 USB 轮询粒度限制，该耗时是真实 tHWAT 的**上界**。
        - ``poll_count``：总轮询次数，用于评估轮询粒度。
    """

    running_order_name: str
    initial_state: dict[str, Any]
    transitions: list[tuple[float, dict[str, Any]]]
    reached_active: bool
    enable_to_active_ms: float | None
    poll_count: int


def _clone_app_config(config: AppConfig) -> AppConfig:
    """通过 dict 中转深拷贝 ``AppConfig``，与 ``gui.clone_app_config`` 等价。

    用途：
        runner 在使用相机/时序等子配置前复制一份，避免就地修改污染调用方持有的快照。
    """
    return app_config_from_dict(app_config_to_dict(config))


def build_daq_test_target_items(daq_config: DaqLineConfig) -> list[tuple[str, str]]:
    """根据当前 DAQ 配置生成"测试目标"下拉项目列表。

    返回：
        ``[(target_id, display_text), ...]``：4 路激光单线脉冲 + 1 路 SIM9 完整测试 + 1 路 SLM 激活时序。
    """
    # 1) 4 路激光角色：显示"Laser xxx -> 线名"。
    items = [
        (role, f"{ROLE_LABELS[role]} -> {getattr(daq_config, role)}")
        for role in DAQ_PULSE_TEST_ROLES
    ]
    # 2) 末尾追加完整 SIM9 测试项；ID 用 ``SIM_ACQUISITION_TEST_ID`` 常量。
    items.append((SIM_ACQUISITION_TEST_ID, "SIM采集"))
    # 3) SLM 激活时序诊断：实测 slm_enable(EXT_RUN) 拉高 -> RO ACT 的延迟上界。
    items.append((SLM_ACTIVATION_TIMING_TEST_ID, "SLM激活时序"))
    return items


class _PulseTestWorker(QObject):
    """一次性诊断测试 worker；在独立 QThread 中执行，通过信号把结果回传 GUI。

    线程模型：
        - ``__init__`` 在 GUI 线程构造；``moveToThread`` 后 ``run`` 在工作线程执行。
        - ``cancel()`` 线程安全：置位 ``_stop_event``，fn 在下一个检查点退出。
        - fn 接受 ``threading.Event`` 参数，fn 内各测试方法的 finally 块负责 DAQ 全低收尾。
    """

    signal_success = pyqtSignal(str)   # 测试通过；payload 为显示消息
    signal_error = pyqtSignal(str)     # 失败；payload 为错误描述（取消时不发出）
    signal_finished = pyqtSignal()     # 成功 / 失败 / 取消三条路径均发出

    def __init__(self, fn, stop_event: threading.Event, parent=None):
        super().__init__(parent)
        self._fn = fn                  # Callable[[threading.Event], str]
        self._stop_event = stop_event

    def cancel(self) -> None:
        self._stop_event.set()

    @pyqtSlot()
    def run(self) -> None:
        try:
            if self._stop_event.is_set():
                return
            message = self._fn(self._stop_event)
            if not self._stop_event.is_set():
                self.signal_success.emit(message)
        except Exception as exc:
            if not self._stop_event.is_set():
                self.signal_error.emit(str(exc))
            else:
                logger.debug("Test exception during cancellation (suppressed): %s", exc)
        finally:
            self.signal_finished.emit()


class DaqTestRunner:
    """脱离 QDialog 的 DAQ 诊断测试执行器（线程契约见模块 docstring）。

    构造参数：
        camera_adapter / slm_adapter / daq_adapter：与 controller 共享的硬件适配器。
        config：``AppConfig`` 配置快照（调用方在 GUI 线程克隆后传入）。
        immediate_ro_indices：``ACT_IMMEDIATE`` RO 索引集合（来自 controller 连接后扫描）；
            SIM 采集 / SLM 激活时序测试选 RO 时经 ``exclude_indices`` 排除，避免误选 immediate RO。
        camera_externally_owned：相机是否由外部（主界面 / controller）持有；True 时测试结束
            只 disarm 不 disconnect，把相机留给主界面继续使用。
        selected_laser_nm：可选的采集波长快照；缺省时回退 ``config.selected_laser_nm``。
    """

    def __init__(
        self,
        camera_adapter,
        slm_adapter,
        daq_adapter,
        config: AppConfig,
        *,
        immediate_ro_indices: set[int] | None = None,
        camera_externally_owned: bool = True,
        selected_laser_nm: int | None = None,
    ) -> None:
        self.camera_adapter = camera_adapter
        self.slm_adapter = slm_adapter
        self.daq_adapter = daq_adapter
        self.config = config
        self.immediate_ro_indices = {int(i) for i in immediate_ro_indices} if immediate_ro_indices else set()
        self._camera_externally_owned = bool(camera_externally_owned)
        self._selected_laser_nm_snapshot = selected_laser_nm
        self._loaded_pattern_result = None

    # ------------------------------------------------------------------ helpers
    def _selected_laser_nm(self) -> int:
        if self._selected_laser_nm_snapshot is not None:
            return int(self._selected_laser_nm_snapshot)
        return int(self.config.selected_laser_nm)

    def _test_capture_path(self, subdir: str, prefix: str) -> Path:
        """生成测试采集的 TIFF 保存路径：``TEST_CAPTURE_ROOT/subdir/prefix_<ts>.tiff``。"""
        target_dir = TEST_CAPTURE_ROOT / subdir
        target_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return target_dir / f"{prefix}_{timestamp}.tiff"

    def _write_uint16_tiff(self, path: Path, image_data: np.ndarray) -> None:
        """把 uint16 numpy 数组写成 16 位 TIFF。"""
        tifffile.imwrite(path, np.asarray(image_data, dtype=np.uint16))

    def _camera_connected_for_test_cleanup(self) -> bool:
        """判断测试前相机是否已连接；用于清理路径决定是否保留连接。"""
        try:
            return bool(self.camera_adapter.is_connected())
        except Exception:
            return False

    def _cleanup_test_camera(self, was_connected_before_test: bool) -> None:
        """测试结束后收尾相机：始终 disarm，仅在适当条件下 disconnect。

        策略：
            - 始终 disarm，避免 snapshot 状态遗留。
            - 共享相机 + 测试前已连接 → 不要 disconnect，让主界面继续使用。
            - 否则（独立持有 / 本次测试中才连接） → 测试结束顺手 disconnect。
        """
        try:
            self.camera_adapter.disarm()
        except Exception:
            logger.warning("Test camera teardown step failed; continuing.", exc_info=True)
        if self._camera_externally_owned and was_connected_before_test:
            return
        try:
            self.camera_adapter.disconnect()
        except Exception:
            logger.warning("Test camera teardown step failed; continuing.", exc_info=True)

    def _execute_sim_capture_sequence(
        self,
        daq_config: DaqLineConfig,
        camera_config: CameraConfig,
        plan,
        frame_count: int = 9,
        stop_event: threading.Event | None = None,
    ) -> tuple[np.ndarray, list[float]]:
        """arm → activate SLM RO → play DAQ waveform → read frames.

        Shared by the SIM acquisition test and future production paths.
        Caller is responsible for cleanup (disarm, DAQ all-low).
        """
        self.camera_adapter.apply_config(camera_config)
        self.camera_adapter.arm(frame_count=frame_count)
        self.slm_adapter.activate_prepared_patterns()
        self.daq_adapter.play_waveform(daq_config.device_name, plan, stop_event=stop_event)
        return self.camera_adapter.read_frame_sequence(
            frame_count=frame_count,
            pattern_files=list(self._loaded_pattern_result.pattern_files),
            laser_wavelength_nm=self.config.selected_laser_nm,
            stop_event=stop_event,
        )

    # ------------------------------------------------------------------- tests
    def _run_laser_pulse_test(
        self,
        daq_config: DaqLineConfig,
        target_role: str,
        stop_event: threading.Event | None = None,
    ) -> None:
        """对选中的激光 TTL 线输出 1 秒短脉冲，用于接线确认。"""
        line_name = getattr(daq_config, target_role)
        device_name, _, line_index = parse_line_name(line_name)
        # 已请求取消则直接跳过脉冲；脉冲期间的取消由 pulse_line 分片 sleep 响应（提前拉低）。
        if stop_event is not None and stop_event.is_set():
            return
        try:
            # 默认 1 秒脉冲让肉眼足以看到激光器响应；DAQ adapter 内部把上限夹到 100 ms（仿真路径）或长脉冲（真实路径）。
            self.daq_adapter.pulse_line(device_name, line_index, duration_s=1.0, stop_event=stop_event)
        finally:
            # B5：runner 层全路径安全归位（即便底层 pulse_line 已自带脉冲后拉低，这里再 set_all_low 双保险）。
            try:
                self.daq_adapter.set_all_low(device_name)
            except Exception:
                logger.warning("SIM teardown step failed; continuing cleanup.", exc_info=True)

    def _run_sim_acquisition_test(
        self,
        daq_config: DaqLineConfig,
        acquisition_started_at_s: float | None = None,
        stop_event: threading.Event | None = None,
        selected_laser_nm: int | None = None,
    ) -> SimAcquisitionTestResult:
        """按当前波长选 RO，执行一次 SIM9 测试采集并保存 stack 到 TIFF。"""
        # 1) 计时起点：用 ``time.perf_counter`` 精确测量端到端耗时。
        if acquisition_started_at_s is None:
            acquisition_started_at_s = time.perf_counter()
        # 2) 必须先连接 SLM；否则没有 RO 列表可挑。
        if not self.slm_adapter.is_connected():
            raise HardwareError("SIM采集测试前需要先连接 SLM。")
        # 3) 使用调用方快照的波长（worker 场景），或从 runner 快照读取。
        laser_nm = selected_laser_nm if selected_laser_nm is not None else self._selected_laser_nm()
        camera_config = _clone_app_config(self.config).camera
        # 4) 测试用固定曝光 500 ms / 帧间 50 ms，与 SLM RO 桶（≥50 ms）对齐。
        camera_config.exposure_us = SIM_ACQUISITION_TEST_EXPOSURE_US
        timing_config = _clone_app_config(self.config).timing
        timing_config.inter_frame_gap_us = SIM_ACQUISITION_TEST_INTER_FRAME_GAP_US
        # 5) 列举 + 选择 RO；排除 ACT_IMMEDIATE RO，复用正式采集过滤语义（B5）。
        running_orders = self.slm_adapter.list_running_orders()
        ro_index, ro_name, warnings = find_best_running_order(
            running_orders,
            wavelength_nm=laser_nm,
            exposure_us=camera_config.exposure_us,
            exclude_indices=self.immediate_ro_indices or None,
        )
        if ro_index is None:
            raise HardwareError("; ".join(warnings) or "未找到匹配的 SLM Running Order。")
        result = self.slm_adapter.select_running_order(ro_index)
        self._loaded_pattern_result = result["pattern_result"]
        self.config.selected_running_order = ro_name

        # 6) 准备输出路径。
        exposure_ms = max(1, int(round(float(camera_config.exposure_us) / 1000.0)))
        output_path = self._test_capture_path(
            "sim_acquisition",
            f"sim_acquisition_{laser_nm}nm_{exposure_ms}ms",
        )
        # 7) 构建波形 plan：与正式采集走同一段代码路径，确保测试与正式一致。
        waveform_builder = NIDaqWaveformBuilder()
        plan = waveform_builder.build(
            daq_config=daq_config,
            timing=timing_config,
            laser_wavelength_nm=laser_nm,
            exposure_us=camera_config.exposure_us,
            frame_count=9,
            include_role_matrix=False,
        )
        was_camera_connected = self._camera_connected_for_test_cleanup()
        try:
            # 8) arm → 激活 SLM RO → 播放 DAQ → 读 9 帧 stack（共享 helper，与正式采集路径一致）。
            stack, _timestamps = self._execute_sim_capture_sequence(
                daq_config=daq_config,
                camera_config=camera_config,
                plan=plan,
                frame_count=9,
                stop_event=stop_event,
            )
            # 9) 计算实际采集耗时 + 写 TIFF；写盘**不**计入 actual_duration。
            actual_duration_s = max(0.0, time.perf_counter() - float(acquisition_started_at_s))
            self._write_uint16_tiff(output_path, stack)
            return SimAcquisitionTestResult(
                output_path=output_path,
                actual_acquisition_duration_s=actual_duration_s,
                daq_waveform_duration_s=float(plan.duration_s),
            )
        finally:
            # 10) 相机 cleanup + DAQ 全 0（与其它测试 finally 收尾一致）。
            self._cleanup_test_camera(was_camera_connected)
            try:
                self.daq_adapter.set_all_low(daq_config.device_name)
            except Exception:
                logger.warning("SIM teardown step failed; continuing cleanup.", exc_info=True)

    def _run_slm_activation_timing_test(
        self,
        daq_config: DaqLineConfig,
        stop_event: threading.Event | None = None,
        selected_laser_nm: int | None = None,
    ) -> SlmActivationTimingTestResult:
        """实测 slm_enable(EXT_RUN) 拉高 -> RO 进入 ACT 的延迟上界。

        流程：
            1. 按当前波长选 RO 并软件 activate（与 SIM 采集测试一致）。
            2. 读初始激活状态：[HWA h] RO 预期为 0x54 MHW（等待 EXT_RUN）。
            3. ``set_line`` 拉高 slm_enable，``perf_counter`` 轮询激活状态直至
               0x56 ACT 或超时，记录每次状态变迁。
            4. finally：slm_enable 拉低 + DAQ 全 0，硬件安全归位。

        注意：
            USB 轮询单次往返为毫秒级，因此测得的 enable->ACT 耗时是真实 tHWAT
            （规格 5~500 µs）的上界；用于确认门控机理与排查异常，不用于精确计时。
        """
        # 1) 必须先连接 SLM；选 RO 沿用 SIM 采集测试的波长 + 500ms 曝光桶。
        if not self.slm_adapter.is_connected():
            raise HardwareError("SLM激活时序测试前需要先连接 SLM。")
        laser_nm = selected_laser_nm if selected_laser_nm is not None else self._selected_laser_nm()
        running_orders = self.slm_adapter.list_running_orders()
        ro_index, ro_name, ro_warnings = find_best_running_order(
            running_orders,
            wavelength_nm=laser_nm,
            exposure_us=SIM_ACQUISITION_TEST_EXPOSURE_US,
            exclude_indices=self.immediate_ro_indices or None,
        )
        if ro_index is None:
            raise HardwareError("; ".join(ro_warnings) or "未找到匹配的 SLM Running Order。")
        self.slm_adapter.select_running_order(int(ro_index))
        self.slm_adapter.activate_prepared_patterns()
        _, _, enable_line_index = parse_line_name(daq_config.slm_enable_line)
        # 2) 软件 activate 后、enable 拉高前的基线状态。
        initial_state = self.slm_adapter.get_running_order_activation_state()
        # 2a) 旧版 R11CommLib 缺 GetActivationState 时直接拒绝：后续轮询永远拿不到
        #     ACT，只会变成 2 秒 GUI 线程纯自旋；提示用户升级 SDK。
        if initial_state.get("code") is None:
            raise HardwareError(
                "当前 R11CommLib 不支持 R11_RpcRoGetActivationState，无法执行激活时序测试；"
                "请升级 R11CommLib（>= 1.8）后重试。"
            )
        transitions: list[tuple[float, dict[str, Any]]] = []
        reached_active = False
        enable_to_active_ms: float | None = None
        poll_count = 0
        try:
            # 3) 拉高 enable 并尽快开始轮询；不 sleep，轮询间隔即 USB 往返时间。
            started_at = time.perf_counter()
            self.daq_adapter.set_line(daq_config.device_name, enable_line_index, high=True)
            last_code = initial_state.get("code")
            while True:
                state = self.slm_adapter.get_running_order_activation_state()
                poll_count += 1
                elapsed_ms = (time.perf_counter() - started_at) * 1000.0
                if state.get("code") != last_code:
                    transitions.append((elapsed_ms, state))
                    last_code = state.get("code")
                if state.get("code") == R11_ACTIVATION_STATE_ACTIVE:
                    reached_active = True
                    enable_to_active_ms = elapsed_ms
                    break
                if stop_event is not None and stop_event.is_set():
                    break
                if elapsed_ms >= SLM_ACTIVATION_TIMING_TIMEOUT_S * 1000.0:
                    break
        finally:
            # 4) 不论成败：enable 拉低 + DAQ 全 0，避免 SLM 停在 Active Mode。
            try:
                self.daq_adapter.set_line(daq_config.device_name, enable_line_index, high=False)
            except Exception:
                logger.warning("SIM teardown step failed; continuing cleanup.", exc_info=True)
            try:
                self.daq_adapter.set_all_low(daq_config.device_name)
            except Exception:
                logger.warning("SIM teardown step failed; continuing cleanup.", exc_info=True)
        return SlmActivationTimingTestResult(
            running_order_name=str(ro_name),
            initial_state=initial_state,
            transitions=transitions,
            reached_active=reached_active,
            enable_to_active_ms=enable_to_active_ms,
            poll_count=poll_count,
        )

    # ------------------------------------------------------------------ dispatch
    def run_test(
        self,
        target_id: str,
        daq_config: DaqLineConfig,
        *,
        selected_laser_nm: int,
        stop_event: threading.Event,
        acquisition_started_at_s: float | None = None,
    ) -> str:
        """按下拉选中的 ``target_id`` 分派到具体测试，返回供 GUI 显示的结果消息。

        本方法在后台 worker 线程执行（经 :class:`_PulseTestWorker`）；``daq_config`` 与
        ``selected_laser_nm`` 由调用方在 GUI 线程快照后传入，方法内不读任何 Qt 控件。
        """
        if target_id == SIM_ACQUISITION_TEST_ID:
            result = self._run_sim_acquisition_test(
                daq_config,
                acquisition_started_at_s=acquisition_started_at_s,
                stop_event=stop_event,
                selected_laser_nm=selected_laser_nm,
            )
            return (
                "SIM采集测试完成，16位 TIFF 已保存到:\n"
                f"{result.output_path}\n"
                f"SIM采集实际用时: {result.actual_acquisition_duration_s * 1000.0:.3f} ms\n"
                f"DAQ完整播放时长: {result.daq_waveform_duration_s * 1000.0:.3f} ms"
            )
        if target_id == SLM_ACTIVATION_TIMING_TEST_ID:
            timing_result = self._run_slm_activation_timing_test(
                daq_config,
                stop_event=stop_event,
                selected_laser_nm=selected_laser_nm,
            )
            lines = [
                f"Running Order: {timing_result.running_order_name}",
                f"软件激活后初始状态: {timing_result.initial_state.get('name')}",
            ]
            for elapsed_ms, state in timing_result.transitions:
                lines.append(f"+{elapsed_ms:.3f} ms -> {state.get('name')}")
            if timing_result.reached_active and timing_result.enable_to_active_ms is not None:
                lines.append(
                    f"slm_enable 拉高 -> ACT(active) 用时: {timing_result.enable_to_active_ms:.3f} ms"
                    f"（USB 轮询粒度上界，共 {timing_result.poll_count} 次轮询；"
                    "数据手册 tHWAT 规格 5~500 µs）"
                )
            else:
                lines.append(
                    f"超时 {SLM_ACTIVATION_TIMING_TIMEOUT_S:.1f}s 未达到 ACT(active)；"
                    "请检查 slm_enable(EXT_RUN) 接线、RO 激活方式与 R11CommLib 版本。"
                )
            return "SLM激活时序测试完成:\n" + "\n".join(lines)
        # SIM采集/SLM激活时序已在前面 return；此处只应是 4 路激光角色之一。显式守卫，
        # 杜绝已删的 camera_trigger_line 或任意未知 id 静默走 _run_laser_pulse_test。
        if target_id not in DAQ_PULSE_TEST_ROLES:
            raise ValueError(f"Unknown test target: {target_id!r}")
        self._run_laser_pulse_test(daq_config, str(target_id), stop_event=stop_event)
        return f"{ROLE_LABELS[str(target_id)]} 脉冲测试完成。"

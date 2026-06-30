"""集成主界面 SIM 预览重启与正式采集状态测试。

作用：
    SIM 系统最厚的测试集（2100+ 行），覆盖 ``control_wangbo/main.py`` 与
    ``sim_control/preview.py``、``sim_control/gui.py`` 在以下场景的不变量：
        1. live preview 停止 / 重启的状态机：``_active`` / ``_stopping`` flag、
           ``signal_status_changed("preview_started"/"preview_stopped")`` 的广播顺序、
           ``QTimer`` 轮询逻辑、worker 重入保护。
        2. ROI / 曝光 / bit_depth SpinBox 提交：用户改控件值时主界面应同步刷新
           SIM 摘要并把更新写回 ``AppConfig``；live preview 期间限制部分字段不可编辑。
        3. 正式采集前后状态恢复：发起 9 帧采集前必须先停 live preview，采集完成
           或失败后必须能恢复到之前的预览请求状态。
        4. DaqTestRunner DAQ 诊断测试执行：复用与 controller 共享的相机/SLM/DAQ 适配器，
           按角色分派 4 路激光单线脉冲/SIM9 采集/SLM 激活时序，并在 finally 收尾相机 + DAQ 全低。
        5. ``running_order_selected`` 状态被广播时主界面摘要刷新；``patterns_prepared``
           会更新 controller 内部 ``pattern_result`` 但不影响 GUI 预览。

协作关系：
    上游：``unittest``、``unittest.mock``、``numpy``、PyQt5 (QtCore/QtTest/QtWidgets)、
          ``importlib`` 用于动态导入 ``control_wangbo.main``。
    下游：``control_wangbo.main.MainWindow`` 与 helper 函数、``sim_control.preview``、
          ``sim_control.daq_testing``、``sim_control.controller``。

维护要点：
    - 本测试在 import 阶段就给 ``MCUTriggerThread`` / ``mvsdk`` / ``FastCameraThread``
      注入空 stub（参考 ``test_main_window_scroll_area.py``）；改动主界面 import
      路径时需要同步更新这些 stub。
    - 测试中 ``MainWindow`` 用真实 PyQt5 实例化但 controller / camera_adapter / slm_adapter
      通过 ``SimpleNamespace`` mock；可以放心地在无硬件环境下运行。
    - 几个类的测试范围：``SimPreviewRestartTests`` 覆盖 live preview 状态机；
      ``SimPreviewControllerTests`` 覆盖 ``preview.py``；``SimPreviewPollingTests``
      覆盖 GUI 端 latest-frame-wins 轮询；``SimCameraSpinBoxCommitTests`` 覆盖
      ROI/曝光控件提交时机；``DaqTestRunnerTests`` 覆盖 ``daq_testing.py`` 的
      DAQ 诊断测试执行。
"""

import importlib
import sys
import time
import types
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np
from PyQt5 import QtCore, QtTest, QtWidgets


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTROL_ROOT = PROJECT_ROOT / "control_wangbo"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(CONTROL_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_ROOT))

sys.modules.setdefault("MCUTriggerThread", types.ModuleType("MCUTriggerThread"))
sys.modules.setdefault("mvsdk", types.ModuleType("mvsdk"))
sys.modules.setdefault("FastCameraThread", types.ModuleType("FastCameraThread"))


def load_legacy_main_module():
    """为测试准备 load_legacy_main_module 所需的轻量对象、导入入口或断言辅助。"""
    return importlib.import_module("control_wangbo.main")


def _wait_for_condition(predicate, timeout_ms: int = 5000, interval_ms: int = 10) -> bool:
    """事件驱动等待：每 ``interval_ms`` 跑一轮 Qt 事件循环，``predicate`` 命中立即返回。

    用于等待 worker 线程经 queued signal 回 GUI 线程后的断言条件；替代固定时长
    ``QTest.qWait(N)``——慢速/高负载环境下不 flaky，条件命中后也不浪费等待时间。
    本机 PyQt5 没有 ``QTest.qWaitFor``，故自行实现。
    """
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if predicate():
            return True
        QtTest.QTest.qWait(interval_ms)
    return predicate()


class TimerSpy:
    """记录 QTimer.start/stop 调用，便于断言预览重启节流行为。"""
    def __init__(self):
        self.start_calls = []
        self.stop_calls = 0

    def start(self, interval_ms):
        self.start_calls.append(interval_ms)

    def stop(self):
        self.stop_calls += 1


class PreviewControllerSpy:
    """模拟预览控制器的 active/stopping 状态和 stop 调用。"""
    def __init__(self):
        self.stop_calls = []
        self.active = True
        self.stopping = False

    def stop(self, wait=False):
        self.stop_calls.append(wait)


class LabelSpy:
    """记录 QLabel.setText 的最后一次文本，用于状态和 FPS 断言。"""
    def __init__(self):
        self.text = None

    def setText(self, text):
        self.text = text


class ButtonSpy:
    """记录按钮 enabled、text、style 状态，替代真实 QPushButton。"""
    def __init__(self):
        self.enabled = None
        self.text = None
        self.style = None

    def setEnabled(self, enabled):
        self.enabled = enabled

    def setText(self, text):
        self.text = text

    def setStyleSheet(self, style):
        self.style = style


class SpinBoxSpy:
    """模拟 QSpinBox 的值、最大值和启用状态。"""
    def __init__(self, value=0, maximum=2304, enabled=True):
        self._value = value
        self._maximum = maximum
        self._enabled = enabled
        self.single_step = None

    def value(self):
        return self._value

    def blockSignals(self, blocked):
        return None

    def setMaximum(self, maximum):
        self._maximum = maximum

    def maximum(self):
        return self._maximum

    def setEnabled(self, enabled):
        self._enabled = enabled

    def isEnabled(self):
        return self._enabled

    def setValue(self, value):
        self._value = value

    def setSingleStep(self, value):
        self.single_step = value


class SignalBlockingSpinBoxSpy(SpinBoxSpy):
    """扩展 SpinBoxSpy，记录 blockSignals 与 setValue 时的阻塞状态。"""
    def __init__(self, value=0, maximum=2304, enabled=True):
        super().__init__(value=value, maximum=maximum, enabled=enabled)
        self._signals_blocked = False
        self.block_signals_calls = []
        self.set_value_blocked_states = []

    def blockSignals(self, blocked):
        self._signals_blocked = blocked
        self.block_signals_calls.append(blocked)

    def setValue(self, value):
        self.set_value_blocked_states.append(self._signals_blocked)
        super().setValue(value)


class InterpretTextSpinBoxSpy(SpinBoxSpy):
    def __init__(self, value=0, pending_value=None, maximum=2304, enabled=True):
        super().__init__(value=value, maximum=maximum, enabled=enabled)
        self.pending_value = pending_value
        self.interpret_calls = 0

    def interpretText(self):
        self.interpret_calls += 1
        if self.pending_value is not None:
            self._value = self.pending_value


class ComboBoxSpy:
    """模拟 QComboBox 的文本、索引、启用状态和条目列表。"""
    def __init__(self, text="", current_index=-1, enabled=True):
        self._text = text
        self._current_index = current_index
        self._enabled = enabled
        self.block_signals_calls = []
        self.items = []
        self.item_data = []

    def currentText(self):
        return self._text

    def setCurrentText(self, text):
        self._text = text

    def currentIndex(self):
        return self._current_index

    def setCurrentIndex(self, index):
        self._current_index = index

    def setEnabled(self, enabled):
        self._enabled = enabled

    def isEnabled(self):
        return self._enabled

    def blockSignals(self, blocked):
        self.block_signals_calls.append(blocked)

    def clear(self):
        self.items = []
        self.item_data = []
        self._current_index = -1

    def addItem(self, text, user_data=None):
        self.items.append(text)
        self.item_data.append(user_data)
        if self._current_index < 0:
            self._current_index = 0
            if not self._text:
                self._text = text

    def currentData(self):
        if 0 <= self._current_index < len(self.item_data):
            return self.item_data[self._current_index]
        return None

    def count(self):
        return len(self.items)

    def itemText(self, index):
        return self.items[index]

    def findText(self, text):
        try:
            return self.items.index(text)
        except ValueError:
            return -1


class SimPreviewRestartTests(unittest.TestCase):
    """``control_wangbo/main.py`` 中 SIM live preview 停 / 启 / 重启的状态机测试。

    覆盖：
        - 用户按 "Live" → 必须先 ``apply_camera_config`` 再 ``start preview``。
        - "Live" 按下时状态机切到 ``preview_requested``；worker 广播
          ``preview_started`` 后切到 ``preview_active``。
        - 正式采集前必须停 live preview；停止失败要阻止采集并恢复请求状态。
        - 9 帧采集完成后若用户原本在 live preview，应自动重启 live preview。
        - 设置弹窗打开/关闭不影响 live preview 状态。
    """

    """验证集成主界面 live preview 的停止、重启和采集前后状态切换。"""
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance()
        if cls.app is None:
            cls.app = QtWidgets.QApplication([])

    def test_reconstruction_runtime_status_does_not_mark_hardware_leds_failed(self):
        legacy_main = load_legacy_main_module()

        class Led:
            state = "gray"

        class Label:
            text = ""

            def setText(self, text):
                self.text = text

        leds = {key: Led() for key in ("camera", "slm", "daq", "reconstruction")}
        labels = {key: Label() for key in ("camera", "slm", "daq", "reconstruction")}
        window = SimpleNamespace(
            sim_runtime_leds=leds,
            sim_runtime_status_labels=labels,
        )
        window.set_sim_runtime_led_state = lambda led, state: setattr(led, "state", state)

        legacy_main.MainWindow.update_sim_runtime_status_widgets(window, "reconstruction_failed", {})

        self.assertEqual(leds["reconstruction"].state, "red")
        self.assertEqual(labels["reconstruction"].text, "Failed")
        self.assertEqual(leds["camera"].state, "gray")
        self.assertEqual(leds["slm"].state, "gray")
        self.assertEqual(leds["daq"].state, "gray")

    def test_runtime_status_setup_is_noop_without_runtime_panel(self):
        legacy_main = load_legacy_main_module()
        from control_wangbo.CellSorting_ui import Ui_Single_Cell_Sorting

        host = QtWidgets.QWidget()
        ui = Ui_Single_Cell_Sorting()
        ui.setupUi(host)
        window = SimpleNamespace(ui=ui)
        window.set_sim_runtime_led_state = mock.Mock()

        legacy_main.MainWindow.setup_sim_runtime_status_widgets(window)

        self.assertEqual(window.sim_runtime_leds, {})
        self.assertEqual(window.sim_runtime_status_labels, {})
        self.assertFalse(hasattr(ui, "led_simRuntimeReconstruction"))
        self.assertTrue(hasattr(ui, "grp_savePath"))
        window.set_sim_runtime_led_state.assert_not_called()

    def _build_save_path_host(self):
        legacy_main = load_legacy_main_module()
        from control_wangbo.CellSorting_ui import Ui_Single_Cell_Sorting

        host_widget = QtWidgets.QWidget()
        ui = Ui_Single_Cell_Sorting()
        ui.setupUi(host_widget)
        window = type("SavePathHost", (), {})()
        window.ui = ui
        window.sim_save_next_number = 1
        window._loading_configure_settings = False
        window._sim_raw_pending_save_path = {}
        window.save_current_settings_to_default = mock.Mock()
        for name in (
            "setup_sim_save_path_module",
            "_coerce_sim_save_start_number",
            "_coerce_sim_save_next_number",
            "_normal_sim_save_folder",
            "_sanitize_sim_save_prefix",
            "_sim_save_filename_for_number",
            "_current_sim_raw_save_path",
            "_set_sim_save_path_controls",
            "_apply_loaded_sim_save_path_settings",
            "_refresh_sim_save_path_preview",
            "_normalize_main_save_path_prefix",
            "_on_main_save_path_start_number_changed",
            "_browse_main_save_path_folder",
            "_reserve_sim_raw_save_path_for_task",
            "_clear_sim_raw_pending_save_path",
            "_route_raw_stack_save",
        ):
            setattr(window, name, getattr(legacy_main.MainWindow, name).__get__(window, type(window)))
        return host_widget, window

    def test_save_path_numbering_reserves_path_and_routes_by_task_id(self):
        from tempfile import TemporaryDirectory

        host_widget, window = self._build_save_path_host()
        emitted = []
        window.signal_request_raw_save = SimpleNamespace(
            emit=lambda batch, path: emitted.append((getattr(batch, "task_id", ""), path))
        )
        try:
            window.setup_sim_save_path_module()
            with TemporaryDirectory() as temp_dir:
                window._set_sim_save_path_controls(
                    folder=temp_dir,
                    prefix="cell..",
                    start_number=7,
                    next_number=7,
                )
                window._reserve_sim_raw_save_path_for_task("task-1")

                expected = str(Path(temp_dir) / "cell0007.tif")
                self.assertEqual(window._sim_raw_pending_save_path["task-1"], expected)
                self.assertEqual(window.sim_save_next_number, 8)
                self.assertEqual(window.ui.lbl_main_savePath_preview.text(), "Next: cell0008.tif")
                window.save_current_settings_to_default.assert_called_once()

                batch = SimpleNamespace(task_id="task-1")
                window._route_raw_stack_save(batch)
                self.assertEqual(emitted, [("task-1", expected)])
                self.assertEqual(window._sim_raw_pending_save_path, {})
        finally:
            host_widget.close()

    def test_save_path_start_number_reset_and_loaded_next_number(self):
        host_widget, window = self._build_save_path_host()
        try:
            window.setup_sim_save_path_module()
            window._apply_loaded_sim_save_path_settings(
                {
                    "sim_save_folder": "data/custom",
                    "sim_save_prefix": "SIM.raw",
                    "sim_save_start_number": 4,
                    "sim_save_next_number": 9,
                }
            )
            self.assertTrue(window.ui.edit_main_savePath_folder.text().endswith(str(Path("data/custom"))))
            self.assertEqual(window.ui.edit_main_savePath_prefix.text(), "SIM.raw")
            self.assertEqual(window.ui.spb_main_savePath_startNumber.value(), 4)
            self.assertEqual(window.sim_save_next_number, 9)
            self.assertEqual(window.ui.lbl_main_savePath_preview.text(), "Next: SIM.raw0009.tif")

            window.ui.spb_main_savePath_startNumber.setValue(12)
            self.assertEqual(window.sim_save_next_number, 12)
            self.assertEqual(window.ui.lbl_main_savePath_preview.text(), "Next: SIM.raw0012.tif")
        finally:
            host_widget.close()

    def test_live_setting_change_keeps_config_update_in_memory(self):
        legacy_main = load_legacy_main_module()
        save_flags = []
        timer = TimerSpy()
        window = SimpleNamespace(
            sim_camera_connected=True,
            sim_preview_active=True,
            sim_preview_restart_timer=timer,
            sync_sim_camera_config_from_ui=lambda save_to_disk=True: save_flags.append(save_to_disk),
        )

        legacy_main.MainWindow.on_sim_camera_setting_changed(window)

        self.assertEqual(save_flags, [False])
        self.assertEqual(timer.start_calls, [150])

    def test_auto_loaded_legacy_configuration_keeps_sim_camera_from_main_config(self):
        legacy_main = load_legacy_main_module()
        from sim_control.models import AppConfig, CameraConfig

        app_config = AppConfig(
            camera=CameraConfig(
                roi_width=1024,
                roi_height=1024,
                roi_x=124,
                roi_y=248,
            )
        )
        window = SimpleNamespace(
            sim_app_config=app_config,
            sync_sim_camera_config_from_ui=mock.Mock(),
            sync_sim_camera_controls_from_config=mock.Mock(),
            refresh_sim_settings_summary=mock.Mock(),
        )

        legacy_main.MainWindow.apply_loaded_sim_settings_payload(
            window,
            {
                "spb_sCMOS_pixelWidth": 2048,
                "spb_sCMOS_pixelHeight": 2048,
                "spb_sCMOS_ROI_X": 0,
                "spb_sCMOS_ROI_Y": 0,
            },
            apply_legacy_sim_camera_settings=False,
        )

        window.sync_sim_camera_config_from_ui.assert_not_called()
        self.assertEqual(window.sim_app_config.camera.roi_width, 1024)
        self.assertEqual(window.sim_app_config.camera.roi_height, 1024)
        self.assertEqual(window.sim_app_config.camera.roi_x, 124)
        self.assertEqual(window.sim_app_config.camera.roi_y, 248)
        window.sync_sim_camera_controls_from_config.assert_called_once_with()
        window.refresh_sim_settings_summary.assert_called_once_with()

    def test_manual_legacy_configuration_load_keeps_flat_sim_camera_fallback(self):
        legacy_main = load_legacy_main_module()
        window = SimpleNamespace(
            sync_sim_camera_config_from_ui=mock.Mock(),
            sync_sim_camera_controls_from_config=mock.Mock(),
            refresh_sim_settings_summary=mock.Mock(),
        )

        legacy_main.MainWindow.apply_loaded_sim_settings_payload(
            window,
            {
                "spb_sCMOS_pixelWidth": 512,
                "spb_sCMOS_pixelHeight": 512,
                "spb_sCMOS_ROI_X": 64,
                "spb_sCMOS_ROI_Y": 96,
            },
            apply_legacy_sim_camera_settings=True,
        )

        window.sync_sim_camera_config_from_ui.assert_called_once_with(save_to_disk=False)
        window.sync_sim_camera_controls_from_config.assert_called_once_with()
        window.refresh_sim_settings_summary.assert_called_once_with()

    def test_sync_sim_camera_config_from_ui_commits_pending_spinbox_text(self):
        legacy_main = load_legacy_main_module()
        camera = SimpleNamespace(
            device_index=0,
            device_label="",
            roi_width=2048,
            roi_height=2048,
            roi_x=0,
            roi_y=0,
            exposure_us=10_000,
            bit_depth=16,
            timeout_ms=5_000,
        )
        roi_x_spin = InterpretTextSpinBoxSpy(value=0, pending_value=125)
        roi_y_spin = InterpretTextSpinBoxSpy(value=0, pending_value=249)
        exposure_spin = InterpretTextSpinBoxSpy(value=20, pending_value=50)
        window = SimpleNamespace(
            sim_app_config=SimpleNamespace(camera=camera),
            sim_available_cameras=[],
            sim_camera_sensor_size=(2048, 2048),
            sim_camera_size_presets=((2048, 2048), (1024, 1024), (512, 512)),
            sim_camera_roi_step_px=4,
            ui=SimpleNamespace(
                cmb_sCMOS_camera=ComboBoxSpy(current_index=-1),
                cmb_sCMOS_imageSize=ComboBoxSpy(text="1024 x 1024", enabled=True),
                spb_sCMOS_ROI_X=roi_x_spin,
                spb_sCMOS_ROI_Y=roi_y_spin,
                spb_sCMOS_exposureTime=exposure_spin,
                cmb_sCMOS_bitDepth=ComboBoxSpy(text="16-bit"),
            ),
            refresh_sim_settings_summary=mock.Mock(),
        )
        window.sync_sim_camera_roi_position_controls = lambda controls_enabled=None: (
            legacy_main.MainWindow.sync_sim_camera_roi_position_controls(
                window,
                controls_enabled=controls_enabled,
            )
        )

        legacy_main.MainWindow.sync_sim_camera_config_from_ui(window, save_to_disk=False)

        self.assertEqual(exposure_spin.interpret_calls, 1)
        self.assertEqual(roi_x_spin.interpret_calls, 1)
        self.assertEqual(roi_y_spin.interpret_calls, 1)
        self.assertEqual(camera.exposure_us, 50_000)
        self.assertEqual(camera.roi_width, 1024)
        self.assertEqual(camera.roi_height, 1024)
        self.assertEqual(camera.roi_x, 124)
        self.assertEqual(camera.roi_y, 248)

    def test_loading_configuration_suppresses_sim_camera_setting_side_effects(self):
        legacy_main = load_legacy_main_module()
        window = SimpleNamespace(
            _loading_configure_settings=True,
            sync_sim_camera_config_from_ui=mock.Mock(),
            select_current_sim_running_order=mock.Mock(),
            refresh_sim_settings_summary=mock.Mock(),
            sim_acquisition_controller=SimpleNamespace(apply_camera_config=mock.Mock()),
        )

        legacy_main.MainWindow.on_sim_camera_setting_changed(window)

        window.sync_sim_camera_config_from_ui.assert_not_called()
        window.select_current_sim_running_order.assert_not_called()
        window.refresh_sim_settings_summary.assert_not_called()
        window.sim_acquisition_controller.apply_camera_config.assert_not_called()

    def test_camera_setting_change_refreshes_slm_running_order_without_camera_connection(self):
        legacy_main = load_legacy_main_module()
        save_flags = []
        refresh_calls = []
        window = SimpleNamespace(
            sim_camera_connected=False,
            sim_slm_connected=True,
            sim_app_config=SimpleNamespace(selected_running_order="old_ro"),
            sync_sim_camera_config_from_ui=lambda save_to_disk=True: save_flags.append(save_to_disk),
            select_current_sim_running_order=lambda save_to_disk=False: refresh_calls.append(save_to_disk),
            refresh_sim_settings_summary=mock.Mock(),
        )

        legacy_main.MainWindow.on_sim_camera_setting_changed(window)

        self.assertEqual(save_flags, [False])
        self.assertEqual(refresh_calls, [False])
        self.assertEqual(window.sim_app_config.selected_running_order, "old_ro")

    def test_connected_camera_setting_change_applies_config_and_refreshes_runtime_timing(self):
        legacy_main = load_legacy_main_module()
        save_flags = []
        refresh_calls = []
        camera = SimpleNamespace(bit_depth=16)
        timing_payload = {
            "timing_readout_time_s": 0.0042,
            "recommended_inter_frame_gap_us": 5200,
        }
        controller = SimpleNamespace(apply_camera_config=mock.Mock(return_value=timing_payload))
        window = SimpleNamespace(
            sim_camera_connected=True,
            sim_slm_connected=False,
            sim_preview_requested=False,
            sim_preview_active=False,
            sim_acquisition_controller=controller,
            sim_app_config=SimpleNamespace(camera=camera),
            sim_runtime_timing_snapshot={},
            sync_sim_camera_config_from_ui=lambda save_to_disk=True: save_flags.append(save_to_disk),
            refresh_sim_settings_summary=lambda: refresh_calls.append("refreshed"),
        )

        legacy_main.MainWindow.on_sim_camera_setting_changed(window)

        self.assertEqual(save_flags, [False])
        controller.apply_camera_config.assert_called_once_with(camera)
        self.assertEqual(window.sim_runtime_timing_snapshot["timing_readout_time_s"], 0.0042)
        self.assertEqual(window.sim_runtime_timing_snapshot["recommended_inter_frame_gap_us"], 5200)
        self.assertEqual(refresh_calls, ["refreshed"])

    def test_start_sim_preview_applies_camera_config_before_preview_start(self):
        legacy_main = load_legacy_main_module()
        events = []
        camera = SimpleNamespace(bit_depth=16, exposure_us=500_000)
        controller = SimpleNamespace(
            apply_camera_config=mock.Mock(
                side_effect=lambda camera_config: events.append("apply") or {
                    "timing_readout_time_s": 0.0038,
                    "recommended_inter_frame_gap_us": 4800,
                }
            )
        )
        preview_controller = SimpleNamespace(
            start=mock.Mock(side_effect=lambda camera_config, timeout_ms=100: events.append("preview"))
        )
        window = SimpleNamespace(
            sim_acquisition_in_progress=False,
            sim_camera_connected=True,
            sim_preview_stop_in_progress=False,
            sim_preview_restart_requested=True,
            sim_preview_requested=False,
            sim_preview_active=False,
            sim_app_config=SimpleNamespace(camera=camera),
            sim_acquisition_controller=controller,
            sim_preview_controller=preview_controller,
            sim_runtime_timing_snapshot={},
            sync_sim_camera_config_from_ui=lambda save_to_disk=True: events.append("sync"),
            ensure_sim_runtime=lambda: None,
            update_sim_camera_action_buttons=lambda: None,
            refresh_sim_settings_summary=lambda: events.append("summary"),
        )

        legacy_main.MainWindow.start_sim_preview(window)

        self.assertEqual(events[:3], ["sync", "apply", "summary"])
        self.assertEqual(events[-1], "preview")
        applied_camera = controller.apply_camera_config.call_args.args[0]
        preview_camera = preview_controller.start.call_args.args[0]
        self.assertIs(applied_camera, preview_camera)
        self.assertIsNot(applied_camera, camera)
        self.assertEqual(applied_camera.exposure_us, legacy_main.SIM_PREVIEW_EXPOSURE_US)
        self.assertEqual(camera.exposure_us, 500_000)
        preview_controller.start.assert_called_once_with(applied_camera, timeout_ms=200)
        self.assertEqual(window.sim_runtime_timing_snapshot["recommended_inter_frame_gap_us"], 4800)

    def test_size_dropdown_activation_centers_1024_roi_on_2048_sensor(self):
        legacy_main = load_legacy_main_module()
        on_change_calls = []
        camera = SimpleNamespace(roi_x=0, roi_y=0, roi_width=2048, roi_height=2048)
        roi_x_spin = SpinBoxSpy(0, maximum=0, enabled=False)
        roi_y_spin = SpinBoxSpy(0, maximum=0, enabled=False)
        window = SimpleNamespace(
            sim_app_config=SimpleNamespace(camera=camera),
            sim_camera_sensor_size=(2048, 2048),
            sim_camera_size_presets=((2048, 2048), (1024, 1024), (512, 512)),
            sim_camera_roi_step_px=4,
            ui=SimpleNamespace(
                cmb_sCMOS_imageSize=ComboBoxSpy(text="1024 x 1024", enabled=True),
                spb_sCMOS_ROI_X=roi_x_spin,
                spb_sCMOS_ROI_Y=roi_y_spin,
            ),
            on_sim_camera_setting_changed=lambda: on_change_calls.append("changed"),
        )
        window.sync_sim_camera_roi_position_controls = (
            lambda controls_enabled=None: legacy_main.MainWindow.sync_sim_camera_roi_position_controls(
                window,
                controls_enabled=controls_enabled,
            )
        )

        legacy_main.MainWindow.on_sim_camera_size_activated(window)

        self.assertEqual(camera.roi_width, 1024)
        self.assertEqual(camera.roi_height, 1024)
        self.assertEqual(camera.roi_x, 512)
        self.assertEqual(camera.roi_y, 512)
        self.assertEqual(roi_x_spin.value(), 512)
        self.assertEqual(roi_y_spin.value(), 512)
        self.assertEqual(roi_x_spin.maximum(), 1024)
        self.assertEqual(roi_y_spin.maximum(), 1024)
        self.assertTrue(roi_x_spin.isEnabled())
        self.assertTrue(roi_y_spin.isEnabled())
        self.assertEqual(on_change_calls, ["changed"])

    def test_size_dropdown_activation_centers_512_roi_on_2048_sensor(self):
        legacy_main = load_legacy_main_module()
        on_change_calls = []
        camera = SimpleNamespace(roi_x=512, roi_y=512, roi_width=1024, roi_height=1024)
        roi_x_spin = SpinBoxSpy(512, maximum=1024, enabled=True)
        roi_y_spin = SpinBoxSpy(512, maximum=1024, enabled=True)
        window = SimpleNamespace(
            sim_app_config=SimpleNamespace(camera=camera),
            sim_camera_sensor_size=(2048, 2048),
            sim_camera_size_presets=((2048, 2048), (1024, 1024), (512, 512)),
            sim_camera_roi_step_px=4,
            ui=SimpleNamespace(
                cmb_sCMOS_imageSize=ComboBoxSpy(text="512 x 512", enabled=True),
                spb_sCMOS_ROI_X=roi_x_spin,
                spb_sCMOS_ROI_Y=roi_y_spin,
            ),
            on_sim_camera_setting_changed=lambda: on_change_calls.append("changed"),
        )
        window.sync_sim_camera_roi_position_controls = (
            lambda controls_enabled=None: legacy_main.MainWindow.sync_sim_camera_roi_position_controls(
                window,
                controls_enabled=controls_enabled,
            )
        )

        legacy_main.MainWindow.on_sim_camera_size_activated(window)

        self.assertEqual(camera.roi_width, 512)
        self.assertEqual(camera.roi_height, 512)
        self.assertEqual(camera.roi_x, 768)
        self.assertEqual(camera.roi_y, 768)
        self.assertEqual(roi_x_spin.value(), 768)
        self.assertEqual(roi_y_spin.value(), 768)
        self.assertEqual(roi_x_spin.maximum(), 1536)
        self.assertEqual(roi_y_spin.maximum(), 1536)
        self.assertTrue(roi_x_spin.isEnabled())
        self.assertTrue(roi_y_spin.isEnabled())
        self.assertEqual(on_change_calls, ["changed"])

    def test_restart_request_stops_preview_non_blocking_and_defers_restart(self):
        legacy_main = load_legacy_main_module()
        controller = PreviewControllerSpy()
        starts = []
        blocking_stop_calls = []
        window = SimpleNamespace(
            sim_preview_active=True,
            sim_camera_connected=True,
            sim_acquisition_in_progress=False,
            sim_preview_restart_requested=False,
            sim_preview_stop_in_progress=False,
            sim_preview_controller=controller,
            stop_sim_preview=lambda: blocking_stop_calls.append("stop"),
            start_sim_preview=lambda: starts.append("start"),
            update_sim_camera_action_buttons=lambda: None,
            ui=SimpleNamespace(lb_sCMOS_FPSshow=LabelSpy()),
        )

        legacy_main.MainWindow.restart_sim_preview_with_current_settings(window)

        self.assertTrue(window.sim_preview_restart_requested)
        self.assertTrue(window.sim_preview_stop_in_progress)
        self.assertEqual(controller.stop_calls, [False])
        self.assertEqual(blocking_stop_calls, [])
        self.assertEqual(starts, [])

    def test_preview_stopped_status_restarts_once_when_restart_is_pending(self):
        legacy_main = load_legacy_main_module()
        starts = []
        fps_label = LabelSpy()
        window = SimpleNamespace(
            sim_preview_active=False,
            sim_camera_connected=True,
            sim_acquisition_in_progress=False,
            sim_preview_restart_requested=True,
            sim_preview_stop_in_progress=True,
            start_sim_preview=lambda: starts.append("start"),
            update_sim_camera_action_buttons=lambda: None,
            ui=SimpleNamespace(lb_sCMOS_FPSshow=fps_label),
        )

        legacy_main.MainWindow.slot_handle_sim_preview_status(window, "preview_stopped", {})

        self.assertFalse(window.sim_preview_restart_requested)
        self.assertFalse(window.sim_preview_stop_in_progress)
        self.assertEqual(fps_label.text, "0")
        self.assertEqual(starts, ["start"])

    def test_preview_stopped_status_does_not_restart_when_live_request_was_cancelled(self):
        legacy_main = load_legacy_main_module()
        starts = []
        fps_label = LabelSpy()
        window = SimpleNamespace(
            sim_preview_active=False,
            sim_preview_requested=False,
            sim_camera_connected=True,
            sim_acquisition_in_progress=False,
            sim_preview_restart_requested=True,
            sim_preview_stop_in_progress=True,
            start_sim_preview=lambda: starts.append("start"),
            update_sim_camera_action_buttons=lambda: None,
            ui=SimpleNamespace(lb_sCMOS_FPSshow=fps_label),
        )

        legacy_main.MainWindow.slot_handle_sim_preview_status(window, "preview_stopped", {})

        self.assertFalse(window.sim_preview_restart_requested)
        self.assertFalse(window.sim_preview_stop_in_progress)
        self.assertEqual(fps_label.text, "0")
        self.assertEqual(starts, [])

    def test_preview_started_status_starts_poll_timer_and_resets_last_sequence(self):
        legacy_main = load_legacy_main_module()
        timer = TimerSpy()
        controller = SimpleNamespace(frame_poll_interval_ms=40)
        window = SimpleNamespace(
            sim_preview_active=False,
            sim_preview_restart_requested=True,
            sim_preview_stop_in_progress=True,
            sim_preview_controller=controller,
            sim_preview_poll_timer=timer,
            sim_last_preview_sequence=17,
            update_sim_camera_action_buttons=lambda: None,
        )

        legacy_main.MainWindow.slot_handle_sim_preview_status(window, "preview_started", {})

        self.assertTrue(window.sim_preview_active)
        self.assertFalse(window.sim_preview_restart_requested)
        self.assertFalse(window.sim_preview_stop_in_progress)
        self.assertEqual(window.sim_last_preview_sequence, -1)
        self.assertEqual(timer.start_calls, [40])

    def test_update_sim_camera_action_buttons_keeps_abort_label_while_live_restart_is_pending(self):
        legacy_main = load_legacy_main_module()
        live_button = ButtonSpy()
        connection_button = ButtonSpy()
        refresh_button = ButtonSpy()
        camera_combo = ComboBoxSpy()
        window = SimpleNamespace(
            sim_available_cameras=[{"index": 0, "display": "0: Camera"}],
            sim_camera_connected=True,
            sim_acquisition_in_progress=False,
            sim_preview_active=False,
            sim_preview_requested=True,
            sim_preview_stop_in_progress=True,
            ui=SimpleNamespace(
                btn_sCMOS_connection=connection_button,
                btn_sCMOS_live=live_button,
                btn_sCMOS_refresh=refresh_button,
                cmb_sCMOS_camera=camera_combo,
            ),
        )

        legacy_main.MainWindow.update_sim_camera_action_buttons(window)

        self.assertEqual(live_button.text, "Abort")

    def test_live_button_cancels_pending_restart_during_stop_in_progress(self):
        legacy_main = load_legacy_main_module()
        stop_calls = []
        window = SimpleNamespace(
            sim_camera_connected=True,
            sim_preview_active=False,
            sim_preview_requested=True,
            sim_preview_restart_requested=True,
            sim_preview_stop_in_progress=True,
            stop_sim_preview=lambda wait=False, clear_restart=True, clear_display=False: stop_calls.append(
                (wait, clear_restart, clear_display)
            ),
            start_sim_preview=mock.Mock(),
        )

        legacy_main.MainWindow.btn_sCMOS_live_function(window)

        self.assertFalse(window.sim_preview_requested)
        self.assertFalse(window.sim_preview_restart_requested)
        self.assertEqual(stop_calls, [(False, True, True)])
        window.start_sim_preview.assert_not_called()

    def test_stop_sim_preview_stops_poll_timer_and_resets_last_sequence(self):
        legacy_main = load_legacy_main_module()
        timer = TimerSpy()
        controller = PreviewControllerSpy()
        window = SimpleNamespace(
            sim_preview_restart_timer=TimerSpy(),
            sim_preview_controller=controller,
            sim_preview_poll_timer=timer,
            sim_preview_active=True,
            sim_preview_stop_in_progress=False,
            sim_preview_restart_requested=False,
            sim_last_preview_sequence=9,
            _clear_sim_preview_display=mock.Mock(),
            update_sim_camera_action_buttons=lambda: None,
            ui=SimpleNamespace(lb_sCMOS_FPSshow=LabelSpy()),
        )

        legacy_main.MainWindow.stop_sim_preview(window, wait=False, clear_restart=True)

        self.assertEqual(timer.stop_calls, 1)
        self.assertEqual(window.sim_last_preview_sequence, -1)
        self.assertEqual(controller.stop_calls, [False])
        self.assertEqual(window.ui.lb_sCMOS_FPSshow.text, "0")
        window._clear_sim_preview_display.assert_not_called()

    def test_stop_sim_preview_clears_display_only_when_requested(self):
        legacy_main = load_legacy_main_module()
        controller = PreviewControllerSpy()
        clear_display = mock.Mock()
        window = SimpleNamespace(
            sim_preview_restart_timer=TimerSpy(),
            sim_preview_controller=controller,
            sim_preview_poll_timer=TimerSpy(),
            sim_preview_active=True,
            sim_preview_stop_in_progress=False,
            sim_preview_restart_requested=False,
            sim_last_preview_sequence=9,
            _clear_sim_preview_display=clear_display,
            update_sim_camera_action_buttons=lambda: None,
            ui=SimpleNamespace(lb_sCMOS_FPSshow=LabelSpy()),
        )

        legacy_main.MainWindow.stop_sim_preview(
            window,
            wait=False,
            clear_restart=True,
            clear_display=True,
        )

        clear_display.assert_called_once_with()

    def test_main_window_uses_module_local_default_configuration_path(self):
        source = (CONTROL_ROOT / "main.py").read_text(encoding="utf-8")

        self.assertIn('default_path = Path(__file__).parent / "lastConfiguration.json"', source)
        self.assertNotIn('default_path = Path(os.getcwd()) / "lastConfiguration.json"', source)

    def test_trigger_sim_formal_acquisition_snapshots_live_request_and_clears_requested_flag(self):
        legacy_main = load_legacy_main_module()
        from sim_control.models import AppConfig, CameraConfig

        stop_calls = []
        timer = TimerSpy()
        app_config = AppConfig(camera=CameraConfig(exposure_us=500_000))
        controller = SimpleNamespace(
            initialize_hardware=mock.Mock(),
            apply_daq_config=mock.Mock(),
            apply_camera_config=mock.Mock(),
            prepare_patterns=mock.Mock(),
            select_running_order_for_task=mock.Mock(return_value={"running_order_name": "488_3.5_2d_1ms"}),
            start_single_acquisition=mock.Mock(return_value="task-1"),
        )
        window = SimpleNamespace(
            sim_camera_connected=True,
            sim_slm_connected=True,
            sim_preview_active=True,
            sim_preview_requested=True,
            sim_preview_restart_requested=True,
            sim_preview_stop_in_progress=False,
            sim_acquisition_in_progress=False,
            sim_resume_preview_after_acquisition=False,
            sim_current_task_id="",
            sim_acquisition_controller=controller,
            sim_app_config=app_config,
            sync_sim_camera_config_from_ui=lambda save_to_disk=False: None,
            ensure_sim_runtime=lambda: None,
            stop_sim_preview=lambda wait=True: (stop_calls.append(wait) or True),
            update_sim_camera_action_buttons=lambda: None,
            set_sim_camera_controls_enabled=lambda enabled: None,
            sim_preview_restart_timer=timer,
        )

        legacy_main.MainWindow.trigger_sim_formal_acquisition(window, trigger_source="test")

        self.assertEqual(timer.stop_calls, 1)
        self.assertTrue(window.sim_resume_preview_after_acquisition)
        self.assertFalse(window.sim_preview_requested)
        self.assertFalse(window.sim_preview_restart_requested)
        self.assertEqual(stop_calls, [True])
        self.assertTrue(window.sim_acquisition_in_progress)
        self.assertFalse(hasattr(window, "sim_last_acquisition_batch"))
        self.assertEqual(window.sim_current_task_id, "task-1")
        task = controller.start_single_acquisition.call_args.args[0]
        self.assertEqual(task.camera.exposure_us, 500_000)
        self.assertEqual(app_config.camera.exposure_us, 500_000)
        controller.initialize_hardware.assert_not_called()
        controller.apply_daq_config.assert_not_called()
        controller.apply_camera_config.assert_not_called()
        controller.prepare_patterns.assert_not_called()
        controller.select_running_order_for_task.assert_not_called()
        start_kwargs = controller.start_single_acquisition.call_args.kwargs
        self.assertTrue(start_kwargs["prepare_running_order"])
        self.assertTrue(start_kwargs["initialize_hardware"])
        self.assertTrue(start_kwargs["apply_daq_config"])
        self.assertTrue(start_kwargs["apply_camera_config"])

    def test_trigger_sim_raw_9frame_acquisition_disables_reconstruction_and_uses_config_z_scan(self):
        legacy_main = load_legacy_main_module()
        from sim_control.models import AppConfig

        combo = ComboBoxSpy()
        combo.addItem("(none)", None)
        combo.addItem("488_3.5_2d_imm_f1", 28)
        combo.setCurrentIndex(1)
        controller = SimpleNamespace(
            start_single_acquisition=mock.Mock(return_value="raw-task-1"),
        )
        app_config = AppConfig()
        app_config.reconstruction.enabled = True
        app_config.z_scan.enabled = True

        window = SimpleNamespace(
            sim_camera_connected=True,
            sim_slm_connected=True,
            sim_preview_active=False,
            sim_preview_requested=False,
            sim_preview_restart_requested=False,
            sim_preview_stop_in_progress=False,
            sim_acquisition_in_progress=False,
            sim_resume_preview_after_acquisition=False,
            sim_current_task_id="",
            sim_current_acquisition_raw_only=False,
            sim_acquisition_controller=controller,
            sim_app_config=app_config,
            sim_immediate_ro_items=[{"index": 28, "name": "488_3.5_2d_imm_f1"}],
            ui=SimpleNamespace(cmb_SLM_immediateRO=combo),
            sync_sim_camera_config_from_ui=lambda save_to_disk=False: None,
            ensure_sim_runtime=lambda: None,
            stop_immediate_live_mode=mock.Mock(),
            _immediate_live_active_or_pending=mock.Mock(return_value=False),
            _select_immediate_ro_none_without_clearing=lambda: legacy_main.MainWindow._select_immediate_ro_none_without_clearing(window),
            _update_immediate_ro_dropdown_tooltip=mock.Mock(),
            ensure_sim_raw_stack_save_worker=mock.Mock(),
            disconnect_sim_reconstruction_worker_from_controller=mock.Mock(),
            connect_sim_raw_stack_save_worker_to_controller=mock.Mock(),
            _reserve_sim_raw_save_path_for_task=mock.Mock(),
            _clear_sim_raw_pending_save_path=mock.Mock(),
            update_sim_camera_action_buttons=mock.Mock(),
            set_sim_camera_controls_enabled=mock.Mock(),
            sim_preview_restart_timer=TimerSpy(),
        )

        legacy_main.MainWindow.trigger_sim_raw_9frame_acquisition(window, trigger_source="test")

        window.stop_immediate_live_mode.assert_called_once_with(reset_dropdown=False)
        window._immediate_live_active_or_pending.assert_called_once_with()
        self.assertEqual(combo.items, ["(none)", "488_3.5_2d_imm_f1"])
        self.assertEqual(combo.item_data, [None, 28])
        self.assertEqual(combo.currentIndex(), 0)
        self.assertEqual(window.sim_immediate_ro_items, [{"index": 28, "name": "488_3.5_2d_imm_f1"}])
        window.ensure_sim_raw_stack_save_worker.assert_called_once()
        window.disconnect_sim_reconstruction_worker_from_controller.assert_called_once()
        window.connect_sim_raw_stack_save_worker_to_controller.assert_called_once()
        self.assertTrue(window.sim_current_acquisition_raw_only)
        self.assertEqual(window.sim_current_task_id, "raw-task-1")
        window._reserve_sim_raw_save_path_for_task.assert_called_once_with("raw-task-1")
        window._clear_sim_raw_pending_save_path.assert_not_called()
        start_kwargs = controller.start_single_acquisition.call_args.kwargs
        self.assertTrue(start_kwargs["prepare_running_order"])
        self.assertTrue(start_kwargs["initialize_hardware"])
        self.assertTrue(start_kwargs["apply_daq_config"])
        self.assertTrue(start_kwargs["apply_camera_config"])
        # SIM9 按钮现读「是否开启 Z-Scan」开关（z_scan.enabled）：此处 config 开启 → 透传 True。
        self.assertIs(start_kwargs["z_scan_enabled"], True)
        self.assertFalse(start_kwargs["reconstruction_config"].enabled)
        self.assertTrue(app_config.reconstruction.enabled)
        self.assertTrue(app_config.z_scan.enabled)

    def test_trigger_sim_formal_acquisition_blocks_when_preview_does_not_stop(self):
        legacy_main = load_legacy_main_module()
        from sim_control.models import AppConfig

        warnings = []
        controller = SimpleNamespace(
            start_single_acquisition=mock.Mock(return_value="task-1"),
        )

        def stop_preview(wait=True):
            window.sim_preview_stop_in_progress = True
            return False

        window = SimpleNamespace(
            sim_camera_connected=True,
            sim_slm_connected=True,
            sim_preview_active=True,
            sim_preview_requested=True,
            sim_preview_restart_requested=False,
            sim_preview_stop_in_progress=False,
            sim_acquisition_in_progress=False,
            sim_resume_preview_after_acquisition=False,
            sim_current_task_id="",
            sim_acquisition_controller=controller,
            sim_app_config=AppConfig(),
            sync_sim_camera_config_from_ui=lambda save_to_disk=False: None,
            ensure_sim_runtime=lambda: None,
            stop_sim_preview=stop_preview,
            update_sim_camera_action_buttons=mock.Mock(),
            set_sim_camera_controls_enabled=mock.Mock(),
            sim_preview_restart_timer=TimerSpy(),
        )

        with mock.patch.object(
            legacy_main.qw.QMessageBox,
            "warning",
            side_effect=lambda _parent, title, message: warnings.append((title, message)),
        ):
            legacy_main.MainWindow.trigger_sim_formal_acquisition(window, trigger_source="test")

        self.assertFalse(window.sim_acquisition_in_progress)
        self.assertTrue(window.sim_preview_requested)
        self.assertFalse(window.sim_preview_restart_requested)
        self.assertFalse(window.sim_resume_preview_after_acquisition)
        controller.start_single_acquisition.assert_not_called()
        self.assertEqual(warnings, [("SIM Preview", "SIM preview is still stopping. Please retry after it stops.")])

    def test_trigger_sim_formal_acquisition_blocks_when_slm_is_disconnected(self):
        legacy_main = load_legacy_main_module()
        from sim_control.models import AppConfig

        warning_messages = []
        controller = SimpleNamespace(
            initialize_hardware=mock.Mock(),
            apply_daq_config=mock.Mock(),
            apply_camera_config=mock.Mock(),
            select_running_order_for_task=mock.Mock(),
            start_single_acquisition=mock.Mock(),
        )
        window = SimpleNamespace(
            sim_camera_connected=True,
            sim_slm_connected=False,
            sim_acquisition_in_progress=False,
            sim_preview_requested=False,
            sim_preview_active=False,
            sim_preview_restart_requested=False,
            sim_preview_stop_in_progress=False,
            sim_preview_restart_timer=TimerSpy(),
            sim_acquisition_controller=controller,
            sim_app_config=AppConfig(),
            sync_sim_camera_config_from_ui=lambda save_to_disk=False: None,
            ensure_sim_runtime=lambda: None,
            update_sim_camera_action_buttons=lambda: None,
            set_sim_camera_controls_enabled=lambda enabled: None,
        )

        with mock.patch.object(legacy_main.qw.QMessageBox, "information", side_effect=lambda _parent, title, message: warning_messages.append((title, message))):
            legacy_main.MainWindow.trigger_sim_formal_acquisition(window, trigger_source="test")

        self.assertEqual(warning_messages, [("SIM SLM", "Please connect the SLM before starting SIM acquisition.")])
        controller.initialize_hardware.assert_not_called()
        controller.start_single_acquisition.assert_not_called()

    def test_slot_handle_sim_acquisition_ready_accepts_summary_payload_without_raw_stack(self):
        legacy_main = load_legacy_main_module()

        summary = {
            "task_id": "task-2",
            "stack_shape": [9, 2, 3],
            "stack_dtype": "uint16",
            "metadata": {"running_order_name": "488_3.5_2d_10ms"},
        }
        action_updates = []
        controls_enabled = []
        starts = []
        window = SimpleNamespace(
            sim_acquisition_in_progress=True,
            sim_camera_connected=True,
            sim_resume_preview_after_acquisition=True,
            sim_current_task_id="",
            update_sim_camera_action_buttons=lambda: action_updates.append("updated"),
            set_sim_camera_controls_enabled=lambda enabled: controls_enabled.append(enabled),
            start_sim_preview=lambda: starts.append("start"),
        )

        legacy_main.MainWindow.slot_handle_sim_acquisition_ready(window, summary)

        self.assertFalse(window.sim_acquisition_in_progress)
        self.assertEqual(action_updates, ["updated"])
        self.assertEqual(controls_enabled, [True])
        # summary 槽不得在窗口上暂存 raw batch / 预览帧：合同为"不新增任何同类属性"。
        self.assertFalse(hasattr(window, "sim_last_acquisition_batch"))
        self.assertFalse(hasattr(window, "sim_last_preview_frame"))
        self.assertEqual(window.sim_current_task_id, "task-2")
        self.assertEqual(starts, ["start"])

    def test_slot_handle_sim_acquisition_ready_raw_only_does_not_start_reconstruction(self):
        legacy_main = load_legacy_main_module()

        summary = {
            "task_id": "raw-task-2",
            "stack_shape": [9, 2, 3],
            "stack_dtype": "uint16",
            "metadata": {"running_order_name": "488_3.5_2d_10ms"},
        }
        status_updates = []
        restore_calls = []
        window = SimpleNamespace(
            sim_acquisition_in_progress=True,
            sim_camera_connected=True,
            sim_resume_preview_after_acquisition=False,
            sim_current_task_id="",
            sim_current_acquisition_raw_only=True,
            sim_raw_stack_save_finished_task_ids=set(),
            update_sim_camera_action_buttons=mock.Mock(),
            set_sim_camera_controls_enabled=mock.Mock(),
            update_sim_runtime_status_widgets=lambda status, payload=None: status_updates.append(status),
            _restore_sim_post_acquisition_routing_after_raw_only=lambda: restore_calls.append("restore"),
        )

        legacy_main.MainWindow.slot_handle_sim_acquisition_ready(window, summary)

        self.assertFalse(window.sim_acquisition_in_progress)
        self.assertIn("acquisition_complete", status_updates)
        self.assertIn("raw_stack_saving", status_updates)
        self.assertNotIn("reconstruction_starting", status_updates)
        self.assertEqual(restore_calls, ["restore"])

    def test_slot_handle_sim_acquisition_ready_late_raw_summary_does_not_override_saved_status(self):
        legacy_main = load_legacy_main_module()

        status_updates = []
        window = SimpleNamespace(
            sim_acquisition_in_progress=True,
            sim_camera_connected=False,
            sim_resume_preview_after_acquisition=False,
            sim_current_task_id="",
            sim_current_acquisition_raw_only=True,
            sim_raw_stack_save_finished_task_ids={"raw-task-3"},
            update_sim_camera_action_buttons=mock.Mock(),
            set_sim_camera_controls_enabled=mock.Mock(),
            update_sim_runtime_status_widgets=lambda status, payload=None: status_updates.append(status),
            _restore_sim_post_acquisition_routing_after_raw_only=mock.Mock(),
        )

        legacy_main.MainWindow.slot_handle_sim_acquisition_ready(
            window,
            {"task_id": "raw-task-3", "stack_shape": [9, 2, 3], "stack_dtype": "uint16"},
        )

        self.assertIn("acquisition_complete", status_updates)
        self.assertNotIn("raw_stack_saving", status_updates)
        self.assertNotIn("reconstruction_starting", status_updates)

    def test_slm_ro_mismatch_disconnects_half_connected_adapter_and_allows_refresh(self):
        legacy_main = load_legacy_main_module()
        from sim_control.errors import HardwareError
        from sim_control.models import AppConfig, CameraConfig

        error_message = "No matching SLM running order found for 488 nm, 10 ms bucket, pitch 3.5, mode 2d."
        config = AppConfig(camera=CameraConfig(exposure_us=10_000))
        config.config_path = "dummy.json"
        config.selected_laser_nm = 488
        config.selected_running_order = "stale_ro"
        initial_slm = {"display": "R11 selected", "path": "r11-initial"}
        refreshed_slm = {"display": "R11 refreshed", "path": "r11-refreshed"}
        controller = SimpleNamespace(
            is_busy=False,
            reset_all_daq_low=mock.Mock(),
            connect_slm=mock.Mock(),
            refresh_immediate_running_orders=mock.Mock(),
            select_running_order_for_task=mock.Mock(side_effect=HardwareError(error_message)),
            list_immediate_running_orders=mock.Mock(),
            disconnect_slm=mock.Mock(),
            refresh_available_slm_devices=mock.Mock(return_value=[refreshed_slm]),
        )
        window = SimpleNamespace(
            sim_slm_connected=False,
            sim_available_slms=[initial_slm],
            sim_acquisition_in_progress=False,
            sim_preview_stop_in_progress=False,
            sim_immediate_ro_items=[{"index": 99, "name": "488_stale_imm"}],
            sim_acquisition_controller=controller,
            sim_app_config=config,
            ensure_sim_runtime=mock.Mock(),
            refresh_sim_settings_summary=mock.Mock(),
            _slm_connect_thread=None,
            _slm_connect_worker=None,
            ui=SimpleNamespace(
                cmb_SLM_device=ComboBoxSpy(current_index=0),
                btn_SLM_refresh=ButtonSpy(),
                btn_SLM_connection=ButtonSpy(),
                cmb_SLM_immediateRO=ComboBoxSpy(),
            ),
        )
        window._reset_immediate_ro_dropdown = lambda: legacy_main.MainWindow._reset_immediate_ro_dropdown(window)
        window.update_sim_slm_controls = lambda: legacy_main.MainWindow.update_sim_slm_controls(window)
        window.refresh_sim_slm_devices = (
            lambda show_dialog_on_error=False: legacy_main.MainWindow.refresh_sim_slm_devices(
                window, show_dialog_on_error=show_dialog_on_error
            )
        )

        warning_messages = []
        finished = False
        with mock.patch.object(legacy_main, "save_app_config") as save_mock, mock.patch.object(
            legacy_main.qw.QMessageBox,
            "warning",
            side_effect=lambda _parent, title, message: warning_messages.append((title, message)),
        ):
            try:
                legacy_main.MainWindow.toggle_sim_slm_connection(window)
                finished = _wait_for_condition(lambda: window._slm_connect_thread is None)
            finally:
                thread = getattr(window, "_slm_connect_thread", None)
                if thread is not None:
                    thread.quit()
                    thread.wait(1000)
                    window._slm_connect_thread = None
                    window._slm_connect_worker = None

        self.assertTrue(finished)
        self.assertEqual(warning_messages, [("SIM SLM", error_message)])
        controller.reset_all_daq_low.assert_called_once_with()
        controller.connect_slm.assert_called_once_with(device_path="r11-initial")
        controller.refresh_immediate_running_orders.assert_called_once_with()
        controller.select_running_order_for_task.assert_called_once_with(488, config.camera.exposure_us)
        controller.list_immediate_running_orders.assert_not_called()
        controller.disconnect_slm.assert_called_once_with()
        save_mock.assert_called_once_with(config, config.config_path)
        window.refresh_sim_settings_summary.assert_called_once_with()
        controller.refresh_available_slm_devices.assert_called_once_with()
        self.assertFalse(window.sim_slm_connected)
        self.assertEqual(window.sim_app_config.selected_running_order, "")
        self.assertEqual(window.sim_immediate_ro_items, [])
        self.assertEqual(window.ui.cmb_SLM_immediateRO.items, ["(none)"])
        self.assertEqual(window.ui.cmb_SLM_immediateRO.item_data, [None])
        self.assertEqual(window.ui.cmb_SLM_immediateRO.currentIndex(), 0)
        self.assertFalse(window.ui.cmb_SLM_immediateRO.isEnabled())
        self.assertEqual(window.sim_available_slms, [refreshed_slm])
        self.assertEqual(window.ui.cmb_SLM_device.items, ["R11 refreshed"])
        self.assertEqual(window.ui.cmb_SLM_device.currentIndex(), 0)
        self.assertTrue(window.ui.cmb_SLM_device.isEnabled())
        self.assertTrue(window.ui.btn_SLM_refresh.enabled)
        self.assertTrue(window.ui.btn_SLM_connection.enabled)
        self.assertEqual(window.ui.btn_SLM_connection.text, "Connect SLM")

    def test_refresh_sim_camera_devices_reuses_connected_camera_without_reenumeration(self):
        legacy_main = load_legacy_main_module()
        connected_info = {
            "index": 0,
            "display": "0: C15440-20UP [S/N: 501679]",
            "model": "C15440-20UP",
            "camera_id": "S/N: 501679",
        }
        combo_box = ComboBoxSpy()
        window = SimpleNamespace(
            sim_camera_connected=True,
            sim_available_cameras=[],
            sim_app_config=SimpleNamespace(
                camera=SimpleNamespace(device_index=0, device_label=""),
            ),
            sim_acquisition_controller=SimpleNamespace(
                camera_connection_info=mock.Mock(return_value=connected_info),
                refresh_available_camera_devices=mock.Mock(side_effect=AssertionError("Should not enumerate while connected")),
            ),
            prefer_real_sim_hardware=mock.Mock(),
            ensure_sim_runtime=mock.Mock(),
            update_sim_camera_action_buttons=mock.Mock(),
            refresh_sim_settings_summary=mock.Mock(),
            ui=SimpleNamespace(cmb_sCMOS_camera=combo_box),
        )

        legacy_main.MainWindow.refresh_sim_camera_devices(window)

        window.prefer_real_sim_hardware.assert_called_once_with(save_to_disk=True, probe_camera=False)
        window.sim_acquisition_controller.camera_connection_info.assert_called_once_with()
        window.ensure_sim_runtime.assert_not_called()
        self.assertEqual(window.sim_available_cameras, [connected_info])
        self.assertEqual(combo_box.items, ["0: C15440-20UP [S/N: 501679]"])
        self.assertEqual(combo_box.currentIndex(), 0)
        self.assertEqual(window.sim_app_config.camera.device_label, connected_info["display"])

    def test_sync_sim_camera_controls_from_config_converts_exposure_to_ms(self):
        legacy_main = load_legacy_main_module()
        exposure_spin = SpinBoxSpy()
        window = SimpleNamespace(
            sim_app_config=SimpleNamespace(
                camera=SimpleNamespace(
                    roi_x=5,
                    roi_y=6,
                    roi_width=1152,
                    roi_height=1152,
                    exposure_us=10_000,
                    bit_depth=16,
                ),
            ),
            ui=SimpleNamespace(
                spb_sCMOS_ROI_X=SpinBoxSpy(),
                spb_sCMOS_ROI_Y=SpinBoxSpy(),
                cmb_sCMOS_imageSize=ComboBoxSpy(),
                spb_sCMOS_exposureTime=exposure_spin,
                cmb_sCMOS_bitDepth=ComboBoxSpy(text="16-bit"),
            ),
        )
        window.sync_sim_camera_roi_position_controls = (
            lambda controls_enabled=None: legacy_main.MainWindow.sync_sim_camera_roi_position_controls(
                window,
                controls_enabled=controls_enabled,
            )
        )

        legacy_main.MainWindow.sync_sim_camera_controls_from_config(window)

        self.assertEqual(exposure_spin.value(), 10)

    def test_sync_sim_camera_controls_from_config_blocks_spinbox_signals_during_programmatic_updates(self):
        legacy_main = load_legacy_main_module()
        roi_x_spin = SignalBlockingSpinBoxSpy()
        roi_y_spin = SignalBlockingSpinBoxSpy()
        exposure_spin = SignalBlockingSpinBoxSpy()
        combo_box = ComboBoxSpy()
        bit_depth_combo = ComboBoxSpy(text="16-bit")
        window = SimpleNamespace(
            sim_app_config=SimpleNamespace(
                camera=SimpleNamespace(
                    roi_x=5,
                    roi_y=6,
                    roi_width=1152,
                    roi_height=1152,
                    exposure_us=10_000,
                    bit_depth=16,
                ),
            ),
            ui=SimpleNamespace(
                spb_sCMOS_ROI_X=roi_x_spin,
                spb_sCMOS_ROI_Y=roi_y_spin,
                cmb_sCMOS_imageSize=combo_box,
                spb_sCMOS_exposureTime=exposure_spin,
                cmb_sCMOS_bitDepth=bit_depth_combo,
            ),
        )
        window.sync_sim_camera_roi_position_controls = (
            lambda controls_enabled=None: legacy_main.MainWindow.sync_sim_camera_roi_position_controls(
                window,
                controls_enabled=controls_enabled,
            )
        )

        legacy_main.MainWindow.sync_sim_camera_controls_from_config(window)

        self.assertEqual(roi_x_spin.block_signals_calls, [True, False])
        self.assertEqual(roi_y_spin.block_signals_calls, [True, False])
        self.assertEqual(exposure_spin.block_signals_calls, [True, False])
        self.assertEqual(roi_x_spin.set_value_blocked_states, [True])
        self.assertEqual(roi_y_spin.set_value_blocked_states, [True])
        self.assertEqual(exposure_spin.set_value_blocked_states, [True])
        self.assertEqual(bit_depth_combo.block_signals_calls, [True, False])

    def test_sync_sim_camera_config_from_ui_converts_exposure_ms_to_us(self):
        legacy_main = load_legacy_main_module()
        camera = SimpleNamespace(
            roi_x=0,
            roi_y=0,
            roi_width=2304,
            roi_height=2304,
            exposure_us=0,
            timeout_ms=1000,
            device_index=0,
            device_label="",
            bit_depth=16,
        )
        window = SimpleNamespace(
            sim_app_config=SimpleNamespace(camera=camera, config_path="dummy.json"),
            sim_available_cameras=[],
            refresh_sim_settings_summary=lambda: None,
            ui=SimpleNamespace(
                cmb_sCMOS_imageSize=ComboBoxSpy(text="1152 x 1152", current_index=-1),
                spb_sCMOS_ROI_X=SpinBoxSpy(1),
                spb_sCMOS_ROI_Y=SpinBoxSpy(2),
                spb_sCMOS_exposureTime=SpinBoxSpy(10),
                cmb_sCMOS_bitDepth=ComboBoxSpy(text="12-bit"),
                cmb_sCMOS_camera=ComboBoxSpy(current_index=-1),
            ),
        )
        window.sync_sim_camera_roi_position_controls = (
            lambda controls_enabled=None: legacy_main.MainWindow.sync_sim_camera_roi_position_controls(
                window,
                controls_enabled=controls_enabled,
            )
        )

        legacy_main.MainWindow.sync_sim_camera_config_from_ui(window, save_to_disk=False)

        self.assertEqual(camera.roi_width, 1152)
        self.assertEqual(camera.roi_height, 1152)
        self.assertEqual(camera.exposure_us, 10_000)
        self.assertEqual(camera.bit_depth, 12)

    def test_sync_sim_camera_controls_from_config_updates_bit_depth_combo(self):
        legacy_main = load_legacy_main_module()
        bit_depth_combo = ComboBoxSpy(text="16-bit")
        bit_depth_combo.items = ["12-bit", "16-bit"]
        window = SimpleNamespace(
            sim_app_config=SimpleNamespace(
                camera=SimpleNamespace(
                    roi_x=5,
                    roi_y=6,
                    roi_width=1152,
                    roi_height=1152,
                    exposure_us=10_000,
                    bit_depth=12,
                ),
            ),
            ui=SimpleNamespace(
                spb_sCMOS_ROI_X=SpinBoxSpy(),
                spb_sCMOS_ROI_Y=SpinBoxSpy(),
                cmb_sCMOS_imageSize=ComboBoxSpy(),
                spb_sCMOS_exposureTime=SpinBoxSpy(),
                cmb_sCMOS_bitDepth=bit_depth_combo,
            ),
        )
        window.sync_sim_camera_roi_position_controls = (
            lambda controls_enabled=None: legacy_main.MainWindow.sync_sim_camera_roi_position_controls(
                window,
                controls_enabled=controls_enabled,
            )
        )

        legacy_main.MainWindow.sync_sim_camera_controls_from_config(window)

        self.assertEqual(bit_depth_combo.currentText(), "12-bit")

    def test_refresh_sim_camera_bit_depth_choices_includes_user_facing_values_and_fallbacks_to_16_bit(self):
        legacy_main = load_legacy_main_module()
        camera = SimpleNamespace(bit_depth=10)
        bit_depth_combo = ComboBoxSpy(text="16-bit")
        window = SimpleNamespace(
            sim_app_config=SimpleNamespace(camera=camera),
            ui=SimpleNamespace(cmb_sCMOS_bitDepth=bit_depth_combo),
            refresh_sim_settings_summary=lambda: None,
        )

        legacy_main.MainWindow.refresh_sim_camera_bit_depth_choices(window, [12, 16])

        self.assertEqual(bit_depth_combo.items, ["8-bit", "12-bit", "16-bit"])
        self.assertEqual(bit_depth_combo.currentText(), "16-bit")
        self.assertEqual(camera.bit_depth, 16)

    def test_apply_sim_camera_runtime_capabilities_refreshes_flash_roi_presets(self):
        legacy_main = load_legacy_main_module()
        camera = SimpleNamespace(
            roi_x=0,
            roi_y=0,
            roi_width=2304,
            roi_height=2304,
            exposure_us=20_000,
            bit_depth=16,
            timeout_ms=5000,
            device_index=0,
            device_label="0: C13440-20C [S/N: 305209]",
        )
        image_size_combo = ComboBoxSpy(text="2304 x 2304", enabled=True)
        roi_x_spin = SpinBoxSpy(0, maximum=0, enabled=False)
        roi_y_spin = SpinBoxSpy(0, maximum=0, enabled=False)
        window = SimpleNamespace(
            sim_app_config=SimpleNamespace(camera=camera, config_path="dummy.json"),
            refresh_sim_settings_summary=mock.Mock(),
            ui=SimpleNamespace(
                cmb_sCMOS_imageSize=image_size_combo,
                spb_sCMOS_ROI_X=roi_x_spin,
                spb_sCMOS_ROI_Y=roi_y_spin,
                spb_sCMOS_exposureTime=SpinBoxSpy(20),
            ),
        )
        window.sync_sim_camera_roi_position_controls = (
            lambda controls_enabled=None: legacy_main.MainWindow.sync_sim_camera_roi_position_controls(
                window,
                controls_enabled=controls_enabled,
            )
        )

        legacy_main.MainWindow.apply_sim_camera_runtime_capabilities(
            window,
            {
                "applied_roi": {"x": 0, "y": 0, "width": 2048, "height": 2048},
                "sensor_width": 2048,
                "sensor_height": 2048,
                "roi_step_px": 4,
                "roi_size_presets": [(2048, 2048), (1024, 1024), (512, 512)],
            },
            save_to_disk=False,
        )

        self.assertEqual(image_size_combo.items, ["2048 x 2048", "1024 x 1024", "512 x 512"])
        self.assertEqual(image_size_combo.currentText(), "2048 x 2048")
        self.assertEqual(camera.roi_width, 2048)
        self.assertEqual(camera.roi_height, 2048)
        self.assertEqual(roi_x_spin.maximum(), 0)
        self.assertEqual(roi_y_spin.maximum(), 0)
        self.assertFalse(roi_x_spin.isEnabled())
        self.assertFalse(roi_y_spin.isEnabled())

    def test_connect_button_persists_applied_flash_roi_from_runtime_payload(self):
        legacy_main = load_legacy_main_module()
        camera = SimpleNamespace(
            roi_x=0,
            roi_y=0,
            roi_width=2304,
            roi_height=2304,
            exposure_us=20_000,
            timeout_ms=1000,
            device_index=0,
            device_label="",
            bit_depth=16,
        )
        controller = SimpleNamespace(
            connect_camera=mock.Mock(return_value={"supported_bit_depths": [12, 16]}),
            apply_camera_config=mock.Mock(
                return_value={
                    "applied_roi": {"x": 0, "y": 0, "width": 2048, "height": 2048},
                    "sensor_width": 2048,
                    "sensor_height": 2048,
                    "roi_step_px": 4,
                    "roi_size_presets": [(2048, 2048), (1024, 1024), (512, 512)],
                    "supported_bit_depths": [12, 16],
                }
            ),
            disconnect_camera=mock.Mock(),
        )
        image_size_combo = ComboBoxSpy(text="2304 x 2304", enabled=True)
        window = SimpleNamespace(
            sim_camera_connected=False,
            sim_available_cameras=[{"index": 0, "display": "0: C13440-20C [S/N: 305209]"}],
            sim_app_config=SimpleNamespace(camera=camera, config_path="dummy.json"),
            sim_acquisition_controller=controller,
            ensure_sim_runtime=lambda: None,
            set_sim_camera_controls_enabled=lambda enabled: None,
            update_sim_camera_action_buttons=lambda: None,
            refresh_sim_settings_summary=lambda: None,
            sim_runtime_timing_snapshot={},
            ui=SimpleNamespace(
                btn_sCMOS_connection=mock.MagicMock(),
                cmb_sCMOS_camera=ComboBoxSpy(current_index=0),
                cmb_sCMOS_imageSize=image_size_combo,
                spb_sCMOS_ROI_X=SpinBoxSpy(0, maximum=0, enabled=False),
                spb_sCMOS_ROI_Y=SpinBoxSpy(0, maximum=0, enabled=False),
                spb_sCMOS_exposureTime=SpinBoxSpy(20),
                cmb_sCMOS_bitDepth=ComboBoxSpy(text="16-bit"),
            ),
        )
        window.sync_sim_camera_roi_position_controls = (
            lambda controls_enabled=None: legacy_main.MainWindow.sync_sim_camera_roi_position_controls(
                window,
                controls_enabled=controls_enabled,
            )
        )
        window.sync_sim_camera_config_from_ui = (
            lambda save_to_disk=True: legacy_main.MainWindow.sync_sim_camera_config_from_ui(
                window,
                save_to_disk=save_to_disk,
            )
        )

        with mock.patch.object(legacy_main, "save_app_config") as save_mock:
            legacy_main.MainWindow.btn_sCMOS_connection_function(window)
            from PyQt5.QtTest import QTest
            QTest.qWait(200)

        self.assertTrue(window.sim_camera_connected)
        self.assertEqual(camera.roi_width, 2048)
        self.assertEqual(camera.roi_height, 2048)
        self.assertEqual(image_size_combo.items, ["2048 x 2048", "1024 x 1024", "512 x 512"])
        save_mock.assert_called()

    def test_slot_handle_sim_acquisition_status_refreshes_summary_from_runtime_timing(self):
        legacy_main = load_legacy_main_module()
        refresh_calls = []
        window = SimpleNamespace(
            sim_current_task_id="",
            sim_runtime_timing_snapshot={},
            refresh_sim_settings_summary=lambda: refresh_calls.append("refreshed"),
        )

        legacy_main.MainWindow.slot_handle_sim_acquisition_status(
            window,
            "camera_config_applied",
            {
                "timing_readout_time_s": 0.00561,
                "recommended_inter_frame_gap_us": 6500,
                "applied_bit_depth": 12,
            },
        )

        self.assertEqual(window.sim_runtime_timing_snapshot["timing_readout_time_s"], 0.00561)
        self.assertEqual(window.sim_runtime_timing_snapshot["recommended_inter_frame_gap_us"], 6500)
        self.assertEqual(window.sim_runtime_timing_snapshot["applied_bit_depth"], 12)
        self.assertEqual(refresh_calls, ["refreshed"])

    def test_sync_sim_camera_controls_from_config_resets_full_frame_origin_and_disables_roi_inputs(self):
        legacy_main = load_legacy_main_module()
        roi_x_spin = SignalBlockingSpinBoxSpy(value=270, maximum=2304, enabled=True)
        roi_y_spin = SignalBlockingSpinBoxSpy(value=245, maximum=2304, enabled=True)
        combo_box = ComboBoxSpy(enabled=True)
        window = SimpleNamespace(
            sim_app_config=SimpleNamespace(
                camera=SimpleNamespace(roi_x=270, roi_y=245, roi_width=2304, roi_height=2304, exposure_us=10_000),
            ),
            ui=SimpleNamespace(
                spb_sCMOS_ROI_X=roi_x_spin,
                spb_sCMOS_ROI_Y=roi_y_spin,
                cmb_sCMOS_imageSize=combo_box,
                spb_sCMOS_exposureTime=SignalBlockingSpinBoxSpy(),
            ),
        )
        window.normalize_sim_camera_config = lambda: legacy_main.MainWindow.normalize_sim_camera_config(window)
        window.sync_sim_camera_roi_position_controls = (
            lambda controls_enabled=None: legacy_main.MainWindow.sync_sim_camera_roi_position_controls(
                window,
                controls_enabled=controls_enabled,
            )
        )

        legacy_main.MainWindow.sync_sim_camera_controls_from_config(window)

        self.assertEqual(window.sim_app_config.camera.roi_x, 0)
        self.assertEqual(window.sim_app_config.camera.roi_y, 0)
        self.assertEqual(roi_x_spin.value(), 0)
        self.assertEqual(roi_y_spin.value(), 0)
        self.assertEqual(roi_x_spin.maximum(), 0)
        self.assertEqual(roi_y_spin.maximum(), 0)
        self.assertFalse(roi_x_spin.isEnabled())
        self.assertFalse(roi_y_spin.isEnabled())

    def test_sync_sim_camera_controls_from_config_aligns_partial_frame_origin_to_four_pixel_grid(self):
        legacy_main = load_legacy_main_module()
        roi_x_spin = SignalBlockingSpinBoxSpy(value=270, maximum=2304, enabled=True)
        roi_y_spin = SignalBlockingSpinBoxSpy(value=245, maximum=2304, enabled=True)
        combo_box = ComboBoxSpy(enabled=True)
        window = SimpleNamespace(
            sim_app_config=SimpleNamespace(
                camera=SimpleNamespace(roi_x=270, roi_y=245, roi_width=1152, roi_height=1152, exposure_us=10_000),
            ),
            ui=SimpleNamespace(
                spb_sCMOS_ROI_X=roi_x_spin,
                spb_sCMOS_ROI_Y=roi_y_spin,
                cmb_sCMOS_imageSize=combo_box,
                spb_sCMOS_exposureTime=SignalBlockingSpinBoxSpy(),
            ),
        )
        window.sync_sim_camera_roi_position_controls = (
            lambda controls_enabled=None: legacy_main.MainWindow.sync_sim_camera_roi_position_controls(
                window,
                controls_enabled=controls_enabled,
            )
        )

        legacy_main.MainWindow.sync_sim_camera_controls_from_config(window)

        self.assertEqual(window.sim_app_config.camera.roi_x, 268)
        self.assertEqual(window.sim_app_config.camera.roi_y, 244)
        self.assertEqual(roi_x_spin.value(), 268)
        self.assertEqual(roi_y_spin.value(), 244)
        self.assertEqual(roi_x_spin.maximum(), 1152)
        self.assertEqual(roi_y_spin.maximum(), 1152)
        self.assertTrue(roi_x_spin.isEnabled())
        self.assertTrue(roi_y_spin.isEnabled())

    def test_sync_sim_camera_config_from_ui_clamps_partial_frame_origin_and_keeps_inputs_enabled(self):
        legacy_main = load_legacy_main_module()
        camera = SimpleNamespace(
            roi_x=0,
            roi_y=0,
            roi_width=2304,
            roi_height=2304,
            exposure_us=0,
            timeout_ms=1000,
            device_index=0,
            device_label="",
        )
        roi_x_spin = SpinBoxSpy(2000, maximum=2304, enabled=True)
        roi_y_spin = SpinBoxSpy(1500, maximum=2304, enabled=True)
        combo_box = ComboBoxSpy(text="1152 x 1152", current_index=-1, enabled=True)
        window = SimpleNamespace(
            sim_app_config=SimpleNamespace(camera=camera, config_path="dummy.json"),
            sim_available_cameras=[],
            refresh_sim_settings_summary=lambda: None,
            ui=SimpleNamespace(
                cmb_sCMOS_imageSize=combo_box,
                spb_sCMOS_ROI_X=roi_x_spin,
                spb_sCMOS_ROI_Y=roi_y_spin,
                spb_sCMOS_exposureTime=SpinBoxSpy(10),
                cmb_sCMOS_camera=ComboBoxSpy(current_index=-1),
            ),
        )
        window.sync_sim_camera_roi_position_controls = (
            lambda controls_enabled=None: legacy_main.MainWindow.sync_sim_camera_roi_position_controls(
                window,
                controls_enabled=controls_enabled,
            )
        )

        legacy_main.MainWindow.sync_sim_camera_config_from_ui(window, save_to_disk=False)

        self.assertEqual(camera.roi_width, 1152)
        self.assertEqual(camera.roi_height, 1152)
        self.assertEqual(camera.roi_x, 1152)
        self.assertEqual(camera.roi_y, 1152)
        self.assertEqual(roi_x_spin.value(), 1152)
        self.assertEqual(roi_y_spin.value(), 1152)
        self.assertEqual(roi_x_spin.maximum(), 1152)
        self.assertEqual(roi_y_spin.maximum(), 1152)
        self.assertTrue(roi_x_spin.isEnabled())
        self.assertTrue(roi_y_spin.isEnabled())

    def test_sync_sim_camera_config_from_ui_for_full_frame_forces_zero_origin(self):
        legacy_main = load_legacy_main_module()
        camera = SimpleNamespace(
            roi_x=120,
            roi_y=80,
            roi_width=1152,
            roi_height=1152,
            exposure_us=0,
            timeout_ms=1000,
            device_index=0,
            device_label="",
        )
        roi_x_spin = SpinBoxSpy(270, maximum=2304, enabled=True)
        roi_y_spin = SpinBoxSpy(245, maximum=2304, enabled=True)
        combo_box = ComboBoxSpy(text="2304 x 2304", current_index=-1, enabled=True)
        window = SimpleNamespace(
            sim_app_config=SimpleNamespace(camera=camera, config_path="dummy.json"),
            sim_available_cameras=[],
            refresh_sim_settings_summary=lambda: None,
            ui=SimpleNamespace(
                cmb_sCMOS_imageSize=combo_box,
                spb_sCMOS_ROI_X=roi_x_spin,
                spb_sCMOS_ROI_Y=roi_y_spin,
                spb_sCMOS_exposureTime=SpinBoxSpy(20),
                cmb_sCMOS_camera=ComboBoxSpy(current_index=-1),
            ),
        )
        window.sync_sim_camera_roi_position_controls = (
            lambda controls_enabled=None: legacy_main.MainWindow.sync_sim_camera_roi_position_controls(
                window,
                controls_enabled=controls_enabled,
            )
        )

        legacy_main.MainWindow.sync_sim_camera_config_from_ui(window, save_to_disk=False)

        self.assertEqual(camera.roi_width, 2304)
        self.assertEqual(camera.roi_height, 2304)
        self.assertEqual(camera.roi_x, 0)
        self.assertEqual(camera.roi_y, 0)
        self.assertEqual(roi_x_spin.value(), 0)
        self.assertEqual(roi_y_spin.value(), 0)
        self.assertEqual(roi_x_spin.maximum(), 0)
        self.assertEqual(roi_y_spin.maximum(), 0)
        self.assertFalse(roi_x_spin.isEnabled())
        self.assertFalse(roi_y_spin.isEnabled())

    def test_connect_button_normalizes_full_frame_origin_before_apply_camera_config(self):
        legacy_main = load_legacy_main_module()
        camera = SimpleNamespace(
            roi_x=270,
            roi_y=245,
            roi_width=1152,
            roi_height=1152,
            exposure_us=20_000,
            timeout_ms=1000,
            device_index=0,
            device_label="",
        )
        controller = SimpleNamespace(
            connect_camera=mock.Mock(),
            apply_camera_config=mock.Mock(),
            disconnect_camera=mock.Mock(),
        )
        window = SimpleNamespace(
            sim_camera_connected=False,
            sim_available_cameras=[{"index": 0, "display": "0: C15440-20UP [S/N: 501679]"}],
            sim_app_config=SimpleNamespace(camera=camera, config_path="dummy.json"),
            sim_acquisition_controller=controller,
            ensure_sim_runtime=lambda: None,
            set_sim_camera_controls_enabled=lambda enabled: None,
            update_sim_camera_action_buttons=lambda: None,
            refresh_sim_settings_summary=lambda: None,
            ui=SimpleNamespace(
                btn_sCMOS_connection=mock.MagicMock(),
                cmb_sCMOS_camera=ComboBoxSpy(current_index=0),
                cmb_sCMOS_imageSize=ComboBoxSpy(text="2304 x 2304", enabled=True),
                spb_sCMOS_ROI_X=SpinBoxSpy(270, maximum=2304, enabled=True),
                spb_sCMOS_ROI_Y=SpinBoxSpy(245, maximum=2304, enabled=True),
                spb_sCMOS_exposureTime=SpinBoxSpy(20),
            ),
        )
        window.sync_sim_camera_roi_position_controls = (
            lambda controls_enabled=None: legacy_main.MainWindow.sync_sim_camera_roi_position_controls(
                window,
                controls_enabled=controls_enabled,
            )
        )
        window.sync_sim_camera_config_from_ui = (
            lambda save_to_disk=True: legacy_main.MainWindow.sync_sim_camera_config_from_ui(
                window,
                save_to_disk=save_to_disk,
            )
        )

        legacy_main.MainWindow.btn_sCMOS_connection_function(window)
        from PyQt5.QtTest import QTest
        QTest.qWait(200)

        self.assertEqual(camera.roi_width, 2304)
        self.assertEqual(camera.roi_height, 2304)
        self.assertEqual(camera.roi_x, 0)
        self.assertEqual(camera.roi_y, 0)
        controller.apply_camera_config.assert_called_once()
        applied_camera = controller.apply_camera_config.call_args[0][0]
        self.assertEqual(applied_camera.roi_x, 0)
        self.assertEqual(applied_camera.roi_y, 0)

    def test_connect_button_disconnects_camera_when_apply_camera_config_fails(self):
        legacy_main = load_legacy_main_module()
        camera = SimpleNamespace(
            roi_x=270,
            roi_y=245,
            roi_width=1152,
            roi_height=1152,
            exposure_us=20_000,
            timeout_ms=1000,
            device_index=0,
            device_label="",
        )
        controller = SimpleNamespace(
            connect_camera=mock.Mock(),
            apply_camera_config=mock.Mock(side_effect=RuntimeError("INVALIDPARAM")),
            disconnect_camera=mock.Mock(),
        )
        set_controls_calls = []
        action_update_calls = []
        window = SimpleNamespace(
            sim_camera_connected=False,
            sim_available_cameras=[{"index": 0, "display": "0: C15440-20UP [S/N: 501679]"}],
            sim_app_config=SimpleNamespace(camera=camera, config_path="dummy.json"),
            sim_acquisition_controller=controller,
            ensure_sim_runtime=lambda: None,
            set_sim_camera_controls_enabled=lambda enabled: set_controls_calls.append(enabled),
            update_sim_camera_action_buttons=lambda: action_update_calls.append("updated"),
            refresh_sim_settings_summary=lambda: None,
            ui=SimpleNamespace(
                btn_sCMOS_connection=mock.MagicMock(),
                cmb_sCMOS_camera=ComboBoxSpy(current_index=0),
                cmb_sCMOS_imageSize=ComboBoxSpy(text="1152 x 1152", enabled=True),
                spb_sCMOS_ROI_X=SpinBoxSpy(270, maximum=2304, enabled=True),
                spb_sCMOS_ROI_Y=SpinBoxSpy(245, maximum=2304, enabled=True),
                spb_sCMOS_exposureTime=SpinBoxSpy(20),
            ),
        )
        window.sync_sim_camera_roi_position_controls = (
            lambda controls_enabled=None: legacy_main.MainWindow.sync_sim_camera_roi_position_controls(
                window,
                controls_enabled=controls_enabled,
            )
        )
        window.sync_sim_camera_config_from_ui = (
            lambda save_to_disk=True: legacy_main.MainWindow.sync_sim_camera_config_from_ui(
                window,
                save_to_disk=save_to_disk,
            )
        )

        with mock.patch.object(legacy_main.qw.QMessageBox, "warning"):
            legacy_main.MainWindow.btn_sCMOS_connection_function(window)
            from PyQt5.QtTest import QTest
            QTest.qWait(200)

        controller.disconnect_camera.assert_called_once()
        self.assertFalse(window.sim_camera_connected)
        self.assertIn(False, set_controls_calls)
        self.assertTrue(action_update_calls)


class SimPreviewControllerTests(unittest.TestCase):
    """``sim_control/preview.py`` 中 ``SimPreviewWorker`` / ``SimPreviewController`` 的状态机测试。

    覆盖：
        - ``prepare_for_start`` 清取消标志 + 清最新快照。
        - ``request_stop`` 线程安全清快照。
        - ``publish_preview_frame`` 单调递增 sequence、覆盖旧快照。
        - ``take_latest_frame`` "取走即清"（latest-frame-wins）。
        - ``SimPreviewController.start`` 拒绝在 active/stopping 时重入。
    """

    """验证预览 controller 与 worker 信号、线程状态之间的协作。"""
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance()
        if cls.app is None:
            cls.app = QtWidgets.QApplication([])

    def test_start_does_not_force_a_blocking_stop(self):
        from sim_control.models import CameraConfig
        from sim_control.preview import SimPreviewController

        class FakeCamera:
            preview_active = False

        controller = SimPreviewController(FakeCamera())
        controller.signal_start_worker.disconnect()
        controller.signal_stop_worker.disconnect()
        controller.stop = mock.Mock()

        try:
            controller.start(CameraConfig(), timeout_ms=200)
        finally:
            controller._thread.quit()
            controller._thread.wait(2000)

        controller.stop.assert_not_called()

    def test_worker_take_latest_frame_returns_only_newest_snapshot_once(self):
        from sim_control.preview import SimPreviewWorker

        worker = SimPreviewWorker()

        with mock.patch("sim_control.preview.time.perf_counter", side_effect=[1.0, 1.1, 1.2]):
            worker.publish_preview_frame(np.array([[1]], dtype=np.uint16), fps=1)
            worker.publish_preview_frame(np.array([[2]], dtype=np.uint16), fps=2)
            worker.publish_preview_frame(np.array([[3]], dtype=np.uint16), fps=3)

        snapshot = worker.take_latest_frame()

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.fps, 3)
        self.assertEqual(snapshot.sequence, 3)
        self.assertEqual(snapshot.captured_at, 1.2)
        np.testing.assert_array_equal(snapshot.frame, np.array([[3]], dtype=np.uint16))
        self.assertIsNone(worker.take_latest_frame())

    def test_controller_take_latest_frame_returns_latest_snapshot_and_clears_store(self):
        from sim_control.preview import SimPreviewController

        class FakeCamera:
            preview_active = False

        controller = SimPreviewController(FakeCamera(), gui_preview_fps_limit=25)

        try:
            with mock.patch("sim_control.preview.time.perf_counter", side_effect=[2.0, 2.2]):
                controller._worker.publish_preview_frame(np.array([[10]], dtype=np.uint16), fps=10)
                controller._worker.publish_preview_frame(np.array([[20]], dtype=np.uint16), fps=11)

            snapshot = controller.take_latest_frame()

            self.assertEqual(controller.frame_poll_interval_ms, 40)
            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot.fps, 11)
            self.assertEqual(snapshot.sequence, 2)
            self.assertEqual(snapshot.captured_at, 2.2)
            np.testing.assert_array_equal(snapshot.frame, np.array([[20]], dtype=np.uint16))
            self.assertIsNone(controller.take_latest_frame())
        finally:
            controller._thread.quit()
            controller._thread.wait(2000)

    def test_stop_wait_false_eventually_emits_preview_stopped(self):
        from sim_control.models import CameraConfig
        from sim_control.preview import SimPreviewController

        class FakeCamera:
            def __init__(self):
                self.preview_active = False
                self.stop_calls = 0

            def start_preview(self, config):
                self.preview_active = True

            def read_preview_frame(self, timeout_ms=100):
                time.sleep(0.02)
                return [[0]]

            def stop_preview(self):
                self.preview_active = False
                self.stop_calls += 1

        camera = FakeCamera()
        controller = SimPreviewController(camera)
        statuses = []
        controller.signal_status_changed.connect(lambda status, payload: statuses.append(status))

        try:
            controller.start(CameraConfig(), timeout_ms=10)
            QtTest.QTest.qWait(100)

            controller.stop(wait=False)

            deadline = time.time() + 0.5
            while "preview_stopped" not in statuses and time.time() < deadline:
                self.app.processEvents()
                QtTest.QTest.qWait(10)

            self.assertIn("preview_started", statuses)
            self.assertIn("preview_stopped", statuses)
            self.assertFalse(controller.stopping)
            self.assertFalse(camera.preview_active)
        finally:
            controller._worker.slot_stop()
            deadline = time.time() + 0.5
            while camera.preview_active and time.time() < deadline:
                self.app.processEvents()
                QtTest.QTest.qWait(10)
            controller._thread.quit()
            controller._thread.wait(2000)


class SimPreviewPollingTests(unittest.TestCase):
    """GUI 主界面 QTimer 轮询 ``take_latest_frame`` 的回归测试。

    覆盖：
        - QTimer interval 与 ``gui_preview_fps_limit`` 一致。
        - 轮询取到 None 时不应崩溃也不应刷新图像。
        - 取到帧后调用 ``preview_contrast.fast_*`` 走显示压缩链路。
    """

    """验证主界面轮询 latest-frame-wins 预览快照时的显示行为。"""
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance()
        if cls.app is None:
            cls.app = QtWidgets.QApplication([])

    def test_poll_latest_sim_preview_frame_renders_only_latest_snapshot(self):
        legacy_main = load_legacy_main_module()
        from sim_control.preview import SimPreviewController

        class FakeCamera:
            preview_active = False

        controller = SimPreviewController(FakeCamera())
        rendered_frames = []
        fps_label = LabelSpy()
        window = SimpleNamespace(
            sim_preview_controller=controller,
            sim_last_preview_sequence=-1,
            _render_sim_preview_frame=lambda frame, copy_cached_frame=True: rendered_frames.append(frame.copy()),
            ui=SimpleNamespace(lb_sCMOS_FPSshow=fps_label),
        )

        try:
            with mock.patch("sim_control.preview.time.perf_counter", side_effect=[3.0, 3.1, 3.2]):
                controller._worker.publish_preview_frame(np.array([[1]], dtype=np.uint16), fps=1)
                controller._worker.publish_preview_frame(np.array([[2]], dtype=np.uint16), fps=2)
                controller._worker.publish_preview_frame(np.array([[3]], dtype=np.uint16), fps=3)

            legacy_main.MainWindow.poll_latest_sim_preview_frame(window)
            legacy_main.MainWindow.poll_latest_sim_preview_frame(window)

            self.assertEqual(window.sim_last_preview_sequence, 3)
            self.assertEqual(fps_label.text, "3")
            self.assertEqual(len(rendered_frames), 1)
            np.testing.assert_array_equal(rendered_frames[0], np.array([[3]], dtype=np.uint16))
        finally:
            controller._thread.quit()
            controller._thread.wait(2000)

    def test_render_sim_preview_frame_uses_manual_contrast_when_auto_is_unchecked(self):
        legacy_main = load_legacy_main_module()
        label = QtWidgets.QLabel()
        label.resize(16, 16)
        frame = np.array([[0, 1000], [2000, 3000]], dtype=np.uint16)
        manual_result = np.array([[0, 85], [170, 255]], dtype=np.uint8)
        calls = []
        window = SimpleNamespace(
            ui=SimpleNamespace(
                lb_sCMOS_cameraView=label,
                spb_sCMOS_displayGray_max=SimpleNamespace(value=lambda: 3000),
                chb_sCMOS_autoContrast=SimpleNamespace(isChecked=lambda: False),
            ),
            sim_auto_contrast_state=object(),
            sim_last_preview_frame=None,
        )

        with mock.patch.object(
            legacy_main,
            "fast_preview_uint16_to_uint8",
            side_effect=lambda input_frame, output_size, **kwargs: calls.append(
                ("fast", input_frame.copy(), output_size, kwargs)
            )
            or manual_result,
        ) as fast:
            legacy_main.MainWindow._render_sim_preview_frame(window, frame)

        fast.assert_called_once()
        self.assertEqual(calls[0][2], (16, 16))
        self.assertEqual(calls[0][3]["gray_max"], 3000)
        self.assertFalse(calls[0][3]["auto_contrast"])
        np.testing.assert_array_equal(window.sim_last_preview_frame, frame)

    def test_render_sim_preview_frame_uses_auto_contrast_when_checked(self):
        legacy_main = load_legacy_main_module()
        label = QtWidgets.QLabel()
        label.resize(16, 16)
        frame = np.array([[0, 1000], [2000, 3000]], dtype=np.uint16)
        auto_result = np.array([[0, 10], [200, 255]], dtype=np.uint8)
        state = object()
        window = SimpleNamespace(
            ui=SimpleNamespace(
                lb_sCMOS_cameraView=label,
                spb_sCMOS_displayGray_max=SimpleNamespace(value=lambda: 3000),
                chb_sCMOS_autoContrast=SimpleNamespace(isChecked=lambda: True),
            ),
            sim_auto_contrast_state=state,
            sim_last_preview_frame=None,
        )

        with mock.patch.object(
            legacy_main,
            "fast_preview_uint16_to_uint8",
            side_effect=lambda input_frame, output_size, **kwargs: auto_result,
        ) as fast:
            legacy_main.MainWindow._render_sim_preview_frame(window, frame)

        fast.assert_called_once()
        self.assertEqual(fast.call_args.args[1], (16, 16))
        self.assertIs(fast.call_args.kwargs["auto_state"], state)
        self.assertTrue(fast.call_args.kwargs["auto_contrast"])
        np.testing.assert_array_equal(window.sim_last_preview_frame, frame)


class SimCameraSpinBoxCommitTests(unittest.TestCase):
    """ROI / 曝光 / bit_depth SpinBox 编辑后提交时机的回归测试。

    覆盖：
        - 失去焦点 / Enter 键提交时同步刷新 AppConfig 与摘要。
        - live preview 期间禁用部分字段，停止后恢复可编辑。
    """

    """验证 SIM ROI 和曝光 spinbox 只在确认输入后提交值。"""
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance()
        if cls.app is None:
            cls.app = QtWidgets.QApplication([])

    def test_snapping_exposure_spinbox_uses_bucketed_step_sequence(self):
        legacy_main = load_legacy_main_module()
        spinbox = legacy_main.SnappingExposureSpinBox()
        spinbox.setRange(1, 10_000)

        cases = [
            (10, 1, 20),
            (10, -1, 9),
            (11, -1, 10),
            (15, 1, 20),
            (49, 1, 50),
            (51, -1, 50),
            (1, -1, 1),
            (10_000, 1, 10_000),
        ]
        for start, steps, expected in cases:
            with self.subTest(start=start, steps=steps):
                spinbox.setValue(start)
                spinbox.stepBy(steps)
                self.assertEqual(spinbox.value(), expected)

    def test_configure_sim_camera_spinboxes_disables_keyboard_tracking(self):
        legacy_main = load_legacy_main_module()
        window = SimpleNamespace(
            ui=SimpleNamespace(
                spb_sCMOS_ROI_X=QtWidgets.QSpinBox(),
                spb_sCMOS_ROI_Y=QtWidgets.QSpinBox(),
                spb_sCMOS_exposureTime=QtWidgets.QSpinBox(),
            ),
        )

        configurator = getattr(legacy_main.MainWindow, "configure_sim_camera_spinboxes", None)
        self.assertTrue(callable(configurator), "MainWindow should expose a SIM spinbox configurator.")

        configurator(window)

        self.assertFalse(window.ui.spb_sCMOS_ROI_X.keyboardTracking())
        self.assertFalse(window.ui.spb_sCMOS_ROI_Y.keyboardTracking())
        self.assertFalse(window.ui.spb_sCMOS_exposureTime.keyboardTracking())
        self.assertEqual(window.ui.spb_sCMOS_ROI_X.singleStep(), 4)
        self.assertEqual(window.ui.spb_sCMOS_ROI_Y.singleStep(), 4)

    def test_configured_sim_spinboxes_commit_only_after_enter_or_focus_loss(self):
        legacy_main = load_legacy_main_module()
        parent = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(parent)
        roi_x_spin = QtWidgets.QSpinBox()
        roi_y_spin = QtWidgets.QSpinBox()
        exposure_spin = QtWidgets.QSpinBox()
        other_field = QtWidgets.QLineEdit()
        for widget in (roi_x_spin, roi_y_spin, exposure_spin):
            widget.setRange(0, 9999)
            layout.addWidget(widget)
        layout.addWidget(other_field)
        window = SimpleNamespace(
            ui=SimpleNamespace(
                spb_sCMOS_ROI_X=roi_x_spin,
                spb_sCMOS_ROI_Y=roi_y_spin,
                spb_sCMOS_exposureTime=exposure_spin,
            ),
        )

        configurator = getattr(legacy_main.MainWindow, "configure_sim_camera_spinboxes", None)
        self.assertTrue(callable(configurator), "MainWindow should expose a SIM spinbox configurator.")
        configurator(window)

        roi_x_events = []
        roi_y_events = []
        roi_x_spin.valueChanged.connect(roi_x_events.append)
        roi_y_spin.valueChanged.connect(roi_y_events.append)

        parent.show()
        self.app.processEvents()

        roi_x_spin.setFocus()
        roi_x_spin.lineEdit().selectAll()
        QtTest.QTest.keyClicks(roi_x_spin.lineEdit(), "123")
        self.app.processEvents()
        self.assertEqual(roi_x_events, [])

        QtTest.QTest.keyClick(roi_x_spin.lineEdit(), QtCore.Qt.Key_Return)
        self.app.processEvents()
        self.assertEqual(roi_x_events, [123])

        roi_y_spin.setFocus()
        roi_y_spin.lineEdit().selectAll()
        QtTest.QTest.keyClicks(roi_y_spin.lineEdit(), "456")
        self.app.processEvents()
        self.assertEqual(roi_y_events, [])

        other_field.setFocus()
        self.app.processEvents()
        self.assertEqual(roi_y_events, [456])

        parent.close()


class DaqTestRunnerTests(unittest.TestCase):
    """``sim_control/daq_testing.py`` 中 ``DaqTestRunner`` DAQ 诊断测试执行的回归测试。

    背景：
        原 ``SimSettingsDialog`` 已删除，其 ``_run_*_test`` 逻辑原样搬迁到
        :class:`~sim_control.daq_testing.DaqTestRunner`；本类直接构造 runner（注入
        与 controller 共享的 mock 相机 / SLM / DAQ 适配器）验证测试执行行为。

    覆盖：
        - 相机触发测试复用外部相机（不重开 DCAM），测试结束只 disarm 不 disconnect；
          测试前未连接的相机则顺手 disconnect。
        - SIM9 采集测试在共享 SLM 上选 RO、强制 500 ms 曝光而不污染 config。
        - ``stop_event`` 被透传到 ``read_frame_sequence`` / ``pulse_line`` / ``play_waveform``。
        - ``run_test`` 分派 SIM 采集并生成结果消息字符串。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance()
        if cls.app is None:
            cls.app = QtWidgets.QApplication([])

    def test_sim_acquisition_test_selects_running_order_on_shared_slm(self):
        from sim_control.daq_testing import DaqTestRunner
        from sim_control.models import AppConfig

        slm_adapter = mock.Mock()
        slm_adapter.is_connected.return_value = True
        slm_adapter.list_running_orders.return_value = [
            (0, "488_3.5_2d_10ms"),
            (1, "488_3.5_2d_50ms"),
        ]
        slm_adapter.select_running_order.return_value = {
            "running_order_name": "488_3.5_2d_50ms",
            "pattern_result": mock.Mock(pattern_files=["488_3.5_2d_50ms"] * 9, handles=[-1]),
        }
        camera = mock.Mock()
        camera.is_connected.return_value = True
        camera.read_frame_sequence.return_value = (np.zeros((9, 2, 2), dtype=np.uint16), [])
        runner = DaqTestRunner(
            camera_adapter=camera,
            slm_adapter=slm_adapter,
            daq_adapter=mock.Mock(),
            config=AppConfig(),
            selected_laser_nm=488,
        )
        with mock.patch.object(runner, "_test_capture_path", return_value=Path("dummy.tiff")), mock.patch.object(
            runner,
            "_write_uint16_tiff",
        ):
            result = runner._run_sim_acquisition_test(runner.config.daq)

        self.assertEqual(result.output_path, Path("dummy.tiff"))
        slm_adapter.select_running_order.assert_called_once_with(1)

    def test_sim_acquisition_test_uses_external_camera_adapter(self):
        from sim_control.daq_testing import DaqTestRunner
        from sim_control.models import AppConfig

        slm_adapter = mock.Mock()
        slm_adapter.is_connected.return_value = True
        slm_adapter.list_running_orders.return_value = [
            (0, "488_3.5_2d_10ms"),
            (1, "488_3.5_2d_50ms"),
        ]
        slm_adapter.select_running_order.return_value = {
            "running_order_name": "488_3.5_2d_50ms",
            "pattern_result": mock.Mock(pattern_files=["488_3.5_2d_50ms"] * 9, handles=[-1]),
        }
        camera_adapter = mock.Mock()
        camera_adapter.is_connected.return_value = True
        camera_adapter.read_frame_sequence.return_value = (np.zeros((9, 2, 2), dtype=np.uint16), [])
        runner = DaqTestRunner(
            camera_adapter=camera_adapter,
            slm_adapter=slm_adapter,
            daq_adapter=mock.Mock(),
            config=AppConfig(),
            selected_laser_nm=488,
        )

        with mock.patch.object(runner, "_test_capture_path", return_value=Path("dummy.tiff")), mock.patch.object(
            runner,
            "_write_uint16_tiff",
        ):
            result = runner._run_sim_acquisition_test(runner.config.daq)

        self.assertEqual(result.output_path, Path("dummy.tiff"))
        camera_adapter.apply_config.assert_called_once()
        camera_adapter.arm.assert_called_once_with(frame_count=9)
        camera_adapter.disarm.assert_called_once_with()
        camera_adapter.disconnect.assert_not_called()

    def test_sim_acquisition_test_forces_500ms_exposure_without_mutating_config(self):
        from sim_control.daq_testing import DaqTestRunner
        from sim_control.models import AppConfig, CameraConfig, TimingConfig

        config = AppConfig(
            camera=CameraConfig(exposure_us=20_000),
            timing=TimingConfig(inter_frame_gap_us=10_000),
        )
        slm_adapter = mock.Mock()
        slm_adapter.is_connected.return_value = True
        slm_adapter.list_running_orders.return_value = [
            (0, "488_3.5_2d_10ms"),
            (1, "488_3.5_2d_50ms"),
        ]
        slm_adapter.select_running_order.return_value = {
            "running_order_name": "488_3.5_2d_50ms",
            "pattern_result": mock.Mock(pattern_files=["488_3.5_2d_50ms"] * 9, handles=[-1]),
        }
        camera_adapter = mock.Mock()
        camera_adapter.is_connected.return_value = True
        camera_adapter.read_frame_sequence.return_value = (np.zeros((9, 2, 2), dtype=np.uint16), [])
        waveform_builder = mock.Mock()
        waveform_plan = SimpleNamespace(duration_s=4.5001)
        waveform_builder.build.return_value = waveform_plan
        daq_adapter = mock.Mock()
        runner = DaqTestRunner(
            camera_adapter=camera_adapter,
            slm_adapter=slm_adapter,
            daq_adapter=daq_adapter,
            config=config,
            selected_laser_nm=488,
        )

        with mock.patch.object(
            runner,
            "_test_capture_path",
            return_value=Path("dummy.tiff"),
        ) as capture_path, mock.patch.object(
            runner,
            "_write_uint16_tiff",
        ), mock.patch(
            "sim_control.daq_testing.NIDaqWaveformBuilder",
            return_value=waveform_builder,
        ), mock.patch(
            "time.perf_counter",
            return_value=11.25,
        ):
            result = runner._run_sim_acquisition_test(runner.config.daq, acquisition_started_at_s=10.0)

        self.assertEqual(result.output_path, Path("dummy.tiff"))
        self.assertAlmostEqual(result.actual_acquisition_duration_s, 1.25)
        self.assertAlmostEqual(result.daq_waveform_duration_s, 4.5001)
        self.assertEqual(runner.config.camera.exposure_us, 20_000)
        self.assertEqual(runner.config.timing.inter_frame_gap_us, 10_000)
        slm_adapter.select_running_order.assert_called_once_with(1)
        capture_path.assert_called_once_with("sim_acquisition", "sim_acquisition_488nm_500ms")
        camera_config = camera_adapter.apply_config.call_args.args[0]
        self.assertEqual(camera_config.exposure_us, 500_000)
        self.assertEqual(waveform_builder.build.call_args.kwargs["exposure_us"], 500_000)
        timing_config = waveform_builder.build.call_args.kwargs["timing"]
        self.assertEqual(timing_config.inter_frame_gap_us, 50_000)
        daq_adapter.play_waveform.assert_called_once_with("Dev1", waveform_plan, stop_event=None)

    def test_sim_acquisition_pulse_message_shows_actual_and_daq_durations_from_command_start(self):
        import threading

        from sim_control.daq_testing import SIM_ACQUISITION_TEST_ID, DaqTestRunner, SimAcquisitionTestResult
        from sim_control.models import AppConfig

        runner = DaqTestRunner(
            camera_adapter=mock.Mock(),
            slm_adapter=mock.Mock(),
            daq_adapter=mock.Mock(),
            config=AppConfig(),
            selected_laser_nm=488,
        )
        runner._run_sim_acquisition_test = mock.Mock(
            return_value=SimAcquisitionTestResult(
                output_path=Path("dummy.tiff"),
                actual_acquisition_duration_s=1.25,
                daq_waveform_duration_s=0.6301,
            )
        )
        stop_event = threading.Event()

        message = runner.run_test(
            SIM_ACQUISITION_TEST_ID,
            runner.config.daq,
            selected_laser_nm=488,
            stop_event=stop_event,
            acquisition_started_at_s=123.0,
        )

        runner._run_sim_acquisition_test.assert_called_once_with(
            runner.config.daq,
            acquisition_started_at_s=123.0,
            stop_event=stop_event,
            selected_laser_nm=488,
        )
        self.assertIn("16位 TIFF 已保存到:\ndummy.tiff", message)
        self.assertIn("SIM采集实际用时: 1250.000 ms", message)
        self.assertIn("DAQ完整播放时长: 630.100 ms", message)


    def test_run_sim_acquisition_test_forwards_stop_event_to_play_waveform(self):
        """_run_sim_acquisition_test 把 stop_event 原样传给 daq_adapter.play_waveform。"""
        import threading
        from sim_control.daq_testing import DaqTestRunner
        from sim_control.models import AppConfig, CameraConfig, TimingConfig

        config = AppConfig(
            camera=CameraConfig(exposure_us=20_000),
            timing=TimingConfig(inter_frame_gap_us=10_000),
        )
        slm_adapter = mock.Mock()
        slm_adapter.is_connected.return_value = True
        slm_adapter.list_running_orders.return_value = [
            (0, "488_3.5_2d_10ms"),
            (1, "488_3.5_2d_50ms"),
        ]
        slm_adapter.select_running_order.return_value = {
            "running_order_name": "488_3.5_2d_50ms",
            "pattern_result": mock.Mock(pattern_files=["488_3.5_2d_50ms"] * 9, handles=[-1]),
        }
        camera_adapter = mock.Mock()
        camera_adapter.is_connected.return_value = True
        camera_adapter.read_frame_sequence.return_value = (np.zeros((9, 2, 2), dtype=np.uint16), [])
        waveform_builder = mock.Mock()
        waveform_plan = SimpleNamespace(duration_s=0.6)
        waveform_builder.build.return_value = waveform_plan
        daq_adapter = mock.Mock()
        runner = DaqTestRunner(
            camera_adapter=camera_adapter,
            slm_adapter=slm_adapter,
            daq_adapter=daq_adapter,
            config=config,
            selected_laser_nm=488,
        )

        sentinel = threading.Event()
        with mock.patch.object(runner, "_test_capture_path", return_value=Path("x.tiff")), mock.patch.object(
            runner, "_write_uint16_tiff"
        ), mock.patch("sim_control.daq_testing.NIDaqWaveformBuilder", return_value=waveform_builder), mock.patch(
            "time.perf_counter", return_value=1.0
        ):
            runner._run_sim_acquisition_test(
                runner.config.daq, acquisition_started_at_s=0.0, stop_event=sentinel
            )

        daq_adapter.play_waveform.assert_called_once_with(
            mock.ANY, mock.ANY, stop_event=sentinel
        )

    def test_run_sim_acquisition_test_uses_snapshot_laser_nm_not_widget(self):
        """selected_laser_nm 快照优先于 _selected_laser_nm() 读取，确保 worker 线程安全。"""
        from sim_control.daq_testing import DaqTestRunner
        from sim_control.models import AppConfig, CameraConfig, TimingConfig

        config = AppConfig(
            camera=CameraConfig(exposure_us=20_000),
            timing=TimingConfig(inter_frame_gap_us=10_000),
        )
        slm_adapter = mock.Mock()
        slm_adapter.is_connected.return_value = True
        slm_adapter.list_running_orders.return_value = [
            (0, "488_3.5_2d_10ms"),
            (1, "488_3.5_2d_50ms"),
        ]
        slm_adapter.select_running_order.return_value = {
            "running_order_name": "488_3.5_2d_50ms",
            "pattern_result": mock.Mock(pattern_files=["488_3.5_2d_50ms"] * 9, handles=[-1]),
        }
        camera_adapter = mock.Mock()
        camera_adapter.is_connected.return_value = True
        camera_adapter.read_frame_sequence.return_value = (np.zeros((9, 2, 2), dtype=np.uint16), [])
        waveform_builder = mock.Mock()
        waveform_builder.build.return_value = SimpleNamespace(duration_s=0.6)
        runner = DaqTestRunner(
            camera_adapter=camera_adapter,
            slm_adapter=slm_adapter,
            daq_adapter=mock.Mock(),
            config=config,
            selected_laser_nm=488,
        )

        with mock.patch.object(
            runner, "_selected_laser_nm", side_effect=RuntimeError("snapshot laser_nm must win")
        ), mock.patch.object(runner, "_test_capture_path", return_value=Path("x.tiff")), mock.patch.object(
            runner, "_write_uint16_tiff"
        ), mock.patch("sim_control.daq_testing.NIDaqWaveformBuilder", return_value=waveform_builder), mock.patch(
            "time.perf_counter", return_value=1.0
        ):
            result = runner._run_sim_acquisition_test(
                runner.config.daq, acquisition_started_at_s=0.0, selected_laser_nm=488
            )

        self.assertIsNotNone(result)

    def test_laser_pulse_test_skips_pulse_when_stop_event_preset(self):
        """stop_event 预先置位时激光脉冲测试直接返回，不再驱动 DAQ。"""
        import threading

        from sim_control.daq_testing import DaqTestRunner
        from sim_control.models import AppConfig

        daq_adapter = mock.Mock()
        runner = DaqTestRunner(
            camera_adapter=mock.Mock(),
            slm_adapter=mock.Mock(),
            daq_adapter=daq_adapter,
            config=AppConfig(),
            selected_laser_nm=488,
        )
        stop_event = threading.Event()
        stop_event.set()

        runner._run_laser_pulse_test(runner.config.daq, "laser_488_line", stop_event=stop_event)

        daq_adapter.pulse_line.assert_not_called()

    def test_laser_pulse_test_forwards_stop_event_to_pulse_line(self):
        """激光脉冲测试把 stop_event 透传给 pulse_line，让 1 秒脉冲期间可被取消（提前拉低）。"""
        import threading

        from sim_control.daq_testing import DaqTestRunner
        from sim_control.models import AppConfig

        daq_adapter = mock.Mock()
        runner = DaqTestRunner(
            camera_adapter=mock.Mock(),
            slm_adapter=mock.Mock(),
            daq_adapter=daq_adapter,
            config=AppConfig(),
            selected_laser_nm=488,
        )
        sentinel = threading.Event()

        runner._run_laser_pulse_test(runner.config.daq, "laser_488_line", stop_event=sentinel)

        kwargs = daq_adapter.pulse_line.call_args.kwargs
        self.assertIs(kwargs["stop_event"], sentinel)


class SimRawStackSavedDialogTests(unittest.TestCase):
    """SIM9 帧采集（raw-only）保存完成后弹出信息框的行为测试。

    覆盖 ``control_wangbo/main.py`` 的 ``slot_handle_sim_raw_stack_saved``：9 帧成功
    落盘后用自定义 ``QDialog`` 弹「SIM9 帧采集」完成框，正文含图像格式（尺寸+
    位深）、曝光、波长、总耗时与保存路径；正文用 RichText 分别指定中文
    ``Microsoft YaHei``、英文/数字/路径 ``Arial``，并保持保存路径逻辑行不换行；
    缓存摘要 task_id 不匹配时降级为只显示路径；保存失败路径仍只走静态
    ``warning``。用 ``MainWindow.__new__`` 构造无 ``__init__`` 实例，并
    patch ``QDialog.exec_`` 避免真实阻塞，无需硬件。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def _make_window(self, main_module):
        window = main_module.MainWindow.__new__(main_module.MainWindow)
        window.sim_raw_stack_save_finished_task_ids = set()
        window.update_sim_runtime_status_widgets = mock.Mock()
        window.sim_runtime_status_labels = {}
        return window

    def _assert_dialog_widths(self, main_module, dialog, text_label, message_lines):
        chinese_font = main_module.QFont("Microsoft YaHei", 13)
        ascii_font = main_module.QFont("Arial", 13)
        longest_px = max(
            main_module.MainWindow._measure_sim_dialog_line_width(
                line, chinese_font, ascii_font
            )
            for line in message_lines
        )
        icon_size = dialog.style().pixelMetric(
            main_module.qw.QStyle.PM_MessageBoxIconSize, None, dialog
        )
        self.assertGreaterEqual(text_label.minimumWidth(), longest_px + 96)
        self.assertGreaterEqual(dialog.minimumWidth(), longest_px + icon_size + 96 + 80)

    def _assert_dialog_fonts(self, main_module, dialog, text_label):
        self.assertEqual(dialog.font().family(), "Microsoft YaHei")
        self.assertEqual(dialog.font().pointSize(), 13)
        self.assertEqual(text_label.font().family(), "Microsoft YaHei")
        self.assertEqual(text_label.font().pointSize(), 13)
        button_box = dialog.findChild(main_module.qw.QDialogButtonBox)
        self.assertIsNotNone(button_box)
        self.assertEqual(button_box.font().family(), "Arial")
        self.assertEqual(button_box.font().pointSize(), 13)
        ok_button = button_box.button(main_module.qw.QDialogButtonBox.Ok)
        self.assertIsNotNone(ok_button)
        self.assertEqual(ok_button.font().family(), "Arial")
        self.assertEqual(ok_button.font().pointSize(), 13)

    def _assert_message_label_html(self, main_module, text_label):
        self.assertEqual(text_label.textFormat(), main_module.Qt.RichText)
        self.assertFalse(text_label.wordWrap())
        self.assertTrue(text_label.textInteractionFlags() & main_module.Qt.TextSelectableByMouse)
        html_message = text_label.text()
        self.assertIn("white-space: nowrap", html_message)
        self.assertIn("font-family:'Microsoft YaHei'", html_message)
        self.assertIn("font-family:'Arial'", html_message)
        self.assertIn("font-size:13pt", html_message)
        return html_message

    def test_raw_stack_saved_shows_completion_dialog_with_all_details(self):
        main_module = load_legacy_main_module()
        window = self._make_window(main_module)
        window.sim_last_raw_acquisition_summary = {
            "task_id": "task-1",
            "stack_shape": [9, 2048, 2048],
            "stack_dtype": "uint16",
            "laser_wavelength_nm": 488,
            "exposure_us": 1_000_000,
            "start_perf": time.perf_counter() - 3.0,
        }
        saved_path = r"D:\sim_data\sim_9frames\sim_9frames_task-1.tif"
        executed_dialogs = []

        def _capture_exec(dialog):
            executed_dialogs.append(dialog)
            return 0

        with mock.patch.object(main_module.qw.QDialog, "exec_", new=_capture_exec):
            main_module.MainWindow.slot_handle_sim_raw_stack_saved(window, "task-1", saved_path)

        self.assertEqual(len(executed_dialogs), 1)
        dialog = executed_dialogs[0]
        self.assertEqual(dialog.windowTitle(), "SIM9 帧采集")
        text_label = dialog.findChild(main_module.qw.QLabel, "sim_raw_stack_saved_message_label")
        self.assertIsNotNone(text_label)
        message = text_label.property("sim_plain_text")
        self.assertIsInstance(message, str)
        message_lines = message.split("\n")
        # 自定义 dialog 不再使用 QMessageBox 内部布局；正文 label 自己禁止换行并撑宽。
        self._assert_message_label_html(main_module, text_label)
        self._assert_dialog_widths(main_module, dialog, text_label, message_lines)
        self._assert_dialog_fonts(main_module, dialog, text_label)
        # 总耗时已移到第一行
        self.assertIn("总耗时:", message_lines[0])
        self.assertIn("SIM9 帧采集已完成", message)
        self.assertIn("图像格式: 2048X2048（16位）", message)
        self.assertIn("曝光时间: 1000 ms", message)
        self.assertIn("激光波长: 488 nm", message)
        self.assertIn(saved_path, message)
        self.assertEqual(message_lines[-1], f"保存路径: {saved_path}")

    def test_raw_stack_saved_dialog_omits_details_when_task_id_mismatch(self):
        main_module = load_legacy_main_module()
        window = self._make_window(main_module)
        # 缓存摘要属于另一个 task：保存完成仍弹窗，但降级为只显示路径、不含明细。
        window.sim_last_raw_acquisition_summary = {
            "task_id": "other-task",
            "stack_shape": [9, 2048, 2048],
            "stack_dtype": "uint16",
            "laser_wavelength_nm": 488,
            "exposure_us": 1_000_000,
            "start_perf": time.perf_counter() - 3.0,
        }
        saved_path = r"D:\sim_data\sim_9frames\sim_9frames_task-1.tif"
        executed_dialogs = []

        def _capture_exec(dialog):
            executed_dialogs.append(dialog)
            return 0

        with mock.patch.object(main_module.qw.QDialog, "exec_", new=_capture_exec):
            main_module.MainWindow.slot_handle_sim_raw_stack_saved(window, "task-1", saved_path)

        self.assertEqual(len(executed_dialogs), 1)
        dialog = executed_dialogs[0]
        text_label = dialog.findChild(main_module.qw.QLabel, "sim_raw_stack_saved_message_label")
        self.assertIsNotNone(text_label)
        message = text_label.property("sim_plain_text")
        self.assertIsInstance(message, str)
        message_lines = message.split("\n")
        self._assert_message_label_html(main_module, text_label)
        self._assert_dialog_widths(main_module, dialog, text_label, message_lines)
        self._assert_dialog_fonts(main_module, dialog, text_label)
        self.assertIn(saved_path, message)
        self.assertEqual(message_lines[-1], f"保存路径: {saved_path}")
        self.assertNotIn("图像格式", message)
        self.assertNotIn("曝光时间", message)
        self.assertNotIn("激光波长", message)
        self.assertNotIn("总耗时", message)

    def test_raw_stack_save_failed_uses_static_warning(self):
        main_module = load_legacy_main_module()
        window = self._make_window(main_module)
        with mock.patch("control_wangbo.main.qw.QMessageBox") as MsgBox:
            main_module.MainWindow.slot_handle_sim_raw_stack_save_failed(window, "task-1", "disk full")

        MsgBox.warning.assert_called_once()
        # 失败走静态 warning，不实例化完成框
        MsgBox.assert_not_called()


if __name__ == "__main__":
    unittest.main()

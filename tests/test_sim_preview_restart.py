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
        4. SimSettingsDialog 共享 adapter：弹窗不应该 disconnect 主界面已连接的相机/SLM。
        5. ``running_order_selected`` 状态被广播时主界面摘要刷新；``patterns_prepared``
           会更新 controller 内部 ``pattern_result`` 但不影响 GUI 预览。

协作关系：
    上游：``unittest``、``unittest.mock``、``numpy``、PyQt5 (QtCore/QtTest/QtWidgets)、
          ``importlib`` 用于动态导入 ``control_wangbo.main``。
    下游：``control_wangbo.main.MainWindow`` 与 helper 函数、``sim_control.preview``、
          ``sim_control.gui``、``sim_control.controller``。

维护要点：
    - 本测试在 import 阶段就给 ``MCUTriggerThread`` / ``mvsdk`` / ``FastCameraThread``
      注入空 stub（参考 ``test_main_window_scroll_area.py``）；改动主界面 import
      路径时需要同步更新这些 stub。
    - 测试中 ``MainWindow`` 用真实 PyQt5 实例化但 controller / camera_adapter / slm_adapter
      通过 ``SimpleNamespace`` mock；可以放心地在无硬件环境下运行。
    - 几个类的测试范围：``SimPreviewRestartTests`` 覆盖 live preview 状态机；
      ``SimPreviewControllerTests`` 覆盖 ``preview.py``；``SimPreviewPollingTests``
      覆盖 GUI 端 latest-frame-wins 轮询；``SimCameraSpinBoxCommitTests`` 覆盖
      ROI/曝光控件提交时机；``SimSettingsDialogTests`` 覆盖共享 adapter 行为。
"""

import importlib
import sys
import time
import types
import unittest
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


class ComboBoxSpy:
    """模拟 QComboBox 的文本、索引、启用状态和条目列表。"""
    def __init__(self, text="", current_index=-1, enabled=True):
        self._text = text
        self._current_index = current_index
        self._enabled = enabled
        self.block_signals_calls = []
        self.items = []

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
        self._current_index = -1

    def addItem(self, text):
        self.items.append(text)
        if self._current_index < 0:
            self._current_index = 0
            if not self._text:
                self._text = text

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

    def test_reconstruction_runtime_led_matches_existing_led_visual_size(self):
        legacy_main = load_legacy_main_module()
        from control_wangbo.CellSorting_ui import Ui_Single_Cell_Sorting

        host = QtWidgets.QWidget()
        ui = Ui_Single_Cell_Sorting()
        ui.setupUi(host)
        window = SimpleNamespace(ui=ui)
        window.set_sim_runtime_led_state = lambda led, state: legacy_main.MainWindow.set_sim_runtime_led_state(
            window, led, state
        )

        legacy_main.MainWindow.setup_sim_runtime_status_widgets(window)

        reconstruction_led = window.sim_runtime_leds["reconstruction"]
        self.assertTrue(hasattr(ui, "led_simRuntimeReconstruction"))
        self.assertTrue(hasattr(ui, "lbl_simRuntimeReconstructionName"))
        self.assertTrue(hasattr(ui, "lbl_simRuntimeReconstructionStatus"))
        self.assertIs(reconstruction_led, ui.led_simRuntimeReconstruction)
        self.assertIs(window.sim_runtime_status_labels["reconstruction"], ui.lbl_simRuntimeReconstructionStatus)
        self.assertEqual(ui.lbl_simRuntimeReconstructionName.text(), "Reconstruction")
        reference_leds = (
            ui.led_simRuntimeCamera,
            ui.led_simRuntimeSlm,
            ui.led_simRuntimeDaq,
        )
        for reference_led in reference_leds:
            with self.subTest(reference=reference_led.objectName()):
                self.assertIsInstance(reconstruction_led, QtWidgets.QLabel)
                self.assertEqual(reconstruction_led.minimumSize(), reference_led.minimumSize())
                self.assertEqual(reconstruction_led.maximumSize(), reference_led.maximumSize())
                self.assertEqual(reconstruction_led.styleSheet(), reference_led.styleSheet())

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
        camera = SimpleNamespace(bit_depth=16)
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
        controller.apply_camera_config.assert_called_once_with(camera)
        preview_controller.start.assert_called_once_with(camera, timeout_ms=200)
        self.assertEqual(window.sim_runtime_timing_snapshot["recommended_inter_frame_gap_us"], 4800)

    def test_size_dropdown_activation_triggers_live_setting_change(self):
        legacy_main = load_legacy_main_module()
        on_change_calls = []
        window = SimpleNamespace(
            sim_app_config=SimpleNamespace(camera=SimpleNamespace(roi_width=1152, roi_height=1152)),
            ui=SimpleNamespace(cmb_sCMOS_imageSize=ComboBoxSpy(text="576 x 576")),
            on_sim_camera_setting_changed=lambda: on_change_calls.append("changed"),
        )

        legacy_main.MainWindow.on_sim_camera_size_activated(window)

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
        from sim_control.models import AppConfig

        stop_calls = []
        timer = TimerSpy()
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
            sim_last_acquisition_batch=None,
            sim_current_task_id="",
            sim_acquisition_controller=controller,
            sim_app_config=AppConfig(),
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
        self.assertEqual(window.sim_current_task_id, "task-1")
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
            sim_last_acquisition_batch=None,
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
            sim_last_acquisition_batch="previous-batch",
            update_sim_camera_action_buttons=lambda: action_updates.append("updated"),
            set_sim_camera_controls_enabled=lambda enabled: controls_enabled.append(enabled),
            start_sim_preview=lambda: starts.append("start"),
        )

        legacy_main.MainWindow.slot_handle_sim_acquisition_ready(window, summary)

        self.assertFalse(window.sim_acquisition_in_progress)
        self.assertEqual(action_updates, ["updated"])
        self.assertEqual(controls_enabled, [True])
        self.assertEqual(window.sim_last_acquisition_batch, "previous-batch")
        self.assertFalse(hasattr(window, "sim_last_preview_frame"))
        self.assertEqual(window.sim_current_task_id, "task-2")
        self.assertEqual(starts, ["start"])

    def test_open_sim_settings_does_not_probe_real_camera_when_connected(self):
        legacy_main = load_legacy_main_module()
        dialog_instances = []

        class FakeSignal:
            def __init__(self):
                self.connected_callbacks = []

            def connect(self, callback):
                self.connected_callbacks.append(callback)

        class FakeDialog:
            def __init__(self, config, parent, slm_adapter=None, camera_adapter=None):
                self.config = config
                self.parent = parent
                self.slm_adapter = slm_adapter
                self.camera_adapter = camera_adapter
                self.exec_calls = 0
                self.signal_settings_saved = FakeSignal()
                dialog_instances.append(self)

            def exec_(self):
                self.exec_calls += 1

        window = SimpleNamespace(
            sim_camera_connected=True,
            sim_preview_requested=False,
            sim_preview_active=False,
            sim_preview_stop_in_progress=False,
            sim_acquisition_in_progress=False,
            sim_app_config=SimpleNamespace(),
            ensure_sim_runtime=mock.Mock(),
            sim_acquisition_controller=SimpleNamespace(
                slm_adapter="shared-slm",
                camera_adapter="shared-camera",
            ),
            prefer_real_sim_hardware=mock.Mock(),
            stop_sim_preview=mock.Mock(),
            start_sim_preview=mock.Mock(),
            apply_sim_settings=mock.Mock(),
        )

        with mock.patch.object(legacy_main, "SimSettingsDialog", FakeDialog):
            legacy_main.MainWindow.btn_openSimSettings_function(window)

        window.prefer_real_sim_hardware.assert_not_called()
        self.assertEqual(len(dialog_instances), 1)
        self.assertEqual(dialog_instances[0].slm_adapter, "shared-slm")
        self.assertEqual(dialog_instances[0].camera_adapter, "shared-camera")
        self.assertEqual(
            dialog_instances[0].signal_settings_saved.connected_callbacks,
            [window.apply_sim_settings],
        )
        self.assertEqual(dialog_instances[0].exec_calls, 1)
        window.stop_sim_preview.assert_not_called()
        window.start_sim_preview.assert_not_called()

    def test_open_sim_settings_suspends_live_and_restores_after_dialog_closes(self):
        legacy_main = load_legacy_main_module()
        dialog_instances = []

        class FakeSignal:
            def __init__(self):
                self.connected_callbacks = []

            def connect(self, callback):
                self.connected_callbacks.append(callback)

        class FakeDialog:
            def __init__(self, config, parent, slm_adapter=None, camera_adapter=None):
                self.config = config
                self.parent = parent
                self.slm_adapter = slm_adapter
                self.camera_adapter = camera_adapter
                self.signal_settings_saved = FakeSignal()
                dialog_instances.append(self)

            def exec_(self):
                return 0

        stop_calls = []
        start_calls = []
        window = SimpleNamespace(
            sim_camera_connected=True,
            sim_preview_requested=True,
            sim_preview_active=True,
            sim_preview_stop_in_progress=False,
            sim_acquisition_in_progress=False,
            sim_app_config=SimpleNamespace(),
            ensure_sim_runtime=mock.Mock(),
            sim_acquisition_controller=SimpleNamespace(
                slm_adapter="shared-slm",
                camera_adapter="shared-camera",
            ),
            prefer_real_sim_hardware=mock.Mock(),
            stop_sim_preview=lambda wait=True: (stop_calls.append(wait) or True),
            start_sim_preview=lambda: start_calls.append("start"),
            apply_sim_settings=mock.Mock(),
        )

        with mock.patch.object(legacy_main, "SimSettingsDialog", FakeDialog):
            legacy_main.MainWindow.btn_openSimSettings_function(window)

        self.assertEqual(stop_calls, [True])
        self.assertEqual(start_calls, ["start"])
        self.assertEqual(len(dialog_instances), 1)
        self.assertEqual(dialog_instances[0].slm_adapter, "shared-slm")
        self.assertEqual(dialog_instances[0].camera_adapter, "shared-camera")
        window.prefer_real_sim_hardware.assert_not_called()

    def test_apply_sim_settings_avoids_camera_reprobe_when_connected(self):
        legacy_main = load_legacy_main_module()
        from sim_control.models import AppConfig

        config = AppConfig()
        config.config_path = "dummy.json"
        window = SimpleNamespace(
            sim_camera_connected=True,
            sim_preview_active=True,
            sim_slm_connected=True,
            sim_app_config=AppConfig(),
            sim_acquisition_controller=SimpleNamespace(
                apply_daq_config=mock.Mock(),
                select_running_order_for_task=mock.Mock(return_value={"running_order_name": "488_3.5_2d_1ms"})
            ),
            prefer_real_sim_hardware=mock.Mock(),
            sync_sim_camera_controls_from_config=mock.Mock(),
            refresh_sim_settings_summary=mock.Mock(),
            refresh_sim_camera_devices=mock.Mock(),
            restart_sim_preview_with_current_settings=mock.Mock(),
        )

        with mock.patch.object(legacy_main, "save_app_config"):
            legacy_main.MainWindow.apply_sim_settings(window, config)

        window.prefer_real_sim_hardware.assert_called_once_with(save_to_disk=True, probe_camera=False)
        window.sim_acquisition_controller.apply_daq_config.assert_called_once_with(config.daq)
        window.sim_acquisition_controller.select_running_order_for_task.assert_called_once_with(488, config.camera.exposure_us)
        self.assertEqual(window.sim_app_config.selected_running_order, "488_3.5_2d_1ms")
        window.sync_sim_camera_controls_from_config.assert_called_once_with()
        window.refresh_sim_settings_summary.assert_called_once_with()
        window.refresh_sim_camera_devices.assert_called_once_with()
        window.restart_sim_preview_with_current_settings.assert_not_called()

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

        worker = SimPreviewWorker(gui_preview_fps_limit=30)

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
            _render_sim_preview_frame=lambda frame: rendered_frames.append(frame.copy()),
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


class SimSettingsDialogTests(unittest.TestCase):
    """``SimSettingsDialog`` 与主界面共享 ``slm_adapter`` / ``camera_adapter`` 的回归测试。

    覆盖：
        - 共享 adapter 的弹窗在 ``closeEvent`` 中不应 disconnect 已连接的硬件。
        - ``signal_settings_saved`` 把更新的 ``AppConfig`` 回传给主界面。
        - SIM 采集测试路径要求 SLM 已连接，否则抛 HardwareError。
    """

    """验证设置弹窗复用外部 adapter、延迟刷新和 DAQ/SIM 测试路径。"""
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance()
        if cls.app is None:
            cls.app = QtWidgets.QApplication([])

    def assert_compact_action_button_style(self, button, *, width):
        self.assertEqual(button.sizePolicy().horizontalPolicy(), QtWidgets.QSizePolicy.Fixed)
        self.assertEqual(button.sizePolicy().verticalPolicy(), QtWidgets.QSizePolicy.Fixed)
        self.assertEqual(button.minimumHeight(), 36)
        self.assertEqual(button.maximumHeight(), 36)
        self.assertEqual(button.minimumWidth(), width)
        self.assertEqual(button.maximumWidth(), width)

    def test_dialog_defers_daq_refresh_until_first_show(self):
        from sim_control.gui import SimSettingsDialog
        from sim_control.models import AppConfig

        with mock.patch.object(SimSettingsDialog, "_refresh_daq_devices", autospec=True) as refresh_daq:
            dialog = SimSettingsDialog(config=AppConfig())
            self.assertEqual(refresh_daq.call_count, 0)

            dialog.show()
            self.app.processEvents()
            QtTest.QTest.qWait(20)
            self.app.processEvents()

            self.assertEqual(refresh_daq.call_count, 1)
            dialog.close()

    def test_dialog_no_longer_exposes_pattern_or_slm_controls(self):
        from sim_control.gui import SimSettingsDialog
        from sim_control.models import AppConfig

        with mock.patch("sim_control.gui.NIDaqAdapter.list_devices", return_value=[]), mock.patch(
            "sim_control.gui.NIDaqAdapter.list_port0_lines",
            return_value=[],
        ):
            dialog = SimSettingsDialog(config=AppConfig())

        self.assertFalse(hasattr(dialog.ui, "tab_patterns"))
        self.assertFalse(hasattr(dialog.ui, "combo_slm_device"))
        self.assertFalse(hasattr(dialog.ui, "btn_load_patterns"))
        self.assertFalse(hasattr(dialog, "pattern_edits"))
        self.assertEqual(dialog.styleSheet(), "")
        dialog.close()

    def test_dialog_accepts_external_slm_adapter_without_taking_lifecycle_ownership(self):
        from sim_control.gui import SimSettingsDialog
        from sim_control.models import AppConfig

        slm_adapter = mock.Mock()
        slm_adapter.disconnect = mock.Mock()
        with mock.patch("sim_control.gui.NIDaqAdapter.list_devices", return_value=[]), mock.patch(
            "sim_control.gui.NIDaqAdapter.list_port0_lines",
            return_value=[],
        ):
            dialog = SimSettingsDialog(config=AppConfig(), slm_adapter=slm_adapter)

        self.assertIs(dialog.slm_adapter, slm_adapter)
        self.assertTrue(dialog._slm_externally_owned)
        dialog.close()
        slm_adapter.disconnect.assert_not_called()

    def test_dialog_accepts_external_camera_adapter_without_taking_lifecycle_ownership(self):
        from sim_control.gui import SimSettingsDialog
        from sim_control.models import AppConfig

        camera_adapter = mock.Mock()
        with mock.patch("sim_control.gui.NIDaqAdapter.list_devices", return_value=[]), mock.patch(
            "sim_control.gui.NIDaqAdapter.list_port0_lines",
            return_value=[],
        ):
            dialog = SimSettingsDialog(config=AppConfig(), camera_adapter=camera_adapter)

        self.assertIs(dialog.camera_adapter, camera_adapter)
        self.assertTrue(dialog._camera_externally_owned)
        dialog.close()
        camera_adapter.disconnect.assert_not_called()

    def test_dialog_keeps_auto_z_start_when_displaying_connected_stage_position(self):
        from sim_control.gui import SimSettingsDialog, read_z_scan_config_from_widgets
        from sim_control.models import AppConfig, ZScanConfig

        stage_adapter = mock.Mock()
        stage_adapter.is_connected = True
        stage_adapter.get_position_um.return_value = 7.25
        with mock.patch("sim_control.gui.NIDaqAdapter.list_devices", return_value=[]), mock.patch(
            "sim_control.gui.NIDaqAdapter.list_port0_lines",
            return_value=[],
        ):
            dialog = SimSettingsDialog(
                config=AppConfig(z_scan=ZScanConfig(start_um=None)),
                stage_adapter=stage_adapter,
            )

        self.assertAlmostEqual(dialog.spin_zscan_start_um.value(), 7.25)
        self.assertIsNone(read_z_scan_config_from_widgets(dialog).start_um)
        dialog.close()

    def test_zscan_test_targets_are_populated_and_disabled_with_zscan(self):
        from sim_control.gui import (
            Z_SCAN_TEST_STAGE_ONLY,
            Z_SCAN_TEST_STAGE_PLUS_CAPTURE,
            SimSettingsDialog,
        )
        from sim_control.models import AppConfig

        with mock.patch("sim_control.gui.NIDaqAdapter.list_devices", return_value=[]), mock.patch(
            "sim_control.gui.NIDaqAdapter.list_port0_lines",
            return_value=[],
        ):
            dialog = SimSettingsDialog(config=AppConfig())

        self.assertEqual(dialog.combo_zscan_test_target.count(), 2)
        self.assertEqual(dialog.combo_zscan_test_target.itemData(0), Z_SCAN_TEST_STAGE_ONLY)
        self.assertEqual(dialog.combo_zscan_test_target.itemData(1), Z_SCAN_TEST_STAGE_PLUS_CAPTURE)

        dialog.check_zscan_enabled.setChecked(False)
        self.assertFalse(dialog.ui.group_zscan_preview.isEnabled())
        self.assertFalse(dialog.ui.group_zscan_test.isEnabled())
        self.assertFalse(dialog.btn_zscan_test.isEnabled())
        dialog._run_zscan_stage_only_test = mock.Mock(side_effect=AssertionError("disabled z-scan should not run tests"))

        with mock.patch("sim_control.gui.QMessageBox.critical") as critical:
            dialog._run_zscan_test()

        critical.assert_called_once()
        dialog._run_zscan_stage_only_test.assert_not_called()
        dialog.close()

    def test_zscan_preview_auto_start_without_connected_stage_avoids_fake_start_position(self):
        from sim_control.gui import SimSettingsDialog
        from sim_control.models import AppConfig, ZScanConfig

        stage_adapter = mock.Mock()
        stage_adapter.is_connected = False
        with mock.patch("sim_control.gui.NIDaqAdapter.list_devices", return_value=[]), mock.patch(
            "sim_control.gui.NIDaqAdapter.list_port0_lines",
            return_value=[],
        ):
            dialog = SimSettingsDialog(
                config=AppConfig(z_scan=ZScanConfig(start_um=None, step_um=0.3, num_steps=4)),
                stage_adapter=stage_adapter,
            )

        self.assertEqual(dialog.label_zscan_preview_start_value.text(), "运行时读取当前 Z")
        self.assertEqual(dialog.label_zscan_preview_end_value.text(), "--")
        self.assertFalse(hasattr(dialog, "label_zscan_preview_breakdown_value"))
        self.assertTrue(dialog.label_zscan_preview_eta_value.text().endswith(" ms"))
        self.assertAlmostEqual(dialog.spin_zscan_step_um.value(), 300.0)
        self.assertAlmostEqual(dialog.spin_zscan_step_um.singleStep(), 10.0)
        self.assertEqual(dialog.spin_zscan_step_um.suffix(), " nm")
        stage_adapter.connect.assert_not_called()
        stage_adapter.move_z_um.assert_not_called()
        dialog.close()

    def test_zscan_scan_gap_spinbox_round_trips_nm_to_um_config(self):
        from sim_control.gui import SimSettingsDialog, read_z_scan_config_from_widgets
        from sim_control.models import AppConfig, ZScanConfig

        with mock.patch("sim_control.gui.NIDaqAdapter.list_devices", return_value=[]), mock.patch(
            "sim_control.gui.NIDaqAdapter.list_port0_lines",
            return_value=[],
        ):
            dialog = SimSettingsDialog(
                config=AppConfig(z_scan=ZScanConfig(start_um=1.0, step_um=0.3, num_steps=10)),
            )

        self.assertAlmostEqual(dialog.spin_zscan_step_um.value(), 300.0)
        self.assertEqual(dialog.ui.label_zscan_step_um.text(), "Scan gap")
        self.assertEqual(dialog.ui.label_zscan_num_steps.text(), "Moves")
        dialog.spin_zscan_step_um.setValue(250.0)
        cfg = read_z_scan_config_from_widgets(dialog)

        self.assertAlmostEqual(cfg.step_um, 0.25)
        dialog.close()

    def test_read_current_z_refreshes_zscan_preview(self):
        from sim_control.gui import SimSettingsDialog
        from sim_control.models import AppConfig, ZScanConfig

        stage_adapter = mock.Mock()
        stage_adapter.is_connected = True
        stage_adapter.get_position_um.return_value = 12.5
        with mock.patch("sim_control.gui.NIDaqAdapter.list_devices", return_value=[]), mock.patch(
            "sim_control.gui.NIDaqAdapter.list_port0_lines",
            return_value=[],
        ):
            dialog = SimSettingsDialog(
                config=AppConfig(z_scan=ZScanConfig(start_um=1.0, step_um=0.5, num_steps=3)),
                stage_adapter=stage_adapter,
            )

        dialog._read_current_z_into_start()

        self.assertAlmostEqual(dialog.spin_zscan_start_um.value(), 12.5)
        self.assertIn("12.500", dialog.label_zscan_preview_start_value.text())
        self.assertIn("14.000", dialog.label_zscan_preview_end_value.text())
        dialog.close()

    def test_read_current_z_button_click_ignores_qpushbutton_checked_argument(self):
        from sim_control.gui import SimSettingsDialog
        from sim_control.models import AppConfig, ZScanConfig

        stage_adapter = mock.Mock()
        stage_adapter.is_connected = True
        stage_adapter.get_position_um.return_value = 21.125
        with mock.patch("sim_control.gui.NIDaqAdapter.list_devices", return_value=[]), mock.patch(
            "sim_control.gui.NIDaqAdapter.list_port0_lines",
            return_value=[],
        ):
            dialog = SimSettingsDialog(
                config=AppConfig(z_scan=ZScanConfig(start_um=1.0, step_um=0.25, num_steps=3)),
                stage_adapter=stage_adapter,
            )

        dialog.btn_zscan_read_current.click()
        self.app.processEvents()

        self.assertAlmostEqual(dialog.spin_zscan_start_um.value(), 21.125)
        self.assertNotIn("positional argument", dialog.lbl_error.text())
        self.assertIn("21.125", dialog.label_zscan_preview_start_value.text())
        dialog.close()

    def test_zscan_test_button_click_ignores_qpushbutton_checked_argument(self):
        from sim_control.gui import SimSettingsDialog, Z_SCAN_TEST_STAGE_ONLY
        from sim_control.models import AppConfig, ZScanConfig

        stage_adapter = mock.Mock()
        stage_adapter.is_connected = True
        stage_adapter.get_position_um.return_value = 1.0
        stage_adapter.get_z_ranges_um.return_value = (0.0, 5.0)
        with mock.patch("sim_control.gui.NIDaqAdapter.list_devices", return_value=[]), mock.patch(
            "sim_control.gui.NIDaqAdapter.list_port0_lines",
            return_value=[],
        ):
            dialog = SimSettingsDialog(
                config=AppConfig(z_scan=ZScanConfig(start_um=1.0, step_um=0.5, num_steps=3)),
                stage_adapter=stage_adapter,
            )
        dialog.combo_zscan_test_target.setCurrentIndex(dialog.combo_zscan_test_target.findData(Z_SCAN_TEST_STAGE_ONLY))
        dialog._run_zscan_stage_only_test = mock.Mock(
            return_value=mock.Mock(
                positions_visited=[1.0, 1.5, 2.0, 2.5],
                total_duration_s=0.1,
                move_latencies_ms=[1.0],
                frame_count=0,
            )
        )

        with mock.patch("sim_control.gui.QMessageBox.information") as information:
            dialog.btn_zscan_test.click()
            self.app.processEvents()

        information.assert_called_once()
        dialog._run_zscan_stage_only_test.assert_called_once()
        self.assertNotIn("positional argument", dialog.lbl_error.text())
        dialog.close()

    def test_zscan_stage_only_test_moves_positions_and_returns_to_start(self):
        from sim_control.gui import SimSettingsDialog
        from sim_control.models import AppConfig, ZScanConfig

        stage_adapter = mock.Mock()
        stage_adapter.is_connected = True
        stage_adapter.get_position_um.return_value = 1.0
        stage_adapter.get_z_ranges_um.return_value = (0.0, 5.0)
        with mock.patch("sim_control.gui.NIDaqAdapter.list_devices", return_value=[]), mock.patch(
            "sim_control.gui.NIDaqAdapter.list_port0_lines",
            return_value=[],
        ):
            dialog = SimSettingsDialog(
                config=AppConfig(z_scan=ZScanConfig(start_um=1.0, step_um=0.5, num_steps=3)),
                stage_adapter=stage_adapter,
            )

        with mock.patch(
            "sim_control.gui.time.perf_counter",
            side_effect=[10.0, 10.01, 10.02, 10.03, 10.04, 10.05, 10.06, 10.07, 10.08],
        ):
            result = dialog._run_zscan_stage_only_test(started_at_s=10.0)

        self.assertEqual(result.positions_visited, [1.0, 1.5, 2.0, 2.5])
        self.assertEqual([call.args[0] for call in stage_adapter.move_z_um.call_args_list], [1.0, 1.5, 2.0, 2.5, 1.0])
        self.assertEqual(result.frame_count, 0)
        self.assertIsNone(getattr(dialog, "_pending_zscan_restore_warning", None))
        dialog.close()

    def test_zscan_stage_only_out_of_range_does_not_move(self):
        from sim_control.gui import SimSettingsDialog
        from sim_control.models import AppConfig, ZScanConfig

        stage_adapter = mock.Mock()
        stage_adapter.is_connected = True
        stage_adapter.get_position_um.return_value = 1.0
        stage_adapter.get_z_ranges_um.return_value = (0.0, 1.2)
        with mock.patch("sim_control.gui.NIDaqAdapter.list_devices", return_value=[]), mock.patch(
            "sim_control.gui.NIDaqAdapter.list_port0_lines",
            return_value=[],
        ):
            dialog = SimSettingsDialog(
                config=AppConfig(z_scan=ZScanConfig(start_um=1.0, step_um=0.5, num_steps=3)),
                stage_adapter=stage_adapter,
            )

        with self.assertRaisesRegex(Exception, "超出位移台量程"):
            dialog._run_zscan_stage_only_test(started_at_s=10.0)

        stage_adapter.move_z_um.assert_not_called()
        dialog.close()

    def test_zscan_stage_only_restore_failure_uses_warning_dialog(self):
        from sim_control.gui import SimSettingsDialog
        from sim_control.models import AppConfig, ZScanConfig

        stage_adapter = mock.Mock()
        stage_adapter.is_connected = True
        stage_adapter.get_position_um.return_value = 1.0
        stage_adapter.get_z_ranges_um.return_value = (0.0, 5.0)
        move_targets = []

        def _move(target):
            move_targets.append(target)
            if len(move_targets) == 5:
                raise RuntimeError("restore failed")

        stage_adapter.move_z_um.side_effect = _move
        with mock.patch("sim_control.gui.NIDaqAdapter.list_devices", return_value=[]), mock.patch(
            "sim_control.gui.NIDaqAdapter.list_port0_lines",
            return_value=[],
        ):
            dialog = SimSettingsDialog(
                config=AppConfig(z_scan=ZScanConfig(start_um=1.0, step_um=0.5, num_steps=3)),
                stage_adapter=stage_adapter,
            )

        with mock.patch("sim_control.gui.QMessageBox.warning") as warning, mock.patch(
            "sim_control.gui.QMessageBox.information"
        ) as info, mock.patch("sim_control.gui.QMessageBox.critical") as critical:
            dialog._run_zscan_test()

        warning.assert_called_once()
        info.assert_not_called()
        critical.assert_not_called()
        self.assertIn("回到起始层失败", dialog.lbl_error.text())
        self.assertEqual(move_targets, [1.0, 1.5, 2.0, 2.5, 1.0])
        dialog.close()

    def test_acquisition_failed_dialog_deduplicates_zscan_ro_warning(self):
        from control_wangbo import main as main_module

        window = main_module.MainWindow.__new__(main_module.MainWindow)
        window.sim_acquisition_in_progress = True
        window.sim_camera_connected = False
        window.sim_current_task_id = ""
        window.sim_resume_preview_after_acquisition = False
        window.update_sim_camera_action_buttons = mock.Mock()
        window.set_sim_camera_controls_enabled = mock.Mock()
        window.update_sim_runtime_status_widgets = mock.Mock()
        warning_line = "Z-scan failed and formal SIM running order could not be restored; SLM may still be on z-scan RO: cannot select 3"
        message = f"z scan failed\n{warning_line}\nTraceback...\n{warning_line}"

        with mock.patch("control_wangbo.main.qw.QMessageBox.warning") as warning:
            main_module.MainWindow.slot_handle_sim_acquisition_failed(window, "task-1", message)

        display_message = warning.call_args.args[2]
        self.assertIn("z scan failed", display_message)
        self.assertEqual(display_message.count("SLM may still be on z-scan RO"), 1)

    def test_zscan_stage_plus_capture_requires_connected_slm(self):
        from sim_control.gui import SimSettingsDialog
        from sim_control.models import AppConfig, ZScanConfig

        stage_adapter = mock.Mock()
        stage_adapter.is_connected = True
        stage_adapter.get_position_um.return_value = 1.0
        slm_adapter = mock.Mock()
        slm_adapter.is_connected.return_value = False
        with mock.patch("sim_control.gui.NIDaqAdapter.list_devices", return_value=[]), mock.patch(
            "sim_control.gui.NIDaqAdapter.list_port0_lines",
            return_value=[],
        ):
            dialog = SimSettingsDialog(
                config=AppConfig(z_scan=ZScanConfig(start_um=1.0, num_steps=3)),
                stage_adapter=stage_adapter,
                slm_adapter=slm_adapter,
            )

        with self.assertRaisesRegex(Exception, "需要先连接 SLM"):
            dialog._run_zscan_stage_plus_capture_test(started_at_s=10.0)

        slm_adapter.connect.assert_not_called()
        dialog.close()

    def test_zscan_stage_plus_capture_rejects_large_stack_before_hardware_actions(self):
        from sim_control.gui import SimSettingsDialog, Z_SCAN_TEST_CAPTURE_MAX_STEPS
        from sim_control.models import AppConfig, ZScanConfig

        stage_adapter = mock.Mock()
        slm_adapter = mock.Mock()
        camera_adapter = mock.Mock()
        with mock.patch("sim_control.gui.NIDaqAdapter.list_devices", return_value=[]), mock.patch(
            "sim_control.gui.NIDaqAdapter.list_port0_lines",
            return_value=[],
        ):
            dialog = SimSettingsDialog(
                config=AppConfig(
                    z_scan=ZScanConfig(start_um=1.0, step_um=0.5, num_steps=Z_SCAN_TEST_CAPTURE_MAX_STEPS + 1)
                ),
                stage_adapter=stage_adapter,
                slm_adapter=slm_adapter,
                camera_adapter=camera_adapter,
            )

        with self.assertRaisesRegex(Exception, "最多允许"):
            dialog._run_zscan_stage_plus_capture_test(started_at_s=10.0)

        stage_adapter.connect.assert_not_called()
        stage_adapter.move_z_um.assert_not_called()
        slm_adapter.is_connected.assert_not_called()
        camera_adapter.disarm.assert_not_called()
        dialog.close()

    def test_zscan_stage_plus_capture_runs_core_with_stack_retention_and_cleans_up(self):
        from sim_control.gui import SimSettingsDialog
        from sim_control.models import AppConfig, ZScanConfig
        from sim_control.z_scan_core import ZFocusPoint, ZScanResult

        stage_adapter = mock.Mock()
        stage_adapter.is_connected = True
        stage_adapter.get_position_um.return_value = 1.0
        stage_adapter.get_z_ranges_um.return_value = (0.0, 5.0)
        slm_adapter = mock.Mock()
        slm_adapter.is_connected.return_value = True
        slm_adapter.list_running_orders.return_value = [(7, "488_3.5_2d_zscan3p_8ms")]
        camera_adapter = mock.Mock()
        camera_adapter.is_connected.return_value = True
        stack = np.zeros((4, 2, 2), dtype=np.uint16)
        z_result = ZScanResult(
            best_z_um=1.5,
            focus_curve=[
                ZFocusPoint(1.0, 1.0),
                ZFocusPoint(1.5, 3.0),
                ZFocusPoint(2.0, 2.0),
                ZFocusPoint(2.5, 1.5),
            ],
            exposure_actual_us=7_884,
            captured_stack=stack,
        )
        with mock.patch("sim_control.gui.NIDaqAdapter.list_devices", return_value=[]), mock.patch(
            "sim_control.gui.NIDaqAdapter.list_port0_lines",
            return_value=[],
        ):
            dialog = SimSettingsDialog(
                config=AppConfig(z_scan=ZScanConfig(start_um=1.0, step_um=0.5, num_steps=3, exposure_preset_ms=8)),
                stage_adapter=stage_adapter,
                slm_adapter=slm_adapter,
                camera_adapter=camera_adapter,
            )
        dialog.daq_adapter = mock.Mock()
        dialog._current_daq_config = mock.Mock(return_value=dialog.config.daq)

        with mock.patch("sim_control.gui.run_z_scan", return_value=z_result) as run_core, mock.patch.object(
            dialog,
            "_test_capture_path",
            return_value=Path("zscan.tiff"),
        ) as capture_path, mock.patch.object(dialog, "_write_uint16_tiff") as write_tiff:
            result = dialog._run_zscan_stage_plus_capture_test(started_at_s=10.0)

        slm_adapter.select_running_order.assert_called_once_with(7)
        self.assertTrue(run_core.call_args.kwargs["keep_captured_stack"])
        self.assertEqual(run_core.call_args.kwargs["z_scan_config"].num_steps, 3)
        capture_path.assert_called_once_with("zscan_capture", "zscan_8ms_3moves")
        write_tiff.assert_called_once()
        self.assertIs(write_tiff.call_args.args[1], stack)
        dialog.daq_adapter.set_all_low.assert_called_once_with("Dev1")
        camera_adapter.disarm.assert_called_once()
        camera_adapter.disconnect.assert_not_called()
        self.assertEqual(result.frame_count, 4)
        self.assertEqual(result.best_layer_index, 1)
        self.assertAlmostEqual(result.best_z_um, 1.5)
        dialog.close()

    def test_zscan_stage_plus_capture_core_failure_after_stage_move_cleans_up_and_returns_start(self):
        from sim_control.gui import SimSettingsDialog
        from sim_control.models import AppConfig, ZScanConfig

        stage_adapter = mock.Mock()
        stage_adapter.is_connected = True
        stage_adapter.get_position_um.return_value = 1.0
        stage_adapter.get_z_ranges_um.return_value = (0.0, 5.0)
        slm_adapter = mock.Mock()
        slm_adapter.is_connected.return_value = True
        slm_adapter.list_running_orders.return_value = [(7, "488_3.5_2d_zscan3p_8ms")]
        camera_adapter = mock.Mock()
        camera_adapter.is_connected.return_value = True
        with mock.patch("sim_control.gui.NIDaqAdapter.list_devices", return_value=[]), mock.patch(
            "sim_control.gui.NIDaqAdapter.list_port0_lines",
            return_value=[],
        ):
            dialog = SimSettingsDialog(
                config=AppConfig(z_scan=ZScanConfig(start_um=1.0, step_um=0.5, num_steps=3, exposure_preset_ms=8)),
                stage_adapter=stage_adapter,
                slm_adapter=slm_adapter,
                camera_adapter=camera_adapter,
            )
        dialog.daq_adapter = mock.Mock()
        dialog._current_daq_config = mock.Mock(return_value=dialog.config.daq)

        def _fail_after_stage_move(**kwargs):
            kwargs["on_status"]("z_scan_stage_positioned", {"z_um": 1.0})
            raise RuntimeError("camera read failed")

        with mock.patch("sim_control.gui.run_z_scan", side_effect=_fail_after_stage_move):
            with self.assertRaisesRegex(RuntimeError, "camera read failed"):
                dialog._run_zscan_stage_plus_capture_test(started_at_s=10.0)

        stage_adapter.move_z_um.assert_called_once_with(1.0)
        dialog.daq_adapter.set_all_low.assert_called_once_with("Dev1")
        camera_adapter.disarm.assert_called_once()
        camera_adapter.disconnect.assert_not_called()
        dialog.close()

    def test_zscan_stage_plus_capture_core_failure_before_stage_move_does_not_move_stage(self):
        from sim_control.gui import SimSettingsDialog
        from sim_control.models import AppConfig, ZScanConfig

        stage_adapter = mock.Mock()
        stage_adapter.is_connected = True
        stage_adapter.get_position_um.return_value = 1.0
        stage_adapter.get_z_ranges_um.return_value = (0.0, 5.0)
        slm_adapter = mock.Mock()
        slm_adapter.is_connected.return_value = True
        slm_adapter.list_running_orders.return_value = [(7, "488_3.5_2d_zscan3p_8ms")]
        camera_adapter = mock.Mock()
        camera_adapter.is_connected.return_value = True
        with mock.patch("sim_control.gui.NIDaqAdapter.list_devices", return_value=[]), mock.patch(
            "sim_control.gui.NIDaqAdapter.list_port0_lines",
            return_value=[],
        ):
            dialog = SimSettingsDialog(
                config=AppConfig(z_scan=ZScanConfig(start_um=1.0, step_um=0.5, num_steps=3, exposure_preset_ms=8)),
                stage_adapter=stage_adapter,
                slm_adapter=slm_adapter,
                camera_adapter=camera_adapter,
            )
        dialog.daq_adapter = mock.Mock()
        dialog._current_daq_config = mock.Mock(return_value=dialog.config.daq)

        with mock.patch("sim_control.gui.run_z_scan", side_effect=RuntimeError("apply config failed")):
            with self.assertRaisesRegex(RuntimeError, "apply config failed"):
                dialog._run_zscan_stage_plus_capture_test(started_at_s=10.0)

        stage_adapter.move_z_um.assert_not_called()
        dialog.daq_adapter.set_all_low.assert_called_once_with("Dev1")
        camera_adapter.disarm.assert_called_once()
        dialog.close()

    def test_zscan_stage_plus_capture_focus_curve_length_must_match_stack(self):
        from sim_control.gui import SimSettingsDialog
        from sim_control.models import AppConfig, ZScanConfig
        from sim_control.z_scan_core import ZFocusPoint, ZScanResult

        stage_adapter = mock.Mock()
        stage_adapter.is_connected = True
        stage_adapter.get_position_um.return_value = 1.0
        stage_adapter.get_z_ranges_um.return_value = (0.0, 5.0)
        slm_adapter = mock.Mock()
        slm_adapter.is_connected.return_value = True
        slm_adapter.list_running_orders.return_value = [(7, "488_3.5_2d_zscan3p_8ms")]
        camera_adapter = mock.Mock()
        camera_adapter.is_connected.return_value = True
        z_result = ZScanResult(
            best_z_um=1.0,
            focus_curve=[ZFocusPoint(1.0, 1.0)],
            exposure_actual_us=7_884,
            captured_stack=np.zeros((4, 2, 2), dtype=np.uint16),
        )
        with mock.patch("sim_control.gui.NIDaqAdapter.list_devices", return_value=[]), mock.patch(
            "sim_control.gui.NIDaqAdapter.list_port0_lines",
            return_value=[],
        ):
            dialog = SimSettingsDialog(
                config=AppConfig(z_scan=ZScanConfig(start_um=1.0, step_um=0.5, num_steps=3, exposure_preset_ms=8)),
                stage_adapter=stage_adapter,
                slm_adapter=slm_adapter,
                camera_adapter=camera_adapter,
            )
        dialog.daq_adapter = mock.Mock()
        dialog._current_daq_config = mock.Mock(return_value=dialog.config.daq)

        with mock.patch("sim_control.gui.run_z_scan", return_value=z_result):
            with self.assertRaisesRegex(Exception, "focus curve"):
                dialog._run_zscan_stage_plus_capture_test(started_at_s=10.0)

        dialog.close()

    def test_zscan_stage_plus_capture_reports_missing_running_order(self):
        from sim_control.gui import SimSettingsDialog
        from sim_control.models import AppConfig, ZScanConfig

        stage_adapter = mock.Mock()
        stage_adapter.is_connected = True
        stage_adapter.get_position_um.return_value = 1.0
        stage_adapter.get_z_ranges_um.return_value = (0.0, 5.0)
        slm_adapter = mock.Mock()
        slm_adapter.is_connected.return_value = True
        slm_adapter.list_running_orders.return_value = [(1, "488_3.5_2d_10ms")]
        camera_adapter = mock.Mock()
        with mock.patch("sim_control.gui.NIDaqAdapter.list_devices", return_value=[]), mock.patch(
            "sim_control.gui.NIDaqAdapter.list_port0_lines",
            return_value=[],
        ):
            dialog = SimSettingsDialog(
                config=AppConfig(z_scan=ZScanConfig(start_um=1.0, step_um=0.5, num_steps=3, exposure_preset_ms=8)),
                stage_adapter=stage_adapter,
                slm_adapter=slm_adapter,
                camera_adapter=camera_adapter,
            )
        dialog.daq_adapter = mock.Mock()
        dialog._current_daq_config = mock.Mock(return_value=dialog.config.daq)

        with self.assertRaisesRegex(Exception, "No z-scan Running Order"):
            dialog._run_zscan_stage_plus_capture_test(started_at_s=10.0)

        slm_adapter.select_running_order.assert_not_called()
        dialog.close()

    def test_zscan_failure_message_mentions_possible_zscan_running_order(self):
        from sim_control.gui import Z_SCAN_TEST_STAGE_PLUS_CAPTURE, SimSettingsDialog
        from sim_control.models import AppConfig

        with mock.patch("sim_control.gui.NIDaqAdapter.list_devices", return_value=[]), mock.patch(
            "sim_control.gui.NIDaqAdapter.list_port0_lines",
            return_value=[],
        ):
            dialog = SimSettingsDialog(config=AppConfig())
        dialog.combo_zscan_test_target.setCurrentIndex(dialog.combo_zscan_test_target.findData(Z_SCAN_TEST_STAGE_PLUS_CAPTURE))

        def _fail_after_select(_started_at_s):
            dialog._pending_zscan_slm_warning = "SLM 当前 RO 可能仍为 z-scan RO。"
            raise RuntimeError("capture failed")

        dialog._run_zscan_stage_plus_capture_test = mock.Mock(side_effect=_fail_after_select)

        with mock.patch("sim_control.gui.QMessageBox.critical") as critical:
            dialog._run_zscan_test()

        message = critical.call_args.args[2]
        self.assertIn("capture failed", message)
        self.assertIn("SLM 当前 RO 可能仍为 z-scan RO", message)
        dialog.close()

    def test_camera_trigger_test_uses_external_camera_adapter_without_reopening_dcam(self):
        from sim_control.gui import SimSettingsDialog
        from sim_control.models import AppConfig

        camera_adapter = mock.Mock()
        camera_adapter.is_connected.return_value = True
        camera_adapter.read_frame_sequence.return_value = (np.zeros((1, 2, 2), dtype=np.uint16), [])
        with mock.patch("sim_control.gui.NIDaqAdapter.list_devices", return_value=[]), mock.patch(
            "sim_control.gui.NIDaqAdapter.list_port0_lines",
            return_value=[],
        ):
            dialog = SimSettingsDialog(config=AppConfig(), camera_adapter=camera_adapter)
        dialog.daq_adapter = mock.Mock()

        with mock.patch.object(dialog, "_test_capture_path", return_value=Path("dummy.tiff")), mock.patch.object(
            dialog,
            "_write_uint16_tiff",
        ) as write_tiff, mock.patch(
            "sim_control.gui.create_camera_adapter_for_backend",
            side_effect=AssertionError("Should reuse injected camera adapter"),
        ):
            output_path = dialog._run_camera_trigger_test(dialog.config.daq)

        self.assertEqual(output_path, Path("dummy.tiff"))
        camera_adapter.apply_config.assert_called_once_with(dialog.config.camera)
        camera_adapter.arm.assert_called_once_with(frame_count=1)
        dialog.daq_adapter.pulse_line.assert_called_once_with("Dev1", 5, duration_s=0.1)
        write_tiff.assert_called_once()
        camera_adapter.disarm.assert_called_once_with()
        camera_adapter.disconnect.assert_not_called()
        dialog.close()

    def test_camera_trigger_test_cleans_up_external_camera_that_was_disconnected_before_test(self):
        from sim_control.gui import SimSettingsDialog
        from sim_control.models import AppConfig

        camera_adapter = mock.Mock()
        camera_adapter.is_connected.return_value = False
        camera_adapter.read_frame_sequence.return_value = (np.zeros((1, 2, 2), dtype=np.uint16), [])
        with mock.patch("sim_control.gui.NIDaqAdapter.list_devices", return_value=[]), mock.patch(
            "sim_control.gui.NIDaqAdapter.list_port0_lines",
            return_value=[],
        ):
            dialog = SimSettingsDialog(config=AppConfig(), camera_adapter=camera_adapter)
        dialog.daq_adapter = mock.Mock()

        with mock.patch.object(dialog, "_test_capture_path", return_value=Path("dummy.tiff")), mock.patch.object(
            dialog,
            "_write_uint16_tiff",
        ):
            output_path = dialog._run_camera_trigger_test(dialog.config.daq)

        self.assertEqual(output_path, Path("dummy.tiff"))
        camera_adapter.disarm.assert_called_once_with()
        camera_adapter.disconnect.assert_called_once_with()
        dialog.close()

    def test_sim_acquisition_test_selects_running_order_on_shared_slm(self):
        from sim_control.gui import SimSettingsDialog
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
        with mock.patch("sim_control.gui.NIDaqAdapter.list_devices", return_value=[]), mock.patch(
            "sim_control.gui.NIDaqAdapter.list_port0_lines",
            return_value=[],
        ):
            camera = mock.Mock()
            camera.is_connected.return_value = True
            camera.read_frame_sequence.return_value = (np.zeros((9, 2, 2), dtype=np.uint16), [])
            dialog = SimSettingsDialog(
                config=AppConfig(),
                slm_adapter=slm_adapter,
                camera_adapter=camera,
            )
        dialog.daq_adapter = mock.Mock()
        with mock.patch.object(dialog, "_test_capture_path", return_value=Path("dummy.tiff")), mock.patch.object(
            dialog,
            "_write_uint16_tiff",
        ):
            result = dialog._run_sim_acquisition_test(dialog.config.daq)

        self.assertEqual(result.output_path, Path("dummy.tiff"))
        slm_adapter.select_running_order.assert_called_once_with(1)
        dialog.close()

    def test_sim_acquisition_test_uses_external_camera_adapter(self):
        from sim_control.gui import SimSettingsDialog
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
        with mock.patch("sim_control.gui.NIDaqAdapter.list_devices", return_value=[]), mock.patch(
            "sim_control.gui.NIDaqAdapter.list_port0_lines",
            return_value=[],
        ):
            dialog = SimSettingsDialog(
                config=AppConfig(),
                slm_adapter=slm_adapter,
                camera_adapter=camera_adapter,
            )
        dialog.daq_adapter = mock.Mock()

        with mock.patch.object(dialog, "_test_capture_path", return_value=Path("dummy.tiff")), mock.patch.object(
            dialog,
            "_write_uint16_tiff",
        ), mock.patch(
            "sim_control.gui.create_camera_adapter_for_backend",
            side_effect=AssertionError("Should reuse injected camera adapter"),
        ):
            result = dialog._run_sim_acquisition_test(dialog.config.daq)

        self.assertEqual(result.output_path, Path("dummy.tiff"))
        camera_adapter.apply_config.assert_called_once()
        camera_adapter.arm.assert_called_once_with(frame_count=9)
        camera_adapter.disarm.assert_called_once_with()
        camera_adapter.disconnect.assert_not_called()
        dialog.close()

    def test_sim_acquisition_test_forces_500ms_exposure_without_mutating_config(self):
        from sim_control.gui import SimSettingsDialog
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
        with mock.patch("sim_control.gui.NIDaqAdapter.list_devices", return_value=[]), mock.patch(
            "sim_control.gui.NIDaqAdapter.list_port0_lines",
            return_value=[],
        ):
            dialog = SimSettingsDialog(
                config=config,
                slm_adapter=slm_adapter,
                camera_adapter=camera_adapter,
            )
        dialog.daq_adapter = mock.Mock()

        with mock.patch.object(
            dialog,
            "_test_capture_path",
            return_value=Path("dummy.tiff"),
        ) as capture_path, mock.patch.object(
            dialog,
            "_write_uint16_tiff",
        ), mock.patch(
            "sim_control.gui.NIDaqWaveformBuilder",
            return_value=waveform_builder,
        ), mock.patch(
            "time.perf_counter",
            return_value=11.25,
        ):
            result = dialog._run_sim_acquisition_test(dialog.config.daq, acquisition_started_at_s=10.0)

        self.assertEqual(result.output_path, Path("dummy.tiff"))
        self.assertAlmostEqual(result.actual_acquisition_duration_s, 1.25)
        self.assertAlmostEqual(result.daq_waveform_duration_s, 4.5001)
        self.assertEqual(dialog.config.camera.exposure_us, 20_000)
        self.assertEqual(dialog.config.timing.inter_frame_gap_us, 10_000)
        slm_adapter.select_running_order.assert_called_once_with(1)
        capture_path.assert_called_once_with("sim_acquisition", "sim_acquisition_488nm_500ms")
        camera_config = camera_adapter.apply_config.call_args.args[0]
        self.assertEqual(camera_config.exposure_us, 500_000)
        self.assertEqual(waveform_builder.build.call_args.kwargs["exposure_us"], 500_000)
        timing_config = waveform_builder.build.call_args.kwargs["timing"]
        self.assertEqual(timing_config.inter_frame_gap_us, 50_000)
        dialog.daq_adapter.play_waveform.assert_called_once_with("Dev1", waveform_plan)
        dialog.close()

    def test_sim_acquisition_pulse_message_shows_actual_and_daq_durations_from_command_start(self):
        from sim_control.gui import SIM_ACQUISITION_TEST_ID, SimAcquisitionTestResult, SimSettingsDialog
        from sim_control.models import AppConfig

        with mock.patch("sim_control.gui.NIDaqAdapter.list_devices", return_value=[]), mock.patch(
            "sim_control.gui.NIDaqAdapter.list_port0_lines",
            return_value=[],
        ):
            dialog = SimSettingsDialog(config=AppConfig())
        dialog.combo_test_target.addItem("SIM采集", SIM_ACQUISITION_TEST_ID)
        dialog.combo_test_target.setCurrentIndex(0)
        events = []
        dialog._current_daq_config = mock.Mock(
            side_effect=lambda: events.append("daq_config") or dialog.config.daq
        )
        dialog._run_sim_acquisition_test = mock.Mock(
            return_value=SimAcquisitionTestResult(
                output_path=Path("dummy.tiff"),
                actual_acquisition_duration_s=1.25,
                daq_waveform_duration_s=0.6301,
            )
        )

        with mock.patch("time.perf_counter", side_effect=lambda: events.append("timer") or 123.0), mock.patch(
            "sim_control.gui.QMessageBox.information"
        ) as info_mock:
            dialog._run_pulse_test()

        self.assertEqual(events[:2], ["timer", "daq_config"])
        dialog._run_sim_acquisition_test.assert_called_once_with(
            dialog.config.daq,
            acquisition_started_at_s=123.0,
        )
        message = info_mock.call_args.args[2]
        self.assertIn("16位 TIFF 已保存到:\ndummy.tiff", message)
        self.assertIn("SIM采集实际用时: 1250.000 ms", message)
        self.assertIn("DAQ完整播放时长: 630.100 ms", message)
        dialog.close()


if __name__ == "__main__":
    unittest.main()

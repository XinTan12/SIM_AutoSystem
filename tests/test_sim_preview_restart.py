import importlib
import sys
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

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
    return importlib.import_module("control_wangbo.main")


class TimerSpy:
    def __init__(self):
        self.start_calls = []
        self.stop_calls = 0

    def start(self, interval_ms):
        self.start_calls.append(interval_ms)

    def stop(self):
        self.stop_calls += 1


class PreviewControllerSpy:
    def __init__(self):
        self.stop_calls = []

    def stop(self, wait=False):
        self.stop_calls.append(wait)


class LabelSpy:
    def __init__(self):
        self.text = None

    def setText(self, text):
        self.text = text


class ButtonSpy:
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
    def __init__(self, value=0, maximum=2304, enabled=True):
        self._value = value
        self._maximum = maximum
        self._enabled = enabled

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


class SignalBlockingSpinBoxSpy(SpinBoxSpy):
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
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance()
        if cls.app is None:
            cls.app = QtWidgets.QApplication([])

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
            stop_sim_preview=lambda wait=False, clear_restart=True: stop_calls.append((wait, clear_restart)),
            start_sim_preview=mock.Mock(),
        )

        legacy_main.MainWindow.btn_sCMOS_live_function(window)

        self.assertFalse(window.sim_preview_requested)
        self.assertFalse(window.sim_preview_restart_requested)
        self.assertEqual(stop_calls, [(False, True)])
        window.start_sim_preview.assert_not_called()

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
            start_single_acquisition=mock.Mock(return_value="task-1"),
        )
        window = SimpleNamespace(
            sim_camera_connected=True,
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
            stop_sim_preview=lambda wait=True: stop_calls.append(wait),
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

    def test_open_sim_settings_does_not_probe_real_camera_when_connected(self):
        legacy_main = load_legacy_main_module()
        dialog_instances = []

        class FakeSignal:
            def __init__(self):
                self.connected_callbacks = []

            def connect(self, callback):
                self.connected_callbacks.append(callback)

        class FakeDialog:
            def __init__(self, config, parent):
                self.config = config
                self.parent = parent
                self.exec_calls = 0
                self.signal_settings_saved = FakeSignal()
                dialog_instances.append(self)

            def exec_(self):
                self.exec_calls += 1

        window = SimpleNamespace(
            sim_camera_connected=True,
            sim_app_config=SimpleNamespace(),
            prefer_real_sim_hardware=mock.Mock(),
            apply_sim_settings=mock.Mock(),
        )

        with mock.patch.object(legacy_main, "SimSettingsDialog", FakeDialog):
            legacy_main.MainWindow.btn_openSimSettings_function(window)

        window.prefer_real_sim_hardware.assert_called_once_with(save_to_disk=True, probe_camera=False)
        self.assertEqual(len(dialog_instances), 1)
        self.assertEqual(
            dialog_instances[0].signal_settings_saved.connected_callbacks,
            [window.apply_sim_settings],
        )
        self.assertEqual(dialog_instances[0].exec_calls, 1)

    def test_apply_sim_settings_avoids_camera_reprobe_when_connected(self):
        legacy_main = load_legacy_main_module()
        from sim_control.models import AppConfig

        config = AppConfig()
        config.config_path = "dummy.json"
        window = SimpleNamespace(
            sim_camera_connected=True,
            sim_preview_active=False,
            sim_app_config=AppConfig(),
            prefer_real_sim_hardware=mock.Mock(),
            sync_sim_camera_controls_from_config=mock.Mock(),
            refresh_sim_settings_summary=mock.Mock(),
            refresh_sim_camera_devices=mock.Mock(),
            restart_sim_preview_with_current_settings=mock.Mock(),
        )

        with mock.patch.object(legacy_main, "save_app_config"):
            legacy_main.MainWindow.apply_sim_settings(window, config)

        window.prefer_real_sim_hardware.assert_called_once_with(save_to_disk=True, probe_camera=False)
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

    def test_refresh_sim_camera_bit_depth_choices_uses_supported_values_and_fallbacks_to_16_bit(self):
        legacy_main = load_legacy_main_module()
        camera = SimpleNamespace(bit_depth=10)
        bit_depth_combo = ComboBoxSpy(text="16-bit")
        window = SimpleNamespace(
            sim_app_config=SimpleNamespace(camera=camera),
            ui=SimpleNamespace(cmb_sCMOS_bitDepth=bit_depth_combo),
            refresh_sim_settings_summary=lambda: None,
        )

        legacy_main.MainWindow.refresh_sim_camera_bit_depth_choices(window, [12, 16])

        self.assertEqual(bit_depth_combo.items, ["12-bit", "16-bit"])
        self.assertEqual(bit_depth_combo.currentText(), "16-bit")
        self.assertEqual(camera.bit_depth, 16)

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

    def test_should_emit_frame_limits_gui_updates_to_latest_frame_at_30_fps(self):
        from sim_control.preview import SimPreviewWorker

        worker = SimPreviewWorker(gui_preview_fps_limit=30)
        worker._last_frame_emit_at = 10.0

        self.assertFalse(worker._should_emit_frame(now=10.01))
        self.assertTrue(worker._should_emit_frame(now=10.04))

    def test_emit_preview_frame_keeps_only_latest_queued_frame(self):
        from sim_control.preview import SimPreviewWorker

        worker = SimPreviewWorker(gui_preview_fps_limit=30)
        delivered = []
        worker.signal_frame_ready.connect(lambda frame, fps: delivered.append((frame, fps)))

        with mock.patch("sim_control.preview.time.perf_counter", side_effect=[1.0, 1.01, 1.05]):
            worker.emit_preview_frame(frame="frame-1", fps=1)
            worker.emit_preview_frame(frame="frame-2", fps=2)
            worker.emit_preview_frame(frame="frame-3", fps=3)

        self.assertEqual(delivered, [("frame-1", 1), ("frame-3", 3)])

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


class SimCameraSpinBoxCommitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance()
        if cls.app is None:
            cls.app = QtWidgets.QApplication([])

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


if __name__ == "__main__":
    unittest.main()

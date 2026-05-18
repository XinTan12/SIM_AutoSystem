"""PyQt5 UI 布局和生成文件回归测试。

测试重点锁住设置弹窗 DAQ 页、按钮尺寸、页签布局和生成 UI 代码声明，避免后续 UI 修改造成控件重叠、宽度退化或错误手改生成文件。
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PyQt5 import QtCore, QtWidgets


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class UiRegressionTests(unittest.TestCase):
    """锁定 PyQt5 设置界面和生成 UI 文件的布局回归行为。"""
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance()
        if cls.app is None:
            cls.app = QtWidgets.QApplication([])

    def test_cellsorting_ui_removes_legacy_elasticity_widgets_and_restructures_sim_controls(self):
        from control_wangbo.CellSorting_ui import Ui_Single_Cell_Sorting

        widget = QtWidgets.QWidget()
        ui = Ui_Single_Cell_Sorting()
        ui.setupUi(widget)

        self.assertFalse(hasattr(ui, "grp_collectedCellsROI_2"))
        self.assertFalse(hasattr(ui, "grp_sortROIView_2"))
        self.assertFalse(hasattr(ui, "groupBox_2"))

        texts = {
            child.text()
            for child in widget.findChildren((QtWidgets.QLabel, QtWidgets.QPushButton))
            if hasattr(child, "text")
        }
        titles = {group.title() for group in widget.findChildren(QtWidgets.QGroupBox)}
        all_text = texts | titles

        self.assertNotIn("Elasticity Measurement ROI", all_text)
        self.assertNotIn("Elasticity Measurement ROI View (sCMOS)", all_text)
        self.assertNotIn("Elasticity ROI Live View", all_text)

        self.assertNotIn("ROI Width", all_text)
        self.assertNotIn("ROI Height", all_text)
        self.assertNotIn("Start SIM Preview", all_text)
        self.assertNotIn("SIM Preview", all_text)
        self.assertNotIn("Delay(us)", all_text)
        self.assertNotIn("Save (sCMOS)", all_text)
        self.assertNotIn("#Pre_T", all_text)
        self.assertNotIn("#Post_T", all_text)

        self.assertIn("ROI_X", all_text)
        self.assertIn("ROI_Y", all_text)
        self.assertIn("Width", all_text)
        self.assertIn("Height", all_text)
        self.assertIn("Hardvare Connection (SIM Camera)", all_text)
        self.assertIn("Hardvare Connection (SLM)", all_text)
        self.assertIn("Connect SIM Camera", all_text)
        self.assertIn("Connect SLM", all_text)
        self.assertIn("Live", all_text)
        self.assertIn("Save", all_text)
        self.assertIn("Pre_T", all_text)
        self.assertIn("Post_T", all_text)

        self.assertTrue(hasattr(ui, "grp_hardvareConnection_sCMOS"))
        self.assertTrue(hasattr(ui, "grp_hardvareConnection_SLM"))
        self.assertTrue(hasattr(ui, "cmb_sCMOS_camera"))
        self.assertTrue(hasattr(ui, "cmb_SLM_device"))
        self.assertTrue(hasattr(ui, "btn_sCMOS_refresh"))
        self.assertTrue(hasattr(ui, "btn_SLM_refresh"))
        self.assertTrue(hasattr(ui, "btn_SLM_connection"))
        self.assertTrue(hasattr(ui, "lbl_SLM_status"))
        self.assertTrue(hasattr(ui, "btn_sCMOS_live"))
        self.assertTrue(hasattr(ui, "chb_sCMOS_autoContrast"))
        self.assertTrue(hasattr(ui, "spb_sCMOS_ROI_X"))
        self.assertTrue(hasattr(ui, "spb_sCMOS_ROI_Y"))
        self.assertTrue(hasattr(ui, "cmb_sCMOS_imageSize"))
        self.assertTrue(hasattr(ui, "cmb_sCMOS_bitDepth"))
        self.assertFalse(hasattr(ui, "spb_sCMOS_ringBufferCapacity"))
        self.assertFalse(hasattr(ui, "spb_sCMOS_delayTime"))
        self.assertFalse(hasattr(ui, "spb_sCMOS_pixelWidth"))
        self.assertFalse(hasattr(ui, "spb_sCMOS_pixelHeight"))

        self.assertIsInstance(ui.spb_sCMOS_ROI_X, QtWidgets.QSpinBox)
        self.assertIsInstance(ui.spb_sCMOS_ROI_Y, QtWidgets.QSpinBox)
        self.assertIsInstance(ui.spb_sCMOS_exposureTime, QtWidgets.QSpinBox)
        self.assertIsInstance(ui.cmb_sCMOS_imageSize, QtWidgets.QComboBox)
        self.assertIsInstance(ui.cmb_sCMOS_bitDepth, QtWidgets.QComboBox)
        self.assertIsInstance(ui.chb_sCMOS_autoContrast, QtWidgets.QCheckBox)
        self.assertFalse(ui.chb_sCMOS_autoContrast.isChecked())
        self.assertEqual(ui.chb_sCMOS_autoContrast.text(), "Auto Contrast")
        self.assertEqual(
            [ui.cmb_sCMOS_imageSize.itemText(index) for index in range(ui.cmb_sCMOS_imageSize.count())],
            ["2304 x 2304", "1152 x 1152", "576 x 576"],
        )
        self.assertEqual(
            [ui.cmb_sCMOS_bitDepth.itemText(index) for index in range(ui.cmb_sCMOS_bitDepth.count())],
            ["8-bit", "12-bit", "16-bit"],
        )
        self.assertEqual(ui.spb_sCMOS_exposureTime.minimum(), 1)
        self.assertEqual(ui.spb_sCMOS_exposureTime.maximum(), 10_000)
        self.assertEqual(ui.spb_sCMOS_exposureTime.value(), 10)
        self.assertIn("Exp(ms)", all_text)
        self.assertNotIn("Exp(us)", all_text)

    def test_cellsorting_main_title_matches_super_resolution_product_name(self):
        from control_wangbo.CellSorting_ui import Ui_Single_Cell_Sorting

        widget = QtWidgets.QWidget()
        ui = Ui_Single_Cell_Sorting()
        ui.setupUi(widget)

        self.assertEqual(widget.windowTitle(), "Intelligent Super-Resolution Imaging")
        self.assertEqual(ui.label.text(), "Intelligent Super-Resolution Imaging")

        ui_source = PROJECT_ROOT / "control_wangbo" / "CellSorting_ui.ui"
        self.assertIn(
            "<string>Intelligent Super-Resolution Imaging</string>",
            ui_source.read_text(encoding="utf-8"),
        )

    def test_cellsorting_left_column_uses_uniform_lines_between_groups(self):
        from control_wangbo.CellSorting_ui import Ui_Single_Cell_Sorting

        widget = QtWidgets.QWidget()
        ui = Ui_Single_Cell_Sorting()
        ui.setupUi(widget)

        column_groups = [
            ui.grp_hardvareConnection,
            ui.grp_hardvareConnection_sCMOS,
            ui.grp_hardvareConnection_2,
            ui.grp_releaseROI,
            ui.grp_captureROI,
            ui.grp_trappedROI,
            ui.grp_sortROI,
            ui.grp_collectedCellsROI,
            ui.grp_flowRateDetection,
        ]
        lines = [
            getattr(ui, name)
            for name in (
                "line_21",
                "line_18",
                "line_16",
                "line_19",
                "line_17",
                "line_22",
                "line_23",
                "line_24",
            )
        ]

        self.assertEqual(ui.grp_hardvareConnection.geometry().y(), 50)

        for upper, line, lower in zip(column_groups, lines, column_groups[1:]):
            self.assertEqual(line.geometry().x(), 10)
            self.assertEqual(line.geometry().width(), 291)
            self.assertEqual(line.geometry().height(), 1)
            self.assertEqual(line.geometry().y(), upper.geometry().y() + upper.geometry().height())
            self.assertEqual(lower.geometry().y(), line.geometry().y() + line.geometry().height())

        lowest_bottom = max(group.geometry().y() + group.geometry().height() for group in column_groups)
        self.assertLessEqual(
            lowest_bottom,
            widget.height(),
            "The entire left column should fit inside the main window height.",
        )

    def test_sim_settings_ui_removes_camera_tab_and_timing_controls(self):
        from sim_control.ui_sim_settings_dialog import Ui_SimSettingsDialog

        dialog = QtWidgets.QDialog()
        ui = Ui_SimSettingsDialog()
        ui.setupUi(dialog)

        self.assertTrue(hasattr(ui, "tab_daq"))
        self.assertFalse(hasattr(ui, "tab_camera"))
        self.assertFalse(hasattr(ui, "group_camera_main"))

        tab_titles = [ui.tabs.tabText(index) for index in range(ui.tabs.count())]
        self.assertNotIn("Camera", tab_titles)
        self.assertEqual(tab_titles, ["Laser", "DAQ"])
        self.assertFalse(hasattr(ui, "tab_patterns"))
        self.assertFalse(hasattr(ui, "combo_slm_device"))
        self.assertFalse(hasattr(ui, "btn_load_patterns"))
        self.assertTrue(hasattr(ui, "combo_test_target"))
        self.assertTrue(hasattr(ui, "btn_pulse_test"))
        self.assertIsInstance(ui.combo_test_target, QtWidgets.QComboBox)
        self.assertIsInstance(ui.btn_pulse_test, QtWidgets.QPushButton)
        texts = {
            child.text()
            for child in dialog.findChildren((QtWidgets.QLabel, QtWidgets.QPushButton))
            if hasattr(child, "text")
        }
        self.assertIn("Pulse Test", texts)
        self.assertIn("Test target", texts)
        self.assertNotIn("Validate Wiring", texts)

        self.assertFalse(hasattr(ui, "group_daq_timing"))
        for widget_name in (
            "spin_sample_rate",
            "spin_edge_pulse_us",
            "spin_inter_frame_gap_us",
            "spin_slm_enable_guard_us",
        ):
            self.assertFalse(hasattr(ui, widget_name))
        self.assertNotIn("Timing", texts)
        self.assertNotIn("Sample Rate (Hz)", texts)
        self.assertNotIn("Edge Pulse (us)", texts)
        self.assertNotIn("Inter Frame Gap (us)", texts)
        self.assertNotIn("SLM Enable Guard (us)", texts)

    def test_sim_settings_dialog_uses_native_style_without_pattern_specific_rules(self):
        from sim_control.ui_sim_settings_dialog import Ui_SimSettingsDialog

        dialog = QtWidgets.QDialog()
        ui = Ui_SimSettingsDialog()
        ui.setupUi(dialog)

        self.assertEqual(dialog.styleSheet(), "")
        self.assertFalse(hasattr(ui, "group_pattern_connection"))

    def test_sim_settings_ui_setup_does_not_emit_invalid_font_size_warnings(self):
        from sim_control.ui_sim_settings_dialog import Ui_SimSettingsDialog

        dialog = QtWidgets.QDialog()
        ui = Ui_SimSettingsDialog()
        messages = []

        def message_handler(_msg_type, _context, message):
            messages.append(message)

        previous_handler = QtCore.qInstallMessageHandler(message_handler)
        try:
            ui.setupUi(dialog)
        finally:
            QtCore.qInstallMessageHandler(previous_handler)

        self.assertFalse(
            any("QFont::setPointSize: Point size <= 0" in message for message in messages),
            "SIM settings dialog should not apply invalid font point sizes.",
        )

    def test_sim_settings_dialog_uses_compact_geometry_and_segoe_ui_fonts(self):
        from sim_control.ui_sim_settings_dialog import Ui_SimSettingsDialog

        dialog = QtWidgets.QDialog()
        ui = Ui_SimSettingsDialog()
        ui.setupUi(dialog)
        dialog.resize(980, 640)
        dialog.show()
        self.app.processEvents()

        self.assertEqual(dialog.minimumSize().width(), 920)
        self.assertEqual(dialog.minimumSize().height(), 600)
        self.assertEqual(dialog.height(), 640)
        self.assertEqual(dialog.font().family(), "Segoe UI")
        self.assertLessEqual(ui.dialogHeader.geometry().height(), 64)
        self.assertLessEqual(ui.tabs.geometry().y(), 80)
        self.assertEqual(ui.lbl_error.font().pointSize(), 11)
        self.assertGreaterEqual(ui.lbl_error.maximumHeight(), 28)
        self.assertEqual(ui.radio_laser_405.font().pointSize(), 13)
        self.assertEqual(ui.radio_laser_405.minimumSize().height(), 56)

    def test_sim_settings_daq_channel_controls_do_not_overlap_at_minimum_size(self):
        from sim_control.ui_sim_settings_dialog import Ui_SimSettingsDialog

        dialog = QtWidgets.QDialog()
        ui = Ui_SimSettingsDialog()
        ui.setupUi(dialog)
        dialog.resize(920, 600)
        dialog.show()
        self.app.processEvents()

        ui.tabs.setCurrentWidget(ui.tab_daq)
        self.app.processEvents()

        self.assertGreaterEqual(ui.group_daq.geometry().width(), 820)
        self.assertGreaterEqual(ui.combo_slm_enable_line.geometry().width(), 240)
        self.assertGreaterEqual(ui.combo_laser_647_line.geometry().width(), 240)
        self.assertGreaterEqual(ui.combo_test_target.geometry().width(), 300)

        labels = [
            ui.label_slm_enable_line,
            ui.label_slm_trigger_line,
            ui.label_slm_finish_line,
            ui.label_camera_trigger_line,
            ui.label_laser_405_line,
            ui.label_laser_488_line,
            ui.label_laser_561_line,
            ui.label_laser_647_line,
        ]
        combos = [
            ui.combo_slm_enable_line,
            ui.combo_slm_trigger_line,
            ui.combo_slm_finish_line,
            ui.combo_camera_trigger_line,
            ui.combo_laser_405_line,
            ui.combo_laser_488_line,
            ui.combo_laser_561_line,
            ui.combo_laser_647_line,
        ]

        for label in labels:
            self.assertGreaterEqual(label.geometry().height(), 20)
        for combo in combos:
            self.assertGreaterEqual(combo.geometry().height(), 36)

        for left_index, left_combo in enumerate(combos):
            left_rect = left_combo.geometry()
            for right_combo in combos[left_index + 1 :]:
                self.assertFalse(
                    left_rect.intersects(right_combo.geometry()),
                    f"{left_combo.objectName()} overlaps {right_combo.objectName()}",
                )

    def test_sim_settings_daq_wiring_uses_vertical_space_evenly(self):
        from sim_control.ui_sim_settings_dialog import Ui_SimSettingsDialog

        dialog = QtWidgets.QDialog()
        ui = Ui_SimSettingsDialog()
        ui.setupUi(dialog)
        dialog.resize(980, 640)
        dialog.show()
        self.app.processEvents()

        ui.tabs.setCurrentWidget(ui.tab_daq)
        self.app.processEvents()

        line_combos = [
            ui.combo_slm_enable_line,
            ui.combo_slm_trigger_line,
            ui.combo_slm_finish_line,
            ui.combo_camera_trigger_line,
            ui.combo_laser_405_line,
            ui.combo_laser_488_line,
            ui.combo_laser_561_line,
            ui.combo_laser_647_line,
        ]
        device_bottom = ui.combo_daq_device.geometry().bottom()
        lines_top = min(combo.geometry().top() for combo in line_combos)
        lines_bottom = max(combo.geometry().bottom() for combo in line_combos)
        test_top = ui.combo_test_target.geometry().top()
        test_bottom = max(
            ui.combo_test_target.geometry().bottom(),
            ui.btn_pulse_test.geometry().bottom(),
        )
        group_bottom = ui.group_daq.contentsRect().bottom()

        self.assertGreaterEqual(lines_top - device_bottom, 35)
        self.assertGreaterEqual(test_top - lines_bottom, 35)
        self.assertLessEqual(group_bottom - test_bottom, 100)

    def test_sim_control_window_limits_runtime_log_blocks(self):
        import tempfile

        from sim_control.config_store import save_app_config
        from sim_control.gui import SimControlWindow
        from sim_control.models import AppConfig, BackendConfig

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "sim_config.json"
            save_app_config(AppConfig(backend=BackendConfig(simulation_mode=True)), config_path)
            window = SimControlWindow(config_path=str(config_path))
            try:
                self.assertEqual(window.log_output.document().maximumBlockCount(), 1000)
            finally:
                window.close()

    def test_sim_control_window_prepare_experiment_uses_running_order_not_manual_patterns(self):
        import tempfile

        from sim_control.config_store import save_app_config
        from sim_control.gui import SimControlWindow
        from sim_control.models import AppConfig, BackendConfig

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "sim_config.json"
            save_app_config(AppConfig(backend=BackendConfig(simulation_mode=True)), config_path)
            window = SimControlWindow(config_path=str(config_path))
            original_controller = window.controller
            controller = SimpleNamespace(
                slm_adapter=SimpleNamespace(is_connected=mock.Mock(return_value=True)),
                start_prepare_experiment=mock.Mock(return_value="prepare-1"),
                apply_daq_config=mock.Mock(side_effect=AssertionError("DAQ preflight should run in worker")),
                initialize_hardware=mock.Mock(side_effect=AssertionError("hardware init should run in worker")),
                apply_camera_config=mock.Mock(side_effect=AssertionError("camera preflight should run in worker")),
                select_running_order_for_task=mock.Mock(side_effect=AssertionError("RO selection should run in worker")),
                prepare_patterns=mock.Mock(side_effect=AssertionError("manual pattern path should not be used")),
            )
            window.controller = controller

            try:
                window._prepare_experiment()
            finally:
                window.controller = original_controller
                window.close()

        controller.start_prepare_experiment.assert_called_once()
        prepare_kwargs = controller.start_prepare_experiment.call_args.kwargs
        self.assertTrue(prepare_kwargs["prepare_running_order"])
        self.assertTrue(prepare_kwargs["initialize_hardware"])
        self.assertTrue(prepare_kwargs["apply_daq_config"])
        self.assertTrue(prepare_kwargs["apply_camera_config"])
        controller.prepare_patterns.assert_not_called()

    def test_sim_control_window_run_acquisition_defers_preflight_to_worker(self):
        import tempfile

        from sim_control.config_store import save_app_config
        from sim_control.gui import SimControlWindow
        from sim_control.models import AppConfig, BackendConfig

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "sim_config.json"
            save_app_config(AppConfig(backend=BackendConfig(simulation_mode=True)), config_path)
            window = SimControlWindow(config_path=str(config_path))
            original_controller = window.controller
            controller = SimpleNamespace(
                slm_adapter=SimpleNamespace(is_connected=mock.Mock(return_value=True)),
                start_single_acquisition=mock.Mock(return_value="task-1"),
                apply_daq_config=mock.Mock(side_effect=AssertionError("DAQ preflight should run in worker")),
                initialize_hardware=mock.Mock(side_effect=AssertionError("hardware init should run in worker")),
                apply_camera_config=mock.Mock(side_effect=AssertionError("camera preflight should run in worker")),
                select_running_order_for_task=mock.Mock(side_effect=AssertionError("RO selection should run in worker")),
                prepare_patterns=mock.Mock(side_effect=AssertionError("manual pattern path should not be used")),
            )
            window.controller = controller

            try:
                window._run_single_acquisition()
            finally:
                window.controller = original_controller
                window.close()

        controller.start_single_acquisition.assert_called_once()
        run_kwargs = controller.start_single_acquisition.call_args.kwargs
        self.assertTrue(run_kwargs["prepare_running_order"])
        self.assertTrue(run_kwargs["initialize_hardware"])
        self.assertTrue(run_kwargs["apply_daq_config"])
        self.assertTrue(run_kwargs["apply_camera_config"])

    def test_sim_control_window_run_acquisition_blocks_when_slm_is_disconnected(self):
        import tempfile

        from sim_control.config_store import save_app_config
        from sim_control.gui import SimControlWindow
        from sim_control.models import AppConfig, BackendConfig

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "sim_config.json"
            save_app_config(AppConfig(backend=BackendConfig(simulation_mode=True)), config_path)
            window = SimControlWindow(config_path=str(config_path))
            original_controller = window.controller
            controller = SimpleNamespace(
                slm_adapter=SimpleNamespace(is_connected=mock.Mock(return_value=False)),
                start_single_acquisition=mock.Mock(return_value="task-1"),
            )
            window.controller = controller

            try:
                window._run_single_acquisition()
            finally:
                window.controller = original_controller
                window.close()

        controller.start_single_acquisition.assert_not_called()

    def test_sim_control_window_syncs_running_order_selected_from_worker_status(self):
        import tempfile

        from sim_control.config_store import save_app_config
        from sim_control.gui import SimControlWindow
        from sim_control.models import AppConfig, BackendConfig

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "sim_config.json"
            save_app_config(AppConfig(backend=BackendConfig(simulation_mode=True)), config_path)
            window = SimControlWindow(config_path=str(config_path))
            try:
                window.config.selected_running_order = ""
                window._handle_status_changed(
                    "running_order_selected",
                    {"running_order_name": "488_3.5_2d_1ms"},
                )
                self.assertEqual(window.config.selected_running_order, "488_3.5_2d_1ms")
            finally:
                window.close()


if __name__ == "__main__":
    unittest.main()

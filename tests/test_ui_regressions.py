import sys
import unittest
from pathlib import Path

from PyQt5 import QtCore, QtWidgets


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class UiRegressionTests(unittest.TestCase):
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

    def test_sim_settings_ui_removes_camera_tab_and_keeps_timing_controls_in_daq(self):
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

        for widget_name in (
            "spin_sample_rate",
            "spin_edge_pulse_us",
            "spin_inter_frame_gap_us",
            "spin_slm_enable_guard_us",
        ):
            self.assertTrue(hasattr(ui, widget_name))
            widget = getattr(ui, widget_name)
            self.assertIsNotNone(widget.parent())

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

        self.assertEqual(dialog.minimumSize().width(), 920)
        self.assertEqual(dialog.minimumSize().height(), 600)
        self.assertEqual(dialog.height(), 640)
        self.assertEqual(dialog.font().family(), "Segoe UI")
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


if __name__ == "__main__":
    unittest.main()

import sys
import unittest
from pathlib import Path

from PyQt5 import QtWidgets


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
        self.assertIn("Connect SIM Camera", all_text)
        self.assertIn("Live", all_text)
        self.assertIn("Save", all_text)
        self.assertIn("Pre_T", all_text)
        self.assertIn("Post_T", all_text)

        self.assertTrue(hasattr(ui, "grp_hardvareConnection_sCMOS"))
        self.assertTrue(hasattr(ui, "cmb_sCMOS_camera"))
        self.assertTrue(hasattr(ui, "btn_sCMOS_refresh"))
        self.assertTrue(hasattr(ui, "btn_sCMOS_live"))
        self.assertTrue(hasattr(ui, "spb_sCMOS_ROI_X"))
        self.assertTrue(hasattr(ui, "spb_sCMOS_ROI_Y"))
        self.assertFalse(hasattr(ui, "spb_sCMOS_ringBufferCapacity"))
        self.assertFalse(hasattr(ui, "spb_sCMOS_delayTime"))

        self.assertIsInstance(ui.spb_sCMOS_ROI_X, QtWidgets.QSpinBox)
        self.assertIsInstance(ui.spb_sCMOS_ROI_Y, QtWidgets.QSpinBox)
        self.assertIsInstance(ui.spb_sCMOS_exposureTime, QtWidgets.QSpinBox)
        self.assertEqual(ui.spb_sCMOS_pixelWidth.maximum(), 2304)
        self.assertEqual(ui.spb_sCMOS_pixelHeight.maximum(), 2304)
        self.assertEqual(ui.spb_sCMOS_exposureTime.minimum(), 1)
        self.assertEqual(ui.spb_sCMOS_exposureTime.maximum(), 10_000_000)

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
        self.assertEqual(tab_titles, ["DAQ", "Patterns", "Laser"])

        for widget_name in (
            "spin_sample_rate",
            "spin_edge_pulse_us",
            "spin_inter_frame_gap_us",
            "spin_slm_enable_guard_us",
        ):
            self.assertTrue(hasattr(ui, widget_name))
            widget = getattr(ui, widget_name)
            self.assertIsNotNone(widget.parent())

    def test_sim_settings_patterns_page_is_not_left_shifted_relative_to_other_tabs(self):
        from PyQt5 import QtCore
        from sim_control.ui_sim_settings_dialog import Ui_SimSettingsDialog

        dialog = QtWidgets.QDialog()
        ui = Ui_SimSettingsDialog()
        ui.setupUi(dialog)
        dialog.show()
        self.app.processEvents()

        patterns_index = ui.tabs.indexOf(ui.tab_patterns)
        ui.tabs.setCurrentIndex(patterns_index)
        self.app.processEvents()

        top_left = ui.group_pattern_connection.mapTo(dialog, QtCore.QPoint(0, 0))
        self.assertGreaterEqual(
            top_left.x(),
            120,
            "Patterns page content should stay centered instead of hugging the left edge.",
        )

    def test_sim_settings_patterns_page_keeps_expected_height_budget(self):
        from sim_control.ui_sim_settings_dialog import Ui_SimSettingsDialog

        dialog = QtWidgets.QDialog()
        ui = Ui_SimSettingsDialog()
        ui.setupUi(dialog)

        self.assertEqual(
            ui.tab_patterns.minimumHeight(),
            601,
            "Patterns page should reserve the same vertical space budget as the other tabs.",
        )


if __name__ == "__main__":
    unittest.main()

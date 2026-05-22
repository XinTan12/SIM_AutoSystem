import sys
import unittest
from pathlib import Path

from PyQt5 import QtWidgets


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class SimFooterHarmonizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_four_bottom_widgets_share_height_24_and_top_y_486(self):
        from control_wangbo.CellSorting_ui import Ui_Single_Cell_Sorting

        host = QtWidgets.QWidget()
        ui = Ui_Single_Cell_Sorting()
        ui.setupUi(host)

        self.assertTrue(hasattr(ui, "layoutWidget27"), "pyuic5 layoutWidget name drifted; update this test and main.py.")
        widgets = [
            ui.lb_sCMOS_FPSshow,
            ui.lb_FPSshow_7,
            ui.chb_sCMOS_autoContrast,
            ui.lbl_z_position_label,
            ui.lbl_z_position_value,
            ui.layoutWidget27,
            ui.label_79,
            ui.spb_sCMOS_displayGray_max,
        ]
        for widget in widgets:
            self.assertEqual(widget.geometry().height(), 24, widget.objectName())

        outer = (
            ui.lb_sCMOS_FPSshow,
            ui.lb_FPSshow_7,
            ui.chb_sCMOS_autoContrast,
            ui.lbl_z_position_label,
            ui.lbl_z_position_value,
            ui.layoutWidget27,
        )
        for widget in outer:
            self.assertEqual(widget.geometry().y(), 486, widget.objectName())

        rects = [widget.geometry() for widget in outer]
        for index, rect in enumerate(rects):
            for other in rects[index + 1 :]:
                self.assertFalse(rect.intersects(other))


if __name__ == "__main__":
    unittest.main()

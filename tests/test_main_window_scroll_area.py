"""历史 CellSorting 主界面滚动容器测试。

测试确认 setup_scrollable_cellsorting_ui 会把固定尺寸的旧 UI 放进 QScrollArea，既保留原布局尺寸，又允许较小窗口滚动查看。
"""

import importlib
import sys
import types
import unittest
from pathlib import Path

from PyQt5 import QtWidgets


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTROL_ROOT = PROJECT_ROOT / "control_wangbo"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(CONTROL_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_ROOT))

sys.modules.setdefault("MCUTriggerThread", types.ModuleType("MCUTriggerThread"))
sys.modules.setdefault("mvsdk", types.ModuleType("mvsdk"))
sys.modules.setdefault("FastCameraThread", types.ModuleType("FastCameraThread"))


class MainWindowScrollAreaTests(unittest.TestCase):
    """验证历史主界面滚动容器封装不破坏窗口基础属性。"""
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance()
        if cls.app is None:
            cls.app = QtWidgets.QApplication([])

    def test_cellsorting_ui_wrapper_exposes_scrollbars_when_host_is_smaller(self):
        legacy_main = importlib.import_module("control_wangbo.main")
        helper = getattr(legacy_main, "setup_scrollable_cellsorting_ui", None)
        self.assertTrue(
            callable(helper),
            "control_wangbo.main should expose setup_scrollable_cellsorting_ui().",
        )

        from control_wangbo.CellSorting_ui import Ui_Single_Cell_Sorting

        host = QtWidgets.QWidget()
        ui = Ui_Single_Cell_Sorting()
        scroll_area, content_widget = helper(host, ui)

        self.assertIsInstance(scroll_area, QtWidgets.QScrollArea)
        self.assertIs(scroll_area.widget(), content_widget)
        self.assertEqual(content_widget.minimumWidth(), 1800)
        self.assertEqual(content_widget.minimumHeight(), 1000)

        host.resize(900, 600)
        host.show()
        self.app.processEvents()

        self.assertGreater(scroll_area.horizontalScrollBar().maximum(), 0)
        self.assertGreater(scroll_area.verticalScrollBar().maximum(), 0)

        host.close()


if __name__ == "__main__":
    unittest.main()

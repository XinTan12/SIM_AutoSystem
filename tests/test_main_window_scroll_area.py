"""历史 CellSorting 主界面滚动容器测试。

作用：
    确认 ``control_wangbo/main.py`` 中的 ``setup_scrollable_cellsorting_ui`` helper
    满足以下不变量：
        - 把固定尺寸的旧 Ui 放进 ``QScrollArea``。
        - 保留 ``content_widget`` 的最小尺寸 ``1800×1000`` 像素（旧 UI 设计尺寸）。
        - 当 host 窗口比内容小时，``QScrollArea`` 出现水平/垂直滚动条。

协作关系：
    上游：``unittest``、``PyQt5`` 真实 QApplication。
    下游：``control_wangbo/main.py``（仅读取 helper 函数）、``control_wangbo/CellSorting_ui.py``。

维护要点：
    - 测试导入 ``control_wangbo.main`` 时会触发它对 ``MCUTriggerThread`` / ``mvsdk`` /
      ``FastCameraThread`` 的 import；缺失这些模块会 ImportError，因此本文件用
      ``sys.modules.setdefault(...)`` 提前注入空 stub。
    - 滚动条出现的判定基于 ``host.resize(900, 600)``；如果旧 UI 设计尺寸调整，需同步
      修改这里的 host 尺寸。
"""

import importlib
import sys
import types
import unittest
from pathlib import Path

from PyQt5 import QtWidgets


# 项目根与 control_wangbo 子目录都加入 sys.path，因 control_wangbo/main.py 用相对裸 import。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTROL_ROOT = PROJECT_ROOT / "control_wangbo"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(CONTROL_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_ROOT))

# ``control_wangbo/main.py`` 启动时会 ``import MCUTriggerThread`` 等顶层模块；
# 测试机器可能缺失对应硬件 SDK 依赖，这里用空模块 stub 让 import 成功。
sys.modules.setdefault("MCUTriggerThread", types.ModuleType("MCUTriggerThread"))
sys.modules.setdefault("mvsdk", types.ModuleType("mvsdk"))
sys.modules.setdefault("FastCameraThread", types.ModuleType("FastCameraThread"))


class MainWindowScrollAreaTests(unittest.TestCase):
    """验证 ``setup_scrollable_cellsorting_ui`` 在小窗口下提供滚动条。"""

    @classmethod
    def setUpClass(cls):
        # 1) 复用进程已有的 QApplication；不存在则新建。多 test 共享一个实例。
        cls.app = QtWidgets.QApplication.instance()
        if cls.app is None:
            cls.app = QtWidgets.QApplication([])

    def test_cellsorting_ui_wrapper_exposes_scrollbars_when_host_is_smaller(self):
        """host 窗口 900×600 时，wrapper 必须能让用户在两个方向滚动。"""
        # 1) 动态 import 让上面 stub 优先生效，避免 import-time 错误。
        legacy_main = importlib.import_module("control_wangbo.main")
        helper = getattr(legacy_main, "setup_scrollable_cellsorting_ui", None)
        self.assertTrue(
            callable(helper),
            "control_wangbo.main should expose setup_scrollable_cellsorting_ui().",
        )

        # 2) 把旧 UI 类 + host 容器交给 helper；它返回 (scroll_area, content_widget)。
        from control_wangbo.CellSorting_ui import Ui_Single_Cell_Sorting

        host = QtWidgets.QWidget()
        ui = Ui_Single_Cell_Sorting()
        scroll_area, content_widget = helper(host, ui)

        # 3) 类型与拥有关系断言：scroll_area 必须是 QScrollArea，且持有 content_widget。
        self.assertIsInstance(scroll_area, QtWidgets.QScrollArea)
        self.assertIs(scroll_area.widget(), content_widget)
        # 4) content_widget 必须保留旧 UI 的最小尺寸约束。
        self.assertEqual(content_widget.minimumWidth(), 1800)
        self.assertEqual(content_widget.minimumHeight(), 1000)

        # 5) 把 host 调小，触发滚动条出现；``processEvents`` 让 Qt 完成 layout 计算。
        host.resize(900, 600)
        host.show()
        self.app.processEvents()

        # 6) 横纵滚动条的 maximum > 0 表示真的有可滚动距离。
        self.assertGreater(scroll_area.horizontalScrollBar().maximum(), 0)
        self.assertGreater(scroll_area.verticalScrollBar().maximum(), 0)

        # 7) 清理：关闭 host 防止后续测试看到遗留窗口。
        host.close()


if __name__ == "__main__":
    unittest.main()

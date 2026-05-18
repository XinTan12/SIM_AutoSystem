"""项目集成 GUI 的顶层启动入口。

这个文件只做启动前的最小准备：把仓库根目录和历史微流控目录加入 Python 导入路径，创建 PyQt5 QApplication，然后打开 control_wangbo.main.MainWindow。真正的微流控控制、SIM 设置、相机/SLM/DAQ 初始化都在窗口和控制器层完成，因此这里应保持轻量，避免放入硬件阻塞逻辑。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication


PROJECT_ROOT = Path(__file__).resolve().parent
CONTROL_WANGBO_ROOT = PROJECT_ROOT / "control_wangbo"


def configure_import_paths() -> None:
    """把项目根目录和 control_wangbo 目录加入 sys.path，保证从根目录启动时能导入历史主界面。"""
    for path in (PROJECT_ROOT, CONTROL_WANGBO_ROOT):
        path_text = str(path)
        if path_text not in sys.path:
            sys.path.insert(0, path_text)


def main() -> int:
    """创建 PyQt5 应用对象、打开集成主窗口，并进入 Qt 事件循环。"""
    configure_import_paths()
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    from control_wangbo.main import MainWindow

    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())

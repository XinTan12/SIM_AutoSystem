"""项目集成 GUI 的顶层启动入口。

作用：
    本文件是 ``start.cmd`` 与 ``python app.py`` 进入项目集成主界面的唯一入口；
    它把仓库根目录和 ``control_wangbo/`` 历史微流控目录加入 ``sys.path``，
    再创建 PyQt5 ``QApplication`` 并打开 ``control_wangbo.main.MainWindow``。
    真正的微流控控制、SIM 设置弹窗、相机/SLM/DAQ 初始化等业务都在窗口和
    SIM ``SimAcquisitionController`` 中完成，因此本入口必须保持轻量，
    任何硬件阻塞调用、文件 IO 或耗时初始化都不应直接放在这里。

协作关系：
    上游：``start.cmd`` 与 IDE 调试器 → 调本文件 ``main()``。
    下游：``control_wangbo/main.py``（集成主窗口，承载相机/MCU/SIM 集成 UI）。
    相关：``sim_control/sim_acquisition_app.py``（独立调 SIM 采集 GUI 的另一个入口）。

关键概念：
    - ``configure_import_paths()``：把仓库根目录和 ``control_wangbo/`` 写到
      ``sys.path[0]``，使得在任何工作目录下启动 ``app.py`` 都能 import 历史模块。
    - High-DPI 属性：在 ``QApplication`` 创建之前设置 ``AA_EnableHighDpiScaling``
      和 ``AA_UseHighDpiPixmaps``，否则属性会被 Qt 忽略。

维护要点：
    - 不要在 ``main()`` 之外做相机/SLM/DAQ 连接，初始化必须保留在控制器层。
    - 启动顺序固定：先设 High-DPI 属性 → 再 import 主窗口 → 再实例化
      ``QApplication``；调换顺序会导致 DPI 设置失效或 ``QApplication`` 警告。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication


# 仓库根目录由本文件路径推导。``__file__`` 在打包/调试两种场景下都可用。
PROJECT_ROOT = Path(__file__).resolve().parent
# ``control_wangbo`` 是队友历史微流控控制代码所在目录，主界面 import 必须依赖它。
CONTROL_WANGBO_ROOT = PROJECT_ROOT / "control_wangbo"


def configure_import_paths() -> None:
    """把项目根目录和 ``control_wangbo/`` 写入 ``sys.path``，让任意工作目录都能启动。

    用途：
        ``control_wangbo/main.py`` 内部以 ``from xxx import yyy`` 形式 import
        其它历史模块（FastCameraThread、MCUTriggerThread 等），这些模块都位于
        ``control_wangbo/`` 目录内；如果启动时当前工作目录不是仓库根，Python 找
        不到这些模块。本函数把两个关键目录强制插到 ``sys.path[0]``，保证 import 成功。

    副作用：
        修改进程级 ``sys.path``。重复调用是幂等的（已存在则跳过）。
    """
    # 1) 遍历需要纳入 sys.path 的两个目录，保持「项目根优先」的搜索顺序。
    for path in (PROJECT_ROOT, CONTROL_WANGBO_ROOT):
        path_text = str(path)
        # 2) 仅在 sys.path 不含该路径时插入，避免重复污染搜索链。
        if path_text not in sys.path:
            sys.path.insert(0, path_text)


def main() -> int:
    """创建 PyQt5 应用对象、打开集成主窗口，并进入 Qt 事件循环。

    返回：
        Qt 事件循环退出码，将由 ``raise SystemExit(main())`` 转给操作系统。

    副作用：
        - 修改 ``sys.path``（通过 ``configure_import_paths()``）。
        - 设置 Qt 全局 High-DPI 属性。
        - 创建 ``QApplication`` 与主窗口，进入事件循环阻塞当前线程。
    """
    # 1) 先扩展 import 路径，否则下面 ``from control_wangbo.main import ...`` 会失败。
    configure_import_paths()
    # 2) 在任何 QApplication 实例化之前打开 High-DPI 缩放与高清位图支持；
    #    这两条属性如果放到 QApplication() 之后再设，Qt 会静默忽略。
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    # 3) 延迟 import 集成主窗口：放到函数内部是因为它依赖于上面刚配置的 sys.path。
    from control_wangbo.main import MainWindow

    # 4) 创建唯一 QApplication 实例与主窗口，并进入 Qt 事件循环；
    #    ``app.exec_()`` 会阻塞直到用户关闭主窗口，返回退出码。
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec_()


if __name__ == "__main__":
    # 命令行直接运行 ``python app.py`` 时，把 ``main()`` 返回值作为进程退出码。
    raise SystemExit(main())

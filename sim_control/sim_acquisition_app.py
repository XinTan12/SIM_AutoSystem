"""独立 SIM 采集 GUI 的命令行入口。

作用：
    本文件用于**单独**启动 ``SimControlWindow``，便于在不打开项目集成主界面的
    情况下调试 SIM 采集链路：相机配置、
    SLM Running Order、NI USB-6423 波形输出、9 帧采集与预览等。
    它与仓库根 ``app.py`` 的核心差异是：``app.py`` 启动集成主界面，
    本入口只启动 SIM 设置窗口。

协作关系：
    上游：``python -m sim_control.sim_acquisition_app --config <path>`` 命令行。
    下游：``sim_control.gui.SimControlWindow`` 主窗口（承载所有 SIM UI 与
          控制器交互）。
    相关：
        - ``sim_control.config_store.DEFAULT_CONFIG_PATH``：默认 JSON 配置路径。
        - ``app.py``：项目集成主界面入口。

关键概念：
    - 命令行参数 ``--config``：允许调试时指向自定义配置 JSON，例如临时
      测试不同 ROI 或不同 SLM Running Order 的快照配置。

维护要点：
    - 入口必须保持轻量：不要在 ``main()`` 中执行硬件 SDK 连接、文件 IO 或
      耗时初始化；这些应留给 ``SimControlWindow`` 或 ``SimAcquisitionController``。
    - 与 ``app.py`` 一致，High-DPI 属性必须在 ``QApplication`` 实例化之前设置，
      否则会被 Qt 忽略。
"""

from __future__ import annotations

import argparse
import sys

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from .config_store import DEFAULT_CONFIG_PATH
from .gui import SimControlWindow


def parse_args() -> argparse.Namespace:
    """解析命令行参数，得到本次启动要使用的配置文件路径。

    返回：
        ``argparse.Namespace``，包含 ``config`` 字符串字段。
        未提供 ``--config`` 时回落到 ``DEFAULT_CONFIG_PATH``（``config/sim_control_config.json``）。
    """
    # 1) 构建 argparse 解析器：只暴露一个可选参数 ``--config``，保持调试入口极简。
    parser = argparse.ArgumentParser(description="Launch the standalone SIM acquisition window.")
    # 2) 默认值取自 ``DEFAULT_CONFIG_PATH``，调试者只在切换配置时显式覆盖。
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Path to the SIM control JSON config file.",
    )
    # 3) 命中 ``--help`` 时由 argparse 直接打印帮助并退出；否则返回参数命名空间。
    return parser.parse_args()


def main() -> int:
    """解析配置参数，创建 ``QApplication`` 并显示独立 SIM 采集窗口。

    返回：
        Qt 事件循环的退出码，由 ``raise SystemExit(main())`` 转给操作系统。

    副作用：
        - 设置进程级 Qt High-DPI 属性。
        - 创建 ``QApplication`` 与 ``SimControlWindow``，进入事件循环阻塞当前线程。
        - 通过 ``SimControlWindow(config_path=...)`` 间接触发配置文件读取。
    """
    # 1) 先解析参数，拿到本次要用的配置路径；任何后续步骤都依赖它。
    args = parse_args()
    # 2) 与 ``app.py`` 一致：High-DPI 属性必须先于 QApplication 创建。
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    # 3) 创建 QApplication 后立即实例化主窗口；窗口内部会从配置文件加载 AppConfig。
    app = QApplication(sys.argv)
    window = SimControlWindow(config_path=args.config)
    window.show()
    # 4) 进入 Qt 事件循环；返回值通常是 0（正常退出）或非 0（异常退出）。
    return app.exec_()


if __name__ == "__main__":
    # 直接命令行运行时，把 ``main()`` 返回值作为进程退出码交回操作系统。
    raise SystemExit(main())

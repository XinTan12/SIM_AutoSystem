"""独立 SIM 采集 GUI 的命令行入口。

这个入口只启动 sim_control.gui.SimControlWindow，用于单独调试 SIM 采集链路。它和根目录 app.py 不同，不加载 control_wangbo 的集成主界面。
"""

from __future__ import annotations

import argparse
import sys

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from .config_store import DEFAULT_CONFIG_PATH
from .gui import SimControlWindow


def parse_args() -> argparse.Namespace:
    """把用户或配置中的文本形式解析成后续流程可直接使用的结构化值。"""
    parser = argparse.ArgumentParser(description="Launch the standalone SIM acquisition window.")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Path to the SIM control JSON config file.",
    )
    return parser.parse_args()


def main() -> int:
    """解析配置参数，创建 QApplication 并显示独立 SIM 采集窗口。"""
    args = parse_args()
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    window = SimControlWindow(config_path=args.config)
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())

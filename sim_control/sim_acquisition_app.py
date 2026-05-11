from __future__ import annotations

import argparse
import sys

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from .config_store import DEFAULT_CONFIG_PATH
from .gui import SimControlWindow


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch the standalone SIM acquisition window.")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Path to the SIM control JSON config file.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    window = SimControlWindow(config_path=args.config)
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())

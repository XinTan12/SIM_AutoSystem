from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from sim_control.gui import SimControlWindow


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch the SIM control window.")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parent / "config" / "sim_control_config.json"),
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

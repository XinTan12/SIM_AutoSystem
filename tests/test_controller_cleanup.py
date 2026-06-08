from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class ControllerCleanupTests(unittest.TestCase):
    def test_shutdown_logs_disconnect_failures_without_reraising(self):
        from PyQt5.QtWidgets import QApplication

        from sim_control.controller import SimAcquisitionController
        from sim_control.models import BackendConfig

        app = QApplication.instance()
        if app is None:
            app = QApplication([])

        controller = SimAcquisitionController(backend=BackendConfig(simulation_mode=True))
        controller.camera_adapter.disconnect = mock.Mock(side_effect=RuntimeError("camera disconnect failed"))
        controller.slm_adapter.disconnect = mock.Mock(side_effect=RuntimeError("slm disconnect failed"))
        controller.stage_adapter.disconnect = mock.Mock(side_effect=RuntimeError("stage disconnect failed"))

        with self.assertLogs("sim_control.controller", level="WARNING") as logs:
            controller.shutdown()

        joined = "\n".join(logs.output)
        self.assertIn("camera", joined)
        self.assertIn("SLM", joined)
        self.assertIn("stage", joined)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class AdapterFactoryTests(unittest.TestCase):
    def test_simulation_bundle_matches_controller_adapter_types(self):
        from PyQt5.QtWidgets import QApplication

        from sim_control.adapter_factory import create_adapter_bundle
        from sim_control.controller import SimAcquisitionController
        from sim_control.models import BackendConfig

        app = QApplication.instance()
        if app is None:
            app = QApplication([])

        backend = BackendConfig(simulation_mode=True)
        bundle = create_adapter_bundle(backend)
        controller = SimAcquisitionController(backend=backend)
        try:
            self.assertIsInstance(controller.camera_adapter, type(bundle.camera))
            self.assertIsInstance(controller.slm_adapter, type(bundle.slm))
            self.assertIsInstance(controller.daq_adapter, type(bundle.daq))
            self.assertIsInstance(controller.stage_adapter, type(bundle.stage))
        finally:
            controller.shutdown()


if __name__ == "__main__":
    unittest.main()

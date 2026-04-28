import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class ConfigValidationTests(unittest.TestCase):
    def test_validate_app_config_reports_invalid_camera_and_timing_values(self):
        from sim_control.config_store import validate_app_config
        from sim_control.models import AppConfig

        config = AppConfig()
        config.camera.exposure_us = 0
        config.camera.roi_width = -1
        config.timing.sample_rate_hz = 999
        config.timing.edge_pulse_us = 0
        config.selected_laser_nm = 640

        errors = validate_app_config(config)

        self.assertTrue(any("exposure_us" in error for error in errors))
        self.assertTrue(any("roi_width" in error for error in errors))
        self.assertTrue(any("sample_rate_hz" in error for error in errors))
        self.assertTrue(any("edge_pulse_us" in error for error in errors))
        self.assertTrue(any("selected_laser_nm" in error for error in errors))

    def test_validate_app_config_allows_empty_legacy_pattern_files_when_running_order_is_selected(self):
        from sim_control.config_store import validate_app_config
        from sim_control.models import AppConfig

        config = AppConfig(pattern_files=[], selected_running_order="488_3.5_2d_1ms")

        errors = validate_app_config(config)

        self.assertFalse(any("pattern_files" in error for error in errors))

    def test_controller_blocks_invalid_config_before_emitting_worker_start(self):
        from sim_control.controller import SimAcquisitionController
        from sim_control.models import BackendConfig, SimTaskConfig

        controller = SimAcquisitionController(BackendConfig(simulation_mode=True))
        emitted_payloads = []
        controller.signal_start_worker.connect(lambda payload: emitted_payloads.append(payload))
        controller.pattern_result.handles = list(range(9))

        try:
            task = SimTaskConfig()
            task.camera.exposure_us = 0
            with self.assertRaisesRegex(ValueError, "exposure_us"):
                controller.start_single_acquisition(task)
        finally:
            controller.shutdown()

        self.assertEqual(emitted_payloads, [])


if __name__ == "__main__":
    unittest.main()

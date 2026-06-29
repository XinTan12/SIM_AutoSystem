import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class ZScanConfigMigrationTests(unittest.TestCase):
    def test_v3_config_migrates_to_current_schema_with_z_scan_defaults(self):
        from sim_control.config_store import app_config_from_dict, app_config_to_dict

        config = app_config_from_dict({"config_version": 3})
        payload = app_config_to_dict(config)

        self.assertEqual(config.config_version, 13)
        self.assertTrue(config.z_scan.enabled)
        self.assertIsNone(config.z_scan.start_um)
        self.assertEqual(config.z_scan.direction, "positive_z")
        self.assertEqual(config.z_scan.step_um, 0.3)
        self.assertEqual(config.z_scan.num_steps, 10)
        self.assertEqual(config.z_scan.exposure_preset_ms, 8)
        self.assertEqual(config.z_scan.focus_metric, "sml")
        self.assertFalse(hasattr(config.z_scan, "return_to_start_on_cancel"))
        self.assertEqual(payload["config_version"], 13)
        self.assertIn("z_scan", payload)
        self.assertNotIn("return_to_start_on_cancel", payload["z_scan"])
        self.assertIn("reconstruction", payload)
        self.assertIn("output_path", payload["reconstruction"])
        self.assertEqual(payload["reconstruction"]["output_path"], "data/reconstruction")

    def test_v7_return_to_start_on_cancel_is_dropped_in_v8(self):
        from sim_control.config_store import app_config_from_dict, app_config_to_dict

        config = app_config_from_dict(
            {
                "config_version": 7,
                "z_scan": {
                    "enabled": True,
                    "start_um": None,
                    "direction": "positive_z",
                    "step_um": 0.5,
                    "num_steps": 3,
                    "exposure_preset_ms": 8,
                    "focus_metric": "sml",
                    "return_to_start_on_cancel": True,
                },
            }
        )
        payload = app_config_to_dict(config)

        self.assertEqual(config.config_version, 13)
        self.assertFalse(hasattr(config.z_scan, "return_to_start_on_cancel"))
        self.assertNotIn("return_to_start_on_cancel", payload["z_scan"])

    def test_z_scan_validation_reports_invalid_fields(self):
        from sim_control.config_store import validate_app_config
        from sim_control.models import AppConfig, ZScanConfig

        config = AppConfig(
            z_scan=ZScanConfig(
                direction="toward_sample",
                step_um=0,
                num_steps=0,
                exposure_preset_ms=10,
                focus_metric="variance",
            )
        )

        errors = validate_app_config(config)

        self.assertTrue(any("z_scan.direction" in error for error in errors))
        self.assertTrue(any("z_scan.step_um" in error for error in errors))
        self.assertTrue(any("z_scan.num_steps" in error for error in errors))
        self.assertTrue(any("z_scan.exposure_preset_ms" in error for error in errors))
        self.assertTrue(any("z_scan.focus_metric" in error for error in errors))


if __name__ == "__main__":
    unittest.main()

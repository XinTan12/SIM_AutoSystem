import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
CONTROL_WANGBO_ROOT = PROJECT_ROOT / "control_wangbo"
if str(CONTROL_WANGBO_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_WANGBO_ROOT))


class LegacySimConfigMigrationTests(unittest.TestCase):
    def test_backend_config_only_keeps_real_hardware_settings(self):
        from sim_control.models import BackendConfig

        backend = BackendConfig()

        self.assertEqual(backend.fusion_bt_sdk_path, "")
        self.assertEqual(backend.slm_sdk_path, "")
        self.assertEqual(set(vars(backend).keys()), {"fusion_bt_sdk_path", "slm_sdk_path"})

    def test_legacy_backend_payload_ignores_removed_deprecated_flags(self):
        from sim_control.models import AppConfig, BackendConfig, CameraConfig

        from control_wangbo.main import merge_legacy_sim_control_payload

        base_config = AppConfig(
            backend=BackendConfig(
                fusion_bt_sdk_path="E:/sdk/dcam",
                slm_sdk_path="E:/sdk/r11",
            ),
            camera=CameraConfig(
                device_index=1,
                device_label="1: C15440-20UP [S/N: 501680]",
                roi_width=512,
                roi_height=256,
                exposure_us=1200,
            ),
        )
        legacy_payload = {
            "backend": {
                "deprecated_camera_mode": True,
                "deprecated_slm_mode": True,
                "deprecated_daq_mode": True,
                "fusion_bt_sdk_path": "",
                "slm_sdk_path": "",
            },
            "camera": {
                "device_index": 0,
                "device_label": "legacy camera placeholder",
                "roi_width": 608,
                "roi_height": 304,
                "exposure_us": 25,
            },
        }

        merged = merge_legacy_sim_control_payload(base_config, legacy_payload)

        self.assertEqual(merged.backend.fusion_bt_sdk_path, "E:/sdk/dcam")
        self.assertEqual(merged.backend.slm_sdk_path, "E:/sdk/r11")
        self.assertEqual(set(vars(merged.backend).keys()), {"fusion_bt_sdk_path", "slm_sdk_path"})
        self.assertEqual(merged.camera.roi_width, 608)
        self.assertEqual(merged.camera.roi_height, 304)
        self.assertEqual(merged.camera.exposure_us, 25)

    def test_app_config_loader_ignores_removed_deprecated_flags(self):
        from sim_control.config_store import app_config_from_dict

        config = app_config_from_dict(
            {
                "backend": {
                    "deprecated_camera_mode": True,
                    "deprecated_slm_mode": True,
                    "deprecated_daq_mode": True,
                    "fusion_bt_sdk_path": "E:/sdk/dcam",
                    "slm_sdk_path": "E:/sdk/r11",
                }
            }
        )

        self.assertEqual(config.backend.fusion_bt_sdk_path, "E:/sdk/dcam")
        self.assertEqual(config.backend.slm_sdk_path, "E:/sdk/r11")
        self.assertEqual(set(vars(config.backend).keys()), {"fusion_bt_sdk_path", "slm_sdk_path"})

    def test_app_config_loader_defaults_camera_bit_depth_to_16_for_legacy_payloads(self):
        from sim_control.config_store import app_config_from_dict

        config = app_config_from_dict(
            {
                "camera": {
                    "device_index": 0,
                    "device_label": "legacy camera placeholder",
                }
            }
        )

        self.assertEqual(config.camera.bit_depth, 16)

    def test_app_config_loader_migrates_legacy_640_laser_fields_to_647(self):
        from sim_control.config_store import app_config_from_dict, app_config_to_dict

        config = app_config_from_dict(
            {
                "daq": {
                    "device_name": "Dev2",
                    "slm_enable_line": "Dev2/port0/line0",
                    "slm_trigger_line": "Dev2/port0/line1",
                    "slm_finish_line": "Dev2/port0/line2",
                    "camera_trigger_line": "Dev2/port0/line5",
                    "laser_405_line": "Dev2/port0/line8",
                    "laser_488_line": "Dev2/port0/line6",
                    "laser_561_line": "Dev2/port0/line7",
                    "laser_640_line": "Dev2/port0/line9",
                },
                "selected_laser_nm": 640,
            }
        )

        self.assertEqual(config.selected_laser_nm, 647)
        self.assertEqual(getattr(config.daq, "laser_647_line", None), "Dev2/port0/line9")
        self.assertFalse(hasattr(config.daq, "laser_640_line"))

        payload = app_config_to_dict(config)

        self.assertEqual(payload["selected_laser_nm"], 647)
        self.assertEqual(payload["daq"].get("laser_647_line"), "Dev2/port0/line9")
        self.assertNotIn("laser_640_line", payload["daq"])

    def test_real_device_detection_replaces_stale_camera_identity(self):
        from sim_control.models import AppConfig, CameraConfig

        from control_wangbo.main import apply_real_hardware_preference

        config = AppConfig(
            camera=CameraConfig(
                device_index=0,
                device_label="legacy camera placeholder",
            ),
        )
        camera_devices = [
            {
                "index": 0,
                "display": "0: C15440-20UP [S/N: 501679]",
                "camera_id": "S/N: 501679",
            }
        ]
        slm_devices = [
            {
                "path": r"\\?\usb#vid_19ec&pid_0503#0175000845#{54ed7ac9-cc23-4165-be32-79016bafb950}",
                "display": "0175000845",
            }
        ]
        daq_devices = ["Dev2"]

        updated = apply_real_hardware_preference(
            config,
            camera_devices=camera_devices,
            slm_devices=slm_devices,
            daq_devices=daq_devices,
        )

        self.assertEqual(updated.camera.device_index, 0)
        self.assertEqual(updated.camera.device_label, "0: C15440-20UP [S/N: 501679]")

    def test_hardware_preference_preserves_camera_identity_when_no_devices_detected(self):
        from sim_control.models import AppConfig, CameraConfig

        from control_wangbo.main import apply_real_hardware_preference

        config = AppConfig(
            camera=CameraConfig(
                device_index=0,
                device_label="legacy camera placeholder",
            ),
        )

        updated = apply_real_hardware_preference(
            config,
            camera_devices=[],
            slm_devices=[],
            daq_devices=[],
        )

        self.assertEqual(updated.camera.device_label, "legacy camera placeholder")


if __name__ == "__main__":
    unittest.main()

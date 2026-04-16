import sys
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class CameraConfigTests(unittest.TestCase):
    def test_app_config_round_trip_preserves_selected_sim_camera_identity(self):
        from sim_control.config_store import app_config_from_dict, app_config_to_dict
        from sim_control.models import AppConfig, CameraConfig

        config = AppConfig(
            camera=CameraConfig(
                device_index=2,
                device_label="2: ORCA-Fusion BT [CAM-002]",
                roi_x=128,
                roi_y=64,
                roi_width=512,
                roi_height=256,
                exposure_us=15000,
            )
        )

        payload = app_config_to_dict(config)
        restored = app_config_from_dict(payload)

        self.assertEqual(restored.camera.device_index, 2)
        self.assertEqual(restored.camera.device_label, "2: ORCA-Fusion BT [CAM-002]")
        self.assertEqual(restored.camera.roi_x, 128)
        self.assertEqual(restored.camera.roi_y, 64)


class FusionBtCameraAdapterTests(unittest.TestCase):
    def test_list_devices_returns_structured_simulated_device(self):
        from sim_control.adapters import FusionBtCameraAdapter

        adapter = FusionBtCameraAdapter(simulate=True)

        devices = adapter.list_devices()

        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["index"], 0)
        self.assertIn("display", devices[0])
        self.assertIn("Simulated", devices[0]["display"])

    def test_list_devices_formats_real_dcam_devices(self):
        from sim_control.adapters import FusionBtCameraAdapter

        class FakeDcamapi:
            @staticmethod
            def init():
                return True

            @staticmethod
            def get_devicecount():
                return 2

            @staticmethod
            def lasterr():
                return type("Err", (), {"name": "none"})()

        class FakeDcamModule:
            Dcamapi = FakeDcamapi

            def __init__(self):
                self._descriptors = {
                    0: {
                        "MODEL": "ORCA-Fusion BT",
                        "CAMERAID": "CAM-001",
                        "DRIVERVERSION": "1.0",
                    },
                    1: {
                        "MODEL": "ORCA-Fusion BT",
                        "CAMERAID": "CAM-002",
                        "DRIVERVERSION": "1.0",
                    },
                }

            def Dcam(self, index):
                descriptor = self._descriptors[index]

                class Camera:
                    def dev_open(self_inner):
                        return True

                    def dev_close(self_inner):
                        return True

                    def dev_getstring(self_inner, key):
                        return descriptor[key]

                    def lasterr(self_inner):
                        return type("Err", (), {"name": "none"})()

                return Camera()

        adapter = FusionBtCameraAdapter(simulate=False)
        adapter._dcamapi4 = type(
            "FakeDcamapi4",
            (),
            {"DCAM_IDSTR": type("IdStr", (), {"MODEL": "MODEL", "CAMERAID": "CAMERAID", "DRIVERVERSION": "DRIVERVERSION"})},
        )()
        adapter._dcam = FakeDcamModule()
        adapter._module_dir = Path(".")

        with mock.patch.object(adapter, "_load_dcam_modules", autospec=True) as mocked_loader:
            devices = adapter.list_devices()

        mocked_loader.assert_called_once()
        self.assertEqual([device["index"] for device in devices], [0, 1])
        self.assertEqual(devices[1]["camera_id"], "CAM-002")
        self.assertEqual(devices[1]["display"], "1: ORCA-Fusion BT [CAM-002]")


if __name__ == "__main__":
    unittest.main()

import sys
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class CameraConfigTests(unittest.TestCase):
    def test_app_config_round_trip_preserves_selected_camera_identity(self):
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

    def test_app_config_round_trip_preserves_camera_bit_depth(self):
        from sim_control.config_store import app_config_from_dict, app_config_to_dict
        from sim_control.models import AppConfig, CameraConfig

        config = AppConfig(
            camera=CameraConfig(
                device_index=1,
                device_label="1: ORCA-Flash4.0 V3 [CAM-003]",
                bit_depth=12,
            )
        )

        payload = app_config_to_dict(config)
        restored = app_config_from_dict(payload)

        self.assertEqual(restored.camera.bit_depth, 12)


class FusionBtCameraAdapterTests(unittest.TestCase):
    def test_adapters_reject_removed_legacy_kwargs(self):
        from sim_control.adapters import FusionBtCameraAdapter, KopinSlmAdapter, NIDaqAdapter

        with self.assertRaises(TypeError):
            FusionBtCameraAdapter(deprecated_flag=True)
        with self.assertRaises(TypeError):
            KopinSlmAdapter(deprecated_flag=True)
        with self.assertRaises(TypeError):
            NIDaqAdapter(deprecated_flag=True)

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

        adapter = FusionBtCameraAdapter()
        adapter._dcamapi4 = type(
            "FakeDcamapi4",
            (),
            {"DCAM_IDSTR": type("IdStr", (), {"MODEL": "MODEL", "CAMERAID": "CAMERAID", "DRIVERVERSION": "DRIVERVERSION"})},
        )()
        adapter._dcam = FakeDcamModule()
        adapter._module_dir = Path(".")

        with mock.patch.object(adapter, "_load_dcam_modules", autospec=True) as mocked_loader:
            devices = adapter.list_devices()

        self.assertEqual([device["index"] for device in devices], [0, 1])
        self.assertEqual(devices[1]["camera_id"], "CAM-002")
        self.assertEqual(devices[1]["display"], "1: ORCA-Fusion BT [CAM-002]")

    def test_apply_config_closes_camera_when_property_configuration_fails(self):
        from sim_control.adapters import FusionBtCameraAdapter
        from sim_control.models import CameraConfig

        adapter = FusionBtCameraAdapter()
        adapter._initialized = True
        adapter._dcamapi4 = type(
            "FakeDcamapi4",
            (),
            {
                "DCAMPROP": type(
                    "Props",
                    (),
                    {
                        "TRIGGERSOURCE": type("TriggerSource", (), {"EXTERNAL": 1}),
                        "TRIGGERACTIVE": type("TriggerActive", (), {"LEVEL": 2}),
                    },
                )
            },
        )()
        adapter._ensure_camera_open = mock.Mock()
        adapter._configure_camera = mock.Mock(side_effect=RuntimeError("INVALIDPARAM"))
        adapter._close_camera = mock.Mock()

        with self.assertRaises(RuntimeError):
            adapter.apply_config(CameraConfig())

        adapter._close_camera.assert_called_once()

    def test_apply_config_returns_timing_and_bit_depth_details(self):
        from sim_control.adapters import FusionBtCameraAdapter
        from sim_control.models import CameraConfig

        class FakeCamera:
            def __init__(self):
                self.set_calls = []
                self.values = {
                    501: 0.031649,
                    502: 0.0155,
                    503: 0.0,
                    504: 16,
                }

            def prop_setgetvalue(self, prop_id, value):
                self.set_calls.append((prop_id, value))
                if prop_id == 401:
                    return 3
                if prop_id == 504:
                    self.values[prop_id] = int(value)
                    return float(int(value))
                if prop_id == 505:
                    return 2
                return float(value)

            def prop_getvalue(self, prop_id):
                return self.values[prop_id]

            def prop_queryvalue(self, prop_id, value):
                if prop_id == 401:
                    return 3
                if prop_id == 504 and value in {8, 12, 16}:
                    return value
                return False

            def prop_getvaluetext(self, prop_id, value):
                if prop_id == 401 and int(value) == 3:
                    return "Fast scan"
                if prop_id == 504:
                    return f"{int(value)} bit"
                return str(value)

            def lasterr(self):
                return type("Err", (), {"name": "none"})()

        dcamapi4 = type(
            "FakeDcamapi4",
            (),
            {
                "DCAM_IDPROP": type(
                    "Props",
                    (),
                    {
                        "TRIGGERSOURCE": 101,
                        "TRIGGERACTIVE": 102,
                        "TRIGGER_MODE": 103,
                        "TRIGGERPOLARITY": 104,
                        "READOUTSPEED": 401,
                        "BITSPERCHANNEL": 504,
                        "IMAGE_PIXELTYPE": 505,
                        "SUBARRAYMODE": 201,
                        "SUBARRAYHPOS": 202,
                        "SUBARRAYVPOS": 203,
                        "SUBARRAYHSIZE": 204,
                        "SUBARRAYVSIZE": 205,
                        "EXPOSURETIME": 301,
                        "TIMING_READOUTTIME": 501,
                        "TIMING_CYCLICTRIGGERPERIOD": 502,
                        "TIMING_MINTRIGGERBLANKING": 503,
                    },
                ),
                "DCAMPROP": type(
                    "DcamProp",
                    (),
                    {
                        "TRIGGERSOURCE": type("TriggerSource", (), {"EXTERNAL": 1, "INTERNAL": 2}),
                        "TRIGGERACTIVE": type("TriggerActive", (), {"LEVEL": 3, "EDGE": 4}),
                        "TRIGGER_MODE": type("TriggerMode", (), {"NORMAL": 5}),
                        "TRIGGERPOLARITY": type("TriggerPolarity", (), {"POSITIVE": 6}),
                        "MODE": type("Mode", (), {"OFF": 0, "ON": 1}),
                        "READOUTSPEED": type("ReadoutSpeed", (), {"FASTEST": 0x7FFFFFFF}),
                        "BITSPERCHANNEL": type(
                            "BitsPerChannel",
                            (),
                            {"_8": 8, "_10": 10, "_12": 12, "_14": 14, "_16": 16},
                        ),
                    },
                ),
                "DCAM_PIXELTYPE": type("PixelType", (), {"MONO8": 1, "MONO16": 2}),
            },
        )()

        adapter = FusionBtCameraAdapter()
        adapter._initialized = True
        adapter._dcamapi4 = dcamapi4
        adapter._dcam_camera = FakeCamera()
        adapter._connection_info = {"model": "ORCA-Fusion BT", "camera_id": "C15440-20UP"}
        adapter._ensure_camera_open = mock.Mock(return_value=dict(adapter._connection_info))

        result = adapter.apply_config(
            CameraConfig(
                device_index=0,
                device_label="0: ORCA-Fusion BT [CAM-001]",
                roi_x=4,
                roi_y=8,
                roi_width=1152,
                roi_height=1152,
                exposure_us=10_000,
                bit_depth=12,
            )
        )

        self.assertEqual(result["applied_readout_speed_text"], "Fast scan")
        self.assertEqual(result["applied_bit_depth"], 12)
        self.assertEqual(result["supported_bit_depths"], [8, 12, 16])
        self.assertEqual(result["recommended_inter_frame_gap_us"], 32649)
        self.assertEqual(result["timing_readout_time_s"], 0.031649)
        self.assertEqual(result["timing_cyclic_trigger_period_s"], 0.0155)
        self.assertEqual(result["timing_min_trigger_blanking_s"], 0.0)
        self.assertIn((504, 12), adapter._dcam_camera.set_calls)
        self.assertIn((505, 2), adapter._dcam_camera.set_calls)

    def test_apply_config_omits_recommended_gap_when_readout_time_is_unavailable(self):
        from sim_control.adapters import FusionBtCameraAdapter
        from sim_control.models import CameraConfig

        class FakeCamera:
            def __init__(self):
                self.values = {
                    502: 0.0120,
                    503: 0.0003,
                    504: 16,
                }

            def prop_setgetvalue(self, prop_id, value):
                if prop_id == 401:
                    return 3
                if prop_id == 504:
                    self.values[prop_id] = int(value)
                    return float(int(value))
                if prop_id == 505:
                    return 2
                return float(value)

            def prop_getvalue(self, prop_id):
                if prop_id == 501:
                    raise RuntimeError("TIMING_READOUTTIME unavailable")
                return self.values[prop_id]

            def prop_queryvalue(self, prop_id, value):
                if prop_id == 401:
                    return 3
                if prop_id == 504 and value in {12, 16}:
                    return value
                return False

            def prop_getvaluetext(self, prop_id, value):
                return "Fast scan" if prop_id == 401 and int(value) == 3 else str(value)

            def lasterr(self):
                return type("Err", (), {"name": "none"})()

        dcamapi4 = type(
            "FakeDcamapi4",
            (),
            {
                "DCAM_IDPROP": type(
                    "Props",
                    (),
                    {
                        "TRIGGERSOURCE": 101,
                        "TRIGGERACTIVE": 102,
                        "TRIGGER_MODE": 103,
                        "TRIGGERPOLARITY": 104,
                        "READOUTSPEED": 401,
                        "BITSPERCHANNEL": 504,
                        "IMAGE_PIXELTYPE": 505,
                        "SUBARRAYMODE": 201,
                        "SUBARRAYHPOS": 202,
                        "SUBARRAYVPOS": 203,
                        "SUBARRAYHSIZE": 204,
                        "SUBARRAYVSIZE": 205,
                        "EXPOSURETIME": 301,
                        "TIMING_READOUTTIME": 501,
                        "TIMING_CYCLICTRIGGERPERIOD": 502,
                        "TIMING_MINTRIGGERBLANKING": 503,
                    },
                ),
                "DCAMPROP": type(
                    "DcamProp",
                    (),
                    {
                        "TRIGGERSOURCE": type("TriggerSource", (), {"EXTERNAL": 1}),
                        "TRIGGERACTIVE": type("TriggerActive", (), {"LEVEL": 3}),
                        "TRIGGER_MODE": type("TriggerMode", (), {"NORMAL": 5}),
                        "TRIGGERPOLARITY": type("TriggerPolarity", (), {"POSITIVE": 6}),
                        "MODE": type("Mode", (), {"OFF": 0, "ON": 1}),
                        "READOUTSPEED": type("ReadoutSpeed", (), {"FASTEST": 0x7FFFFFFF}),
                        "BITSPERCHANNEL": type("BitsPerChannel", (), {"_12": 12, "_16": 16}),
                    },
                ),
                "DCAM_PIXELTYPE": type("PixelType", (), {"MONO8": 1, "MONO16": 2}),
            },
        )()

        adapter = FusionBtCameraAdapter()
        adapter._initialized = True
        adapter._dcamapi4 = dcamapi4
        adapter._dcam_camera = FakeCamera()
        adapter._connection_info = {"model": "ORCA-Fusion BT", "camera_id": "C15440-20UP"}
        adapter._ensure_camera_open = mock.Mock(return_value=dict(adapter._connection_info))

        result = adapter.apply_config(CameraConfig(bit_depth=16))

        self.assertIsNone(result["timing_readout_time_s"])
        self.assertNotIn("recommended_inter_frame_gap_us", result)

    def test_flash_camera_uses_standard_scan_and_falls_back_to_16_bit_combo(self):
        from sim_control.adapters import FusionBtCameraAdapter
        from sim_control.models import CameraConfig

        class FakeCamera:
            def __init__(self):
                self.values = {
                    501: 0.0056,
                    502: 0.0120,
                    503: 0.0003,
                    504: 16,
                }
                self.set_calls = []

            def prop_setgetvalue(self, prop_id, value):
                self.set_calls.append((prop_id, value))
                if prop_id == 401:
                    return 2
                if prop_id == 504:
                    self.values[prop_id] = int(value)
                    return float(int(value))
                if prop_id == 505:
                    return 2
                return float(value)

            def prop_getvalue(self, prop_id):
                return self.values[prop_id]

            def prop_queryvalue(self, prop_id, value):
                if prop_id == 401:
                    return 2
                if prop_id == 504 and value in {12, 16}:
                    return value
                return False

            def prop_getvaluetext(self, prop_id, value):
                if prop_id == 401 and int(value) == 2:
                    return "Standard scan"
                return str(value)

            def lasterr(self):
                return type("Err", (), {"name": "none"})()

        dcamapi4 = type(
            "FakeDcamapi4",
            (),
            {
                "DCAM_IDPROP": type(
                    "Props",
                    (),
                    {
                        "TRIGGERSOURCE": 101,
                        "TRIGGERACTIVE": 102,
                        "TRIGGER_MODE": 103,
                        "TRIGGERPOLARITY": 104,
                        "READOUTSPEED": 401,
                        "BITSPERCHANNEL": 504,
                        "IMAGE_PIXELTYPE": 505,
                        "SUBARRAYMODE": 201,
                        "SUBARRAYHPOS": 202,
                        "SUBARRAYVPOS": 203,
                        "SUBARRAYHSIZE": 204,
                        "SUBARRAYVSIZE": 205,
                        "EXPOSURETIME": 301,
                        "TIMING_READOUTTIME": 501,
                        "TIMING_CYCLICTRIGGERPERIOD": 502,
                        "TIMING_MINTRIGGERBLANKING": 503,
                    },
                ),
                "DCAMPROP": type(
                    "DcamProp",
                    (),
                    {
                        "TRIGGERSOURCE": type("TriggerSource", (), {"EXTERNAL": 1, "INTERNAL": 2}),
                        "TRIGGERACTIVE": type("TriggerActive", (), {"LEVEL": 3, "EDGE": 4}),
                        "TRIGGER_MODE": type("TriggerMode", (), {"NORMAL": 5}),
                        "TRIGGERPOLARITY": type("TriggerPolarity", (), {"POSITIVE": 6}),
                        "MODE": type("Mode", (), {"OFF": 0, "ON": 1}),
                        "READOUTSPEED": type("ReadoutSpeed", (), {"FASTEST": 0x7FFFFFFF}),
                        "BITSPERCHANNEL": type(
                            "BitsPerChannel",
                            (),
                            {"_8": 8, "_10": 10, "_12": 12, "_14": 14, "_16": 16},
                        ),
                    },
                ),
                "DCAM_PIXELTYPE": type("PixelType", (), {"MONO8": 1, "MONO16": 2}),
            },
        )()

        adapter = FusionBtCameraAdapter()
        adapter._initialized = True
        adapter._dcamapi4 = dcamapi4
        adapter._dcam_camera = FakeCamera()
        adapter._connection_info = {"model": "ORCA-Flash4.0 V3", "camera_id": "C13440-20CU"}
        adapter._ensure_camera_open = mock.Mock(return_value=dict(adapter._connection_info))

        result = adapter.apply_config(CameraConfig(bit_depth=10))

        self.assertEqual(result["applied_readout_speed_text"], "Standard scan")
        self.assertEqual(result["supported_bit_depths"], [12, 16])
        self.assertEqual(result["applied_bit_depth"], 16)
        self.assertEqual(result["recommended_inter_frame_gap_us"], 6900)


class SimAcquisitionControllerTests(unittest.TestCase):
    def test_apply_camera_config_emits_runtime_timing_payload(self):
        from sim_control.controller import SimAcquisitionController
        from sim_control.models import CameraConfig

        controller = SimAcquisitionController()
        statuses = []
        controller.signal_status_changed.connect(lambda status, payload: statuses.append((status, payload)))
        controller.camera_adapter = mock.Mock()
        controller.camera_adapter.apply_config.return_value = {
            "timing_readout_time_s": 0.00561,
            "recommended_inter_frame_gap_us": 6500,
            "supported_bit_depths": [12, 16],
            "applied_bit_depth": 12,
        }

        try:
            result = controller.apply_camera_config(CameraConfig(bit_depth=12))
        finally:
            controller._thread.quit()
            controller._thread.wait(2000)

        self.assertEqual(result["recommended_inter_frame_gap_us"], 6500)
        self.assertEqual(statuses[-1][0], "camera_config_applied")
        self.assertEqual(statuses[-1][1]["applied_bit_depth"], 12)
        self.assertEqual(statuses[-1][1]["supported_bit_depths"], [12, 16])

    def test_start_single_acquisition_overrides_gap_from_last_camera_timing(self):
        from sim_control.controller import SimAcquisitionController
        from sim_control.models import SimTaskConfig

        controller = SimAcquisitionController()
        emitted_payloads = []
        controller.signal_start_worker.connect(lambda payload: emitted_payloads.append(payload))
        controller.pattern_result.handles = [0]
        controller._latest_camera_timing = {"recommended_inter_frame_gap_us": 6500}

        try:
            task = SimTaskConfig()
            task.timing.inter_frame_gap_us = 15000
            controller.start_single_acquisition(task)
        finally:
            controller._thread.quit()
            controller._thread.wait(2000)

        self.assertEqual(task.timing.inter_frame_gap_us, 6500)
        self.assertEqual(emitted_payloads[-1]["task"].timing.inter_frame_gap_us, 6500)

    def test_start_single_acquisition_accepts_running_order_mode(self):
        from sim_control.controller import SimAcquisitionController
        from sim_control.models import PatternPreparationResult, SimTaskConfig

        controller = SimAcquisitionController()
        emitted_payloads = []
        controller.signal_start_worker.connect(lambda payload: emitted_payloads.append(payload))
        controller.pattern_result = PatternPreparationResult(
            pattern_files=["488_3.5_2d_1ms"] * 9,
            handles=[-1],
            metadata={"mode": "running_order", "running_order_name": "488_3.5_2d_1ms"},
        )

        try:
            task = SimTaskConfig(running_order_name="488_3.5_2d_1ms")
            controller.start_single_acquisition(task)
        finally:
            controller._thread.quit()
            controller._thread.wait(2000)

        self.assertEqual(emitted_payloads[-1]["pattern_result"].handles, [-1])


if __name__ == "__main__":
    unittest.main()

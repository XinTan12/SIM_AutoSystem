import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _start_single_acquisition_for_payload_test(controller, task, **kwargs):
    emitted_payloads = []
    try:
        controller.signal_start_worker.disconnect()
    except (TypeError, RuntimeError):
        pass
    controller.signal_start_worker.connect(lambda payload: emitted_payloads.append(payload))
    task_id = controller.start_single_acquisition(task, **kwargs)
    return task_id, emitted_payloads


def _start_prepare_experiment_for_payload_test(controller, task, **kwargs):
    emitted_payloads = []
    try:
        controller.signal_start_worker.disconnect()
    except (TypeError, RuntimeError):
        pass
    controller.signal_start_worker.connect(lambda payload: emitted_payloads.append(payload))
    task_id = controller.start_prepare_experiment(task, **kwargs)
    return task_id, emitted_payloads


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


def _fake_dcamapi4_for_roi_tests():
    return type(
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
                    "SUBARRAYMODE": 201,
                    "SUBARRAYHPOS": 202,
                    "SUBARRAYVPOS": 203,
                    "SUBARRAYHSIZE": 204,
                    "SUBARRAYVSIZE": 205,
                    "EXPOSURETIME": 301,
                    "BITSPERCHANNEL": 504,
                    "IMAGE_PIXELTYPE": 505,
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

    def test_read_frame_sequence_returns_frames_already_buffered_before_waiting(self):
        from sim_control.adapters import FusionBtCameraAdapter
        from sim_control.models import CameraConfig

        class FakeCamera:
            def __init__(self):
                self.wait_calls = 0
                self.frames = [np.full((2, 2), index, dtype=np.uint16) for index in range(3)]

            def cap_transferinfo(self):
                return SimpleNamespace(nFrameCount=3)

            def wait_capevent_frameready(self, timeout_ms):
                self.wait_calls += 1
                return False

            def buf_getframedata(self, index):
                return self.frames[index]

            def lasterr(self):
                return SimpleNamespace(name="TIMEOUT")

        adapter = FusionBtCameraAdapter()
        adapter._armed = True
        adapter._camera_config = CameraConfig(roi_width=2, roi_height=2, exposure_us=500_000)
        adapter._dcam_camera = FakeCamera()

        stack, timestamps = adapter.read_frame_sequence(
            frame_count=3,
            pattern_files=[""] * 3,
            laser_wavelength_nm=488,
        )

        self.assertEqual(adapter._dcam_camera.wait_calls, 0)
        self.assertEqual(stack.shape, (3, 2, 2))
        self.assertEqual([int(frame[0, 0]) for frame in stack], [0, 1, 2])
        self.assertEqual(len(timestamps), 3)

    def test_read_frame_sequence_timeout_reports_partial_capture_count(self):
        from sim_control.adapters import FusionBtCameraAdapter, HardwareError
        from sim_control.models import CameraConfig

        class FakeCamera:
            def cap_transferinfo(self):
                return SimpleNamespace(nFrameCount=3)

            def wait_capevent_frameready(self, timeout_ms):
                return False

            def lasterr(self):
                return SimpleNamespace(name="TIMEOUT")

        adapter = FusionBtCameraAdapter()
        adapter._armed = True
        adapter._camera_config = CameraConfig(roi_width=2, roi_height=2, exposure_us=500_000)
        adapter._dcam_camera = FakeCamera()

        with self.assertRaisesRegex(HardwareError, r"TIMEOUT.*captured 3/9"):
            adapter.read_frame_sequence(
                frame_count=9,
                pattern_files=[""] * 9,
                laser_wavelength_nm=488,
            )

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

    def test_flash_camera_clips_fusion_roi_to_dcam_subarray_limits(self):
        from sim_control.adapters import FusionBtCameraAdapter
        from sim_control.models import CameraConfig

        class FakeCamera:
            def __init__(self):
                self.set_calls = []
                self.values = {
                    201: 0,
                    202: 0,
                    203: 0,
                    204: 2048,
                    205: 2048,
                    401: 2,
                    501: 0.0056,
                    502: 0.0120,
                    503: 0.0003,
                    504: 16,
                    505: 2,
                }

            def prop_setgetvalue(self, prop_id, value):
                self.set_calls.append((prop_id, value))
                if prop_id in {204, 205} and int(value) > 2048:
                    return False
                self.values[prop_id] = int(value) if prop_id != 301 else float(value)
                return float(self.values[prop_id])

            def prop_getvalue(self, prop_id):
                return self.values[prop_id]

            def prop_queryvalue(self, prop_id, value):
                if prop_id == 401:
                    return 2
                if prop_id == 504 and value in {12, 16}:
                    return value
                return False

            def prop_getattr(self, prop_id):
                if prop_id in {202, 203}:
                    return SimpleNamespace(valuemin=0, valuemax=2044, valuestep=4)
                if prop_id in {204, 205}:
                    return SimpleNamespace(valuemin=4, valuemax=2048, valuestep=4)
                return False

            def prop_getvaluetext(self, prop_id, value):
                if prop_id == 401 and int(value) == 2:
                    return "Standard scan"
                return str(value)

            def lasterr(self):
                return type("Err", (), {"name": "INVALIDPARAM"})()

        dcamapi4 = _fake_dcamapi4_for_roi_tests()
        adapter = FusionBtCameraAdapter()
        adapter._initialized = True
        adapter._dcamapi4 = dcamapi4
        adapter._dcam_camera = FakeCamera()
        adapter._connection_info = {"model": "ORCA-Flash4.0 V3", "camera_id": "C13440-20C"}
        adapter._ensure_camera_open = mock.Mock(return_value=dict(adapter._connection_info))

        config = CameraConfig(roi_width=2304, roi_height=2304, bit_depth=16)
        result = adapter.apply_config(config)

        self.assertEqual(config.roi_width, 2048)
        self.assertEqual(config.roi_height, 2048)
        self.assertEqual(result["applied_roi"], {"x": 0, "y": 0, "width": 2048, "height": 2048})
        self.assertEqual(result["sensor_width"], 2048)
        self.assertEqual(result["sensor_height"], 2048)
        self.assertEqual(result["roi_step_px"], 4)
        self.assertEqual(result["roi_size_presets"], [(2048, 2048), (1024, 1024), (512, 512)])
        self.assertIn((204, 2048), adapter._dcam_camera.set_calls)
        self.assertIn((205, 2048), adapter._dcam_camera.set_calls)
        self.assertNotIn((204, 2304), adapter._dcam_camera.set_calls)

    def test_flash_camera_clamps_nonzero_roi_origin_to_sensor_bounds_and_step(self):
        from sim_control.adapters import FusionBtCameraAdapter
        from sim_control.models import CameraConfig

        class FakeCamera:
            def __init__(self):
                self.set_calls = []
                self.values = {
                    201: 0,
                    202: 0,
                    203: 0,
                    204: 2048,
                    205: 2048,
                    401: 2,
                    501: 0.0056,
                    502: 0.0120,
                    503: 0.0003,
                    504: 16,
                    505: 2,
                }

            def prop_setgetvalue(self, prop_id, value):
                self.set_calls.append((prop_id, value))
                self.values[prop_id] = int(value) if prop_id != 301 else float(value)
                return float(self.values[prop_id])

            def prop_getvalue(self, prop_id):
                return self.values[prop_id]

            def prop_queryvalue(self, prop_id, value):
                if prop_id == 401:
                    return 2
                if prop_id == 504 and value in {12, 16}:
                    return value
                return False

            def prop_getattr(self, prop_id):
                if prop_id in {202, 203}:
                    return SimpleNamespace(valuemin=0, valuemax=2044, valuestep=4)
                if prop_id in {204, 205}:
                    return SimpleNamespace(valuemin=4, valuemax=2048, valuestep=4)
                return False

            def prop_getvaluetext(self, prop_id, value):
                return str(value)

            def lasterr(self):
                return type("Err", (), {"name": "none"})()

        dcamapi4 = _fake_dcamapi4_for_roi_tests()
        adapter = FusionBtCameraAdapter()
        adapter._initialized = True
        adapter._dcamapi4 = dcamapi4
        adapter._dcam_camera = FakeCamera()
        adapter._connection_info = {"model": "ORCA-Flash4.0 V3", "camera_id": "C13440-20C"}
        adapter._ensure_camera_open = mock.Mock(return_value=dict(adapter._connection_info))

        config = CameraConfig(roi_x=1501, roi_y=1477, roi_width=1152, roi_height=1152, bit_depth=16)
        result = adapter.apply_config(config)

        self.assertEqual(result["applied_roi"], {"x": 896, "y": 896, "width": 1152, "height": 1152})
        self.assertEqual(config.roi_x, 896)
        self.assertEqual(config.roi_y, 896)
        self.assertIn((202, 896), adapter._dcam_camera.set_calls)
        self.assertIn((203, 896), adapter._dcam_camera.set_calls)

    def test_fusion_camera_preserves_2304_full_frame_roi_when_supported(self):
        from sim_control.adapters import FusionBtCameraAdapter
        from sim_control.models import CameraConfig

        class FakeCamera:
            def __init__(self):
                self.set_calls = []
                self.values = {
                    201: 0,
                    202: 0,
                    203: 0,
                    204: 2304,
                    205: 2304,
                    401: 3,
                    501: 0.031649,
                    502: 0.0155,
                    503: 0.0,
                    504: 16,
                    505: 2,
                }

            def prop_setgetvalue(self, prop_id, value):
                self.set_calls.append((prop_id, value))
                self.values[prop_id] = int(value) if prop_id != 301 else float(value)
                return float(self.values[prop_id])

            def prop_getvalue(self, prop_id):
                return self.values[prop_id]

            def prop_queryvalue(self, prop_id, value):
                if prop_id == 401:
                    return 3
                if prop_id == 504 and value in {12, 16}:
                    return value
                return False

            def prop_getattr(self, prop_id):
                if prop_id in {202, 203}:
                    return SimpleNamespace(valuemin=0, valuemax=2300, valuestep=4)
                if prop_id in {204, 205}:
                    return SimpleNamespace(valuemin=4, valuemax=2304, valuestep=4)
                return False

            def prop_getvaluetext(self, prop_id, value):
                return str(value)

            def lasterr(self):
                return type("Err", (), {"name": "none"})()

        dcamapi4 = _fake_dcamapi4_for_roi_tests()
        adapter = FusionBtCameraAdapter()
        adapter._initialized = True
        adapter._dcamapi4 = dcamapi4
        adapter._dcam_camera = FakeCamera()
        adapter._connection_info = {"model": "ORCA-Fusion BT", "camera_id": "C15440-20UP"}
        adapter._ensure_camera_open = mock.Mock(return_value=dict(adapter._connection_info))

        config = CameraConfig(roi_x=120, roi_y=80, roi_width=2304, roi_height=2304, bit_depth=16)
        result = adapter.apply_config(config)

        self.assertEqual(result["applied_roi"], {"x": 0, "y": 0, "width": 2304, "height": 2304})
        self.assertEqual(result["roi_size_presets"], [(2304, 2304), (1152, 1152), (576, 576)])
        self.assertEqual(config.roi_x, 0)
        self.assertEqual(config.roi_y, 0)


class SimAcquisitionControllerTests(unittest.TestCase):
    def test_payload_capture_helper_does_not_start_internal_worker(self):
        from sim_control.controller import SimAcquisitionController
        from sim_control.models import PatternPreparationResult, SimTaskConfig

        controller = SimAcquisitionController()
        controller.pattern_result = PatternPreparationResult(
            pattern_files=["488_3.5_2d_1ms"] * 9,
            handles=[-1],
            metadata={"mode": "running_order", "running_order_name": "488_3.5_2d_1ms"},
        )
        controller._worker.slot_start = mock.Mock(side_effect=AssertionError("internal worker should stay disconnected"))

        try:
            task_id, emitted_payloads = _start_single_acquisition_for_payload_test(
                controller,
                SimTaskConfig(running_order_name="488_3.5_2d_1ms"),
            )
        finally:
            controller._thread.quit()
            controller._thread.wait(2000)

        self.assertTrue(task_id.startswith("sim_"))
        self.assertEqual(len(emitted_payloads), 1)
        self.assertEqual(emitted_payloads[0]["pattern_result"].handles, [-1])

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

    def test_apply_camera_config_syncs_applied_roi_from_adapter(self):
        from sim_control.controller import SimAcquisitionController
        from sim_control.models import CameraConfig

        controller = SimAcquisitionController()
        statuses = []
        controller.signal_status_changed.connect(lambda status, payload: statuses.append((status, payload)))
        controller.camera_adapter = mock.Mock()
        controller.camera_adapter.apply_config.return_value = {
            "applied_roi": {"x": 0, "y": 0, "width": 2048, "height": 2048},
            "sensor_width": 2048,
            "sensor_height": 2048,
            "roi_step_px": 4,
            "roi_size_presets": [(2048, 2048), (1024, 1024), (512, 512)],
        }

        try:
            config = CameraConfig(roi_width=2304, roi_height=2304)
            result = controller.apply_camera_config(config)
        finally:
            controller._thread.quit()
            controller._thread.wait(2000)

        self.assertEqual(config.roi_width, 2048)
        self.assertEqual(config.roi_height, 2048)
        self.assertEqual(result["applied_roi"]["width"], 2048)
        self.assertEqual(statuses[-1][1]["camera_config"]["roi_width"], 2048)

    def test_start_single_acquisition_uses_calculated_gap_only_below_default(self):
        from sim_control.controller import SimAcquisitionController
        from sim_control.models import SimTaskConfig

        cases = (
            (6_500, 6_500),
            (50_000, 50_000),
            (65_000, 50_000),
            (None, 50_000),
        )
        for recommended_gap_us, expected_gap_us in cases:
            with self.subTest(recommended_gap_us=recommended_gap_us):
                controller = SimAcquisitionController()
                controller.pattern_result.handles = [0]
                if recommended_gap_us is not None:
                    controller._latest_camera_timing = {"recommended_inter_frame_gap_us": recommended_gap_us}

                try:
                    task = SimTaskConfig()
                    task.timing.inter_frame_gap_us = 15_000
                    _, emitted_payloads = _start_single_acquisition_for_payload_test(controller, task)
                finally:
                    controller._thread.quit()
                    controller._thread.wait(2000)

                self.assertEqual(task.timing.inter_frame_gap_us, expected_gap_us)
                self.assertEqual(emitted_payloads[-1]["task"].timing.inter_frame_gap_us, expected_gap_us)

    def test_worker_camera_apply_uses_default_gap_when_recommendation_is_not_below_default(self):
        from sim_control.controller import SimAcquisitionWorker
        from sim_control.models import DaqLineConfig, PatternPreparationResult, SimTaskConfig

        worker = SimAcquisitionWorker()
        task = SimTaskConfig()
        task.timing.inter_frame_gap_us = 15_000
        camera = mock.Mock()
        camera.apply_config.return_value = {"recommended_inter_frame_gap_us": 65_000}

        worker.slot_start(
            {
                "task": task,
                "task_id": "task-1",
                "daq_config": DaqLineConfig(),
                "pattern_result": PatternPreparationResult(handles=[0]),
                "waveform_builder": mock.Mock(),
                "daq_adapter": mock.Mock(),
                "camera_adapter": camera,
                "slm_adapter": mock.Mock(),
                "apply_camera_config": True,
                "prepare_only": True,
            }
        )

        self.assertEqual(task.timing.inter_frame_gap_us, 50_000)

    def test_start_single_acquisition_accepts_running_order_mode(self):
        from sim_control.controller import SimAcquisitionController
        from sim_control.models import PatternPreparationResult, SimTaskConfig

        controller = SimAcquisitionController()
        controller.pattern_result = PatternPreparationResult(
            pattern_files=["488_3.5_2d_1ms"] * 9,
            handles=[-1],
            metadata={"mode": "running_order", "running_order_name": "488_3.5_2d_1ms"},
        )

        try:
            task = SimTaskConfig(running_order_name="488_3.5_2d_1ms")
            _, emitted_payloads = _start_single_acquisition_for_payload_test(controller, task)
        finally:
            controller._thread.quit()
            controller._thread.wait(2000)

        self.assertEqual(emitted_payloads[-1]["pattern_result"].handles, [-1])

    def test_start_single_acquisition_payload_includes_stop_event_and_stop_only_sets_event(self):
        from sim_control.controller import SimAcquisitionController
        from sim_control.models import PatternPreparationResult, SimTaskConfig

        controller = SimAcquisitionController()
        controller.pattern_result = PatternPreparationResult(
            pattern_files=["488_3.5_2d_1ms"] * 9,
            handles=[-1],
            metadata={"mode": "running_order", "running_order_name": "488_3.5_2d_1ms"},
        )
        controller.daq_adapter = mock.Mock()
        controller.camera_adapter = mock.Mock()

        try:
            _, emitted_payloads = _start_single_acquisition_for_payload_test(
                controller,
                SimTaskConfig(running_order_name="488_3.5_2d_1ms"),
            )
            stop_event = emitted_payloads[-1]["stop_event"]

            controller.stop()
        finally:
            controller._thread.quit()
            controller._thread.wait(2000)

        self.assertIsInstance(stop_event, threading.Event)
        self.assertTrue(stop_event.is_set())
        controller.daq_adapter.set_all_low.assert_not_called()
        controller.camera_adapter.disarm.assert_not_called()

    def test_worker_emits_cancelled_signal_without_failed_signal_for_acquisition_cancelled(self):
        from sim_control.acquisition_core import AcquisitionCancelled
        from sim_control.controller import SimAcquisitionWorker
        import sim_control.controller as controller_module

        worker = SimAcquisitionWorker()
        statuses = []
        cancelled = []
        failed = []
        worker.signal_status_changed.connect(lambda status, payload: statuses.append((status, payload)))
        worker.signal_acquisition_cancelled.connect(lambda task_id, message: cancelled.append((task_id, message)))
        worker.signal_acquisition_failed.connect(lambda task_id, message: failed.append((task_id, message)))

        payload = {
            "task_id": "cancel-test",
            "task": mock.Mock(),
            "daq_config": mock.Mock(),
            "pattern_result": mock.Mock(),
            "waveform_builder": mock.Mock(),
            "daq_adapter": mock.Mock(),
            "camera_adapter": mock.Mock(),
            "slm_adapter": mock.Mock(),
            "stop_event": threading.Event(),
        }
        with mock.patch.object(
            controller_module,
            "run_single_acquisition",
            side_effect=AcquisitionCancelled("cancel requested"),
        ):
            worker.slot_start(payload)

        self.assertEqual([status for status, _payload in statuses], ["acquisition_cancelled"])
        self.assertEqual(cancelled, [("cancel-test", "cancel requested")])
        self.assertEqual(failed, [])

    def test_start_single_acquisition_can_defer_running_order_selection_to_worker_payload(self):
        from sim_control.controller import SimAcquisitionController
        from sim_control.models import SimTaskConfig

        controller = SimAcquisitionController()

        try:
            _, emitted_payloads = _start_single_acquisition_for_payload_test(
                controller,
                SimTaskConfig(),
                prepare_running_order=True,
                initialize_hardware=True,
                apply_daq_config=True,
                apply_camera_config=True,
            )
        finally:
            controller._thread.quit()
            controller._thread.wait(2000)

        payload = emitted_payloads[-1]
        self.assertTrue(payload["prepare_running_order"])
        self.assertTrue(payload["initialize_hardware"])
        self.assertTrue(payload["apply_daq_config"])
        self.assertTrue(payload["apply_camera_config"])

    def test_start_prepare_experiment_emits_prepare_only_worker_payload(self):
        from sim_control.controller import SimAcquisitionController
        from sim_control.models import SimTaskConfig

        controller = SimAcquisitionController()

        try:
            _, emitted_payloads = _start_prepare_experiment_for_payload_test(
                controller,
                SimTaskConfig(),
                prepare_running_order=True,
                initialize_hardware=True,
                apply_daq_config=True,
                apply_camera_config=True,
            )
        finally:
            controller._thread.quit()
            controller._thread.wait(2000)

        payload = emitted_payloads[-1]
        self.assertTrue(payload["prepare_only"])
        self.assertTrue(payload["prepare_running_order"])
        self.assertTrue(payload["initialize_hardware"])
        self.assertTrue(payload["apply_daq_config"])
        self.assertTrue(payload["apply_camera_config"])


if __name__ == "__main__":
    unittest.main()

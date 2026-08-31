"""``FusionBtCameraAdapter`` 与 ``SimAcquisitionController`` 的综合测试。

作用：
    本文件是 SIM 系统最厚的测试集，覆盖：
        - ``CameraConfig`` round-trip：device_index/label、ROI、bit_depth、曝光等字段
          在 ``app_config_*_dict`` 转换前后保持一致。
        - 真实 ``FusionBtCameraAdapter`` 的 ROI 对齐、bit_depth 选择、readout speed 锁定、
          ``apply_config`` 错误回滚、``cap_transferinfo`` 路径、``read_preview_frame``
          dtype 等行为。这些都用 mock DCAM 模块替代真实 ``dcam.py`` / ``dcamapi4``。
        - ``SimAcquisitionController`` worker payload 字段：``start_single_acquisition``
          与 ``start_prepare_experiment`` 投递给 worker 的字典必须包含
          ``initialize_hardware/apply_daq_config/apply_camera_config/prepare_running_order``
          等开关、task/daq_config/pattern_result 引用、stop_event 引用。
        - Worker 中相机 / SLM / DAQ 预备阶段顺序：相机 ``apply_config`` 在 SLM
          ``select_running_order`` 之前；``prepare_only`` 路径不进 DAQ 播放。
        - controller 状态同步：``running_order_selected`` 状态广播让 controller 也更新
          ``pattern_result`` / ``selected_running_order``。

协作关系：
    上游：``unittest``、``unittest.mock``、``numpy``、``threading``。
    下游：``sim_control.adapters.FusionBtCameraAdapter``、``sim_control.controller``、
          ``sim_control.config_store``、``sim_control.models``、``sim_control.waveform``。

维护要点：
    - 真实 DCAM SDK 不可用时本测试依然能跑（全靠 mock）；改动 ``adapters.py`` 中
      DCAM 调用顺序前，请先阅读本测试覆盖的属性写入序列。
    - ``_start_*_for_payload_test`` 这两个 helper 把 controller 的 ``signal_start_worker``
      重新绑定到本地 list，便于断言 worker payload 内容；用完测试不要忘了
      ``controller.shutdown()`` 避免 QThread 泄漏。
"""

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
    """断开 controller 默认的 ``signal_start_worker`` 信号并改接到本地 list。

    用途：
        许多测试需要"controller 发起 worker 时"捕获 payload 内容做断言。该 helper
        让 ``start_single_acquisition`` 把 payload 投递到 ``emitted_payloads``，
        而不是真正的 worker。
    返回：
        ``(task_id, emitted_payloads)``；后者会在 emit 后追加 payload dict。
    """
    emitted_payloads = []
    # 旧绑定可能不存在；``TypeError/RuntimeError`` 都安全吞掉。
    try:
        controller.signal_start_worker.disconnect()
    except (TypeError, RuntimeError):
        pass
    controller.signal_start_worker.connect(lambda payload: emitted_payloads.append(payload))
    if not controller.reconstruction_config.otf_path_for_wavelength(task.laser_wavelength_nm):
        setattr(
            controller.reconstruction_config,
            f"otf_{int(task.laser_wavelength_nm)}_path",
            __file__,
        )
    task_id = controller.start_single_acquisition(task, **kwargs)
    return task_id, emitted_payloads


def _start_prepare_experiment_for_payload_test(controller, task, **kwargs):
    """同 ``_start_single_acquisition_for_payload_test``，但走 prepare-only 路径。"""
    emitted_payloads = []
    try:
        controller.signal_start_worker.disconnect()
    except (TypeError, RuntimeError):
        pass
    controller.signal_start_worker.connect(lambda payload: emitted_payloads.append(payload))
    if not controller.reconstruction_config.otf_path_for_wavelength(task.laser_wavelength_nm):
        setattr(
            controller.reconstruction_config,
            f"otf_{int(task.laser_wavelength_nm)}_path",
            __file__,
        )
    task_id = controller.start_prepare_experiment(task, **kwargs)
    return task_id, emitted_payloads


class CameraConfigTests(unittest.TestCase):
    """覆盖 ``CameraConfig`` 在 dict round-trip 与默认值层面的不变量。"""
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
    """为测试准备 _fake_dcamapi4_for_roi_tests 所需的轻量对象、导入入口或断言辅助。"""
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
    """覆盖 ``FusionBtCameraAdapter`` 在 mock DCAM SDK 下的 ROI/位深/触发/读帧行为。

    本测试用一组手写 mock 替代真实 ``dcam.py`` / ``dcamapi4``：
        - ``apply_config`` 写入属性顺序、错误回滚、ROI 步进对齐、bit_depth 选择。
        - ``read_frame_sequence`` 优先消费 ``cap_transferinfo`` 已到帧，再 wait 新增帧。
        - ``read_preview_frame`` dtype/uint16 转换。
        - 通过 ``mock.patch.object`` 替换模块内的 ``importlib.import_module`` 来注入假 DCAM 模块。
    """
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
        # FakeCamera 只有 buf_getframedata（无 buf_getframe）→ 走回退路径，无硬件时间戳。
        self.assertIsNone(adapter.get_last_hardware_timestamps())

    def test_read_frame_sequence_collects_dcam_hardware_timestamps_via_buf_getframe(self):
        """绑定支持 buf_getframe 时：像素与 DCAMBUF_FRAME.timestamp 一次取出；

        软件消费时刻（返回值第二项）语义不变，硬件时间戳走 get_last_hardware_timestamps()
        新字段（审查条目 #24：新增而非替换）。
        """
        from sim_control.adapters import FusionBtCameraAdapter
        from sim_control.models import CameraConfig

        class FakeCamera:
            def __init__(self):
                self.frames = [np.full((2, 2), index, dtype=np.uint16) for index in range(3)]

            def cap_transferinfo(self):
                return SimpleNamespace(nFrameCount=3)

            def wait_capevent_frameready(self, timeout_ms):
                return False

            def buf_getframe(self, index):
                frame_struct = SimpleNamespace(
                    timestamp=SimpleNamespace(sec=100 + index, microsec=250_000),
                    framestamp=index,
                )
                return (frame_struct, self.frames[index])

            def buf_getframedata(self, index):
                raise AssertionError("buf_getframe available; fallback path should not be used")

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

        self.assertEqual(stack.shape, (3, 2, 2))
        self.assertEqual([int(frame[0, 0]) for frame in stack], [0, 1, 2])
        # 1) 软件时间戳合同不变：长度 = 帧数。
        self.assertEqual(len(timestamps), 3)
        # 2) 硬件时间戳 = sec + microsec×1e-6。
        self.assertEqual(adapter.get_last_hardware_timestamps(), [100.25, 101.25, 102.25])
        # 3) getter 返回副本：调用方修改不影响 adapter 内部状态。
        adapter.get_last_hardware_timestamps().append(999.0)
        self.assertEqual(len(adapter.get_last_hardware_timestamps()), 3)

    def test_read_frame_sequence_missing_timestamp_field_reports_none_not_partial_list(self):
        """个别帧缺 timestamp 字段时整组置 None，不返回残缺硬件时间戳列表。"""
        from sim_control.adapters import FusionBtCameraAdapter
        from sim_control.models import CameraConfig

        class FakeCamera:
            def __init__(self):
                self.frames = [np.full((2, 2), index, dtype=np.uint16) for index in range(2)]

            def cap_transferinfo(self):
                return SimpleNamespace(nFrameCount=2)

            def wait_capevent_frameready(self, timeout_ms):
                return False

            def buf_getframe(self, index):
                # 第 2 帧没有 timestamp 字段，模拟旧固件/驱动。
                frame_struct = (
                    SimpleNamespace(timestamp=SimpleNamespace(sec=1, microsec=0))
                    if index == 0
                    else SimpleNamespace()
                )
                return (frame_struct, self.frames[index])

            def lasterr(self):
                return SimpleNamespace(name="TIMEOUT")

        adapter = FusionBtCameraAdapter()
        adapter._armed = True
        adapter._camera_config = CameraConfig(roi_width=2, roi_height=2, exposure_us=500_000)
        adapter._dcam_camera = FakeCamera()

        stack, timestamps = adapter.read_frame_sequence(
            frame_count=2,
            pattern_files=[""] * 2,
            laser_wavelength_nm=488,
        )

        self.assertEqual(stack.shape, (2, 2, 2))
        self.assertEqual(len(timestamps), 2)
        self.assertIsNone(adapter.get_last_hardware_timestamps())

    def test_read_frame_sequence_all_zero_timestamps_report_none_not_epoch_zero(self):
        """固件不支持 timestamp 时字段留 0 不抛异常 → 整组全 0 应视为 None，不输出 epoch-0 假值。"""
        from sim_control.adapters import FusionBtCameraAdapter
        from sim_control.models import CameraConfig

        class FakeCamera:
            def __init__(self):
                self.frames = [np.full((2, 2), index, dtype=np.uint16) for index in range(2)]

            def cap_transferinfo(self):
                return SimpleNamespace(nFrameCount=2)

            def wait_capevent_frameready(self, timeout_ms):
                return False

            def buf_getframe(self, index):
                frame_struct = SimpleNamespace(timestamp=SimpleNamespace(sec=0, microsec=0))
                return (frame_struct, self.frames[index])

            def lasterr(self):
                return SimpleNamespace(name="TIMEOUT")

        adapter = FusionBtCameraAdapter()
        adapter._armed = True
        adapter._camera_config = CameraConfig(roi_width=2, roi_height=2, exposure_us=500_000)
        adapter._dcam_camera = FakeCamera()

        stack, timestamps = adapter.read_frame_sequence(
            frame_count=2,
            pattern_files=[""] * 2,
            laser_wavelength_nm=488,
        )

        self.assertEqual(stack.shape, (2, 2, 2))
        self.assertEqual(len(timestamps), 2)
        self.assertIsNone(adapter.get_last_hardware_timestamps())

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
                self.get_calls = []
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
                self.get_calls.append(prop_id)
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
        self.assertEqual(result["theoretical_min_inter_frame_gap_us"], 5501)
        self.assertEqual(result["inter_frame_gap_safety_margin_us"], 500)
        self.assertEqual(result["recommended_inter_frame_gap_us"], 6001)
        self.assertFalse(result["timing_fallback_used"])
        self.assertEqual(result["timing_readout_time_s"], 0.031649)
        self.assertEqual(result["timing_cyclic_trigger_period_s"], 0.0155)
        self.assertEqual(result["timing_min_trigger_blanking_s"], 0.0)
        self.assertIn((504, 12), adapter._dcam_camera.set_calls)
        self.assertIn((505, 2), adapter._dcam_camera.set_calls)

        adapter.apply_config(
            CameraConfig(
                device_index=0,
                device_label="0: ORCA-Fusion BT [CAM-001]",
                roi_x=324,
                roi_y=324,
                roi_width=512,
                roi_height=512,
                exposure_us=3_000,
                bit_depth=16,
            )
        )
        for timing_prop_id in (501, 502, 503):
            self.assertEqual(adapter._dcam_camera.get_calls.count(timing_prop_id), 2)

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
        self.assertTrue(result["timing_fallback_used"])
        self.assertIn("TIMING_READOUTTIME", result["timing_fallback_reason"])

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
        self.assertEqual(result["theoretical_min_inter_frame_gap_us"], 2001)
        self.assertEqual(result["recommended_inter_frame_gap_us"], 2501)

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
    """覆盖 ``SimAcquisitionController`` 的 payload 字段、状态同步与 worker 调度。

    重点验证：
        - ``start_single_acquisition`` 与 ``start_prepare_experiment`` 投递给 worker
          的 payload 包含正确开关与共享 adapter 引用。
        - ``running_order_selected`` / ``patterns_prepared`` / ``camera_config_applied``
          状态信号被 controller 正确转发并更新本地缓存。
        - ``stop()`` 把 stop_event 置位且不直接接触硬件。
        - ``shutdown()`` 收尾 QThread 与 adapter disconnect。
    """

    def test_simulated_camera_reports_complete_timing_contract_for_current_roi_and_exposure(self):
        from sim_control.models import CameraConfig
        from sim_control.sim_adapters import SimulatedCameraAdapter

        camera = SimulatedCameraAdapter()
        result = camera.apply_config(
            CameraConfig(roi_width=512, roi_height=512, exposure_us=3_000)
        )

        self.assertEqual(result["timing_readout_time_s"], 0.0025)
        self.assertEqual(result["timing_cyclic_trigger_period_s"], 0.0035)
        self.assertEqual(result["timing_min_trigger_blanking_s"], 0.00025)
        self.assertEqual(result["theoretical_min_inter_frame_gap_us"], 501)
        self.assertEqual(result["inter_frame_gap_safety_margin_us"], 500)
        self.assertEqual(result["recommended_inter_frame_gap_us"], 1001)
        self.assertFalse(result["timing_fallback_used"])

    def test_647_machine_controller_validates_and_worker_selects_exact_647_zscan_ro(self):
        from sim_control.controller import SimAcquisitionController, SimAcquisitionWorker
        from sim_control.models import BackendConfig, CameraConfig, SimTaskConfig, TimingConfig, ZScanConfig

        controller = SimAcquisitionController(
            BackendConfig(simulation_mode=True),
            red_laser_nm=647,
        )
        task = SimTaskConfig(
            laser_wavelength_nm=647,
            camera=CameraConfig(roi_width=16, roi_height=16, exposure_us=10_000),
            timing=TimingConfig(inter_frame_gap_us=1_000),
        )
        z_scan = ZScanConfig(enabled=True, start_um=0.0, step_um=0.1, num_steps=1, exposure_preset_ms=8)
        worker = SimAcquisitionWorker()
        statuses = []
        worker.signal_status_changed.connect(lambda status, payload: statuses.append((status, payload)))

        try:
            _task_id, emitted_payloads = _start_single_acquisition_for_payload_test(
                controller,
                task,
                prepare_running_order=True,
                initialize_hardware=False,
                apply_daq_config=False,
                apply_camera_config=False,
                z_scan_config=z_scan,
                z_scan_enabled=True,
            )
            payload = emitted_payloads[-1]
            # 直接让同一 payload 走 worker 选 RO，prepare-only 避免此 API 测试追加正式 SIM9。
            payload["prepare_only"] = True
            worker.slot_start(payload)
        finally:
            controller.shutdown()

        zscan_payload = next(data for status, data in statuses if status == "z_scan_running_order_selected")
        self.assertEqual(controller.red_laser_nm, 647)
        self.assertEqual(zscan_payload["running_order_name"], "647_3.5_2d_zscan3p_8ms")
        self.assertEqual(zscan_payload["warnings"], [])

    def test_647_machine_worker_falls_back_to_638_zscan_ro_with_warning_when_exact_missing(self):
        from sim_control.controller import SimAcquisitionController, SimAcquisitionWorker
        from sim_control.models import BackendConfig, PatternPreparationResult, SimTaskConfig, ZScanConfig

        controller = SimAcquisitionController(
            BackendConfig(simulation_mode=True),
            red_laser_nm=647,
        )
        task = SimTaskConfig(laser_wavelength_nm=647)
        z_scan = ZScanConfig(enabled=True, start_um=0.0, step_um=0.1, num_steps=1, exposure_preset_ms=8)
        worker = SimAcquisitionWorker()
        statuses = []
        worker.signal_status_changed.connect(lambda status, payload: statuses.append((status, payload)))
        orders = {
            4: "647_3.5_2d_10ms",
            9: "638_3.5_2d_zscan3p_8ms",
        }
        slm = mock.Mock()
        slm.list_running_orders.return_value = list(orders.items())

        def select_running_order(index):
            name = orders[int(index)]
            result = PatternPreparationResult(
                pattern_files=[name] * 9,
                handles=[-1],
                metadata={
                    "mode": "running_order",
                    "running_order_index": int(index),
                    "running_order_name": name,
                },
            )
            return {"running_order_name": name, "pattern_result": result}

        slm.select_running_order.side_effect = select_running_order

        try:
            _task_id, emitted_payloads = _start_single_acquisition_for_payload_test(
                controller,
                task,
                prepare_running_order=True,
                z_scan_config=z_scan,
                z_scan_enabled=True,
            )
            payload = emitted_payloads[-1]
            payload["slm_adapter"] = slm
            payload["prepare_only"] = True
            worker.slot_start(payload)
        finally:
            controller.shutdown()

        zscan_payload = next(data for status, data in statuses if status == "z_scan_running_order_selected")
        self.assertEqual(zscan_payload["running_order_name"], "638_3.5_2d_zscan3p_8ms")
        self.assertTrue(
            any("Using 638 nm" in warning and "requested 647 nm" in warning
                for warning in zscan_payload["warnings"])
        )

    def test_controller_defaults_to_638_machine_identity(self):
        from sim_control.controller import SimAcquisitionController
        from sim_control.models import SimTaskConfig

        controller = SimAcquisitionController()
        try:
            self.assertEqual(controller.red_laser_nm, 638)
            with self.assertRaisesRegex(ValueError, "selected_laser_nm"):
                _start_single_acquisition_for_payload_test(
                    controller,
                    SimTaskConfig(laser_wavelength_nm=647),
                    prepare_running_order=True,
                )
        finally:
            controller.shutdown()

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

    def test_start_single_acquisition_uses_signature_matched_calculated_gap_without_50ms_cap(self):
        from sim_control.controller import SimAcquisitionController
        from sim_control.models import SimTaskConfig

        cases = (
            (6_500, 6_500),
            (50_000, 50_000),
            (65_000, 65_000),
            (None, 50_000),
        )
        for recommended_gap_us, expected_gap_us in cases:
            with self.subTest(recommended_gap_us=recommended_gap_us):
                controller = SimAcquisitionController()
                controller.pattern_result.handles = [0]
                if recommended_gap_us is not None:
                    controller._latest_camera_timing = {
                        "camera_config": dict(SimTaskConfig().camera.__dict__),
                        "recommended_inter_frame_gap_us": recommended_gap_us,
                    }

                try:
                    task = SimTaskConfig()
                    task.timing.inter_frame_gap_us = 15_000
                    _, emitted_payloads = _start_single_acquisition_for_payload_test(controller, task)
                finally:
                    controller._thread.quit()
                    controller._thread.wait(2000)

                self.assertEqual(task.timing.inter_frame_gap_us, expected_gap_us)
                self.assertEqual(emitted_payloads[-1]["task"].timing.inter_frame_gap_us, expected_gap_us)

    def test_start_single_acquisition_rejects_stale_preview_timing_signature(self):
        from sim_control.controller import SimAcquisitionController
        from sim_control.models import SimTaskConfig

        controller = SimAcquisitionController()
        controller.pattern_result.handles = [0]
        task = SimTaskConfig()
        preview_config = dict(task.camera.__dict__)
        preview_config["exposure_us"] = 1_000
        controller._latest_camera_timing = {
            "camera_config": preview_config,
            "recommended_inter_frame_gap_us": 2_500,
        }

        try:
            _, emitted_payloads = _start_single_acquisition_for_payload_test(controller, task)
        finally:
            controller._thread.quit()
            controller._thread.wait(2000)

        self.assertEqual(task.timing.inter_frame_gap_us, 50_000)
        self.assertEqual(emitted_payloads[-1]["task"].timing.inter_frame_gap_us, 50_000)

    def test_start_single_acquisition_rejects_disabled_camera_timing_preflight(self):
        from sim_control.controller import SimAcquisitionController
        from sim_control.models import SimTaskConfig

        controller = SimAcquisitionController()
        controller.pattern_result.handles = [0]

        try:
            with self.assertRaisesRegex(ValueError, "apply_camera_config=True"):
                _start_single_acquisition_for_payload_test(
                    controller,
                    SimTaskConfig(),
                    apply_camera_config=False,
                )
        finally:
            controller._thread.quit()
            controller._thread.wait(2000)

    def test_worker_camera_apply_uses_recommendation_above_default(self):
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

        self.assertEqual(task.timing.inter_frame_gap_us, 65_000)

    def test_formal_worker_applies_current_timing_before_acquisition_build_boundary(self):
        from sim_control.controller import SimAcquisitionWorker
        from sim_control.models import DaqLineConfig, PatternPreparationResult, SimTaskConfig
        import sim_control.controller as controller_module

        worker = SimAcquisitionWorker()
        task = SimTaskConfig()
        task.timing.inter_frame_gap_us = 2_500
        call_order = []
        camera = mock.Mock()

        def apply_current_config(_config):
            call_order.append("apply_camera_config")
            return {"recommended_inter_frame_gap_us": 65_000}

        def run_formal_acquisition(**kwargs):
            call_order.append("run_single_acquisition")
            self.assertEqual(kwargs["task"].timing.inter_frame_gap_us, 65_000)
            return SimpleNamespace(
                task_id="task-1",
                stack=np.zeros((9, 2, 2), dtype=np.uint16),
                metadata={},
            )

        camera.apply_config.side_effect = apply_current_config
        with mock.patch.object(
            controller_module,
            "run_single_acquisition",
            side_effect=run_formal_acquisition,
        ):
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
                    "prepare_only": False,
                }
            )

        self.assertEqual(call_order, ["apply_camera_config", "run_single_acquisition"])

    def test_effective_inter_frame_gap_rejects_non_finite_or_negative_values(self):
        from sim_control.models import effective_inter_frame_gap_us

        for value in (None, False, True, -1, float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value):
                self.assertEqual(effective_inter_frame_gap_us(value), 50_000)

    def test_worker_camera_status_refreshes_timing_cache_and_disconnect_clears_it(self):
        from sim_control.controller import SimAcquisitionController
        from sim_control.models import CameraConfig

        controller = SimAcquisitionController()
        payload = {
            "camera_config": dict(CameraConfig(exposure_us=3_000).__dict__),
            "recommended_inter_frame_gap_us": 4_500,
        }
        controller.camera_adapter = mock.Mock()

        try:
            controller._handle_worker_status("camera_config_applied", payload)
            self.assertEqual(controller._latest_camera_timing, payload)
            controller.disconnect_camera()
            self.assertEqual(controller._latest_camera_timing, {})
        finally:
            controller._thread.quit()
            controller._thread.wait(2000)

    def test_disconnect_camera_clears_timing_cache_when_adapter_disconnect_fails(self):
        from sim_control.controller import SimAcquisitionController

        controller = SimAcquisitionController()
        controller._latest_camera_timing = {"recommended_inter_frame_gap_us": 4_500}
        controller.camera_adapter = mock.Mock()
        controller.camera_adapter.disconnect.side_effect = RuntimeError("disconnect failed")

        try:
            with self.assertRaisesRegex(RuntimeError, "disconnect failed"):
                controller.disconnect_camera()
            self.assertEqual(controller._latest_camera_timing, {})
        finally:
            controller._thread.quit()
            controller._thread.wait(2000)

    def test_camera_timing_fallback_emits_warning_status(self):
        from sim_control.controller import SimAcquisitionController
        from sim_control.models import CameraConfig

        controller = SimAcquisitionController()
        statuses = []
        controller.signal_status_changed.connect(lambda status, payload: statuses.append((status, payload)))
        controller.camera_adapter = mock.Mock()
        controller.camera_adapter.apply_config.return_value = {
            "timing_fallback_used": True,
            "timing_fallback_reason": "TIMING_READOUTTIME is unavailable.",
        }

        try:
            controller.apply_camera_config(CameraConfig())
        finally:
            controller._thread.quit()
            controller._thread.wait(2000)

        warning_statuses = [payload for status, payload in statuses if status == "camera_timing_fallback_warning"]
        self.assertEqual(len(warning_statuses), 1)
        self.assertIn("TIMING_READOUTTIME", warning_statuses[0]["message"])

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

    def test_start_single_acquisition_payload_includes_z_scan_config_and_stage_adapter(self):
        from sim_control.controller import SimAcquisitionController
        from sim_control.models import SimTaskConfig, ZScanConfig

        controller = SimAcquisitionController()

        try:
            _, emitted_payloads = _start_single_acquisition_for_payload_test(
                controller,
                SimTaskConfig(),
                prepare_running_order=True,
                initialize_hardware=True,
                apply_daq_config=True,
                apply_camera_config=True,
                z_scan_config=ZScanConfig(enabled=True, exposure_preset_ms=8),
            )
        finally:
            controller._thread.quit()
            controller._thread.wait(2000)

        payload = emitted_payloads[-1]
        self.assertTrue(payload["z_scan_enabled"])
        self.assertEqual(payload["z_scan_config"].exposure_preset_ms, 8)
        self.assertIs(payload["stage_adapter"], controller.stage_adapter)

    def test_worker_runs_z_scan_then_restores_formal_running_order_before_sim9(self):
        from sim_control.controller import SimAcquisitionWorker
        from sim_control.models import CameraConfig, DaqLineConfig, PatternPreparationResult, SimTaskConfig, TimingConfig, ZScanConfig
        from sim_control.sim_adapters import SimulatedCameraAdapter, SimulatedDaqAdapter, SimulatedSlmAdapter
        from sim_control.stage_adapter import SimulatedZStageAdapter
        from sim_control.waveform import NIDaqWaveformBuilder

        worker = SimAcquisitionWorker()
        statuses = []
        ready_batches = []
        summaries = []
        worker.signal_status_changed.connect(lambda status, payload: statuses.append((status, payload)))
        worker.signal_acquisition_ready.connect(lambda batch: ready_batches.append(batch))
        worker.signal_acquisition_summary_ready.connect(lambda payload: summaries.append(payload))

        task = SimTaskConfig(
            laser_wavelength_nm=561,
            camera=CameraConfig(roi_width=32, roi_height=32, exposure_us=10_000),
            timing=TimingConfig(inter_frame_gap_us=1_000),
        )

        worker.slot_start(
            {
                "task": task,
                "task_id": "zscan-worker-test",
                "daq_config": DaqLineConfig(),
                "pattern_result": PatternPreparationResult(),
                "waveform_builder": NIDaqWaveformBuilder(),
                "daq_adapter": SimulatedDaqAdapter(),
                "camera_adapter": SimulatedCameraAdapter(),
                "slm_adapter": SimulatedSlmAdapter(),
                "stage_adapter": SimulatedZStageAdapter(start_um=0.0, min_um=-5.0, max_um=5.0),
                "z_scan_config": ZScanConfig(start_um=0.0, step_um=0.2, num_steps=2, exposure_preset_ms=8),
                "z_scan_enabled": True,
                "prepare_running_order": True,
                "apply_daq_config": True,
                "apply_camera_config": True,
            }
        )

        status_names = [status for status, _payload in statuses]
        self.assertIn("running_order_selected", status_names)
        self.assertIn("z_scan_running_order_selected", status_names)
        self.assertIn("z_scan_complete", status_names)
        self.assertIn("running_order_restored", status_names)
        self.assertEqual(status_names[-1], "acquisition_complete")
        self.assertEqual(len(ready_batches), 1)
        self.assertEqual(ready_batches[0].stack.shape, (9, 32, 32))
        self.assertEqual(ready_batches[0].metadata["running_order_name"], "561_3.5_2d_10ms")
        self.assertIn("z_scan", ready_batches[0].metadata)
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["task_id"], "zscan-worker-test")
        self.assertEqual(summaries[0]["stack_shape"], [9, 32, 32])
        self.assertEqual(summaries[0]["stack_dtype"], "uint16")
        self.assertEqual(summaries[0]["metadata"]["running_order_name"], "561_3.5_2d_10ms")
        self.assertIn("z_scan", summaries[0]["metadata"])
        self.assertNotIn("stack", summaries[0])
        self.assertNotIn("frames", summaries[0])

    def test_worker_owns_formal_ro_restore_across_success_cancel_error_and_restore_failure(self):
        from sim_control.controller import SimAcquisitionWorker
        from sim_control.models import (
            AcquisitionBatch,
            DaqLineConfig,
            PatternPreparationResult,
            SimTaskConfig,
            ZScanConfig,
        )

        orders = {
            3: "488_3.5_2d_10ms",
            7: "488_3.5_2d_zscan3p_8ms",
        }

        class RecordingSlm:
            def __init__(self, stop_event, *, cancel_after_zscan=False, fail_restore=False):
                self.stop_event = stop_event
                self.cancel_after_zscan = cancel_after_zscan
                self.fail_restore = fail_restore
                self.selected = []
                self.formal_select_count = 0

            def list_running_orders(self):
                return list(orders.items())

            def initialize(self):
                return None

            def select_running_order(self, index):
                index = int(index)
                self.selected.append(index)
                if index == 3:
                    self.formal_select_count += 1
                    if self.fail_restore and self.formal_select_count >= 2:
                        raise RuntimeError("formal restore failed")
                if index == 7 and self.cancel_after_zscan:
                    self.stop_event.set()
                name = orders[index]
                pattern = PatternPreparationResult(
                    pattern_files=[name] * 9,
                    handles=[-1],
                    metadata={
                        "mode": "running_order",
                        "running_order_index": index,
                        "running_order_name": name,
                    },
                )
                return {"running_order_name": name, "pattern_result": pattern}

        success_batch = AcquisitionBatch(
            task_id="worker-ro-restore",
            stack=np.zeros((9, 2, 2), dtype=np.uint16),
            timestamps=[float(index) for index in range(9)],
            laser_wavelength_nm=488,
            exposure_us=10_000,
            pattern_files=["formal"] * 9,
        )
        cases = (
            ("success", False, False, None),
            ("cancel_after_zscan", True, False, None),
            ("scan_error", False, False, RuntimeError("scan failed")),
            ("restore_error", False, True, None),
            ("combined_error", False, True, RuntimeError("scan failed")),
        )
        for name, cancel_after_zscan, fail_restore, scan_error in cases:
            with self.subTest(name=name):
                worker = SimAcquisitionWorker()
                stop_event = threading.Event()
                slm = RecordingSlm(
                    stop_event,
                    cancel_after_zscan=cancel_after_zscan,
                    fail_restore=fail_restore,
                )
                ready = []
                failed = []
                cancelled = []
                worker.signal_acquisition_ready.connect(ready.append)
                worker.signal_acquisition_failed.connect(lambda task_id, message: failed.append(message))
                worker.signal_acquisition_cancelled.connect(
                    lambda task_id, message: cancelled.append(message)
                )
                payload = {
                    "task": SimTaskConfig(laser_wavelength_nm=488),
                    "task_id": "worker-ro-restore",
                    "daq_config": DaqLineConfig(),
                    "pattern_result": PatternPreparationResult(),
                    "waveform_builder": mock.Mock(),
                    "daq_adapter": mock.Mock(),
                    "camera_adapter": mock.Mock(
                        apply_config=mock.Mock(return_value={}),
                    ),
                    "slm_adapter": slm,
                    "stage_adapter": SimpleNamespace(
                        is_connected=True,
                        get_position_um=lambda: 0.0,
                        get_z_ranges_um=lambda: (-5.0, 5.0),
                    ),
                    "z_scan_config": ZScanConfig(
                        enabled=True,
                        start_um=0.0,
                        step_um=0.1,
                        num_steps=1,
                        exposure_preset_ms=8,
                    ),
                    "z_scan_enabled": True,
                    "stop_event": stop_event,
                    "prepare_running_order": True,
                }
                run_effect = scan_error if scan_error is not None else success_batch
                with mock.patch("sim_control.controller.run_single_acquisition", side_effect=[run_effect]):
                    worker.slot_start(payload)

                self.assertEqual(slm.selected, [3, 7, 3])
                if name == "success":
                    self.assertEqual(len(ready), 1)
                    self.assertEqual(failed, [])
                    self.assertEqual(cancelled, [])
                elif name == "cancel_after_zscan":
                    self.assertEqual(ready, [])
                    self.assertEqual(failed, [])
                    self.assertEqual(len(cancelled), 1)
                else:
                    self.assertEqual(ready, [])
                    self.assertEqual(len(failed), 1)
                    if fail_restore:
                        self.assertIn("formal restore failed", failed[0])
                    if scan_error is not None:
                        self.assertIn("scan failed", failed[0])

    def test_controller_clears_stop_event_from_acquisition_summary(self):
        from sim_control.controller import SimAcquisitionController

        controller = SimAcquisitionController()
        try:
            stop_event = threading.Event()
            controller._active_stop_events["summary-task"] = stop_event
            controller._current_stop_event = stop_event

            controller._clear_stop_event_for_summary(
                {
                    "task_id": "summary-task",
                    "stack_shape": [9, 32, 32],
                    "stack_dtype": "uint16",
                    "metadata": {},
                }
            )

            self.assertEqual(controller._active_stop_events, {})
            self.assertIsNone(controller._current_stop_event)
        finally:
            controller.shutdown()


class ZStackControllerWorkerTests(unittest.TestCase):
    """Dedicated Z-stack/focus-writer routing must stay separate from normal SIM9."""

    @staticmethod
    def _formal_slm():
        from sim_control.models import PatternPreparationResult

        orders = {
            3: "488_3.5_2d_10ms",
            7: "488_3.5_2d_zscan3p_8ms",
        }
        slm = mock.Mock()
        slm.list_running_orders.return_value = list(orders.items())

        def select(index):
            index = int(index)
            name = orders[index]
            pattern = PatternPreparationResult(
                pattern_files=[name] * 9,
                handles=[-1],
                metadata={
                    "mode": "running_order",
                    "running_order_index": index,
                    "running_order_name": name,
                },
            )
            return {"running_order_name": name, "pattern_result": pattern}

        slm.select_running_order.side_effect = select
        return slm

    @staticmethod
    def _stage():
        stage = mock.Mock()
        stage.is_connected = True
        stage.get_position_um.return_value = 10.0
        stage.get_z_ranges_um.return_value = (0.0, 100.0)
        return stage

    def test_finalize_timeout_waits_for_existing_terminal_without_resubmitting(self):
        from sim_control.controller import _finalize_series_writer
        from sim_control.z_stack_io import SeriesWriteResult, SeriesWriteTimeout

        terminal = SeriesWriteResult(
            job_id="timeout-job",
            kind="zstack",
            outcome="complete",
            completed_layers=1,
            total_layers=1,
            output_paths=(Path("stack.tif"),),
            partial=False,
            bigtiff=False,
        )
        writer = mock.Mock()
        writer.finalize.side_effect = SeriesWriteTimeout("terminal still pending")
        writer.wait_for_terminal.return_value = terminal

        result = _finalize_series_writer(writer, "complete", message="")

        self.assertIs(result, terminal)
        writer.finalize.assert_called_once_with("complete", message="")
        writer.wait_for_terminal.assert_called_once_with(timeout=10.0)

    def test_finalize_keeps_waiting_for_a_large_merge_without_resubmitting_terminal(self):
        from sim_control.controller import _finalize_series_writer
        from sim_control.z_stack_io import SeriesWriteResult, SeriesWriteTimeout

        terminal = SeriesWriteResult(
            job_id="large-merge",
            kind="zstack",
            outcome="complete",
            completed_layers=20,
            total_layers=20,
            output_paths=(Path("large-stack.tif"),),
            partial=False,
            bigtiff=True,
        )
        writer = mock.Mock()
        writer.finalize.side_effect = SeriesWriteTimeout("initial merge wait expired")
        writer.wait_for_terminal.side_effect = [
            SeriesWriteTimeout("merge still running"),
            SeriesWriteTimeout("merge still running"),
            terminal,
        ]

        result = _finalize_series_writer(writer, "complete", message="done")

        self.assertIs(result, terminal)
        writer.finalize.assert_called_once_with("complete", message="done")
        self.assertEqual(writer.wait_for_terminal.call_count, 3)
        writer.wait_for_terminal.assert_has_calls([mock.call(timeout=10.0)] * 3)

    def test_controller_z_stack_result_clears_parent_stop_event(self):
        from sim_control.acquisition_core import ZStackAcquisitionResult
        from sim_control.controller import SimAcquisitionController

        controller = SimAcquisitionController()
        try:
            stop_event = threading.Event()
            controller._active_stop_events["stack-done"] = stop_event
            controller._current_stop_event = stop_event
            controller._clear_stop_event_for_z_stack_result(
                ZStackAcquisitionResult(
                    task_id="stack-done",
                    requested_z_um=(0.0,),
                    measured_z_um=(0.0,),
                    completed_layers=1,
                    total_layers=1,
                    layer_shape=(9, 4, 4),
                    status="complete",
                )
            )
            self.assertEqual(controller._active_stop_events, {})
            self.assertIsNone(controller._current_stop_event)
        finally:
            controller.shutdown()

    def test_controller_z_stack_api_validates_mode_and_emits_dedicated_payload(self):
        from sim_control.controller import SimAcquisitionController
        from sim_control.models import BackendConfig, SimTaskConfig, ZScanConfig

        injected_writer = mock.Mock()
        controller = SimAcquisitionController(
            BackendConfig(simulation_mode=True),
            z_stack_writer=injected_writer,
        )
        emitted = []
        try:
            controller.signal_start_worker.disconnect()
            controller.signal_start_worker.connect(emitted.append)
            controller.reconstruction_config.otf_488_path = __file__
            stack_config = ZScanConfig(
                enabled=True,
                select_focus_plane=False,
                start_um=999.0,
                step_um=0.5,
                num_steps=2,
            )
            task_id = controller.start_z_stack_acquisition(
                SimTaskConfig(),
                output_path=Path("z-stack.tif"),
                z_scan_config=stack_config,
            )

            self.assertEqual(len(emitted), 1)
            payload = emitted[0]
            self.assertEqual(payload["acquisition_mode"], "z_stack")
            self.assertIs(payload["series_writer"], injected_writer)
            self.assertEqual(payload["z_stack_output_path"], Path("z-stack.tif"))
            self.assertTrue(payload["initialize_hardware"])
            self.assertTrue(payload["apply_daq_config"])
            self.assertTrue(payload["apply_camera_config"])
            self.assertTrue(payload["prepare_running_order"])
            self.assertIsNone(payload["z_scan_config"].start_um)
            self.assertIn(task_id, controller._active_stop_events)

            with self.assertRaisesRegex(ValueError, "start_z_stack_acquisition"):
                controller.start_single_acquisition(
                    SimTaskConfig(),
                    prepare_running_order=True,
                    z_scan_config=stack_config,
                    z_scan_enabled=True,
                )
            with self.assertRaises(ValueError):
                controller.start_z_stack_acquisition(
                    SimTaskConfig(),
                    output_path=Path("bad.tif"),
                    z_scan_config=ZScanConfig(enabled=True, select_focus_plane=True),
                )
            with self.assertRaises(ValueError):
                controller.start_z_stack_acquisition(
                    SimTaskConfig(),
                    output_path="",
                    z_scan_config=stack_config,
                )
            with self.assertRaisesRegex(ValueError, "preflight flag"):
                controller.start_z_stack_acquisition(
                    SimTaskConfig(),
                    output_path=Path("bad-flags.tif"),
                    z_scan_config=stack_config,
                    apply_camera_config=False,
                )
        finally:
            controller.shutdown()

        injected_writer.shutdown.assert_called_once()

    def test_worker_z_stack_preflights_actual_roi_uses_only_formal_ro_and_dedicated_result(self):
        from sim_control.acquisition_core import ZStackAcquisitionResult
        from sim_control.controller import SimAcquisitionWorker
        from sim_control.models import CameraConfig, DaqLineConfig, PatternPreparationResult, SimTaskConfig, ZScanConfig
        from sim_control.z_stack_io import SeriesWriteResult
        import sim_control.controller as controller_module

        worker = SimAcquisitionWorker()
        ready = []
        summaries = []
        results = []
        progress = []
        worker.signal_acquisition_ready.connect(ready.append)
        worker.signal_acquisition_summary_ready.connect(summaries.append)
        worker.signal_z_stack_result_ready.connect(results.append)
        worker.signal_z_stack_progress.connect(progress.append)
        task = SimTaskConfig(camera=CameraConfig(roi_width=8, roi_height=8, exposure_us=10_000))
        camera = mock.Mock()
        camera.apply_config.return_value = {
            "applied_roi": {"x": 0, "y": 0, "width": 6, "height": 4}
        }
        daq = mock.Mock()
        slm = self._formal_slm()
        stage = self._stage()
        writer = mock.Mock()
        writer.finalize.return_value = SeriesWriteResult(
            job_id="stack-task",
            kind="zstack",
            outcome="complete",
            completed_layers=3,
            total_layers=3,
            output_paths=(Path("stack.tif"),),
            partial=False,
            bigtiff=False,
        )
        builder = mock.Mock()
        builder.build.return_value = SimpleNamespace(metadata={})

        def run_stack(**kwargs):
            self.assertTrue(writer.begin_job.called)
            self.assertTrue(builder.build.called)
            self.assertEqual(stage.move_z_um.call_count, 0)
            self.assertIs(kwargs["layer_sink"], writer)
            kwargs["on_status"](
                "z_stack_layer_accepted",
                {
                    "parent_task_id": "stack-task",
                    "plane_index": 0,
                    "total_layers": 3,
                    "requested_z_um": 10.0,
                    "measured_z_um": 10.1,
                    "layer_shape": [9, 4, 6],
                },
            )
            return ZStackAcquisitionResult(
                task_id="stack-task",
                requested_z_um=(10.0, 10.5, 11.0),
                measured_z_um=(10.1, 10.6, 11.1),
                completed_layers=3,
                total_layers=3,
                layer_shape=(9, 4, 6),
                status="complete",
            )

        with mock.patch.object(controller_module, "run_z_stack_acquisition", side_effect=run_stack):
            worker.slot_start(
                {
                    "task": task,
                    "task_id": "stack-task",
                    "daq_config": DaqLineConfig(),
                    "pattern_result": PatternPreparationResult(),
                    "waveform_builder": builder,
                    "daq_adapter": daq,
                    "camera_adapter": camera,
                    "slm_adapter": slm,
                    "stage_adapter": stage,
                    "z_scan_config": ZScanConfig(
                        enabled=True,
                        select_focus_plane=False,
                        start_um=None,
                        step_um=0.5,
                        num_steps=2,
                    ),
                    "z_scan_enabled": True,
                    "stop_event": threading.Event(),
                    "prepare_running_order": True,
                    "initialize_hardware": True,
                    "apply_daq_config": True,
                    "apply_camera_config": True,
                    "acquisition_mode": "z_stack",
                    "z_stack_output_path": Path("stack.tif"),
                    "series_writer": writer,
                }
            )

        self.assertEqual(slm.select_running_order.call_args_list, [mock.call(3)])
        spec = writer.begin_job.call_args.args[0]
        self.assertEqual(spec.kind, "zstack")
        self.assertEqual(spec.frame_shape, (4, 6))
        self.assertEqual(spec.total_layers, 3)
        self.assertEqual(spec.running_order, "488_3.5_2d_10ms")
        writer.finalize.assert_called_once_with("complete", message="")
        self.assertEqual(ready, [])
        self.assertEqual(summaries, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].output_paths, ("stack.tif",))
        self.assertEqual(results[0].measured_z_um, (10.1, 10.6, 11.1))
        self.assertEqual(progress[0]["plane_index"], 0)
        camera.disarm.assert_called()
        daq.set_all_low.assert_called()

    def test_worker_z_stack_range_preflight_fails_before_move_capture_or_writer_begin(self):
        from sim_control.controller import SimAcquisitionWorker
        from sim_control.models import DaqLineConfig, PatternPreparationResult, SimTaskConfig, ZScanConfig
        import sim_control.controller as controller_module

        worker = SimAcquisitionWorker()
        results = []
        failures = []
        worker.signal_z_stack_result_ready.connect(results.append)
        worker.signal_acquisition_failed.connect(lambda task_id, message: failures.append(message))
        stage = self._stage()
        stage.get_position_um.return_value = 0.0
        stage.get_z_ranges_um.return_value = (0.0, 0.5)
        camera = mock.Mock()
        camera.apply_config.return_value = {}
        daq = mock.Mock()
        writer = mock.Mock()
        builder = mock.Mock(build=mock.Mock(return_value=SimpleNamespace(metadata={})))

        with mock.patch.object(controller_module, "run_z_stack_acquisition") as run_stack:
            worker.slot_start(
                {
                    "task": SimTaskConfig(),
                    "task_id": "stack-preflight",
                    "daq_config": DaqLineConfig(),
                    "pattern_result": PatternPreparationResult(),
                    "waveform_builder": builder,
                    "daq_adapter": daq,
                    "camera_adapter": camera,
                    "slm_adapter": self._formal_slm(),
                    "stage_adapter": stage,
                    "z_scan_config": ZScanConfig(
                        enabled=True,
                        select_focus_plane=False,
                        step_um=1.0,
                        num_steps=1,
                    ),
                    "z_scan_enabled": True,
                    "prepare_running_order": True,
                    "acquisition_mode": "z_stack",
                    "z_stack_output_path": Path("stack.tif"),
                    "series_writer": writer,
                }
            )

        run_stack.assert_not_called()
        writer.begin_job.assert_not_called()
        stage.move_z_um.assert_not_called()
        camera.arm.assert_not_called()
        daq.play_waveform.assert_not_called()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "failed")
        self.assertEqual(results[0].requested_z_um, (0.0, 1.0))
        self.assertEqual(len(failures), 1)

    def test_worker_z_stack_writer_terminal_failure_never_reports_success(self):
        from sim_control.acquisition_core import ZStackAcquisitionResult
        from sim_control.controller import SimAcquisitionWorker
        from sim_control.models import DaqLineConfig, PatternPreparationResult, SimTaskConfig, ZScanConfig
        import sim_control.controller as controller_module

        worker = SimAcquisitionWorker()
        events = []
        worker.signal_z_stack_result_ready.connect(lambda result: events.append(("result", result)))
        worker.signal_acquisition_failed.connect(lambda task_id, message: events.append(("failed", message)))
        writer = mock.Mock()
        writer.finalize.side_effect = RuntimeError("writer assembly failed")
        camera = mock.Mock()
        camera.apply_config.return_value = {}
        core_result = ZStackAcquisitionResult(
            task_id="stack-writer-fail",
            requested_z_um=(10.0, 10.5),
            measured_z_um=(10.0, 10.5),
            completed_layers=2,
            total_layers=2,
            layer_shape=(9, 4, 4),
            status="complete",
        )

        with mock.patch.object(controller_module, "run_z_stack_acquisition", return_value=core_result):
            worker.slot_start(
                {
                    "task": SimTaskConfig(),
                    "task_id": "stack-writer-fail",
                    "daq_config": DaqLineConfig(),
                    "pattern_result": PatternPreparationResult(),
                    "waveform_builder": mock.Mock(),
                    "daq_adapter": mock.Mock(),
                    "camera_adapter": camera,
                    "slm_adapter": self._formal_slm(),
                    "stage_adapter": self._stage(),
                    "z_scan_config": ZScanConfig(
                        enabled=True,
                        select_focus_plane=False,
                        step_um=0.5,
                        num_steps=1,
                    ),
                    "z_scan_enabled": True,
                    "prepare_running_order": True,
                    "acquisition_mode": "z_stack",
                    "z_stack_output_path": Path("stack.tif"),
                    "series_writer": writer,
                }
            )

        self.assertEqual([kind for kind, _value in events], ["result", "failed"])
        self.assertEqual(events[0][1].status, "failed")
        self.assertIn("writer assembly failed", events[0][1].message)

    def test_worker_z_stack_cancel_emits_committed_partial_before_cancel_signal(self):
        from sim_control.acquisition_core import ZStackAcquisitionCancelled, ZStackAcquisitionResult
        from sim_control.controller import SimAcquisitionWorker
        from sim_control.models import DaqLineConfig, PatternPreparationResult, SimTaskConfig, ZScanConfig
        from sim_control.z_stack_io import SeriesWriteResult
        import sim_control.controller as controller_module

        partial = ZStackAcquisitionResult(
            task_id="stack-cancel",
            requested_z_um=(10.0, 11.0, 12.0),
            measured_z_um=(10.0,),
            completed_layers=1,
            total_layers=3,
            layer_shape=(9, 4, 4),
            status="cancelled",
            message="stop requested",
        )
        writer = mock.Mock()
        writer.finalize.return_value = SeriesWriteResult(
            job_id="stack-cancel",
            kind="zstack",
            outcome="cancelled",
            completed_layers=1,
            total_layers=3,
            output_paths=(Path("stack.partial.tif"),),
            partial=True,
            bigtiff=False,
            message="stop requested",
        )
        worker = SimAcquisitionWorker()
        events = []
        worker.signal_z_stack_result_ready.connect(lambda result: events.append(("result", result)))
        worker.signal_acquisition_cancelled.connect(lambda task_id, message: events.append(("cancel", message)))
        camera = mock.Mock()
        camera.apply_config.return_value = {}

        with mock.patch.object(
            controller_module,
            "run_z_stack_acquisition",
            side_effect=ZStackAcquisitionCancelled("stop requested", partial),
        ):
            worker.slot_start(
                {
                    "task": SimTaskConfig(),
                    "task_id": "stack-cancel",
                    "daq_config": DaqLineConfig(),
                    "pattern_result": PatternPreparationResult(),
                    "waveform_builder": mock.Mock(),
                    "daq_adapter": mock.Mock(),
                    "camera_adapter": camera,
                    "slm_adapter": self._formal_slm(),
                    "stage_adapter": self._stage(),
                    "z_scan_config": ZScanConfig(enabled=True, select_focus_plane=False, num_steps=2),
                    "z_scan_enabled": True,
                    "prepare_running_order": True,
                    "acquisition_mode": "z_stack",
                    "z_stack_output_path": Path("stack.tif"),
                    "series_writer": writer,
                }
            )

        self.assertEqual([kind for kind, _value in events], ["result", "cancel"])
        self.assertEqual(events[0][1].status, "cancelled")
        self.assertEqual(events[0][1].completed_layers, 1)
        self.assertEqual(events[0][1].output_paths, ("stack.partial.tif",))

    def test_worker_z_stack_emits_partial_result_before_failure(self):
        from sim_control.acquisition_core import ZStackAcquisitionFailed, ZStackAcquisitionResult
        from sim_control.controller import SimAcquisitionWorker
        from sim_control.models import DaqLineConfig, PatternPreparationResult, SimTaskConfig, ZScanConfig
        from sim_control.z_stack_io import SeriesWriteResult
        import sim_control.controller as controller_module

        partial = ZStackAcquisitionResult(
            task_id="stack-fail",
            requested_z_um=(10.0, 11.0, 12.0),
            measured_z_um=(10.1,),
            completed_layers=1,
            total_layers=3,
            layer_shape=(9, 4, 4),
            status="failed",
            message="camera failed",
        )
        writer = mock.Mock()
        writer.finalize.return_value = SeriesWriteResult(
            job_id="stack-fail",
            kind="zstack",
            outcome="failed",
            completed_layers=1,
            total_layers=3,
            output_paths=(Path("stack.partial.tif"),),
            partial=True,
            bigtiff=False,
            message="camera failed",
        )
        worker = SimAcquisitionWorker()
        events = []
        worker.signal_z_stack_result_ready.connect(lambda result: events.append(("result", result)))
        worker.signal_acquisition_failed.connect(lambda task_id, message: events.append(("failed", message)))
        camera = mock.Mock()
        camera.apply_config.return_value = {}
        builder = mock.Mock(build=mock.Mock(return_value=SimpleNamespace(metadata={})))

        with mock.patch.object(
            controller_module,
            "run_z_stack_acquisition",
            side_effect=ZStackAcquisitionFailed("camera failed", partial),
        ):
            worker.slot_start(
                {
                    "task": SimTaskConfig(),
                    "task_id": "stack-fail",
                    "daq_config": DaqLineConfig(),
                    "pattern_result": PatternPreparationResult(),
                    "waveform_builder": builder,
                    "daq_adapter": mock.Mock(),
                    "camera_adapter": camera,
                    "slm_adapter": self._formal_slm(),
                    "stage_adapter": self._stage(),
                    "z_scan_config": ZScanConfig(enabled=True, select_focus_plane=False, num_steps=2),
                    "z_scan_enabled": True,
                    "prepare_running_order": True,
                    "apply_camera_config": True,
                    "apply_daq_config": True,
                    "acquisition_mode": "z_stack",
                    "z_stack_output_path": Path("stack.tif"),
                    "series_writer": writer,
                }
            )

        self.assertEqual([kind for kind, _value in events], ["result", "failed"])
        self.assertEqual(events[0][1].completed_layers, 1)
        self.assertEqual(events[0][1].measured_z_um, (10.1,))
        self.assertEqual(events[0][1].output_paths, ("stack.partial.tif",))
        writer.finalize.assert_called_once_with("failed", message="camera failed")

    def test_focus_diagnostics_submit_each_record_and_finalize_without_blocking_normal_ready(self):
        from sim_control.acquisition_core import ZStackAcquisitionResult
        from sim_control.controller import SimAcquisitionWorker
        from sim_control.models import AcquisitionBatch, DaqLineConfig, PatternPreparationResult, SimTaskConfig, ZScanConfig
        from sim_control.z_scan_core import FocusFrameRecord
        from sim_control.z_stack_io import SeriesWriteResult
        import sim_control.controller as controller_module

        del ZStackAcquisitionResult  # Keep this test focused on the normal acquisition route.
        writer = mock.Mock()
        writer.finalize.return_value = SeriesWriteResult(
            job_id="focus-task:focus",
            kind="focus",
            outcome="complete",
            completed_layers=2,
            total_layers=2,
            output_paths=(Path("focus.tif"), Path("focus.csv")),
            partial=False,
            bigtiff=False,
        )
        worker = SimAcquisitionWorker()
        ready = []
        statuses = []
        worker.signal_acquisition_ready.connect(ready.append)
        worker.signal_status_changed.connect(lambda name, data: statuses.append((name, data)))
        task = SimTaskConfig()
        batch = AcquisitionBatch(
            task_id="focus-task",
            stack=np.zeros((9, task.camera.roi_height, task.camera.roi_width), dtype=np.uint16),
            timestamps=[float(index) for index in range(9)],
            laser_wavelength_nm=488,
            exposure_us=task.camera.exposure_us,
            pattern_files=["formal"] * 9,
        )

        def run_single(**kwargs):
            callback = kwargs["on_focus_frame"]
            for index, score in enumerate((5.0, 4.0)):
                callback(
                    FocusFrameRecord(
                        task_id="focus-task",
                        plane_index=index,
                        total_layers=2,
                        requested_z_um=10.0 + index,
                        measured_z_um=10.1 + index,
                        wavelength_nm=488,
                        exposure_actual_us=8_000,
                        timestamp=100.0 + index,
                        score=score,
                        frame=np.zeros((task.camera.roi_height, task.camera.roi_width), dtype=np.uint16),
                    )
                )
            return batch

        camera = mock.Mock()
        camera.apply_config.return_value = {}
        with mock.patch.object(controller_module, "run_single_acquisition", side_effect=run_single):
            worker.slot_start(
                {
                    "task": task,
                    "task_id": "focus-task",
                    "daq_config": DaqLineConfig(),
                    "pattern_result": PatternPreparationResult(),
                    "waveform_builder": mock.Mock(),
                    "daq_adapter": mock.Mock(),
                    "camera_adapter": camera,
                    "slm_adapter": self._formal_slm(),
                    "stage_adapter": self._stage(),
                    "z_scan_config": ZScanConfig(enabled=True, select_focus_plane=True, num_steps=1),
                    "z_scan_enabled": True,
                    "prepare_running_order": True,
                    "focus_output_path": Path("focus.tif"),
                    "series_writer": writer,
                }
            )

        self.assertEqual(len(ready), 1)
        self.assertEqual(writer.submit_focus_layer.call_count, 2)
        first_submit = writer.submit_focus_layer.call_args_list[0].kwargs
        self.assertEqual(first_submit["plane_index"], 0)
        self.assertEqual(first_submit["requested_z_um"], 10.0)
        self.assertEqual(first_submit["measured_z_um"], 10.1)
        self.assertEqual(first_submit["sml_score"], 5.0)
        writer.finalize.assert_called_once_with("complete", best_plane_index=0, message="")
        saved = [data for name, data in statuses if name == "focus_diagnostics_saved"]
        self.assertEqual(saved[0]["output_paths"], ["focus.tif", "focus.csv"])

    def test_focus_diagnostics_failure_warns_but_final_sim9_still_ready(self):
        from sim_control.controller import SimAcquisitionWorker
        from sim_control.models import AcquisitionBatch, DaqLineConfig, PatternPreparationResult, SimTaskConfig, ZScanConfig
        from sim_control.z_scan_core import FocusFrameRecord
        from sim_control.z_stack_io import SeriesWriteResult
        import sim_control.controller as controller_module

        task = SimTaskConfig()
        batch = AcquisitionBatch(
            task_id="focus-soft-fail",
            stack=np.zeros((9, task.camera.roi_height, task.camera.roi_width), dtype=np.uint16),
            timestamps=[float(index) for index in range(9)],
            laser_wavelength_nm=488,
            exposure_us=task.camera.exposure_us,
            pattern_files=["formal"] * 9,
        )

        def run_single(**kwargs):
            callback = kwargs.get("on_focus_frame")
            if callback is not None:
                callback(
                    FocusFrameRecord(
                        task_id="focus-soft-fail",
                        plane_index=0,
                        total_layers=2,
                        requested_z_um=10.0,
                        measured_z_um=10.0,
                        wavelength_nm=488,
                        exposure_actual_us=8_000,
                        timestamp=100.0,
                        score=1.0,
                        frame=np.zeros((task.camera.roi_height, task.camera.roi_width), dtype=np.uint16),
                    )
                )
            return batch

        failure_cases = {
            "begin": ("begin_job", "diagnostic begin failed"),
            "health": ("check_health", "diagnostic health failed"),
            "submit": ("submit_focus_layer", "diagnostic queue failed"),
            "finalize": ("finalize", "diagnostic finalize failed"),
            "partial_publish": ("finalize_result", "focus CSV publish failed"),
        }
        for name, (method_name, failure_message) in failure_cases.items():
            with self.subTest(name=name):
                writer = mock.Mock()
                if method_name == "finalize_result":
                    writer.finalize.return_value = SeriesWriteResult(
                        job_id="focus-soft-fail:focus",
                        kind="focus",
                        outcome="failed",
                        completed_layers=1,
                        total_layers=2,
                        output_paths=(Path("focus.tif"),),
                        partial=True,
                        bigtiff=False,
                        message=failure_message,
                    )
                else:
                    getattr(writer, method_name).side_effect = RuntimeError(failure_message)
                    if method_name in {"check_health", "submit_focus_layer"}:
                        writer.finalize.return_value = SeriesWriteResult(
                            job_id="focus-soft-fail:focus",
                            kind="focus",
                            outcome="failed",
                            completed_layers=0,
                            total_layers=2,
                            output_paths=(),
                            partial=True,
                            bigtiff=False,
                            message=failure_message,
                        )
                worker = SimAcquisitionWorker()
                ready = []
                statuses = []
                worker.signal_acquisition_ready.connect(ready.append)
                worker.signal_status_changed.connect(
                    lambda event, data: statuses.append((event, data))
                )
                camera = mock.Mock()
                camera.apply_config.return_value = {}
                with mock.patch.object(
                    controller_module,
                    "run_single_acquisition",
                    side_effect=run_single,
                ):
                    worker.slot_start(
                        {
                            "task": task,
                            "task_id": "focus-soft-fail",
                            "daq_config": DaqLineConfig(),
                            "pattern_result": PatternPreparationResult(),
                            "waveform_builder": mock.Mock(),
                            "daq_adapter": mock.Mock(),
                            "camera_adapter": camera,
                            "slm_adapter": self._formal_slm(),
                            "stage_adapter": self._stage(),
                            "z_scan_config": ZScanConfig(
                                enabled=True,
                                select_focus_plane=True,
                                num_steps=1,
                            ),
                            "z_scan_enabled": True,
                            "prepare_running_order": True,
                            "focus_output_path": Path("focus.tif"),
                            "focus_csv_path": Path("focus.csv"),
                            "series_writer": writer,
                        }
                    )

                self.assertEqual(len(ready), 1)
                warnings = [
                    data for event, data in statuses
                    if event == "focus_diagnostics_warning"
                ]
                self.assertTrue(
                    any(failure_message in item["message"] for item in warnings),
                    statuses,
                )
                if name == "partial_publish":
                    self.assertTrue(
                        any("focus.tif" in item["message"] for item in warnings),
                        statuses,
                    )
                    self.assertFalse(
                        any(event == "focus_diagnostics_saved" for event, _data in statuses),
                        statuses,
                    )

    def test_focus_diagnostics_submit_failure_reports_retained_partial_and_keeps_final_sim9_ready(self):
        from sim_control.controller import SimAcquisitionWorker
        from sim_control.models import AcquisitionBatch, DaqLineConfig, PatternPreparationResult, SimTaskConfig, ZScanConfig
        from sim_control.z_scan_core import FocusFrameRecord
        from sim_control.z_stack_io import SeriesWriteResult
        import sim_control.controller as controller_module

        task = SimTaskConfig()
        batch = AcquisitionBatch(
            task_id="focus-retained",
            stack=np.zeros((9, task.camera.roi_height, task.camera.roi_width), dtype=np.uint16),
            timestamps=[float(index) for index in range(9)],
            laser_wavelength_nm=488,
            exposure_us=task.camera.exposure_us,
            pattern_files=["formal"] * 9,
        )

        def run_single(**kwargs):
            callback = kwargs["on_focus_frame"]
            for index, score in enumerate((1.0, 99.0)):
                callback(
                    FocusFrameRecord(
                        task_id="focus-retained",
                        plane_index=index,
                        total_layers=2,
                        requested_z_um=10.0 + index,
                        measured_z_um=10.0 + index,
                        wavelength_nm=488,
                        exposure_actual_us=4_884,
                        timestamp=100.0 + index,
                        score=score,
                        frame=np.zeros((task.camera.roi_height, task.camera.roi_width), dtype=np.uint16),
                    )
                )
            return batch

        writer = mock.Mock()
        writer.submit_focus_layer.side_effect = [None, RuntimeError("diagnostic queue failed")]
        writer.finalize.return_value = SeriesWriteResult(
            job_id="focus-retained:focus",
            kind="focus",
            outcome="failed",
            completed_layers=1,
            total_layers=2,
            output_paths=(
                Path("focus.partial-z1of2.tif"),
                Path("focus.partial-z1of2.csv"),
            ),
            partial=True,
            bigtiff=False,
            message="diagnostic queue failed",
        )
        worker = SimAcquisitionWorker()
        ready = []
        statuses = []
        worker.signal_acquisition_ready.connect(ready.append)
        worker.signal_status_changed.connect(lambda event, data: statuses.append((event, data)))
        camera = mock.Mock()
        camera.apply_config.return_value = {}

        with mock.patch.object(controller_module, "run_single_acquisition", side_effect=run_single):
            worker.slot_start(
                {
                    "task": task,
                    "task_id": "focus-retained",
                    "daq_config": DaqLineConfig(),
                    "pattern_result": PatternPreparationResult(),
                    "waveform_builder": mock.Mock(),
                    "daq_adapter": mock.Mock(),
                    "camera_adapter": camera,
                    "slm_adapter": self._formal_slm(),
                    "stage_adapter": self._stage(),
                    "z_scan_config": ZScanConfig(
                        enabled=True,
                        select_focus_plane=True,
                        num_steps=1,
                    ),
                    "z_scan_enabled": True,
                    "prepare_running_order": True,
                    "focus_output_path": Path("focus.tif"),
                    "focus_csv_path": Path("focus.csv"),
                    "series_writer": writer,
                }
            )

        self.assertEqual(len(ready), 1)
        warnings = [data["message"] for event, data in statuses if event == "focus_diagnostics_warning"]
        self.assertTrue(any("diagnostic queue failed" in message for message in warnings), statuses)
        self.assertTrue(any("focus.partial-z1of2.tif" in message for message in warnings), statuses)
        self.assertTrue(any("focus.partial-z1of2.csv" in message for message in warnings), statuses)
        self.assertFalse(any(event == "focus_diagnostics_saved" for event, _data in statuses), statuses)
        writer.finalize.assert_called_once_with("failed", message="")


if __name__ == "__main__":
    unittest.main()

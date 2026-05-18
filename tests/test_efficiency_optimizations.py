"""采集、预览和 pipeline 性能优化的防回归测试。

测试重点不是速度基准，而是锁住关键实现选择：DAQ 播放复用 packed uint32、取消时停止任务、相机帧读取避免多余拷贝、预览/重建/特征计算走低开销路径。
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


class EfficiencyOptimizationTests(unittest.TestCase):
    """验证关键性能优化路径不会退化为多余拷贝或阻塞等待。"""
    def test_ni_daq_play_waveform_reuses_uint32_packed_array(self):
        from sim_control import adapters
        from sim_control.models import DaqLineConfig, TimingConfig
        from sim_control.waveform import NIDaqWaveformBuilder

        plan = NIDaqWaveformBuilder().build(
            daq_config=DaqLineConfig(),
            timing=TimingConfig(sample_rate_hz=100_000, inter_frame_gap_us=1_000),
            laser_wavelength_nm=488,
            exposure_us=1_000,
            frame_count=2,
        )
        captured = {}

        class FakeTask:
            out_stream = object()

            def __enter__(self):
                return self

            def __exit__(self, _exc_type, _exc, _tb):
                return False

            do_channels = SimpleNamespace(add_do_chan=lambda *args, **kwargs: None)
            timing = SimpleNamespace(cfg_samp_clk_timing=lambda *args, **kwargs: None)

            def start(self):
                return None

            def is_task_done(self):
                return True

            def wait_until_done(self, timeout):
                captured["timeout"] = timeout

        class FakeWriter:
            def __init__(self, out_stream, auto_start=False):
                captured["out_stream"] = out_stream
                captured["auto_start"] = auto_start

            def write_many_sample_port_uint32(self, values):
                captured["values"] = values

        with (
            mock.patch.object(adapters, "nidaqmx", SimpleNamespace(Task=FakeTask)),
            mock.patch.object(adapters, "LineGrouping", SimpleNamespace(CHAN_FOR_ALL_LINES=1)),
            mock.patch.object(adapters, "AcquisitionType", SimpleNamespace(FINITE=1)),
            mock.patch.object(adapters, "DigitalSingleChannelWriter", FakeWriter),
        ):
            daq = adapters.NIDaqAdapter()
            daq.play_waveform("Dev1", plan)

        self.assertIs(captured["values"], plan.packed_port_values)
        self.assertEqual(plan.packed_port_values.dtype, np.uint32)

    def test_ni_daq_play_waveform_stops_task_when_stop_event_is_set(self):
        from sim_control import adapters
        from sim_control.models import DaqLineConfig, TimingConfig
        from sim_control.waveform import NIDaqWaveformBuilder

        plan = NIDaqWaveformBuilder().build(
            daq_config=DaqLineConfig(),
            timing=TimingConfig(sample_rate_hz=100_000, inter_frame_gap_us=1_000),
            laser_wavelength_nm=488,
            exposure_us=1_000,
            frame_count=2,
        )
        captured = {"is_done_calls": 0, "stop_calls": 0}

        class FakeTask:
            out_stream = object()
            do_channels = SimpleNamespace(add_do_chan=lambda *args, **kwargs: None)
            timing = SimpleNamespace(cfg_samp_clk_timing=lambda *args, **kwargs: None)

            def __enter__(self):
                return self

            def __exit__(self, _exc_type, _exc, _tb):
                return False

            def start(self):
                captured["started"] = True

            def is_task_done(self):
                captured["is_done_calls"] += 1
                return False

            def stop(self):
                captured["stop_calls"] += 1

        class FakeWriter:
            def __init__(self, out_stream, auto_start=False):
                return None

            def write_many_sample_port_uint32(self, values):
                return None

        stop_event = threading.Event()
        stop_event.set()

        with (
            mock.patch.object(adapters, "nidaqmx", SimpleNamespace(Task=FakeTask)),
            mock.patch.object(adapters, "LineGrouping", SimpleNamespace(CHAN_FOR_ALL_LINES=1)),
            mock.patch.object(adapters, "AcquisitionType", SimpleNamespace(FINITE=1)),
            mock.patch.object(adapters, "DigitalSingleChannelWriter", FakeWriter),
        ):
            daq = adapters.NIDaqAdapter()
            daq.play_waveform("Dev1", plan, stop_event=stop_event)

        self.assertTrue(captured["started"])
        self.assertEqual(captured["stop_calls"], 1)

    def test_waveform_pack_uses_boolean_masks_without_uint32_role_copies(self):
        from sim_control.models import DAQ_ROLE_ORDER, DaqLineConfig
        from sim_control.waveform import NIDaqWaveformBuilder

        class RoleArray:
            def __init__(self, values):
                self.values = np.asarray(values, dtype=np.uint8)

            def __ne__(self, other):
                return self.values != other

            def astype(self, *_args, **_kwargs):
                raise AssertionError("extra uint32 role copy")

        sample_count = 5
        matrix = {
            role: RoleArray(np.zeros(sample_count, dtype=np.uint8))
            for role in DAQ_ROLE_ORDER
        }
        matrix["slm_enable_line"] = RoleArray([1, 1, 1, 1, 1])
        matrix["slm_trigger_line"] = RoleArray([0, 1, 0, 0, 0])
        matrix["camera_trigger_line"] = RoleArray([0, 1, 1, 0, 0])
        matrix["laser_488_line"] = RoleArray([0, 0, 1, 1, 0])

        packed = NIDaqWaveformBuilder._pack_port_values(DaqLineConfig(), matrix, sample_count)

        expected = np.array(
            [
                1 << 0,
                (1 << 0) | (1 << 1) | (1 << 5),
                (1 << 0) | (1 << 5) | (1 << 6),
                (1 << 0) | (1 << 6),
                1 << 0,
            ],
            dtype=np.uint32,
        )
        np.testing.assert_array_equal(packed, expected)

    def test_real_camera_sequence_assigns_frame_views_without_intermediate_array_copy(self):
        from sim_control import adapters
        from sim_control.models import CameraConfig

        class FakeDcamCamera:
            def cap_transferinfo(self):
                return SimpleNamespace(nFrameCount=2)

            def buf_getframedata(self, index):
                return np.full((2, 3), index + 1, dtype=np.uint16)

            def lasterr(self):
                return SimpleNamespace(name="OK")

        camera = adapters.FusionBtCameraAdapter.__new__(adapters.FusionBtCameraAdapter)
        camera._armed = True
        camera._camera_config = CameraConfig(roi_width=3, roi_height=2, exposure_us=1000, timeout_ms=100)
        camera._dcam_camera = FakeDcamCamera()

        with mock.patch.object(adapters.np, "array", side_effect=AssertionError("extra np.array copy")):
            stack, timestamps = camera.read_frame_sequence(
                frame_count=2,
                pattern_files=[""] * 2,
                laser_wavelength_nm=488,
            )

        self.assertEqual(stack.shape, (2, 2, 3))
        self.assertEqual(stack.dtype, np.uint16)
        self.assertEqual(len(timestamps), 2)
        self.assertTrue(np.all(stack[0] == 1))
        self.assertTrue(np.all(stack[1] == 2))

    def test_real_camera_sequence_wait_loop_responds_to_stop_event(self):
        from sim_control import adapters
        from sim_control.models import CameraConfig

        class FakeDcamCamera:
            def __init__(self):
                self.wait_timeouts = []
                self.stop_event = None

            def cap_transferinfo(self):
                return SimpleNamespace(nFrameCount=0)

            def wait_capevent_frameready(self, timeout_ms):
                self.wait_timeouts.append(timeout_ms)
                self.stop_event.set()
                return False

            def lasterr(self):
                return SimpleNamespace(name="TIMEOUT")

        fake_dcam = FakeDcamCamera()
        stop_event = threading.Event()
        fake_dcam.stop_event = stop_event
        camera = adapters.FusionBtCameraAdapter.__new__(adapters.FusionBtCameraAdapter)
        camera._armed = True
        camera._camera_config = CameraConfig(roi_width=3, roi_height=2, exposure_us=500_000, timeout_ms=5_000)
        camera._dcam_camera = fake_dcam

        with self.assertRaisesRegex(adapters.HardwareError, "cancelled"):
            camera.read_frame_sequence(
                frame_count=9,
                pattern_files=[""] * 9,
                laser_wavelength_nm=488,
                stop_event=stop_event,
            )

        self.assertTrue(fake_dcam.wait_timeouts)
        self.assertLessEqual(max(fake_dcam.wait_timeouts), 50)

    def test_real_camera_sequence_raises_non_timeout_wait_error_immediately(self):
        from sim_control import adapters
        from sim_control.models import CameraConfig

        class FakeDcamCamera:
            def __init__(self):
                self.wait_calls = 0

            def cap_transferinfo(self):
                return SimpleNamespace(nFrameCount=0)

            def wait_capevent_frameready(self, timeout_ms):
                self.wait_calls += 1
                return False

            def lasterr(self):
                return SimpleNamespace(name="FAILURE")

        fake_dcam = FakeDcamCamera()
        camera = adapters.FusionBtCameraAdapter.__new__(adapters.FusionBtCameraAdapter)
        camera._armed = True
        camera._camera_config = CameraConfig(roi_width=3, roi_height=2, exposure_us=500_000, timeout_ms=5_000)
        camera._dcam_camera = fake_dcam

        with self.assertRaisesRegex(adapters.HardwareError, "FAILURE"):
            camera.read_frame_sequence(
                frame_count=9,
                pattern_files=[""] * 9,
                laser_wavelength_nm=488,
            )

        self.assertEqual(fake_dcam.wait_calls, 1)

    def test_real_camera_preview_reuses_dcam_numpy_frame_without_extra_array_copy(self):
        from sim_control import adapters

        class FakeDcamCamera:
            def wait_capevent_frameready(self, timeout_ms):
                return True

            def buf_getlastframedata(self):
                return np.full((2, 3), 7, dtype=np.uint16)

            def lasterr(self):
                return SimpleNamespace(name="OK")

        camera = adapters.FusionBtCameraAdapter.__new__(adapters.FusionBtCameraAdapter)
        camera._preview_active = True
        camera._preview_frame_counter = 0
        camera._dcam_camera = FakeDcamCamera()

        with mock.patch.object(adapters.np, "array", side_effect=AssertionError("extra np.array copy")):
            frame = camera.read_preview_frame(timeout_ms=10)

        self.assertEqual(frame.shape, (2, 3))
        self.assertEqual(frame.dtype, np.uint16)
        self.assertEqual(camera._preview_frame_counter, 1)
        self.assertTrue(np.all(frame == 7))

    def test_simulated_camera_sequence_does_not_stack_frame_list(self):
        from sim_control.models import CameraConfig
        from sim_control.sim_adapters import SimulatedCameraAdapter
        import sim_control.sim_adapters as sim_adapters

        camera = SimulatedCameraAdapter()
        camera.apply_config(CameraConfig(roi_width=4, roi_height=3, bit_depth=8))
        camera.arm(frame_count=3)
        captured_frames = []

        with mock.patch.object(sim_adapters.np, "stack", side_effect=AssertionError("extra np.stack")):
            stack, timestamps = camera.read_frame_sequence(
                frame_count=3,
                pattern_files=[""] * 3,
                laser_wavelength_nm=488,
                frame_callback=lambda frame_index, _timestamp: captured_frames.append(frame_index),
            )

        self.assertEqual(stack.shape, (3, 3, 4))
        self.assertEqual(stack.dtype, np.uint16)
        self.assertEqual(len(timestamps), 3)
        self.assertEqual(captured_frames, [1, 2, 3])

    def test_placeholder_reconstruction_uses_integer_reduce_not_float_mean(self):
        from sim_control.models import AcquisitionBatch
        from sim_control.pipeline import ReconstructionWorker
        import sim_control.pipeline as pipeline

        stack = np.arange(9 * 2 * 3, dtype=np.uint16).reshape(9, 2, 3)
        ready = []
        failed = []
        worker = ReconstructionWorker()
        worker.signal_reconstruction_ready.connect(lambda result: ready.append(result))
        worker.signal_reconstruction_failed.connect(lambda task_id, message: failed.append((task_id, message)))

        with mock.patch.object(pipeline.np, "mean", side_effect=AssertionError("float mean path")):
            worker.slot_reconstruct(
                AcquisitionBatch(
                    task_id="reduce-test",
                    stack=stack,
                    timestamps=[],
                    laser_wavelength_nm=488,
                    exposure_us=1000,
                    pattern_files=[""] * 9,
                )
            )

        expected = (np.add.reduce(stack, axis=0, dtype=np.uint32) // np.uint32(9)).astype(np.uint16)
        self.assertEqual(failed, [])
        self.assertEqual(len(ready), 1)
        self.assertTrue(np.array_equal(ready[0].preview_image, expected))

    def test_feature_worker_uses_opencv_statistics_when_available(self):
        from sim_control.models import ReconstructionResult
        from sim_control.pipeline import FeatureWorker
        import sim_control.pipeline as pipeline

        image = np.array([[0, 1000, 2000], [3000, 4000, 5000]], dtype=np.uint16)
        expected = np.asarray(image, dtype=np.float32)
        expected_mean = float(expected.mean())
        expected_std = float(expected.std())
        ready = []
        failed = []
        worker = FeatureWorker()
        worker.signal_features_ready.connect(lambda result: ready.append(result))
        worker.signal_features_failed.connect(lambda task_id, message: failed.append((task_id, message)))

        with (
            mock.patch.object(pipeline.np, "mean", side_effect=AssertionError("numpy mean fallback")),
            mock.patch.object(pipeline.np, "std", side_effect=AssertionError("numpy std fallback")),
        ):
            worker.slot_extract(ReconstructionResult(task_id="features-opencv", preview_image=image))

        self.assertEqual(failed, [])
        self.assertEqual(len(ready), 1)
        features = ready[0].features
        self.assertAlmostEqual(features["mean_intensity"], expected_mean, places=5)
        self.assertAlmostEqual(features["std_intensity"], expected_std, delta=1e-3)
        self.assertEqual(features["max_intensity"], 5000.0)
        self.assertEqual(features["min_intensity"], 0.0)


if __name__ == "__main__":
    unittest.main()

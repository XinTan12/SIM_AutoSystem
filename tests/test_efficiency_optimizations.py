"""采集、预览与 pipeline 性能优化的防回归测试。

作用：
    本测试集**不**做速度基准；而是用 mock 锁住几条关键实现选择，避免后续 refactor
    把它们退化成多余拷贝或同步阻塞：
        1. ``NIDaqAdapter.play_waveform`` 必须直接复用 ``WaveformPlan.packed_port_values``
           （不能 ``.astype`` 出新数组），且采用 ``is_task_done`` 短轮询。
        2. stop_event 被置位时 ``play_waveform`` 应主动 ``task.stop()``。
        3. ``_pack_port_values`` 使用布尔掩码做 OR，不对每个 role 数组做 uint32 副本。
        4. 真实 Fusion BT 相机的 ``read_frame_sequence`` 与 ``read_preview_frame``
           直接复用 DCAM 返回的 NumPy 帧，不走 ``np.array(...)`` 复制。
        5. 真实相机 wait 循环响应 stop_event，且非 TIMEOUT 错误立即抛错。
        6. 仿真相机的 ``read_frame_sequence`` 不调 ``np.stack`` 把列表 stack 成数组。
        7. 占位重建走 ``np.add.reduce`` + 整除，不退化到 ``np.mean`` 浮点路径。
        8. ``FeatureWorker`` 在 OpenCV 可用时走 ``cv2.meanStdDev`` 而非 ``np.mean``。

协作关系：
    上游：``unittest``、``unittest.mock``、``numpy``。
    下游：``sim_control.adapters``、``sim_control.sim_adapters``、``sim_control.pipeline``、
          ``sim_control.waveform``、``sim_control.models``。

维护要点：
    - 测试通过 ``mock.patch.object(..., side_effect=AssertionError("..."))`` 把
      "不该被调用的函数"换成立刻 raise；如果实现回归会触发 AssertionError。
    - 修改任一被测函数前，请阅读对应用例文档以确认优化意图。
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
    """覆盖采集/预览/pipeline 关键热路径的"不退化"约束。"""

    def test_ni_daq_play_waveform_reuses_uint32_packed_array(self):
        """``play_waveform`` 必须把 ``plan.packed_port_values`` 原对象传给 writer，不复制。"""
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

        # FakeTask + FakeWriter：记录 writer 拿到的 values 引用，便于 ``assertIs`` 验证零拷贝。
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
                # 立即返回 True，避免 ``play_waveform`` 进入等待循环。
                return True

            def wait_until_done(self, timeout):
                captured["timeout"] = timeout

        class FakeWriter:
            def __init__(self, out_stream, auto_start=False):
                captured["out_stream"] = out_stream
                captured["auto_start"] = auto_start

            def write_many_sample_port_uint32(self, values):
                captured["values"] = values

        # 用 4 处 mock.patch 把 nidaqmx / LineGrouping / AcquisitionType / DigitalSingleChannelWriter 都替成假对象。
        with (
            mock.patch.object(adapters, "nidaqmx", SimpleNamespace(Task=FakeTask)),
            mock.patch.object(adapters, "LineGrouping", SimpleNamespace(CHAN_FOR_ALL_LINES=1)),
            mock.patch.object(adapters, "AcquisitionType", SimpleNamespace(FINITE=1)),
            mock.patch.object(adapters, "DigitalSingleChannelWriter", FakeWriter),
        ):
            daq = adapters.NIDaqAdapter()
            daq.play_waveform("Dev1", plan)

        # ``assertIs`` 严格比对对象引用：若实现做了 ``.astype`` 副本则会失败。
        self.assertIs(captured["values"], plan.packed_port_values)
        self.assertEqual(plan.packed_port_values.dtype, np.uint32)

    def test_ni_daq_play_waveform_stops_task_when_stop_event_is_set(self):
        """stop_event 已 set 时 ``play_waveform`` 必须主动调 ``task.stop()``。"""
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

        # FakeTask：is_task_done 永远 False，让 play_waveform 进入等待循环。
        # 测试期望它检测到 stop_event 后立即 stop()。
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

        # stop_event 在调用前就 set；play_waveform 应在循环首次检查时就 stop+return。
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

        # ``start()`` 应被调（先启动后才能 stop）；``stop()`` 必须恰好一次。
        self.assertTrue(captured["started"])
        self.assertEqual(captured["stop_calls"], 1)

    def test_waveform_pack_uses_boolean_masks_without_uint32_role_copies(self):
        """``_pack_port_values`` 用布尔比较做掩码，不能对 role 数组做 uint32 副本。"""
        from sim_control.models import DAQ_ROLE_ORDER, DaqLineConfig
        from sim_control.waveform import NIDaqWaveformBuilder

        # RoleArray：``astype`` 内置 AssertionError，让"对 role 做 dtype 转换"立即失败。
        class RoleArray:
            def __init__(self, values):
                self.values = np.asarray(values, dtype=np.uint8)

            def __ne__(self, other):
                # 委托给 numpy 的 != 返回布尔掩码；``packed[mask] |= bit`` 不需要类型转换。
                return self.values != other

            def astype(self, *_args, **_kwargs):
                raise AssertionError("extra uint32 role copy")

        sample_count = 5
        # 构造每个角色一个 RoleArray；其中 4 个角色填非零值以覆盖位掩码合成的多个位。
        matrix = {
            role: RoleArray(np.zeros(sample_count, dtype=np.uint8))
            for role in DAQ_ROLE_ORDER
        }
        matrix["slm_enable_line"] = RoleArray([1, 1, 1, 1, 1])
        matrix["slm_trigger_line"] = RoleArray([0, 1, 0, 0, 0])
        matrix["camera_trigger_line"] = RoleArray([0, 1, 1, 0, 0])
        matrix["laser_488_line"] = RoleArray([0, 0, 1, 1, 0])

        packed = NIDaqWaveformBuilder._pack_port_values(DaqLineConfig(), matrix, sample_count)

        # 预期 packed[i] = 各角色按 line_index 位掩码 OR 后的结果。
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
        """``read_frame_sequence`` 应把 DCAM 返回的 ndarray 直接赋值到 stack 行，不走 ``np.array`` 复制。"""
        from sim_control import adapters
        from sim_control.models import CameraConfig

        # FakeDcamCamera：transferinfo 一次性报告 2 帧已就绪，buf_getframedata 返回常量帧。
        class FakeDcamCamera:
            def cap_transferinfo(self):
                return SimpleNamespace(nFrameCount=2)

            def buf_getframedata(self, index):
                return np.full((2, 3), index + 1, dtype=np.uint16)

            def lasterr(self):
                return SimpleNamespace(name="OK")

        # 用 ``__new__`` 绕过 ``__init__``，手动注入测试所需字段，避免触发 DCAM 真实初始化。
        camera = adapters.FusionBtCameraAdapter.__new__(adapters.FusionBtCameraAdapter)
        camera._armed = True
        camera._camera_config = CameraConfig(roi_width=3, roi_height=2, exposure_us=1000, timeout_ms=100)
        camera._dcam_camera = FakeDcamCamera()

        # ``np.array`` 被替换为立即 raise，确保任何额外复制都会让本测试失败。
        with mock.patch.object(adapters.np, "array", side_effect=AssertionError("extra np.array copy")):
            stack, timestamps = camera.read_frame_sequence(
                frame_count=2,
                pattern_files=[""] * 2,
                laser_wavelength_nm=488,
            )

        # 验证最终 stack 内容正确，且时间戳与帧数一致。
        self.assertEqual(stack.shape, (2, 2, 3))
        self.assertEqual(stack.dtype, np.uint16)
        self.assertEqual(len(timestamps), 2)
        self.assertTrue(np.all(stack[0] == 1))
        self.assertTrue(np.all(stack[1] == 2))

    def test_real_camera_sequence_wait_loop_responds_to_stop_event(self):
        """wait 循环必须响应 stop_event 取消（TIMEOUT + stop_event=True → 抛 cancelled）。"""
        from sim_control import adapters
        from sim_control.models import CameraConfig

        # FakeDcamCamera.wait_capevent_frameready 在被调时 set stop_event，制造"等待中取消"。
        class FakeDcamCamera:
            def __init__(self):
                self.wait_timeouts = []
                self.stop_event = None

            def cap_transferinfo(self):
                return SimpleNamespace(nFrameCount=0)

            def wait_capevent_frameready(self, timeout_ms):
                self.wait_timeouts.append(timeout_ms)
                self.stop_event.set()
                # SDK 返回 False → 表示 TIMEOUT；测试期望 read_frame_sequence 检测 stop_event 后抛 cancelled。
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

        # 验证 wait_capevent_frameready 至少被调过一次，且每次 timeout 不超过 50 ms（分片等待）。
        self.assertTrue(fake_dcam.wait_timeouts)
        self.assertLessEqual(max(fake_dcam.wait_timeouts), 50)

    def test_real_camera_sequence_raises_non_timeout_wait_error_immediately(self):
        """非 TIMEOUT 错误应立即抛出 HardwareError，不进入"重试"分支。"""
        from sim_control import adapters
        from sim_control.models import CameraConfig

        # FakeDcamCamera.lasterr().name 返回"FAILURE"；wait_calls 用于检查只被调一次。
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

        # wait 只被调一次：非 TIMEOUT 错误不重试。
        self.assertEqual(fake_dcam.wait_calls, 1)

    def test_real_camera_preview_reuses_dcam_numpy_frame_without_extra_array_copy(self):
        """``read_preview_frame`` 不应走 ``np.array(...)`` 复制 SDK 返回的帧。"""
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

        # 同样用 mock.patch 把 np.array 替成 raise，让额外复制立即失败。
        with mock.patch.object(adapters.np, "array", side_effect=AssertionError("extra np.array copy")):
            frame = camera.read_preview_frame(timeout_ms=10)

        # 帧值/dtype/计数器一致；后续 LUT 转换由上层完成。
        self.assertEqual(frame.shape, (2, 3))
        self.assertEqual(frame.dtype, np.uint16)
        self.assertEqual(camera._preview_frame_counter, 1)
        self.assertTrue(np.all(frame == 7))

    def test_simulated_camera_sequence_does_not_stack_frame_list(self):
        """仿真相机 ``read_frame_sequence`` 应预分配 stack，不调 ``np.stack``。"""
        from sim_control.models import CameraConfig
        from sim_control.sim_adapters import SimulatedCameraAdapter
        import sim_control.sim_adapters as sim_adapters

        camera = SimulatedCameraAdapter()
        camera.apply_config(CameraConfig(roi_width=4, roi_height=3, bit_depth=8))
        camera.arm(frame_count=3)
        captured_frames = []

        # 将 ``np.stack`` 替成 raise；若实现意外走 stack 路径会立即失败。
        with mock.patch.object(sim_adapters.np, "stack", side_effect=AssertionError("extra np.stack")):
            stack, timestamps = camera.read_frame_sequence(
                frame_count=3,
                pattern_files=[""] * 3,
                laser_wavelength_nm=488,
                frame_callback=lambda frame_index, _timestamp: captured_frames.append(frame_index),
            )

        # 形状 / dtype / 时间戳数 / 帧回调顺序四类断言。
        self.assertEqual(stack.shape, (3, 3, 4))
        self.assertEqual(stack.dtype, np.uint16)
        self.assertEqual(len(timestamps), 3)
        self.assertEqual(captured_frames, [1, 2, 3])

    def test_placeholder_reconstruction_uses_integer_reduce_not_float_mean(self):
        """占位重建走 ``np.add.reduce`` + 整除，不走 ``np.mean`` 浮点路径。"""
        from sim_control.models import AcquisitionBatch
        from sim_control.pipeline import ReconstructionWorker
        import sim_control.pipeline as pipeline

        # 9 帧线性递增 uint16 stack；用 add.reduce 期望值与 np.mean 浮点结果不同（前者整除）。
        stack = np.arange(9 * 2 * 3, dtype=np.uint16).reshape(9, 2, 3)
        ready = []
        failed = []
        worker = ReconstructionWorker()
        worker.signal_reconstruction_ready.connect(lambda result: ready.append(result))
        worker.signal_reconstruction_failed.connect(lambda task_id, message: failed.append((task_id, message)))

        # 把 ``np.mean`` 替成 raise；若实现回退到 mean 会立即失败。
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

        # 预期 = 整除版本的 reduce 平均；与 ``np.mean`` 浮点路径不同。
        expected = (np.add.reduce(stack, axis=0, dtype=np.uint32) // np.uint32(9)).astype(np.uint16)
        self.assertEqual(failed, [])
        self.assertEqual(len(ready), 1)
        self.assertTrue(np.array_equal(ready[0].preview_image, expected))

    def test_reconstruction_worker_calls_gpu_wiener_when_enabled(self):
        """启用重建配置时，worker 应调用 GPU Wiener 包装接口而不是占位均值。"""
        from sim_control.models import AcquisitionBatch, ReconstructionConfig
        from sim_control.pipeline import ReconstructionWorker

        stack = np.arange(9 * 2 * 3, dtype=np.uint16).reshape(9, 2, 3)
        ready = []
        failed = []
        worker = ReconstructionWorker(
            ReconstructionConfig(
                enabled=True,
                device="cpu",
                otf_488_path=__file__,
                output_path="",
            )
        )
        worker.signal_reconstruction_ready.connect(lambda result: ready.append(result))
        worker.signal_reconstruction_failed.connect(lambda task_id, message: failed.append((task_id, message)))

        with mock.patch(
            "reconstruction.sim_wiener.reconstruct_sim9_stack",
            return_value={
                "reconstruction": np.ones((1, 4, 6), dtype=np.float32),
                "metadata": {
                    "algorithm": "sim_wiener_gpu",
                    "c6": [0.2, 0.3, 0.4],
                    "angle6": [0.1, 0.2, 0.3],
                    "R2_angles": [0.9, 0.8, 0.7],
                    "timings_ms": {"total_pipeline_ms": 1.0},
                },
            },
        ) as reconstruct_mock:
            worker.slot_reconstruct(
                AcquisitionBatch(
                    task_id="gpu-test",
                    stack=stack,
                    timestamps=[],
                    laser_wavelength_nm=488,
                    exposure_us=1000,
                    pattern_files=[""] * 9,
                )
            )

        self.assertEqual(failed, [])
        self.assertEqual(len(ready), 1)
        reconstruct_mock.assert_called_once()
        self.assertEqual(ready[0].preview_image.dtype, np.float32)
        self.assertEqual(ready[0].preview_image.shape, (4, 6))
        self.assertFalse(ready[0].metadata["placeholder"])
        self.assertEqual(ready[0].metadata["algorithm"], "sim_wiener_gpu")

    def test_reconstruction_worker_saves_float32_tiff_in_output_directory(self):
        """配置 output_path 目录时，worker 应按时间戳文件名保存 float32 TIFF。"""
        import tempfile

        import tifffile

        from sim_control.models import AcquisitionBatch, ReconstructionConfig
        from sim_control.pipeline import ReconstructionWorker

        stack = np.arange(9 * 2 * 3, dtype=np.uint16).reshape(9, 2, 3)
        reconstruction = np.arange(1 * 4 * 6, dtype=np.float32).reshape(1, 4, 6)
        ready = []
        failed = []

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "reconstruction"
            worker = ReconstructionWorker(
                ReconstructionConfig(
                    enabled=True,
                    device="cpu",
                    otf_488_path=__file__,
                    output_path=str(output_dir),
                )
            )
            worker.signal_reconstruction_ready.connect(lambda result: ready.append(result))
            worker.signal_reconstruction_failed.connect(lambda task_id, message: failed.append((task_id, message)))

            with mock.patch(
                "reconstruction.sim_wiener.reconstruct_sim9_stack",
                return_value={
                    "reconstruction": reconstruction,
                    "metadata": {"algorithm": "sim_wiener_gpu"},
                },
            ):
                worker.slot_reconstruct(
                    AcquisitionBatch(
                        task_id="gpu-save",
                        stack=stack,
                        timestamps=[],
                        laser_wavelength_nm=488,
                        exposure_us=1000,
                        pattern_files=[""] * 9,
                    )
                )

            saved_paths = sorted(output_dir.glob("sim_reconstruction_*.tif"))
            self.assertEqual(len(saved_paths), 1)
            output_path = saved_paths[0]
            self.assertRegex(output_path.name, r"^sim_reconstruction_\d{8}_\d{6}\.tif$")
            saved = tifffile.imread(output_path)

        self.assertEqual(failed, [])
        self.assertEqual(len(ready), 1)
        self.assertEqual(saved.dtype, np.float32)
        self.assertTrue(np.array_equal(saved, reconstruction))
        self.assertEqual(ready[0].metadata["output_path"], str(output_path))
        self.assertTrue(ready[0].metadata["output_saved"])

    def test_reconstruction_worker_resolves_relative_output_directory_from_project_root(self):
        import os
        import tempfile

        from sim_control.models import AcquisitionBatch, ReconstructionConfig
        from sim_control.pipeline import ReconstructionWorker

        stack = np.ones((9, 2, 3), dtype=np.uint16)
        ready = []
        failed = []
        worker = ReconstructionWorker(
            ReconstructionConfig(
                enabled=True,
                device="cpu",
                otf_488_path=__file__,
                output_path="data/reconstruction",
            )
        )
        worker.signal_reconstruction_ready.connect(lambda result: ready.append(result))
        worker.signal_reconstruction_failed.connect(lambda task_id, message: failed.append((task_id, message)))

        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch(
            "reconstruction.sim_wiener.reconstruct_sim9_stack",
            return_value={
                "reconstruction": np.ones((1, 4, 6), dtype=np.float32),
                "metadata": {"algorithm": "sim_wiener_gpu"},
            },
        ), mock.patch("sim_control.pipeline.tifffile.imwrite") as imwrite_mock:
            try:
                os.chdir(temp_dir)
                worker.slot_reconstruct(
                    AcquisitionBatch(
                        task_id="gpu-relative-output",
                        stack=stack,
                        timestamps=[],
                        laser_wavelength_nm=488,
                        exposure_us=1000,
                        pattern_files=[""] * 9,
                    )
                )
            finally:
                os.chdir(original_cwd)

        self.assertEqual(failed, [])
        self.assertEqual(len(ready), 1)
        saved_path = Path(imwrite_mock.call_args.args[0])
        self.assertEqual(saved_path.parent, PROJECT_ROOT / "data" / "reconstruction")
        self.assertEqual(ready[0].metadata["output_dir"], str(PROJECT_ROOT / "data" / "reconstruction"))

    def test_reconstruction_worker_reports_tiff_save_failure_without_placeholder_fallback(self):
        """重建结果写盘失败时应发失败信号，不回退占位均值图。"""
        import tempfile

        from sim_control.models import AcquisitionBatch, ReconstructionConfig
        from sim_control.pipeline import ReconstructionWorker

        stack = np.ones((9, 2, 3), dtype=np.uint16)
        ready = []
        failed = []

        with tempfile.TemporaryDirectory() as temp_dir:
            worker = ReconstructionWorker(
                ReconstructionConfig(
                    enabled=True,
                    device="cpu",
                    otf_488_path=__file__,
                    output_path=str(Path(temp_dir) / "reconstruction"),
                )
            )
            worker.signal_reconstruction_ready.connect(lambda result: ready.append(result))
            worker.signal_reconstruction_failed.connect(lambda task_id, message: failed.append((task_id, message)))

            with mock.patch(
                "reconstruction.sim_wiener.reconstruct_sim9_stack",
                return_value={
                    "reconstruction": np.ones((1, 4, 6), dtype=np.float32),
                    "metadata": {"algorithm": "sim_wiener_gpu"},
                },
            ), mock.patch(
                "sim_control.pipeline.tifffile.imwrite",
                side_effect=OSError("cannot save reconstruction"),
            ):
                worker.slot_reconstruct(
                    AcquisitionBatch(
                        task_id="gpu-save-fail",
                        stack=stack,
                        timestamps=[],
                        laser_wavelength_nm=488,
                        exposure_us=1000,
                        pattern_files=[""] * 9,
                    )
                )

        self.assertEqual(ready, [])
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0][0], "gpu-save-fail")
        self.assertIn("cannot save reconstruction", failed[0][1])

    def test_reconstruction_worker_reports_gpu_wiener_failure_without_placeholder_fallback(self):
        """GPU Wiener 失败时应发失败信号，不回退到占位均值图。"""
        from sim_control.models import AcquisitionBatch, ReconstructionConfig
        from sim_control.pipeline import ReconstructionWorker

        stack = np.ones((9, 2, 3), dtype=np.uint16)
        ready = []
        failed = []
        worker = ReconstructionWorker(
            ReconstructionConfig(
                enabled=True,
                device="cpu",
                otf_488_path=__file__,
            )
        )
        worker.signal_reconstruction_ready.connect(lambda result: ready.append(result))
        worker.signal_reconstruction_failed.connect(lambda task_id, message: failed.append((task_id, message)))

        with mock.patch(
            "reconstruction.sim_wiener.reconstruct_sim9_stack",
            side_effect=RuntimeError("missing torch"),
        ):
            worker.slot_reconstruct(
                AcquisitionBatch(
                    task_id="gpu-fail",
                    stack=stack,
                    timestamps=[],
                    laser_wavelength_nm=488,
                    exposure_us=1000,
                    pattern_files=[""] * 9,
                )
            )

        self.assertEqual(ready, [])
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0][0], "gpu-fail")
        self.assertIn("missing torch", failed[0][1])

    def test_feature_worker_uses_opencv_statistics_when_available(self):
        """OpenCV 可用时，``FeatureWorker`` 应走 ``cv2.meanStdDev``，不走 numpy 回退。"""
        from sim_control.models import ReconstructionResult
        from sim_control.pipeline import FeatureWorker
        import sim_control.pipeline as pipeline

        image = np.array([[0, 1000, 2000], [3000, 4000, 5000]], dtype=np.uint16)
        # 期望值与 OpenCV 路径输出一致；如果走 numpy 回退也能得到相同数值，但调用路径不同。
        expected = np.asarray(image, dtype=np.float32)
        expected_mean = float(expected.mean())
        expected_std = float(expected.std())
        ready = []
        failed = []
        worker = FeatureWorker()
        worker.signal_features_ready.connect(lambda result: ready.append(result))
        worker.signal_features_failed.connect(lambda task_id, message: failed.append((task_id, message)))

        # 替换 numpy mean/std 为 raise；若回退到 numpy 路径会触发 AssertionError。
        with (
            mock.patch.object(pipeline.np, "mean", side_effect=AssertionError("numpy mean fallback")),
            mock.patch.object(pipeline.np, "std", side_effect=AssertionError("numpy std fallback")),
        ):
            worker.slot_extract(ReconstructionResult(task_id="features-opencv", preview_image=image))

        # 4 项强度特征与期望浮点值一致（容忍 1e-3 浮点误差）。
        self.assertEqual(failed, [])
        self.assertEqual(len(ready), 1)
        features = ready[0].features
        self.assertAlmostEqual(features["mean_intensity"], expected_mean, places=5)
        self.assertAlmostEqual(features["std_intensity"], expected_std, delta=1e-3)
        self.assertEqual(features["max_intensity"], 5000.0)
        self.assertEqual(features["min_intensity"], 0.0)


if __name__ == "__main__":
    unittest.main()

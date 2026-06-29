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

    @classmethod
    def setUpClass(cls):
        # 本类创建 FeatureWorker 等 QObject；与套件内其它 Qt 测试类一致地确保存在 QApplication。
        # 否则全量 ``discover`` 运行时（导入了大量含 Qt 的测试模块），本类无父 QObject 的 C++
        # 对象会在跨测试 QObject GC 中被提前删除，导致 ``slot_extract`` emit 抛
        # “wrapped C/C++ object of type FeatureWorker has been deleted”（单独运行本文件不复现）。
        from PyQt5 import QtWidgets

        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_raw_stack_save_worker_writes_uint16_tiff_stack(self):
        from tempfile import TemporaryDirectory

        import tifffile

        from sim_control.models import AcquisitionBatch
        from sim_control.pipeline import RawStackSaveWorker

        stack = np.arange(9 * 4 * 5, dtype=np.uint16).reshape(9, 4, 5)
        saved = []
        failed = []

        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "sim_9frames"
            worker = RawStackSaveWorker(output_dir=output_dir)
            worker.signal_stack_saved.connect(lambda task_id, path: saved.append((task_id, path)))
            worker.signal_stack_save_failed.connect(lambda task_id, message: failed.append((task_id, message)))

            worker.slot_save(
                AcquisitionBatch(
                    task_id="raw task:1",
                    stack=stack,
                    timestamps=[float(index) for index in range(9)],
                    laser_wavelength_nm=488,
                    exposure_us=10_000,
                    pattern_files=["488_3.5_2d_10ms"] * 9,
                    metadata={"running_order_name": "488_3.5_2d_10ms"},
                )
            )

            self.assertEqual(failed, [])
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0][0], "raw task:1")
            saved_path = Path(saved[0][1])
            # 落盘改为按当天日期(%Y%m%d)归档：文件落在 output_dir/<YYYYMMDD>/ 子目录下。
            self.assertEqual(saved_path.parent.parent, output_dir)
            self.assertRegex(saved_path.parent.name, r"^\d{8}$")
            # 新命名规则：采集波长_直径_曝光_图像大小(WxH)_时间戳(YYYYMMDDHHMMSS).tif
            self.assertRegex(saved_path.name, r"^488_3\.5_10ms_5x4_\d{14}\.tif$")
            self.assertTrue(saved_path.exists())
            loaded = tifffile.imread(saved_path)

        self.assertEqual(loaded.shape, (9, 4, 5))
        self.assertEqual(loaded.dtype, np.uint16)
        np.testing.assert_array_equal(loaded, stack)

    def test_raw_stack_save_worker_rejects_non_sim9_or_non_uint16_stack(self):
        from tempfile import TemporaryDirectory

        from sim_control.models import AcquisitionBatch
        from sim_control.pipeline import RawStackSaveWorker

        failed = []
        saved = []
        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "sim_9frames"
            worker = RawStackSaveWorker(output_dir=output_dir)
            worker.signal_stack_saved.connect(lambda task_id, path: saved.append((task_id, path)))
            worker.signal_stack_save_failed.connect(lambda task_id, message: failed.append((task_id, message)))

            for task_id, stack in (
                ("bad-shape", np.zeros((8, 4, 5), dtype=np.uint16)),
                ("bad-dtype", np.zeros((9, 4, 5), dtype=np.float32)),
            ):
                worker.slot_save(
                    AcquisitionBatch(
                        task_id=task_id,
                        stack=stack,
                        timestamps=[],
                        laser_wavelength_nm=488,
                        exposure_us=10_000,
                        pattern_files=[],
                    )
                )

            written_files = list(output_dir.rglob("*.tif"))
            output_dir_created = output_dir.exists()

        self.assertEqual(saved, [])
        self.assertEqual([task_id for task_id, _message in failed], ["bad-shape", "bad-dtype"])
        self.assertEqual(written_files, [])
        # 坏 stack 在校验阶段提前 return，不应创建任何目录（含按日期归档的空子目录）。
        self.assertFalse(output_dir_created)

    def test_raw_stack_filename_follows_wavelength_pitch_exposure_size_timestamp(self):
        """命名规则护栏：采集波长_直径_曝光(ms)_图像大小(WxH)_时间戳(14 位).tif。"""
        from sim_control.models import AcquisitionBatch
        from sim_control.pipeline import _build_raw_stack_filename

        stack = np.zeros((9, 1024, 2048), dtype=np.uint16)
        name = _build_raw_stack_filename(
            AcquisitionBatch(
                task_id="t",
                stack=stack,
                timestamps=[],
                laser_wavelength_nm=561,
                exposure_us=500_000,
                pattern_files=[],
                metadata={"running_order_name": "561_3.5_2d_50ms"},
            ),
            stack,
        )
        # 561_3.5_500ms_2048x1024_<YYYYMMDDHHMMSS>.tif（曝光 us→ms 去尾零、尺寸 WxH）
        self.assertRegex(name, r"^561_3\.5_500ms_2048x1024_\d{14}\.tif$")

    def test_raw_stack_filename_falls_back_to_pattern_files_for_pitch(self):
        """无 running_order_name 时，从 pattern_files[0] 的 basename 解析 pitch；
        目录里的下划线不应干扰（取 basename 后再分段）。"""
        from sim_control.models import AcquisitionBatch
        from sim_control.pipeline import _build_raw_stack_filename

        stack = np.zeros((9, 8, 8), dtype=np.uint16)
        name = _build_raw_stack_filename(
            AcquisitionBatch(
                task_id="t",
                stack=stack,
                timestamps=[],
                laser_wavelength_nm=488,
                exposure_us=10_000,
                pattern_files=["/some_dir/with_under/488_3.5_2d_imm_f1"],
            ),
            stack,
        )
        self.assertRegex(name, r"^488_3\.5_10ms_8x8_\d{14}\.tif$")

    def test_parse_pitch_token_requires_leading_numeric_wavelength(self):
        """pitch 取第 2 段，但首段必须是数字波长，否则回退 NA（杜绝 lus_3.5_x 误判）。"""
        from sim_control.pipeline import _parse_pitch_token

        self.assertEqual(_parse_pitch_token("488_3.5_2d_10ms"), "3.5")
        self.assertEqual(_parse_pitch_token("488_3.5_2d_imm_f1"), "3.5")
        self.assertEqual(_parse_pitch_token("512_4_2d_10ms"), "4")
        for bad in ("lus_3.5_xxx", "_3.5_x", "488", ""):
            self.assertEqual(_parse_pitch_token(bad), "NA", bad)

    def test_unique_output_path_avoids_overwrite(self):
        """同名碰撞兜底：不存在→原名；存在→追加 _2，再存在→_3（不静默覆盖原始数据）。"""
        from tempfile import TemporaryDirectory

        from sim_control.pipeline import _unique_output_path

        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            name = "488_3.5_10ms_5x4_20260625143022.tif"
            self.assertEqual(_unique_output_path(output_dir, name), output_dir / name)
            (output_dir / name).write_bytes(b"x")
            p2 = _unique_output_path(output_dir, name)
            self.assertEqual(p2.name, "488_3.5_10ms_5x4_20260625143022_2.tif")
            p2.write_bytes(b"x")
            p3 = _unique_output_path(output_dir, name)
            self.assertEqual(p3.name, "488_3.5_10ms_5x4_20260625143022_3.tif")

    def test_dated_output_dir_appends_today(self):
        """``_dated_output_dir`` 在 base 下追加 ``%Y%m%d`` 日期子目录；用传入的时间快照，不依赖墙钟。"""
        from datetime import datetime as real_datetime

        from sim_control.pipeline import _dated_output_dir

        base = Path("data") / "sim_9frames"
        when = real_datetime(2026, 6, 29, 15, 40, 23)
        dated = _dated_output_dir(base, when)
        self.assertEqual(dated, base / "20260629")
        self.assertEqual(dated.parent, base)

    def test_raw_stack_save_reuses_existing_date_dir(self):
        """同一天连续两次保存复用同一日期子目录；同秒同名由 ``_unique_output_path`` 加 ``_2`` 兜底、不覆盖。"""
        from datetime import datetime as real_datetime
        from tempfile import TemporaryDirectory

        from sim_control import pipeline
        from sim_control.models import AcquisitionBatch
        from sim_control.pipeline import RawStackSaveWorker

        stack = np.zeros((9, 4, 5), dtype=np.uint16)
        saved = []
        failed = []
        fixed = real_datetime(2026, 6, 29, 15, 40, 23)

        def _make_batch(task_id):
            return AcquisitionBatch(
                task_id=task_id,
                stack=stack,
                timestamps=[],
                laser_wavelength_nm=488,
                exposure_us=10_000,
                pattern_files=[],
                metadata={"running_order_name": "488_3.5_2d_10ms"},
            )

        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "sim_9frames"
            worker = RawStackSaveWorker(output_dir=output_dir)
            worker.signal_stack_saved.connect(lambda task_id, path: saved.append((task_id, path)))
            worker.signal_stack_save_failed.connect(lambda task_id, message: failed.append((task_id, message)))

            # 锁定 datetime.now() 到同一秒：save_time 与文件名时间戳共用 fixed，逼出同名碰撞。
            with mock.patch.object(pipeline, "datetime") as mock_dt:
                mock_dt.now.return_value = fixed
                worker.slot_save(_make_batch("raw-1"))
                worker.slot_save(_make_batch("raw-2"))

            self.assertEqual(failed, [])
            self.assertEqual(len(saved), 2)
            p1 = Path(saved[0][1])
            p2 = Path(saved[1][1])
            date_dir = output_dir / "20260629"
            # 两次都落在同一个当天日期子目录（复用，不重复新建）。
            self.assertEqual(p1.parent, date_dir)
            self.assertEqual(p2.parent, date_dir)
            # 同秒同名：第二个由 _unique_output_path 追加 _2，不覆盖第一个。
            self.assertEqual(p1.name, "488_3.5_10ms_5x4_20260629154023.tif")
            self.assertEqual(p2.name, "488_3.5_10ms_5x4_20260629154023_2.tif")
            self.assertTrue(p1.exists())
            self.assertTrue(p2.exists())

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
                (1 << 0) | (1 << 1) | (1 << 8),
                (1 << 0) | (1 << 8) | (1 << 10),
                (1 << 0) | (1 << 10),
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
        """启用重建配置时，worker 应走常驻热重建器而不是占位均值。"""
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

        with mock.patch("sim_control.sim_reconstruction.WarmSIMReconstructor") as warm_cls:
            warm_cls.return_value.reconstruct.return_value = {
                "reconstruction": np.ones((1, 4, 6), dtype=np.float32),
                "metadata": {
                    "algorithm": "sim_wiener_gpu",
                    "c6": [0.2, 0.3, 0.4],
                    "angle6": [0.1, 0.2, 0.3],
                    "R2_angles": [0.9, 0.8, 0.7],
                    "timings_ms": {"total_pipeline_ms": 1.0},
                },
            }
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
            warm_cls.return_value.reconstruct.assert_called_once()

        self.assertEqual(failed, [])
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0].preview_image.dtype, np.float32)
        self.assertEqual(ready[0].preview_image.shape, (4, 6))
        self.assertFalse(ready[0].metadata["placeholder"])
        self.assertEqual(ready[0].metadata["algorithm"], "sim_wiener_gpu")
        # 未配 output_path -> 不落盘。
        self.assertEqual(ready[0].metadata["output_save_status"], "disabled")

    def test_reconstruction_worker_saves_float32_tiff_in_output_directory(self):
        """配置 output_path 目录时，worker 应按时间戳文件名保存 float32 TIFF。

        新语义：先 emit 结果（metadata 标 output_save_status="requested"），再落盘；落盘
        结果经 signal_reconstruction_saved 通知。这里用 async=False 让落盘同步、可断言。
        """
        import tempfile

        import tifffile

        from sim_control.models import AcquisitionBatch, ReconstructionConfig
        from sim_control.pipeline import ReconstructionWorker

        stack = np.arange(9 * 2 * 3, dtype=np.uint16).reshape(9, 2, 3)
        reconstruction = np.arange(1 * 4 * 6, dtype=np.float32).reshape(1, 4, 6)
        ready = []
        failed = []
        saved_signals = []

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "reconstruction"
            worker = ReconstructionWorker(
                ReconstructionConfig(
                    enabled=True,
                    device="cpu",
                    otf_488_path=__file__,
                    output_path=str(output_dir),
                    async_save_reconstruction_output=False,
                )
            )
            worker.signal_reconstruction_ready.connect(lambda result: ready.append(result))
            worker.signal_reconstruction_failed.connect(lambda task_id, message: failed.append((task_id, message)))
            worker.signal_reconstruction_saved.connect(
                lambda task_id, status, path, message: saved_signals.append((task_id, status, path))
            )

            with mock.patch("sim_control.sim_reconstruction.WarmSIMReconstructor") as warm_cls:
                warm_cls.return_value.reconstruct.return_value = {
                    "reconstruction": reconstruction,
                    "metadata": {"algorithm": "sim_wiener_gpu"},
                }
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
        self.assertEqual(ready[0].metadata["output_save_status"], "requested")
        self.assertEqual(saved_signals, [("gpu-save", "saved", str(output_path))])

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
                async_save_reconstruction_output=False,
            )
        )
        worker.signal_reconstruction_ready.connect(lambda result: ready.append(result))
        worker.signal_reconstruction_failed.connect(lambda task_id, message: failed.append((task_id, message)))

        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch(
            "sim_control.sim_reconstruction.WarmSIMReconstructor"
        ) as warm_cls, mock.patch("sim_control.pipeline.tifffile.imwrite") as imwrite_mock:
            warm_cls.return_value.reconstruct.return_value = {
                "reconstruction": np.ones((1, 4, 6), dtype=np.float32),
                "metadata": {"algorithm": "sim_wiener_gpu"},
            }
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

    def test_reconstruction_worker_reports_tiff_save_failure_via_saved_signal_not_recon_failed(self):
        """写盘失败不再让整次重建失败：结果已先 emit，写盘失败只经 saved("failed") 通知。

        实时分选语义：落盘是归档，发生在结果下发之后；写盘失败不应回滚已推进的
        重建→特征→决策链路。重建本身失败（见下一用例）才走 signal_reconstruction_failed。
        """
        import tempfile

        from sim_control.models import AcquisitionBatch, ReconstructionConfig
        from sim_control.pipeline import ReconstructionWorker

        stack = np.ones((9, 2, 3), dtype=np.uint16)
        ready = []
        failed = []
        saved_signals = []

        with tempfile.TemporaryDirectory() as temp_dir:
            worker = ReconstructionWorker(
                ReconstructionConfig(
                    enabled=True,
                    device="cpu",
                    otf_488_path=__file__,
                    output_path=str(Path(temp_dir) / "reconstruction"),
                    async_save_reconstruction_output=False,
                )
            )
            worker.signal_reconstruction_ready.connect(lambda result: ready.append(result))
            worker.signal_reconstruction_failed.connect(lambda task_id, message: failed.append((task_id, message)))
            worker.signal_reconstruction_saved.connect(
                lambda task_id, status, path, message: saved_signals.append((task_id, status, message))
            )

            with mock.patch("sim_control.sim_reconstruction.WarmSIMReconstructor") as warm_cls, mock.patch(
                "sim_control.pipeline.tifffile.imwrite",
                side_effect=OSError("cannot save reconstruction"),
            ):
                warm_cls.return_value.reconstruct.return_value = {
                    "reconstruction": np.ones((1, 4, 6), dtype=np.float32),
                    "metadata": {"algorithm": "sim_wiener_gpu"},
                }
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

        self.assertEqual(failed, [])
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0].metadata["output_save_status"], "requested")
        self.assertEqual(len(saved_signals), 1)
        self.assertEqual(saved_signals[0][0], "gpu-save-fail")
        self.assertEqual(saved_signals[0][1], "failed")
        self.assertIn("cannot save reconstruction", saved_signals[0][2])

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

        with mock.patch("sim_control.sim_reconstruction.WarmSIMReconstructor") as warm_cls:
            warm_cls.return_value.reconstruct.side_effect = RuntimeError("missing torch")
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

    def test_reconstruction_worker_async_save_drains_and_rejects_after_shutdown(self):
        """异步落盘：slot_shutdown 应 drain 已入队任务并 emit finished；关停后新任务被丢弃，不重启 writer。"""
        import os
        import tempfile
        import time

        from sim_control.models import AcquisitionBatch, ReconstructionConfig
        from sim_control.pipeline import ReconstructionWorker

        stack = np.ones((9, 2, 3), dtype=np.uint16)
        batch = AcquisitionBatch(
            task_id="async-save",
            stack=stack,
            timestamps=[],
            laser_wavelength_nm=488,
            exposure_us=1000,
            pattern_files=[""] * 9,
        )
        finished = []
        dropped = []

        with tempfile.TemporaryDirectory() as temp_dir:
            otf = Path(temp_dir) / "otf.tif"
            otf.write_bytes(b"x")
            output_dir = Path(temp_dir) / "out"
            worker = ReconstructionWorker(
                ReconstructionConfig(
                    enabled=True,
                    device="cpu",
                    otf_488_path=str(otf),
                    output_path=str(output_dir),
                    async_save_reconstruction_output=True,
                    save_queue_maxsize=4,
                )
            )
            worker.signal_shutdown_finished.connect(lambda: finished.append(1))
            worker.signal_reconstruction_saved.connect(
                lambda task_id, status, path, message: dropped.append((task_id, status, message))
            )
            with mock.patch("sim_control.sim_reconstruction.WarmSIMReconstructor") as warm_cls:
                warm_cls.return_value.reconstruct.return_value = {
                    "reconstruction": np.ones((1, 4, 6), dtype=np.float32),
                    "metadata": {"algorithm": "sim_wiener_gpu"},
                }
                worker.slot_reconstruct(batch)  # 入队一个异步保存
                worker.slot_shutdown()  # drain + finished
                time.sleep(0.1)
                self.assertEqual(len(list(output_dir.glob("sim_reconstruction_*.tif"))), 1)
                # 关停后再来一个细胞：保存被丢弃，不重启 writer。
                worker.slot_reconstruct(batch)

        self.assertEqual(finished, [1])
        self.assertTrue(any(status == "failed" and "shutting down" in message for _t, status, message in dropped))
        self.assertFalse(worker._save_thread.is_alive() if worker._save_thread is not None else False)

    def test_slot_warmup_skips_backend_when_otf_missing(self):
        """缺 OTF 的正常波长预热必须在构造热实例之前 return（不 import 后端/torch）。"""
        from sim_control.models import ReconstructionConfig
        from sim_control.pipeline import ReconstructionWorker

        worker = ReconstructionWorker(ReconstructionConfig(enabled=True, device="cpu", otf_488_path=""))
        worker._get_warm_reconstructor = mock.MagicMock()
        worker.slot_warmup(488)
        worker._get_warm_reconstructor.assert_not_called()

    def test_slot_warmup_sentinel_does_environment_warmup(self):
        """哨兵 wavelength<=0 应显式做 import/CUDA 级预热（warmup_environment），不跑 warmup()。"""
        from sim_control.models import ReconstructionConfig
        from sim_control.pipeline import ReconstructionWorker

        worker = ReconstructionWorker(ReconstructionConfig(enabled=True, device="cpu"))
        warm = mock.MagicMock()
        worker._get_warm_reconstructor = mock.MagicMock(return_value=warm)
        worker.slot_warmup(0)
        worker._get_warm_reconstructor.assert_called_once()
        warm.warmup_environment.assert_called_once()
        warm.warmup.assert_not_called()

    def test_slot_warmup_saved_params_estimate_fallback_passes_use_saved_false(self):
        """saved+estimate-fallback 且 .mat 缺失：warmup 必须以 use_saved_params=False 调用（estimate 预热）。"""
        from sim_control.models import ReconstructionConfig
        from sim_control.pipeline import ReconstructionWorker

        worker = ReconstructionWorker(
            ReconstructionConfig(
                enabled=True,
                device="cpu",
                otf_488_path=__file__,  # 真实存在的 OTF 占位
                use_saved_params=True,
                saved_params_fallback="estimate",
                estimated_params_488_path="this/path/does/not/exist.mat",
            )
        )
        warm = mock.MagicMock()
        worker._get_warm_reconstructor = mock.MagicMock(return_value=warm)
        worker.slot_warmup(488)
        warm.warmup.assert_called_once()
        self.assertFalse(warm.warmup.call_args.kwargs["use_saved_params"])

    def test_slot_warmup_saved_params_fail_fallback_skips_when_mat_missing(self):
        """saved+fail-fallback 且 .mat 缺失：直接 warning+return，不构造热实例、不预热。"""
        from sim_control.models import ReconstructionConfig
        from sim_control.pipeline import ReconstructionWorker

        worker = ReconstructionWorker(
            ReconstructionConfig(
                enabled=True,
                device="cpu",
                otf_488_path=__file__,
                use_saved_params=True,
                saved_params_fallback="fail",
                estimated_params_488_path="this/path/does/not/exist.mat",
            )
        )
        worker._get_warm_reconstructor = mock.MagicMock()
        worker.slot_warmup(488)
        worker._get_warm_reconstructor.assert_not_called()

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

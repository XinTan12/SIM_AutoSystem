"""SIM 设置弹窗 DAQ 测试动作的回归测试。

作用：
    覆盖三类与 DAQ 测试相关的小型回归：
        1. ``build_daq_test_target_items``：测试目标下拉的项目顺序与中性红光命名
           （旧 640 配置经迁移后红光行显示为 ``Laser red``）。
        2. 默认 ``DaqLineConfig`` 必须使用 USB-6423 稀疏映射
           ``slm_enable=0/trigger=1/finish=2/cam=8/405=9/488=10/561=11/red=12``。
        3. ``NIDaqWaveformBuilder`` 在迁移 640/647 配置后红光角色统一为中性
           ``laser_red_line``，且 packed port 波形的 bit 模式与稀疏线位匹配。
        4. ``NIDaqAdapter.pulse_line`` 通过 mock nidaqmx 测试：先写 0 → 写位掩码 → 等待 →
           最终写 0；目标位是 ``1 << line_index``。

协作关系：
    上游：``unittest``、``unittest.mock``。
    下游：``sim_control.gui.build_daq_test_target_items``、``sim_control.models``、
          ``sim_control.config_store.app_config_from_dict``、``sim_control.waveform``、
          ``sim_control.adapters.NIDaqAdapter``。

维护要点：
    - 测试中"638 命名迁移"覆盖 ``640/647 → 638`` 历史改动；该迁移逻辑由
      ``_migrate_v0_to_v1`` 完成，删除该迁移前请保留本测试。
    - DAQ 线位约定改变（不太可能）时需同步本测试与 ``models.DEFAULT_DAQ_LINE_INDICES``。
"""

import sys
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class DaqTestTargetTests(unittest.TestCase):
    """覆盖 DAQ 测试目标下拉、默认线位映射与波形 638 名称迁移。"""

    def test_build_daq_test_target_items_uses_638_labels_and_appends_sim_entry(self):
        """旧 640 配置迁移后，测试下拉红光行显示中性 ``Laser red``，末尾追加 ``SIM采集`` 与 ``SLM激活时序``。"""
        from sim_control.config_store import app_config_from_dict
        from sim_control.gui import (
            SIM_ACQUISITION_TEST_ID,
            SLM_ACTIVATION_TIMING_TEST_ID,
            build_daq_test_target_items,
        )

        # 1) 构造一个 v0 旧配置：含 ``laser_640_line`` 和 ``selected_laser_nm=640``。
        config = app_config_from_dict(
            {
                "daq": {
                    "device_name": "Dev1",
                    "slm_enable_line": "Dev1/port0/line0",
                    "slm_trigger_line": "Dev1/port0/line1",
                    "slm_finish_line": "Dev1/port0/line2",
                    "camera_trigger_line": "Dev1/port0/line8",
                    "laser_405_line": "Dev1/port0/line9",
                    "laser_488_line": "Dev1/port0/line10",
                    "laser_561_line": "Dev1/port0/line11",
                    "laser_640_line": "Dev1/port0/line12",
                },
                "selected_laser_nm": 640,
            }
        ).daq

        items = build_daq_test_target_items(config)

        # 2) 列表顺序与文案锁死；SIM 采集与 SLM 激活时序诊断按固定顺序排在末尾。
        # "Camera Trigger + Capture" 项已删除；只剩 4 路激光 + SIM采集 + SLM激活时序。
        self.assertEqual(
            items,
            [
                ("laser_405_line", "Laser 405 -> Dev1/port0/line9"),
                ("laser_488_line", "Laser 488 -> Dev1/port0/line10"),
                ("laser_561_line", "Laser 561 -> Dev1/port0/line11"),
                ("laser_red_line", "Laser red -> Dev1/port0/line12"),
                (SIM_ACQUISITION_TEST_ID, "SIM采集"),
                (SLM_ACTIVATION_TIMING_TEST_ID, "SLM激活时序"),
            ],
        )

    def test_daq_line_config_defaults_follow_sparse_usb_6423_mapping(self):
        """默认 ``DaqLineConfig`` 必须使用项目唯一的稀疏 USB-6423 映射。"""
        from sim_control.models import DaqLineConfig

        config = DaqLineConfig()

        # 8 个角色逐条断言；任何偏移都说明 DEFAULT_DAQ_LINE_INDICES 被改动。
        self.assertEqual(config.slm_enable_line, "Dev1/port0/line0")
        self.assertEqual(config.slm_trigger_line, "Dev1/port0/line1")
        self.assertEqual(config.slm_finish_line, "Dev1/port0/line2")
        self.assertEqual(config.camera_trigger_line, "Dev1/port0/line8")
        self.assertEqual(config.laser_405_line, "Dev1/port0/line9")
        self.assertEqual(config.laser_488_line, "Dev1/port0/line10")
        self.assertEqual(config.laser_561_line, "Dev1/port0/line11")
        # 中性红光键 ``laser_red_line`` 必须存在（line 12 不变）；旧 ``laser_638_line`` /
        # ``laser_640_line`` / ``laser_647_line`` 命名必须不存在。
        self.assertEqual(getattr(config, "laser_red_line", None), "Dev1/port0/line12")
        self.assertFalse(hasattr(config, "laser_638_line"))
        self.assertFalse(hasattr(config, "laser_640_line"))
        self.assertFalse(hasattr(config, "laser_647_line"))

    def test_waveform_builder_uses_638_role_name_and_sparse_line_bits(self):
        """旧 640 配置迁移后，波形 builder 选用中性 ``laser_red_line`` 并产出正确位掩码。"""
        from sim_control.config_store import app_config_from_dict
        from sim_control.models import TimingConfig
        from sim_control.waveform import NIDaqWaveformBuilder

        # 1) 与上面相同的 v0 旧配置；经 from_dict 迁移后内部已是 638 命名。
        config = app_config_from_dict(
            {
                "daq": {
                    "device_name": "Dev1",
                    "slm_enable_line": "Dev1/port0/line0",
                    "slm_trigger_line": "Dev1/port0/line1",
                    "slm_finish_line": "Dev1/port0/line2",
                    "camera_trigger_line": "Dev1/port0/line8",
                    "laser_405_line": "Dev1/port0/line9",
                    "laser_488_line": "Dev1/port0/line10",
                    "laser_561_line": "Dev1/port0/line11",
                    "laser_640_line": "Dev1/port0/line12",
                },
                "selected_laser_nm": 640,
            }
        )
        # 2) 取极低采样率 + 长脉冲，让单帧的 packed 值只有 2 个 sample，便于断言。
        timing = TimingConfig(
            sample_rate_hz=10,
            edge_pulse_us=100_000,
            inter_frame_gap_us=100_000,
            slm_enable_guard_us=100_000,
        )

        plan = NIDaqWaveformBuilder().build(
            config.daq,
            timing,
            laser_wavelength_nm=config.selected_laser_nm,
            exposure_us=100_000,
            frame_count=1,
        )

        # 3) 元数据应反映迁移后的中性红光角色（638/647 都落到 laser_red_line）。
        self.assertEqual(plan.metadata["active_laser_role"], "laser_red_line")
        # 4) packed value = bit0(SLM enable) + bit1(SLM trigger) + bit8(camera) + bit12(red laser)
        #    = 1 + 2 + 256 + 4096 = 4355。该值证明位掩码按稀疏线位正确合成。
        self.assertEqual(int(plan.packed_port_values[1]), 4355)


class NIDaqAdapterPulseTests(unittest.TestCase):
    """覆盖真实 ``NIDaqAdapter.pulse_line`` 的 ctypes 调用顺序。"""

    def test_pulse_line_drives_selected_bit_then_returns_all_lines_low(self):
        """``pulse_line(line_index=3)`` 应依次写 0 → 写 1<<3=8 → sleep → 写 0。"""
        from sim_control import adapters

        # 1) 准备 mock：用一组假对象替换 nidaqmx，并记录所有 write 调用。
        writes = []
        added_channels = []

        class FakeDoChannels:
            def add_do_chan(self, channel_name, line_grouping=None):
                added_channels.append((channel_name, line_grouping))

        class FakeTask:
            def __init__(self):
                self.do_channels = FakeDoChannels()

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def write(self, value, auto_start=True):
                writes.append((value, auto_start))

        fake_nidaqmx = type("FakeNidaqmx", (), {"Task": FakeTask})()

        # 2) 用 mock.patch 把模块内的 ``nidaqmx`` / ``LineGrouping`` / ``time.sleep`` 替换掉。
        with mock.patch.object(adapters, "nidaqmx", fake_nidaqmx), mock.patch.object(
            adapters, "LineGrouping", type("LG", (), {"CHAN_FOR_ALL_LINES": "all_lines"})
        ), mock.patch.object(adapters.time, "sleep", autospec=True) as mocked_sleep:
            adapter = adapters.NIDaqAdapter()
            adapter.pulse_line("Dev2", 3, duration_s=0.1)

        # 3) ``pulse_line`` 现复用 ``set_line``：三次静态写各开一个任务，
        #    每次通道注册的 line_grouping 都必须是 CHAN_FOR_ALL_LINES。
        self.assertEqual(added_channels, [("Dev2/port0", "all_lines")] * 3)
        # 4) 写顺序不变：起点 0 → 1<<3=8 → 终点 0；不传 stop_event 时保持单次
        #    sleep(0.1) 原行为（_interruptible_sleep 的退化路径）。
        self.assertEqual(writes, [(0, True), (8, True), (0, True)])
        mocked_sleep.assert_called_once_with(0.1)

    def test_pulse_line_stop_event_set_during_sleep_ends_pulse_early(self):
        """sleep 分片期间 stop_event 置位 → 不再继续后续分片，finally 仍写 0 收尾。"""
        import threading

        from sim_control import adapters

        writes = []

        class FakeDoChannels:
            def add_do_chan(self, channel_name, line_grouping=None):
                return None

        class FakeTask:
            def __init__(self):
                self.do_channels = FakeDoChannels()

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def write(self, value, auto_start=True):
                writes.append((value, auto_start))

        fake_nidaqmx = type("FakeNidaqmx", (), {"Task": FakeTask})()
        stop_event = threading.Event()

        def _set_event_on_first_sleep(_duration):
            stop_event.set()

        with mock.patch.object(adapters, "nidaqmx", fake_nidaqmx), mock.patch.object(
            adapters, "LineGrouping", type("LG", (), {"CHAN_FOR_ALL_LINES": "all_lines"})
        ), mock.patch.object(adapters.time, "sleep", side_effect=_set_event_on_first_sleep) as mocked_sleep:
            adapter = adapters.NIDaqAdapter()
            # 0.2s 脉冲按 0.05s 分片应有 4 次 sleep；第 1 次分片后置位 → 只 sleep 1 次。
            adapter.pulse_line("Dev2", 3, duration_s=0.2, stop_event=stop_event)

        mocked_sleep.assert_called_once_with(0.05)
        # 脉冲形状完整：0 → 8 → 0（取消时 finally 拉低，不遗留高电平）。
        self.assertEqual(writes, [(0, True), (8, True), (0, True)])

    def test_pulse_line_preset_stop_event_skips_sleep_entirely(self):
        """stop_event 预先置位 → 完全不 sleep；写序列仍为 0 → 掩码 → 0。"""
        import threading

        from sim_control import adapters

        writes = []

        class FakeDoChannels:
            def add_do_chan(self, channel_name, line_grouping=None):
                return None

        class FakeTask:
            def __init__(self):
                self.do_channels = FakeDoChannels()

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def write(self, value, auto_start=True):
                writes.append((value, auto_start))

        fake_nidaqmx = type("FakeNidaqmx", (), {"Task": FakeTask})()
        stop_event = threading.Event()
        stop_event.set()

        with mock.patch.object(adapters, "nidaqmx", fake_nidaqmx), mock.patch.object(
            adapters, "LineGrouping", type("LG", (), {"CHAN_FOR_ALL_LINES": "all_lines"})
        ), mock.patch.object(adapters.time, "sleep", autospec=True) as mocked_sleep:
            adapter = adapters.NIDaqAdapter()
            adapter.pulse_line("Dev2", 3, duration_s=0.2, stop_event=stop_event)

        mocked_sleep.assert_not_called()
        self.assertEqual(writes, [(0, True), (8, True), (0, True)])


class NIDaqAdapterSetLineTests(unittest.TestCase):
    """覆盖 ``NIDaqAdapter.set_line`` 静态单线输出与仿真 DAQ 的调用记录。"""

    def test_set_line_writes_target_mask_then_zero_when_lowered(self):
        """``set_line(high=True)`` 写位掩码；``high=False`` 写 0；各开一个任务。"""
        from sim_control import adapters

        writes = []
        added_channels = []

        class FakeDoChannels:
            def add_do_chan(self, channel_name, line_grouping=None):
                added_channels.append((channel_name, line_grouping))

        class FakeTask:
            def __init__(self):
                self.do_channels = FakeDoChannels()

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def write(self, value, auto_start=True):
                writes.append((value, auto_start))

        fake_nidaqmx = type("FakeNidaqmx", (), {"Task": FakeTask})()

        with mock.patch.object(adapters, "nidaqmx", fake_nidaqmx), mock.patch.object(
            adapters, "LineGrouping", type("LG", (), {"CHAN_FOR_ALL_LINES": "all_lines"})
        ):
            adapter = adapters.NIDaqAdapter()
            adapter.set_line("Dev2", 0, high=True)
            adapter.set_line("Dev2", 0, high=False)

        self.assertEqual(added_channels, [("Dev2/port0", "all_lines")] * 2)
        self.assertEqual(writes, [(1, True), (0, True)])

    def test_set_line_rejects_invalid_line_index(self):
        """line_index 超出 [0, 31] 时应抛 HardwareError，不触碰 NI 任务。"""
        from sim_control import adapters

        with mock.patch.object(adapters, "nidaqmx", object()):
            adapter = adapters.NIDaqAdapter()
            with self.assertRaises(adapters.HardwareError):
                adapter.set_line("Dev2", 32, high=True)

    def test_simulated_daq_adapter_records_set_line_and_set_all_low(self):
        """仿真 DAQ 应记录 set_line 调用序列与 set_all_low 次数供测试断言。"""
        from sim_control.sim_adapters import SimulatedDaqAdapter

        adapter = SimulatedDaqAdapter()
        adapter.set_line("Dev2", 0, True)
        adapter.set_line("Dev2", 0, False)
        adapter.set_all_low("Dev2")

        self.assertEqual(adapter.set_line_calls, [("Dev2", 0, True), ("Dev2", 0, False)])
        self.assertEqual(adapter.set_all_low_calls, 1)

    def test_simulated_pulse_line_preset_stop_event_returns_immediately(self):
        """仿真 pulse_line：stop_event 预先置位时立即返回，不再模拟 100 ms 脉冲时长。"""
        import threading
        import time

        from sim_control.sim_adapters import SimulatedDaqAdapter

        adapter = SimulatedDaqAdapter()
        stop_event = threading.Event()
        stop_event.set()

        started_at = time.perf_counter()
        adapter.pulse_line("Dev2", 6, duration_s=0.1, stop_event=stop_event)
        elapsed_s = time.perf_counter() - started_at

        self.assertLess(elapsed_s, 0.05)


class SlmActivationTimingTestFlowTests(unittest.TestCase):
    """用仿真 adapter 驱动 ``_run_slm_activation_timing_test`` 的核心流程。"""

    def test_activation_timing_test_reaches_active_and_returns_enable_low(self):
        """诊断流程应到达 ACT，并在 finally 中把 enable 拉低 + DAQ 全 0。"""
        from sim_control.config_store import app_config_from_dict
        from sim_control.daq_testing import DaqTestRunner
        from sim_control.sim_adapters import SimulatedDaqAdapter, SimulatedSlmAdapter

        app_config = app_config_from_dict({"config_version": 9})
        slm = SimulatedSlmAdapter()
        slm.connect()
        daq = SimulatedDaqAdapter()

        # DAQ 测试逻辑已从 SimSettingsDialog 迁到可复用的 DaqTestRunner（注入共享 adapter +
        # 配置快照）；行为不变，这里直接构造 runner 调同名方法。
        runner = DaqTestRunner(
            camera_adapter=None,
            slm_adapter=slm,
            daq_adapter=daq,
            config=app_config,
            selected_laser_nm=488,
        )
        result = runner._run_slm_activation_timing_test(app_config.daq)

        self.assertTrue(result.reached_active)
        self.assertIsNotNone(result.enable_to_active_ms)
        self.assertGreaterEqual(result.poll_count, 1)
        # 仿真 SLM 软件激活即视为 ACT，因此初始状态就是 0x56。
        self.assertEqual(result.initial_state["code"], 0x56)
        self.assertIn("488_3.5_2d_50ms", result.running_order_name)
        # slm_enable 先拉高、finally 拉低，随后 set_all_low 安全归位。
        self.assertEqual(daq.set_line_calls, [("Dev1", 0, True), ("Dev1", 0, False)])
        self.assertEqual(daq.set_all_low_calls, 1)

    def test_run_test_rejects_camera_and_unknown_targets(self):
        """删 camera 项后，run_test 对 camera_trigger_line 或未知 id 必须 ValueError（不静默走激光脉冲）。"""
        import threading

        from sim_control.config_store import app_config_from_dict
        from sim_control.daq_testing import DaqTestRunner
        from sim_control.sim_adapters import SimulatedDaqAdapter

        app_config = app_config_from_dict({"config_version": 9})
        runner = DaqTestRunner(
            camera_adapter=None,
            slm_adapter=None,
            daq_adapter=SimulatedDaqAdapter(),
            config=app_config,
            selected_laser_nm=488,
        )
        for bad_id in ("camera_trigger_line", "nonsense_target"):
            with self.assertRaises(ValueError):
                runner.run_test(
                    bad_id,
                    app_config.daq,
                    selected_laser_nm=488,
                    stop_event=threading.Event(),
                )


if __name__ == "__main__":
    unittest.main()

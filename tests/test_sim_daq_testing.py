"""SIM 设置弹窗 DAQ 测试动作的回归测试。

作用：
    覆盖三类与 DAQ 测试相关的小型回归：
        1. ``build_daq_test_target_items``：测试目标下拉的项目顺序与 647nm 命名
           （旧 640 配置经迁移后必须显示为 ``Laser 647``）。
        2. 默认 ``DaqLineConfig`` 必须使用 USB-6423 稀疏映射
           ``slm_enable=0/trigger=1/finish=2/cam=5/405=8/488=6/561=7/647=9``。
        3. ``NIDaqWaveformBuilder`` 在迁移 640→647 配置后仍正确使用 ``laser_647_line``，
           且 packed port 波形的 bit 模式与稀疏线位匹配。
        4. ``NIDaqAdapter.pulse_line`` 通过 mock nidaqmx 测试：先写 0 → 写位掩码 → 等待 →
           最终写 0；目标位是 ``1 << line_index``。

协作关系：
    上游：``unittest``、``unittest.mock``。
    下游：``sim_control.gui.build_daq_test_target_items``、``sim_control.models``、
          ``sim_control.config_store.app_config_from_dict``、``sim_control.waveform``、
          ``sim_control.adapters.NIDaqAdapter``。

维护要点：
    - 测试中"647 命名迁移"覆盖 ``640 → 647`` 历史改动；该迁移逻辑由
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
    """覆盖 DAQ 测试目标下拉、默认线位映射与波形 647 名称迁移。"""

    def test_build_daq_test_target_items_uses_647_labels_and_appends_sim_entry(self):
        """旧 640 配置迁移后，测试下拉显示 ``Laser 647``，并末尾追加 ``SIM采集``。"""
        from sim_control.config_store import app_config_from_dict
        from sim_control.gui import SIM_ACQUISITION_TEST_ID, build_daq_test_target_items

        # 1) 构造一个 v0 旧配置：含 ``laser_640_line`` 和 ``selected_laser_nm=640``。
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
        ).daq

        items = build_daq_test_target_items(config)

        # 2) 列表顺序与文案锁死；测试目标 ID 末尾必须是 ``SIM_ACQUISITION_TEST_ID``。
        self.assertEqual(
            items,
            [
                ("camera_trigger_line", "Camera Trigger + Capture -> Dev2/port0/line5"),
                ("laser_405_line", "Laser 405 -> Dev2/port0/line8"),
                ("laser_488_line", "Laser 488 -> Dev2/port0/line6"),
                ("laser_561_line", "Laser 561 -> Dev2/port0/line7"),
                ("laser_647_line", "Laser 647 -> Dev2/port0/line9"),
                (SIM_ACQUISITION_TEST_ID, "SIM采集"),
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
        self.assertEqual(config.camera_trigger_line, "Dev1/port0/line5")
        self.assertEqual(config.laser_405_line, "Dev1/port0/line8")
        self.assertEqual(config.laser_488_line, "Dev1/port0/line6")
        self.assertEqual(config.laser_561_line, "Dev1/port0/line7")
        # ``laser_647_line`` 必须存在，旧 ``laser_640_line`` 必须不存在。
        self.assertEqual(getattr(config, "laser_647_line", None), "Dev1/port0/line9")
        self.assertFalse(hasattr(config, "laser_640_line"))

    def test_waveform_builder_uses_647_role_name_and_sparse_line_bits(self):
        """旧 640 配置迁移后，波形 builder 选用 ``laser_647_line`` 并产出正确位掩码。"""
        from sim_control.config_store import app_config_from_dict
        from sim_control.models import TimingConfig
        from sim_control.waveform import NIDaqWaveformBuilder

        # 1) 与上面相同的 v0 旧配置；经 from_dict 迁移后内部已是 647 命名。
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

        # 3) 元数据应反映迁移后的 647 角色。
        self.assertEqual(plan.metadata["active_laser_role"], "laser_647_line")
        # 4) packed value = bit0(SLM enable) + bit1(SLM trigger) + bit5(camera) + bit9(647 laser)
        #    = 1 + 2 + 32 + 512 = 547。该值证明位掩码按稀疏线位正确合成。
        self.assertEqual(int(plan.packed_port_values[1]), 547)


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

        # 3) 通道注册时 line_grouping 必须是 CHAN_FOR_ALL_LINES。
        self.assertEqual(added_channels, [("Dev2/port0", "all_lines")])
        # 4) 写顺序：起点 0 → 1<<3=8 → 终点 0；sleep 持续 0.1s。
        self.assertEqual(writes, [(0, True), (8, True), (0, True)])
        mocked_sleep.assert_called_once_with(0.1)


if __name__ == "__main__":
    unittest.main()

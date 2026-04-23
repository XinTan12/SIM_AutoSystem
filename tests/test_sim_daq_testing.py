import sys
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class DaqTestTargetTests(unittest.TestCase):
    def test_build_daq_test_target_items_uses_647_labels_and_appends_sim_entry(self):
        from sim_control.config_store import app_config_from_dict
        from sim_control.gui import SIM_ACQUISITION_TEST_ID, build_daq_test_target_items

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

        self.assertEqual(
            items,
            [
                ("camera_trigger_line", "Camera Trigger -> Dev2/port0/line5"),
                ("laser_405_line", "Laser 405 -> Dev2/port0/line8"),
                ("laser_488_line", "Laser 488 -> Dev2/port0/line6"),
                ("laser_561_line", "Laser 561 -> Dev2/port0/line7"),
                ("laser_647_line", "Laser 647 -> Dev2/port0/line9"),
                (SIM_ACQUISITION_TEST_ID, "SIM采集"),
            ],
        )

    def test_daq_line_config_defaults_follow_sparse_usb_6423_mapping(self):
        from sim_control.models import DaqLineConfig

        config = DaqLineConfig()

        self.assertEqual(config.slm_enable_line, "Dev1/port0/line0")
        self.assertEqual(config.slm_trigger_line, "Dev1/port0/line1")
        self.assertEqual(config.slm_finish_line, "Dev1/port0/line2")
        self.assertEqual(config.camera_trigger_line, "Dev1/port0/line5")
        self.assertEqual(config.laser_405_line, "Dev1/port0/line8")
        self.assertEqual(config.laser_488_line, "Dev1/port0/line6")
        self.assertEqual(config.laser_561_line, "Dev1/port0/line7")
        self.assertEqual(getattr(config, "laser_647_line", None), "Dev1/port0/line9")
        self.assertFalse(hasattr(config, "laser_640_line"))

    def test_waveform_builder_uses_647_role_name_and_sparse_line_bits(self):
        from sim_control.config_store import app_config_from_dict
        from sim_control.models import TimingConfig
        from sim_control.waveform import NIDaqWaveformBuilder

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

        self.assertEqual(plan.metadata["active_laser_role"], "laser_647_line")
        self.assertEqual(int(plan.packed_port_values[1]), 547)


class NIDaqAdapterPulseTests(unittest.TestCase):
    def test_pulse_line_drives_selected_bit_then_returns_all_lines_low(self):
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
        ), mock.patch.object(adapters.time, "sleep", autospec=True) as mocked_sleep:
            adapter = adapters.NIDaqAdapter()
            adapter.pulse_line("Dev2", 3, duration_s=0.1)

        self.assertEqual(added_channels, [("Dev2/port0", "all_lines")])
        self.assertEqual(writes, [(0, True), (8, True), (0, True)])
        mocked_sleep.assert_called_once_with(0.1)


if __name__ == "__main__":
    unittest.main()

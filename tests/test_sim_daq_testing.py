import sys
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class DaqTestTargetTests(unittest.TestCase):
    def test_build_daq_test_target_items_uses_role_labels_and_appends_sim_entry(self):
        from sim_control.gui import SIM_ACQUISITION_TEST_ID, build_daq_test_target_items
        from sim_control.models import DaqLineConfig

        config = DaqLineConfig(
            device_name="Dev2",
            slm_enable_line="Dev2/port0/line0",
            slm_trigger_line="Dev2/port0/line1",
            slm_finish_line="Dev2/port0/line2",
            camera_trigger_line="Dev2/port0/line3",
            laser_405_line="Dev2/port0/line4",
            laser_488_line="Dev2/port0/line5",
            laser_561_line="Dev2/port0/line6",
            laser_640_line="Dev2/port0/line7",
        )

        items = build_daq_test_target_items(config)

        self.assertEqual(
            items,
            [
                ("camera_trigger_line", "Camera Trigger -> Dev2/port0/line3"),
                ("laser_405_line", "Laser 405 -> Dev2/port0/line4"),
                ("laser_488_line", "Laser 488 -> Dev2/port0/line5"),
                ("laser_561_line", "Laser 561 -> Dev2/port0/line6"),
                ("laser_640_line", "Laser 640 -> Dev2/port0/line7"),
                (SIM_ACQUISITION_TEST_ID, "SIM采集"),
            ],
        )


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

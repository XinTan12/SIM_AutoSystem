import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class SimulatedZStageAdapterTests(unittest.TestCase):
    def test_simulated_stage_connects_moves_and_reports_range(self):
        from sim_control.stage_adapter import SimulatedZStageAdapter

        stage = SimulatedZStageAdapter(start_um=10.0, min_um=0.0, max_um=20.0)

        self.assertFalse(stage.is_connected)
        stage.connect()
        self.assertTrue(stage.is_connected)
        self.assertEqual(stage.get_z_ranges_um(), (0.0, 20.0))

        stage.move_z_um(12.5)

        self.assertEqual(stage.get_position_um(), 12.5)

    def test_simulated_stage_rejects_out_of_range_moves(self):
        from sim_control.stage_adapter import SimulatedZStageAdapter

        stage = SimulatedZStageAdapter(start_um=10.0, min_um=0.0, max_um=20.0)
        stage.connect()

        with self.assertRaisesRegex(ValueError, "outside"):
            stage.move_z_um(25.0)

        self.assertEqual(stage.get_position_um(), 10.0)


class Ti2ZStageAdapterTests(unittest.TestCase):
    def test_ti2_stage_adapter_accepts_ti2_sdk_range_field_names(self):
        from sim_control.stage_adapter import Ti2ZStageAdapter

        adapter = Ti2ZStageAdapter()
        adapter._stage = SimpleNamespace(
            get_z_ranges_um=lambda: {
                "physical": SimpleNamespace(lower_um=0.0, upper_um=10000.0),
                "logical": SimpleNamespace(lower_um=0.0, upper_um=10000.0),
            }
        )
        adapter.is_connected = True

        self.assertEqual(adapter.get_z_ranges_um(), (0.0, 10000.0))

    def test_ti2_stage_adapter_keeps_min_max_range_fallback(self):
        from sim_control.stage_adapter import Ti2ZStageAdapter

        adapter = Ti2ZStageAdapter()
        adapter._stage = SimpleNamespace(
            get_z_ranges_um=lambda: {
                "physical": SimpleNamespace(min_um=-5.0, max_um=5.0),
            }
        )
        adapter.is_connected = True

        self.assertEqual(adapter.get_z_ranges_um(), (-5.0, 5.0))


if __name__ == "__main__":
    unittest.main()

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class WaveformValidationTests(unittest.TestCase):
    def test_waveform_plan_exposes_warnings_for_short_exposure_and_gap(self):
        from sim_control.models import DaqLineConfig, TimingConfig
        from sim_control.waveform import NIDaqWaveformBuilder

        plan = NIDaqWaveformBuilder().build(
            daq_config=DaqLineConfig(),
            timing=TimingConfig(
                sample_rate_hz=1_000,
                edge_pulse_us=50,
                inter_frame_gap_us=0,
                slm_enable_guard_us=50,
            ),
            laser_wavelength_nm=488,
            exposure_us=1,
            frame_count=9,
        )

        joined = "\n".join(plan.warnings)
        self.assertIn("exposure", joined)
        self.assertIn("inter_frame_gap_us", joined)

    def test_waveform_warns_when_slm_finish_pulse_extends_beyond_frame_gap(self):
        from sim_control.models import DaqLineConfig, TimingConfig
        from sim_control.waveform import NIDaqWaveformBuilder

        plan = NIDaqWaveformBuilder().build(
            daq_config=DaqLineConfig(),
            timing=TimingConfig(
                sample_rate_hz=1_000_000,
                edge_pulse_us=100,
                inter_frame_gap_us=50,
                slm_enable_guard_us=50,
            ),
            laser_wavelength_nm=488,
            exposure_us=1_000,
            frame_count=9,
        )

        self.assertTrue(any("slm_finish" in warning for warning in plan.warnings))

    def test_standard_sim9_waveform_has_50ms_gap_from_finish_to_next_trigger(self):
        from sim_control.models import DaqLineConfig, TimingConfig
        from sim_control.waveform import NIDaqWaveformBuilder

        plan = NIDaqWaveformBuilder().build(
            daq_config=DaqLineConfig(),
            timing=TimingConfig(
                sample_rate_hz=1_000_000,
                edge_pulse_us=50,
                inter_frame_gap_us=50_000,
                slm_enable_guard_us=50,
            ),
            laser_wavelength_nm=488,
            exposure_us=500_000,
            frame_count=9,
        )

        frame_starts = plan.metadata["frame_start_samples"]
        frame_ends = plan.metadata["frame_end_samples"]
        for frame_index in range(8):
            self.assertEqual(frame_starts[frame_index + 1] - frame_ends[frame_index], 50_000)

        slm_trigger = plan.role_matrix["slm_trigger_line"]
        slm_finish = plan.role_matrix["slm_finish_line"]
        self.assertEqual(int(slm_trigger[frame_starts[1]]), 1)
        self.assertEqual(int(slm_finish[frame_ends[0]]), 1)
        self.assertEqual(int(slm_trigger[frame_ends[0]]), 0)
        self.assertEqual(int(slm_finish[frame_starts[1]]), 0)


if __name__ == "__main__":
    unittest.main()

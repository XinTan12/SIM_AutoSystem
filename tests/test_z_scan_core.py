import sys
import unittest
import math
from pathlib import Path

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class _SyntheticFocusCamera:
    def __init__(self, stage, best_z: float) -> None:
        self.stage = stage
        self.best_z = best_z
        self.applied_configs = []
        self.arm_counts = []
        self.disarm_count = 0

    def apply_config(self, config):
        self.applied_configs.append(config)
        return {"timing_readout_time_s": 0.0}

    def arm(self, frame_count: int) -> None:
        self.arm_counts.append(frame_count)

    def disarm(self) -> None:
        self.disarm_count += 1

    def read_frame_sequence(self, frame_count, pattern_files, laser_wavelength_nm, frame_callback=None, stop_event=None):
        if frame_count != 1:
            raise AssertionError("z-scan should capture one camera frame per z position")
        yy, xx = np.indices((48, 48))
        checker = ((xx + yy) % 2).astype(np.float64)
        distance = abs(self.stage.get_position_um() - self.best_z)
        amplitude = max(0.0, 4000.0 - distance * 1500.0)
        image = (checker * amplitude + 100).astype(np.uint16)
        return image[np.newaxis, ...], [self.stage.get_position_um()]


class _FakeSlm:
    def __init__(self) -> None:
        self.activation_count = 0

    def activate_prepared_patterns(self) -> None:
        self.activation_count += 1


class _FakeDaq:
    def __init__(self) -> None:
        self.plans = []
        self.set_low_count = 0

    def play_waveform(self, device_name, plan, stop_event=None) -> None:
        self.plans.append(plan)

    def set_all_low(self, device_name) -> None:
        self.set_low_count += 1


class ZScanCoreTests(unittest.TestCase):
    def test_scan_positions_uses_runtime_stage_position_when_start_is_auto(self):
        from sim_control.models import ZScanConfig
        from sim_control.z_scan_core import scan_positions

        positive = scan_positions(
            ZScanConfig(start_um=None, direction="positive_z", step_um=0.5, num_steps=4),
            stage_position_um=7.25,
        )
        negative = scan_positions(
            ZScanConfig(start_um=None, direction="negative_z", step_um=0.5, num_steps=4),
            stage_position_um=7.25,
        )

        self.assertEqual(positive, [7.25, 7.75, 8.25, 8.75, 9.25])
        self.assertEqual(negative, [7.25, 6.75, 6.25, 5.75, 5.25])

    def test_scan_positions_treats_num_steps_as_move_count(self):
        from sim_control.models import ZScanConfig
        from sim_control.z_scan_core import scan_positions

        positive = scan_positions(
            ZScanConfig(start_um=1.0, direction="positive_z", step_um=0.3, num_steps=10),
            stage_position_um=99.0,
        )
        negative = scan_positions(
            ZScanConfig(start_um=1.0, direction="negative_z", step_um=0.3, num_steps=10),
            stage_position_um=99.0,
        )

        self.assertEqual(len(positive), 11)
        self.assertEqual(len(negative), 11)
        self.assertAlmostEqual(positive[-1], 4.0)
        self.assertAlmostEqual(negative[-1], -2.0)

    def test_run_z_scan_selects_best_focus_from_single_frame_per_step(self):
        from sim_control.models import CameraConfig, DaqLineConfig, TimingConfig, ZScanConfig
        from sim_control.stage_adapter import SimulatedZStageAdapter
        from sim_control.z_scan_core import run_z_scan

        stage = SimulatedZStageAdapter(start_um=0.0, min_um=-5.0, max_um=10.0)
        stage.connect()
        camera = _SyntheticFocusCamera(stage, best_z=2.0)
        slm = _FakeSlm()
        daq = _FakeDaq()
        statuses = []

        result = run_z_scan(
            stage_adapter=stage,
            camera_adapter=camera,
            slm_adapter=slm,
            daq_adapter=daq,
            daq_config=DaqLineConfig(),
            camera_config=CameraConfig(exposure_us=10_000),
            timing=TimingConfig(sample_rate_hz=1_000_000, slm_enable_guard_us=50),
            z_scan_config=ZScanConfig(
                start_um=0.0,
                direction="positive_z",
                step_um=1.0,
                num_steps=5,
                exposure_preset_ms=8,
            ),
            on_status=lambda event, payload: statuses.append((event, payload)),
        )

        self.assertEqual(result.best_z_um, 2.0)
        self.assertEqual([point.z_um for point in result.focus_curve], [0.0, 1.0, 2.0, 3.0, 4.0, 5.0])
        self.assertEqual(camera.arm_counts, [1, 1, 1, 1, 1, 1])
        self.assertEqual(camera.applied_configs[-1].exposure_us, 7_884)
        self.assertEqual(slm.activation_count, 6)
        self.assertEqual(len(daq.plans), 6)
        self.assertTrue(all(plan.metadata["z_scan"] for plan in daq.plans))
        self.assertEqual(stage.get_position_um(), 2.0)
        self.assertEqual([event for event, _ in statuses].count("z_scan_progress"), 6)
        self.assertEqual(statuses[-1][0], "z_scan_complete")

    def test_run_z_scan_returns_to_start_when_cancelled(self):
        import threading

        from sim_control.models import CameraConfig, DaqLineConfig, TimingConfig, ZScanConfig
        from sim_control.stage_adapter import SimulatedZStageAdapter
        from sim_control.z_scan_core import ZScanCancelled, run_z_scan

        stage = SimulatedZStageAdapter(start_um=3.0, min_um=0.0, max_um=10.0)
        stage.connect()
        camera = _SyntheticFocusCamera(stage, best_z=4.0)
        slm = _FakeSlm()
        daq = _FakeDaq()
        stop_event = threading.Event()

        def _stop_after_first_step(event, payload):
            if event == "z_scan_progress":
                stop_event.set()

        with self.assertRaises(ZScanCancelled):
            run_z_scan(
                stage_adapter=stage,
                camera_adapter=camera,
                slm_adapter=slm,
                daq_adapter=daq,
                daq_config=DaqLineConfig(),
                camera_config=CameraConfig(),
                timing=TimingConfig(),
                z_scan_config=ZScanConfig(
                    start_um=3.0,
                    direction="positive_z",
                    step_um=1.0,
                    num_steps=4,
                    return_to_start_on_cancel=True,
                ),
                stop_event=stop_event,
                on_status=_stop_after_first_step,
            )

        self.assertEqual(stage.get_position_um(), 3.0)

    def test_run_z_scan_keep_captured_stack_true_returns_full_stack_and_still_picks_best_z(self):
        from sim_control.models import CameraConfig, DaqLineConfig, TimingConfig, ZScanConfig
        from sim_control.stage_adapter import SimulatedZStageAdapter
        from sim_control.z_scan_core import run_z_scan

        stage = SimulatedZStageAdapter(start_um=0.0, min_um=-5.0, max_um=10.0)
        stage.connect()
        camera = _SyntheticFocusCamera(stage, best_z=2.0)
        slm = _FakeSlm()
        daq = _FakeDaq()

        result = run_z_scan(
            stage_adapter=stage,
            camera_adapter=camera,
            slm_adapter=slm,
            daq_adapter=daq,
            daq_config=DaqLineConfig(),
            camera_config=CameraConfig(exposure_us=10_000),
            timing=TimingConfig(sample_rate_hz=1_000_000, slm_enable_guard_us=50),
            z_scan_config=ZScanConfig(
                start_um=0.0,
                direction="positive_z",
                step_um=1.0,
                num_steps=5,
                exposure_preset_ms=8,
            ),
            keep_captured_stack=True,
        )

        self.assertIsNotNone(result.captured_stack)
        self.assertEqual(result.captured_stack.shape, (6, 48, 48))
        self.assertEqual(result.captured_stack.dtype, np.uint16)
        self.assertAlmostEqual(result.best_z_um, 2.0)
        self.assertAlmostEqual(stage.get_position_um(), 2.0)
        for point in result.focus_curve:
            self.assertFalse(math.isnan(point.focus_score))

    def test_run_z_scan_default_keep_captured_stack_false_preserves_legacy_memory_behavior(self):
        from sim_control.models import CameraConfig, DaqLineConfig, TimingConfig, ZScanConfig
        from sim_control.stage_adapter import SimulatedZStageAdapter
        from sim_control.z_scan_core import run_z_scan

        stage = SimulatedZStageAdapter(start_um=0.0, min_um=-5.0, max_um=10.0)
        stage.connect()
        camera = _SyntheticFocusCamera(stage, best_z=2.0)
        slm = _FakeSlm()
        daq = _FakeDaq()

        result = run_z_scan(
            stage_adapter=stage,
            camera_adapter=camera,
            slm_adapter=slm,
            daq_adapter=daq,
            daq_config=DaqLineConfig(),
            camera_config=CameraConfig(exposure_us=10_000),
            timing=TimingConfig(sample_rate_hz=1_000_000, slm_enable_guard_us=50),
            z_scan_config=ZScanConfig(
                start_um=0.0,
                direction="positive_z",
                step_um=1.0,
                num_steps=5,
                exposure_preset_ms=8,
            ),
        )

        self.assertIsNone(result.captured_stack)
        self.assertAlmostEqual(result.best_z_um, 2.0)


if __name__ == "__main__":
    unittest.main()

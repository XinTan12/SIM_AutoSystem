import sys
import unittest
import math
from pathlib import Path
from types import SimpleNamespace

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
        self.read_wavelengths = []

    def apply_config(self, config):
        self.applied_configs.append(config)
        return {"timing_readout_time_s": 0.0}

    def arm(self, frame_count: int) -> None:
        self.arm_counts.append(frame_count)

    def disarm(self) -> None:
        self.disarm_count += 1

    def read_frame_sequence(self, frame_count, pattern_files, laser_wavelength_nm, frame_callback=None, stop_event=None):
        self.read_wavelengths.append(int(laser_wavelength_nm))
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
        best_focus_events = [
            payload for event, payload in statuses if event == "z_scan_best_focus_positioned"
        ]
        self.assertEqual(len(best_focus_events), 1)
        self.assertEqual(best_focus_events[0]["z_um"], 2.0)
        self.assertIn("move_ms", best_focus_events[0])
        self.assertEqual(statuses[-1][0], "z_scan_complete")

    def test_run_z_scan_forwards_requested_wavelength_to_waveform_and_camera(self):
        from sim_control.models import CameraConfig, DaqLineConfig, TimingConfig, ZScanConfig
        from sim_control.stage_adapter import SimulatedZStageAdapter
        from sim_control.z_scan_core import run_z_scan

        stage = SimulatedZStageAdapter(start_um=0.0, min_um=-5.0, max_um=5.0)
        stage.connect()
        camera = _SyntheticFocusCamera(stage, best_z=0.0)
        from unittest import mock

        builder = mock.Mock()
        builder.build_z_scan.return_value = SimpleNamespace(warnings=[])

        run_z_scan(
            stage_adapter=stage,
            camera_adapter=camera,
            slm_adapter=_FakeSlm(),
            daq_adapter=_FakeDaq(),
            daq_config=DaqLineConfig(),
            camera_config=CameraConfig(),
            timing=TimingConfig(),
            z_scan_config=ZScanConfig(start_um=0.0, step_um=0.5, num_steps=1),
            laser_wavelength_nm=561,
            waveform_builder=builder,
        )

        self.assertEqual(camera.read_wavelengths, [561, 561])
        self.assertEqual(builder.build_z_scan.call_count, 1)
        self.assertEqual(builder.build_z_scan.call_args.kwargs["laser_wavelength_nm"], 561)

    def test_run_z_scan_waveform_failure_is_preflighted_before_camera_or_stage_motion(self):
        from unittest import mock

        from sim_control.models import CameraConfig, DaqLineConfig, TimingConfig, ZScanConfig
        from sim_control.z_scan_core import run_z_scan

        stage = mock.Mock()
        stage.get_position_um.return_value = 5.0
        camera = mock.Mock()
        builder = mock.Mock()
        builder.build_z_scan.side_effect = RuntimeError("invalid z-scan waveform")

        with self.assertRaisesRegex(RuntimeError, "invalid z-scan waveform"):
            run_z_scan(
                stage_adapter=stage,
                camera_adapter=camera,
                slm_adapter=mock.Mock(),
                daq_adapter=mock.Mock(),
                daq_config=DaqLineConfig(),
                camera_config=CameraConfig(),
                timing=TimingConfig(),
                z_scan_config=ZScanConfig(start_um=None, step_um=0.5, num_steps=1),
                laser_wavelength_nm=488,
                waveform_builder=builder,
            )

        camera.apply_config.assert_not_called()
        camera.arm.assert_not_called()
        stage.move_z_um.assert_not_called()

    def test_run_z_scan_cancel_keeps_current_z_and_cleans_up(self):
        import threading

        from sim_control.models import CameraConfig, DaqLineConfig, TimingConfig, ZScanConfig
        from sim_control.stage_adapter import SimulatedZStageAdapter
        from sim_control.z_scan_core import ZScanCancelled, run_z_scan

        stage = SimulatedZStageAdapter(start_um=3.0, min_um=0.0, max_um=10.0)
        stage.connect()
        camera = _SyntheticFocusCamera(stage, best_z=4.0)
        slm = _FakeSlm()
        stop_event = threading.Event()

        class _CancelAfterSecondWaveformDaq(_FakeDaq):
            def play_waveform(self, device_name, plan, stop_event=None) -> None:
                super().play_waveform(device_name, plan, stop_event=stop_event)
                if len(self.plans) == 2:
                    stop_event.set()

        daq = _CancelAfterSecondWaveformDaq()

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
                ),
                stop_event=stop_event,
            )

        self.assertEqual(stage.get_position_um(), 4.0)
        self.assertEqual(camera.disarm_count, 2)
        self.assertGreaterEqual(daq.set_low_count, 2)

    def test_run_z_scan_early_cancel_sets_daq_low_without_stage_motion(self):
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
                z_scan_config=ZScanConfig(start_um=3.0, step_um=1.0, num_steps=4),
                stop_event=stop_event,
            )

        self.assertEqual(stage.get_position_um(), 3.0)
        self.assertEqual(camera.arm_counts, [])
        self.assertGreaterEqual(daq.set_low_count, 1)

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

    def test_run_z_scan_reuses_waveform_plan_but_keeps_warning_event_per_step(self):
        from sim_control.models import CameraConfig, DaqLineConfig, TimingConfig, ZScanConfig
        from sim_control.stage_adapter import SimulatedZStageAdapter
        from sim_control.z_scan_core import run_z_scan

        class CountingBuilder:
            def __init__(self) -> None:
                self.calls = 0
                self.plan = SimpleNamespace(metadata={"z_scan": True}, warnings=["cached warning"])

            def build_z_scan(self, **_kwargs):
                self.calls += 1
                return self.plan

        stage = SimulatedZStageAdapter(start_um=0.0, min_um=-5.0, max_um=10.0)
        stage.connect()
        camera = _SyntheticFocusCamera(stage, best_z=1.0)
        slm = _FakeSlm()
        daq = _FakeDaq()
        builder = CountingBuilder()
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
                num_steps=3,
                exposure_preset_ms=8,
            ),
            waveform_builder=builder,
            on_status=lambda event, payload: statuses.append((event, payload)),
        )

        self.assertEqual(builder.calls, 1)
        self.assertEqual(len(daq.plans), 4)
        self.assertTrue(all(plan is builder.plan for plan in daq.plans))
        self.assertEqual(result.best_z_um, 1.0)
        warning_payloads = [payload for event, payload in statuses if event == "z_scan_waveform_warning"]
        self.assertEqual(len(warning_payloads), 4)
        self.assertEqual([payload["warnings"] for payload in warning_payloads], [["cached warning"]] * 4)

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


class _RecordingStage:
    """Fake Z stage recording move_z_um calls; ``is_connected`` is an attribute."""

    def __init__(self, start_um: float = 0.0, min_um: float = -100.0, max_um: float = 100.0) -> None:
        self._position_um = float(start_um)
        self._min_um = float(min_um)
        self._max_um = float(max_um)
        self.is_connected = False
        self.connect_count = 0
        self.moves: list[float] = []

    def connect(self) -> dict:
        self.is_connected = True
        self.connect_count += 1
        return {"mode": "recording"}

    def get_position_um(self) -> float:
        return self._position_um

    def get_z_ranges_um(self):
        return self._min_um, self._max_um

    def move_z_um(self, target_um: float) -> None:
        target = float(target_um)
        self.moves.append(target)
        self._position_um = target


class _CountingStopEvent:
    """stop_event that reports set() only after ``trip_after`` checks."""

    def __init__(self, trip_after: int) -> None:
        self.trip_after = int(trip_after)
        self.checks = 0

    def is_set(self) -> bool:
        self.checks += 1
        return self.checks > self.trip_after


class _ConnectableCamera:
    """Fake camera/SLM-like adapter with ``is_connected`` as a METHOD."""

    def __init__(self, connected: bool = True) -> None:
        self._connected = bool(connected)
        self.applied_configs = []

    def is_connected(self) -> bool:
        return self._connected

    def apply_config(self, config):
        self.applied_configs.append(config)
        return {"timing_readout_time_s": 0.0}


class _RoSlm(_ConnectableCamera):
    """Fake SLM with method ``is_connected`` and RO selection recording."""

    def __init__(self, connected: bool = True, running_orders=None) -> None:
        super().__init__(connected=connected)
        self._running_orders = running_orders if running_orders is not None else [(0, "488_3.5_2d_zscan3p_1ms")]
        self.selected = []

    def list_running_orders(self):
        return list(self._running_orders)

    def select_running_order(self, ro_index: int):
        self.selected.append(int(ro_index))
        return {"ro_index": int(ro_index)}


class RunZScanStageOnlyTests(unittest.TestCase):
    def test_positive_direction_moves_match_scan_positions(self):
        from sim_control.models import ZScanConfig
        from sim_control.z_scan_core import run_z_scan_stage_only, scan_positions

        stage = _RecordingStage(start_um=5.0, min_um=-100.0, max_um=100.0)
        stage.connect()
        cfg = ZScanConfig(start_um=None, direction="positive_z", step_um=0.5, num_steps=4)
        expected = scan_positions(cfg, stage_position_um=5.0)

        result = run_z_scan_stage_only(stage_adapter=stage, z_scan_config=cfg)

        self.assertEqual(stage.moves, expected)
        self.assertEqual(result.positions_visited, expected)
        self.assertEqual(result.started_from_um, expected[0])
        self.assertEqual(len(result.move_latencies_ms), len(expected))

    def test_negative_direction_moves_match_scan_positions(self):
        from sim_control.models import ZScanConfig
        from sim_control.z_scan_core import run_z_scan_stage_only, scan_positions

        stage = _RecordingStage(start_um=5.0)
        stage.connect()
        cfg = ZScanConfig(start_um=None, direction="negative_z", step_um=0.5, num_steps=4)
        expected = scan_positions(cfg, stage_position_um=5.0)

        result = run_z_scan_stage_only(stage_adapter=stage, z_scan_config=cfg)

        self.assertEqual(stage.moves, expected)
        self.assertEqual(result.positions_visited, expected)

    def test_connects_when_not_connected(self):
        from sim_control.models import ZScanConfig
        from sim_control.z_scan_core import run_z_scan_stage_only

        stage = _RecordingStage(start_um=0.0)
        self.assertFalse(stage.is_connected)
        cfg = ZScanConfig(start_um=0.0, direction="positive_z", step_um=1.0, num_steps=3)

        run_z_scan_stage_only(stage_adapter=stage, z_scan_config=cfg)

        self.assertTrue(stage.is_connected)
        self.assertEqual(stage.connect_count, 1)

    def test_out_of_range_raises_before_any_move(self):
        from sim_control.errors import HardwareError
        from sim_control.models import ZScanConfig
        from sim_control.z_scan_core import run_z_scan_stage_only

        stage = _RecordingStage(start_um=0.0, min_um=0.0, max_um=1.0)
        stage.connect()
        cfg = ZScanConfig(start_um=0.0, direction="positive_z", step_um=1.0, num_steps=5)

        with self.assertRaises(HardwareError):
            run_z_scan_stage_only(stage_adapter=stage, z_scan_config=cfg)

        self.assertEqual(stage.moves, [])

    def test_num_steps_zero_raises_value_error(self):
        from sim_control.models import ZScanConfig
        from sim_control.z_scan_core import run_z_scan_stage_only

        stage = _RecordingStage(start_um=0.0)
        stage.connect()
        cfg = ZScanConfig(start_um=0.0, direction="positive_z", step_um=1.0, num_steps=0)

        with self.assertRaises(ValueError):
            run_z_scan_stage_only(stage_adapter=stage, z_scan_config=cfg)

        self.assertEqual(stage.moves, [])

    def test_none_stage_raises_hardware_error(self):
        from sim_control.errors import HardwareError
        from sim_control.models import ZScanConfig
        from sim_control.z_scan_core import run_z_scan_stage_only

        cfg = ZScanConfig(start_um=0.0, direction="positive_z", step_um=1.0, num_steps=3)
        with self.assertRaises(HardwareError):
            run_z_scan_stage_only(stage_adapter=None, z_scan_config=cfg)

    def test_cancel_after_two_moves_stops_remaining_positions(self):
        from sim_control.models import ZScanConfig
        from sim_control.z_scan_core import ZScanCancelled, run_z_scan_stage_only

        stage = _RecordingStage(start_um=0.0)
        stage.connect()
        cfg = ZScanConfig(start_um=0.0, direction="positive_z", step_um=1.0, num_steps=5)
        # positions = [0,1,2,3,4,5]; cancel-check passes for index 0 and 1, trips at index 2.
        stop_event = _CountingStopEvent(trip_after=2)

        with self.assertRaises(ZScanCancelled):
            run_z_scan_stage_only(stage_adapter=stage, z_scan_config=cfg, stop_event=stop_event)

        self.assertEqual(stage.moves, [0.0, 1.0])

    def test_on_status_called_once_per_position_with_payload_fields(self):
        from sim_control.models import ZScanConfig
        from sim_control.z_scan_core import run_z_scan_stage_only, scan_positions

        stage = _RecordingStage(start_um=0.0)
        stage.connect()
        cfg = ZScanConfig(start_um=0.0, direction="positive_z", step_um=1.0, num_steps=4)
        positions = scan_positions(cfg, stage_position_um=0.0)
        statuses = []

        run_z_scan_stage_only(
            stage_adapter=stage,
            z_scan_config=cfg,
            on_status=lambda event, payload: statuses.append((event, payload)),
        )

        self.assertEqual(len(statuses), len(positions))
        self.assertTrue(all(event == "z_scan_stage_positioned" for event, _ in statuses))
        first_payload = statuses[0][1]
        self.assertEqual(first_payload["step_index"], 0)
        self.assertEqual(first_payload["total_steps"], len(positions) - 1)
        self.assertEqual(first_payload["z_um"], positions[0])
        self.assertEqual(first_payload["distance_um"], 0.0)
        self.assertEqual(statuses[-1][1]["step_index"], len(positions) - 1)
        self.assertEqual(statuses[-1][1]["z_um"], positions[-1])

    def test_stays_at_final_position_not_start(self):
        from sim_control.models import ZScanConfig
        from sim_control.z_scan_core import run_z_scan_stage_only, scan_positions

        stage = _RecordingStage(start_um=0.0)
        stage.connect()
        cfg = ZScanConfig(start_um=0.0, direction="positive_z", step_um=1.0, num_steps=4)
        positions = scan_positions(cfg, stage_position_um=0.0)

        run_z_scan_stage_only(stage_adapter=stage, z_scan_config=cfg)

        self.assertEqual(stage.moves[-1], positions[-1])
        self.assertNotEqual(stage.moves[-1], positions[0])
        self.assertEqual(stage.get_position_um(), positions[-1])


class RunZScanAutofocusTests(unittest.TestCase):
    def _cfg(self):
        from sim_control.models import ZScanConfig

        return ZScanConfig(start_um=0.0, direction="positive_z", step_um=1.0, num_steps=3, exposure_preset_ms=8)

    def _common_kwargs(self, stage, camera, slm):
        from sim_control.models import CameraConfig, DaqLineConfig, TimingConfig

        return dict(
            stage_adapter=stage,
            camera_adapter=camera,
            slm_adapter=slm,
            daq_adapter=_FakeDaq(),
            daq_config=DaqLineConfig(),
            camera_config=CameraConfig(),
            timing=TimingConfig(),
            z_scan_config=self._cfg(),
        )

    def test_selects_running_order_and_delegates_to_run_z_scan(self):
        import sim_control.adapters as adapters_mod
        import sim_control.z_scan_core as zsc
        from sim_control.z_scan_core import ZScanResult, run_z_scan_autofocus

        stage = _RecordingStage(start_um=0.0)
        stage.connect()
        camera = _ConnectableCamera(connected=True)
        slm = _RoSlm(
            connected=True,
            running_orders=[
                (3, "561_3.5_2d_10ms"),
                (7, "561_3.5_2d_zscan3p_8ms"),
            ],
        )

        captured = {}
        sentinel = ZScanResult(best_z_um=1.0, focus_curve=[], exposure_actual_us=1000)

        def fake_run_z_scan(**kwargs):
            captured.update(kwargs)
            return sentinel

        orig_run = zsc.run_z_scan
        zsc.run_z_scan = fake_run_z_scan
        try:
            stop_event = object()
            result = run_z_scan_autofocus(
                **self._common_kwargs(stage, camera, slm),
                laser_wavelength_nm=561,
                stop_event=stop_event,
            )
        finally:
            zsc.run_z_scan = orig_run

        self.assertIs(result, sentinel)
        self.assertEqual(slm.selected, [7, 3])
        self.assertIs(captured["stage_adapter"], stage)
        self.assertIs(captured["camera_adapter"], camera)
        self.assertIs(captured["slm_adapter"], slm)
        self.assertIs(captured["stop_event"], stop_event)
        self.assertEqual(captured["laser_wavelength_nm"], 561)

    def test_scan_cancellation_or_failure_restores_formal_running_order(self):
        import sim_control.z_scan_core as zsc
        from sim_control.models import ZScanConfig
        from sim_control.z_scan_core import ZScanCancelled, run_z_scan_autofocus

        for error in (ZScanCancelled("cancelled"), RuntimeError("scan failed")):
            with self.subTest(error=type(error).__name__):
                stage = _RecordingStage(start_um=0.0)
                stage.connect()
                slm = _RoSlm(
                    connected=True,
                    running_orders=[
                        (3, "488_3.5_2d_10ms"),
                        (7, "488_3.5_2d_zscan3p_8ms"),
                    ],
                )
                original = zsc.run_z_scan
                zsc.run_z_scan = lambda **_kwargs: (_ for _ in ()).throw(error)
                try:
                    kwargs = self._common_kwargs(stage, _ConnectableCamera(True), slm)
                    kwargs["z_scan_config"] = ZScanConfig(
                        start_um=0.0, step_um=1.0, num_steps=1, exposure_preset_ms=8
                    )
                    with self.assertRaises(type(error)):
                        run_z_scan_autofocus(**kwargs)
                finally:
                    zsc.run_z_scan = original
                self.assertEqual(slm.selected, [7, 3])

    def test_restore_failure_is_reported_and_combined_with_scan_failure(self):
        import sim_control.z_scan_core as zsc
        from sim_control.errors import HardwareError
        from sim_control.z_scan_core import run_z_scan_autofocus

        class RestoreFailSlm(_RoSlm):
            def select_running_order(self, ro_index: int):
                self.selected.append(int(ro_index))
                if int(ro_index) == 3:
                    raise RuntimeError("formal restore failed")
                return {"ro_index": int(ro_index)}

        for scan_error in (None, RuntimeError("scan failed")):
            with self.subTest(scan_error=scan_error is not None):
                stage = _RecordingStage(start_um=0.0)
                stage.connect()
                slm = RestoreFailSlm(
                    connected=True,
                    running_orders=[
                        (3, "488_3.5_2d_10ms"),
                        (7, "488_3.5_2d_zscan3p_8ms"),
                    ],
                )
                original = zsc.run_z_scan
                if scan_error is None:
                    zsc.run_z_scan = lambda **_kwargs: object()
                else:
                    zsc.run_z_scan = lambda **_kwargs: (_ for _ in ()).throw(scan_error)
                try:
                    with self.assertRaisesRegex(HardwareError, "formal restore failed") as caught:
                        run_z_scan_autofocus(
                            **self._common_kwargs(stage, _ConnectableCamera(True), slm)
                        )
                finally:
                    zsc.run_z_scan = original
                if scan_error is not None:
                    self.assertIn("scan failed", str(caught.exception))
                self.assertEqual(slm.selected, [7, 3])

    def test_missing_formal_running_order_fails_before_zscan_selection_or_motion(self):
        import sim_control.z_scan_core as zsc
        from sim_control.errors import HardwareError
        from sim_control.z_scan_core import run_z_scan_autofocus

        stage = _RecordingStage(start_um=0.0)
        stage.connect()
        slm = _RoSlm(
            connected=True,
            running_orders=[(7, "488_3.5_2d_zscan3p_8ms")],
        )
        original = zsc.run_z_scan
        zsc.run_z_scan = lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("scan must not start without formal RO")
        )
        try:
            with self.assertRaises(HardwareError):
                run_z_scan_autofocus(**self._common_kwargs(stage, _ConnectableCamera(True), slm))
        finally:
            zsc.run_z_scan = original

        self.assertEqual(stage.moves, [])
        self.assertEqual(slm.selected, [])

    def test_no_matching_running_order_raises_hardware_error(self):
        import sim_control.adapters as adapters_mod
        import sim_control.z_scan_core as zsc
        from sim_control.errors import HardwareError
        from sim_control.z_scan_core import run_z_scan_autofocus

        stage = _RecordingStage(start_um=0.0)
        stage.connect()
        camera = _ConnectableCamera(connected=True)
        slm = _RoSlm(
            connected=True,
            running_orders=[(3, "488_3.5_2d_10ms")],
        )

        def fake_find(running_orders, wavelength_nm, exposure_preset_ms):
            return None, "", ["No matching z-scan running order found."]

        def fail_run_z_scan(**kwargs):
            raise AssertionError("run_z_scan must not be called when no RO matches")

        orig_run = zsc.run_z_scan
        orig_find = adapters_mod.find_z_scan_running_order
        zsc.run_z_scan = fail_run_z_scan
        adapters_mod.find_z_scan_running_order = fake_find
        try:
            with self.assertRaises(HardwareError):
                run_z_scan_autofocus(**self._common_kwargs(stage, camera, slm))
        finally:
            zsc.run_z_scan = orig_run
            adapters_mod.find_z_scan_running_order = orig_find

        self.assertEqual(slm.selected, [])

    def test_camera_not_connected_raises_hardware_error(self):
        from sim_control.errors import HardwareError
        from sim_control.z_scan_core import run_z_scan_autofocus

        stage = _RecordingStage(start_um=0.0)
        stage.connect()
        camera = _ConnectableCamera(connected=False)
        slm = _RoSlm(connected=True)

        with self.assertRaises(HardwareError):
            run_z_scan_autofocus(**self._common_kwargs(stage, camera, slm))

        self.assertEqual(slm.selected, [])

    def test_slm_not_connected_raises_hardware_error(self):
        from sim_control.errors import HardwareError
        from sim_control.z_scan_core import run_z_scan_autofocus

        stage = _RecordingStage(start_um=0.0)
        stage.connect()
        camera = _ConnectableCamera(connected=True)
        slm = _RoSlm(connected=False)

        with self.assertRaises(HardwareError):
            run_z_scan_autofocus(**self._common_kwargs(stage, camera, slm))

        self.assertEqual(slm.selected, [])

    def test_out_of_range_raises_before_connect_checks(self):
        from sim_control.errors import HardwareError
        from sim_control.z_scan_core import run_z_scan_autofocus

        # Narrow range so preflight fails; camera/SLM disconnected would also raise,
        # but preflight runs first, so a disconnected-camera HardwareError must not mask it.
        stage = _RecordingStage(start_um=0.0, min_um=0.0, max_um=1.0)
        stage.connect()
        camera = _ConnectableCamera(connected=True)
        slm = _RoSlm(connected=True)

        with self.assertRaises(HardwareError):
            run_z_scan_autofocus(**self._common_kwargs(stage, camera, slm))

        self.assertEqual(stage.moves, [])
        self.assertEqual(slm.selected, [])


if __name__ == "__main__":
    unittest.main()

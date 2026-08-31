"""Per-plane autofocus diagnostics for the Qt-free Z-scan core."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

from sim_control.errors import HardwareError
from sim_control.models import (
    CameraConfig,
    DaqLineConfig,
    PatternPreparationResult,
    SimTaskConfig,
    TimingConfig,
    ZScanConfig,
)


class _MeasuredStage:
    def __init__(self, *, offset_um: float = 0.05) -> None:
        self.is_connected = True
        self.position_um = 0.0
        self.offset_um = float(offset_um)
        self.moves: list[float] = []

    def connect(self) -> None:
        self.is_connected = True

    def get_position_um(self) -> float:
        return self.position_um

    def get_z_ranges_um(self) -> tuple[float, float]:
        return -10.0, 10.0

    def move_z_um(self, position_um: float) -> None:
        requested = float(position_um)
        self.moves.append(requested)
        if requested in (0.0, 1.0, 2.0):
            self.position_um = requested + self.offset_um
        else:
            self.position_um = requested


class _FocusCamera:
    def __init__(self, stage: _MeasuredStage, timestamp_lists: list[list[float]] | None = None) -> None:
        self.stage = stage
        self.timestamp_lists = timestamp_lists or [[101.0], [102.0], [103.0]]
        self.read_count = 0
        self.disarm_count = 0

    def is_connected(self) -> bool:
        return True

    def apply_config(self, _config: CameraConfig) -> dict[str, float]:
        return {"timing_readout_time_s": 0.0}

    def arm(self, _frame_count: int) -> None:
        return None

    def disarm(self) -> None:
        self.disarm_count += 1

    def read_frame_sequence(self, *_args, **_kwargs):
        timestamps = self.timestamp_lists[self.read_count]
        self.read_count += 1
        value = int(round(self.stage.get_position_um() * 100)) + 100
        return np.full((1, 4, 5), value, dtype=np.uint16), timestamps


class _Slm:
    def __init__(self) -> None:
        self.selected: list[int] = []

    def is_connected(self) -> bool:
        return True

    def list_running_orders(self):
        return [(3, "488_3.5_2d_30ms"), (7, "488_3.5_2d_zscan3p_8ms")]

    def select_running_order(self, index: int):
        self.selected.append(int(index))
        return {"ro_index": int(index)}

    def activate_prepared_patterns(self) -> None:
        return None


class _Daq:
    def __init__(self) -> None:
        self.low_count = 0

    def play_waveform(self, *_args, **_kwargs) -> None:
        return None

    def set_all_low(self, _device_name: str) -> None:
        self.low_count += 1


def _zscan_kwargs(stage: _MeasuredStage, camera: _FocusCamera, daq: _Daq):
    return dict(
        stage_adapter=stage,
        camera_adapter=camera,
        slm_adapter=_Slm(),
        daq_adapter=daq,
        daq_config=DaqLineConfig(),
        camera_config=CameraConfig(exposure_us=30_000),
        timing=TimingConfig(),
        z_scan_config=ZScanConfig(start_um=None, step_um=1.0, num_steps=2, exposure_preset_ms=8),
        task_id="focus-parent",
        laser_wavelength_nm=561,
    )


class FocusFrameRecordTests(unittest.TestCase):
    def test_records_requested_and_measured_positions_and_uses_measured_tie_first(self) -> None:
        from sim_control.z_scan_core import run_z_scan

        stage = _MeasuredStage()
        camera = _FocusCamera(stage)
        daq = _Daq()
        records = []
        statuses = []

        with mock.patch("sim_control.z_scan_core.sum_modified_laplacian", side_effect=[5.0, 5.0, 1.0]):
            result = run_z_scan(
                **_zscan_kwargs(stage, camera, daq),
                on_focus_frame=records.append,
                on_status=lambda event, payload: statuses.append((event, payload)),
            )

        self.assertEqual(len(records), 3)
        self.assertEqual([record.task_id for record in records], ["focus-parent"] * 3)
        self.assertEqual([record.plane_index for record in records], [0, 1, 2])
        self.assertEqual([record.total_layers for record in records], [3, 3, 3])
        self.assertEqual([record.requested_z_um for record in records], [0.0, 1.0, 2.0])
        self.assertEqual([record.measured_z_um for record in records], [0.05, 1.05, 2.05])
        self.assertEqual([record.wavelength_nm for record in records], [561, 561, 561])
        self.assertEqual([record.exposure_actual_us for record in records], [7_884, 7_884, 7_884])
        self.assertEqual([record.timestamp for record in records], [101.0, 102.0, 103.0])
        self.assertEqual([record.score for record in records], [5.0, 5.0, 1.0])
        self.assertTrue(all(record.frame.dtype == np.uint16 for record in records))
        self.assertTrue(all(record.frame.shape == (4, 5) for record in records))
        self.assertTrue(all(not record.frame.flags.writeable for record in records))
        self.assertEqual([point.z_um for point in result.focus_curve], [0.05, 1.05, 2.05])
        self.assertEqual(result.best_z_um, 0.05)
        self.assertEqual(stage.moves, [0.0, 1.0, 2.0, 0.05])
        self.assertIsNone(result.captured_stack)
        progress = [payload for event, payload in statuses if event == "z_scan_progress"]
        self.assertEqual(progress[0]["task_id"], "focus-parent")
        self.assertEqual(progress[0]["plane_index"], 0)
        self.assertEqual(progress[0]["requested_z_um"], 0.0)
        self.assertEqual(progress[0]["measured_z_um"], 0.05)
        self.assertEqual(progress[0]["layer_shape"], [4, 5])

    def test_camera_must_return_exactly_one_timestamp_per_focus_plane(self) -> None:
        from sim_control.z_scan_core import run_z_scan

        for timestamps in ([], [1.0, 2.0]):
            with self.subTest(timestamps=timestamps):
                stage = _MeasuredStage()
                camera = _FocusCamera(stage, timestamp_lists=[timestamps])
                daq = _Daq()
                records = []
                with self.assertRaisesRegex(HardwareError, "timestamp"):
                    run_z_scan(
                        **_zscan_kwargs(stage, camera, daq),
                        on_focus_frame=records.append,
                    )
                self.assertEqual(records, [])
                self.assertGreaterEqual(camera.disarm_count, 1)
                self.assertGreaterEqual(daq.low_count, 1)

    def test_focus_camera_must_return_uint16_without_silent_cast(self) -> None:
        from sim_control.z_scan_core import run_z_scan

        stage = _MeasuredStage()
        camera = _FocusCamera(stage)
        camera.read_frame_sequence = mock.Mock(
            return_value=(np.zeros((1, 4, 5), dtype=np.float32), [101.0])
        )
        records = []

        with self.assertRaisesRegex(HardwareError, "uint16"):
            run_z_scan(
                **_zscan_kwargs(stage, camera, _Daq()),
                on_focus_frame=records.append,
            )

        self.assertEqual(records, [])

    def test_focus_callback_error_propagates_without_being_silenced(self) -> None:
        from sim_control.z_scan_core import run_z_scan

        stage = _MeasuredStage()
        camera = _FocusCamera(stage)
        daq = _Daq()
        diagnostic_error = RuntimeError("diagnostic sink failed")

        def fail_callback(_record) -> None:
            raise diagnostic_error

        with mock.patch("sim_control.z_scan_core.sum_modified_laplacian", return_value=1.0):
            with self.assertRaises(RuntimeError) as caught:
                run_z_scan(
                    **_zscan_kwargs(stage, camera, daq),
                    on_focus_frame=fail_callback,
                )

        self.assertIs(caught.exception, diagnostic_error)
        self.assertGreaterEqual(camera.disarm_count, 1)
        self.assertGreaterEqual(daq.low_count, 1)

    def test_run_z_scan_autofocus_forwards_task_and_focus_callback(self) -> None:
        from sim_control.z_scan_core import ZScanResult, run_z_scan_autofocus

        stage = _MeasuredStage(offset_um=0.0)
        camera = _FocusCamera(stage)
        slm = _Slm()
        callback = mock.Mock()
        sentinel = ZScanResult(best_z_um=0.0, focus_curve=[], exposure_actual_us=7_884)

        with (
            mock.patch("sim_control.adapters.find_best_running_order", return_value=(3, "formal", [])),
            mock.patch("sim_control.adapters.find_z_scan_running_order", return_value=(7, "focus", [])),
            mock.patch("sim_control.z_scan_core.run_z_scan", return_value=sentinel) as scan,
        ):
            result = run_z_scan_autofocus(
                stage_adapter=stage,
                camera_adapter=camera,
                slm_adapter=slm,
                daq_adapter=_Daq(),
                daq_config=DaqLineConfig(),
                camera_config=CameraConfig(),
                timing=TimingConfig(),
                z_scan_config=ZScanConfig(start_um=None, step_um=1.0, num_steps=1),
                task_id="focus-wrapper",
                on_focus_frame=callback,
            )

        self.assertIs(result, sentinel)
        self.assertEqual(scan.call_args.kwargs["task_id"], "focus-wrapper")
        self.assertIs(scan.call_args.kwargs["on_focus_frame"], callback)

    def test_run_single_acquisition_forwards_parent_task_and_focus_callback(self) -> None:
        from sim_control.acquisition_core import run_single_acquisition
        from sim_control.z_scan_core import ZScanResult

        stage = _MeasuredStage(offset_um=0.0)
        camera = mock.Mock()
        camera.read_frame_sequence.return_value = (
            np.zeros((9, 4, 5), dtype=np.uint16),
            [float(index) for index in range(9)],
        )
        builder = mock.Mock()
        builder.build.return_value = SimpleNamespace(
            sample_count=1,
            duration_s=0.001,
            warnings=[],
            metadata={},
        )
        callback = mock.Mock()

        with mock.patch(
            "sim_control.acquisition_core.run_z_scan",
            return_value=ZScanResult(best_z_um=0.0, focus_curve=[], exposure_actual_us=7_884),
        ) as scan:
            run_single_acquisition(
                task=SimTaskConfig(),
                daq_config=DaqLineConfig(),
                pattern_result=PatternPreparationResult(
                    pattern_files=["formal"] * 9,
                    handles=[-1],
                    metadata={"mode": "running_order", "running_order_index": 3},
                ),
                camera=camera,
                slm=mock.Mock(),
                daq=mock.Mock(),
                waveform_builder=builder,
                task_id="formal-parent",
                stage_adapter=stage,
                z_scan_config=ZScanConfig(enabled=True, start_um=None, step_um=1.0, num_steps=1),
                z_scan_pattern_result=PatternPreparationResult(handles=[-1]),
                on_focus_frame=callback,
            )

        self.assertEqual(scan.call_args.kwargs["task_id"], "formal-parent")
        self.assertIs(scan.call_args.kwargs["on_focus_frame"], callback)


class CameraConfigRestoreTests(unittest.TestCase):
    def _run_single_case(
        self,
        *,
        scan_error: Exception | None = None,
        restore_error: Exception | None = None,
        applied_exposures: list[int] | None = None,
        running_order_restore_error: Exception | None = None,
        on_status=None,
    ):
        from sim_control.acquisition_core import run_single_acquisition
        from sim_control.z_scan_core import ZScanResult

        formal_camera_config = CameraConfig(exposure_us=30_000)
        camera = mock.Mock()
        camera.read_frame_sequence.return_value = (
            np.zeros((9, 4, 5), dtype=np.uint16),
            [float(index) for index in range(9)],
        )
        exposures = applied_exposures if applied_exposures is not None else []

        def apply_config(config: CameraConfig):
            exposures.append(int(config.exposure_us))
            if (
                restore_error is not None
                and len(exposures) >= 2
                and int(config.exposure_us) == formal_camera_config.exposure_us
            ):
                raise restore_error
            return {"timing_readout_time_s": 0.0}

        camera.apply_config.side_effect = apply_config
        builder = mock.Mock()
        builder.build.return_value = SimpleNamespace(
            sample_count=1,
            duration_s=0.001,
            warnings=[],
            metadata={},
        )
        stage = _MeasuredStage(offset_um=0.0)
        statuses = []
        slm = mock.Mock()
        if running_order_restore_error is not None:
            slm.select_running_order.side_effect = running_order_restore_error

        def fake_scan(**_kwargs):
            camera.apply_config(CameraConfig(exposure_us=7_884))
            if scan_error is not None:
                raise scan_error
            return ZScanResult(best_z_um=0.0, focus_curve=[], exposure_actual_us=7_884)

        with mock.patch("sim_control.acquisition_core.run_z_scan", side_effect=fake_scan):
            result = run_single_acquisition(
                task=SimTaskConfig(camera=formal_camera_config),
                daq_config=DaqLineConfig(),
                pattern_result=PatternPreparationResult(
                    pattern_files=["formal"] * 9,
                    handles=[-1],
                    metadata={"mode": "running_order", "running_order_index": 3},
                ),
                camera=camera,
                slm=slm,
                daq=mock.Mock(),
                waveform_builder=builder,
                task_id="formal-restore",
                stage_adapter=stage,
                z_scan_config=ZScanConfig(enabled=True, start_um=None, step_um=1.0, num_steps=1),
                z_scan_pattern_result=PatternPreparationResult(handles=[-1]),
                on_status=on_status or (lambda event, payload: statuses.append((event, payload))),
            )
        return result, exposures, statuses

    def test_run_single_success_restores_formal_camera_config_before_final_sim9(self) -> None:
        result, exposures, _statuses = self._run_single_case()

        self.assertEqual(result.stack.shape, (9, 4, 5))
        self.assertEqual(exposures, [7_884, 30_000])

    def test_run_single_failure_restores_formal_camera_config_and_preserves_error(self) -> None:
        original = RuntimeError("focus scan failed")
        exposures: list[int] = []

        with self.assertRaises(RuntimeError) as caught:
            self._run_single_case(scan_error=original, applied_exposures=exposures)

        self.assertIs(caught.exception, original)
        self.assertEqual(exposures, [7_884, 30_000])

    def test_run_single_cancel_restores_formal_camera_config(self) -> None:
        from sim_control.acquisition_core import AcquisitionCancelled
        from sim_control.z_scan_core import ZScanCancelled

        exposures: list[int] = []
        with self.assertRaises(AcquisitionCancelled):
            self._run_single_case(
                scan_error=ZScanCancelled("operator stop"),
                applied_exposures=exposures,
            )
        self.assertEqual(exposures, [7_884, 30_000])

    def test_run_single_restore_failure_reports_both_errors_with_original_as_cause(self) -> None:
        original = RuntimeError("focus scan failed")
        restore_error = RuntimeError("formal camera restore failed")

        with self.assertRaisesRegex(HardwareError, "formal camera restore failed") as caught:
            self._run_single_case(scan_error=original, restore_error=restore_error)

        self.assertIn("focus scan failed", str(caught.exception))
        self.assertIs(caught.exception.__cause__, original)

    def test_run_single_restore_warning_callback_cannot_mask_original_or_skip_camera_restore(self) -> None:
        original = RuntimeError("focus scan failed")
        exposures: list[int] = []

        def fail_warning_status(event, _payload) -> None:
            if event == "running_order_restore_warning":
                raise RuntimeError("status callback failed")

        with self.assertRaises(HardwareError) as caught:
            self._run_single_case(
                scan_error=original,
                running_order_restore_error=RuntimeError("formal RO restore failed"),
                applied_exposures=exposures,
                on_status=fail_warning_status,
            )

        self.assertIs(caught.exception.__cause__, original)
        self.assertIn("formal RO restore failed", str(caught.exception))
        self.assertEqual(exposures, [7_884, 30_000])

    def _run_autofocus_wrapper_case(
        self,
        *,
        scan_error: Exception | None = None,
        restore_error: Exception | None = None,
        applied_exposures: list[int] | None = None,
        slm_adapter=None,
        on_status=None,
    ):
        from sim_control.z_scan_core import ZScanResult, run_z_scan_autofocus

        formal_camera_config = CameraConfig(exposure_us=30_000)
        stage = _MeasuredStage(offset_um=0.0)
        camera = _FocusCamera(stage)
        exposures = applied_exposures if applied_exposures is not None else []

        def apply_config(config: CameraConfig):
            exposures.append(int(config.exposure_us))
            if (
                restore_error is not None
                and len(exposures) >= 2
                and int(config.exposure_us) == formal_camera_config.exposure_us
            ):
                raise restore_error
            return {"timing_readout_time_s": 0.0}

        camera.apply_config = apply_config

        def fake_scan(**_kwargs):
            camera.apply_config(CameraConfig(exposure_us=7_884))
            if scan_error is not None:
                raise scan_error
            return ZScanResult(best_z_um=0.0, focus_curve=[], exposure_actual_us=7_884)

        with (
            mock.patch("sim_control.adapters.find_best_running_order", return_value=(3, "formal", [])),
            mock.patch("sim_control.adapters.find_z_scan_running_order", return_value=(7, "focus", [])),
            mock.patch("sim_control.z_scan_core.run_z_scan", side_effect=fake_scan),
        ):
            result = run_z_scan_autofocus(
                stage_adapter=stage,
                camera_adapter=camera,
                slm_adapter=slm_adapter or _Slm(),
                daq_adapter=_Daq(),
                daq_config=DaqLineConfig(),
                camera_config=formal_camera_config,
                timing=TimingConfig(),
                z_scan_config=ZScanConfig(start_um=None, step_um=1.0, num_steps=1),
                on_status=on_status,
            )
        return result, exposures

    def test_autofocus_wrapper_restores_camera_after_success_cancel_and_failure(self) -> None:
        from sim_control.z_scan_core import ZScanCancelled

        cases = [None, ZScanCancelled("operator stop"), RuntimeError("focus scan failed")]
        for scan_error in cases:
            with self.subTest(scan_error=type(scan_error).__name__ if scan_error else "success"):
                exposures: list[int] = []
                if scan_error is None:
                    self._run_autofocus_wrapper_case(applied_exposures=exposures)
                else:
                    with self.assertRaises(type(scan_error)) as caught:
                        self._run_autofocus_wrapper_case(
                            scan_error=scan_error,
                            applied_exposures=exposures,
                        )
                    self.assertIs(caught.exception, scan_error)
                self.assertEqual(exposures, [7_884, 30_000])

    def test_autofocus_wrapper_restore_failure_combines_original_error(self) -> None:
        original = RuntimeError("focus scan failed")
        restore_error = RuntimeError("formal camera restore failed")

        with self.assertRaisesRegex(HardwareError, "formal camera restore failed") as caught:
            self._run_autofocus_wrapper_case(
                scan_error=original,
                restore_error=restore_error,
            )

        self.assertIn("focus scan failed", str(caught.exception))
        self.assertIs(caught.exception.__cause__, original)

    def test_autofocus_restore_warning_callback_cannot_mask_original_or_skip_camera_restore(self) -> None:
        original = RuntimeError("focus scan failed")
        exposures: list[int] = []

        class RestoreFailSlm(_Slm):
            def select_running_order(self, index: int):
                if int(index) == 3:
                    raise RuntimeError("formal RO restore failed")
                return super().select_running_order(index)

        def fail_warning_status(event, _payload) -> None:
            if event == "running_order_restore_warning":
                raise RuntimeError("status callback failed")

        with self.assertRaises(HardwareError) as caught:
            self._run_autofocus_wrapper_case(
                scan_error=original,
                applied_exposures=exposures,
                slm_adapter=RestoreFailSlm(),
                on_status=fail_warning_status,
            )

        self.assertIs(caught.exception.__cause__, original)
        self.assertIn("formal RO restore failed", str(caught.exception))
        self.assertEqual(exposures, [7_884, 30_000])


if __name__ == "__main__":
    unittest.main()

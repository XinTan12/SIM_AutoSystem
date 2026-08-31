"""Qt-free Z-stack acquisition orchestration tests."""

from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

from sim_control.errors import HardwareError
from sim_control.models import (
    AcquisitionBatch,
    DaqLineConfig,
    PatternPreparationResult,
    SimTaskConfig,
    ZScanConfig,
)


class _Stage:
    def __init__(
        self,
        *,
        start_um: float = 10.0,
        min_um: float = 0.0,
        max_um: float = 100.0,
        readback_offset_um: float = 0.0,
    ) -> None:
        self.is_connected = True
        self.position_um = float(start_um)
        self.min_um = float(min_um)
        self.max_um = float(max_um)
        self.readback_offset_um = float(readback_offset_um)
        self.moves: list[float] = []

    def connect(self) -> None:
        self.is_connected = True

    def get_position_um(self) -> float:
        return self.position_um

    def get_z_ranges_um(self) -> tuple[float, float]:
        return self.min_um, self.max_um

    def move_z_um(self, position_um: float) -> None:
        requested = float(position_um)
        self.moves.append(requested)
        self.position_um = requested + self.readback_offset_um


class _Camera:
    def __init__(self) -> None:
        self.disarm_count = 0

    def disarm(self) -> None:
        self.disarm_count += 1


class _Daq:
    def __init__(self) -> None:
        self.set_low_count = 0

    def set_all_low(self, _device_name: str) -> None:
        self.set_low_count += 1


class _LayerSink:
    def __init__(self, *, fail_at: int | None = None, error: Exception | None = None) -> None:
        self.fail_at = fail_at
        self.error = error or RuntimeError("sink rejected layer")
        self.submissions: list[dict[str, object]] = []

    def submit_z_layer(self, **kwargs: object) -> None:
        if self.fail_at is not None and int(kwargs["plane_index"]) == self.fail_at:
            raise self.error
        self.submissions.append(kwargs)


def _plan() -> SimpleNamespace:
    return SimpleNamespace(sample_count=1, duration_s=0.001, warnings=[], metadata={})


def _batch(task_id: str, value: int = 1, *, shape: tuple[int, ...] = (9, 4, 5), dtype=np.uint16):
    stack = np.full(shape, value, dtype=dtype)
    return AcquisitionBatch(
        task_id=task_id,
        stack=stack,
        timestamps=[float(index) for index in range(9)],
        laser_wavelength_nm=488,
        exposure_us=30_000,
        pattern_files=["formal"] * 9,
    )


class ZStackAcquisitionCoreTests(unittest.TestCase):
    def _kwargs(self, stage: _Stage, camera: _Camera, daq: _Daq, sink: _LayerSink):
        return dict(
            task=SimTaskConfig(),
            daq_config=DaqLineConfig(),
            pattern_result=PatternPreparationResult(pattern_files=["formal"] * 9),
            camera=camera,
            slm=mock.Mock(),
            daq=daq,
            stage_adapter=stage,
            z_scan_config=ZScanConfig(
                start_um=999.0,
                direction="positive_z",
                step_um=1.0,
                num_steps=2,
            ),
            layer_sink=sink,
            waveform_builder=mock.Mock(build=mock.Mock(return_value=_plan())),
            task_id="parent-task",
        )

    def test_captures_exactly_one_valid_sim9_batch_per_requested_layer(self) -> None:
        from sim_control.acquisition_core import run_z_stack_acquisition

        stage = _Stage(readback_offset_um=0.1)
        camera = _Camera()
        daq = _Daq()
        sink = _LayerSink()
        calls: list[dict[str, object]] = []
        statuses: list[tuple[str, dict[str, object]]] = []

        def fake_single(**kwargs):
            calls.append(kwargs)
            return _batch(str(kwargs["task_id"]), value=len(calls))

        with mock.patch("sim_control.acquisition_core.run_single_acquisition", side_effect=fake_single):
            result = run_z_stack_acquisition(
                **self._kwargs(stage, camera, daq, sink),
                on_status=lambda event, payload: statuses.append((event, payload)),
            )

        self.assertEqual(len(calls), 3)
        self.assertEqual(stage.moves, [10.0, 11.0, 12.0])
        self.assertEqual(result.requested_z_um, (10.0, 11.0, 12.0))
        self.assertEqual(result.measured_z_um, (10.1, 11.1, 12.1))
        self.assertEqual(result.completed_layers, 3)
        self.assertEqual(result.total_layers, 3)
        self.assertEqual(result.layer_shape, (9, 4, 5))
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.output_paths, ())
        self.assertFalse(hasattr(result, "stack"))
        self.assertEqual(
            [str(call["task_id"]) for call in calls],
            ["parent-task:z0000", "parent-task:z0001", "parent-task:z0002"],
        )
        self.assertTrue(all(call["z_scan_config"] is None for call in calls))
        self.assertEqual(len(sink.submissions), 3)
        for plane_index, submission in enumerate(sink.submissions):
            frames = submission["frames"]
            self.assertIsInstance(frames, np.ndarray)
            self.assertEqual(frames.shape, (9, 4, 5))
            self.assertEqual(frames.dtype, np.uint16)
            self.assertEqual(submission["plane_index"], plane_index)
            self.assertEqual(submission["batch_id"], f"parent-task:z{plane_index:04d}")
        accepted = [payload for event, payload in statuses if event == "z_stack_layer_accepted"]
        self.assertEqual(len(accepted), 3)
        self.assertEqual(accepted[1]["parent_task_id"], "parent-task")
        self.assertEqual(accepted[1]["plane_index"], 1)
        self.assertEqual(accepted[1]["requested_z_um"], 11.0)
        self.assertEqual(accepted[1]["measured_z_um"], 11.1)
        self.assertEqual(accepted[1]["layer_shape"], [9, 4, 5])

    def test_waits_for_sink_acceptance_before_starting_the_next_layer(self) -> None:
        from sim_control.acquisition_core import run_z_stack_acquisition

        order: list[str] = []
        stage = _Stage()
        camera = _Camera()
        daq = _Daq()

        class OrderedSink(_LayerSink):
            def submit_z_layer(self, **kwargs: object) -> None:
                order.append(f"sink-{kwargs['plane_index']}")
                super().submit_z_layer(**kwargs)

        sink = OrderedSink()

        def fake_single(**kwargs):
            order.append(f"capture-{len(order) // 2}")
            return _batch(str(kwargs["task_id"]))

        with mock.patch("sim_control.acquisition_core.run_single_acquisition", side_effect=fake_single):
            run_z_stack_acquisition(**self._kwargs(stage, camera, daq, sink))

        self.assertEqual(order, ["capture-0", "sink-0", "capture-1", "sink-1", "capture-2", "sink-2"])

    def test_sink_failure_preserves_original_error_and_reports_only_accepted_layers(self) -> None:
        from sim_control.acquisition_core import ZStackAcquisitionFailed, run_z_stack_acquisition

        original = RuntimeError("bounded queue rejected layer")
        stage = _Stage(readback_offset_um=0.1)
        camera = _Camera()
        daq = _Daq()
        sink = _LayerSink(fail_at=1, error=original)

        with mock.patch(
            "sim_control.acquisition_core.run_single_acquisition",
            side_effect=lambda **kwargs: _batch(str(kwargs["task_id"])),
        ):
            with self.assertRaises(ZStackAcquisitionFailed) as caught:
                run_z_stack_acquisition(**self._kwargs(stage, camera, daq, sink))

        self.assertIs(caught.exception.__cause__, original)
        partial = caught.exception.partial_result
        self.assertEqual(partial.status, "failed")
        self.assertEqual(partial.completed_layers, 1)
        self.assertEqual(partial.measured_z_um, (10.1,))
        self.assertEqual(stage.moves, [10.0, 11.0])
        self.assertGreaterEqual(camera.disarm_count, 1)
        self.assertGreaterEqual(daq.set_low_count, 1)

    def test_completed_background_writer_failure_is_checked_before_next_stage_move(self) -> None:
        from sim_control.acquisition_core import ZStackAcquisitionFailed, run_z_stack_acquisition

        original = RuntimeError("background writer failed after accepting plane 0")
        stage = _Stage()
        camera = _Camera()
        daq = _Daq()

        class HealthCheckedSink(_LayerSink):
            def __init__(self) -> None:
                super().__init__()
                self.health_checks = 0

            def check_health(self) -> None:
                self.health_checks += 1
                if self.health_checks == 2:
                    raise original

        sink = HealthCheckedSink()
        with mock.patch(
            "sim_control.acquisition_core.run_single_acquisition",
            side_effect=lambda **kwargs: _batch(str(kwargs["task_id"])),
        ):
            with self.assertRaises(ZStackAcquisitionFailed) as caught:
                run_z_stack_acquisition(**self._kwargs(stage, camera, daq, sink))

        self.assertIs(caught.exception.__cause__, original)
        self.assertEqual(caught.exception.partial_result.completed_layers, 1)
        self.assertEqual(stage.moves, [10.0])
        self.assertEqual(len(sink.submissions), 1)

    def test_stop_after_an_accepted_layer_reports_partial_cancel_and_leaves_current_z(self) -> None:
        from sim_control.acquisition_core import (
            AcquisitionCancelled,
            ZStackAcquisitionCancelled,
            run_z_stack_acquisition,
        )

        stop_event = threading.Event()
        stage = _Stage()
        camera = _Camera()
        daq = _Daq()

        class CancellingSink(_LayerSink):
            def submit_z_layer(self, **kwargs: object) -> None:
                super().submit_z_layer(**kwargs)
                stop_event.set()

        sink = CancellingSink()
        with mock.patch(
            "sim_control.acquisition_core.run_single_acquisition",
            side_effect=lambda **kwargs: _batch(str(kwargs["task_id"])),
        ):
            with self.assertRaises(ZStackAcquisitionCancelled) as caught:
                run_z_stack_acquisition(
                    **self._kwargs(stage, camera, daq, sink),
                    stop_event=stop_event,
                )

        self.assertIsInstance(caught.exception, AcquisitionCancelled)
        partial = caught.exception.partial_result
        self.assertEqual(partial.status, "cancelled")
        self.assertEqual(partial.completed_layers, 1)
        self.assertEqual(partial.measured_z_um, (10.0,))
        self.assertEqual(stage.moves, [10.0])
        self.assertEqual(stage.get_position_um(), 10.0)
        self.assertGreaterEqual(camera.disarm_count, 1)
        self.assertGreaterEqual(daq.set_low_count, 1)

    def test_stop_aware_sink_cancellation_propagates_as_partial_cancel(self) -> None:
        from sim_control.acquisition_core import ZStackAcquisitionCancelled, run_z_stack_acquisition

        stop_event = threading.Event()
        sink_error = RuntimeError("writer cancelled while waiting for queue space")
        stage = _Stage()
        camera = _Camera()
        daq = _Daq()

        class StopAwareSink(_LayerSink):
            def submit_z_layer(self, **kwargs: object) -> None:
                self.submissions.append(kwargs)
                if int(kwargs["plane_index"]) == 1:
                    stop_event.set()
                    raise sink_error

        sink = StopAwareSink()
        with mock.patch(
            "sim_control.acquisition_core.run_single_acquisition",
            side_effect=lambda **kwargs: _batch(str(kwargs["task_id"])),
        ):
            with self.assertRaises(ZStackAcquisitionCancelled) as caught:
                run_z_stack_acquisition(
                    **self._kwargs(stage, camera, daq, sink),
                    stop_event=stop_event,
                )

        self.assertIs(caught.exception.__cause__, sink_error)
        self.assertEqual(caught.exception.partial_result.status, "cancelled")
        self.assertEqual(caught.exception.partial_result.completed_layers, 1)
        self.assertIs(sink.submissions[0]["stop_event"], stop_event)

    def test_invalid_layer_shape_or_dtype_is_rejected_before_sink_acceptance(self) -> None:
        from sim_control.acquisition_core import ZStackAcquisitionFailed, run_z_stack_acquisition

        invalid_arrays = [
            ((8, 4, 5), np.uint16),
            ((9, 4, 5), np.float32),
        ]
        for shape, dtype in invalid_arrays:
            with self.subTest(shape=shape, dtype=dtype):
                stage = _Stage()
                camera = _Camera()
                daq = _Daq()
                sink = _LayerSink()
                with mock.patch(
                    "sim_control.acquisition_core.run_single_acquisition",
                    side_effect=lambda **kwargs: _batch(str(kwargs["task_id"]), shape=shape, dtype=dtype),
                ):
                    with self.assertRaises(ZStackAcquisitionFailed) as caught:
                        run_z_stack_acquisition(**self._kwargs(stage, camera, daq, sink))

                self.assertIsInstance(caught.exception.__cause__, HardwareError)
                self.assertEqual(caught.exception.partial_result.completed_layers, 0)
                self.assertEqual(sink.submissions, [])

    def test_out_of_range_preflight_fails_before_motion_or_hardware_use(self) -> None:
        from sim_control.acquisition_core import run_z_stack_acquisition

        stage = _Stage(start_um=0.0, min_um=0.0, max_um=1.0)
        camera = _Camera()
        daq = _Daq()
        sink = _LayerSink()
        kwargs = self._kwargs(stage, camera, daq, sink)
        kwargs["z_scan_config"] = ZScanConfig(start_um=None, step_um=2.0, num_steps=1)

        with mock.patch("sim_control.acquisition_core.run_single_acquisition") as single:
            with self.assertRaises(HardwareError):
                run_z_stack_acquisition(**kwargs)

        single.assert_not_called()
        self.assertEqual(stage.moves, [])
        self.assertEqual(camera.disarm_count, 0)
        self.assertEqual(daq.set_low_count, 0)
        self.assertEqual(sink.submissions, [])


class RunSingleAutofocusPreflightTests(unittest.TestCase):
    def test_out_of_range_autofocus_fails_before_run_z_scan_or_hardware_actions(self) -> None:
        from sim_control.acquisition_core import run_single_acquisition

        stage = _Stage(start_um=0.0, min_um=0.0, max_um=1.0)
        camera = mock.Mock()
        slm = mock.Mock()
        daq = mock.Mock()
        builder = mock.Mock()
        builder.build.return_value = _plan()

        with mock.patch("sim_control.acquisition_core.run_z_scan") as run_z_scan:
            with self.assertRaises(HardwareError):
                run_single_acquisition(
                    task=SimTaskConfig(),
                    daq_config=DaqLineConfig(),
                    pattern_result=PatternPreparationResult(
                        handles=[-1],
                        metadata={"mode": "running_order", "running_order_index": 3},
                    ),
                    camera=camera,
                    slm=slm,
                    daq=daq,
                    waveform_builder=builder,
                    stage_adapter=stage,
                    z_scan_config=ZScanConfig(enabled=True, start_um=None, step_um=2.0, num_steps=1),
                    z_scan_pattern_result=PatternPreparationResult(handles=[-1]),
                )

        run_z_scan.assert_not_called()
        self.assertEqual(stage.moves, [])
        camera.apply_config.assert_not_called()
        camera.arm.assert_not_called()
        daq.play_waveform.assert_not_called()


if __name__ == "__main__":
    unittest.main()

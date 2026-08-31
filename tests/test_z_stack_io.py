from __future__ import annotations

import csv
import gc
import json
import os
import threading
import time
import unittest
import weakref
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import numpy as np
import tifffile

from sim_control.z_stack_io import (
    SeriesWriteCancelled,
    SeriesWriteError,
    SeriesWriteJob,
    SeriesTerminalInProgress,
    SeriesWriteTimeout,
    ZStackAsyncWriter,
    _Command,
)


def _stack(value: int, shape: tuple[int, int] = (4, 5)) -> np.ndarray:
    return np.full((9, *shape), value, dtype=np.uint16)


def _frame(value: int, shape: tuple[int, int] = (4, 5)) -> np.ndarray:
    return np.full(shape, value, dtype=np.uint16)


class TestZStackAsyncWriter(unittest.TestCase):
    def test_submit_rejects_immediately_while_terminal_waiter_holds_operation_lock(self) -> None:
        with TemporaryDirectory() as tmp:
            writer = ZStackAsyncWriter(sync_timeout_s=2.0, enqueue_poll_s=0.01)
            entered = threading.Event()
            release = threading.Event()
            original_merge = writer._merge_z_stack

            def delayed_merge(*args, **kwargs):
                entered.set()
                self.assertTrue(release.wait(2.0))
                return original_merge(*args, **kwargs)

            writer._merge_z_stack = delayed_merge
            writer.begin_job(
                SeriesWriteJob("terminal-live", "zstack", Path(tmp) / "live.tif", 2, (4, 5), 488, 30_000, "formal")
            )
            writer.submit_z_layer(0, _stack(1), 0.0, 0.0, "b0")
            finalize_errors: list[BaseException] = []

            def finalize() -> None:
                try:
                    writer.finalize("complete", timeout=2.0)
                except BaseException as exc:
                    finalize_errors.append(exc)

            terminal_waiter = threading.Thread(target=finalize)
            terminal_waiter.start()
            self.assertTrue(entered.wait(1.0))
            started = time.monotonic()
            with self.assertRaises(SeriesTerminalInProgress):
                writer.submit_z_layer(1, _stack(2), 0.3, 0.3, "b1", timeout=0.2)
            self.assertLess(time.monotonic() - started, 0.1)
            release.set()
            terminal_waiter.join(2.0)
            self.assertFalse(finalize_errors)
            writer.shutdown()

    def test_finalize_pending_rejects_z_submit_without_copy_or_count_change(self) -> None:
        with TemporaryDirectory() as tmp:
            writer = ZStackAsyncWriter(sync_timeout_s=0.05, enqueue_poll_s=0.01)
            entered = threading.Event()
            release = threading.Event()
            original_merge = writer._merge_z_stack

            def delayed_merge(*args, **kwargs):
                entered.set()
                self.assertTrue(release.wait(2.0))
                return original_merge(*args, **kwargs)

            writer._merge_z_stack = delayed_merge
            writer.begin_job(
                SeriesWriteJob("terminal-z", "zstack", Path(tmp) / "terminal.tif", 2, (4, 5), 488, 30_000, "formal")
            )
            writer.submit_z_layer(0, _stack(1), 0.0, 0.0, "b0")
            with self.assertRaises(SeriesWriteTimeout):
                writer.finalize("complete", timeout=0.05)
            self.assertTrue(entered.is_set())
            queue_size = writer._queue.qsize()
            rejected = _stack(2)
            rejected_ref = weakref.ref(rejected)
            with mock.patch("sim_control.z_stack_io.np.array") as copy_mock:
                with self.assertRaisesRegex(SeriesTerminalInProgress, "terminal.*progress"):
                    writer.submit_z_layer(1, rejected, 0.3, 0.3, "b1")
                copy_mock.assert_not_called()
            self.assertEqual(writer._queue.qsize(), queue_size)
            health = writer.check_health()
            self.assertEqual((health.submitted_layers, health.committed_layers, health.pending_layers), (1, 1, 0))
            del rejected
            gc.collect()
            self.assertIsNone(rejected_ref())

            release.set()
            writer.wait_for_terminal(timeout=2.0)
            writer.shutdown()

    def test_shutdown_pending_rejects_focus_submit_without_copy_or_count_change(self) -> None:
        with TemporaryDirectory() as tmp:
            writer = ZStackAsyncWriter(sync_timeout_s=0.05, enqueue_poll_s=0.01)
            entered = threading.Event()
            release = threading.Event()
            original_finalize = writer._finalize_state

            def delayed_finalize(*args, **kwargs):
                entered.set()
                self.assertTrue(release.wait(2.0))
                return original_finalize(*args, **kwargs)

            writer._finalize_state = delayed_finalize
            writer.begin_job(
                SeriesWriteJob(
                    "terminal-focus",
                    "focus",
                    Path(tmp) / "focus.tif",
                    2,
                    (4, 5),
                    488,
                    4_884,
                    "focus",
                    Path(tmp) / "focus.csv",
                )
            )
            writer.submit_focus_layer(0, _frame(1), 0.0, 0.0, 1.0)
            with self.assertRaises(SeriesWriteTimeout):
                writer.shutdown(timeout=0.05)
            self.assertTrue(entered.is_set())
            queue_size = writer._queue.qsize()
            rejected = _frame(2)
            rejected_ref = weakref.ref(rejected)
            with mock.patch("sim_control.z_stack_io.np.array") as copy_mock:
                with self.assertRaisesRegex(SeriesTerminalInProgress, "terminal.*progress"):
                    writer.submit_focus_layer(1, rejected, 0.3, 0.3, 2.0)
                copy_mock.assert_not_called()
            self.assertEqual(writer._queue.qsize(), queue_size)
            health = writer.check_health()
            self.assertEqual((health.submitted_layers, health.committed_layers, health.pending_layers), (1, 1, 0))
            del rejected
            gc.collect()
            self.assertIsNone(rejected_ref())

            release.set()
            result = writer.shutdown(timeout=2.0)
            self.assertIsNotNone(result)
            self.assertTrue(writer.closed)

    def test_zstack_publish_does_not_use_samefile_or_post_link_target_rollback(self) -> None:
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "no-rollback.tif"
            writer = ZStackAsyncWriter()
            writer.begin_job(
                SeriesWriteJob("no-rollback", "zstack", output, 1, (4, 5), 488, 30_000, "formal")
            )
            writer.submit_z_layer(0, _stack(1), 0.0, 0.0, "b0")
            unlink_hook = mock.Mock(side_effect=OSError("stage cleanup failure"))
            writer._unlink_staging_after_publish = unlink_hook
            with mock.patch(
                "sim_control.z_stack_io.os.path.samefile",
                side_effect=AssertionError("samefile must not be called after exclusive publish"),
            ) as samefile_mock:
                result = writer.finalize("complete")
            writer.shutdown()

            samefile_mock.assert_not_called()
            unlink_hook.assert_not_called()
            self.assertEqual(result.outcome, "complete")
            self.assertEqual(result.output_paths, (output,))
            self.assertTrue(output.exists())
            self.assertFalse(result.staging_paths)

    def test_zstack_external_replacement_after_link_is_never_deleted_and_publish_is_reported(self) -> None:
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "external-race.tif"
            writer = ZStackAsyncWriter()
            writer.begin_job(
                SeriesWriteJob("external", "zstack", output, 1, (4, 5), 488, 30_000, "formal")
            )
            writer.submit_z_layer(0, _stack(1), 0.0, 0.0, "b0")
            original_link = os.link
            unlink_hook = mock.Mock(side_effect=OSError("stage cleanup failure"))
            writer._unlink_staging_after_publish = unlink_hook

            def link_then_external_replace(stage: Path, target: Path) -> None:
                original_link(stage, target)
                if target == output:
                    target.unlink()
                    target.write_bytes(b"external-replacement")

            with mock.patch("sim_control.z_stack_io.os.link", side_effect=link_then_external_replace):
                with mock.patch(
                    "sim_control.z_stack_io.os.path.samefile",
                    side_effect=AssertionError("samefile must not be called"),
                ) as samefile_mock:
                    result = writer.finalize("complete")
            writer.shutdown()

            samefile_mock.assert_not_called()
            unlink_hook.assert_not_called()
            self.assertEqual(result.outcome, "complete")
            self.assertEqual(result.output_paths, (output,))
            self.assertEqual(output.read_bytes(), b"external-replacement")
            self.assertFalse(result.staging_paths)

    def test_async_submit_allows_inflight_layers_then_applies_real_backpressure(self) -> None:
        with TemporaryDirectory() as tmp:
            writer = ZStackAsyncWriter(maxsize=2, sync_timeout_s=2.0, enqueue_poll_s=0.01)
            release = threading.Event()
            entered = threading.Event()
            original = writer._write_z_chunk

            def slow_first(state, payload):
                if payload["plane_index"] == 0:
                    entered.set()
                    self.assertTrue(release.wait(2.0))
                return original(state, payload)

            writer._write_z_chunk = slow_first
            writer.begin_job(
                SeriesWriteJob("async", "zstack", Path(tmp) / "async.tif", 4, (4, 5), 488, 30_000, "formal")
            )

            writer.submit_z_layer(0, _stack(0), 0.0, 0.0, "b0")
            self.assertTrue(entered.wait(1.0))
            writer.submit_z_layer(1, _stack(1), 0.3, 0.3, "b1")
            writer.submit_z_layer(2, _stack(2), 0.6, 0.6, "b2")
            health = writer.check_health()
            self.assertEqual(health.submitted_layers, 3)
            self.assertEqual(health.committed_layers, 0)
            self.assertEqual(health.pending_layers, 3)

            stop_event = threading.Event()
            errors: list[BaseException] = []

            def submit_fourth() -> None:
                try:
                    writer.submit_z_layer(
                        3,
                        _stack(3),
                        0.9,
                        0.9,
                        "b3",
                        stop_event=stop_event,
                        timeout=2.0,
                    )
                except BaseException as exc:
                    errors.append(exc)

            producer = threading.Thread(target=submit_fourth)
            producer.start()
            time.sleep(0.05)
            self.assertTrue(producer.is_alive())
            stop_event.set()
            producer.join(1.0)
            self.assertFalse(producer.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], SeriesWriteCancelled)

            release.set()
            result = writer.finalize("cancelled", timeout=2.0)
            writer.shutdown()
            self.assertEqual(result.completed_layers, 3)

    def test_background_failure_surfaces_via_health_and_before_another_enqueue(self) -> None:
        with TemporaryDirectory() as tmp:
            writer = ZStackAsyncWriter(maxsize=2)
            failed = threading.Event()

            def fail_first(*args, **kwargs):
                failed.set()
                raise OSError("asynchronous chunk failure")

            writer._write_z_chunk = fail_first
            writer.begin_job(
                SeriesWriteJob("health", "zstack", Path(tmp) / "health.tif", 2, (4, 5), 488, 30_000, "formal")
            )
            writer.submit_z_layer(0, _stack(1), 0.0, 0.0, "b0")
            self.assertTrue(failed.wait(1.0))
            deadline = time.monotonic() + 1.0
            while True:
                try:
                    writer.check_health()
                except SeriesWriteError as exc:
                    self.assertIn("asynchronous chunk failure", str(exc))
                    break
                if time.monotonic() >= deadline:
                    self.fail("background failure was not published to check_health")
                time.sleep(0.01)
            with self.assertRaisesRegex(SeriesWriteError, "asynchronous chunk failure"):
                writer.submit_z_layer(1, _stack(2), 0.3, 0.3, "b1")
            result = writer.finalize("failed")
            writer.shutdown()
            self.assertEqual(result.completed_layers, 0)

    def test_begin_preflights_exclusive_publish_in_each_focus_target_directory(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            tiff_dir = root / "tiff"
            csv_dir = root / "csv"
            probes: list[Path] = []
            writer = ZStackAsyncWriter()
            original_probe = writer._probe_exclusive_publish

            def recorded_probe(parent: Path) -> None:
                probes.append(parent)
                original_probe(parent)

            writer._probe_exclusive_publish = recorded_probe
            writer.begin_job(
                SeriesWriteJob(
                    "cross-dir",
                    "focus",
                    tiff_dir / "focus.tif",
                    1,
                    (4, 5),
                    488,
                    4_884,
                    "focus",
                    csv_dir / "focus.csv",
                )
            )
            writer.shutdown()
            self.assertEqual(set(probes), {tiff_dir, csv_dir})

    def test_begin_fails_closed_when_exclusive_publish_is_unsupported(self) -> None:
        with TemporaryDirectory() as tmp:
            writer = ZStackAsyncWriter()
            with mock.patch(
                "sim_control.z_stack_io.os.link",
                side_effect=OSError("exclusive hard-link publish unsupported"),
            ):
                with self.assertRaisesRegex(OSError, "unsupported"):
                    writer.begin_job(
                        SeriesWriteJob("no-link", "zstack", Path(tmp) / "x.tif", 1, (4, 5), 488, 30_000, "formal")
                    )
            writer.shutdown()

    def test_focus_outputs_publish_from_separate_target_local_staging(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "tiff" / "focus.tif"
            csv_path = root / "csv" / "focus.csv"
            writer = ZStackAsyncWriter()
            writer.begin_job(
                SeriesWriteJob("separate", "focus", output, 1, (4, 5), 488, 4_884, "focus", csv_path)
            )
            writer.submit_focus_layer(0, _frame(1), 0.0, 0.0, 2.0)
            result = writer.finalize("complete")
            writer.shutdown()
            self.assertEqual(result.output_paths, (output, csv_path))
            self.assertTrue(output.exists())
            self.assertTrue(csv_path.exists())
            self.assertFalse(result.staging_paths)

    def test_zstack_publish_race_does_not_overwrite_competing_target(self) -> None:
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "race.tif"
            writer = ZStackAsyncWriter()
            writer.begin_job(
                SeriesWriteJob("race", "zstack", output, 1, (4, 5), 488, 30_000, "formal")
            )
            writer.submit_z_layer(0, _stack(1), 0.0, 0.0, "b0")
            original_publish = writer._publish_staged_file

            def competing_publish(stage: Path, target: Path) -> None:
                if target == output:
                    target.write_bytes(b"competing-user-file")
                original_publish(stage, target)

            writer._publish_staged_file = competing_publish
            result = writer.finalize("complete")
            writer.shutdown()
            self.assertEqual(result.outcome, "failed")
            self.assertEqual(output.read_bytes(), b"competing-user-file")
            self.assertFalse(result.output_paths)
            self.assertTrue(result.staging_paths)
            self.assertTrue(any(path.exists() for path in result.staging_paths))

    def test_finalize_timeout_has_recoverable_terminal_result_and_no_duplicate_command(self) -> None:
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "late-finalize.tif"
            writer = ZStackAsyncWriter(sync_timeout_s=0.05, enqueue_poll_s=0.01)
            release = threading.Event()
            entered = threading.Event()
            original_merge = writer._merge_z_stack

            def delayed_merge(*args, **kwargs):
                entered.set()
                self.assertTrue(release.wait(2.0))
                return original_merge(*args, **kwargs)

            writer._merge_z_stack = delayed_merge
            writer.begin_job(
                SeriesWriteJob("late-finalize", "zstack", output, 1, (4, 5), 488, 30_000, "formal")
            )
            writer.submit_z_layer(0, _stack(1), 0.0, 0.0, "b0")
            with self.assertRaises(SeriesWriteTimeout):
                writer.finalize("complete", timeout=0.05)
            self.assertTrue(entered.is_set())
            pending_command = writer._pending_terminal.command
            release.set()
            result = writer.wait_for_terminal(timeout=2.0)
            self.assertEqual(result.output_paths, (output,))
            self.assertIs(writer.last_result, result)
            replayed = writer.finalize("complete", timeout=1.0)
            self.assertIs(replayed, result)
            self.assertIs(writer._terminal_replay.command, pending_command)
            writer.shutdown()

    def test_shutdown_timeout_is_recoverable_and_retry_reuses_terminal(self) -> None:
        with TemporaryDirectory() as tmp:
            writer = ZStackAsyncWriter(sync_timeout_s=0.05, enqueue_poll_s=0.01)
            release = threading.Event()
            entered = threading.Event()
            original_finalize = writer._finalize_state

            def delayed_finalize(*args, **kwargs):
                entered.set()
                self.assertTrue(release.wait(2.0))
                return original_finalize(*args, **kwargs)

            writer._finalize_state = delayed_finalize
            writer.begin_job(
                SeriesWriteJob("late-shutdown", "zstack", Path(tmp) / "shutdown.tif", 1, (4, 5), 488, 30_000, "formal")
            )
            writer.submit_z_layer(0, _stack(1), 0.0, 0.0, "b0")
            with self.assertRaises(SeriesWriteTimeout):
                writer.shutdown(timeout=0.05)
            self.assertTrue(entered.is_set())
            pending_command = writer._pending_terminal.command
            release.set()
            result = writer.shutdown(timeout=2.0)
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.completed_layers, 1)
            self.assertTrue(writer.closed)
            self.assertIs(writer.last_result, result)
            self.assertIs(writer._terminal_replay.command, pending_command)

    def test_begin_and_file_io_run_on_writer_thread_and_queue_is_bounded(self) -> None:
        with TemporaryDirectory() as tmp:
            disk_thread_ids: list[int] = []

            def disk_usage(path: str | os.PathLike[str]):
                disk_thread_ids.append(threading.get_ident())
                return mock.Mock(free=10**12)

            writer = ZStackAsyncWriter(maxsize=2, disk_usage=disk_usage)
            original = writer._write_z_chunk
            write_thread_ids: list[int] = []

            def recorded_write(*args, **kwargs):
                write_thread_ids.append(threading.get_ident())
                return original(*args, **kwargs)

            writer._write_z_chunk = recorded_write
            writer.begin_job(
                SeriesWriteJob(
                    job_id="bounded",
                    kind="zstack",
                    output_path=Path(tmp) / "bounded.tif",
                    total_layers=4,
                    frame_shape=(4, 5),
                    wavelength_nm=488,
                    exposure_us=30_000,
                    running_order="488_3.5_2d_30ms",
                )
            )
            writer.submit_z_layer(0, _stack(0), 0.0, 0.01, "b0")
            writer.submit_z_layer(1, _stack(1), 0.3, 0.31, "b1")
            writer.submit_z_layer(2, _stack(2), 0.6, 0.61, "b2")
            writer.submit_z_layer(3, _stack(3), 0.9, 0.91, "b3")
            result = writer.finalize("complete")
            writer.shutdown()

            self.assertEqual(writer.queue_maxsize, 2)
            self.assertIsNotNone(writer.writer_thread_ident)
            self.assertTrue(disk_thread_ids)
            self.assertTrue(all(item == writer.writer_thread_ident for item in disk_thread_ids))
            self.assertTrue(all(item == writer.writer_thread_ident for item in write_thread_ids))
            self.assertEqual(result.completed_layers, 4)

    def test_stop_interrupts_a_blocked_submit_and_a_full_queue_put(self) -> None:
        with TemporaryDirectory() as tmp:
            writer = ZStackAsyncWriter(maxsize=1, sync_timeout_s=5.0, enqueue_poll_s=0.01)
            release = threading.Event()
            entered = threading.Event()
            original = writer._write_z_chunk

            def blocked_write(*args, **kwargs):
                entered.set()
                self.assertTrue(release.wait(5.0))
                return original(*args, **kwargs)

            writer._write_z_chunk = blocked_write
            writer.begin_job(
                SeriesWriteJob("stop", "zstack", Path(tmp) / "stop.tif", 1, (4, 5), 488, 30_000, "formal")
            )
            stop_event = threading.Event()
            submit_error: list[BaseException] = []

            def submit() -> None:
                try:
                    writer.submit_z_layer(
                        0,
                        _stack(1),
                        0.0,
                        0.0,
                        "b0",
                        stop_event=stop_event,
                        timeout=5.0,
                    )
                except BaseException as exc:
                    submit_error.append(exc)

            producer = threading.Thread(target=submit)
            producer.start()
            self.assertTrue(entered.wait(2.0))
            writer._queue.put(_Command("barrier"), timeout=1.0)
            put_errors: list[BaseException] = []

            def blocked_put() -> None:
                try:
                    writer._put_stop_aware(
                        _Command("never"),
                        deadline=time.monotonic() + 5.0,
                        stop_event=stop_event,
                    )
                except BaseException as exc:
                    put_errors.append(exc)

            queue_waiter = threading.Thread(target=blocked_put)
            queue_waiter.start()
            time.sleep(0.05)
            self.assertTrue(queue_waiter.is_alive())
            stop_event.set()
            started = time.monotonic()
            queue_waiter.join(1.0)
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertFalse(queue_waiter.is_alive())
            self.assertEqual(len(put_errors), 1)
            self.assertIsInstance(put_errors[0], SeriesWriteCancelled)
            producer.join(1.0)
            self.assertFalse(producer.is_alive())
            self.assertFalse(submit_error)

            release.set()
            result = writer.finalize("cancelled")
            writer.shutdown()
            self.assertEqual(result.completed_layers, 1)

    def test_sync_timeout_and_dead_writer_fail_fast_without_an_infinite_wait(self) -> None:
        with TemporaryDirectory() as tmp:
            writer = ZStackAsyncWriter(sync_timeout_s=0.1, enqueue_poll_s=0.01)
            release = threading.Event()
            entered = threading.Event()
            original = writer._write_z_chunk

            def blocked_write(*args, **kwargs):
                entered.set()
                self.assertTrue(release.wait(5.0))
                return original(*args, **kwargs)

            writer._write_z_chunk = blocked_write
            writer.begin_job(
                SeriesWriteJob("timeout", "zstack", Path(tmp) / "timeout.tif", 1, (4, 5), 488, 30_000, "formal")
            )
            writer.submit_z_layer(0, _stack(1), 0.0, 0.0, "b0")
            self.assertTrue(entered.wait(1.0))
            started = time.monotonic()
            with self.assertRaises(SeriesWriteTimeout):
                writer.finalize("complete")
            self.assertLess(time.monotonic() - started, 0.8)
            release.set()
            writer.wait_for_terminal(timeout=2.0)
            writer.shutdown()

            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, "shut down"):
                writer.begin_job(
                    SeriesWriteJob("dead", "zstack", Path(tmp) / "dead.tif", 1, (4, 5), 488, 30_000, "formal")
                )
            self.assertLess(time.monotonic() - started, 0.2)

    def test_shutdown_times_out_if_writer_is_stuck_and_join_is_bounded(self) -> None:
        with TemporaryDirectory() as tmp:
            writer = ZStackAsyncWriter(sync_timeout_s=0.1, join_timeout_s=0.1, enqueue_poll_s=0.01)
            release = threading.Event()
            entered = threading.Event()
            original = writer._write_z_chunk

            def blocked_write(*args, **kwargs):
                entered.set()
                self.assertTrue(release.wait(5.0))
                return original(*args, **kwargs)

            writer._write_z_chunk = blocked_write
            writer.begin_job(
                SeriesWriteJob("stuck", "zstack", Path(tmp) / "stuck.tif", 1, (4, 5), 488, 30_000, "formal")
            )
            submit_error: list[BaseException] = []

            def submit() -> None:
                try:
                    writer.submit_z_layer(0, _stack(1), 0.0, 0.0, "b0", timeout=2.0)
                except BaseException as exc:
                    submit_error.append(exc)

            producer = threading.Thread(target=submit)
            producer.start()
            self.assertTrue(entered.wait(1.0))
            started = time.monotonic()
            with self.assertRaises(SeriesWriteTimeout):
                writer.finalize("cancelled")
            with self.assertRaisesRegex(RuntimeError, "finalize terminal"):
                writer.shutdown()
            self.assertLess(time.monotonic() - started, 0.8)
            self.assertTrue(writer._thread.is_alive())
            release.set()
            producer.join(2.0)
            writer.wait_for_terminal(timeout=2.0)
            writer.shutdown(timeout=2.0)

    def test_shutdown_join_timeout_never_reports_success(self) -> None:
        writer = ZStackAsyncWriter(sync_timeout_s=1.0, join_timeout_s=0.01)
        actual_join = writer._thread.join
        actual_is_alive = writer._thread.is_alive
        writer._thread.join = mock.Mock(return_value=None)
        writer._thread.is_alive = mock.Mock(return_value=True)
        with self.assertRaisesRegex(SeriesWriteTimeout, "still alive"):
            writer.shutdown()
        writer._thread.join = actual_join
        writer._thread.is_alive = actual_is_alive
        actual_join(2.0)

    def test_unexpected_writer_death_is_detected_during_sync_wait(self) -> None:
        with TemporaryDirectory() as tmp:
            writer = ZStackAsyncWriter(sync_timeout_s=2.0, enqueue_poll_s=0.01)
            entered = threading.Event()
            release = threading.Event()
            original_write = writer._write_z_chunk
            actual_is_alive = writer._thread.is_alive

            def blocked_write(*args, **kwargs):
                entered.set()
                self.assertTrue(release.wait(2.0))
                return original_write(*args, **kwargs)

            writer._write_z_chunk = blocked_write
            writer.begin_job(
                SeriesWriteJob("death", "zstack", Path(tmp) / "death.tif", 1, (4, 5), 488, 30_000, "formal")
            )
            writer.submit_z_layer(0, _stack(1), 0.0, 0.0, "b0")
            self.assertTrue(entered.wait(1.0))
            errors: list[BaseException] = []

            def finalize() -> None:
                try:
                    writer.finalize("complete")
                except BaseException as exc:
                    errors.append(exc)

            producer = threading.Thread(target=finalize)
            producer.start()
            time.sleep(0.05)
            writer._thread.is_alive = mock.Mock(return_value=False)
            producer.join(1.0)
            self.assertFalse(producer.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], SeriesWriteError)
            self.assertIn("stopped before acknowledging", str(errors[0]))
            writer._thread.is_alive = actual_is_alive
            release.set()
            writer.wait_for_terminal(timeout=2.0)
            writer.shutdown(timeout=2.0)

    def test_z_stack_two_layers_have_18_ordered_pages_and_json_metadata(self) -> None:
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "sample.tif"
            writer = ZStackAsyncWriter()
            writer.begin_job(
                SeriesWriteJob(
                    job_id="parent-1",
                    kind="zstack",
                    output_path=output,
                    total_layers=2,
                    frame_shape=(4, 5),
                    wavelength_nm=561,
                    exposure_us=12_345,
                    running_order="561_3.5_2d_14ms",
                )
            )
            writer.submit_z_layer(0, _stack(10), 1.0, 1.01, "batch-a", timestamp="2026-08-22T01:02:03Z")
            writer.submit_z_layer(1, _stack(20), 1.3, 1.31, "batch-b", timestamp="2026-08-22T01:02:04Z")
            result = writer.finalize("complete")
            writer.shutdown()

            self.assertEqual(result.outcome, "complete", result.message)
            self.assertEqual(result.output_paths, (output,))
            self.assertFalse(result.partial)
            with tifffile.TiffFile(output) as tif:
                self.assertEqual(len(tif.pages), 18)
                for page_number, page in enumerate(tif.pages):
                    plane_index, frame_index = divmod(page_number, 9)
                    metadata = json.loads(page.description)
                    self.assertEqual(metadata["parent_task_id"], "parent-1")
                    self.assertEqual(metadata["batch_id"], "batch-a" if plane_index == 0 else "batch-b")
                    self.assertEqual(metadata["plane_index"], plane_index)
                    self.assertEqual(metadata["frame_index"], frame_index)
                    self.assertEqual(metadata["requested_z_um"], 1.0 + 0.3 * plane_index)
                    self.assertAlmostEqual(metadata["measured_z_um"], 1.01 + 0.3 * plane_index)
                    self.assertEqual(metadata["wavelength_nm"], 561)
                    self.assertEqual(metadata["exposure_us"], 12_345)
                    self.assertEqual(metadata["running_order"], "561_3.5_2d_14ms")
                    self.assertEqual(int(page.asarray()[0, 0]), 10 if plane_index == 0 else 20)

    def test_bigtiff_threshold_is_injectable(self) -> None:
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "forced-big.tif"
            writer = ZStackAsyncWriter(bigtiff_threshold_bytes=1)
            writer.begin_job(
                SeriesWriteJob("big", "zstack", output, 1, (4, 5), 405, 5_000, "405_zscan")
            )
            writer.submit_z_layer(0, _stack(7), 0.0, 0.0, "batch")
            result = writer.finalize("complete")
            writer.shutdown()
            self.assertTrue(result.bigtiff)
            with tifffile.TiffFile(output) as tif:
                self.assertTrue(tif.is_bigtiff)

    def test_cancel_writes_only_committed_layers_to_precisely_named_partial(self) -> None:
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "cancel-me.tif"
            writer = ZStackAsyncWriter()
            writer.begin_job(SeriesWriteJob("cancel", "zstack", output, 3, (4, 5), 488, 30_000, "formal"))
            writer.submit_z_layer(0, _stack(1), 0.0, 0.0, "b0")
            writer.submit_z_layer(1, _stack(2), 0.3, 0.3, "b1")
            result = writer.finalize("cancelled", message="operator stop")
            writer.shutdown()

            expected = Path(tmp) / "cancel-me.partial-z2of3.tif"
            self.assertEqual(result.output_paths, (expected,))
            self.assertEqual(result.completed_layers, 2)
            self.assertTrue(result.partial)
            with tifffile.TiffFile(expected) as tif:
                self.assertEqual(len(tif.pages), 18)

    def test_second_layer_chunk_failure_cannot_report_complete(self) -> None:
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "chunk-fail.tif"
            writer = ZStackAsyncWriter()
            original = writer._write_z_chunk
            calls = 0

            def fail_second(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected chunk failure")
                return original(*args, **kwargs)

            writer._write_z_chunk = fail_second
            writer.begin_job(SeriesWriteJob("fail", "zstack", output, 2, (4, 5), 488, 30_000, "formal"))
            writer.submit_z_layer(0, _stack(1), 0.0, 0.0, "b0")
            try:
                writer.submit_z_layer(1, _stack(2), 0.3, 0.3, "b1")
            except SeriesWriteError as exc:
                self.assertIn("injected chunk failure", str(exc))
            result = writer.finalize("complete")
            writer.shutdown()

            expected = Path(tmp) / "chunk-fail.partial-z1of2.tif"
            self.assertEqual(result.outcome, "failed")
            self.assertEqual(result.completed_layers, 1)
            self.assertIn("injected chunk failure", result.message)
            self.assertEqual(result.output_paths, (expected,))
            with tifffile.TiffFile(expected) as tif:
                self.assertEqual(len(tif.pages), 9)

    def test_focus_chunk_failure_falls_back_to_best_committed_plane_and_publishes_partial(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "focus.tif"
            csv_path = root / "focus.csv"
            writer = ZStackAsyncWriter()
            original = writer._write_focus_chunk
            calls = 0

            def fail_second(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected focus chunk failure")
                return original(*args, **kwargs)

            writer._write_focus_chunk = fail_second
            writer.begin_job(
                SeriesWriteJob(
                    "focus-fail",
                    "focus",
                    output,
                    2,
                    (4, 5),
                    488,
                    4_884,
                    "488_zscan",
                    csv_path,
                )
            )
            writer.submit_focus_layer(0, _frame(1), 0.0, 0.0, 1.0)
            deadline = time.monotonic() + 1.0
            while writer.check_health().committed_layers != 1:
                if time.monotonic() >= deadline:
                    self.fail("first focus layer was not committed")
                time.sleep(0.01)
            writer.submit_focus_layer(1, _frame(2), 0.3, 0.3, 99.0)

            result = writer.finalize("failed", best_plane_index=1)
            writer.shutdown()

            expected_tiff = root / "focus.partial-z1of2.tif"
            expected_csv = root / "focus.partial-z1of2.csv"
            self.assertEqual(result.outcome, "failed")
            self.assertTrue(result.partial)
            self.assertEqual(result.completed_layers, 1)
            self.assertEqual(result.output_paths, (expected_tiff, expected_csv))
            self.assertIn("injected focus chunk failure", result.message)
            with tifffile.TiffFile(expected_tiff) as tif:
                self.assertEqual(len(tif.pages), 1)
                metadata = json.loads(tif.pages[0].description)
                self.assertEqual(metadata["plane_index"], 0)
                self.assertTrue(metadata["is_best"])
            with expected_csv.open(newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["plane_index"], "0")
            self.assertEqual(rows[0]["is_best"], "True")

    def test_chunk_failure_is_reported_before_next_plane_and_commit_count_does_not_advance(self) -> None:
        with TemporaryDirectory() as tmp:
            writer = ZStackAsyncWriter()
            writer._write_z_chunk = mock.Mock(side_effect=OSError("background write failed"))
            writer.begin_job(
                SeriesWriteJob("ack", "zstack", Path(tmp) / "ack.tif", 2, (4, 5), 488, 30_000, "formal")
            )
            writer.submit_z_layer(0, _stack(1), 0.0, 0.0, "b0")
            deadline = time.monotonic() + 1.0
            while True:
                try:
                    writer.check_health()
                except SeriesWriteError as exc:
                    self.assertIn("background write failed", str(exc))
                    break
                if time.monotonic() >= deadline:
                    self.fail("writer failure was not reported")
                time.sleep(0.01)
            with self.assertRaisesRegex(SeriesWriteError, "background write failed"):
                writer.submit_z_layer(1, _stack(2), 0.3, 0.3, "b1")
            self.assertEqual(writer._client_next_plane, 1)
            self.assertEqual(writer._client_committed_layers, 0)
            result = writer.finalize("failed")
            writer.shutdown()
            self.assertEqual(result.completed_layers, 0)
            self.assertEqual(result.outcome, "failed")

    def test_submit_copies_the_complete_plane_before_background_write(self) -> None:
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "ownership.tif"
            writer = ZStackAsyncWriter(sync_timeout_s=2.0)
            entered = threading.Event()
            release = threading.Event()
            original_write = writer._write_z_chunk

            def delayed_write(*args, **kwargs):
                entered.set()
                self.assertTrue(release.wait(2.0))
                return original_write(*args, **kwargs)

            writer._write_z_chunk = delayed_write
            writer.begin_job(SeriesWriteJob("copy", "zstack", output, 1, (4, 5), 488, 30_000, "formal"))
            source = _stack(11)
            submit_error: list[BaseException] = []

            def submit() -> None:
                try:
                    writer.submit_z_layer(0, source, 0.0, 0.0, "b0")
                except BaseException as exc:
                    submit_error.append(exc)

            producer = threading.Thread(target=submit)
            producer.start()
            self.assertTrue(entered.wait(1.0))
            source.fill(99)
            release.set()
            producer.join(2.0)
            self.assertFalse(submit_error)
            writer.finalize("complete")
            writer.shutdown()
            with tifffile.TiffFile(output) as tif:
                self.assertTrue(all(int(page.asarray()[0, 0]) == 11 for page in tif.pages))

    def test_merge_failure_retains_spool_and_committed_chunks(self) -> None:
        with TemporaryDirectory() as tmp:
            writer = ZStackAsyncWriter()
            output = Path(tmp) / "merge-fail.tif"
            writer.begin_job(SeriesWriteJob("merge", "zstack", output, 1, (4, 5), 647, 20_000, "formal"))
            writer.submit_z_layer(0, _stack(1), 0.0, 0.0, "b0")
            writer._merge_z_stack = mock.Mock(side_effect=OSError("injected merge failure"))
            result = writer.finalize("complete")
            writer.shutdown()

            self.assertEqual(result.outcome, "failed")
            self.assertFalse(result.output_paths)
            self.assertIsNotNone(result.spool_path)
            assert result.spool_path is not None
            self.assertTrue(result.spool_path.is_dir())
            self.assertEqual(len(list(result.spool_path.glob("plane-*.tif"))), 1)

    def test_focus_writes_tiff_csv_best_flag_and_cancelled_partial(self) -> None:
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "focus.zfocus.tif"
            csv_path = Path(tmp) / "focus.sml.csv"
            writer = ZStackAsyncWriter()
            writer.begin_job(
                SeriesWriteJob("focus", "focus", output, 3, (4, 5), 405, 4_884, "405_zscan", csv_path)
            )
            for index, score in enumerate((1.0, 5.0, 3.0)):
                writer.submit_focus_layer(
                    index,
                    _frame(index),
                    float(index),
                    float(index) + 0.01,
                    score,
                    timestamp=f"2026-08-22T00:00:0{index}Z",
                )
            result = writer.finalize("complete")
            writer.shutdown()

            self.assertEqual(result.output_paths, (output, csv_path))
            with tifffile.TiffFile(output) as tif:
                self.assertEqual(len(tif.pages), 3)
                metadata = [json.loads(page.description) for page in tif.pages]
            self.assertEqual([item["plane_index"] for item in metadata], [0, 1, 2])
            self.assertEqual([item["is_best"] for item in metadata], [False, True, False])
            with csv_path.open("r", newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual([row["is_best"] for row in rows], ["False", "True", "False"])
            self.assertEqual([float(row["sml_score"]) for row in rows], [1.0, 5.0, 3.0])

            partial_output = Path(tmp) / "partial.zfocus.tif"
            partial_csv = Path(tmp) / "partial.sml.csv"
            writer = ZStackAsyncWriter()
            writer.begin_job(
                SeriesWriteJob("focus-partial", "focus", partial_output, 3, (4, 5), 561, 7_884, "561_zscan", partial_csv)
            )
            writer.submit_focus_layer(0, _frame(0), 0.0, 0.0, 2.0)
            writer.submit_focus_layer(1, _frame(1), 0.3, 0.3, 4.0)
            partial = writer.finalize("cancelled")
            writer.shutdown()
            self.assertEqual(
                partial.output_paths,
                (
                    Path(tmp) / "partial.zfocus.partial-z2of3.tif",
                    Path(tmp) / "partial.sml.partial-z2of3.csv",
                ),
            )
            with tifffile.TiffFile(partial.output_paths[0]) as tif:
                self.assertEqual(len(tif.pages), 2)
                self.assertTrue(all(page.asarray().dtype == np.uint16 for page in tif.pages))
                partial_metadata = [json.loads(page.description) for page in tif.pages]
            self.assertEqual([item["is_best"] for item in partial_metadata], [False, True])
            with partial.output_paths[1].open("r", newline="", encoding="utf-8") as stream:
                partial_rows = list(csv.DictReader(stream))
            self.assertEqual(len(partial_rows), 2)
            self.assertEqual([row["is_best"] for row in partial_rows], ["False", "True"])

    def test_focus_tied_sml_selects_the_earliest_committed_plane(self) -> None:
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "tie.zfocus.tif"
            writer = ZStackAsyncWriter()
            writer.begin_job(
                SeriesWriteJob("tie", "focus", output, 3, (4, 5), 561, 7_884, "focus", Path(tmp) / "tie.csv")
            )
            for index, score in enumerate((4.0, 4.0, 3.0)):
                writer.submit_focus_layer(index, _frame(index), index, index, score)
            result = writer.finalize("complete")
            writer.shutdown()
            with tifffile.TiffFile(result.output_paths[0]) as tif:
                flags = [json.loads(page.description)["is_best"] for page in tif.pages]
            self.assertEqual(flags, [True, False, False])

    def test_focus_paths_must_be_distinct_after_resolution(self) -> None:
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "same.tif"
            writer = ZStackAsyncWriter()
            with self.assertRaisesRegex(ValueError, "distinct"):
                writer.begin_job(
                    SeriesWriteJob("same", "focus", output, 1, (4, 5), 488, 4_884, "focus", output)
                )
            writer.shutdown()

    def test_focus_second_publish_failure_reports_first_exclusive_publish(self) -> None:
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "tiff" / "atomic.zfocus.tif"
            csv_path = Path(tmp) / "csv" / "atomic.sml.csv"
            writer = ZStackAsyncWriter()
            writer.begin_job(
                SeriesWriteJob("atomic", "focus", output, 1, (4, 5), 488, 4_884, "focus", csv_path)
            )
            writer.submit_focus_layer(0, _frame(1), 0.0, 0.0, 1.0)
            original_publish = writer._publish_staged_file
            calls = 0

            def fail_second(stage: Path, target: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("CSV publish failed")
                original_publish(stage, target)

            writer._publish_staged_file = fail_second
            result = writer.finalize("complete")
            writer.shutdown()

            self.assertEqual(result.outcome, "failed")
            self.assertEqual(result.output_paths, (output,))
            self.assertTrue(output.exists())
            self.assertFalse(csv_path.exists())
            self.assertIsNotNone(result.spool_path)
            assert result.spool_path is not None
            self.assertTrue((result.spool_path / "focus-merged.tif").exists())
            self.assertTrue(
                any((staging / "focus-merged.csv").exists() for staging in result.staging_paths)
            )

    def test_preflight_rejects_insufficient_two_copy_space(self) -> None:
        with TemporaryDirectory() as tmp:
            disk_usage = mock.Mock(return_value=mock.Mock(free=100))
            writer = ZStackAsyncWriter(disk_usage=disk_usage)
            with self.assertRaisesRegex(OSError, "space"):
                writer.begin_job(
                    SeriesWriteJob("space", "zstack", Path(tmp) / "x.tif", 2, (10, 10), 488, 30_000, "formal")
                )
            self.assertEqual(disk_usage.call_count, 1)
            self.assertEqual(disk_usage.call_args.args[0], Path(tmp))
            writer.shutdown()

    def test_invalid_arrays_are_rejected_before_enqueue(self) -> None:
        with TemporaryDirectory() as tmp:
            writer = ZStackAsyncWriter()
            writer.begin_job(
                SeriesWriteJob("invalid", "zstack", Path(tmp) / "x.tif", 1, (4, 5), 488, 30_000, "formal")
            )
            with self.assertRaisesRegex(TypeError, "numpy.uint16"):
                writer.submit_z_layer(0, np.zeros((9, 4, 5), dtype=np.float32), 0.0, 0.0, "b0")
            with self.assertRaisesRegex(ValueError, "shape"):
                writer.submit_z_layer(0, np.zeros((8, 4, 5), dtype=np.uint16), 0.0, 0.0, "b0")
            result = writer.finalize("cancelled")
            self.assertEqual(result.completed_layers, 0)
            writer.shutdown()

    def test_shutdown_drains_pending_layers_and_finalizes_partial(self) -> None:
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "shutdown.tif"
            writer = ZStackAsyncWriter()
            writer.begin_job(SeriesWriteJob("shutdown", "zstack", output, 3, (4, 5), 488, 30_000, "formal"))
            writer.submit_z_layer(0, _stack(1), 0.0, 0.0, "b0")
            writer.submit_z_layer(1, _stack(2), 0.3, 0.3, "b1")
            result = writer.shutdown()
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.outcome, "cancelled")
            self.assertEqual(result.completed_layers, 2)
            self.assertEqual(result.output_paths, (Path(tmp) / "shutdown.partial-z2of3.tif",))


if __name__ == "__main__":
    unittest.main()

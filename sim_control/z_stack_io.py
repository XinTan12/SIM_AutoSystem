"""Bounded asynchronous persistence for Z-stack and focus-scan series.

Capture-side submission validates and copies a complete plane, then returns as
soon as a stop-aware bounded queue accepts it.  A dedicated writer thread owns
all filesystem work and reports commit acknowledgements through a separate
completion queue.  Terminal commands are retained after caller timeouts so a
large merge or shutdown can be queried and recovered instead of duplicated.
"""

from __future__ import annotations

import csv
import json
import math
import os
import queue
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Literal

import numpy as np
import tifffile


SeriesKind = Literal["zstack", "focus"]
SeriesOutcome = Literal["complete", "cancelled", "failed"]

DEFAULT_BIGTIFF_THRESHOLD_BYTES = 4 * 1024**3 - 32 * 1024**2


class SeriesWriteError(RuntimeError):
    """Base class for asynchronous series-writer failures."""


class SeriesWriteCancelled(SeriesWriteError):
    """Raised when a stop request interrupts a bounded queue operation."""


class SeriesWriteTimeout(SeriesWriteError):
    """Raised when the writer cannot acknowledge an operation in time."""


class SeriesTerminalInProgress(SeriesWriteError):
    """Raised when a terminal command has closed capture-side submission."""


class _PartialPublishError(SeriesWriteError):
    """Internal error carrying paths already published by an exclusive link."""

    def __init__(self, message: str, published_paths: tuple[Path, ...]) -> None:
        super().__init__(message)
        self.published_paths = published_paths


@dataclass(frozen=True)
class SeriesWriteJob:
    """Immutable description of one series-writing job."""

    job_id: str
    kind: SeriesKind
    output_path: Path
    total_layers: int
    frame_shape: tuple[int, int]
    wavelength_nm: int
    exposure_us: int
    running_order: str
    csv_path: Path | None = None


@dataclass(frozen=True)
class SeriesWriteResult:
    """Small terminal result; image arrays deliberately never appear here."""

    job_id: str
    kind: SeriesKind
    outcome: SeriesOutcome
    completed_layers: int
    total_layers: int
    output_paths: tuple[Path, ...]
    partial: bool
    bigtiff: bool
    message: str = ""
    spool_path: Path | None = None
    staging_paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class SeriesWriteHealth:
    """Non-array snapshot of submitted, committed, and pending planes."""

    job_id: str
    submitted_layers: int
    committed_layers: int
    pending_layers: int


@dataclass(frozen=True)
class _Completion:
    job_id: str
    plane_index: int
    committed: bool
    error: str = ""


@dataclass
class _Command:
    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    done: threading.Event | None = None
    reply: dict[str, Any] | None = None


@dataclass
class _TerminalState:
    kind: Literal["finalize", "shutdown"]
    command: _Command
    job_id: str | None
    result: SeriesWriteResult | None = None


@dataclass
class _WriterJobState:
    spec: SeriesWriteJob
    spool_path: Path
    expected_uncompressed_bytes: int
    chunks: list[Path] = field(default_factory=list)
    focus_rows: list[dict[str, Any]] = field(default_factory=list)
    failure_message: str = ""
    staging_by_parent: dict[str, Path] = field(default_factory=dict)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _partial_path(path: Path, completed: int, total: int) -> Path:
    return path.with_name(f"{path.stem}.partial-z{completed}of{total}{path.suffix}")


def _join_messages(*messages: str) -> str:
    return "; ".join(message.strip() for message in messages if message and message.strip())


class ZStackAsyncWriter:
    """Serialize one active Z-stack or focus series on a bounded worker queue."""

    def __init__(
        self,
        *,
        maxsize: int = 2,
        bigtiff_threshold_bytes: int = DEFAULT_BIGTIFF_THRESHOLD_BYTES,
        disk_usage: Callable[[str | os.PathLike[str]], Any] = shutil.disk_usage,
        sync_timeout_s: float = 30.0,
        enqueue_poll_s: float = 0.05,
        join_timeout_s: float = 5.0,
    ) -> None:
        if int(maxsize) < 1:
            raise ValueError("maxsize must be at least 1")
        if int(bigtiff_threshold_bytes) < 0:
            raise ValueError("bigtiff_threshold_bytes must be non-negative")
        if float(sync_timeout_s) <= 0:
            raise ValueError("sync_timeout_s must be positive")
        if float(enqueue_poll_s) <= 0:
            raise ValueError("enqueue_poll_s must be positive")
        if float(join_timeout_s) <= 0:
            raise ValueError("join_timeout_s must be positive")

        self._queue: queue.Queue[_Command] = queue.Queue(maxsize=int(maxsize))
        self._completion_queue: queue.SimpleQueue[_Completion] = queue.SimpleQueue()
        self._bigtiff_threshold_bytes = int(bigtiff_threshold_bytes)
        self._disk_usage = disk_usage
        self._sync_timeout_s = float(sync_timeout_s)
        self._enqueue_poll_s = float(enqueue_poll_s)
        self._join_timeout_s = float(join_timeout_s)
        self._client_lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self._client_spec: SeriesWriteJob | None = None
        self._client_next_plane = 0
        self._client_committed_layers = 0
        self._client_completed_acks = 0
        self._client_failure_message = ""
        self._closed = False
        self._state: _WriterJobState | None = None
        self._writer_thread_ident: int | None = None
        self._last_result: SeriesWriteResult | None = None
        self._pending_terminal: _TerminalState | None = None
        self._terminal_replay: _TerminalState | None = None
        self._thread = threading.Thread(
            target=self._writer_loop,
            name="z-stack-series-writer",
            daemon=True,
        )
        self._thread.start()

    @property
    def queue_maxsize(self) -> int:
        return self._queue.maxsize

    @property
    def writer_thread_ident(self) -> int | None:
        return self._writer_thread_ident

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def last_result(self) -> SeriesWriteResult | None:
        if self._last_result is not None:
            return self._last_result
        terminal = self._pending_terminal
        if terminal is None or terminal.command.done is None or not terminal.command.done.is_set():
            return None
        reply = terminal.command.reply or {}
        result = reply.get("result")
        return result if isinstance(result, SeriesWriteResult) else None

    def check_health(self) -> SeriesWriteHealth:
        """Drain completed acknowledgements without waiting or touching disk."""

        with self._client_lock:
            spec = self._client_spec
            if spec is None:
                raise RuntimeError("no active series writer job")
            self._drain_completions_locked()
            self._raise_client_failure()
            return SeriesWriteHealth(
                job_id=spec.job_id,
                submitted_layers=self._client_next_plane,
                committed_layers=self._client_committed_layers,
                pending_layers=max(0, self._client_next_plane - self._client_completed_acks),
            )

    def begin_job(self, spec: SeriesWriteJob) -> None:
        """Synchronously preflight *spec* on the writer thread."""

        normalized = self._validate_job(spec)
        deadline = time.monotonic() + self._sync_timeout_s
        with self._bounded_operation(deadline):
            with self._client_lock:
                self._ensure_open()
                if self._client_spec is not None:
                    raise RuntimeError("a series writer job is already active")
            self._send_sync(_Command("begin", {"spec": normalized}), deadline=deadline)
            with self._client_lock:
                self._client_spec = normalized
                self._client_next_plane = 0
                self._client_committed_layers = 0
                self._client_completed_acks = 0
                self._client_failure_message = ""
                self._pending_terminal = None
                self._terminal_replay = None
                self._last_result = None

    def submit_z_layer(
        self,
        plane_index: int,
        frames: np.ndarray,
        requested_z_um: float,
        measured_z_um: float,
        batch_id: str,
        *,
        timestamp: str | None = None,
        timeout: float | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        """Validate and enqueue one complete ``(9, H, W) uint16`` plane."""

        with self._client_lock:
            self._raise_terminal_in_progress_locked()
        deadline = self._operation_deadline(timeout)
        with self._bounded_operation(deadline, stop_event):
            with self._client_lock:
                spec = self._require_client_job("zstack")
                self._drain_completions_locked()
                self._raise_client_failure()
                self._validate_plane_index(plane_index, spec)
                self._validate_z_stack_array(frames, spec.frame_shape)
                requested = self._finite_float(requested_z_um, "requested_z_um")
                measured = self._finite_float(measured_z_um, "measured_z_um")
                if not str(batch_id).strip():
                    raise ValueError("batch_id must not be empty")
                owned_frames = np.array(frames, dtype=np.uint16, order="C", copy=True)
                command = _Command(
                    "z_layer",
                    {
                        "plane_index": int(plane_index),
                        "frames": owned_frames,
                        "requested_z_um": requested,
                        "measured_z_um": measured,
                        "batch_id": str(batch_id),
                        "timestamp": str(timestamp or _utc_timestamp()),
                    },
                )
            self._put_stop_aware(
                command,
                deadline=deadline,
                stop_event=stop_event,
                monitor_health=True,
            )
            with self._client_lock:
                self._client_next_plane += 1
                self._drain_completions_locked()
                self._raise_client_failure()

    def submit_focus_layer(
        self,
        plane_index: int,
        frame: np.ndarray,
        requested_z_um: float,
        measured_z_um: float,
        sml_score: float,
        *,
        timestamp: str | None = None,
        timeout: float | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        """Validate and enqueue one focus image and its SML score."""

        with self._client_lock:
            self._raise_terminal_in_progress_locked()
        deadline = self._operation_deadline(timeout)
        with self._bounded_operation(deadline, stop_event):
            with self._client_lock:
                spec = self._require_client_job("focus")
                self._drain_completions_locked()
                self._raise_client_failure()
                self._validate_plane_index(plane_index, spec)
                self._validate_focus_array(frame, spec.frame_shape)
                owned_frame = np.array(frame, dtype=np.uint16, order="C", copy=True)
                command = _Command(
                    "focus_layer",
                    {
                        "plane_index": int(plane_index),
                        "frame": owned_frame,
                        "requested_z_um": self._finite_float(requested_z_um, "requested_z_um"),
                        "measured_z_um": self._finite_float(measured_z_um, "measured_z_um"),
                        "sml_score": self._finite_float(sml_score, "sml_score"),
                        "timestamp": str(timestamp or _utc_timestamp()),
                    },
                )
            self._put_stop_aware(
                command,
                deadline=deadline,
                stop_event=stop_event,
                monitor_health=True,
            )
            with self._client_lock:
                self._client_next_plane += 1
                self._drain_completions_locked()
                self._raise_client_failure()

    def finalize(
        self,
        outcome: SeriesOutcome,
        *,
        best_plane_index: int | None = None,
        message: str = "",
        timeout: float | None = None,
        stop_event: threading.Event | None = None,
    ) -> SeriesWriteResult:
        """Drain queued planes, assemble outputs, and return a small result."""

        if outcome not in {"complete", "cancelled", "failed"}:
            raise ValueError(f"unsupported outcome: {outcome!r}")
        deadline = self._operation_deadline(timeout)
        with self._bounded_operation(deadline, stop_event):
            terminal_to_wait: _TerminalState | None = None
            with self._client_lock:
                pending = self._pending_terminal
                replay = self._terminal_replay
                if pending is not None:
                    if pending.kind != "finalize":
                        raise RuntimeError("shutdown terminal command is already pending")
                    terminal_to_wait = pending
                if replay is not None and replay.kind == "finalize" and replay.result is not None:
                    return replay.result
                if terminal_to_wait is not None:
                    spec = None
                else:
                    self._ensure_open()
                    spec = self._client_spec
                    if spec is None:
                        raise RuntimeError("no active series writer job")
                    self._drain_completions_locked()
            if terminal_to_wait is not None:
                replayed_result = self._wait_for_terminal_locked(
                    terminal_to_wait,
                    deadline,
                    stop_event,
                )
                if not isinstance(replayed_result, SeriesWriteResult):
                    raise RuntimeError("finalize returned an invalid terminal result")
                return replayed_result
            assert spec is not None
            command = self._prepare_sync_command(
                _Command(
                    "finalize",
                    {
                        "outcome": outcome,
                        "best_plane_index": best_plane_index,
                        "message": str(message),
                    },
                )
            )
            self._put_stop_aware(command, deadline=deadline, stop_event=stop_event)
            terminal = _TerminalState("finalize", command, spec.job_id)
            with self._client_lock:
                self._pending_terminal = terminal
            result = self._wait_for_terminal_locked(terminal, deadline, stop_event)
            if not isinstance(result, SeriesWriteResult):
                raise RuntimeError("finalize returned an invalid terminal result")
            return result

    def shutdown(self, *, timeout: float | None = None) -> SeriesWriteResult | None:
        """Drain pending commands and stop; an active job becomes a partial."""

        deadline = self._operation_deadline(timeout)
        with self._bounded_operation(deadline):
            terminal_to_wait: _TerminalState | None = None
            with self._client_lock:
                if self._closed:
                    return self._last_result
                pending = self._pending_terminal
                if pending is not None:
                    if pending.kind != "shutdown":
                        raise RuntimeError(
                            "finalize terminal command is pending; call wait_for_terminal first"
                        )
                    terminal_to_wait = pending
                replay = self._terminal_replay
                if replay is not None and replay.kind == "shutdown":
                    return replay.result
                if terminal_to_wait is None:
                    self._ensure_open()
                    job_id = self._client_spec.job_id if self._client_spec is not None else None
                    self._drain_completions_locked()
                else:
                    job_id = None
            if terminal_to_wait is not None:
                return self._wait_for_terminal_locked(terminal_to_wait, deadline, None)
            command = self._prepare_sync_command(_Command("shutdown"))
            self._put_stop_aware(command, deadline=deadline)
            terminal = _TerminalState("shutdown", command, job_id)
            with self._client_lock:
                self._pending_terminal = terminal
            return self._wait_for_terminal_locked(terminal, deadline, None)

    def wait_for_terminal(
        self,
        *,
        timeout: float | None = None,
        stop_event: threading.Event | None = None,
    ) -> SeriesWriteResult | None:
        """Wait for an already-enqueued finalize/shutdown command without duplicating it."""

        deadline = self._operation_deadline(timeout)
        with self._bounded_operation(deadline, stop_event):
            with self._client_lock:
                terminal = self._pending_terminal
                if terminal is None:
                    if self._terminal_replay is not None:
                        return self._terminal_replay.result
                    raise RuntimeError("no terminal command is pending or available for replay")
            return self._wait_for_terminal_locked(terminal, deadline, stop_event)

    def _wait_for_terminal_locked(
        self,
        terminal: _TerminalState,
        deadline: float,
        stop_event: threading.Event | None,
    ) -> SeriesWriteResult | None:
        result = self._wait_prepared_command(
            terminal.command,
            deadline=deadline,
            stop_event=stop_event,
        )
        if result is not None and not isinstance(result, SeriesWriteResult):
            raise RuntimeError("writer returned an invalid terminal result")
        with self._client_lock:
            self._drain_completions_locked()
        if terminal.kind == "shutdown":
            remaining = max(0.0, min(self._join_timeout_s, deadline - time.monotonic()))
            self._thread.join(remaining)
            if self._thread.is_alive():
                raise SeriesWriteTimeout(
                    f"writer thread is still alive after {remaining:.3f}s shutdown join timeout"
                )
        with self._client_lock:
            terminal.result = result
            self._terminal_replay = terminal
            self._pending_terminal = None
            if result is not None:
                self._last_result = result
            self._client_spec = None
            self._client_next_plane = 0
            self._client_committed_layers = 0
            self._client_completed_acks = 0
            self._client_failure_message = ""
            if terminal.kind == "shutdown":
                self._closed = True
        return result

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("series writer is shut down")
        if not self._thread.is_alive():
            raise RuntimeError("series writer thread is not running")

    def _require_client_job(self, expected_kind: SeriesKind) -> SeriesWriteJob:
        self._raise_terminal_in_progress_locked()
        self._ensure_open()
        spec = self._client_spec
        if spec is None:
            raise RuntimeError("no active series writer job")
        if spec.kind != expected_kind:
            raise RuntimeError(f"active job is {spec.kind!r}, not {expected_kind!r}")
        return spec

    def _raise_terminal_in_progress_locked(self) -> None:
        if self._pending_terminal is None:
            return
        raise SeriesTerminalInProgress(
            f"{self._pending_terminal.kind} terminal command is in progress; "
            "no additional layers may be submitted"
        )

    def _raise_client_failure(self) -> None:
        if self._client_failure_message:
            raise SeriesWriteError(self._client_failure_message)

    def _drain_completions_locked(self) -> None:
        active_job_id = self._client_spec.job_id if self._client_spec is not None else None
        while True:
            try:
                completion = self._completion_queue.get_nowait()
            except queue.Empty:
                return
            if completion.job_id != active_job_id:
                continue
            self._client_completed_acks += 1
            if completion.committed:
                self._client_committed_layers += 1
            elif not self._client_failure_message:
                self._client_failure_message = completion.error or (
                    f"plane {completion.plane_index} was not committed"
                )

    def _validate_plane_index(self, plane_index: int, spec: SeriesWriteJob) -> None:
        if int(plane_index) != plane_index:
            raise ValueError("plane_index must be an integer")
        if int(plane_index) != self._client_next_plane:
            raise ValueError(
                f"plane_index must be sequential: expected {self._client_next_plane}, got {plane_index}"
            )
        if int(plane_index) >= spec.total_layers:
            raise ValueError("plane_index exceeds total_layers")

    @staticmethod
    def _validate_z_stack_array(frames: np.ndarray, frame_shape: tuple[int, int]) -> None:
        if not isinstance(frames, np.ndarray) or frames.dtype != np.uint16:
            raise TypeError("frames must be a numpy.uint16 array")
        expected = (9, *frame_shape)
        if frames.shape != expected:
            raise ValueError(f"frames shape must be {expected}, got {frames.shape}")

    @staticmethod
    def _validate_focus_array(frame: np.ndarray, frame_shape: tuple[int, int]) -> None:
        if not isinstance(frame, np.ndarray) or frame.dtype != np.uint16:
            raise TypeError("frame must be a numpy.uint16 array")
        if frame.shape != frame_shape:
            raise ValueError(f"frame shape must be {frame_shape}, got {frame.shape}")

    @staticmethod
    def _finite_float(value: float, name: str) -> float:
        converted = float(value)
        if not math.isfinite(converted):
            raise ValueError(f"{name} must be finite")
        return converted

    @staticmethod
    def _validate_job(spec: SeriesWriteJob) -> SeriesWriteJob:
        if not isinstance(spec, SeriesWriteJob):
            raise TypeError("spec must be a SeriesWriteJob")
        if not str(spec.job_id).strip():
            raise ValueError("job_id must not be empty")
        if spec.kind not in {"zstack", "focus"}:
            raise ValueError(f"unsupported series kind: {spec.kind!r}")
        if int(spec.total_layers) != spec.total_layers or int(spec.total_layers) < 1:
            raise ValueError("total_layers must be a positive integer")
        if len(tuple(spec.frame_shape)) != 2 or any(int(item) < 1 for item in spec.frame_shape):
            raise ValueError("frame_shape must contain two positive integers")
        if int(spec.wavelength_nm) < 1:
            raise ValueError("wavelength_nm must be positive")
        if int(spec.exposure_us) < 1:
            raise ValueError("exposure_us must be positive")
        output_path = Path(spec.output_path)
        csv_path = Path(spec.csv_path) if spec.csv_path is not None else None
        if spec.kind == "focus" and csv_path is None:
            csv_path = output_path.with_suffix(".csv")
        return replace(
            spec,
            job_id=str(spec.job_id),
            output_path=output_path,
            total_layers=int(spec.total_layers),
            frame_shape=(int(spec.frame_shape[0]), int(spec.frame_shape[1])),
            wavelength_nm=int(spec.wavelength_nm),
            exposure_us=int(spec.exposure_us),
            running_order=str(spec.running_order),
            csv_path=csv_path,
        )

    def _operation_deadline(self, timeout: float | None) -> float:
        duration = self._sync_timeout_s if timeout is None else float(timeout)
        if duration <= 0:
            raise ValueError("timeout must be positive")
        return time.monotonic() + duration

    @contextmanager
    def _bounded_operation(
        self,
        deadline: float,
        stop_event: threading.Event | None = None,
    ) -> Iterator[None]:
        while True:
            self._raise_if_stopped(stop_event)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SeriesWriteTimeout("timed out waiting for another writer operation")
            if self._operation_lock.acquire(timeout=min(self._enqueue_poll_s, remaining)):
                break
        try:
            yield
        finally:
            self._operation_lock.release()

    @staticmethod
    def _raise_if_stopped(stop_event: threading.Event | None) -> None:
        if stop_event is not None and stop_event.is_set():
            raise SeriesWriteCancelled("series writer operation cancelled by stop request")

    def _put_stop_aware(
        self,
        command: _Command,
        *,
        deadline: float,
        stop_event: threading.Event | None = None,
        monitor_health: bool = False,
    ) -> None:
        while True:
            self._raise_if_stopped(stop_event)
            self._ensure_open()
            if monitor_health:
                with self._client_lock:
                    self._drain_completions_locked()
                    self._raise_client_failure()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SeriesWriteTimeout("timed out waiting for space in the writer queue")
            try:
                self._queue.put(
                    command,
                    block=True,
                    timeout=min(self._enqueue_poll_s, remaining),
                )
                return
            except queue.Full:
                continue

    def _send_sync(
        self,
        command: _Command,
        *,
        deadline: float,
        stop_event: threading.Event | None = None,
    ) -> Any:
        self._prepare_sync_command(command)
        self._put_stop_aware(command, deadline=deadline, stop_event=stop_event)
        return self._wait_prepared_command(
            command,
            deadline=deadline,
            stop_event=stop_event,
        )

    @staticmethod
    def _prepare_sync_command(command: _Command) -> _Command:
        command.done = threading.Event()
        command.reply = {}
        return command

    def _wait_prepared_command(
        self,
        command: _Command,
        *,
        deadline: float,
        stop_event: threading.Event | None = None,
    ) -> Any:
        if command.done is None or command.reply is None:
            raise RuntimeError("synchronous command was not prepared before waiting")
        while True:
            self._raise_if_stopped(stop_event)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SeriesWriteTimeout(
                    f"writer did not acknowledge {command.name!r} before the timeout"
                )
            if command.done.wait(min(self._enqueue_poll_s, remaining)):
                break
            if not self._thread.is_alive():
                raise SeriesWriteError(
                    f"writer thread stopped before acknowledging {command.name!r}"
                )
        error = command.reply.get("error")
        if error is not None:
            raise error
        return command.reply.get("result")

    def _writer_loop(self) -> None:
        self._writer_thread_ident = threading.get_ident()
        while True:
            command = self._queue.get()
            stop = False
            try:
                result = self._handle_command(command)
                if command.reply is not None:
                    command.reply["result"] = result
                stop = command.name == "shutdown"
            except BaseException as exc:  # synchronous callers must always be released
                if command.reply is not None:
                    command.reply["error"] = exc
                elif self._state is not None and not self._state.failure_message:
                    self._state.failure_message = f"{type(exc).__name__}: {exc}"
            finally:
                if command.done is not None:
                    command.done.set()
                self._queue.task_done()
            if stop:
                return

    def _handle_command(self, command: _Command) -> SeriesWriteResult | None:
        if command.name == "begin":
            if self._state is not None:
                raise RuntimeError("a writer-thread job is already active")
            spec = command.payload["spec"]
            self._state = self._preflight(spec)
            return None
        if command.name == "z_layer":
            self._handle_z_layer(command.payload)
            return None
        if command.name == "focus_layer":
            self._handle_focus_layer(command.payload)
            return None
        if command.name == "barrier":
            return None
        if command.name == "finalize":
            state = self._require_writer_state()
            result = self._finalize_state(
                state,
                command.payload["outcome"],
                command.payload.get("best_plane_index"),
                command.payload.get("message", ""),
            )
            self._state = None
            self._last_result = result
            return result
        if command.name == "shutdown":
            if self._state is None:
                return None
            result = self._finalize_state(
                self._state,
                "cancelled",
                None,
                "writer shutdown before explicit finalization",
            )
            self._state = None
            self._last_result = result
            return result
        raise RuntimeError(f"unknown writer command: {command.name!r}")

    def _require_writer_state(self) -> _WriterJobState:
        if self._state is None:
            raise RuntimeError("no writer-thread job is active")
        return self._state

    def _preflight(self, spec: SeriesWriteJob) -> _WriterJobState:
        if spec.kind == "focus":
            assert spec.csv_path is not None
            resolved_tiff = os.path.normcase(os.fspath(spec.output_path.resolve(strict=False)))
            resolved_csv = os.path.normcase(os.fspath(spec.csv_path.resolve(strict=False)))
            if resolved_tiff == resolved_csv:
                raise ValueError("focus TIFF and CSV output paths must be distinct")
        page_count = spec.total_layers * (9 if spec.kind == "zstack" else 1)
        expected_bytes = page_count * spec.frame_shape[0] * spec.frame_shape[1] * 2
        output_paths = [spec.output_path]
        if spec.csv_path is not None:
            output_paths.append(spec.csv_path)
        parents: dict[str, Path] = {}
        for output_path in output_paths:
            parent = output_path.parent
            parent.mkdir(parents=True, exist_ok=True)
            parent_key = os.path.normcase(os.fspath(parent.resolve(strict=False)))
            parents.setdefault(parent_key, parent)
            if output_path.exists():
                raise FileExistsError(f"output already exists: {output_path}")

        for parent in parents.values():
            self._probe_exclusive_publish(parent)

        usage = self._disk_usage(spec.output_path.parent)
        free_bytes = int(getattr(usage, "free"))
        required_bytes = expected_bytes * 2
        if free_bytes < required_bytes:
            raise OSError(
                "insufficient disk space for two uncompressed copies: "
                f"required {required_bytes} bytes, available {free_bytes} bytes"
            )

        safe_job_id = "".join(character if character.isalnum() or character in "-_" else "_" for character in spec.job_id)
        spool = spec.output_path.parent / (
            f"zseries-spool-{spec.output_path.stem}-{safe_job_id}-{uuid.uuid4().hex}.spool"
        )
        spool.mkdir()
        staging_by_parent: dict[str, Path] = {}
        output_parent_key = os.path.normcase(
            os.fspath(spec.output_path.parent.resolve(strict=False))
        )
        staging_by_parent[output_parent_key] = spool
        created_staging = [spool]
        try:
            for parent_key, parent in parents.items():
                if parent_key in staging_by_parent:
                    continue
                staging = parent / (
                    f"zseries-stage-{safe_job_id}-{uuid.uuid4().hex}.spool"
                )
                staging.mkdir()
                staging_by_parent[parent_key] = staging
                created_staging.append(staging)
        except BaseException:
            for staging in reversed(created_staging):
                shutil.rmtree(staging, ignore_errors=True)
            raise
        return _WriterJobState(
            spec=spec,
            spool_path=spool,
            expected_uncompressed_bytes=expected_bytes,
            staging_by_parent=staging_by_parent,
        )

    @staticmethod
    def _probe_exclusive_publish(parent: Path) -> None:
        """Verify same-volume hard-link publication and no-overwrite semantics."""

        token = uuid.uuid4().hex
        source = parent / f"zseries-publish-probe-{token}.source"
        target = parent / f"zseries-publish-probe-{token}.target"
        source.write_bytes(b"exclusive-publish-probe")
        try:
            os.link(source, target)
            try:
                os.link(source, target)
            except FileExistsError:
                pass
            else:
                raise OSError("exclusive publish probe unexpectedly overwrote its target")
        finally:
            target.unlink(missing_ok=True)
            source.unlink(missing_ok=True)

    def _handle_z_layer(self, payload: dict[str, Any]) -> None:
        state = self._require_writer_state()
        plane_index = int(payload["plane_index"])
        if state.spec.kind != "zstack":
            raise RuntimeError("received a Z-stack layer for a focus job")
        if state.failure_message:
            self._completion_queue.put(
                _Completion(state.spec.job_id, plane_index, False, state.failure_message)
            )
            return
        try:
            chunk = self._write_z_chunk(state, payload)
        except BaseException as exc:
            state.failure_message = f"{type(exc).__name__}: {exc}"
            self._completion_queue.put(
                _Completion(state.spec.job_id, plane_index, False, state.failure_message)
            )
            return
        state.chunks.append(chunk)
        self._completion_queue.put(_Completion(state.spec.job_id, plane_index, True))

    def _handle_focus_layer(self, payload: dict[str, Any]) -> None:
        state = self._require_writer_state()
        plane_index = int(payload["plane_index"])
        if state.spec.kind != "focus":
            raise RuntimeError("received a focus layer for a Z-stack job")
        if state.failure_message:
            self._completion_queue.put(
                _Completion(state.spec.job_id, plane_index, False, state.failure_message)
            )
            return
        try:
            chunk, row = self._write_focus_chunk(state, payload)
        except BaseException as exc:
            state.failure_message = f"{type(exc).__name__}: {exc}"
            self._completion_queue.put(
                _Completion(state.spec.job_id, plane_index, False, state.failure_message)
            )
            return
        state.chunks.append(chunk)
        state.focus_rows.append(row)
        self._completion_queue.put(_Completion(state.spec.job_id, plane_index, True))

    def _write_z_chunk(self, state: _WriterJobState, payload: dict[str, Any]) -> Path:
        plane_index = int(payload["plane_index"])
        chunk = state.spool_path / f"plane-{plane_index:06d}.tif"
        assembling = state.spool_path / f"plane-{plane_index:06d}.assembling.tif"
        with tifffile.TiffWriter(assembling) as writer:
            for frame_index, frame in enumerate(payload["frames"]):
                description = json.dumps(
                    {
                        "parent_task_id": state.spec.job_id,
                        "batch_id": payload["batch_id"],
                        "plane_index": plane_index,
                        "frame_index": frame_index,
                        "requested_z_um": payload["requested_z_um"],
                        "measured_z_um": payload["measured_z_um"],
                        "wavelength_nm": state.spec.wavelength_nm,
                        "exposure_us": state.spec.exposure_us,
                        "timestamp": payload["timestamp"],
                        "running_order": state.spec.running_order,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                writer.write(
                    frame,
                    description=description,
                    metadata=None,
                    compression=None,
                    photometric="minisblack",
                )
        os.replace(assembling, chunk)
        return chunk

    def _write_focus_chunk(
        self,
        state: _WriterJobState,
        payload: dict[str, Any],
    ) -> tuple[Path, dict[str, Any]]:
        plane_index = int(payload["plane_index"])
        chunk = state.spool_path / f"plane-{plane_index:06d}.tif"
        assembling = state.spool_path / f"plane-{plane_index:06d}.assembling.tif"
        row: dict[str, Any] = {
            "parent_task_id": state.spec.job_id,
            "plane_index": plane_index,
            "requested_z_um": payload["requested_z_um"],
            "measured_z_um": payload["measured_z_um"],
            "sml_score": payload["sml_score"],
            "is_best": False,
            "wavelength_nm": state.spec.wavelength_nm,
            "exposure_us": state.spec.exposure_us,
            "timestamp": payload["timestamp"],
            "running_order": state.spec.running_order,
        }
        tifffile.imwrite(
            assembling,
            payload["frame"],
            description=json.dumps(row, ensure_ascii=False, separators=(",", ":")),
            metadata=None,
            compression=None,
            photometric="minisblack",
        )
        os.replace(assembling, chunk)
        return chunk, row

    def _finalize_state(
        self,
        state: _WriterJobState,
        requested_outcome: SeriesOutcome,
        best_plane_index: int | None,
        message: str,
    ) -> SeriesWriteResult:
        completed = len(state.chunks)
        effective_outcome: SeriesOutcome = requested_outcome
        failure = state.failure_message
        if failure:
            effective_outcome = "failed"
        elif requested_outcome == "complete" and completed != state.spec.total_layers:
            effective_outcome = "failed"
            failure = f"only {completed} of {state.spec.total_layers} layers were committed"

        partial = effective_outcome != "complete" or completed != state.spec.total_layers
        bigtiff = state.expected_uncompressed_bytes >= self._bigtiff_threshold_bytes
        outputs: tuple[Path, ...] = ()
        spool_path: Path | None = state.spool_path
        staging_paths: tuple[Path, ...] = self._existing_staging_paths(state)
        merged_message = _join_messages(message, failure)

        if completed:
            if partial:
                tiff_target = _partial_path(state.spec.output_path, completed, state.spec.total_layers)
            else:
                tiff_target = state.spec.output_path
            try:
                if state.spec.kind == "zstack":
                    self._merge_z_stack(state, tiff_target, bigtiff)
                    outputs = (tiff_target,)
                else:
                    assert state.spec.csv_path is not None
                    csv_target = (
                        _partial_path(state.spec.csv_path, completed, state.spec.total_layers)
                        if partial
                        else state.spec.csv_path
                    )
                    self._merge_focus(
                        state,
                        tiff_target,
                        csv_target,
                        bigtiff,
                        best_plane_index,
                        allow_uncommitted_best=partial,
                    )
                    outputs = (tiff_target, csv_target)
            except BaseException as exc:
                effective_outcome = "failed"
                partial = True
                outputs = exc.published_paths if isinstance(exc, _PartialPublishError) else ()
                merged_message = _join_messages(
                    merged_message,
                    f"merge failed: {type(exc).__name__}: {exc}",
                )
            else:
                try:
                    self._cleanup_staging(state)
                    spool_path = None
                    staging_paths = ()
                except OSError as exc:
                    merged_message = _join_messages(
                        merged_message,
                        f"output saved but staging cleanup failed: {exc}",
                    )
                    spool_path = state.spool_path if state.spool_path.exists() else None
                    staging_paths = self._existing_staging_paths(state)
        else:
            try:
                self._cleanup_staging(state)
                spool_path = None
                staging_paths = ()
            except OSError as exc:
                merged_message = _join_messages(merged_message, f"staging cleanup failed: {exc}")
                spool_path = state.spool_path if state.spool_path.exists() else None
                staging_paths = self._existing_staging_paths(state)

        if outputs == ():
            spool_path = state.spool_path if state.spool_path.exists() else None
            staging_paths = self._existing_staging_paths(state)

        return SeriesWriteResult(
            job_id=state.spec.job_id,
            kind=state.spec.kind,
            outcome=effective_outcome,
            completed_layers=completed,
            total_layers=state.spec.total_layers,
            output_paths=outputs,
            partial=partial,
            bigtiff=bigtiff,
            message=merged_message,
            spool_path=spool_path,
            staging_paths=staging_paths,
        )

    def _merge_z_stack(
        self,
        state: _WriterJobState,
        target: Path,
        bigtiff: bool,
    ) -> None:
        assembling = self._stage_path(state, target, "zstack-merged.tif")
        self._ensure_targets_absent(assembling)
        with tifffile.TiffWriter(assembling, bigtiff=bigtiff) as output:
            for chunk_path in state.chunks:
                with tifffile.TiffFile(chunk_path) as chunk:
                    for page in chunk.pages:
                        output.write(
                            page.asarray(),
                            description=page.description,
                            metadata=None,
                            compression=None,
                            photometric="minisblack",
                        )
        self._publish_staged_file(assembling, target)

    def _merge_focus(
        self,
        state: _WriterJobState,
        tiff_target: Path,
        csv_target: Path,
        bigtiff: bool,
        best_plane_index: int | None,
        *,
        allow_uncommitted_best: bool,
    ) -> None:
        self._ensure_targets_absent(tiff_target, csv_target)
        committed_plane_indices = {int(row["plane_index"]) for row in state.focus_rows}
        if best_plane_index is None or (
            allow_uncommitted_best and int(best_plane_index) not in committed_plane_indices
        ):
            best_plane_index = max(
                state.focus_rows,
                key=lambda row: float(row["sml_score"]),
            )["plane_index"]
        if int(best_plane_index) not in committed_plane_indices:
            raise ValueError(f"best_plane_index {best_plane_index} is not committed")

        rows: list[dict[str, Any]] = []
        for row in state.focus_rows:
            updated = dict(row)
            updated["is_best"] = int(updated["plane_index"]) == int(best_plane_index)
            rows.append(updated)

        tiff_assembling = self._stage_path(state, tiff_target, "focus-merged.tif")
        csv_assembling = self._stage_path(state, csv_target, "focus-merged.csv")
        self._ensure_targets_absent(tiff_assembling, csv_assembling)
        with tifffile.TiffWriter(tiff_assembling, bigtiff=bigtiff) as output:
            for chunk_path, row in zip(state.chunks, rows, strict=True):
                with tifffile.TiffFile(chunk_path) as chunk:
                    output.write(
                        chunk.pages[0].asarray(),
                        description=json.dumps(row, ensure_ascii=False, separators=(",", ":")),
                        metadata=None,
                        compression=None,
                        photometric="minisblack",
                    )

        fieldnames = [
            "parent_task_id",
            "plane_index",
            "requested_z_um",
            "measured_z_um",
            "sml_score",
            "is_best",
            "wavelength_nm",
            "exposure_us",
            "timestamp",
            "running_order",
        ]
        with csv_assembling.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        published_paths: list[Path] = []
        try:
            self._publish_staged_file(tiff_assembling, tiff_target)
            published_paths.append(tiff_target)
            self._publish_staged_file(csv_assembling, csv_target)
            published_paths.append(csv_target)
        except BaseException as publish_exc:
            detail = f"focus output publish failed: {publish_exc}"
            raise _PartialPublishError(detail, tuple(published_paths)) from publish_exc

    def _publish_staged_file(self, stage: Path, target: Path) -> None:
        """Publish exclusively; staging cleanup is a later independent step."""

        if target.exists():
            raise FileExistsError(f"refusing to overwrite existing path: {target}")
        os.link(stage, target)

    @staticmethod
    def _parent_key(parent: Path) -> str:
        return os.path.normcase(os.fspath(parent.resolve(strict=False)))

    def _stage_path(
        self,
        state: _WriterJobState,
        target: Path,
        filename: str,
    ) -> Path:
        staging = state.staging_by_parent.get(self._parent_key(target.parent))
        if staging is None:
            raise RuntimeError(f"no preflighted staging directory for {target.parent}")
        return staging / filename

    @staticmethod
    def _existing_staging_paths(state: _WriterJobState) -> tuple[Path, ...]:
        return tuple(
            path
            for path in dict.fromkeys(state.staging_by_parent.values())
            if path.exists()
        )

    @staticmethod
    def _cleanup_staging(state: _WriterJobState) -> None:
        for staging in reversed(tuple(dict.fromkeys(state.staging_by_parent.values()))):
            if staging.exists():
                shutil.rmtree(staging)

    @staticmethod
    def _ensure_targets_absent(*paths: Path) -> None:
        for path in paths:
            if path.exists():
                raise FileExistsError(f"refusing to overwrite existing path: {path}")


__all__ = [
    "DEFAULT_BIGTIFF_THRESHOLD_BYTES",
    "SeriesWriteCancelled",
    "SeriesWriteError",
    "SeriesWriteHealth",
    "SeriesWriteJob",
    "SeriesWriteResult",
    "SeriesTerminalInProgress",
    "SeriesWriteTimeout",
    "ZStackAsyncWriter",
]

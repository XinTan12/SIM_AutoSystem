from __future__ import annotations

import threading
import time
import traceback

from PyQt5.QtCore import QCoreApplication, QObject, QThread, pyqtSignal, pyqtSlot

from .adapters import FusionBtCameraAdapter, HardwareError
from .models import CameraConfig


class SimPreviewWorker(QObject):
    signal_frame_ready = pyqtSignal(object, int)
    signal_status_changed = pyqtSignal(str, dict)
    signal_error = pyqtSignal(str)

    def __init__(self, gui_preview_fps_limit: int = 30) -> None:
        super().__init__()
        self._running = False
        self._stop_requested = threading.Event()
        self._camera: FusionBtCameraAdapter | None = None
        self._config = CameraConfig()
        self._timeout_ms = 100
        self._gui_preview_fps_limit = max(1, int(gui_preview_fps_limit))
        self._gui_preview_interval_s = 1.0 / float(self._gui_preview_fps_limit)
        self._last_frame_emit_at: float | None = None

    def prepare_for_start(self) -> None:
        self._running = False
        self._stop_requested.clear()

    def request_stop(self) -> None:
        self._running = False
        self._stop_requested.set()

    def _should_emit_frame(self, now: float) -> bool:
        return self._last_frame_emit_at is None or (now - self._last_frame_emit_at) >= self._gui_preview_interval_s

    def emit_preview_frame(self, frame, fps: int) -> bool:
        now = time.perf_counter()
        if not self._should_emit_frame(now):
            return False
        self._last_frame_emit_at = now
        self.signal_frame_ready.emit(frame, fps)
        return True

    @pyqtSlot(object)
    def slot_start(self, payload: dict) -> None:
        self._camera = payload["camera"]
        self._config = payload["config"]
        self._timeout_ms = int(payload.get("timeout_ms", 100))
        self._last_frame_emit_at = None
        self._running = True
        frame_counter = 0
        last_fps_at = time.perf_counter()
        fps = 0
        try:
            if self._stop_requested.is_set():
                return
            self._camera.start_preview(self._config)
            if self._stop_requested.is_set():
                return
            self.signal_status_changed.emit("preview_started", {"camera_config": self._config.__dict__})
            while self._running and not self._stop_requested.is_set():
                try:
                    frame = self._camera.read_preview_frame(self._timeout_ms)
                except HardwareError:
                    if self._stop_requested.is_set() or not self._running or not self._camera.preview_active:
                        break
                    raise
                if self._stop_requested.is_set():
                    break
                frame_counter += 1
                now = time.perf_counter()
                elapsed = now - last_fps_at
                if elapsed >= 1.0:
                    fps = int(round(frame_counter / elapsed))
                    frame_counter = 0
                    last_fps_at = now
                self.emit_preview_frame(frame, fps)
        except Exception as exc:
            self.signal_error.emit(f"{exc}\n{traceback.format_exc()}")
        finally:
            self._running = False
            if self._camera is not None:
                try:
                    self._camera.stop_preview()
                except Exception:
                    pass
            self.signal_status_changed.emit("preview_stopped", {})

    @pyqtSlot()
    def slot_stop(self) -> None:
        self.request_stop()


class SimPreviewController(QObject):
    signal_frame_ready = pyqtSignal(object, int)
    signal_status_changed = pyqtSignal(str, dict)
    signal_error = pyqtSignal(str)
    signal_start_worker = pyqtSignal(object)
    signal_stop_worker = pyqtSignal()

    def __init__(self, camera: FusionBtCameraAdapter, parent: QObject | None = None, gui_preview_fps_limit: int = 30) -> None:
        super().__init__(parent)
        self.camera = camera
        self._thread = QThread(self)
        self._worker = SimPreviewWorker(gui_preview_fps_limit=gui_preview_fps_limit)
        self._worker.moveToThread(self._thread)
        self._worker.signal_frame_ready.connect(self.signal_frame_ready)
        self._worker.signal_status_changed.connect(self._handle_worker_status)
        self._worker.signal_status_changed.connect(self.signal_status_changed)
        self._worker.signal_error.connect(self.signal_error)
        self.signal_start_worker.connect(self._worker.slot_start)
        self.signal_stop_worker.connect(self._worker.slot_stop)
        self._thread.start()
        self._active = False
        self._stopping = False

    @property
    def active(self) -> bool:
        return self._active

    @property
    def stopping(self) -> bool:
        return self._stopping

    @pyqtSlot(str, dict)
    def _handle_worker_status(self, status: str, payload: dict) -> None:
        if status == "preview_started":
            self._active = True
            self._stopping = False
        elif status == "preview_stopped":
            self._active = False
            self._stopping = False

    def start(self, config: CameraConfig, timeout_ms: int = 100) -> None:
        if self._active or self.stopping:
            raise RuntimeError("SIM preview is already active or stopping.")
        self._worker.prepare_for_start()
        self._active = True
        self.signal_start_worker.emit(
            {
                "camera": self.camera,
                "config": config,
                "timeout_ms": timeout_ms,
            }
        )

    def stop(self, wait: bool = False) -> None:
        if not self._active and not self.stopping:
            return
        self._worker.request_stop()
        if not self.stopping:
            self._active = False
            self._stopping = True
        if wait:
            deadline = time.time() + 2.0
            while self._stopping and time.time() < deadline:
                QCoreApplication.processEvents()
                QThread.msleep(10)

    def shutdown(self) -> None:
        self.stop(wait=True)
        self._thread.quit()
        self._thread.wait(2000)

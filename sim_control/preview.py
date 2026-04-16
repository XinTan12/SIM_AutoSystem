from __future__ import annotations

import time
import traceback

from PyQt5.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

from .adapters import FusionBtCameraAdapter, HardwareError
from .models import CameraConfig


class SimPreviewWorker(QObject):
    signal_frame_ready = pyqtSignal(object, int)
    signal_status_changed = pyqtSignal(str, dict)
    signal_error = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self._running = False
        self._camera: FusionBtCameraAdapter | None = None
        self._config = CameraConfig()
        self._timeout_ms = 100

    @pyqtSlot(object)
    def slot_start(self, payload: dict) -> None:
        self._camera = payload["camera"]
        self._config = payload["config"]
        self._timeout_ms = int(payload.get("timeout_ms", 100))
        self._running = True
        frame_counter = 0
        last_fps_at = time.perf_counter()
        fps = 0
        try:
            self._camera.start_preview(self._config)
            self.signal_status_changed.emit("preview_started", {"camera_config": self._config.__dict__})
            while self._running:
                try:
                    frame = self._camera.read_preview_frame(self._timeout_ms)
                except HardwareError:
                    if not self._running or not self._camera.preview_active:
                        break
                    raise
                frame_counter += 1
                now = time.perf_counter()
                elapsed = now - last_fps_at
                if elapsed >= 1.0:
                    fps = int(round(frame_counter / elapsed))
                    frame_counter = 0
                    last_fps_at = now
                self.signal_frame_ready.emit(frame, fps)
        except Exception as exc:
            self.signal_error.emit(f"{exc}\n{traceback.format_exc()}")
        finally:
            if self._camera is not None:
                try:
                    self._camera.stop_preview()
                except Exception:
                    pass
            self.signal_status_changed.emit("preview_stopped", {})

    @pyqtSlot()
    def slot_stop(self) -> None:
        self._running = False


class SimPreviewController(QObject):
    signal_frame_ready = pyqtSignal(object, int)
    signal_status_changed = pyqtSignal(str, dict)
    signal_error = pyqtSignal(str)
    signal_start_worker = pyqtSignal(object)
    signal_stop_worker = pyqtSignal()

    def __init__(self, camera: FusionBtCameraAdapter, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.camera = camera
        self._thread = QThread(self)
        self._worker = SimPreviewWorker()
        self._worker.moveToThread(self._thread)
        self._worker.signal_frame_ready.connect(self.signal_frame_ready)
        self._worker.signal_status_changed.connect(self.signal_status_changed)
        self._worker.signal_error.connect(self.signal_error)
        self.signal_start_worker.connect(self._worker.slot_start)
        self.signal_stop_worker.connect(self._worker.slot_stop)
        self._thread.start()
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def start(self, config: CameraConfig, timeout_ms: int = 100) -> None:
        self.stop(wait=True)
        self._active = True
        self.signal_start_worker.emit(
            {
                "camera": self.camera,
                "config": config,
                "timeout_ms": timeout_ms,
            }
        )

    def stop(self, wait: bool = False) -> None:
        if not self._active:
            return
        self._active = False
        self.signal_stop_worker.emit()
        if wait:
            deadline = time.time() + 2.0
            while self.camera.preview_active and time.time() < deadline:
                QThread.msleep(50)

    def shutdown(self) -> None:
        self.stop(wait=True)
        self._thread.quit()
        self._thread.wait(2000)

"""SIM live 预览的后台 worker 和 latest-frame-wins 缓存。

预览线程持续从相机 adapter 读取最新帧，按 FPS 限制发布快照；GUI 端通过定时器取走最后一帧，中间旧帧可以被覆盖丢弃，从而保持低延迟显示而不是积压队列。
"""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
import traceback

from PyQt5.QtCore import QCoreApplication, QObject, QThread, pyqtSignal, pyqtSlot

from .adapters import FusionBtCameraAdapter, HardwareError
from .models import CameraConfig


@dataclass
class PreviewFrameSnapshot:
    """GUI 轮询时取走的预览帧快照，包含帧、FPS、序号和时间戳。"""
    frame: object
    fps: int
    sequence: int
    captured_at: float


# 预览 worker 持续读相机帧，但只发布最新快照，避免 GUI 处理陈旧帧队列。
class SimPreviewWorker(QObject):
    """相机预览后台 worker，持续读帧并发布 latest-frame-wins 快照。"""
    signal_status_changed = pyqtSignal(str, dict)
    signal_error = pyqtSignal(str)

    def __init__(self, gui_preview_fps_limit: int = 30) -> None:
        super().__init__()
        self._stop_requested = threading.Event()
        self._camera: FusionBtCameraAdapter | None = None
        self._config = CameraConfig()
        self._timeout_ms = 100
        self._snapshot_lock = threading.Lock()
        self._latest_snapshot: PreviewFrameSnapshot | None = None
        self._frame_sequence = 0

    def prepare_for_start(self) -> None:
        self._stop_requested.clear()
        with self._snapshot_lock:
            self._latest_snapshot = None
            self._frame_sequence = 0

    def request_stop(self) -> None:
        self._stop_requested.set()
        with self._snapshot_lock:
            self._latest_snapshot = None

    def publish_preview_frame(self, frame, fps: int) -> PreviewFrameSnapshot:
        """采集线程只发布最新预览帧，旧帧可被覆盖以避免 GUI 积压。"""
        with self._snapshot_lock:
            self._frame_sequence += 1
            snapshot = PreviewFrameSnapshot(
                frame=frame,
                fps=int(fps),
                sequence=self._frame_sequence,
                captured_at=time.perf_counter(),
            )
            self._latest_snapshot = snapshot
        return snapshot

    def take_latest_frame(self) -> PreviewFrameSnapshot | None:
        """GUI 定时器取走最新帧快照；取走后清空槽位实现 latest-frame-wins。"""
        with self._snapshot_lock:
            snapshot = self._latest_snapshot
            self._latest_snapshot = None
        return snapshot

    @pyqtSlot(object)
    def slot_start(self, payload: dict) -> None:
        """处理 Qt 信号或后台状态回调，并把结果更新到界面状态。"""
        self._camera = payload["camera"]
        self._config = payload["config"]
        self._timeout_ms = int(payload.get("timeout_ms", 100))
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
            while not self._stop_requested.is_set():
                try:
                    frame = self._camera.read_preview_frame(self._timeout_ms)
                except HardwareError:
                    if self._stop_requested.is_set() or not self._camera.preview_active:
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
                self.publish_preview_frame(frame, fps)
        except Exception as exc:
            self.signal_error.emit(f"{exc}\n{traceback.format_exc()}")
        finally:
            if self._camera is not None:
                try:
                    self._camera.stop_preview()
                except Exception:
                    pass
            with self._snapshot_lock:
                self._latest_snapshot = None
            self.signal_status_changed.emit("preview_stopped", {})

    @pyqtSlot()
    def slot_stop(self) -> None:
        """处理 Qt 信号或后台状态回调，并把结果更新到界面状态。"""
        self.request_stop()


# 预览 controller 管理 worker 线程生命周期，并向主界面暴露 start/stop/take_latest_frame。
class SimPreviewController(QObject):
    """预览线程控制器，管理 worker 生命周期和 GUI 状态信号。"""
    signal_status_changed = pyqtSignal(str, dict)
    signal_error = pyqtSignal(str)
    signal_start_worker = pyqtSignal(object)
    signal_stop_worker = pyqtSignal()

    def __init__(self, camera: FusionBtCameraAdapter, parent: QObject | None = None, gui_preview_fps_limit: int = 30) -> None:
        super().__init__(parent)
        self.camera = camera
        self.frame_poll_interval_ms = max(1, int(round(1000.0 / float(max(1, int(gui_preview_fps_limit))))))
        self._thread = QThread(self)
        self._worker = SimPreviewWorker(gui_preview_fps_limit=gui_preview_fps_limit)
        self._worker.moveToThread(self._thread)
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
        """处理 Qt 信号或后台状态回调，并把结果更新到界面状态。"""
        if status == "preview_started":
            self._active = True
            self._stopping = False
        elif status == "preview_stopped":
            self._active = False
            self._stopping = False

    def take_latest_frame(self) -> PreviewFrameSnapshot | None:
        """转交 worker 的 latest-frame-wins 快照给 GUI 定时器。"""
        return self._worker.take_latest_frame()

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

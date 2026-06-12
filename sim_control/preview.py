"""SIM live 预览的后台 worker 和 latest-frame-wins 缓存。

作用：
    SIM live 预览需要"低延迟、可丢弃旧帧"。本文件实现：
        - ``SimPreviewWorker``：运行在 QThread 中，持续从 ``FusionBtCameraAdapter``
          读取最新预览帧，按 FPS 限制把帧打包成 ``PreviewFrameSnapshot`` 并写入
          受锁保护的 ``_latest_snapshot`` 槽位。
        - ``SimPreviewController``：GUI 端门面，维护 worker 生命周期，并对外暴露
          ``start`` / ``stop`` / ``take_latest_frame`` 三个核心方法。GUI 用 QTimer
          定时调 ``take_latest_frame()`` 取最新快照展示，中间被覆盖的旧帧自动丢弃，
          从而避免显示队列积压老旧帧。

协作关系：
    上游：``sim_control/gui.py``、``control_wangbo/main.py`` 的 SIM 预览面板。
    下游：``adapters.FusionBtCameraAdapter``、``models.CameraConfig``。

关键概念：
    - ``latest-frame-wins``：发布端总是覆盖旧帧；消费端取一次后清空槽位。
    - ``_snapshot_lock``：保护 ``_latest_snapshot`` 和 ``_frame_sequence`` 的
      ``threading.Lock``。读写都在锁内完成，避免 GUI 取走"半构造"的快照。
    - ``preview_started`` / ``preview_stopped`` 状态信号：GUI 据此切换按钮可用状态。

维护要点：
    - **不要**把 ``_latest_snapshot`` 改成 ``queue.Queue``：那是 FIFO 模型，会
      导致 GUI 处理一帧时积累若干旧帧，违背 latest-frame-wins 语义。
    - worker 必须在 GUI 线程之外运行，否则相机读出会阻塞主界面。
"""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
import traceback

from PyQt5.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

from .adapters import FusionBtCameraAdapter, HardwareError
from .models import CameraConfig


@dataclass
class PreviewFrameSnapshot:
    """GUI 轮询时取走的预览帧快照。

    职责：
        - ``frame``：``numpy.uint16`` 帧（GUI 层做 LUT/缩放展示）。
        - ``fps``：worker 估算的瞬时 FPS（仅展示用，非精确测量）。
        - ``sequence``：单调递增帧序号，用于诊断丢帧统计。
        - ``captured_at``：``time.perf_counter()``，便于计算 GUI 显示延迟。
    """
    frame: object
    fps: int
    sequence: int
    captured_at: float


# 预览 worker 持续读相机帧，但只发布最新快照，避免 GUI 处理陈旧帧队列。
class SimPreviewWorker(QObject):
    """相机预览后台 worker，持续读帧并发布 latest-frame-wins 快照。

    线程模型：
        - ``__init__`` 在 GUI 线程构造；
        - ``moveToThread(QThread)`` 后所有 slot 都在工作线程执行；
        - ``request_stop`` / ``publish_preview_frame`` 等方法是线程安全的，
          通过 ``_snapshot_lock`` 保护共享状态。
    """
    # 状态变更通知：GUI 据此切换"开始预览/停止预览"按钮。
    signal_status_changed = pyqtSignal(str, dict)
    # 错误广播：worker 捕获异常后把堆栈文本一次性发回 GUI。
    signal_error = pyqtSignal(str)

    def __init__(self) -> None:
        # 1) 调父类构造让 QObject 信号系统就绪。
        super().__init__()
        # 2) ``threading.Event`` 比 pyqtSignal 更适合做"快速取消标志"，因为 worker
        #    内部循环不希望被 Qt 事件循环转发开销影响。
        self._stop_requested = threading.Event()
        # 3) ``_camera`` / ``_config`` 在 ``slot_start`` 中被填充；声明在这里仅为类型提示。
        self._camera: FusionBtCameraAdapter | None = None
        self._config = CameraConfig()
        self._timeout_ms = 100
        # 4) ``_snapshot_lock`` 保护 ``_latest_snapshot`` 与 ``_frame_sequence`` 两个共享字段。
        self._snapshot_lock = threading.Lock()
        self._latest_snapshot: PreviewFrameSnapshot | None = None
        self._frame_sequence = 0
        # 5) ``_stopped_event``：worker 循环真正退出（slot_start 的 finally 跑完）后置位；
        #    controller 的 ``stop(wait=True)`` 直接在该 Event 上等待，不再泵 Qt 事件循环。
        self._stopped_event = threading.Event()
        # 6) ``_generation``：预览代数。controller 每次 start 前递增并经 prepare_for_start
        #    写入；worker 把它盖在状态 payload 上，controller 据此丢弃"上一代"迟到的
        #    preview_stopped，避免它把新一代的 active 状态错误清零。
        self._generation = 0

    def prepare_for_start(self, generation: int = 0) -> None:
        """把内部状态恢复到"未启动"基线，供 controller 在每次重启前调用。"""
        # 清取消标志 + 清最新帧 + 重置帧序号；保证下一次启动从 0 开始计数。
        # ``generation`` 在 GUI 线程写入、worker 线程读取：本调用发生在 start 信号
        # 投递之前，且上一代 worker 已退出（stop(wait=True) 或收到 stopped 信号后
        # 才允许重启），不存在并发写读。
        self._generation = int(generation)
        self._stop_requested.clear()
        self._stopped_event.clear()
        with self._snapshot_lock:
            self._latest_snapshot = None
            self._frame_sequence = 0

    def wait_until_stopped(self, timeout_s: float) -> bool:
        """阻塞等待预览循环退出；返回是否在超时内退出。线程安全，不进 Qt 事件循环。"""
        return self._stopped_event.wait(timeout_s)

    def request_stop(self) -> None:
        """从任意线程请求停止预览循环；线程安全。"""
        # 1) 置位取消标志；worker 主循环每次迭代都检查它。
        self._stop_requested.set()
        # 2) 同时清最新快照，防止 GUI 在停止过程中还取到旧帧造成视觉错位。
        with self._snapshot_lock:
            self._latest_snapshot = None

    def publish_preview_frame(self, frame, fps: int) -> PreviewFrameSnapshot:
        """采集线程只发布最新预览帧，旧帧可被覆盖以避免 GUI 积压。"""
        # 锁内完成"递增序号 + 构造快照 + 写回槽位"三件事，保证 GUI 不会取到半成品。
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
        # 锁内完成"读 + 清"原子操作；返回值可能是 None 表示尚无新帧。
        with self._snapshot_lock:
            snapshot = self._latest_snapshot
            self._latest_snapshot = None
        return snapshot

    @pyqtSlot(object)
    def slot_start(self, payload: dict) -> None:
        """Qt 信号入口：在工作线程启动预览循环。

        payload 结构：``{"camera": adapter, "config": CameraConfig, "timeout_ms": int}``。
        副作用：
            - 调用 ``camera.start_preview(config)`` 开启硬件预览。
            - 持续读帧并 ``publish_preview_frame`` 直到 ``_stop_requested`` 置位或
              相机自身关闭预览。
            - 退出时（无论成功或异常）都调用 ``stop_preview()`` 并广播
              ``preview_stopped``。
        """
        # 1) 从 payload 取出 worker 运行所需的 3 个变量。
        self._camera = payload["camera"]
        self._config = payload["config"]
        self._timeout_ms = int(payload.get("timeout_ms", 100))
        # 1b) 入口处把当前代数捕获成局部变量：本轮 started/stopped 两个信号必须
        #     盖同一代数戳，即便极端情况下 _generation 字段中途被新一轮改写。
        generation = self._generation
        # 2) FPS 估算的累计字段；每秒重置一次。
        frame_counter = 0
        last_fps_at = time.perf_counter()
        fps = 0
        try:
            # 3) 启动相机预览前先检查取消信号，避免"未启动就被请求停止"的并发竞争。
            if self._stop_requested.is_set():
                return
            self._camera.start_preview(self._config)
            if self._stop_requested.is_set():
                return
            # 4) 广播"预览已启动"，GUI 据此切换按钮可用状态。
            self.signal_status_changed.emit(
                "preview_started",
                {"camera_config": self._config.__dict__, "generation": generation},
            )
            # 5) 主循环：持续读帧；HardwareError 在 preview 正常停止时被吞掉。
            while not self._stop_requested.is_set():
                try:
                    frame = self._camera.read_preview_frame(self._timeout_ms)
                except HardwareError:
                    # 真实相机有时在 stop_preview 后再读会抛 HardwareError；
                    # 若 stop_requested 已置位或相机已退出预览，视为正常退出。
                    if self._stop_requested.is_set() or not self._camera.preview_active:
                        break
                    raise
                # 6) 拿到帧后再次检查取消信号，避免把停止后的帧发到 GUI。
                if self._stop_requested.is_set():
                    break
                # 7) FPS 估算：累计 1 秒内的帧数，再除以时长得到瞬时 FPS。
                frame_counter += 1
                now = time.perf_counter()
                elapsed = now - last_fps_at
                if elapsed >= 1.0:
                    fps = int(round(frame_counter / elapsed))
                    frame_counter = 0
                    last_fps_at = now
                # 8) 发布最新帧快照；旧帧自动被覆盖。
                self.publish_preview_frame(frame, fps)
        except Exception as exc:
            # 9) 任何未预料的异常都通过 ``signal_error`` 发回 GUI；带堆栈便于排查。
            self.signal_error.emit(f"{exc}\n{traceback.format_exc()}")
        finally:
            # 10) 不论成功或异常，最后都要把硬件预览关掉。
            if self._camera is not None:
                try:
                    self._camera.stop_preview()
                except Exception:
                    pass
            # 11) 清最新快照并广播"预览已停止"，让 GUI 进入空闲态。
            with self._snapshot_lock:
                self._latest_snapshot = None
            self.signal_status_changed.emit("preview_stopped", {"generation": generation})
            # 12) 最后置位 stopped event（先 emit 后 set：保证等待方醒来时 stopped
            #     信号已经在 Qt 队列里，不会丢广播）。
            self._stopped_event.set()

    @pyqtSlot()
    def slot_stop(self) -> None:
        """Qt 信号入口：触发停止请求，等价于 ``request_stop()``。"""
        self.request_stop()


# 预览 controller 管理 worker 线程生命周期，并向主界面暴露 start/stop/take_latest_frame。
class SimPreviewController(QObject):
    """预览线程控制器，管理 worker 生命周期和 GUI 状态信号。

    职责：
        - 拥有一个独立的 ``QThread``，把 ``SimPreviewWorker`` 移过去。
        - 用 ``signal_start_worker`` / ``signal_stop_worker`` 把 GUI 请求转给 worker。
        - 转发 worker 的状态/错误信号给 GUI；维护 ``active`` / ``stopping`` 状态。
        - 提供 ``take_latest_frame()`` 让 GUI QTimer 取最新帧。

    协作：
        被 ``SimControlWindow`` 或集成主界面持有；不直接持有相机生命周期，
        只复用上层共享的 ``FusionBtCameraAdapter``。
    """
    signal_status_changed = pyqtSignal(str, dict)
    signal_error = pyqtSignal(str)
    # 跨线程信号：把 ``start`` payload 投递到 worker 的 ``slot_start``。
    signal_start_worker = pyqtSignal(object)
    signal_stop_worker = pyqtSignal()

    def __init__(self, camera: FusionBtCameraAdapter, parent: QObject | None = None, gui_preview_fps_limit: int = 30) -> None:
        # 1) 调父类构造，挂到 parent 对象树便于自动清理。
        super().__init__(parent)
        self.camera = camera
        # 2) 把 FPS 限制换算成轮询间隔（ms），供 GUI QTimer 设定 interval。
        self.frame_poll_interval_ms = max(1, int(round(1000.0 / float(max(1, int(gui_preview_fps_limit))))))
        # 3) 创建独立 QThread + worker，把 worker 移过去；此后 worker 的 slot 都在工作线程执行。
        self._thread = QThread(self)
        self._worker = SimPreviewWorker()
        self._worker.moveToThread(self._thread)
        # 4) 连接信号：worker → controller 的 ``_handle_worker_status``（用于本地状态机），
        #    状态信号只连到 ``_handle_worker_status``，由它过滤"上一代"迟到的
        #    preview_stopped 后再对外转发（见 _handle_worker_status 注释）。
        self._worker.signal_status_changed.connect(self._handle_worker_status)
        self._worker.signal_error.connect(self.signal_error)
        self.signal_start_worker.connect(self._worker.slot_start)
        self.signal_stop_worker.connect(self._worker.slot_stop)
        # 5) 立即启动线程；线程 idle 等待 ``signal_start_worker`` 投递。
        self._thread.start()
        # 6) 状态字段：``active`` 表示 worker 正在跑；``stopping`` 表示请求了停止但未确认结束。
        self._active = False
        self._stopping = False
        # 7) 预览代数：每次 start 前递增；与 worker payload 里的 "generation" 对账。
        self._generation = 0

    @property
    def active(self) -> bool:
        return self._active

    @property
    def stopping(self) -> bool:
        return self._stopping

    @pyqtSlot(str, dict)
    def _handle_worker_status(self, status: str, payload: dict) -> None:
        """维护本地 ``_active`` / ``_stopping`` 标志，并把非陈旧状态对外转发。

        代数过滤：``stop(wait=True)`` 改为在 worker 的 threading.Event 上等待并
        本地收尾（不再泵 Qt 事件循环），若调用方随后立刻 ``start()`` 新一轮预览，
        上一轮 queued 的 ``preview_stopped`` 会迟到——它携带旧 generation，在这里
        整体丢弃（含对外转发），避免把新一轮的 active 状态和外部按钮状态错误清零。
        payload 无 "generation" 键时（外部直调/旧测试路径）不过滤。
        """
        generation = payload.get("generation") if isinstance(payload, dict) else None
        if generation is not None and int(generation) != self._generation:
            return
        # ``preview_started`` 表示 worker 已经成功打开相机预览；切到 active=True。
        if status == "preview_started":
            self._active = True
            self._stopping = False
        # ``preview_stopped`` 在 worker 主循环退出后才广播；此处清空两个状态。
        elif status == "preview_stopped":
            self._active = False
            self._stopping = False
        # 对外转发前剥掉内部 "generation" 键，保持外部 payload 合同不变。
        if isinstance(payload, dict) and "generation" in payload:
            payload = {key: value for key, value in payload.items() if key != "generation"}
        self.signal_status_changed.emit(status, payload)

    def take_latest_frame(self) -> PreviewFrameSnapshot | None:
        """转交 worker 的 latest-frame-wins 快照给 GUI 定时器。"""
        return self._worker.take_latest_frame()

    def start(self, config: CameraConfig, timeout_ms: int = 100) -> None:
        """启动预览循环；若已活跃或正在停止则拒绝重入。

        抛出：
            ``RuntimeError``：当预览还活跃或停止流程未完成时再次调用。
        """
        # 1) 拒绝重入：避免在未确认上次结束前发起新的预览，造成双重连接。
        if self._active or self.stopping:
            raise RuntimeError("SIM preview is already active or stopping.")
        # 2) 代数 +1 并重置 worker 内部状态，再立刻置 active；后续状态变更由 worker 广播。
        self._generation += 1
        self._worker.prepare_for_start(self._generation)
        self._active = True
        # 3) 把 (camera, config, timeout_ms) 投递给 worker 的 slot_start。
        self.signal_start_worker.emit(
            {
                "camera": self.camera,
                "config": config,
                "timeout_ms": timeout_ms,
            }
        )

    def stop(self, wait: bool = False) -> None:
        """请求停止预览；``wait=True`` 时同步等待 worker 退出（最多 2 秒）。"""
        # 1) 已经空闲就直接返回，避免重复广播状态。
        if not self._active and not self.stopping:
            return
        # 2) 触发 worker 取消标志；它会在下一个循环迭代里退出。
        self._worker.request_stop()
        # 3) 切到 stopping 状态，等 ``preview_stopped`` 广播再清零。
        if not self.stopping:
            self._active = False
            self._stopping = True
        # 4) 调用方需要"同步等待"时，直接在 worker 的 threading.Event 上等待循环
        #    退出（彻底移除 processEvents 重入面）；2 秒 deadline 防止无限等。
        #    等到后本地先行收尾，保证返回后可立即重启预览；queued 的
        #    ``preview_stopped`` 稍后照常投递（同代→正常转发；若期间已重启新一轮
        #    则携带旧代数→被 ``_handle_worker_status`` 丢弃）。
        if wait and self._worker.wait_until_stopped(2.0):
            self._active = False
            self._stopping = False

    def shutdown(self) -> None:
        """组件关闭：先停止预览，再关闭线程，最多等 2 秒。"""
        # 1) 同步停止预览（最多 2 秒）。
        self.stop(wait=True)
        # 2) 请求线程退出并 join；超过 2 秒未退出会被静默丢弃，调用方可重启进程。
        self._thread.quit()
        self._thread.wait(2000)

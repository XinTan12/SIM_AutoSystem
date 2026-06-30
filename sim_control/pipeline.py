"""占位重建、特征提取与决策 pipeline。

作用：
    把"采集完 9 帧 stack → 重建预览图 → 提取强度特征 → 给出 release/sort 决策"
    这条链路用 Qt worker + 信号串起来，作为**未来真实重建/分析算法**的接入骨架。
    当前实现是占位级的：``ReconstructionWorker`` 仅做按帧轴的均值压缩，``FeatureWorker``
    给出均值/标准差/极值四项强度统计，``DecisionEngine`` 用最简阈值判定。

    SIM 9 帧重建的同学接入时，应当替换这三段占位逻辑（保留 worker 信号边界），让
    新算法仍以 ``AcquisitionBatch -> ReconstructionResult -> FeatureResult ->
    DecisionResult`` 的链路运行。

协作关系：
    上游：``controller.py``（采集成功后触发本 pipeline 的 worker）。
    下游：``models.AcquisitionBatch`` 与三个 Result 数据类。
    相关：``tests/test_acquisition_core.py`` / ``test_efficiency_optimizations.py``
          会跑 ``_extract_intensity_features`` 的 OpenCV / NumPy 双路径。

关键概念：
    - ``QObject + pyqtSlot``：把每段工作都包成 worker，便于 controller 把它们
      ``moveToThread`` 到独立线程，避免阻塞 GUI。
    - OpenCV 可选：``cv2`` 在某些部署中可能不存在，本文件用 ``try/except`` 兜底，
      并在 ``_extract_intensity_features`` 提供 NumPy 回退路径。

维护要点：
    - 真实重建算法替换 ``ReconstructionWorker.slot_reconstruct`` 时，必须保证
      输入 ``batch.stack`` 形状为 ``(9, H, W)`` ``uint16``，输出
      ``ReconstructionResult.preview_image`` 为 2D NumPy 数组。
    - 信号 ``signal_reconstruction_ready/failed``、``signal_features_ready/failed``
      与 controller / GUI 强耦合，**信号名与参数顺序绝不可改**。
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import traceback
from datetime import datetime
from pathlib import Path

import numpy as np
from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot
import tifffile

from .models import AcquisitionBatch, DecisionResult, FeatureResult, ReconstructionConfig, ReconstructionResult

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RAW_STACK_OUTPUT_DIR = Path("data") / "sim_9frames"

logger = logging.getLogger(__name__)

# OpenCV 在部分部署中可能缺失；用 try/except 兜底，使本文件可在最小环境跑通。
try:
    import cv2
except Exception:  # pragma: no cover - exercised when OpenCV is absent in a deployment.
    cv2 = None


def _resolve_reconstruction_output_dir(output_path: str) -> Path:
    output_dir = Path(output_path)
    if output_dir.is_absolute():
        return output_dir
    return PROJECT_ROOT / output_dir


def _resolve_raw_stack_output_dir(output_path: str | Path | None = None) -> Path:
    output_dir = Path(output_path) if output_path is not None else DEFAULT_RAW_STACK_OUTPUT_DIR
    if output_dir.is_absolute():
        return output_dir
    return PROJECT_ROOT / output_dir


def _dated_output_dir(base_dir: str | Path, when: datetime) -> Path:
    """在 base_dir 下按 when 的日期（``%Y%m%d``）分子目录，返回形如 ``base_dir/20260629`` 的路径。

    by-design 接收调用方传入的同一个时间快照，使日期目录与文件名时间戳出自同一 ``datetime.now()``，
    避免跨午夜整点时出现目录日期与文件名时间戳错配。
    """
    return Path(base_dir) / when.strftime("%Y%m%d")


def _safe_filename_token(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value).strip())
    return token.strip("._") or datetime.now().strftime("sim_%Y%m%d_%H%M%S")


def _parse_pitch_token(ro_name: str) -> str:
    """从 Running Order 名取 mask 直径（下划线第 2 段，如 ``488_3.5_2d_10ms`` → ``3.5``）。

    兼容正式名与 immediate 名（``488_3.5_2d_imm_f1`` → ``3.5``）；解析不出返回 ``"NA"``。
    """
    parts = str(ro_name or "").split("_")
    if (
        len(parts) >= 2
        and re.fullmatch(r"\d+", parts[0])  # 首段必须是纯整数波长，杜绝 lus_3.5_x 误判
        and re.fullmatch(r"\d+(?:\.\d+)?", parts[1])
    ):
        return parts[1]
    return "NA"


def _build_raw_stack_filename(batch: AcquisitionBatch, stack: np.ndarray, when: datetime | None = None) -> str:
    """raw SIM9 ``.tif`` 文件名：``采集波长_直径_曝光_图像大小_时间戳.tif``。

    - 采集波长：``batch.laser_wavelength_nm``（nm，如 488）。
    - 直径(pitch)：优先解析 ``metadata['running_order_name']`` 第 2 段（如 3.5），
      缺失则回退 ``pattern_files[0]`` 的 basename，再否则 ``"NA"``。
    - 曝光：``batch.exposure_us`` → ms（``:g`` 去尾零，如 ``500ms`` / ``5.61ms``）。
    - 图像大小：``WxH``（已校验 stack 的 ``shape[2]×shape[1]``，如 ``2048x2048``）。
    - 时间戳：保存时刻 ``when``（缺省 ``datetime.now()``）年月日时分秒（14 位）；
      由 ``slot_save`` 传入同一 ``save_time``，与所在日期目录同源、不跨午夜错配。

    例：``488_3.5_500ms_2048x2048_20260625143022.tif``。
    """
    wavelength = _safe_filename_token(str(getattr(batch, "laser_wavelength_nm", "") or "NA"))
    meta = getattr(batch, "metadata", None)
    ro_name = str(meta.get("running_order_name", "") or "") if isinstance(meta, dict) else ""
    if not ro_name:
        pattern_files = getattr(batch, "pattern_files", None) or []
        if pattern_files:
            ro_name = Path(str(pattern_files[0] or "")).name
    pitch = _parse_pitch_token(ro_name)
    exposure_us = int(getattr(batch, "exposure_us", 0) or 0)
    exposure_token = f"{exposure_us / 1000:g}ms" if exposure_us > 0 else "NAms"
    height, width = int(stack.shape[1]), int(stack.shape[2])
    timestamp = (when or datetime.now()).strftime("%Y%m%d%H%M%S")
    return f"{wavelength}_{pitch}_{exposure_token}_{width}x{height}_{timestamp}.tif"


def _unique_output_path(output_dir: Path, filename: str) -> Path:
    """避免同名覆盖：``filename`` 已存在时追加 ``_2``/``_3``… 后缀，保护原始 SIM9 数据。

    仅在罕见同秒碰撞时触发（文件名以 ``.tif`` 结尾，``stem``/``suffix`` 不会误伤 pitch 的点）。
    """
    candidate = output_dir / filename
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    counter = 2
    while True:
        candidate = output_dir / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


class RawStackSaveWorker(QObject):
    """Save raw SIM9 uint16 stacks after acquisition, outside the realtime capture path."""

    signal_stack_saved = pyqtSignal(str, str)
    signal_stack_save_failed = pyqtSignal(str, str)

    def __init__(self, output_dir: str | Path | None = None, parent: QObject | None = None):
        super().__init__(parent)
        self.output_dir = _resolve_raw_stack_output_dir(output_dir)

    @staticmethod
    def _validated_stack(batch: AcquisitionBatch) -> tuple[str, np.ndarray]:
        task_id = str(getattr(batch, "task_id", "") or "")
        stack = np.asarray(getattr(batch, "stack", None))
        if stack.ndim != 3 or stack.shape[0] != 9:
            raise ValueError(f"Expected raw SIM9 stack shape (9, H, W), got {stack.shape}.")
        if stack.dtype != np.uint16:
            raise TypeError(f"Expected raw SIM9 stack dtype uint16, got {stack.dtype}.")
        return task_id, stack

    @pyqtSlot(object)
    def slot_save(self, batch: AcquisitionBatch) -> None:
        task_id = str(getattr(batch, "task_id", "") or "")
        try:
            task_id, stack = self._validated_stack(batch)
            save_time = datetime.now()
            output_dir = _dated_output_dir(self.output_dir, save_time)
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = _unique_output_path(output_dir, _build_raw_stack_filename(batch, stack, save_time))
            tifffile.imwrite(
                str(output_path),
                stack,
                photometric="minisblack",
            )
            self.signal_stack_saved.emit(task_id, str(output_path))
        except Exception as exc:
            logger.warning("Failed to save raw SIM9 stack for %s: %s", task_id, exc)
            self.signal_stack_save_failed.emit(task_id, str(exc))

    @pyqtSlot(object, str)
    def slot_save_to_path(self, batch: AcquisitionBatch, path: str) -> None:
        if not str(path or "").strip():
            self.slot_save(batch)
            return
        task_id = str(getattr(batch, "task_id", "") or "")
        try:
            task_id, stack = self._validated_stack(batch)
            output_path = Path(path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            tifffile.imwrite(
                str(output_path),
                stack,
                photometric="minisblack",
            )
            self.signal_stack_saved.emit(task_id, str(output_path))
        except Exception as exc:
            logger.warning("Failed to save raw SIM9 stack for %s: %s", task_id, exc)
            self.signal_stack_save_failed.emit(task_id, str(exc))


class ReconstructionWorker(QObject):
    """占位重建 worker，把 9 帧 stack 压缩成一张重建预览图。

    职责：
        - 接受 ``AcquisitionBatch``；
        - 校验 stack 形状 ``(9, H, W)``；
        - 输出 ``ReconstructionResult``（preview_image 为 stack 帧均值，uint16）。

    协作：
        被 ``SimAcquisitionController`` 在采集成功后 ``moveToThread`` 调度；
        失败时通过 ``signal_reconstruction_failed`` 把 traceback 抛回 GUI。
    """
    signal_reconstruction_ready = pyqtSignal(object)
    signal_reconstruction_failed = pyqtSignal(str, str)
    # 异步落盘结果通知：(task_id, status, path, message)；status ∈ {"saved","failed"}。
    # 注意：落盘在结果 emit 之后进行，不回填已下发的 ReconstructionResult。
    signal_reconstruction_saved = pyqtSignal(str, str, str, str)
    # 关闭握手：worker 把保存队列 drain 完后发出，GUI 收到再 quit/wait 线程。
    signal_shutdown_finished = pyqtSignal()

    def __init__(self, reconstruction_config: ReconstructionConfig | None = None, parent: QObject | None = None):
        super().__init__(parent)
        self.reconstruction_config = reconstruction_config
        # 常驻热重建器：跨细胞复用同一引擎实例，保留 cuFFT plan / allocator / 网格缓存，
        # 让第二个细胞起进入热速度。惰性创建（首个真实细胞或预热时），且只在重建线程上访问。
        self._warm_reconstructor = None
        self._warm_device_fingerprint: tuple | None = None
        # 有界单 writer 异步落盘：recon 线程入队即返回，writer 线程负责写 TIFF。
        self._save_queue: "queue.Queue | None" = None
        self._save_thread: threading.Thread | None = None
        self._save_stop = threading.Event()
        self._shutdown_timeout_s = 5.0
        self._shutting_down = False

    # ----- 配置更新（跨线程：GUI 经 signal 调用 slot_update_reconstruction_config）-----

    @pyqtSlot(object)
    def slot_update_reconstruction_config(self, reconstruction_config: ReconstructionConfig | None) -> None:
        """在重建线程上应用新配置：按指纹失效热实例 / 重建保存队列。

        GUI 线程**不直接**改 worker 内部状态，而是 emit 一个携带配置快照的信号连到本 slot，
        天然 queued、在重建线程执行，避免跨线程改 cuFFT/队列状态。
        """
        old = self.reconstruction_config
        self.reconstruction_config = reconstruction_config
        # device/dtype 变化必须重建热实例（CUDA 上下文绑定）；OTF/params 文件变化由热实例
        # 内部按 (路径,mtime,size,device,dtype) 指纹自动失效缓存，无需在此重建。
        if self._device_fingerprint(old) != self._device_fingerprint(reconstruction_config):
            self._warm_reconstructor = None
            self._warm_device_fingerprint = None
        old_max = getattr(old, "save_queue_maxsize", None)
        new_max = getattr(reconstruction_config, "save_queue_maxsize", None)
        if old_max != new_max and self._save_thread is not None and self._save_thread.is_alive():
            self._restart_writer()

    def set_reconstruction_config(self, reconstruction_config: ReconstructionConfig | None) -> None:
        """同线程直调入口（测试/向后兼容）；GUI 跨线程请走 slot_update_reconstruction_config。"""
        self.slot_update_reconstruction_config(reconstruction_config)

    @staticmethod
    def _device_fingerprint(config: ReconstructionConfig | None) -> tuple | None:
        if config is None:
            return None
        return (str(config.device), str(config.dtype))

    def _get_warm_reconstructor(self, reconstruction_config: ReconstructionConfig):
        # 惰性 import：保持 GUI 启动不引入 torch（无 torch 也能启动）。
        from sim_control.sim_reconstruction import WarmSIMReconstructor

        fingerprint = self._device_fingerprint(reconstruction_config)
        if self._warm_reconstructor is None or self._warm_device_fingerprint != fingerprint:
            self._warm_reconstructor = WarmSIMReconstructor(
                device=reconstruction_config.device, dtype=reconstruction_config.dtype
            )
            self._warm_device_fingerprint = fingerprint
        return self._warm_reconstructor

    # ----- 预热（在重建线程上执行，非致命）-----

    @pyqtSlot(int)
    def slot_warmup(self, wavelength_nm: int) -> None:
        """启动预热：焐热与形状无关的一次性 GPU 成本，让首个真实细胞更接近热速度。

        非致命：任何缺失/失败都跳过，不影响 GUI。
        - 哨兵 ``wavelength_nm <= 0``：显式做 import/CUDA-context 级预热（测试/手动诊断），
          **仅此分支**允许在无 OTF 时触发后端 import。
        - 正常波长 ``> 0``：所有 OTF / saved ``.mat`` 检查都在 ``_get_warm_reconstructor()``
          **之前**完成，任何 ``return`` 分支都不构造热实例、不 import 后端——故默认配置
          （OTF 空）下 GUI 启动不会 import torch。
        """
        config = self.reconstruction_config
        if config is None or not config.enabled:
            return

        # 哨兵：显式 import/CUDA 级预热（唯一允许无 OTF 触发后端 import 的分支）。
        if not wavelength_nm or int(wavelength_nm) <= 0:
            try:
                self._get_warm_reconstructor(config).warmup_environment()
            except Exception:
                logger.warning("Reconstruction env warmup skipped: backend unavailable.", exc_info=True)
            return

        # 正常波长：先做全部检查（不触发后端 import），再决定是否构造热实例。
        otf_path = config.otf_path_for_wavelength(wavelength_nm)
        if not str(otf_path).strip() or not Path(str(otf_path)).is_file():
            return  # 无可用 OTF：重建尚不可运行，跳过预热（不 import 后端）。

        use_saved = False
        params_path = ""
        if config.use_saved_params:
            params_path = config.estimated_params_path_for_wavelength(wavelength_nm)
            mat_ok = bool(params_path) and Path(str(params_path)).is_file()
            if not mat_ok:
                if config.saved_params_fallback == "fail":
                    # 生产档 saved+fail 缺 .mat：不偷偷用 estimate 预热掩盖配置错误；
                    # 真正阻止采集交给配置校验。
                    logger.warning(
                        "Saved-params .mat missing for %snm; skip warmup (acquisition blocked by config validation).",
                        wavelength_nm,
                    )
                    return
                # fallback == "estimate"：按 estimate 预热，use_saved 保持 False——
                # 否则 WarmSIMReconstructor.reconstruct 会先 _validate_file_path(.mat) 抛错，到不了 fallback。
            else:
                use_saved = True

        try:
            warm = self._get_warm_reconstructor(config)
            warm.warmup(
                wavelength_nm=int(wavelength_nm),
                otf_path=str(otf_path),
                background_path=config.background_path,
                use_saved_params=use_saved,
                estimated_params_path=str(params_path or ""),
                saved_params_fallback=config.saved_params_fallback,
                wiener=config.wiener,
                pixel_size_nm=config.pixel_size_nm,
                excitation_na=config.excitation_na,
                theta_ratio=tuple(config.theta_ratio),
                recon_group_batch=config.recon_group_batch,
            )
        except Exception:
            logger.warning("Reconstruction warmup skipped (non-fatal).", exc_info=True)

    @pyqtSlot(object)
    def slot_reconstruct(self, batch: AcquisitionBatch) -> None:
        """从 9 帧 SIM stack 重建一张预览图。

        启用 GPU 重建时走常驻热重建器，并**先 emit 结果再异步落盘**，避免磁盘 I/O 拖慢
        重建→特征→决策回传；未启用时退化为按帧轴整除均值的占位图。
        """
        try:
            # 1) 防御性校验：stack 不能为空、必须 3 维且首维等于 9。
            if batch.stack is None:
                raise ValueError("batch.stack is None")
            stack = np.asarray(batch.stack)
            if stack.ndim != 3 or stack.shape[0] != 9:
                raise ValueError(f"Expected stack shape (9, H, W), got {stack.shape}")
            if stack.dtype != np.uint16:
                raise TypeError(f"Expected stack dtype uint16, got {stack.dtype}")
            reconstruction_config = self.reconstruction_config
            if reconstruction_config is not None and reconstruction_config.enabled:
                # 重建失败（GPU/缺 torch/saved .mat 失败）会在此抛出 -> 走 except -> 失败信号。
                result, save_job = self._run_gpu_wiener_reconstruction(batch, stack, reconstruction_config)
                # 先下发结果，让特征/决策链路立刻推进。
                self.signal_reconstruction_ready.emit(result)
                # 再落盘（异步或同步），落盘失败不影响已下发的结果，只发 saved("failed") 信号。
                if save_job is not None:
                    self._submit_save(save_job, reconstruction_config)
                return
            # 2) 用 uint32 累加防止 9 帧叠加溢出，再整除帧数得到 uint16 平均图。
            #    真实算法接入时把这一行替换成 SIM 重建即可。
            preview = (np.add.reduce(stack, axis=0, dtype=np.uint32) // np.uint32(stack.shape[0])).astype(np.uint16)
            # 3) 把 metadata 标 ``placeholder=True``，便于后续日志或 UI 区分占位与真实重建。
            result = ReconstructionResult(
                task_id=batch.task_id,
                preview_image=preview,
                metadata={
                    "stack_shape": list(batch.stack.shape),
                    "laser_wavelength_nm": batch.laser_wavelength_nm,
                    "placeholder": True,
                },
                succeeded=True,
            )
            self.signal_reconstruction_ready.emit(result)
        except Exception as exc:
            # 4) 任意异常打包成 (task_id, traceback) 字符串发回 GUI，方便用户复现。
            self.signal_reconstruction_failed.emit(batch.task_id, f"{exc}\n{traceback.format_exc()}")

    def _run_gpu_wiener_reconstruction(
        self,
        batch: AcquisitionBatch,
        stack: np.ndarray,
        reconstruction_config: ReconstructionConfig,
    ) -> tuple[ReconstructionResult, dict | None]:
        warm = self._get_warm_reconstructor(reconstruction_config)
        otf_path = reconstruction_config.otf_path_for_wavelength(batch.laser_wavelength_nm)
        estimated_params_path = reconstruction_config.estimated_params_path_for_wavelength(
            batch.laser_wavelength_nm
        )
        output = warm.reconstruct(
            stack,
            wavelength_nm=batch.laser_wavelength_nm,
            otf_path=otf_path,
            background_path=reconstruction_config.background_path,
            device=reconstruction_config.device,
            dtype=reconstruction_config.dtype,
            wiener=reconstruction_config.wiener,
            pixel_size_nm=reconstruction_config.pixel_size_nm,
            excitation_na=reconstruction_config.excitation_na,
            theta_ratio=tuple(reconstruction_config.theta_ratio),
            recon_group_batch=reconstruction_config.recon_group_batch,
            use_saved_params=reconstruction_config.use_saved_params,
            estimated_params_path=estimated_params_path,
            saved_params_fallback=reconstruction_config.saved_params_fallback,
        )
        reconstruction = np.asarray(output["reconstruction"], dtype=np.float32)
        if reconstruction.ndim == 3:
            preview = reconstruction[0]
        elif reconstruction.ndim == 2:
            preview = reconstruction
        else:
            raise ValueError(f"Expected 2D or 3D reconstruction output, got {reconstruction.shape}")
        metadata = dict(output.get("metadata", {}) or {})
        # 决定是否落盘：emit 期还没真正入队，故 output_save_status 只表示"是否请求落盘"
        # （requested / disabled），**不承诺最终落盘结果**——终态 saved/failed 只由
        # signal_reconstruction_saved 给出，不回填已 emit 的 metadata（codex 第 4 轮）。
        save_job: dict | None = None
        if reconstruction_config.save_reconstruction_output and reconstruction_config.output_path.strip():
            output_dir = _resolve_reconstruction_output_dir(reconstruction_config.output_path.strip())
            output_path = output_dir / datetime.now().strftime("sim_reconstruction_%Y%m%d_%H%M%S.tif")
            metadata["output_dir"] = str(output_dir)
            metadata["output_path"] = str(output_path)
            metadata["output_save_status"] = "requested"
            save_job = {
                "task_id": batch.task_id,
                "path": str(output_path),
                "array": reconstruction.astype(np.float32, copy=False),
            }
        else:
            metadata["output_path"] = ""
            metadata["output_save_status"] = "disabled"
        metadata.update(
            {
                "task_id": batch.task_id,
                "placeholder": False,
                "stack_shape": list(stack.shape),
                "preview_shape": list(preview.shape),
            }
        )
        result = ReconstructionResult(
            task_id=batch.task_id,
            preview_image=np.asarray(preview, dtype=np.float32),
            metadata=metadata,
            succeeded=True,
        )
        return result, save_job

    # ----- 有界单 writer 异步落盘 -----

    def _submit_save(self, save_job: dict, reconstruction_config: ReconstructionConfig) -> None:
        """提交落盘任务：异步则入队（满则丢弃+告警），同步则当场写（仍在 emit 之后）。"""
        if reconstruction_config.async_save_reconstruction_output:
            save_queue = self._ensure_writer(reconstruction_config.save_queue_maxsize)
            if save_queue is None:  # 已进入关停流程，不再接收新保存任务。
                logger.warning("Reconstruction worker shutting down; dropping save for %s.", save_job["task_id"])
                self.signal_reconstruction_saved.emit(save_job["task_id"], "failed", "", "worker shutting down")
                return
            try:
                save_queue.put_nowait(save_job)
            except queue.Full:
                logger.warning("Reconstruction save queue full; dropping save for %s.", save_job["task_id"])
                self.signal_reconstruction_saved.emit(save_job["task_id"], "failed", "", "save queue full")
        else:
            self._write_save_job(save_job)

    def _ensure_writer(self, maxsize: int) -> "queue.Queue | None":
        """确保 writer 线程存在；关停中返回 None。返回当前队列（供调用方直接入队，避免读已置空的 self 引用）。"""
        if self._shutting_down:
            return None
        if self._save_thread is None or not self._save_thread.is_alive():
            save_queue = queue.Queue(maxsize=max(1, int(maxsize)))
            stop_event = threading.Event()
            self._save_queue = save_queue
            self._save_stop = stop_event
            # 把 queue/stop 作为局部参数传入线程，writer 循环只用局部引用——即使配置变更或关停
            # 把 self._save_queue 置空，正在运行的 writer 也不会触碰到 None（避免 race）。
            self._save_thread = threading.Thread(
                target=self._writer_loop,
                args=(save_queue, stop_event),
                name="recon-tiff-writer",
                daemon=True,
            )
            self._save_thread.start()
        return self._save_queue

    def _writer_loop(self, save_queue: "queue.Queue", stop_event: threading.Event) -> None:
        while True:
            try:
                job = save_queue.get(timeout=0.2)
            except queue.Empty:
                if stop_event.is_set():
                    return
                continue
            try:
                self._write_save_job(job)
            finally:
                save_queue.task_done()

    def _write_save_job(self, save_job: dict) -> None:
        """实际写 TIFF；成功/失败都经 signal_reconstruction_saved 通知，绝不抛出。"""
        task_id = save_job.get("task_id", "")
        try:
            output_path = Path(save_job["path"])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            tifffile.imwrite(
                str(output_path),
                np.asarray(save_job["array"], dtype=np.float32),
                photometric="minisblack",
            )
            self.signal_reconstruction_saved.emit(task_id, "saved", str(output_path), "")
        except Exception as exc:
            logger.warning("Failed to save reconstruction TIFF for %s: %s", task_id, exc)
            self.signal_reconstruction_saved.emit(task_id, "failed", "", str(exc))

    def _restart_writer(self) -> None:
        """save_queue_maxsize 变化时重建 writer。旧线程用自己的局部 queue，置空 self 引用安全。"""
        thread = self._save_thread
        self._save_stop.set()
        if thread is not None:
            thread.join(self._shutdown_timeout_s)
        self._save_thread = None
        self._save_queue = None
        self._save_stop = threading.Event()

    @pyqtSlot()
    def slot_shutdown(self) -> None:
        """停 writer：限时 drain 已入队任务，超时记丢弃数；完成后 emit signal_shutdown_finished。

        置 ``_shutting_down`` 后不再接收新保存任务（防止关停后又被排队的重建事件重启 writer）。
        """
        try:
            self._shutting_down = True
            thread = self._save_thread
            if thread is not None and thread.is_alive():
                self._save_stop.set()
                thread.join(self._shutdown_timeout_s)
                if thread.is_alive():
                    dropped = self._save_queue.qsize() if self._save_queue is not None else 0
                    logger.warning(
                        "Reconstruction TIFF writer did not drain within %ss; %d save job(s) may be dropped.",
                        self._shutdown_timeout_s,
                        dropped,
                    )
        finally:
            self.signal_shutdown_finished.emit()


class FeatureWorker(QObject):
    """占位特征 worker，从重建图中提取均值、标准差和强度范围。

    职责：
        - 接受 ``ReconstructionResult.preview_image``；
        - 优先用 OpenCV（如可用）做向量化均值/标准差/极值；
        - 缺失 OpenCV 时回退 NumPy；
        - 输出 ``FeatureResult``（dict 形式四项强度特征）。
    """
    signal_features_ready = pyqtSignal(object)
    signal_features_failed = pyqtSignal(str, str)

    @pyqtSlot(object)
    def slot_extract(self, recon_result: ReconstructionResult) -> None:
        """从重建预览图提取强度特征。

        输入：``ReconstructionResult.preview_image`` 形状 ``(H, W)``。
        输出：``FeatureResult.features`` = ``{"mean_intensity", "std_intensity",
        "max_intensity", "min_intensity"}``。
        """
        try:
            # 1) 防御性校验：preview_image 不能为空。
            if recon_result.preview_image is None:
                raise ValueError("preview_image is None")
            image = np.asarray(recon_result.preview_image)
            # 2) 委托给 ``_extract_intensity_features``，由它决定走 OpenCV 还是 NumPy 路径。
            features = _extract_intensity_features(image)
            # 3) 打包 ``FeatureResult``；metadata 标占位，便于上层区分。
            result = FeatureResult(
                task_id=recon_result.task_id,
                features=features,
                metadata={"placeholder": True},
                succeeded=True,
            )
            self.signal_features_ready.emit(result)
        except Exception as exc:
            self.signal_features_failed.emit(recon_result.task_id, f"{exc}\n{traceback.format_exc()}")


def _extract_intensity_features(image: np.ndarray) -> dict[str, float]:
    """提取均值/标准差/极值四项强度特征；优先 OpenCV，必要时回退 NumPy。

    设计：
        - OpenCV ``meanStdDev`` + ``minMaxLoc`` 在大图上比 NumPy 快显著；
        - OpenCV 不可用或异常时回退到 NumPy 路径，保持功能正确性。
    """
    # 1) OpenCV 可用 + 2D + 非空 → 走加速路径。
    if cv2 is not None and image.ndim == 2 and image.size > 0:
        try:
            # ``ascontiguousarray`` 保证 cv2 能直接读取内存，无须复制。
            cv_image = np.ascontiguousarray(image)
            mean, stddev = cv2.meanStdDev(cv_image)
            min_value, max_value, _min_loc, _max_loc = cv2.minMaxLoc(cv_image)
            return {
                "mean_intensity": float(mean[0][0]),
                "std_intensity": float(stddev[0][0]),
                "max_intensity": float(max_value),
                "min_intensity": float(min_value),
            }
        except Exception:
            # OpenCV 路径异常（如非连续内存、特殊 dtype）静默回退到 NumPy 路径。
            pass

    # 2) NumPy 回退路径：用 float32 累加保证精度且兼容 uint16 输入。
    fallback = np.asarray(image, dtype=np.float32)
    return {
        "mean_intensity": float(np.mean(fallback)),
        "std_intensity": float(np.std(fallback)),
        "max_intensity": float(np.max(fallback)),
        "min_intensity": float(np.min(fallback)),
    }


class DecisionEngine:
    """占位决策器，根据特征结果给出 release / sort / invalid 决策接口。

    维护要点：
        - 接入真实决策算法时，保持 ``decide(feature_result) -> DecisionResult`` 签名。
        - ``DecisionResult.decision`` 字符串当前只产出 ``invalid`` 与 ``keep``；
          真实业务接入后可扩展为 ``release`` / ``sort`` / ``discard`` 等约定值。
    """
    def decide(self, feature_result: FeatureResult) -> DecisionResult:
        """根据特征字典给出占位决策。

        输入：``FeatureResult.features`` dict，必须含 ``mean_intensity`` 键。
        输出：``DecisionResult``；``mean_intensity <= 0`` 视为 ``invalid``，
        否则一律 ``keep`` 并把均值作为 ``score``。
        """
        # 1) 读取均值强度；缺失时默认 0，会被下一步判为 invalid。
        mean_intensity = float(feature_result.features.get("mean_intensity", 0.0))
        # 2) 均值非正 → invalid；保留占位实现，真实决策接入后可换更复杂判据。
        if mean_intensity <= 0:
            return DecisionResult(
                task_id=feature_result.task_id,
                decision="invalid",
                reason="No usable preview intensity in placeholder pipeline.",
                score=0.0,
                metadata={"placeholder": True},
            )
        # 3) 其它情况一律 keep，并把均值塞到 score，方便下游做阈值或排序。
        return DecisionResult(
            task_id=feature_result.task_id,
            decision="keep",
            reason="Placeholder decision engine keeps all non-empty reconstructions.",
            score=mean_intensity,
            metadata={"placeholder": True},
        )

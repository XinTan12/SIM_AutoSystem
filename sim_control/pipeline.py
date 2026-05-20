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

import traceback

import numpy as np
from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot

from .models import AcquisitionBatch, DecisionResult, FeatureResult, ReconstructionResult

# OpenCV 在部分部署中可能缺失；用 try/except 兜底，使本文件可在最小环境跑通。
try:
    import cv2
except Exception:  # pragma: no cover - exercised when OpenCV is absent in a deployment.
    cv2 = None


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

    @pyqtSlot(object)
    def slot_reconstruct(self, batch: AcquisitionBatch) -> None:
        """从 9 帧 SIM stack 重建一张预览图。

        输入：``AcquisitionBatch.stack`` 形状 ``(9, H, W)`` ``uint16``。
        输出：``ReconstructionResult.preview_image`` 形状 ``(H, W)`` ``uint16``，
        当前实现是按帧轴的算术平均（用 uint32 累加防溢出，再整除回 uint16）。
        """
        try:
            # 1) 防御性校验：stack 不能为空、必须 3 维且首维等于 9。
            if batch.stack is None:
                raise ValueError("batch.stack is None")
            stack = np.asarray(batch.stack)
            if stack.ndim != 3 or stack.shape[0] != 9:
                raise ValueError(f"Expected stack shape (9, H, W), got {stack.shape}")
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

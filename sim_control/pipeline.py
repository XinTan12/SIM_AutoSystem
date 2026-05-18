"""占位重建、特征提取和决策 pipeline。

当前实现用于把采集得到的 9 帧 uint16 图像栈串到后续分析链路：ReconstructionWorker 生成简化重建图，FeatureWorker 提取强度统计，DecisionEngine 产出 release/sort 决策占位结果。它为后续真实重建算法保留线程和信号边界。
"""

from __future__ import annotations

import traceback

import numpy as np
from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot

from .models import AcquisitionBatch, DecisionResult, FeatureResult, ReconstructionResult

try:
    import cv2
except Exception:  # pragma: no cover - exercised when OpenCV is absent in a deployment.
    cv2 = None


class ReconstructionWorker(QObject):
    """占位重建 worker，把 9 帧 stack 压缩成一张重建预览图。"""
    signal_reconstruction_ready = pyqtSignal(object)
    signal_reconstruction_failed = pyqtSignal(str, str)

    @pyqtSlot(object)
    def slot_reconstruct(self, batch: AcquisitionBatch) -> None:
        """Reconstruct a preview image from a 9-frame SIM stack.

        Input: AcquisitionBatch with stack shaped (9, H, W) uint16.
        Output: ReconstructionResult with preview_image shaped (H, W) uint16.
        """
        try:
            if batch.stack is None:
                raise ValueError("batch.stack is None")
            stack = np.asarray(batch.stack)
            if stack.ndim != 3 or stack.shape[0] != 9:
                raise ValueError(f"Expected stack shape (9, H, W), got {stack.shape}")
            preview = (np.add.reduce(stack, axis=0, dtype=np.uint32) // np.uint32(stack.shape[0])).astype(np.uint16)
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
            self.signal_reconstruction_failed.emit(batch.task_id, f"{exc}\n{traceback.format_exc()}")


class FeatureWorker(QObject):
    """占位特征 worker，从重建图中提取均值、标准差和强度范围。"""
    signal_features_ready = pyqtSignal(object)
    signal_features_failed = pyqtSignal(str, str)

    @pyqtSlot(object)
    def slot_extract(self, recon_result: ReconstructionResult) -> None:
        """Extract intensity features from a reconstructed preview image.

        Input: ReconstructionResult with preview_image shaped (H, W).
        Output: FeatureResult with features dict containing mean/std/max/min intensity.
        """
        try:
            if recon_result.preview_image is None:
                raise ValueError("preview_image is None")
            image = np.asarray(recon_result.preview_image)
            features = _extract_intensity_features(image)
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
    """从重建图像中提取平均、峰值等占位强度特征，供决策阶段演示。"""
    if cv2 is not None and image.ndim == 2 and image.size > 0:
        try:
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
            pass

    fallback = np.asarray(image, dtype=np.float32)
    return {
        "mean_intensity": float(np.mean(fallback)),
        "std_intensity": float(np.std(fallback)),
        "max_intensity": float(np.max(fallback)),
        "min_intensity": float(np.min(fallback)),
    }


class DecisionEngine:
    """占位决策器，根据特征结果给出 release/sort 决策接口。"""
    def decide(self, feature_result: FeatureResult) -> DecisionResult:
        """Decide keep/sort/invalid based on extracted features.

        Input: FeatureResult with features dict (requires 'mean_intensity').
        Output: DecisionResult with decision string and score.
        """
        mean_intensity = float(feature_result.features.get("mean_intensity", 0.0))
        if mean_intensity <= 0:
            return DecisionResult(
                task_id=feature_result.task_id,
                decision="invalid",
                reason="No usable preview intensity in placeholder pipeline.",
                score=0.0,
                metadata={"placeholder": True},
            )
        return DecisionResult(
            task_id=feature_result.task_id,
            decision="keep",
            reason="Placeholder decision engine keeps all non-empty reconstructions.",
            score=mean_intensity,
            metadata={"placeholder": True},
        )

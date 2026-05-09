from __future__ import annotations

import traceback

import numpy as np
from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot

from .models import AcquisitionBatch, DecisionResult, FeatureResult, ReconstructionResult


class ReconstructionWorker(QObject):
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
            image = np.asarray(recon_result.preview_image, dtype=np.float32)
            result = FeatureResult(
                task_id=recon_result.task_id,
                features={
                    "mean_intensity": float(np.mean(image)),
                    "std_intensity": float(np.std(image)),
                    "max_intensity": float(np.max(image)),
                    "min_intensity": float(np.min(image)),
                },
                metadata={"placeholder": True},
                succeeded=True,
            )
            self.signal_features_ready.emit(result)
        except Exception as exc:
            self.signal_features_failed.emit(recon_result.task_id, f"{exc}\n{traceback.format_exc()}")


class DecisionEngine:
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

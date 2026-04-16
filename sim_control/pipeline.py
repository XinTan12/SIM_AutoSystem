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
        try:
            preview = np.mean(batch.stack.astype(np.float32), axis=0)
            preview = np.clip(preview, 0, 65535).astype(np.uint16)
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
        try:
            image = recon_result.preview_image.astype(np.float32)
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

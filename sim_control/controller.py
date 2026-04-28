from __future__ import annotations

import traceback
from typing import Any

from PyQt5.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

from .acquisition_core import run_single_acquisition
from .adapters import FusionBtCameraAdapter, HardwareError, KopinSlmAdapter, NIDaqAdapter, find_best_running_order
from .config_store import validate_app_config
from .models import AppConfig, BackendConfig, CameraConfig, DaqLineConfig, PatternPreparationResult, SimTaskConfig, new_task_id
from .sim_adapters import SimulatedCameraAdapter, SimulatedDaqAdapter, SimulatedSlmAdapter
from .waveform import NIDaqWaveformBuilder, validate_daq_line_config


class SimAcquisitionWorker(QObject):
    signal_status_changed = pyqtSignal(str, dict)
    signal_acquisition_ready = pyqtSignal(object)
    signal_acquisition_failed = pyqtSignal(str, str)

    @pyqtSlot(object)
    def slot_start(self, payload: dict[str, Any]) -> None:
        task: SimTaskConfig = payload["task"]
        task_id: str = payload["task_id"]
        daq_config: DaqLineConfig = payload["daq_config"]
        pattern_result: PatternPreparationResult = payload["pattern_result"]
        waveform_builder: NIDaqWaveformBuilder = payload["waveform_builder"]
        daq = payload["daq_adapter"]
        camera = payload["camera_adapter"]
        slm = payload["slm_adapter"]

        try:
            batch = run_single_acquisition(
                task=task,
                daq_config=daq_config,
                pattern_result=pattern_result,
                camera=camera,
                slm=slm,
                daq=daq,
                waveform_builder=waveform_builder,
                task_id=task_id,
                on_status=lambda status, data: self.signal_status_changed.emit(status, data),
            )
            self.signal_acquisition_ready.emit(batch)
        except Exception as exc:
            self.signal_acquisition_failed.emit(task_id, f"{exc}\n{traceback.format_exc()}")


class SimAcquisitionController(QObject):
    signal_status_changed = pyqtSignal(str, dict)
    signal_acquisition_ready = pyqtSignal(object)
    signal_acquisition_failed = pyqtSignal(str, str)
    signal_start_worker = pyqtSignal(object)

    def __init__(self, backend: BackendConfig | None = None, parent: QObject | None = None):
        super().__init__(parent)
        self.backend = backend or BackendConfig()
        self.waveform_builder = NIDaqWaveformBuilder()
        self.camera_adapter, self.slm_adapter, self.daq_adapter = self._create_adapters(self.backend)
        self.daq_config = DaqLineConfig()
        self.camera_config = CameraConfig()
        self.pattern_result = PatternPreparationResult()
        self._latest_camera_timing: dict[str, Any] = {}
        self._shutdown_requested = False

        self._thread = QThread(self)
        self._worker = SimAcquisitionWorker()
        self._worker.moveToThread(self._thread)
        self._worker.signal_status_changed.connect(self.signal_status_changed)
        self._worker.signal_acquisition_ready.connect(self.signal_acquisition_ready)
        self._worker.signal_acquisition_failed.connect(self.signal_acquisition_failed)
        self.signal_start_worker.connect(self._worker.slot_start)
        self._thread.start()

    @staticmethod
    def _create_adapters(backend: BackendConfig):
        if backend.simulation_mode:
            return SimulatedCameraAdapter(), SimulatedSlmAdapter(), SimulatedDaqAdapter()
        return (
            FusionBtCameraAdapter(sdk_path=backend.fusion_bt_sdk_path),
            KopinSlmAdapter(sdk_path=backend.slm_sdk_path),
            NIDaqAdapter(),
        )

    def shutdown(self) -> None:
        self._shutdown_requested = True
        try:
            self.stop()
        except Exception:
            pass
        self._thread.quit()
        self._thread.wait(2000)
        try:
            self.camera_adapter.disconnect()
        except Exception:
            pass
        try:
            self.slm_adapter.disconnect()
        except Exception:
            pass

    def initialize_hardware(self) -> None:
        self.signal_status_changed.emit("hardware_initializing", {})
        self.camera_adapter.initialize()
        self.slm_adapter.initialize()
        self.signal_status_changed.emit("hardware_initialized", {})

    def initialize_camera(self) -> None:
        self.camera_adapter.initialize()
        self.signal_status_changed.emit("camera_initialized", {})

    def refresh_available_camera_devices(self) -> list[dict[str, Any]]:
        return self.camera_adapter.list_devices()

    def connect_camera(self, device_index: int | None = None, device_label: str = "") -> dict[str, Any]:
        info = self.camera_adapter.connect(device_index=device_index, device_label=device_label)
        self.signal_status_changed.emit("camera_connected", info)
        return info

    def disconnect_camera(self) -> None:
        self.camera_adapter.disconnect()
        self.signal_status_changed.emit("camera_disconnected", {})

    def camera_connection_info(self) -> dict[str, Any]:
        return self.camera_adapter.connection_info()

    def arm_camera(self, frame_count: int = 9) -> None:
        self.camera_adapter.arm(frame_count=frame_count)
        self.signal_status_changed.emit("camera_armed", {"frame_count": frame_count})

    def disarm_camera(self) -> None:
        self.camera_adapter.disarm()
        self.signal_status_changed.emit("camera_disarmed", {})

    def refresh_available_daq_devices(self) -> list[str]:
        return self.daq_adapter.list_devices(default_device=self.daq_config.device_name)

    def refresh_available_lines(self, device_name: str | None = None) -> list[str]:
        selected_device = device_name or self.daq_config.device_name
        return self.daq_adapter.list_port0_lines(device_name=selected_device, default_device=self.daq_config.device_name)

    def apply_daq_config(self, config: DaqLineConfig) -> None:
        validate_daq_line_config(config)
        self.daq_config = config
        self.signal_status_changed.emit("daq_config_applied", {"device_name": config.device_name})

    def apply_camera_config(self, config: CameraConfig) -> dict[str, Any]:
        if config.trigger_mode != "external_level":
            raise HardwareError("Only external_level trigger mode is supported.")
        self.camera_config = config
        result = self.camera_adapter.apply_config(config) or {}
        if result.get("applied_bit_depth") is not None:
            config.bit_depth = int(result["applied_bit_depth"])
        applied_roi = result.get("applied_roi")
        if isinstance(applied_roi, dict):
            config.roi_x = int(applied_roi.get("x", config.roi_x))
            config.roi_y = int(applied_roi.get("y", config.roi_y))
            config.roi_width = int(applied_roi.get("width", config.roi_width))
            config.roi_height = int(applied_roi.get("height", config.roi_height))
        self._latest_camera_timing = dict(result)
        payload = {"camera_config": dict(config.__dict__), **dict(result)}
        self.signal_status_changed.emit("camera_config_applied", payload)
        return dict(result)

    def refresh_available_slm_devices(self) -> list[dict[str, str]]:
        return self.slm_adapter.list_devices()

    def connect_slm(self, device_path: str | None = None) -> dict[str, Any]:
        info = self.slm_adapter.connect(device_path=device_path)
        self.signal_status_changed.emit("slm_connected", info)
        return info

    def disconnect_slm(self) -> None:
        self.slm_adapter.disconnect()
        self.signal_status_changed.emit("slm_disconnected", {})

    def slm_connection_info(self) -> dict[str, Any]:
        return self.slm_adapter.connection_info()

    def prepare_patterns(self, pattern_files: list[str], device_path: str | None = None) -> PatternPreparationResult:
        self.pattern_result = self.slm_adapter.program_patterns(pattern_files, device_path=device_path)
        self.signal_status_changed.emit(
            "patterns_prepared",
            {
                "handles": list(self.pattern_result.handles),
                "prepared_at": self.pattern_result.prepared_at,
            },
        )
        return self.pattern_result

    def select_running_order_for_task(self, wavelength_nm: int, exposure_us: int) -> dict[str, Any]:
        running_orders = self.slm_adapter.list_running_orders()
        ro_index, ro_name, warnings = find_best_running_order(
            running_orders,
            wavelength_nm=int(wavelength_nm),
            exposure_us=int(exposure_us),
        )
        if ro_index is None:
            raise HardwareError("; ".join(warnings) or "No matching SLM running order found.")
        result = self.slm_adapter.select_running_order(ro_index)
        pattern_result = result.get("pattern_result")
        if not isinstance(pattern_result, PatternPreparationResult):
            pattern_result = PatternPreparationResult(
                pattern_files=[ro_name] * 9,
                handles=[-1],
                prepared_at=0.0,
                metadata={
                    "mode": "running_order",
                    "running_order_index": ro_index,
                    "running_order_name": ro_name,
                },
            )
        self.pattern_result = pattern_result
        payload = {
            **dict(result),
            "running_order_index": ro_index,
            "running_order_name": ro_name,
            "warnings": list(warnings),
            "handles": list(self.pattern_result.handles),
            "prepared_at": self.pattern_result.prepared_at,
        }
        self.signal_status_changed.emit("running_order_selected", payload)
        return payload

    def start_single_acquisition(self, task: SimTaskConfig) -> str:
        if not self.pattern_result.handles:
            raise HardwareError("Patterns must be prepared before acquisition.")
        recommended_gap_us = self._latest_camera_timing.get("recommended_inter_frame_gap_us")
        if recommended_gap_us is not None:
            task.timing.inter_frame_gap_us = int(recommended_gap_us)
        pattern_files = list(task.pattern_files)
        selected_running_order = task.running_order_name
        if self.pattern_result.metadata.get("mode") == "running_order":
            selected_running_order = selected_running_order or str(
                self.pattern_result.metadata.get("running_order_name", "")
            )
            pattern_files = list(self.pattern_result.pattern_files)
            task.running_order_name = selected_running_order
        validation_config = AppConfig(
            daq=self.daq_config,
            camera=task.camera,
            timing=task.timing,
            backend=self.backend,
            pattern_files=pattern_files,
            selected_running_order=selected_running_order,
            selected_laser_nm=task.laser_wavelength_nm,
        )
        validation_errors = validate_app_config(validation_config)
        if validation_errors:
            raise ValueError("Invalid SIM acquisition config: " + "; ".join(validation_errors))
        task_id = new_task_id()
        payload = {
            "task_id": task_id,
            "task": task,
            "daq_config": self.daq_config,
            "pattern_result": self.pattern_result,
            "waveform_builder": self.waveform_builder,
            "daq_adapter": self.daq_adapter,
            "camera_adapter": self.camera_adapter,
            "slm_adapter": self.slm_adapter,
        }
        self.signal_start_worker.emit(payload)
        return task_id

    def stop(self) -> None:
        try:
            self.daq_adapter.set_all_low(self.daq_config.device_name)
        finally:
            try:
                self.camera_adapter.disarm()
            except Exception:
                pass
        self.signal_status_changed.emit("stop_requested", {})

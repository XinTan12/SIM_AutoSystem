from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any

import numpy as np

from .models import CameraConfig, PatternPreparationResult
from .waveform import WaveformPlan, parse_line_name


SIMULATED_RUNNING_ORDERS = [
    f"{wavelength}_3.5_2d_{exposure}{suffix}"
    for wavelength in (405, 488, 561, 647)
    for exposure in ("10ms", "1ms", "50ms")
    for suffix in ("", "_ang0")
]
SIMULATED_CAMERA_BIT_DEPTHS = [8, 12, 16]


class SimulatedCameraAdapter:
    def __init__(self):
        self._initialized = False
        self._connected = False
        self._armed = False
        self._frame_count = 0
        self._camera_config = CameraConfig()
        self._connection_info: dict[str, Any] = {}
        self._preview_active = False
        self._rng = np.random.default_rng(12345)

    def initialize(self) -> None:
        self._initialized = True

    def list_devices(self) -> list[dict[str, Any]]:
        self.initialize()
        return [
            {
                "index": 0,
                "model": "Simulated ORCA-Fusion BT",
                "camera_id": "SIM-CAMERA-001",
                "driver_version": "simulation",
                "display": "0: Simulated ORCA-Fusion BT [SIM-CAMERA-001]",
            }
        ]

    def is_connected(self) -> bool:
        return self._connected

    def connection_info(self) -> dict[str, Any]:
        return dict(self._connection_info)

    def connect(self, device_index: int | None = None, device_label: str = "") -> dict[str, Any]:
        self.initialize()
        selected_index = 0 if device_index is None else int(device_index)
        display = device_label or f"{selected_index}: Simulated ORCA-Fusion BT [SIM-CAMERA-001]"
        self._connected = True
        self._connection_info = {
            "index": selected_index,
            "model": "Simulated ORCA-Fusion BT",
            "camera_id": "SIM-CAMERA-001",
            "driver_version": "simulation",
            "display": display,
            "supported_bit_depths": list(SIMULATED_CAMERA_BIT_DEPTHS),
            "simulation": True,
        }
        return dict(self._connection_info)

    def disconnect(self) -> None:
        self._connected = False
        self._preview_active = False
        self._armed = False
        self._connection_info = {}

    def apply_config(self, config: CameraConfig) -> dict[str, Any]:
        self.initialize()
        if not self._connected:
            self.connect(device_index=config.device_index, device_label=config.device_label)
        self._camera_config = config
        if int(config.bit_depth) not in SIMULATED_CAMERA_BIT_DEPTHS:
            config.bit_depth = 16
        return {
            "camera_model": "Simulated ORCA-Fusion BT",
            "camera_id": "SIM-CAMERA-001",
            "supported_bit_depths": list(SIMULATED_CAMERA_BIT_DEPTHS),
            "applied_bit_depth": int(config.bit_depth),
            "recommended_inter_frame_gap_us": max(1000, int(config.exposure_us // 2)),
            "simulation": True,
        }

    def get_supported_bit_depths(self) -> list[int]:
        return list(SIMULATED_CAMERA_BIT_DEPTHS)

    @property
    def preview_active(self) -> bool:
        return self._preview_active

    def start_preview(self, config: CameraConfig, frame_buffer_count: int = 3) -> None:
        self.apply_config(config)
        self._preview_active = True

    def read_preview_frame(self, timeout_ms: int = 100) -> np.ndarray:
        if not self._preview_active:
            raise RuntimeError("Simulated preview is not active.")
        time.sleep(min(max(timeout_ms, 1) / 1000.0, 0.01))
        return self._generate_frame()

    def stop_preview(self) -> None:
        self._preview_active = False

    def arm(self, frame_count: int) -> None:
        self._frame_count = int(frame_count)
        self._armed = True

    def disarm(self) -> None:
        self._armed = False

    def read_frame_sequence(
        self,
        frame_count: int,
        pattern_files: list[str],
        laser_wavelength_nm: int,
        frame_callback: Any | None = None,
    ) -> tuple[np.ndarray, list[float]]:
        if not self._armed:
            raise RuntimeError("Simulated camera must be armed before reading frames.")
        height = int(self._camera_config.roi_height)
        width = int(self._camera_config.roi_width)
        frames = np.empty((int(frame_count), height, width), dtype=np.uint16)
        timestamps: list[float] = []
        for index in range(1, int(frame_count) + 1):
            frame = self._generate_frame()
            frames[index - 1] = frame
            timestamp = time.time()
            timestamps.append(timestamp)
            if frame_callback is not None:
                frame_callback(index, timestamp)
        return frames, timestamps

    def _generate_frame(self) -> np.ndarray:
        height = int(self._camera_config.roi_height)
        width = int(self._camera_config.roi_width)
        max_value = min((1 << int(self._camera_config.bit_depth)) - 1, np.iinfo(np.uint16).max)
        low = min(500, max_value // 8)
        high = min(4000, max_value + 1)
        if high <= low:
            low = 0
            high = max(1, max_value + 1)
        signal = self._rng.integers(low, high, size=(height, width), dtype=np.uint16)
        return signal


class SimulatedSlmAdapter:
    def __init__(self):
        self._initialized = False
        self._connected = False
        self._prepared = PatternPreparationResult()
        self._connection_info: dict[str, Any] = {}

    def initialize(self) -> None:
        self._initialized = True

    def list_devices(self) -> list[dict[str, str]]:
        self.initialize()
        return [{"display": "Simulated Kopin QXGA-R11", "path": "simulated-r11", "id": "SIM-SLM-001", "serial": "SIM-SLM-001"}]

    def is_connected(self) -> bool:
        return self._connected

    def connection_info(self) -> dict[str, Any]:
        return dict(self._connection_info)

    def connect(self, device_path: str | None = None) -> dict[str, Any]:
        self.initialize()
        self._connected = True
        self._connection_info = {
            "device_id": "SIM-SLM-001",
            "device_path": device_path or "simulated-r11",
            "device_serial_hint": "SIM-SLM-001",
            "serial_number": "SIM-SLM-001",
            "repertoire_name": "simulation",
            "simulation": True,
        }
        return dict(self._connection_info)

    def disconnect(self) -> None:
        self._connected = False
        self._connection_info = {}

    def list_running_orders(self) -> list[tuple[int, str]]:
        self.initialize()
        return list(enumerate(SIMULATED_RUNNING_ORDERS))

    def select_running_order(self, ro_index: int) -> dict[str, Any]:
        self.initialize()
        if not self._connected:
            self.connect()
        ro_index = int(ro_index)
        try:
            ro_name = SIMULATED_RUNNING_ORDERS[ro_index]
        except IndexError as exc:
            raise RuntimeError(f"Simulated running order index out of range: {ro_index}") from exc
        self._prepared = PatternPreparationResult(
            pattern_files=[ro_name] * 9,
            handles=[-1],
            prepared_at=time.time(),
            metadata={
                "mode": "running_order",
                "running_order_index": ro_index,
                "running_order_name": ro_name,
                "simulation": True,
                **self._connection_info,
            },
        )
        return {
            "running_order_index": ro_index,
            "running_order_name": ro_name,
            "pattern_result": self._prepared,
            "simulation": True,
        }

    def program_patterns(self, pattern_files: list[str], device_path: str | None = None) -> PatternPreparationResult:
        if len(pattern_files) != 9:
            raise RuntimeError("Exactly 9 pattern files are required.")
        if not self._connected:
            self.connect(device_path=device_path)
        self._prepared = PatternPreparationResult(
            pattern_files=list(pattern_files),
            handles=list(range(len(pattern_files))),
            prepared_at=time.time(),
            metadata={"mode": "simulation", **self._connection_info},
        )
        return self._prepared

    def activate_prepared_patterns(self) -> None:
        if not self._prepared.handles:
            raise RuntimeError("No simulated patterns or running order prepared on SLM.")
        if not self._connected:
            self.connect()

    def prepared_summary(self) -> dict[str, Any]:
        return asdict(self._prepared)


class SimulatedDaqAdapter:
    def list_devices(self, default_device: str = "Dev1") -> list[str]:
        return [default_device or "Dev1"]

    def list_port0_lines(self, device_name: str | None = None, default_device: str | None = None) -> list[str]:
        selected_device = device_name or default_device or "Dev1"
        return [f"{selected_device}/port0/line{index}" for index in range(16)]

    def play_waveform(self, device_name: str, plan: WaveformPlan) -> None:
        time.sleep(min(max(plan.duration_s, 0.0), 0.1))

    def set_all_low(self, device_name: str) -> None:
        return None

    def pulse_line(self, device_name: str, line_index: int, duration_s: float) -> None:
        parse_line_name(f"{device_name}/port0/line{int(line_index)}")
        time.sleep(min(max(float(duration_s), 0.0), 0.1))

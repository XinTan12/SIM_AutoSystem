"""Z stage adapter boundary for Nikon Ti2 ZDrive integration."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ZStageAdapter(Protocol):
    def connect(self) -> dict[str, Any]: ...

    def disconnect(self) -> None: ...

    def get_position_um(self) -> float: ...

    def move_z_um(self, target_um: float) -> None: ...

    def get_z_ranges_um(self) -> tuple[float, float]: ...


class SimulatedZStageAdapter:
    """In-memory Z stage for GUI and acquisition simulation."""

    def __init__(self, start_um: float = 0.0, min_um: float = -100.0, max_um: float = 100.0) -> None:
        self._position_um = float(start_um)
        self._min_um = float(min_um)
        self._max_um = float(max_um)
        self.is_connected = False

    def connect(self) -> dict[str, Any]:
        self.is_connected = True
        return {"mode": "simulated", "position_um": self._position_um, "range_um": self.get_z_ranges_um()}

    def disconnect(self) -> None:
        self.is_connected = False

    def get_position_um(self) -> float:
        return self._position_um

    def move_z_um(self, target_um: float) -> None:
        target = float(target_um)
        if not self._min_um <= target <= self._max_um:
            raise ValueError(f"Z target {target:.3f} um is outside range {self._min_um:.3f}..{self._max_um:.3f} um.")
        self._position_um = target

    def get_z_ranges_um(self) -> tuple[float, float]:
        return self._min_um, self._max_um


class Ti2ZStageAdapter:
    """Lazy wrapper around ``z-scan/ti2_sdk.py::Ti2Stage``."""

    def __init__(
        self,
        dll_path: str | Path | None = None,
        sdk_module_path: str | Path | None = None,
        device_index: int = 0,
    ) -> None:
        self.dll_path = Path(dll_path) if dll_path else None
        self.sdk_module_path = Path(sdk_module_path) if sdk_module_path else (
            Path(__file__).resolve().parent.parent / "z-scan" / "ti2_sdk.py"
        )
        self.device_index = int(device_index)
        self._stage: Any | None = None
        self.is_connected = False

    def connect(self) -> dict[str, Any]:
        if self._stage is None:
            module = self._load_ti2_module()
            self._stage = module.Ti2Stage(self.dll_path)
        self._stage.open(device_index=self.device_index)
        self.is_connected = True
        return {"mode": "ti2", "position_um": self.get_position_um(), "range_um": self.get_z_ranges_um()}

    def disconnect(self) -> None:
        if self._stage is not None:
            self._stage.close()
        self.is_connected = False

    def get_position_um(self) -> float:
        self._ensure_connected()
        return float(self._stage.read_z_position_um())

    def move_z_um(self, target_um: float) -> None:
        self._ensure_connected()
        result = self._stage.move_z_um(float(target_um))
        retcode = getattr(result, "retcode", 0)
        if retcode != 0:
            raise RuntimeError(f"Ti2 ZDrive move_z_um failed with retcode {retcode}.")

    def get_z_ranges_um(self) -> tuple[float, float]:
        self._ensure_connected()
        ranges = self._stage.get_z_ranges_um()
        selected = ranges.get("physical") or ranges.get("logical")
        if selected is None:
            raise RuntimeError("Ti2 SDK did not return a ZDrive range.")
        lower = getattr(selected, "lower_um", getattr(selected, "min_um", None))
        upper = getattr(selected, "upper_um", getattr(selected, "max_um", None))
        if lower is None or upper is None:
            raise RuntimeError(f"Ti2 SDK returned an unsupported ZDrive range object: {selected!r}")
        return float(lower), float(upper)

    def _ensure_connected(self) -> None:
        if self._stage is None or not self.is_connected:
            raise RuntimeError("Z stage is not connected.")

    def _load_ti2_module(self) -> Any:
        module_path = self.sdk_module_path
        if not module_path.exists():
            raise RuntimeError(f"Ti2 SDK wrapper not found: {module_path}")
        spec = importlib.util.spec_from_file_location("sim_control_ti2_sdk", module_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Cannot load Ti2 SDK wrapper: {module_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

"""Shared adapter construction helpers for SIM camera, SLM, DAQ, and Z stage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .adapters import FusionBtCameraAdapter, KopinSlmAdapter, NIDaqAdapter
from .models import BackendConfig
from .sim_adapters import SimulatedCameraAdapter, SimulatedDaqAdapter, SimulatedSlmAdapter
from .stage_adapter import SimulatedZStageAdapter, Ti2ZStageAdapter


@dataclass(frozen=True)
class AdapterBundle:
    camera: Any
    slm: Any
    daq: Any
    stage: Any


def create_camera_adapter_for_backend(backend: BackendConfig) -> Any:
    if backend.simulation_mode:
        return SimulatedCameraAdapter()
    return FusionBtCameraAdapter(sdk_path=backend.fusion_bt_sdk_path)


def create_slm_adapter_for_backend(backend: BackendConfig) -> Any:
    if backend.simulation_mode:
        return SimulatedSlmAdapter()
    return KopinSlmAdapter(sdk_path=backend.slm_sdk_path)


def create_daq_adapter_for_backend(backend: BackendConfig) -> Any:
    if backend.simulation_mode:
        return SimulatedDaqAdapter()
    return NIDaqAdapter()


def create_stage_adapter_for_backend(backend: BackendConfig) -> Any:
    if backend.simulation_mode:
        return SimulatedZStageAdapter()
    # 空字符串路径透传给 adapter 表示"沿用内部默认推导"，保持默认行为不变。
    return Ti2ZStageAdapter(
        dll_path=backend.ti2_dll_path or None,
        sdk_module_path=backend.ti2_sdk_module_path or None,
    )


def create_adapter_bundle(backend: BackendConfig) -> AdapterBundle:
    return AdapterBundle(
        camera=create_camera_adapter_for_backend(backend),
        slm=create_slm_adapter_for_backend(backend),
        daq=create_daq_adapter_for_backend(backend),
        stage=create_stage_adapter_for_backend(backend),
    )


__all__ = [
    "AdapterBundle",
    "create_adapter_bundle",
    "create_camera_adapter_for_backend",
    "create_daq_adapter_for_backend",
    "create_slm_adapter_for_backend",
    "create_stage_adapter_for_backend",
]

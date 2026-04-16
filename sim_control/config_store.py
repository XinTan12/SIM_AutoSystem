from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path

from .models import AppConfig, BackendConfig, CameraConfig, DaqLineConfig, TimingConfig


APP_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = APP_ROOT / "config" / "sim_control_config.json"
LEGACY_CONFIG_PATH = APP_ROOT / "sim_control_config.json"


def _merge_list(values: list[str], desired_length: int = 9) -> list[str]:
    merged = list(values[:desired_length])
    if len(merged) < desired_length:
        merged.extend([""] * (desired_length - len(merged)))
    return merged


def app_config_to_dict(config: AppConfig) -> dict:
    payload = asdict(config)
    if not payload.get("config_path"):
        payload["config_path"] = str(DEFAULT_CONFIG_PATH)
    payload["pattern_files"] = _merge_list(payload.get("pattern_files", []))
    return payload


def app_config_from_dict(payload: dict) -> AppConfig:
    daq = DaqLineConfig(**payload.get("daq", {}))
    camera = CameraConfig(**payload.get("camera", {}))
    timing = TimingConfig(**payload.get("timing", {}))
    backend = BackendConfig(**payload.get("backend", {}))
    pattern_files = _merge_list(payload.get("pattern_files", []))
    selected_laser_nm = int(payload.get("selected_laser_nm", 488))
    config_path = str(payload.get("config_path", DEFAULT_CONFIG_PATH))
    return AppConfig(
        daq=daq,
        camera=camera,
        timing=timing,
        backend=backend,
        pattern_files=pattern_files,
        selected_laser_nm=selected_laser_nm,
        config_path=config_path,
    )


def load_app_config(path: str | Path | None = None) -> AppConfig:
    config_path = Path(path or DEFAULT_CONFIG_PATH)
    if not path and not config_path.exists() and LEGACY_CONFIG_PATH.exists():
        with LEGACY_CONFIG_PATH.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        config = app_config_from_dict(payload)
        config.config_path = str(config_path)
        save_app_config(config, config_path)
        return config
    if not config_path.exists():
        config = AppConfig(config_path=str(config_path))
        save_app_config(config, config_path)
        return config
    with config_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    config = app_config_from_dict(payload)
    config.config_path = str(config_path)
    return config


def save_app_config(config: AppConfig, path: str | Path | None = None) -> Path:
    config_path = Path(path or config.config_path or DEFAULT_CONFIG_PATH)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config.config_path = str(config_path)
    payload = app_config_to_dict(config)
    with config_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    return config_path

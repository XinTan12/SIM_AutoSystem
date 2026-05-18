"""SIM 配置文件读写、迁移和校验。

这个模块负责把 JSON 配置与 AppConfig 数据类互相转换，并在加载旧配置时执行 schema 迁移，例如补齐 9 个 pattern 文件、将旧 640nm 命名迁移到 647nm、移除本机绝对 config_path。GUI 和集成主界面都通过这里读写默认配置。
"""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path

from .models import AppConfig, BackendConfig, CameraConfig, DaqLineConfig, SUPPORTED_LASERS, TimingConfig


APP_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = APP_ROOT / "config" / "sim_control_config.json"
LEGACY_CONFIG_PATH = APP_ROOT / "sim_control_config.json"

CURRENT_CONFIG_VERSION = 3


def _merge_list(values: list[str], desired_length: int = 9) -> list[str]:
    """按目标长度补齐或截断 pattern 文件列表，保持 SIM9 schema 稳定。"""
    merged = list(values[:desired_length])
    if len(merged) < desired_length:
        merged.extend([""] * (desired_length - len(merged)))
    return merged


def _migrate_v0_to_v1(payload: dict) -> dict:
    """Rename laser_640 → laser_647 in DAQ lines and selected_laser_nm."""
    daq = dict(payload.get("daq") or {})
    legacy_laser_line = daq.pop("laser_640_line", "")
    if legacy_laser_line and "laser_647_line" not in daq:
        daq["laser_647_line"] = legacy_laser_line
    payload["daq"] = daq
    selected = payload.get("selected_laser_nm")
    if selected is not None and int(selected) == 640:
        payload["selected_laser_nm"] = 647
    payload["config_version"] = 1
    return payload


def _migrate_v1_to_v2(payload: dict) -> dict:
    """执行一个配置 schema 版本迁移步骤，只处理该版本新增或变更的字段。"""
    backend = dict(payload.get("backend") or {})
    backend.setdefault("simulation_mode", False)
    payload["backend"] = backend
    payload["config_version"] = 2
    return payload


def _migrate_v2_to_v3(payload: dict) -> dict:
    """执行一个配置 schema 版本迁移步骤，只处理该版本新增或变更的字段。"""
    payload.setdefault("selected_running_order", "")
    payload["config_version"] = 3
    return payload


_MIGRATIONS: list[tuple[int, callable]] = [
    (0, _migrate_v0_to_v1),
    (1, _migrate_v1_to_v2),
    (2, _migrate_v2_to_v3),
]


# 迁移按版本串联执行，保证旧配置逐步升级而不是靠一次性猜测字段。
def _run_migrations(payload: dict) -> dict:
    """按版本顺序执行配置迁移，把旧 JSON 升级到当前 schema。"""
    version = int(payload.get("config_version", 0))
    for from_version, migrate_fn in _MIGRATIONS:
        if version <= from_version:
            payload = migrate_fn(payload)
            version = int(payload.get("config_version", from_version + 1))
    return payload


def app_config_to_dict(config: AppConfig) -> dict:
    """把 AppConfig 展开为可写入 JSON 的 schema 字典。"""
    payload = asdict(config)
    payload.pop("config_path", None)
    payload["pattern_files"] = _merge_list(payload.get("pattern_files", []))
    return payload


def app_config_from_dict(payload: dict) -> AppConfig:
    """从 JSON 字典恢复 AppConfig，并完成旧字段兼容和默认值补齐。"""
    payload = _run_migrations(dict(payload))
    daq = DaqLineConfig(**(payload.get("daq") or {}))
    camera = CameraConfig(**(payload.get("camera") or {}))
    timing = TimingConfig(**(payload.get("timing") or {}))
    backend_payload = payload.get("backend") or {}
    backend = BackendConfig(
        fusion_bt_sdk_path=str(backend_payload.get("fusion_bt_sdk_path", "")),
        slm_sdk_path=str(backend_payload.get("slm_sdk_path", "")),
        simulation_mode=bool(backend_payload.get("simulation_mode", False)),
    )
    pattern_files = _merge_list(payload.get("pattern_files", []))
    selected_running_order = str(payload.get("selected_running_order", ""))
    selected_laser_nm = int(payload.get("selected_laser_nm", 488))
    config_path = str(payload.get("config_path", DEFAULT_CONFIG_PATH))
    return AppConfig(
        daq=daq,
        camera=camera,
        timing=timing,
        backend=backend,
        pattern_files=pattern_files,
        selected_running_order=selected_running_order,
        selected_laser_nm=selected_laser_nm,
        config_version=CURRENT_CONFIG_VERSION,
        config_path=config_path,
    )


def validate_app_config(config: AppConfig) -> list[str]:
    """集中校验输入条件，把错误尽早转成可报告的问题。"""
    errors: list[str] = []
    camera = config.camera
    timing = config.timing

    if camera.exposure_us <= 0:
        errors.append("camera.exposure_us must be greater than 0.")
    if camera.roi_width <= 0:
        errors.append("camera.roi_width must be greater than 0.")
    if camera.roi_height <= 0:
        errors.append("camera.roi_height must be greater than 0.")
    if camera.roi_x < 0:
        errors.append("camera.roi_x must be >= 0.")
    if camera.roi_y < 0:
        errors.append("camera.roi_y must be >= 0.")
    if camera.roi_x + camera.roi_width > 2304:
        errors.append("camera ROI width exceeds the 2304 px sensor bounds.")
    if camera.roi_y + camera.roi_height > 2304:
        errors.append("camera ROI height exceeds the 2304 px sensor bounds.")
    if camera.timeout_ms < 100:
        errors.append("camera.timeout_ms must be >= 100.")

    if timing.sample_rate_hz < 1000:
        errors.append("timing.sample_rate_hz must be >= 1000.")
    if timing.edge_pulse_us <= 0:
        errors.append("timing.edge_pulse_us must be greater than 0.")
    if timing.slm_enable_guard_us <= 0:
        errors.append("timing.slm_enable_guard_us must be greater than 0.")
    if timing.inter_frame_gap_us < 0:
        errors.append("timing.inter_frame_gap_us must be >= 0.")

    if config.selected_laser_nm not in SUPPORTED_LASERS:
        errors.append(f"selected_laser_nm must be one of {SUPPORTED_LASERS}.")
    if not config.selected_running_order and len(config.pattern_files) != 9:
        errors.append("pattern_files must contain exactly 9 entries.")
    return errors


def load_app_config(path: str | Path | None = None) -> AppConfig:
    """从默认或指定路径读取配置，完成迁移和数据类转换。"""
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
    """把当前 AppConfig 写回 JSON 文件，并记录实际保存路径。"""
    config_path = Path(path or config.config_path or DEFAULT_CONFIG_PATH)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config.config_path = str(config_path)
    payload = app_config_to_dict(config)
    with config_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    return config_path

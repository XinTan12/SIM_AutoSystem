"""SIM 配置文件读写、版本迁移与校验。

作用：
    本文件是 ``AppConfig`` 数据类与磁盘上 JSON 配置文件之间的唯一桥梁：
        - ``load_app_config(path)``：从 JSON 读取并升级到当前 schema 版本。
        - ``save_app_config(config, path)``：把 ``AppConfig`` 写回 JSON。
        - ``app_config_from_dict`` / ``app_config_to_dict``：纯转换函数，
          便于测试与界面层不依赖文件系统。
        - ``_MIGRATIONS`` 串联版本迁移（v0→v1→v2→v3），让旧配置不会因为字段
          变更而失效。
        - ``validate_app_config``：在加载/保存前做范围校验，把错误尽早暴露。

协作关系：
    上游：``sim_control/gui.py``、``control_wangbo/main.py``、所有需要持久化
          配置的入口。
    下游：``models.py``（数据类与默认值）。
    相关：``tests/test_config_validation.py`` / ``test_legacy_sim_config_migration.py``
          覆盖本文件全部分支。

关键概念：
    - ``CURRENT_CONFIG_VERSION``：当前 schema 版本号。新增字段或重命名时
      要让此值 +1，并在 ``_MIGRATIONS`` 末尾追加迁移函数。
    - ``LEGACY_CONFIG_PATH``：仓库根目录的旧 ``sim_control_config.json``，
      仅在没有新路径文件时回落使用，加载后立刻迁移到默认目录。
    - 迁移链：``v0→v1``（640nm → 638nm 重命名）、``v1→v2``（补 simulation_mode
      默认）、``v2→v3``（补 ``selected_running_order``）。

维护要点：
    - 任何对 ``models.AppConfig`` 字段的添加/重命名/默认值变更都必须同步加
      一个迁移函数到 ``_MIGRATIONS``，并把 ``CURRENT_CONFIG_VERSION`` 升一。
    - ``save_app_config`` 会把 ``config_path`` 写回 ``config`` 对象，确保
      下次保存默认沿用相同路径。
    - 不要在本文件做硬件 SDK 调用或 GUI 弹窗；它只处理纯文本配置 IO。
"""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path

from .models import (
    AppConfig,
    BackendConfig,
    CameraConfig,
    DaqLineConfig,
    DEFAULT_RED_LASER_NM,
    RED_EQUIVALENT_WAVELENGTHS,
    RED_LASER_CHOICES,
    ReconstructionConfig,
    SLM_ENABLE_GUARD_RECOMMENDED_US,
    SUPPORTED_LASERS,
    TimingConfig,
    Z_SCAN_DIRECTIONS,
    Z_SCAN_EXPOSURE_PRESETS_MS,
    Z_SCAN_FOCUS_METRICS,
    ZScanConfig,
    supported_lasers_for,
)
from .sim_camera_presets import DEFAULT_SIM_CAMERA_SIZE


# 仓库根目录（``sim_control/`` 的上一级）。其它路径常量都以它为基准。
APP_ROOT = Path(__file__).resolve().parent.parent
# 默认配置文件路径。``app.py`` 与独立 SIM 入口默认从这里加载。
DEFAULT_CONFIG_PATH = APP_ROOT / "config" / "sim_control_config.json"
# 旧版本的根目录配置路径。新代码不再使用，但加载逻辑会回落兼容，免得用户的旧 JSON 丢失。
LEGACY_CONFIG_PATH = APP_ROOT / "sim_control_config.json"

# 当前 schema 版本号；新增字段时此值递增并配合 ``_MIGRATIONS`` 增加迁移。
# 必须与 ``models.AppConfig.config_version`` 默认值保持一致。
CURRENT_CONFIG_VERSION = 14
DEFAULT_RECONSTRUCTION_OUTPUT_DIR = "data/reconstruction"
RECONSTRUCTION_SAVED_PARAMS_FALLBACKS = {"fail", "estimate"}


def _merge_list(values: list[str], desired_length: int = 9) -> list[str]:
    """把 pattern 文件列表对齐到 ``desired_length``（默认 9 = SIM9 帧数）。

    用途：
        旧配置可能只存 0/6/12 项 pattern；本函数保证写回 JSON 与加载后内存
        中的列表长度始终为 9，避免下游 ``run_single_acquisition`` 越界。
    """
    # 1) 先截断到目标长度（多余项丢弃，通常表示旧 schema 多写了备份槽位）。
    merged = list(values[:desired_length])
    # 2) 不足则用空字符串补齐到 ``desired_length``；空字符串表示「该槽位未使用」。
    if len(merged) < desired_length:
        merged.extend([""] * (desired_length - len(merged)))
    return merged


def _parse_bool_field(value: object, field_name: str) -> bool:
    """严格解析配置布尔值，并兼容旧 JSON 的 0/1 与字符串表示。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1"):
            return True
        if normalized in ("false", "0"):
            return False
    raise ValueError(
        f"{field_name} must be a boolean (true/false or 1/0), got {value!r}"
    )


def _migrate_red_laser_aliases(payload: dict) -> dict:
    """归一**最老的错误红光命名 640** 到默认 638；**不再**触碰 647 与 DAQ 线键。

    双机红光（638/647）说明：638 与 647 都是真实、合法的机器红光波长身份，历史上"统一
    压成 638"的逻辑会丢失 647 机器身份，已废除。本函数只处理最老的错误命名 ``640``
    （既不是 638 也不是 647 的合法身份，是早期笔误），把 ``selected_laser_nm`` 与
    ``otf_640_path``/``estimated_params_640_path`` 归一到默认红光 638。

    DAQ 红光线键的中性化（``laser_638/647/640_line`` → ``laser_red_line``）以及 647 身份
    的推断/保留，统一交给 ``_migrate_v12_to_v13``——这里**刻意不动 DAQ**，以免在迁移链
    早期（v0→v1 / v11→v12）就把 ``laser_647_line`` 证据消耗掉，导致 v13 无法据它推断红光。
    """
    selected = payload.get("selected_laser_nm")
    try:
        selected_nm = int(selected) if selected is not None else None
    except (TypeError, ValueError):
        selected_nm = None
    if selected_nm == 640:
        payload["selected_laser_nm"] = 638

    if isinstance(payload.get("reconstruction"), dict):
        reconstruction = dict(payload.get("reconstruction") or {})
        for legacy_key, current_key in (
            ("otf_640_path", "otf_638_path"),
            ("estimated_params_640_path", "estimated_params_638_path"),
        ):
            value = reconstruction.pop(legacy_key, "")
            if value and current_key not in reconstruction:
                reconstruction[current_key] = value
        payload["reconstruction"] = reconstruction
    return payload


def _infer_red_laser_nm(payload: dict) -> int:
    """从 payload 推断机器第四路红光波长（638/647）。

    优先级：① 显式 ``red_laser_nm``（校验 ∈ RED_LASER_CHOICES，否则默认）；
    ② 647 证据（``selected_laser_nm==647`` / daq 非空 ``laser_647_line`` /
    reconstruction 非空 ``otf_647_path`` 或 ``estimated_params_647_path``）→ 647；
    ③ 否则默认 ``DEFAULT_RED_LASER_NM``（638）。

    必须在合并 ``laser_647_line``→``laser_red_line`` **之前**调用，否则线键证据已被消耗。
    """
    explicit = payload.get("red_laser_nm")
    try:
        explicit_nm = int(explicit) if explicit is not None else None
    except (TypeError, ValueError):
        explicit_nm = None
    if explicit_nm in RED_LASER_CHOICES:
        return explicit_nm

    selected = payload.get("selected_laser_nm")
    try:
        selected_nm = int(selected) if selected is not None else None
    except (TypeError, ValueError):
        selected_nm = None
    if selected_nm == 647:
        return 647

    daq = payload.get("daq") or {}
    if isinstance(daq, dict) and str(daq.get("laser_647_line", "")).strip():
        return 647

    reconstruction = payload.get("reconstruction") or {}
    if isinstance(reconstruction, dict) and (
        str(reconstruction.get("otf_647_path", "")).strip()
        or str(reconstruction.get("estimated_params_647_path", "")).strip()
    ):
        return 647

    return DEFAULT_RED_LASER_NM


def _migrate_v0_to_v1(payload: dict) -> dict:
    """v0 -> v1 migration: rename legacy 640 nm red-laser fields to 638 nm."""
    payload = _migrate_red_laser_aliases(payload)
    payload["config_version"] = 1
    return payload


def _migrate_v1_to_v2(payload: dict) -> dict:
    """v1 → v2 迁移：补齐 ``backend.simulation_mode`` 默认字段。"""
    # 1) backend 段是可选的，缺失时按空字典处理。
    backend = dict(payload.get("backend") or {})
    # 2) 旧配置不一定写过 simulation_mode，``setdefault`` 保证不覆盖用户已设置的值。
    backend.setdefault("simulation_mode", False)
    payload["backend"] = backend
    # 3) 版本号 +1，链式迁移逻辑据此前进。
    payload["config_version"] = 2
    return payload


def _migrate_v2_to_v3(payload: dict) -> dict:
    """v2 → v3 迁移：补齐 ``selected_running_order`` 字段。

    背景：
        2026-04-26 SLM 改走 Running Order 之后新增此字段；旧配置缺少该键时
        给空字符串作为安全默认（GUI 会显示「SLM 未连接」）。
    """
    # 仅当顶层字典缺少键时补默认值；``setdefault`` 不会覆盖已有数据。
    payload.setdefault("selected_running_order", "")
    payload["config_version"] = 3
    return payload


def _migrate_v3_to_v4(payload: dict) -> dict:
    """v3 → v4 migration: add pre-SIM z-scan autofocus settings."""
    payload.setdefault("z_scan", asdict(ZScanConfig()))
    payload["config_version"] = 4
    return payload


def _migrate_v4_to_v5(payload: dict) -> dict:
    """v4 -> v5 migration: add SIM9 reconstruction settings."""
    payload.setdefault("reconstruction", asdict(ReconstructionConfig()))
    payload["config_version"] = 5
    return payload


def _migrate_v5_to_v6(payload: dict) -> dict:
    """v5 -> v6 migration: add reconstruction output path and enable recon by default."""
    reconstruction = dict(payload.get("reconstruction") or {})
    reconstruction["enabled"] = True
    reconstruction.setdefault("output_path", "")
    payload["reconstruction"] = reconstruction
    payload["config_version"] = 6
    return payload


def _reconstruction_output_dir_from_legacy_value(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return DEFAULT_RECONSTRUCTION_OUTPUT_DIR
    trimmed = raw.rstrip("/\\")
    if trimmed.lower().endswith((".tif", ".tiff")):
        separator_index = max(trimmed.rfind("/"), trimmed.rfind("\\"))
        if separator_index > 0:
            return trimmed[:separator_index]
        return DEFAULT_RECONSTRUCTION_OUTPUT_DIR
    return raw


def _migrate_v6_to_v7(payload: dict) -> dict:
    """v6 -> v7 migration: treat reconstruction output_path as an output directory."""
    reconstruction = dict(payload.get("reconstruction") or {})
    reconstruction["output_path"] = _reconstruction_output_dir_from_legacy_value(
        reconstruction.get("output_path", "")
    )
    payload["reconstruction"] = reconstruction
    payload["config_version"] = 7
    return payload


def _migrate_v7_to_v8(payload: dict) -> dict:
    """v7 -> v8 migration: drop formal Z-scan cancel return-to-start setting."""
    z_scan = dict(payload.get("z_scan") or {})
    z_scan.pop("return_to_start_on_cancel", None)
    payload["z_scan"] = z_scan
    payload["config_version"] = 8
    return payload


def _migrate_v8_to_v9(payload: dict) -> dict:
    """v8 -> v9 migration: raise slm_enable_guard_us below the R11 tHWAT margin.

    背景：
        R11 数据手册 PD0011CA Table 7-1 规定 EXT_RUN 拉高后最长 500 µs（tHWAT）
        才进入 Active Mode；旧默认 guard=50 µs 会让首个 SLM trigger 在 [HWA h]
        RO 激活完成前被丢弃（第一帧黑帧 + 图案错位一帧）。低于推荐值的旧配置
        统一抬到 ``SLM_ENABLE_GUARD_RECOMMENDED_US``；用户仍可事后手动调低。
    """
    timing = dict(payload.get("timing") or {})
    try:
        guard_value = int(timing.get("slm_enable_guard_us", 0))
    except (TypeError, ValueError):
        guard_value = 0
    if guard_value < SLM_ENABLE_GUARD_RECOMMENDED_US:
        timing["slm_enable_guard_us"] = SLM_ENABLE_GUARD_RECOMMENDED_US
    payload["timing"] = timing
    payload["config_version"] = 9
    return payload


def _migrate_v9_to_v10(payload: dict) -> dict:
    """v9 -> v10 migration: 补齐 BackendConfig 的 Ti2 路径字段默认空串。

    背景：
        v10 把 Nikon Ti2 ZDrive 的 DLL / SDK wrapper 路径纳入 ``BackendConfig``，
        以便按配置定位 SDK 而非硬编码推导（见 AGENTS.md「SDK 定位不要硬编码，
        优先走配置」）。旧配置没有这两个键，统一 ``setdefault`` 为空字符串；
        空串表示沿用 ``Ti2ZStageAdapter`` 内部默认推导路径，运行行为与 v9 一致。
    """
    backend = dict(payload.get("backend") or {})
    backend.setdefault("ti2_dll_path", "")
    backend.setdefault("ti2_sdk_module_path", "")
    payload["backend"] = backend
    payload["config_version"] = 10
    return payload


def _migrate_v10_to_v11(payload: dict) -> dict:
    """v10 -> v11 migration: 补齐 saved-params 与异步落盘相关重建字段。

    背景：
        v11 为 SIM9 重建新增 saved-params 快路径（~45ms 热）与异步落盘控制。生产现场
        标定好每波长 ``.mat`` 后可置 ``use_saved_params=True``；但旧配置通常还没有
        ``.mat`` 路径。为遵守"仿真优先"硬约束、避免升级后默认配置直接无法重建，迁移时
        若旧配置没有任何 ``estimated_params_*_path``，强制 ``use_saved_params=False``，
        让旧配置升级后仍走 estimate 路径，由用户标定后自行开启 saved。
    """
    reconstruction = dict(payload.get("reconstruction") or {})
    reconstruction.setdefault("use_saved_params", False)
    reconstruction.setdefault("saved_params_fallback", "fail")
    for wavelength in SUPPORTED_LASERS:
        reconstruction.setdefault(f"estimated_params_{wavelength}_path", "")
    reconstruction.setdefault("save_reconstruction_output", True)
    reconstruction.setdefault("async_save_reconstruction_output", True)
    reconstruction.setdefault("save_queue_maxsize", 4)
    has_saved_params = any(
        str(reconstruction.get(f"estimated_params_{wavelength}_path", "")).strip()
        for wavelength in SUPPORTED_LASERS
    )
    if not has_saved_params:
        reconstruction["use_saved_params"] = False
    payload["reconstruction"] = reconstruction
    payload["config_version"] = 11
    return payload


def _migrate_v11_to_v12(payload: dict) -> dict:
    """v11 -> v12 migration: 归一最老的错误红光命名 640，并补齐 638 重建字段。

    注意：双机红光改造后本步**不再**把 647 压成 638（647 身份保留给 v13 推断）；
    ``_migrate_red_laser_aliases`` 现在只处理 640。这里仍 ``setdefault`` 638 OTF/params
    字段，保证旧 v11 配置升级后有完整的 638 字段。
    """
    payload = _migrate_red_laser_aliases(payload)
    reconstruction = dict(payload.get("reconstruction") or {})
    reconstruction.setdefault("otf_638_path", "")
    reconstruction.setdefault("estimated_params_638_path", "")
    payload["reconstruction"] = reconstruction
    payload["config_version"] = 12
    return payload


def _migrate_v12_to_v13(payload: dict) -> dict:
    """v12 -> v13 migration: 第四路红光波长按机器可配置（638/647）。

    取代历史"统一压成 638"：
      1) **先**按 647 证据推断 ``red_laser_nm``（``_infer_red_laser_nm`` 须在合并线键前调）；
      2) DAQ 红光线键 ``laser_638/647/640_line`` 统一合并到中性 ``laser_red_line``
         （保留线值，首个非空者胜出，不覆盖已有 ``laser_red_line``）；
      3) reconstruction 补齐 647 OTF/params 字段；
      4) 校正 ``selected_laser_nm`` 使其 ∈ ``supported_lasers_for(red_laser_nm)``
         （选中的另一红光身份归位到本机红光；非红光的 405/488/561 保持不变）。
    """
    # 1) 先推断红光身份（必须在消耗 laser_647_line 之前）。
    red_laser_nm = _infer_red_laser_nm(payload)
    payload["red_laser_nm"] = red_laser_nm

    # 2) DAQ 红光线键统一中性化为 laser_red_line。
    daq = dict(payload.get("daq") or {})
    legacy_laser_line = ""
    for legacy_key in ("laser_640_line", "laser_647_line", "laser_638_line"):
        value = daq.pop(legacy_key, "")
        if value and not legacy_laser_line:
            legacy_laser_line = value
    if legacy_laser_line and not str(daq.get("laser_red_line", "")).strip():
        daq["laser_red_line"] = legacy_laser_line
    if daq or "daq" in payload:
        payload["daq"] = daq

    # 3) 补齐 647 重建字段（与 638 字段并存，按机器波长取用）。
    reconstruction = dict(payload.get("reconstruction") or {})
    reconstruction.setdefault("otf_647_path", "")
    reconstruction.setdefault("estimated_params_647_path", "")
    payload["reconstruction"] = reconstruction

    # 4) 校正 selected_laser_nm：选中的红光身份归位到本机红光，保证三方一致。
    selected = payload.get("selected_laser_nm")
    try:
        selected_nm = int(selected) if selected is not None else None
    except (TypeError, ValueError):
        selected_nm = None
    if selected_nm in RED_EQUIVALENT_WAVELENGTHS and selected_nm != red_laser_nm:
        payload["selected_laser_nm"] = red_laser_nm

    payload["config_version"] = 13
    return payload


def _migrate_v13_to_v14(payload: dict) -> dict:
    """v13 -> v14 migration: persist focus selection and discard stale absolute Z starts.

    Existing Z-Scan behavior selected the best focus plane.  Defaulting the new flag to
    ``True`` preserves that behavior instead of silently switching upgraded configs to the
    reserved Z-stack mode.  ``start_um`` is intentionally reset because each run must start
    from the stage's current position rather than a machine-specific saved coordinate.
    """
    z_scan = dict(payload.get("z_scan") or {})
    z_scan.setdefault("select_focus_plane", True)
    z_scan["start_um"] = None
    payload["z_scan"] = z_scan
    payload["config_version"] = 14
    return payload


# 迁移链表：(适用起始版本, 迁移函数)；按顺序串联，逐版本前进。
_MIGRATIONS: list[tuple[int, callable]] = [
    (0, _migrate_v0_to_v1),
    (1, _migrate_v1_to_v2),
    (2, _migrate_v2_to_v3),
    (3, _migrate_v3_to_v4),
    (4, _migrate_v4_to_v5),
    (5, _migrate_v5_to_v6),
    (6, _migrate_v6_to_v7),
    (7, _migrate_v7_to_v8),
    (8, _migrate_v8_to_v9),
    (9, _migrate_v9_to_v10),
    (10, _migrate_v10_to_v11),
    (11, _migrate_v11_to_v12),
    (12, _migrate_v12_to_v13),
    (13, _migrate_v13_to_v14),
]


def _run_migrations(payload: dict) -> dict:
    """按版本顺序执行配置迁移，把旧 JSON 升级到当前 schema。

    用途：
        新加载的 dict 可能是 v0 / v1 / v2 / v3 任一版本；本函数把它一路升到
        ``CURRENT_CONFIG_VERSION``。中途若某步抛错，迁移在出错点停止，
        外层调用方应捕获并提示用户重置或手动迁移。
    """
    # 1) 读取当前版本号，缺失视为 v0（最古早格式）。
    version = int(payload.get("config_version", 0))
    # 2) 依次比对迁移起始版本，命中则执行并刷新本地版本号。
    for from_version, migrate_fn in _MIGRATIONS:
        if version <= from_version:
            payload = migrate_fn(payload)
            # 迁移函数自身会写新版本号；这里再读一次以容忍迁移函数遗漏。
            version = int(payload.get("config_version", from_version + 1))
    return payload


def app_config_to_dict(config: AppConfig) -> dict:
    """把 ``AppConfig`` 展开为可写入 JSON 的 schema 字典。

    用途：
        - ``save_app_config`` 在写盘前调用本函数得到纯字典。
        - 单元测试可以脱离文件直接断言 JSON 结构。

    维护要点：
        - 不把本机绝对 ``config_path`` 写入 JSON：避免在多机环境下回放配置时
          锁定到陌生路径。
        - 强制对 ``pattern_files`` 做 ``_merge_list`` 对齐，保证 9 项不变。
    """
    # 1) ``asdict`` 递归把 dataclass 转成 dict，子配置一并展开。
    payload = asdict(config)
    # 2) 移除本机路径，避免不同机器加载时出现陌生绝对路径。
    payload.pop("config_path", None)
    # 3) 强制对 pattern_files 做长度对齐，写盘格式始终 9 项。
    payload["pattern_files"] = _merge_list(payload.get("pattern_files", []))
    # 4) 写盘前强制标当前 schema 版本：防止 ``AppConfig`` 默认值漂移时写出
    #    过期版本号、导致下次加载重跑迁移（迁移非幂等时会出错）。
    payload["config_version"] = CURRENT_CONFIG_VERSION
    return payload


def app_config_from_dict(payload: dict) -> AppConfig:
    """从 JSON 字典恢复 ``AppConfig``，并完成旧字段兼容、默认值补齐。

    流程：
        1. 复制 dict 防止外部副作用，再跑迁移链升级 schema 版本。
        2. 逐段构造子 dataclass（DAQ/Camera/Timing/Backend）。
        3. 补齐 9 项 pattern_files，并强制 ``config_version`` 标到当前版本。
    """
    # 1) 用 dict 副本喂迁移链，避免修改调用方传入的对象。
    #    注意：绝不在迁移链外再调用 ``_migrate_red_laser_aliases``——历史上那条二次调用会把
    #    每次加载的 647 机器配置重新压回 638（命门）。红光身份归一只在迁移链内（v13）处理。
    payload = _run_migrations(dict(payload))
    # 2) 各子配置直接用 ``**`` 解包：缺失字段由 dataclass 默认值兜底。
    daq = DaqLineConfig(**(payload.get("daq") or {}))
    camera = CameraConfig(**(payload.get("camera") or {}))
    timing = TimingConfig(**(payload.get("timing") or {}))
    # 3) backend 段需要显式 ``str(...)`` / ``bool(...)`` 规整，因为旧 JSON
    #    可能用 ``null`` 或 ``1/0`` 表达，dataclass 默认值无法直接吸收。
    backend_payload = payload.get("backend") or {}
    backend = BackendConfig(
        fusion_bt_sdk_path=str(backend_payload.get("fusion_bt_sdk_path", "")),
        slm_sdk_path=str(backend_payload.get("slm_sdk_path", "")),
        ti2_dll_path=str(backend_payload.get("ti2_dll_path", "")),
        ti2_sdk_module_path=str(backend_payload.get("ti2_sdk_module_path", "")),
        simulation_mode=bool(backend_payload.get("simulation_mode", False)),
    )
    z_scan_payload = payload.get("z_scan") or {}
    z_scan = ZScanConfig(
        enabled=_parse_bool_field(z_scan_payload.get("enabled", True), "z_scan.enabled"),
        start_um=(
            None
            if z_scan_payload.get("start_um", None) in (None, "")
            else float(z_scan_payload.get("start_um"))
        ),
        select_focus_plane=_parse_bool_field(
            z_scan_payload.get("select_focus_plane", True),
            "z_scan.select_focus_plane",
        ),
        direction=str(z_scan_payload.get("direction", "positive_z")),
        step_um=float(z_scan_payload.get("step_um", 0.3)),
        num_steps=int(z_scan_payload.get("num_steps", 10)),
        exposure_preset_ms=int(z_scan_payload.get("exposure_preset_ms", 8)),
        focus_metric=str(z_scan_payload.get("focus_metric", "sml")),
    )
    reconstruction_payload = payload.get("reconstruction") or {}
    theta_values = tuple(int(value) for value in reconstruction_payload.get("theta_ratio", (1, 1, 1)))
    if len(theta_values) != 3:
        theta_values = (1, 1, 1)
    reconstruction = ReconstructionConfig(
        enabled=bool(reconstruction_payload.get("enabled", True)),
        backend=str(reconstruction_payload.get("backend", "sim_wiener_gpu")),
        device=str(reconstruction_payload.get("device", "cuda")),
        dtype=str(reconstruction_payload.get("dtype", "single")),
        otf_405_path=str(reconstruction_payload.get("otf_405_path", "")),
        otf_488_path=str(reconstruction_payload.get("otf_488_path", "")),
        otf_561_path=str(reconstruction_payload.get("otf_561_path", "")),
        otf_638_path=str(reconstruction_payload.get("otf_638_path", "")),
        otf_647_path=str(reconstruction_payload.get("otf_647_path", "")),
        background_path=str(reconstruction_payload.get("background_path", "")),
        output_path=str(reconstruction_payload.get("output_path", DEFAULT_RECONSTRUCTION_OUTPUT_DIR)),
        wiener=float(reconstruction_payload.get("wiener", 2.0)),
        pixel_size_nm=float(reconstruction_payload.get("pixel_size_nm", 65.0)),
        excitation_na=float(reconstruction_payload.get("excitation_na", 1.49)),
        theta_ratio=theta_values,
        recon_group_batch=int(reconstruction_payload.get("recon_group_batch", 1)),
        use_saved_params=bool(reconstruction_payload.get("use_saved_params", False)),
        saved_params_fallback=str(reconstruction_payload.get("saved_params_fallback", "fail")),
        estimated_params_405_path=str(reconstruction_payload.get("estimated_params_405_path", "")),
        estimated_params_488_path=str(reconstruction_payload.get("estimated_params_488_path", "")),
        estimated_params_561_path=str(reconstruction_payload.get("estimated_params_561_path", "")),
        estimated_params_638_path=str(reconstruction_payload.get("estimated_params_638_path", "")),
        estimated_params_647_path=str(reconstruction_payload.get("estimated_params_647_path", "")),
        save_reconstruction_output=bool(reconstruction_payload.get("save_reconstruction_output", True)),
        async_save_reconstruction_output=bool(
            reconstruction_payload.get("async_save_reconstruction_output", True)
        ),
        save_queue_maxsize=int(reconstruction_payload.get("save_queue_maxsize", 4)),
    )
    # 4) pattern_files 对齐到 9，并提取顶层用户选择字段。
    pattern_files = _merge_list(payload.get("pattern_files", []))
    selected_running_order = str(payload.get("selected_running_order", ""))
    selected_laser_nm = int(payload.get("selected_laser_nm", 488))
    red_laser_nm = int(payload.get("red_laser_nm", DEFAULT_RED_LASER_NM))
    # 5) ``config_path`` 在 JSON 中通常为空，回落到 DEFAULT_CONFIG_PATH 字符串。
    config_path = str(payload.get("config_path", DEFAULT_CONFIG_PATH))
    return AppConfig(
        daq=daq,
        camera=camera,
        timing=timing,
        backend=backend,
        z_scan=z_scan,
        reconstruction=reconstruction,
        pattern_files=pattern_files,
        selected_running_order=selected_running_order,
        selected_laser_nm=selected_laser_nm,
        red_laser_nm=red_laser_nm,
        config_version=CURRENT_CONFIG_VERSION,
        config_path=config_path,
    )


def validate_app_config(config: AppConfig) -> list[str]:
    """集中校验 ``AppConfig`` 字段范围，把错误打包成可在 UI 弹窗中显示的字符串列表。

    返回：
        错误信息列表；空列表表示通过校验。

    维护要点：
        - 错误消息使用英文短句，方便日志/界面统一；调用方可自行翻译。
        - 校验维度覆盖相机 ROI、曝光、超时；时序采样率与脉冲宽度；激光波长与 pattern 数量。
    """
    errors: list[str] = []
    camera = config.camera
    timing = config.timing

    # 1) 相机基本字段必须为正整数，否则 DCAM 设置时会被 SDK 拒绝。
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
    # 2) ROI 不能超过 Fusion BT 传感器物理边界；尺寸统一引用
    #    ``sim_camera_presets.DEFAULT_SIM_CAMERA_SIZE``，避免 2304 双源硬编码。
    sensor_width, sensor_height = DEFAULT_SIM_CAMERA_SIZE
    if camera.roi_x + camera.roi_width > sensor_width:
        errors.append(f"camera ROI width exceeds the {sensor_width} px sensor bounds.")
    if camera.roi_y + camera.roi_height > sensor_height:
        errors.append(f"camera ROI height exceeds the {sensor_height} px sensor bounds.")
    # 3) 相机超时不应小于 100 ms，否则容易误报 timeout。
    if camera.timeout_ms < 100:
        errors.append("camera.timeout_ms must be >= 100.")

    # 4) DAQ 时序：采样率不能低于 1 kHz，脉冲与保护时间必须为正。
    if timing.sample_rate_hz < 1000:
        errors.append("timing.sample_rate_hz must be >= 1000.")
    if timing.edge_pulse_us <= 0:
        errors.append("timing.edge_pulse_us must be greater than 0.")
    if timing.slm_enable_guard_us <= 0:
        errors.append("timing.slm_enable_guard_us must be greater than 0.")
    if timing.inter_frame_gap_us < 0:
        errors.append("timing.inter_frame_gap_us must be >= 0.")

    z_scan = config.z_scan
    if z_scan.direction not in Z_SCAN_DIRECTIONS:
        errors.append(f"z_scan.direction must be one of {Z_SCAN_DIRECTIONS}.")
    if z_scan.step_um <= 0:
        errors.append("z_scan.step_um must be greater than 0.")
    if z_scan.num_steps < 1:
        errors.append("z_scan.num_steps must be >= 1.")
    if int(z_scan.exposure_preset_ms) not in Z_SCAN_EXPOSURE_PRESETS_MS:
        errors.append(f"z_scan.exposure_preset_ms must be one of {Z_SCAN_EXPOSURE_PRESETS_MS}.")
    if z_scan.focus_metric not in Z_SCAN_FOCUS_METRICS:
        errors.append(f"z_scan.focus_metric must be one of {Z_SCAN_FOCUS_METRICS}.")

    reconstruction = config.reconstruction
    if reconstruction.backend != "sim_wiener_gpu":
        errors.append("reconstruction.backend must be 'sim_wiener_gpu'.")
    if reconstruction.dtype not in {"single", "float32", "fp32", "double", "float64", "fp64"}:
        errors.append("reconstruction.dtype must be single/float32/fp32 or double/float64/fp64.")
    if reconstruction.wiener <= 0:
        errors.append("reconstruction.wiener must be greater than 0.")
    if reconstruction.pixel_size_nm <= 0:
        errors.append("reconstruction.pixel_size_nm must be greater than 0.")
    if reconstruction.excitation_na <= 0:
        errors.append("reconstruction.excitation_na must be greater than 0.")
    if reconstruction.recon_group_batch < 1:
        errors.append("reconstruction.recon_group_batch must be >= 1.")
    if reconstruction.output_path.strip().lower().endswith((".tif", ".tiff")):
        errors.append("reconstruction.output_path must be an output directory, not a TIFF file.")
    if tuple(reconstruction.theta_ratio) == () or any(int(value) <= 0 for value in reconstruction.theta_ratio):
        errors.append("reconstruction.theta_ratio values must be positive.")
    if reconstruction.saved_params_fallback not in RECONSTRUCTION_SAVED_PARAMS_FALLBACKS:
        errors.append(
            "reconstruction.saved_params_fallback must be one of "
            f"{sorted(RECONSTRUCTION_SAVED_PARAMS_FALLBACKS)}."
        )
    if reconstruction.save_queue_maxsize < 1:
        errors.append("reconstruction.save_queue_maxsize must be >= 1.")
    if reconstruction.enabled:
        otf_field = f"otf_{int(config.selected_laser_nm)}_path"
        if not reconstruction.otf_path_for_wavelength(config.selected_laser_nm).strip():
            errors.append(f"reconstruction.{otf_field} must be set when reconstruction is enabled.")
        # saved-params 生产档：启用 saved 时所选波长 .mat 必须配置且存在；缺失直接阻止
        # 正式采集（不静默降级），与 saved_params_fallback="fail" 的安全语义一致。
        if reconstruction.use_saved_params:
            params_field = f"estimated_params_{int(config.selected_laser_nm)}_path"
            params_path = reconstruction.estimated_params_path_for_wavelength(config.selected_laser_nm).strip()
            if not params_path:
                errors.append(
                    f"reconstruction.{params_field} must be set when use_saved_params is enabled."
                )
            elif not Path(params_path).is_file():
                errors.append(
                    f"reconstruction.{params_field} file does not exist: {params_path}"
                )

    # 5) 顶层用户选择：红光波长身份合法；采集波长必须在本机四档内；非 RO 模式 pattern 9 项。
    if config.red_laser_nm not in RED_LASER_CHOICES:
        errors.append(f"red_laser_nm must be one of {RED_LASER_CHOICES}.")
    machine_lasers = supported_lasers_for(config.red_laser_nm)
    if config.selected_laser_nm not in machine_lasers:
        errors.append(f"selected_laser_nm must be one of {machine_lasers}.")
    if not config.selected_running_order and len(config.pattern_files) != 9:
        errors.append("pattern_files must contain exactly 9 entries.")
    return errors


def load_app_config(path: str | Path | None = None) -> AppConfig:
    """从指定（或默认）路径读取配置，完成迁移和数据类转换。

    路径解析顺序：
        1. 传入 ``path`` 不为空 → 直接使用。
        2. ``path`` 为空且 ``DEFAULT_CONFIG_PATH`` 不存在但 ``LEGACY_CONFIG_PATH`` 存在
           → 读旧路径并立刻搬到默认路径。
        3. 默认路径都不存在 → 创建默认 ``AppConfig`` 并写盘，返回新对象。

    副作用：
        - 当回落到旧路径或创建默认时，会**写盘**保存一份新的默认配置。
        - 修改返回的 ``AppConfig.config_path`` 字段为实际加载路径。
    """
    # 1) 解析候选路径：显式参数优先，否则用项目默认路径。
    config_path = Path(path or DEFAULT_CONFIG_PATH)
    # 2) 旧路径回落：仅当用户未显式指定 ``path`` 且默认文件不存在时启用，
    #    避免显式指向某个不存在的路径时被旧路径"吞掉"。
    if not path and not config_path.exists() and LEGACY_CONFIG_PATH.exists():
        with LEGACY_CONFIG_PATH.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        config = app_config_from_dict(payload)
        config.config_path = str(config_path)
        # 把旧路径内容立刻另存到新路径，下次启动就走正常分支。
        save_app_config(config, config_path)
        return config
    # 3) 默认路径不存在：创建默认 AppConfig 并写盘，返回新对象。
    if not config_path.exists():
        config = AppConfig(config_path=str(config_path))
        save_app_config(config, config_path)
        return config
    # 4) 正常路径：读 JSON → 迁移 → 构造 AppConfig，并记录实际加载路径。
    with config_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    config = app_config_from_dict(payload)
    config.config_path = str(config_path)
    return config


def save_app_config(config: AppConfig, path: str | Path | None = None) -> Path:
    """把当前 ``AppConfig`` 写回 JSON 文件，并记录实际保存路径。

    路径选择：
        优先使用显式 ``path`` 参数；其次使用 ``config.config_path``；最后回落
        到 ``DEFAULT_CONFIG_PATH``。

    副作用：
        - 创建目标目录（``mkdir(parents=True, exist_ok=True)``）。
        - 修改 ``config.config_path`` 字段，反映本次保存位置。
        - 用 UTF-8 写盘，并保留中文（``ensure_ascii=False``）以便人读。
    """
    # 1) 解析最终保存路径：显式参数 → 配置自身记录 → 默认路径。
    config_path = Path(path or config.config_path or DEFAULT_CONFIG_PATH)
    # 2) 创建目录（若不存在）；``exist_ok`` 避免重复创建抛错。
    config_path.parent.mkdir(parents=True, exist_ok=True)
    # 3) 更新 AppConfig 自身的路径字段，方便上层显示「上次保存到这里」。
    config.config_path = str(config_path)
    # 4) 转 dict（自动剔除 config_path、对齐 pattern_files）并写盘。
    payload = app_config_to_dict(config)
    with config_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    return config_path

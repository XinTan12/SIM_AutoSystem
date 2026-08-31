"""主界面 SIM 设置摘要文本生成器。

作用：
    把 ``AppConfig`` 与运行时相机 timing 信息格式化成一段多行文本，供
    ``control_wangbo/main.py`` 集成主界面以只读方式展示正式采集 Running
    Order、相机原始 timing、理论/有效帧间隔、9 帧采集预估总时长、Z-Scan
    派生信息以及内部 Timing 字段。摘要只保留主 GUI 其他模块没有直接显示的
    信息，避免重复展示激光、相机、DAQ 接线等已在一级模块中可见的配置。

协作关系：
    上游：``control_wangbo/main.py``（调用 ``build_sim_settings_summary()``
          刷新只读 ``QTextEdit`` 摘要区域）、``tests/test_sim_summary.py``。
    下游：``models.AppConfig``。

关键概念：
    - ``runtime_timing``：相机实际下发配置后回报的 dict，包含当次
      ``camera_config`` 签名、三个原始 timing、理论最小 gap、安全余量、最终
      有效 gap 与 fallback 诊断。
    - SIM9 总时长估算：``2 × guard_samples + 9 × (exposure_samples +
      gap_samples)``，再加固定 10 ms stack 整理开销。
    - runtime 的 ``camera_config`` 必须与当前正式配置逐字段匹配；否则视为 stale
      preview timing，全部运行时 timing 显示未知并回退 50 ms 估时。

维护要点：
    - 摘要输出格式（行顺序、键名）会被 ``tests/test_sim_summary.py`` 断言；
      调整文本前请同步更新测试。
    - 总时长估算与 ``waveform.NIDaqWaveformBuilder`` 的样本数算法**必须**保
      持一致（同样用 ``math.ceil`` 折算），否则 GUI 显示的预估与真实播放时
      长会偏差。
"""

from __future__ import annotations

import math
from typing import Any, Sequence

from .camera_timing import camera_config_signature
from .models import AppConfig, DEFAULT_INTER_FRAME_GAP_US, Z_SCAN_EXPOSURE_PRESETS_US
from .z_scan_timing_history import (
    ZScanTimingEntry,
    estimate_z_scan_capture_test_total_time_ms,
    estimate_z_scan_move_only_total_time_ms,
    load_z_scan_timing_records,
)

# SIM9 固定 9 帧：与 ``acquisition_core.run_single_acquisition`` 内的 ``frame_count`` 对齐。
SIM9_FRAME_COUNT = 9
# 从波形结束到 9 帧 stack 可读出额外预留的 stack 整理开销（微秒）。
# 经验值，覆盖 DCAM transfer + numpy 重整 + 控制器信号传递的累计开销。
SIM9_STACK_TRANSFER_OVERHEAD_US = 10_000
def _camera_timing_signature_matches(
    sim_config: AppConfig,
    runtime_timing: dict[str, Any] | None,
) -> tuple[bool, str]:
    """判断 runtime timing 是否确属当前正式相机配置。"""
    if not runtime_timing:
        return False, "runtime_timing_unavailable"
    runtime_camera = runtime_timing.get("camera_config")
    if not isinstance(runtime_camera, dict):
        return False, "camera_config_missing"
    try:
        runtime_signature = camera_config_signature(runtime_camera)
        current_signature = camera_config_signature(sim_config.camera)
    except KeyError:
        return False, "camera_config_missing"
    except (AttributeError, TypeError, ValueError, OverflowError):
        return False, "camera_config_mismatch"
    if runtime_signature != current_signature:
        return False, "camera_config_mismatch"
    return True, ""


def _nonnegative_microseconds(value: Any) -> int | None:
    """把合法的有限非负微秒值安全地向上折算成整数。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(numeric) or numeric < 0:
        return None
    return int(math.ceil(numeric))


def _runtime_timing_state(
    sim_config: AppConfig,
    runtime_timing: dict[str, Any] | None,
) -> dict[str, Any]:
    """生成摘要显示和 SIM9 估时共用的已验证 timing 状态。"""
    signature_matches, signature_reason = _camera_timing_signature_matches(sim_config, runtime_timing)
    if not signature_matches:
        return {
            "runtime_timing": None,
            "effective_gap_us": DEFAULT_INTER_FRAME_GAP_US,
            "fallback_used": True,
            "fallback_reason": signature_reason,
        }

    assert runtime_timing is not None
    recommended_gap_us = _nonnegative_microseconds(runtime_timing.get("recommended_inter_frame_gap_us"))
    if recommended_gap_us is None:
        fallback_reason = str(runtime_timing.get("timing_fallback_reason") or "").strip()
        return {
            "runtime_timing": runtime_timing,
            "effective_gap_us": DEFAULT_INTER_FRAME_GAP_US,
            "fallback_used": True,
            "fallback_reason": fallback_reason or "recommended_inter_frame_gap_unavailable",
        }

    fallback_used = bool(runtime_timing.get("timing_fallback_used", False))
    fallback_reason = str(runtime_timing.get("timing_fallback_reason") or "").strip()
    return {
        "runtime_timing": runtime_timing,
        "effective_gap_us": recommended_gap_us,
        "fallback_used": fallback_used,
        "fallback_reason": fallback_reason or ("unspecified" if fallback_used else ""),
    }


def _format_readout_time_ms(runtime_timing: dict[str, Any] | None) -> str:
    """格式化相机读出时间字段，缺失或非数字时显示 "-"。

    用途：
        相机在 ``apply_config`` 后会通过 controller 回传 ``timing_readout_time_s``。
        本函数把秒转毫秒并保留 3 位小数；任何缺失/空串/None 都返回 "-"。
    """
    # 1) ``runtime_timing`` 为空字典或 None → 表示尚未应用过相机配置。
    if not runtime_timing:
        return "-"
    value_s = runtime_timing.get("timing_readout_time_s")
    # 2) 字段缺失或空字符串视为未知；保持显示与上一行一致。
    if value_s in (None, ""):
        return "-"
    # 3) 秒转毫秒并保留 3 位小数，便于在主界面看到亚毫秒差异。
    try:
        numeric = float(value_s)
    except (TypeError, ValueError, OverflowError):
        return "-"
    if not math.isfinite(numeric) or numeric < 0:
        return "-"
    return f"{numeric * 1000.0:.3f} ms"


def _format_runtime_seconds_ms(runtime_timing: dict[str, Any] | None, field_name: str) -> str:
    """把 runtime timing 的秒字段格式化成毫秒，非法值显示未知。"""
    if not runtime_timing:
        return "-"
    value = runtime_timing.get(field_name)
    if value is None or isinstance(value, bool):
        return "-"
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return "-"
    if not math.isfinite(numeric) or numeric < 0:
        return "-"
    return f"{numeric * 1000.0:.3f} ms"


def _format_runtime_microseconds_ms(runtime_timing: dict[str, Any] | None, field_name: str) -> str:
    """把 runtime timing 的微秒字段格式化成毫秒，非法值显示未知。"""
    if not runtime_timing:
        return "-"
    value_us = _nonnegative_microseconds(runtime_timing.get(field_name))
    if value_us is None:
        return "-"
    return f"{value_us / 1000.0:.3f} ms"


def _summary_inter_frame_gap_us(
    sim_config: AppConfig,
    runtime_timing: dict[str, Any] | None,
) -> int:
    """生成摘要展示用的实际帧间隔（微秒）。

    规则：
        - runtime 相机配置签名不匹配或推荐值非法 → 回退 50 ms。
        - 签名匹配且推荐值合法 → 使用完整推荐值，不以 50 ms 为上限。
    """
    return int(_runtime_timing_state(sim_config, runtime_timing)["effective_gap_us"])


def _duration_us_to_samples(microseconds: int, sample_rate_hz: int) -> int:
    """按 DAQ 波形 builder 的规则把微秒时长折算成采样点数。

    注意：
        必须用 ``math.ceil``，与 ``waveform.NIDaqWaveformBuilder._us_to_samples`` 完全一致；
        否则总时长估算与真实播放时长出现亚毫秒级偏差。
    """
    # ``max(1, ...)`` 保护极短时长被向下取整为 0 的情况；至少 1 采样点。
    return max(1, int(math.ceil((int(microseconds) / 1_000_000.0) * int(sample_rate_hz))))


def _format_sim9_estimated_total_time_ms(
    sim_config: AppConfig,
    runtime_timing: dict[str, Any] | None,
) -> str:
    """估算从发起 SIM9 采集指令到 9 帧 stack 可读出的总时长（毫秒）。

    公式：
        sample_count = 2 × guard + 9 × (exposure + gap)
        duration_ms  = sample_count / sample_rate × 1000 + 10 ms（stack 整理开销）

    这是给用户的预估时间，与真实硬件运行存在硬件抖动、相机准备等不可建模因素差。
    """
    # 1) 取得当前 timing 子配置；不存在或异常都会被上层捕获。
    timing = sim_config.timing
    # 2) 采样率夹到 ≥1，避免后续 ``/ sample_rate_hz`` 触发 ZeroDivisionError。
    sample_rate_hz = max(1, int(timing.sample_rate_hz))
    # 3) 三类时长（曝光 / 帧间隔 / 保护时间）都折算成同一采样率下的样本点数。
    exposure_samples = _duration_us_to_samples(int(sim_config.camera.exposure_us), sample_rate_hz)
    gap_samples = _duration_us_to_samples(_summary_inter_frame_gap_us(sim_config, runtime_timing), sample_rate_hz)
    guard_samples = _duration_us_to_samples(int(timing.slm_enable_guard_us), sample_rate_hz)
    # 4) 一次完整 SIM9 波形 = 头/尾 guard + 9 × (曝光 + 帧间隔)。
    sample_count = (guard_samples * 2) + (SIM9_FRAME_COUNT * (exposure_samples + gap_samples))
    # 5) 转毫秒后再加固定 10 ms stack 整理开销，得到面向用户的估算总时长。
    duration_ms = (sample_count / float(sample_rate_hz) * 1000.0) + (SIM9_STACK_TRANSFER_OVERHEAD_US / 1000.0)
    return f"{duration_ms:.3f} ms"


def _format_z_scan_estimated_times_ms(
    sim_config: AppConfig,
    records: Sequence[ZScanTimingEntry] | None = None,
) -> tuple[str, str]:
    """估算前置 Z-Scan 的仅位移与位移+采图测试口径总用时（毫秒）。"""
    timing_records = load_z_scan_timing_records() if records is None else records
    move_only_ms = estimate_z_scan_move_only_total_time_ms(
        sim_config.z_scan,
        records=timing_records,
    )
    capture_ms = estimate_z_scan_capture_test_total_time_ms(
        sim_config.z_scan,
        daq_config=sim_config.daq,
        timing=sim_config.timing,
        records=timing_records,
    )
    return f"{move_only_ms:.3f}", f"{capture_ms:.3f}"


def build_sim_settings_summary(
    sim_config: AppConfig,
    runtime_timing: dict[str, Any] | None = None,
    z_scan_timing_records: Sequence[ZScanTimingEntry] | None = None,
) -> str:
    """把当前 SIM 配置和运行时相机 timing 信息格式化为主界面摘要文本。

    返回：
        多行字符串，行间以 ``\\n`` 分隔；包含主 GUI 其他模块没有直接显示的
        正式 RO、运行时/派生耗时、Z-Scan 派生值与内部 Timing。

    维护要点：
        - 显示顺序与 ``tests/test_sim_summary.py`` 中的断言耦合。
        - SLM 未连接时把 ``Pattern RO`` 显示为「(SLM 未连接)」，提示用户先连接。
    """
    # 1) 解构常用子配置，避免后续 f-string 中重复 ``sim_config.xxx``。
    timing = sim_config.timing
    # 2) Running Order 选中名为空（SLM 未连接）时显示提示文本，提醒用户先连接 SLM。
    selected_ro = sim_config.selected_running_order or "(SLM 未连接)"
    # 3) 只拼装主 GUI 其他模块未直接显示的信息，避免右侧摘要与一级模块重复。
    timing_state = _runtime_timing_state(sim_config, runtime_timing)
    valid_runtime_timing = timing_state["runtime_timing"]
    effective_gap_us = int(timing_state["effective_gap_us"])
    fallback_reason = str(timing_state["fallback_reason"] or "-")
    summary_lines = [
        f"Pattern RO: {selected_ro}",
        f"TIMING_READOUTTIME: {_format_readout_time_ms(valid_runtime_timing)}",
        "TIMING_CYCLICTRIGGERPERIOD: "
        f"{_format_runtime_seconds_ms(valid_runtime_timing, 'timing_cyclic_trigger_period_s')}",
        "TIMING_MINTRIGGERBLANKING: "
        f"{_format_runtime_seconds_ms(valid_runtime_timing, 'timing_min_trigger_blanking_s')}",
        "TIMING_THEORETICAL_MIN_INTER_FRAME_GAP: "
        f"{_format_runtime_microseconds_ms(valid_runtime_timing, 'theoretical_min_inter_frame_gap_us')}",
        "TIMING_INTER_FRAME_GAP_SAFETY_MARGIN: "
        f"{_format_runtime_microseconds_ms(valid_runtime_timing, 'inter_frame_gap_safety_margin_us')}",
        f"TIMING_EFFECTIVE_INTER_FRAME_GAP: {effective_gap_us / 1000.0:.3f} ms",
        f"TIMING_FALLBACK_USED: {'yes' if timing_state['fallback_used'] else 'no'}",
        f"TIMING_FALLBACK_REASON: {fallback_reason}",
        f"SIM9_ESTIMATED_TOTAL_TIME: {_format_sim9_estimated_total_time_ms(sim_config, runtime_timing)}",
    ]
    if sim_config.z_scan.enabled:
        start_um = "-" if sim_config.z_scan.start_um is None else f"{float(sim_config.z_scan.start_um):.3f}"
        actual_exposure_us = Z_SCAN_EXPOSURE_PRESETS_US[int(sim_config.z_scan.exposure_preset_ms)]
        scan_moves = int(sim_config.z_scan.num_steps)
        image_layers = scan_moves + 1
        total_distance_um = abs(float(sim_config.z_scan.step_um) * scan_moves)
        estimated_move_only_time_ms, estimated_move_capture_time_ms = _format_z_scan_estimated_times_ms(
            sim_config,
            z_scan_timing_records,
        )
        summary_lines.extend(
            [
                "",
                "Z-Scan:",
                f"  start_um: {start_um}",
                f"  image_layers: {image_layers}",
                f"  total_distance_um: {total_distance_um:.3f}",
                f"  estimated_move_only_time_ms: {estimated_move_only_time_ms}",
                f"  estimated_move_capture_time_ms: {estimated_move_capture_time_ms}",
                f"  actual_exposure_us: {actual_exposure_us}",
            ]
        )
    summary_lines.extend(
        [
        "",
        "Timing:",
        f"  sample_rate_hz: {timing.sample_rate_hz}",
        f"  edge_pulse_us: {timing.edge_pulse_us}",
        f"  inter_frame_gap_us: {effective_gap_us}",
        f"  slm_enable_guard_us: {timing.slm_enable_guard_us}",
        ]
    )
    # 5) 用换行符拼成最终摘要文本，主界面直接 ``setPlainText`` 即可显示。
    return "\n".join(summary_lines)

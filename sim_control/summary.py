"""主界面 SIM 设置摘要文本生成器。

作用：
    把 ``AppConfig`` 与运行时相机 timing 信息格式化成一段多行文本，供
    ``control_wangbo/main.py`` 集成主界面以只读方式展示当前激光、Running
    Order、相机选择/曝光/位深/ROI、读出时间、9 帧采集预估总时长、DAQ 各
    TTL 线位以及内部 Timing 字段。摘要的目的是让用户在不打开 SIM 设置弹窗
    的情况下就能"看一眼明白"当前会按什么参数发起一次采集。

协作关系：
    上游：``control_wangbo/main.py``（调用 ``build_sim_settings_summary()``
          刷新只读 ``QTextEdit`` 摘要区域）、``tests/test_sim_summary.py``。
    下游：``models.AppConfig`` 与 ``models.effective_inter_frame_gap_us``。

关键概念：
    - ``runtime_timing``：相机实际下发配置后回报的 dict，可能含
      ``timing_readout_time_s``（实际读出时间，秒）和
      ``recommended_inter_frame_gap_us``（建议帧间隔，微秒）。
    - SIM9 总时长估算：``2 × guard_samples + 9 × (exposure_samples +
      gap_samples)``，再加固定 10 ms stack 整理开销。
    - ``effective_inter_frame_gap_us``：相机推荐值 < 50 ms 才覆盖默认值。

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

from .models import AppConfig, Z_SCAN_EXPOSURE_PRESETS_US, effective_inter_frame_gap_us
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


def _sim_exposure_us_to_ms(exposure_us: int) -> int:
    """把配置中的曝光微秒值转换成摘要里更易读的毫秒整数。

    返回：
        至少 1 ms 的整数；亚毫秒曝光会被四舍五入到 1 ms。
    """
    # 用 ``round`` 再 ``int`` 保证常见整毫秒值（10_000 → 10）准确；
    # ``max(1, ...)`` 保证显示出的值至少为 1 ms，避免主界面显示 0 让用户误解。
    return max(1, int(round(float(exposure_us) / 1000.0)))


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
    return f"{float(value_s) * 1000.0:.3f} ms"


def _summary_inter_frame_gap_us(runtime_timing: dict[str, Any] | None) -> int:
    """生成摘要展示用的实际帧间隔（微秒）。

    规则：
        - 没有 runtime_timing 或字段缺失 → 默认 50 ms（来自 ``effective_inter_frame_gap_us``）。
        - 推荐值 < 50 ms → 使用推荐值。
        - 推荐值 ≥ 50 ms → 仍使用默认 50 ms。
    """
    # 1) 把"运行时尚未收到相机回报"的情况收敛到 None。
    recommended_gap_us = None
    if runtime_timing:
        recommended_gap_us = runtime_timing.get("recommended_inter_frame_gap_us")
    # 2) 委托给 ``models.effective_inter_frame_gap_us`` 做策略判定，
    #    保证 GUI 与 controller 使用同一规则。
    return effective_inter_frame_gap_us(recommended_gap_us)


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
    gap_samples = _duration_us_to_samples(_summary_inter_frame_gap_us(runtime_timing), sample_rate_hz)
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
        多行字符串，行间以 ``\\n`` 分隔；上半段是用户可改字段（激光/RO/相机/曝光等），
        中段是 DAQ 各 TTL 线，末段是只读 Timing。

    维护要点：
        - 显示顺序与 ``tests/test_sim_summary.py`` 中的断言耦合。
        - SLM 未连接时把 ``Pattern RO`` 显示为「(SLM 未连接)」，提示用户先连接。
    """
    # 1) 解构常用子配置，避免后续 f-string 中重复 ``sim_config.xxx``。
    daq = sim_config.daq
    camera = sim_config.camera
    timing = sim_config.timing
    # 2) 相机名优先用 ``device_label``（人可读）；若空则用 "#index" 作为兜底。
    selected_device = camera.device_label or f"#{camera.device_index}"
    # 3) Running Order 选中名为空（SLM 未连接）时显示提示文本，提醒用户先连接 SLM。
    selected_ro = sim_config.selected_running_order or "(SLM 未连接)"
    # 4) 按"用户视图 → DAQ 线位 → Timing"三段顺序拼装摘要行，再 join 成最终字符串。
    summary_lines = [
        f"Laser: {sim_config.selected_laser_nm} nm",
        f"Pattern RO: {selected_ro}",
        f"Selected SIM Camera: {selected_device}",
        f"Exposure: {_sim_exposure_us_to_ms(camera.exposure_us)} ms",
        f"Bit Depth: {int(camera.bit_depth)}-bit",
        f"ROI: x={camera.roi_x}, y={camera.roi_y}, w={camera.roi_width}, h={camera.roi_height}",
        f"TIMING_READOUTTIME: {_format_readout_time_ms(runtime_timing)}",
        f"SIM9_ESTIMATED_TOTAL_TIME: {_format_sim9_estimated_total_time_ms(sim_config, runtime_timing)}",
    ]
    if sim_config.z_scan.enabled:
        start_um = "-" if sim_config.z_scan.start_um is None else f"{float(sim_config.z_scan.start_um):.3f}"
        actual_exposure_us = Z_SCAN_EXPOSURE_PRESETS_US[int(sim_config.z_scan.exposure_preset_ms)]
        scan_moves = int(sim_config.z_scan.num_steps)
        image_layers = scan_moves + 1
        scan_gap_nm = float(sim_config.z_scan.step_um) * 1000.0
        total_distance_um = abs(float(sim_config.z_scan.step_um) * scan_moves)
        estimated_move_only_time_ms, estimated_move_capture_time_ms = _format_z_scan_estimated_times_ms(
            sim_config,
            z_scan_timing_records,
        )
        summary_lines.extend(
            [
                "",
                "Z-Scan:",
                f"  enabled: {sim_config.z_scan.enabled}",
                f"  start_um: {start_um}",
                f"  direction: {sim_config.z_scan.direction}",
                f"  scan_gap_nm: {scan_gap_nm:.0f}",
                f"  scan_moves: {scan_moves}",
                f"  image_layers: {image_layers}",
                f"  total_distance_um: {total_distance_um:.3f}",
                f"  estimated_move_only_time_ms: {estimated_move_only_time_ms}",
                f"  estimated_move_capture_time_ms: {estimated_move_capture_time_ms}",
                f"  exposure_preset_ms: {int(sim_config.z_scan.exposure_preset_ms)}",
                f"  actual_exposure_us: {actual_exposure_us}",
            ]
        )
    summary_lines.extend(
        [
        "",
        "DAQ:",
        f"  device_name: {daq.device_name}",
        f"  slm_enable_line: {daq.slm_enable_line}",
        f"  slm_trigger_line: {daq.slm_trigger_line}",
        f"  slm_finish_line: {daq.slm_finish_line}",
        f"  cam_trigger_line: {daq.camera_trigger_line}",
        f"  laser_405_line: {daq.laser_405_line}",
        f"  laser_488_line: {daq.laser_488_line}",
        f"  laser_561_line: {daq.laser_561_line}",
        f"  laser_638_line: {daq.laser_638_line}",
        "",
        "Timing:",
        f"  sample_rate_hz: {timing.sample_rate_hz}",
        f"  edge_pulse_us: {timing.edge_pulse_us}",
        f"  inter_frame_gap_us: {_summary_inter_frame_gap_us(runtime_timing)}",
        f"  slm_enable_guard_us: {timing.slm_enable_guard_us}",
        ]
    )
    # 5) 用换行符拼成最终摘要文本，主界面直接 ``setPlainText`` 即可显示。
    return "\n".join(summary_lines)

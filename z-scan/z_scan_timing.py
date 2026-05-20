"""Nikon Ti2 ZDrive 步进定时测量工具（独立脚本，不属于 SIM 主流程）。

作用：
    本脚本用于在已安装 Nikon Ti2 SDK 的机器上测量 ZDrive 在不同步长 / 速度 / 容忍度
    设定下完成一次 ``MIC_DataSet`` 调用所需的时间。它把每次步进的 SDK 返回时长、
    曝光就绪时长、整个 step cycle 时长都记录到 CSV 与 JSONL 文件中，供后期分析。

    脚本支持两种模式：
        - **Dry run**（默认）：连接 SDK、读取 Z 范围、打印 10 个 target 位置后退出，
          **不**真正移动 ZDrive。便于先确认 target 都在样品/物镜的安全范围内。
        - **Hardware run**（``--execute``）：按 target 序列执行 N 次 run × N 步移动。
          每步收集时长记录并写盘；遇到 ``MIC_DataSet`` 失败时把已收集的记录写盘后停止。

协作关系：
    上游：命令行入口 ``z-scan/z_scan_timing.py [--execute] [--start-um ...]``。
    下游：``z-scan/ti2_sdk.py``（ctypes 包装 Nikon Ti2 SDK DLL）。
    输出：``z-scan/results/z_scan_timing_<timestamp>.csv`` 与 ``.jsonl``。

关键概念：
    - ``ZRange``：``ti2_sdk.py`` 暴露的 dataclass，含 physical/logical 上下界（μm）。
    - ``DEFAULT_SPEED=1``：Nikon "2.50 mm/s" Z 速度预设；``DEFAULT_TOLERANCE=0``：最严格容忍度。
    - 默认步长 0.3 μm × 10 步：覆盖小范围 SIM 焦面扫描场景。

维护要点：
    - 与 SIM 采集主流程完全解耦：本脚本不 import ``sim_control``，且单独运行。
    - 任何代码修改都必须保留"未传 ``--execute`` 时绝不移动 ZDrive"的安全语义。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Iterable

# 脚本所在目录；把它放到 sys.path[0] 让 ``import ti2_sdk`` 走脚本同目录而非全局。
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from ti2_sdk import LX_OK, Ti2SdkError, Ti2Stage, ZRange


# 默认步长 0.3 μm × 10 步：覆盖小范围 SIM 焦面扫描；保守起点 50 μm。
DEFAULT_STEP_UM = 0.3
DEFAULT_STEPS = 10
DEFAULT_START_UM = 50.0
# Nikon 速度预设 1 = 2.50 mm/s；最严格容忍度 0。
DEFAULT_SPEED = 1
DEFAULT_TOLERANCE = 0


def build_targets(start_um: float, step_um: float, steps: int, direction: str) -> list[float]:
    """根据起始位置 + 步长 + 步数 + 方向，生成 ``steps`` 个 target 位置（μm）。

    抛出：
        ``ValueError``：``steps``/``step_um`` 非正，或 ``direction`` 不在合法集合中。
    """
    # 1) 三类输入校验：步数/步长必须为正，方向必须在合法集合内。
    if steps <= 0:
        raise ValueError("steps must be greater than zero")
    if step_um <= 0:
        raise ValueError("step_um must be greater than zero")
    if direction not in {"increasing", "decreasing"}:
        raise ValueError("direction must be 'increasing' or 'decreasing'")
    # 2) 方向 → 符号；列表推导生成 ``[start + sign*step*1, ..., start + sign*step*steps]``。
    sign = 1.0 if direction == "increasing" else -1.0
    return [round(start_um + sign * step_um * (index + 1), 6) for index in range(steps)]


def summarize_records(records: Iterable[dict[str, object]]) -> dict[str, float | int]:
    """对一批 step 记录的 ``sdk_call_ms`` 字段做 min/median/p95/max 统计。"""
    # 1) 提取所有 sdk_call_ms 数值；空列表直接返回 count=0。
    values = [float(record["sdk_call_ms"]) for record in records]
    if not values:
        return {"count": 0}
    # 2) 排序后从中提取 min/median/p95/max。
    sorted_values = sorted(values)
    return {
        "count": len(sorted_values),
        "min_ms": sorted_values[0],
        "median_ms": statistics.median(sorted_values),
        "p95_ms": percentile(sorted_values, 95),
        "max_ms": sorted_values[-1],
    }


def percentile(sorted_values: list[float], percentile_value: float) -> float:
    """对已排序列表做线性插值百分位计算。"""
    # 单元素列表直接返回该值，避免后续除零。
    if len(sorted_values) == 1:
        return sorted_values[0]
    # 标准百分位公式：``(n-1) * p/100`` 的整数与小数部分分别决定相邻两点的权重。
    position = (len(sorted_values) - 1) * (percentile_value / 100.0)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[int(position)]
    weight = position - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def validate_targets(targets: list[float], z_range: ZRange) -> None:
    """检查所有 target 都在 Z 物理范围内；越界抛 ValueError 列出原始值。"""
    # 分别筛出"低于下限"与"高于上限"两组越界值，便于错误信息一次性列全。
    below = [value for value in targets if value < z_range.lower_um]
    above = [value for value in targets if value > z_range.upper_um]
    if below or above:
        raise ValueError(
            "Generated target positions exceed Z range "
            f"{z_range.lower_um:.6f}..{z_range.upper_um:.6f} um: {targets}"
        )


def raise_if_move_failed(retcode: int, context: str) -> None:
    """SDK 返回非 LX_OK 时把 retcode 包成 ``Ti2SdkError`` 抛出，附上 context。"""
    if retcode != LX_OK:
        raise Ti2SdkError(f"{context} failed (retcode={retcode})", retcode=retcode)


def base_move_record(
    *,
    run_index: int,
    move_index: int,
    start_um: float,
    target_um: float,
    final_um: float,
    sdk_reported_final_um: float,
    sdk_call_ms: float,
    command_to_exposure_ready_ms: float,
    step_cycle_ms: float,
    sdk_retcode: int,
    args: argparse.Namespace,
    exposure_triggered: bool,
    error: str = "",
) -> dict[str, object]:
    """构造一条 step 移动记录字典，统一时间精度（μm 6 位、ms 3 位）。

    用途：
        ``run_scan`` 在每一步移动完成后用本函数生成一条记录；后续被 ``write_outputs``
        写入 CSV/JSONL。统一在这里 round 让磁盘文件干净。
    """
    return {
        "timestamp": datetime.now().isoformat(timespec="milliseconds"),
        "run_index": run_index,
        "move_index": move_index,
        "start_um": round(start_um, 6),
        "target_um": round(target_um, 6),
        "final_um": round(final_um, 6),
        "sdk_reported_final_um": round(sdk_reported_final_um, 6),
        "sdk_call_ms": round(sdk_call_ms, 3),
        "command_to_exposure_ready_ms": round(command_to_exposure_ready_ms, 3),
        "step_cycle_ms": round(step_cycle_ms, 3),
        "exposure_triggered": exposure_triggered,
        "sdk_retcode": sdk_retcode,
        "speed": args.speed,
        "tolerance": args.tolerance,
        "error": error,
    }


def run_scan(stage: Ti2Stage, args: argparse.Namespace, z_range: ZRange, output_prefix: Path) -> list[dict[str, object]]:
    """执行 ``args.runs`` 次 × ``args.steps`` 步的 Z 扫描，返回所有 step 记录。

    流程：
        1. 生成并校验 target 列表；
        2. 先移到 start_um；
        3. 每个 run 内部逐步移到 targets[i]，记录 SDK 调用时长与 step cycle 间距；
        4. 每步记录都立即 ``write_outputs``，避免崩溃丢失部分记录；
        5. 末尾按需 ``--return-to-start`` 回到起点。

    抛出：
        ``Ti2SdkError``：任一步 ``MIC_DataSet`` 失败时；失败前已收集的记录会被写盘。
    """
    records: list[dict[str, object]] = []
    # 1) 生成 + 校验 target，确保不会越出 Z 物理范围。
    targets = build_targets(args.start_um, args.step_um, args.steps, args.direction)
    validate_targets(targets, z_range)

    # 2) 先移到 start_um；这次移动不计入 timing 记录，但失败仍要立即抛错。
    print(f"Moving to start position {args.start_um:.6f} um")
    start_move = stage.move_z_um(args.start_um, speed=args.speed, tolerance=args.tolerance)
    raise_if_move_failed(start_move.retcode, "Initial move to start position")

    for run_index in range(1, args.runs + 1):
        previous_record: dict[str, object] | None = None
        previous_command_start_s: float | None = None
        planned_start_um = args.start_um
        for move_index, target_um in enumerate(targets, start=1):
            command_start_s = time.perf_counter()
            if previous_record is not None and previous_command_start_s is not None:
                previous_record["step_cycle_ms"] = round((command_start_s - previous_command_start_s) * 1000.0, 3)
            move_result = stage.move_z_um(target_um, speed=args.speed, tolerance=args.tolerance)
            sdk_return_s = time.perf_counter()
            sdk_call_ms = (sdk_return_s - command_start_s) * 1000.0
            if move_result.retcode != LX_OK:
                record = base_move_record(
                    run_index=run_index,
                    move_index=move_index,
                    start_um=planned_start_um,
                    target_um=target_um,
                    final_um=move_result.final_um,
                    sdk_reported_final_um=move_result.final_um,
                    sdk_call_ms=sdk_call_ms,
                    command_to_exposure_ready_ms=math.nan,
                    step_cycle_ms=(sdk_return_s - command_start_s) * 1000.0,
                    sdk_retcode=move_result.retcode,
                    args=args,
                    exposure_triggered=False,
                    error=f"MIC_DataSet failed (retcode={move_result.retcode})",
                )
                records.append(record)
                write_outputs(records, output_prefix)
                raise Ti2SdkError(
                    f"MIC_DataSet failed for run {run_index} step {move_index} "
                    f"target {target_um:.6f} um (retcode={move_result.retcode})",
                    retcode=move_result.retcode,
                )
            record = base_move_record(
                run_index=run_index,
                move_index=move_index,
                start_um=planned_start_um,
                target_um=target_um,
                final_um=move_result.final_um,
                sdk_reported_final_um=move_result.final_um,
                sdk_call_ms=sdk_call_ms,
                command_to_exposure_ready_ms=sdk_call_ms,
                step_cycle_ms=sdk_call_ms,
                sdk_retcode=move_result.retcode,
                args=args,
                exposure_triggered=True,
            )
            print(
                f"run {run_index} step {move_index}: "
                f"target={target_um:.6f} um final={move_result.final_um:.6f} um "
                f"sdk={sdk_call_ms:.3f} ms exposure_ready={sdk_call_ms:.3f} ms"
            )
            records.append(record)
            write_outputs(records, output_prefix)
            previous_record = record
            previous_command_start_s = command_start_s
            planned_start_um = target_um

        if run_index < args.runs:
            reset_move = stage.move_z_um(args.start_um, speed=args.speed, tolerance=args.tolerance)
            raise_if_move_failed(reset_move.retcode, "Inter-run move to start position")

    if args.return_to_start:
        print(f"Returning to start position {args.start_um:.6f} um")
        return_move = stage.move_z_um(args.start_um, speed=args.speed, tolerance=args.tolerance)
        raise_if_move_failed(return_move.retcode, "Return-to-start move")
    return records


def write_outputs(records: list[dict[str, object]], output_prefix: Path) -> None:
    """把 records 写为同前缀的 CSV 和 JSONL 两份文件。

    CSV 适合在 Excel 打开做散点图；JSONL 适合 Python/Pandas 加载做更细分析。
    """
    # 1) 确保输出目录存在；脚本支持自动创建 ``z-scan/results/``。
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    csv_path = output_prefix.with_suffix(".csv")
    jsonl_path = output_prefix.with_suffix(".jsonl")
    # 2) CSV 字段名由首条记录决定；空 records 时写空 header 文件即可。
    fieldnames = list(records[0].keys()) if records else []
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)
    # 3) JSONL：每行一个 record，便于流式处理。``ensure_ascii=False`` 保留中文。
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def print_summary(records: list[dict[str, object]]) -> None:
    """把 step 记录的统计摘要打印到控制台，方便实验现场即时查看。"""
    summary = summarize_records(records)
    if summary["count"] == 0:
        print("No movement records collected.")
        return
    print("\nSummary for sdk_call_ms / command_to_exposure_ready_ms:")
    print(
        "count={count} min={min_ms:.3f} ms median={median_ms:.3f} ms "
        "p95={p95_ms:.3f} ms max={max_ms:.3f} ms".format(**summary)
    )


def resolve_start_um(args: argparse.Namespace, z_range: ZRange) -> float:
    """决定本次扫描的 ``start_um``：用户提供则用，否则回落到 ``DEFAULT_START_UM``。

    抛出：
        ``ValueError``：Z 范围读出异常（lower >= upper / 非有限值），或 start 超出范围。
    """
    # 1) Z 范围异常 → 立刻抛错，避免后续移动到非法位置。
    if not math.isfinite(z_range.lower_um) or z_range.lower_um >= z_range.upper_um:
        raise ValueError(f"Invalid Z lower range from SDK: {asdict(z_range)}")
    # 2) 默认 50 μm；用户显式提供则用，但越界仍拒绝。
    start_um = args.start_um if args.start_um is not None else DEFAULT_START_UM
    if start_um < z_range.lower_um or start_um > z_range.upper_um:
        raise ValueError(
            f"Start position {start_um:.6f} um is outside Z range "
            f"{z_range.lower_um:.6f}..{z_range.upper_um:.6f} um"
        )
    return start_um


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数；最重要的是 ``--execute`` 开关，没有它脚本只做 dry run。"""
    parser = argparse.ArgumentParser(description="Measure Nikon Ti2 ZDrive step timing.")
    parser.add_argument("--execute", action="store_true", help="Actually move the ZDrive. Without this, only dry-run.")
    parser.add_argument("--dll", default=None, help="Path to Ti2_Mic_Driver.dll.")
    parser.add_argument("--device-index", type=int, default=0, help="Device index for MIC_Open. Default uses first valid device.")
    parser.add_argument("--simulator", action="store_true", help="Use MIC_SimulatorOpen instead of real hardware.")
    parser.add_argument("--start-um", type=float, default=None, help=f"Explicit start position in um. Default is {DEFAULT_START_UM:.1f} um.")
    parser.add_argument("--direction", choices=["increasing", "decreasing"], default="increasing")
    parser.add_argument("--step-um", type=float, default=DEFAULT_STEP_UM)
    parser.add_argument("--steps", type=int, default=DEFAULT_STEPS)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--speed", type=int, default=DEFAULT_SPEED, help="iZPOSITIONSpeed, default 1 = 2.50 mm/s.")
    parser.add_argument("--tolerance", type=int, default=DEFAULT_TOLERANCE, help="iZPOSITIONTolerance, default 0.")
    parser.add_argument("--return-to-start", action="store_true")
    parser.add_argument("--output-dir", default=str(SCRIPT_DIR / "results"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """命令行入口：dry-run 默认 / ``--execute`` 触发真实硬件移动。

    返回：
        进程退出码：0 表示成功；2 表示 SDK / ValueError 异常。
    """
    args = parse_args(argv)
    # 输出前缀使用启动时间戳，避免覆盖之前的结果。
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_prefix = Path(args.output_dir) / f"z_scan_timing_{timestamp}"
    try:
        stage = Ti2Stage(args.dll)
        devices = stage.get_device_list()
        stage.open(device_index=args.device_index, simulator=args.simulator)
        try:
            ranges = stage.get_z_ranges_um()
            z_range = ranges["physical"]
            args.start_um = resolve_start_um(args, z_range)
            targets = build_targets(args.start_um, args.step_um, args.steps, args.direction)
            validate_targets(targets, z_range)
            print(f"Devices: {devices}")
            print(f"Z physical range: {z_range.lower_um:.6f}..{z_range.upper_um:.6f} um")
            print(f"Z logical range: {ranges['logical'].lower_um:.6f}..{ranges['logical'].upper_um:.6f} um")
            print(f"Start: {args.start_um:.6f} um")
            print("Targets:")
            for index, target in enumerate(targets, start=1):
                print(f"  {index:02d}: {target:.6f} um")
            if not args.execute:
                print("\nDry run only. Re-run with --execute to move the ZDrive.")
                return 0
            records = run_scan(stage, args, z_range, output_prefix)
            write_outputs(records, output_prefix)
            print(f"\nCSV:   {output_prefix.with_suffix('.csv')}")
            print(f"JSONL: {output_prefix.with_suffix('.jsonl')}")
            print_summary(records)
            return 0
        finally:
            stage.close()
    except (Ti2SdkError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Persistent timing history and ETA helpers for Z-scan tests."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
import math
import statistics
from pathlib import Path
from typing import Sequence

from .models import DaqLineConfig, TimingConfig, ZScanConfig
from .waveform import NIDaqWaveformBuilder


APP_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_Z_SCAN_TIMING_HISTORY_PATH = APP_ROOT / "data" / "z_scan_timing_history.jsonl"
DEFAULT_Z_SCAN_MOVE_MS = 25.0
MIN_Z_SCAN_MOVE_MS = 1.0


@dataclass(frozen=True)
class ZScanTimingRecord:
    record_type: str = field(default="step", init=False)
    timestamp: str
    mode: str
    scan_gap_nm: float
    num_steps: int
    exposure_preset_ms: int
    direction: str
    move_index: int
    total_moves: int
    from_z_um: float
    to_z_um: float
    distance_um: float
    move_ms: float
    cycle_ms: float | None = None
    success: bool = True

    @classmethod
    def for_test(
        cls,
        *,
        scan_gap_nm: float,
        move_ms: float,
        cycle_ms: float | None = None,
        exposure_preset_ms: int = 8,
        num_steps: int = 1,
    ) -> "ZScanTimingRecord":
        return cls(
            timestamp="2026-05-25T00:00:00",
            mode="test",
            scan_gap_nm=float(scan_gap_nm),
            num_steps=int(num_steps),
            exposure_preset_ms=int(exposure_preset_ms),
            direction="positive_z",
            move_index=1,
            total_moves=1,
            from_z_um=0.0,
            to_z_um=float(scan_gap_nm) / 1000.0,
            distance_um=abs(float(scan_gap_nm) / 1000.0),
            move_ms=float(move_ms),
            cycle_ms=None if cycle_ms is None else float(cycle_ms),
            success=True,
        )

    @classmethod
    def from_dict(cls, payload: dict) -> "ZScanTimingRecord":
        cycle_value = payload.get("cycle_ms", None)
        return cls(
            timestamp=str(payload.get("timestamp", "")),
            mode=str(payload.get("mode", "")),
            scan_gap_nm=float(payload.get("scan_gap_nm", 0.0)),
            num_steps=int(payload.get("num_steps", 0)),
            exposure_preset_ms=int(payload.get("exposure_preset_ms", 0)),
            direction=str(payload.get("direction", "")),
            move_index=int(payload.get("move_index", 0)),
            total_moves=int(payload.get("total_moves", 0)),
            from_z_um=float(payload.get("from_z_um", 0.0)),
            to_z_um=float(payload.get("to_z_um", 0.0)),
            distance_um=abs(float(payload.get("distance_um", 0.0))),
            move_ms=float(payload.get("move_ms", 0.0)),
            cycle_ms=None if cycle_value is None else float(cycle_value),
            success=bool(payload.get("success", True)),
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ZScanTimingRunRecord:
    record_type: str = field(default="run", init=False)
    timestamp: str
    mode: str
    scan_gap_nm: float
    num_steps: int
    image_layers: int
    exposure_preset_ms: int
    direction: str
    total_duration_ms: float
    initial_position_ms: float
    scan_move_ms_sum: float
    scan_move_ms_mean: float
    scan_move_count: int
    restore_ms: float
    fixed_overhead_ms: float
    capture_nonmove_ms_sum: float = 0.0
    capture_nonmove_ms_mean: float | None = None
    best_focus_move_ms: float = 0.0
    tiff_write_ms: float = 0.0
    success: bool = True

    @classmethod
    def for_test(
        cls,
        *,
        mode: str,
        scan_gap_nm: float,
        num_steps: int,
        total_duration_ms: float,
        initial_position_ms: float = 0.0,
        scan_move_ms_sum: float = 0.0,
        scan_move_ms_mean: float = 0.0,
        scan_move_count: int | None = None,
        restore_ms: float = 0.0,
        fixed_overhead_ms: float = 0.0,
        exposure_preset_ms: int = 8,
        direction: str = "positive_z",
        capture_nonmove_ms_sum: float = 0.0,
        capture_nonmove_ms_mean: float | None = None,
        best_focus_move_ms: float = 0.0,
        tiff_write_ms: float = 0.0,
        success: bool = True,
    ) -> "ZScanTimingRunRecord":
        move_count = int(num_steps) if scan_move_count is None else int(scan_move_count)
        return cls(
            timestamp="2026-05-25T00:00:00",
            mode=str(mode),
            scan_gap_nm=float(scan_gap_nm),
            num_steps=int(num_steps),
            image_layers=int(num_steps) + 1,
            exposure_preset_ms=int(exposure_preset_ms),
            direction=str(direction),
            total_duration_ms=float(total_duration_ms),
            initial_position_ms=float(initial_position_ms),
            scan_move_ms_sum=float(scan_move_ms_sum),
            scan_move_ms_mean=float(scan_move_ms_mean),
            scan_move_count=move_count,
            restore_ms=float(restore_ms),
            fixed_overhead_ms=float(fixed_overhead_ms),
            capture_nonmove_ms_sum=float(capture_nonmove_ms_sum),
            capture_nonmove_ms_mean=(
                None if capture_nonmove_ms_mean is None else float(capture_nonmove_ms_mean)
            ),
            best_focus_move_ms=float(best_focus_move_ms),
            tiff_write_ms=float(tiff_write_ms),
            success=bool(success),
        )

    @classmethod
    def from_dict(cls, payload: dict) -> "ZScanTimingRunRecord":
        capture_mean = payload.get("capture_nonmove_ms_mean", None)
        return cls(
            timestamp=str(payload.get("timestamp", "")),
            mode=str(payload.get("mode", "")),
            scan_gap_nm=float(payload.get("scan_gap_nm", 0.0)),
            num_steps=int(payload.get("num_steps", 0)),
            image_layers=int(payload.get("image_layers", int(payload.get("num_steps", 0)) + 1)),
            exposure_preset_ms=int(payload.get("exposure_preset_ms", 0)),
            direction=str(payload.get("direction", "")),
            total_duration_ms=float(payload.get("total_duration_ms", 0.0)),
            initial_position_ms=float(payload.get("initial_position_ms", 0.0)),
            scan_move_ms_sum=float(payload.get("scan_move_ms_sum", 0.0)),
            scan_move_ms_mean=float(payload.get("scan_move_ms_mean", 0.0)),
            scan_move_count=int(payload.get("scan_move_count", 0)),
            restore_ms=float(payload.get("restore_ms", 0.0)),
            fixed_overhead_ms=float(payload.get("fixed_overhead_ms", 0.0)),
            capture_nonmove_ms_sum=float(payload.get("capture_nonmove_ms_sum", 0.0)),
            capture_nonmove_ms_mean=None if capture_mean is None else float(capture_mean),
            best_focus_move_ms=float(payload.get("best_focus_move_ms", 0.0)),
            tiff_write_ms=float(payload.get("tiff_write_ms", 0.0)),
            success=bool(payload.get("success", True)),
        )

    def to_dict(self) -> dict:
        return asdict(self)


ZScanTimingEntry = ZScanTimingRecord | ZScanTimingRunRecord


def _now_timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _gap_matches(left_nm: float, right_nm: float) -> bool:
    return math.isclose(float(left_nm), float(right_nm), rel_tol=0.0, abs_tol=1e-6)


def load_z_scan_timing_records(
    path: str | Path = DEFAULT_Z_SCAN_TIMING_HISTORY_PATH,
) -> list[ZScanTimingEntry]:
    history_path = Path(path)
    if not history_path.exists():
        return []

    records: list[ZScanTimingEntry] = []
    with history_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            raw = line.strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
                if str(payload.get("record_type", "step")) == "run":
                    records.append(ZScanTimingRunRecord.from_dict(payload))
                else:
                    records.append(ZScanTimingRecord.from_dict(payload))
            except Exception:
                continue
    return records


def append_z_scan_timing_records(
    records: Sequence[ZScanTimingEntry],
    path: str | Path = DEFAULT_Z_SCAN_TIMING_HISTORY_PATH,
) -> None:
    if not records:
        return
    history_path = Path(path)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record.to_dict(), ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")


def _successful_move_records(records: Sequence[ZScanTimingEntry]) -> list[ZScanTimingRecord]:
    return [
        record
        for record in records
        if isinstance(record, ZScanTimingRecord)
        and bool(record.success)
        and math.isfinite(float(record.move_ms))
        and float(record.move_ms) > 0.0
    ]


def _successful_run_records(
    records: Sequence[ZScanTimingEntry] | None,
    *,
    mode: str | None = None,
    exposure_preset_ms: int | None = None,
) -> list[ZScanTimingRunRecord]:
    result: list[ZScanTimingRunRecord] = []
    for record in records or ():
        if not isinstance(record, ZScanTimingRunRecord) or not bool(record.success):
            continue
        if mode is not None and str(record.mode) != str(mode):
            continue
        if exposure_preset_ms is not None and int(record.exposure_preset_ms) != int(exposure_preset_ms):
            continue
        result.append(record)
    return result


def _median(values: Sequence[float], default: float = 0.0) -> float:
    usable = [float(value) for value in values if math.isfinite(float(value))]
    if not usable:
        return float(default)
    return float(statistics.median(usable))


def _linear_estimate_from_points(points: dict[float, float], x_value: float, default: float) -> float:
    if not points:
        return float(default)
    if len(points) == 1:
        known_x, known_y = next(iter(points.items()))
        if known_x <= 0.0:
            return max(MIN_Z_SCAN_MOVE_MS, float(known_y))
        return max(MIN_Z_SCAN_MOVE_MS, float(known_y) * max(0.0, float(x_value)) / known_x)

    sorted_points = sorted(points.items())
    n = float(len(sorted_points))
    sum_x = sum(x for x, _y in sorted_points)
    sum_y = sum(y for _x, y in sorted_points)
    sum_xx = sum(x * x for x, _y in sorted_points)
    sum_xy = sum(x * y for x, y in sorted_points)
    denominator = (n * sum_xx) - (sum_x * sum_x)
    if math.isclose(denominator, 0.0):
        return max(MIN_Z_SCAN_MOVE_MS, float(statistics.median(points.values())))

    slope = ((n * sum_xy) - (sum_x * sum_y)) / denominator
    intercept = (sum_y - (slope * sum_x)) / n
    return max(MIN_Z_SCAN_MOVE_MS, float(intercept + (slope * float(x_value))))


def _run_median_by_gap(records: Sequence[ZScanTimingRunRecord]) -> dict[float, float]:
    grouped: dict[float, list[float]] = {}
    for record in records:
        if int(record.scan_move_count) <= 0 or float(record.scan_move_ms_mean) <= 0.0:
            continue
        grouped.setdefault(float(record.scan_gap_nm), []).append(float(record.scan_move_ms_mean))
    return {gap_nm: float(statistics.median(values)) for gap_nm, values in grouped.items() if values}


def _estimate_scan_move_ms_from_runs(
    scan_gap_nm: float,
    records: Sequence[ZScanTimingEntry] | None,
) -> float | None:
    run_records = _successful_run_records(records)
    exact_values = [
        float(record.scan_move_ms_mean)
        for record in run_records
        if _gap_matches(record.scan_gap_nm, scan_gap_nm)
        and int(record.scan_move_count) > 0
        and float(record.scan_move_ms_mean) > 0.0
    ]
    if exact_values:
        return max(MIN_Z_SCAN_MOVE_MS, float(statistics.median(exact_values)))

    medians = _run_median_by_gap(run_records)
    if not medians:
        return None
    return _linear_estimate_from_points(medians, scan_gap_nm, DEFAULT_Z_SCAN_MOVE_MS)


def _estimate_total_distance_component_ms(
    attr_name: str,
    z_scan_config: ZScanConfig,
    records: Sequence[ZScanTimingEntry] | None,
    *,
    mode: str | None,
    default_ms: float,
) -> float:
    total_distance_nm = abs(float(z_scan_config.step_um) * 1000.0 * int(z_scan_config.num_steps))
    run_records = _successful_run_records(records, mode=mode)
    exact_values = [
        float(getattr(record, attr_name))
        for record in run_records
        if _gap_matches(float(record.scan_gap_nm) * int(record.num_steps), total_distance_nm)
        and float(getattr(record, attr_name)) > 0.0
    ]
    if exact_values:
        return float(statistics.median(exact_values))

    points: dict[float, list[float]] = {}
    for record in run_records:
        component = float(getattr(record, attr_name))
        if component <= 0.0:
            continue
        points.setdefault(float(record.scan_gap_nm) * int(record.num_steps), []).append(component)
    medians = {distance_nm: float(statistics.median(values)) for distance_nm, values in points.items()}
    return _linear_estimate_from_points(medians, total_distance_nm, default_ms) if medians else float(default_ms)


def _estimate_run_component_ms(
    attr_name: str,
    records: Sequence[ZScanTimingEntry] | None,
    *,
    mode: str | None,
    default_ms: float,
    exposure_preset_ms: int | None = None,
) -> float:
    values = [
        float(getattr(record, attr_name))
        for record in _successful_run_records(records, mode=mode, exposure_preset_ms=exposure_preset_ms)
        if getattr(record, attr_name) is not None and math.isfinite(float(getattr(record, attr_name)))
    ]
    return _median(values, default_ms)


def _median_by_gap(records: Sequence[ZScanTimingRecord]) -> dict[float, float]:
    grouped: dict[float, list[float]] = {}
    for record in _successful_move_records(records):
        grouped.setdefault(float(record.scan_gap_nm), []).append(float(record.move_ms))
    return {gap_nm: float(statistics.median(values)) for gap_nm, values in grouped.items() if values}


def estimate_z_scan_move_ms(
    scan_gap_nm: float,
    records: Sequence[ZScanTimingEntry] | None,
    *,
    default_ms: float = DEFAULT_Z_SCAN_MOVE_MS,
) -> float:
    run_estimate = _estimate_scan_move_ms_from_runs(scan_gap_nm, records)
    if run_estimate is not None:
        return run_estimate

    move_records = [
        record
        for record in _successful_move_records(list(records or ()))
        if float(record.distance_um) > 0.0
    ]
    exact_values = [
        float(record.move_ms) for record in move_records if _gap_matches(record.scan_gap_nm, scan_gap_nm)
    ]
    if exact_values:
        return max(MIN_Z_SCAN_MOVE_MS, float(statistics.median(exact_values)))

    medians = _median_by_gap(move_records)
    if not medians:
        return max(MIN_Z_SCAN_MOVE_MS, float(default_ms))
    if len(medians) == 1:
        known_gap_nm, known_ms = next(iter(medians.items()))
        if known_gap_nm <= 0:
            return max(MIN_Z_SCAN_MOVE_MS, float(known_ms))
        return max(MIN_Z_SCAN_MOVE_MS, float(known_ms) * max(0.0, float(scan_gap_nm)) / known_gap_nm)

    points = sorted(medians.items())
    n = float(len(points))
    sum_x = sum(gap_nm for gap_nm, _move_ms in points)
    sum_y = sum(move_ms for _gap_nm, move_ms in points)
    sum_xx = sum(gap_nm * gap_nm for gap_nm, _move_ms in points)
    sum_xy = sum(gap_nm * move_ms for gap_nm, move_ms in points)
    denominator = (n * sum_xx) - (sum_x * sum_x)
    if math.isclose(denominator, 0.0):
        return max(MIN_Z_SCAN_MOVE_MS, float(statistics.median(medians.values())))

    slope = ((n * sum_xy) - (sum_x * sum_y)) / denominator
    intercept = (sum_y - (slope * sum_x)) / n
    predicted_ms = intercept + (slope * float(scan_gap_nm))
    return max(MIN_Z_SCAN_MOVE_MS, float(predicted_ms))


def _estimate_exact_cycle_ms(
    z_scan_config: ZScanConfig,
    records: Sequence[ZScanTimingEntry] | None,
) -> float | None:
    scan_gap_nm = abs(float(z_scan_config.step_um) * 1000.0)
    exposure_preset_ms = int(z_scan_config.exposure_preset_ms)
    cycle_values = [
        float(record.cycle_ms)
        for record in records or ()
        if isinstance(record, ZScanTimingRecord)
        and bool(record.success)
        and record.cycle_ms is not None
        and float(record.cycle_ms) > 0.0
        and _gap_matches(record.scan_gap_nm, scan_gap_nm)
        and int(record.exposure_preset_ms) == exposure_preset_ms
    ]
    if not cycle_values:
        return None
    return max(MIN_Z_SCAN_MOVE_MS, float(statistics.median(cycle_values)))


def estimate_z_scan_total_time_ms(
    z_scan_config: ZScanConfig,
    *,
    daq_config: DaqLineConfig,
    timing: TimingConfig,
    records: Sequence[ZScanTimingEntry] | None = None,
    waveform_builder: NIDaqWaveformBuilder | None = None,
) -> float:
    if not z_scan_config.enabled:
        return 0.0
    image_layers = int(z_scan_config.num_steps) + 1
    exact_cycle_ms = _estimate_exact_cycle_ms(z_scan_config, records)
    if exact_cycle_ms is not None and image_layers > 0:
        return float(exact_cycle_ms) * image_layers
    return estimate_z_scan_capture_test_total_time_ms(
        z_scan_config,
        daq_config=daq_config,
        timing=timing,
        records=records,
        waveform_builder=waveform_builder,
    )


def estimate_z_scan_move_only_total_time_ms(
    z_scan_config: ZScanConfig,
    *,
    records: Sequence[ZScanTimingEntry] | None = None,
) -> float:
    if not z_scan_config.enabled:
        return 0.0
    move_count = max(0, int(z_scan_config.num_steps))
    scan_gap_nm = abs(float(z_scan_config.step_um) * 1000.0)
    move_ms = estimate_z_scan_move_ms(scan_gap_nm, records)
    initial_ms = _estimate_run_component_ms(
        "initial_position_ms",
        records,
        mode="zscan_stage_only",
        default_ms=0.0,
    )
    restore_ms = _estimate_total_distance_component_ms(
        "restore_ms",
        z_scan_config,
        records,
        mode="zscan_stage_only",
        default_ms=DEFAULT_Z_SCAN_MOVE_MS if move_count > 0 else 0.0,
    )
    fixed_ms = _estimate_run_component_ms(
        "fixed_overhead_ms",
        records,
        mode="zscan_stage_only",
        default_ms=0.0,
    )
    return max(0.0, float(fixed_ms) + float(initial_ms) + (move_ms * move_count) + float(restore_ms))


def _estimate_capture_nonmove_ms(
    z_scan_config: ZScanConfig,
    *,
    daq_config: DaqLineConfig,
    timing: TimingConfig,
    records: Sequence[ZScanTimingEntry] | None,
    waveform_builder: NIDaqWaveformBuilder | None,
) -> float:
    exposure_preset_ms = int(z_scan_config.exposure_preset_ms)
    capture_run_values = [
        float(record.capture_nonmove_ms_mean)
        for record in _successful_run_records(
            records,
            mode="zscan_stage_plus_capture",
            exposure_preset_ms=exposure_preset_ms,
        )
        if record.capture_nonmove_ms_mean is not None and float(record.capture_nonmove_ms_mean) > 0.0
    ]
    if capture_run_values:
        return float(statistics.median(capture_run_values))

    scan_gap_nm = abs(float(z_scan_config.step_um) * 1000.0)
    cycle_nonmove_values = [
        max(0.0, float(record.cycle_ms) - float(record.move_ms))
        for record in _successful_move_records(records or ())
        if record.cycle_ms is not None
        and float(record.cycle_ms) > 0.0
        and _gap_matches(record.scan_gap_nm, scan_gap_nm)
        and int(record.exposure_preset_ms) == exposure_preset_ms
    ]
    if cycle_nonmove_values:
        return float(statistics.median(cycle_nonmove_values))

    builder = waveform_builder or NIDaqWaveformBuilder()
    plan = builder.build_z_scan(
        daq_config=daq_config,
        timing=timing,
        exposure_us=int(z_scan_config.actual_exposure_us),
        include_role_matrix=False,
    )
    return float(plan.duration_s) * 1000.0


def estimate_z_scan_capture_test_total_time_ms(
    z_scan_config: ZScanConfig,
    *,
    daq_config: DaqLineConfig,
    timing: TimingConfig,
    records: Sequence[ZScanTimingEntry] | None = None,
    waveform_builder: NIDaqWaveformBuilder | None = None,
) -> float:
    if not z_scan_config.enabled:
        return 0.0
    image_layers = int(z_scan_config.num_steps) + 1
    if image_layers <= 0:
        return 0.0
    move_count = max(0, int(z_scan_config.num_steps))
    scan_gap_nm = abs(float(z_scan_config.step_um) * 1000.0)
    move_ms = estimate_z_scan_move_ms(scan_gap_nm, records)
    capture_nonmove_ms = _estimate_capture_nonmove_ms(
        z_scan_config,
        daq_config=daq_config,
        timing=timing,
        records=records,
        waveform_builder=waveform_builder,
    )
    exposure_preset_ms = int(z_scan_config.exposure_preset_ms)
    fixed_ms = _estimate_run_component_ms(
        "fixed_overhead_ms",
        records,
        mode="zscan_stage_plus_capture",
        default_ms=0.0,
        exposure_preset_ms=exposure_preset_ms,
    )
    initial_ms = _estimate_run_component_ms(
        "initial_position_ms",
        records,
        mode="zscan_stage_plus_capture",
        default_ms=0.0,
        exposure_preset_ms=exposure_preset_ms,
    )
    restore_ms = _estimate_total_distance_component_ms(
        "restore_ms",
        z_scan_config,
        records,
        mode="zscan_stage_plus_capture",
        default_ms=DEFAULT_Z_SCAN_MOVE_MS if move_count > 0 else 0.0,
    )
    best_focus_move_ms = _estimate_run_component_ms(
        "best_focus_move_ms",
        records,
        mode="zscan_stage_plus_capture",
        default_ms=0.0,
        exposure_preset_ms=exposure_preset_ms,
    )
    tiff_write_ms = _estimate_run_component_ms(
        "tiff_write_ms",
        records,
        mode="zscan_stage_plus_capture",
        default_ms=0.0,
        exposure_preset_ms=exposure_preset_ms,
    )
    return max(
        0.0,
        float(fixed_ms)
        + float(initial_ms)
        + (move_ms * move_count)
        + (capture_nonmove_ms * image_layers)
        + float(best_focus_move_ms)
        + float(restore_ms)
        + float(tiff_write_ms),
    )


def build_z_scan_run_timing_record(
    z_scan_config: ZScanConfig,
    *,
    mode: str,
    total_duration_ms: float,
    initial_position_ms: float,
    scan_move_latencies_ms: Sequence[float],
    restore_ms: float,
    capture_nonmove_latencies_ms: Sequence[float] | None = None,
    best_focus_move_ms: float = 0.0,
    tiff_write_ms: float = 0.0,
    fixed_overhead_ms: float | None = None,
    success: bool = True,
) -> ZScanTimingRunRecord:
    scan_moves = [float(value) for value in scan_move_latencies_ms]
    capture_nonmoves = [float(value) for value in capture_nonmove_latencies_ms or ()]
    scan_sum = sum(scan_moves)
    capture_sum = sum(capture_nonmoves)
    if fixed_overhead_ms is None:
        fixed_overhead_ms = max(
            0.0,
            float(total_duration_ms)
            - float(initial_position_ms)
            - scan_sum
            - float(restore_ms)
            - capture_sum
            - float(best_focus_move_ms)
            - float(tiff_write_ms),
        )
    return ZScanTimingRunRecord(
        timestamp=_now_timestamp(),
        mode=str(mode),
        scan_gap_nm=abs(float(z_scan_config.step_um) * 1000.0),
        num_steps=int(z_scan_config.num_steps),
        image_layers=int(z_scan_config.num_steps) + 1,
        exposure_preset_ms=int(z_scan_config.exposure_preset_ms),
        direction=str(z_scan_config.direction),
        total_duration_ms=float(total_duration_ms),
        initial_position_ms=float(initial_position_ms),
        scan_move_ms_sum=float(scan_sum),
        scan_move_ms_mean=float(statistics.mean(scan_moves)) if scan_moves else 0.0,
        scan_move_count=len(scan_moves),
        restore_ms=float(restore_ms),
        fixed_overhead_ms=float(fixed_overhead_ms),
        capture_nonmove_ms_sum=float(capture_sum),
        capture_nonmove_ms_mean=float(statistics.mean(capture_nonmoves)) if capture_nonmoves else None,
        best_focus_move_ms=float(best_focus_move_ms),
        tiff_write_ms=float(tiff_write_ms),
        success=bool(success),
    )


def build_z_scan_timing_records(
    z_scan_config: ZScanConfig,
    *,
    mode: str,
    positions: Sequence[float],
    move_latencies_ms: Sequence[float],
    cycle_latencies_ms: Sequence[float] | None = None,
    started_from_um: float | None = None,
    success: bool = True,
) -> list[ZScanTimingRecord]:
    total_positions = min(len(positions), len(move_latencies_ms))
    if total_positions <= 0:
        return []

    cycle_values = list(cycle_latencies_ms or ())
    timestamp = _now_timestamp()
    scan_gap_nm = abs(float(z_scan_config.step_um) * 1000.0)
    records: list[ZScanTimingRecord] = []
    for index in range(total_positions):
        to_z_um = float(positions[index])
        if index == 0:
            from_z_um = float(started_from_um) if started_from_um is not None else to_z_um
        else:
            from_z_um = float(positions[index - 1])
        cycle_ms = float(cycle_values[index]) if index < len(cycle_values) else None
        records.append(
            ZScanTimingRecord(
                timestamp=timestamp,
                mode=str(mode),
                scan_gap_nm=scan_gap_nm,
                num_steps=int(z_scan_config.num_steps),
                exposure_preset_ms=int(z_scan_config.exposure_preset_ms),
                direction=str(z_scan_config.direction),
                move_index=index + 1,
                total_moves=total_positions,
                from_z_um=from_z_um,
                to_z_um=to_z_um,
                distance_um=abs(to_z_um - from_z_um),
                move_ms=float(move_latencies_ms[index]),
                cycle_ms=cycle_ms,
                success=bool(success),
            )
        )
    return records


__all__ = [
    "DEFAULT_Z_SCAN_MOVE_MS",
    "DEFAULT_Z_SCAN_TIMING_HISTORY_PATH",
    "ZScanTimingEntry",
    "ZScanTimingRecord",
    "ZScanTimingRunRecord",
    "append_z_scan_timing_records",
    "build_z_scan_run_timing_record",
    "build_z_scan_timing_records",
    "estimate_z_scan_capture_test_total_time_ms",
    "estimate_z_scan_move_ms",
    "estimate_z_scan_move_only_total_time_ms",
    "estimate_z_scan_total_time_ms",
    "load_z_scan_timing_records",
]

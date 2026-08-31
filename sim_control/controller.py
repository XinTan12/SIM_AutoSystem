"""SIM 采集控制器与后台 worker。

作用：
    本文件是 GUI 与 SIM 硬件 / 采集核心之间的"调度门面"：
        - ``SimAcquisitionWorker``：``QObject``，运行在独立 ``QThread`` 中，
          负责"初始化硬件 → 应用 DAQ 与相机配置 → 选择 SLM Running Order →
          调用 ``run_single_acquisition`` 或仅做 prepare-only"。所有可能阻塞的硬件
          调用都放在这里，避免阻塞 GUI 主线程。
        - ``SimAcquisitionController``：``QObject``，由 GUI 直接持有；
          组装 worker payload、管理 ``stop_event``、转发 worker 的状态/结果/失败/
          取消信号给 GUI。

协作关系：
    上游：``sim_control/gui.py``、``control_wangbo/main.py`` 的 SIM 集成模块。
    下游：``acquisition_core.run_single_acquisition``、``adapters.*``、
          ``sim_adapters.*``、``waveform.NIDaqWaveformBuilder``。
    相关：``config_store.validate_app_config`` 在每次启动 worker 前校验配置。

关键概念：
    - ``stop_event``（``threading.Event``）：每次启动一个 worker task 都建一个；
      ``stop()`` 把它置位；worker 与 ``acquisition_core`` 内部都会检查它，最大程度
      缩短取消响应延迟。
    - ``signal_start_worker``：跨线程 Qt 信号，把 payload 投递到 worker。
    - ``effective_inter_frame_gap_us``：只在相机回报与当前 CameraConfig 签名一致时
      使用推荐 gap；缺失、非法或陈旧 preview timing 都回退 50 ms。

维护要点：
    - 任何新增硬件分支（如多相机）都必须保证：worker payload 中所有 adapter
      引用都来自 controller 实例（避免 SLM/相机重复打开）。
    - ``stop_event`` 在 worker 完成/失败/取消三种路径里都要清理；本文件通过
      ``_clear_stop_event_for_*`` 集中处理。
    - GUI 不可直接持有 ``SimSettingsDialog`` 之外的 SLM/相机生命周期，必须共
      享本 controller 的 ``camera_adapter`` / ``slm_adapter``（决策日志
      2026-04-26 的硬约束）。
"""

from __future__ import annotations

import copy
import logging
import re
import threading
import traceback
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from PyQt5.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

from .acquisition_core import (
    AcquisitionCancelled,
    ZStackAcquisitionCancelled,
    ZStackAcquisitionFailed,
    ZStackAcquisitionResult,
    run_single_acquisition,
    run_z_stack_acquisition,
)
from .adapter_factory import create_adapter_bundle
from .adapters import (
    HardwareError,
    R11_ACTIVATION_TYPE_NAMES,
    find_best_running_order,
    find_z_scan_running_order,
    is_immediate_activation_type,
)
from .camera_timing import camera_config_signature
from .config_store import validate_app_config
from .models import (
    AppConfig,
    BackendConfig,
    CameraConfig,
    DaqLineConfig,
    DEFAULT_RED_LASER_NM,
    LASER_ROLE_MAP,
    PatternPreparationResult,
    ReconstructionConfig,
    SimTaskConfig,
    ZScanConfig,
    effective_inter_frame_gap_us,
    new_task_id,
)
from .waveform import NIDaqWaveformBuilder, parse_line_name, validate_daq_line_config
from .z_scan_core import FocusFrameRecord, preflight_z_scan_positions, scan_positions
from .z_stack_io import (
    SeriesWriteJob,
    SeriesWriteResult,
    SeriesWriteTimeout,
    ZStackAsyncWriter,
)


logger = logging.getLogger(__name__)

_IMMEDIATE_LIVE_RO_RE = re.compile(r"^(?P<wavelength>\d+)_3\.5_2d_imm_(?P<slot>f[1-9]|3dir)$")


def _parse_immediate_live_ro_name(name: str) -> dict[str, Any] | None:
    match = _IMMEDIATE_LIVE_RO_RE.match(str(name))
    if not match:
        return None
    return {"wavelength_nm": int(match.group("wavelength")), "slot": match.group("slot")}


def immediate_live_wavelength_matches(parsed_wavelength_nm: Any, selected_wavelength_nm: Any) -> bool:
    """判断 immediate RO 名前导波长是否兼容当前选择的激光波长。

    红光兼容是双向的：638 与 647 互为等价，任一为选择波长、另一为 RO 前缀都算兼容
    （支持两台机器分别只烧录 638 或 647 命名的 RO）；其它波长仍必须严格等值匹配。
    ``parsed_wavelength_nm`` 为 None 或无法解析时返回 False（不放行未知前缀）。
    """
    try:
        parsed = int(parsed_wavelength_nm)
        selected = int(selected_wavelength_nm)
    except (TypeError, ValueError):
        return False
    return parsed == selected or {parsed, selected} == {638, 647}


def _activation_type_label(value: Any) -> str:
    if value is None:
        return "None"
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{R11_ACTIVATION_TYPE_NAMES.get(numeric, f'0x{numeric:02X}')} (0x{numeric:02X})"


def _acquisition_summary_payload(batch: Any) -> dict[str, Any]:
    """Build the GUI-facing acquisition summary without copying image data."""
    stack = getattr(batch, "stack", None)
    shape = getattr(stack, "shape", ())
    dtype = getattr(stack, "dtype", "")
    metadata = getattr(batch, "metadata", {}) or {}
    return {
        "task_id": str(getattr(batch, "task_id", "")),
        "stack_shape": list(shape),
        "stack_dtype": str(dtype),
        "metadata": dict(metadata),
    }


def _finalize_series_writer(
    writer: Any,
    outcome: str,
    *,
    best_plane_index: int | None = None,
    message: str = "",
) -> SeriesWriteResult:
    """Finalize once and poll the same terminal command until a large merge ends.

    A multi-gigabyte TIFF merge can legitimately exceed the writer's ordinary
    synchronous timeout.  The terminal command is already queued at that point,
    so it must never be resubmitted or converted into a false acquisition
    failure merely because one wait window elapsed.  ``wait_for_terminal``
    checks writer-thread liveness and therefore still raises on a real failure.
    """
    kwargs: dict[str, Any] = {"message": str(message)}
    if best_plane_index is not None:
        kwargs["best_plane_index"] = int(best_plane_index)
    try:
        return writer.finalize(outcome, **kwargs)
    except SeriesWriteTimeout as timeout_error:
        last_timeout = timeout_error
        while True:
            try:
                result = writer.wait_for_terminal(timeout=10.0)
            except SeriesWriteTimeout as wait_timeout:
                last_timeout = wait_timeout
                continue
            except Exception as recovery_error:
                raise recovery_error from last_timeout
            if not isinstance(result, SeriesWriteResult):
                raise RuntimeError(
                    "Series writer terminal recovery returned no result."
                ) from last_timeout
            return result


def _merge_z_stack_write_result(
    result: ZStackAcquisitionResult,
    writer_result: SeriesWriteResult,
) -> ZStackAcquisitionResult:
    """Make writer-committed progress authoritative without retaining image arrays."""
    committed_layers = max(
        0,
        min(
            int(writer_result.completed_layers),
            int(result.total_layers),
            len(result.measured_z_um),
        ),
    )
    return replace(
        result,
        measured_z_um=tuple(result.measured_z_um[:committed_layers]),
        completed_layers=committed_layers,
        status=str(writer_result.outcome),
        output_paths=tuple(str(path) for path in writer_result.output_paths),
        message=str(writer_result.message or result.message),
    )


# Worker 运行在 QThread 中，所有可能阻塞的硬件初始化和采集动作都从 GUI 线程移出。
class SimAcquisitionWorker(QObject):
    """后台采集 worker，在 QThread 中执行硬件准备、RO 选择和正式采集。

    线程模型：
        - ``__init__`` 在 GUI 线程构造，``moveToThread(QThread)`` 之后所有 slot
          都在工作线程执行。
        - 通过 ``signal_start_worker`` 接收 payload；处理过程同步阻塞。

    状态信号：
        - ``signal_status_changed(status_name, payload_dict)``：阶段性进度。
        - ``signal_acquisition_ready(batch)``：正式采集成功。
        - ``signal_acquisition_summary_ready(payload)``：正式采集成功后的 GUI 轻量摘要。
        - ``signal_acquisition_failed(task_id, traceback)``：硬件/逻辑失败。
        - ``signal_acquisition_cancelled(task_id, message)``：用户/stop_event 取消。
        - ``signal_prepare_ready(task_id, payload)``：prepare_only 路径完成。
    """
    signal_status_changed = pyqtSignal(str, dict)
    signal_acquisition_ready = pyqtSignal(object)
    signal_acquisition_summary_ready = pyqtSignal(dict)
    signal_acquisition_failed = pyqtSignal(str, str)
    signal_acquisition_cancelled = pyqtSignal(str, str)
    signal_prepare_ready = pyqtSignal(str, dict)
    signal_z_scan_progress = pyqtSignal(int, int, float, float)
    signal_z_scan_complete = pyqtSignal(float, object)
    signal_z_stack_progress = pyqtSignal(dict)
    signal_z_stack_result_ready = pyqtSignal(object)

    @pyqtSlot(object)
    def slot_start(self, payload: dict[str, Any]) -> None:
        """Qt 信号入口：在工作线程执行一整套 SIM 采集流程。

        payload 字典字段（``_start_worker_task`` 中组装）：
            - task / task_id / daq_config / pattern_result：核心数据。
            - waveform_builder / daq_adapter / camera_adapter / slm_adapter：注入对象。
            - stop_event：可选取消标志。
            - initialize_hardware / apply_daq_config / apply_camera_config /
              prepare_running_order / prepare_only：开关，控制本次跑哪些阶段。
        """
        # 1) 拆解 payload，命中类型注解便于 IDE 检查（运行时不强校验）。
        task: SimTaskConfig = payload["task"]
        task_id: str = payload["task_id"]
        daq_config: DaqLineConfig = payload["daq_config"]
        pattern_result: PatternPreparationResult = payload["pattern_result"]
        waveform_builder: NIDaqWaveformBuilder = payload["waveform_builder"]
        daq = payload["daq_adapter"]
        camera = payload["camera_adapter"]
        slm = payload["slm_adapter"]
        stage = payload.get("stage_adapter")
        z_scan_config: ZScanConfig | None = payload.get("z_scan_config")
        z_scan_enabled = bool(payload.get("z_scan_enabled")) and z_scan_config is not None
        z_scan_pattern_result: PatternPreparationResult | None = None
        stop_event = payload.get("stop_event")
        acquisition_mode = str(payload.get("acquisition_mode", "single"))
        is_z_stack = acquisition_mode == "z_stack"
        series_writer = payload.get("series_writer")
        z_stack_writer_started = False
        z_stack_requested_positions: tuple[float, ...] = ()
        z_stack_terminal_emitted = False
        focus_writer_started = False
        focus_writer_terminal = False
        focus_diagnostics_enabled = False
        focus_diagnostics_failed = False
        focus_records: list[FocusFrameRecord] = []
        formal_restore_pending = False
        formal_ro_index: int | None = None
        formal_ro_name = ""

        def emit_status(status: str, data: dict[str, Any]) -> None:
            self.signal_status_changed.emit(status, data)
            if status == "z_scan_progress":
                self.signal_z_scan_progress.emit(
                    int(data.get("step_index", 0)),
                    int(data.get("total_steps", 0)),
                    float(data.get("z_um", 0.0)),
                    float(data.get("focus_score", 0.0)),
                )
            elif status == "z_scan_complete":
                self.signal_z_scan_complete.emit(
                    float(data.get("best_z_um", 0.0)),
                    data.get("focus_curve", []),
                )
            elif status == "z_stack_layer_accepted":
                self.signal_z_stack_progress.emit(dict(data))

        def restore_formal_running_order(status: str = "running_order_restored") -> None:
            """Fulfil the worker-owned restore obligation exactly once."""
            nonlocal formal_restore_pending
            if not formal_restore_pending:
                return
            # Clear before the hardware call so a failed restore is reported once,
            # rather than retried and obscured by an outer exception handler.
            formal_restore_pending = False
            if formal_ro_index is None:
                raise HardwareError(
                    "Cannot restore formal SIM running order after z-scan: "
                    "missing running_order_index."
                )
            try:
                slm.select_running_order(int(formal_ro_index))
            except Exception as exc:
                message = (
                    "Failed to restore formal SIM running order "
                    f"{formal_ro_name!r} (index {int(formal_ro_index)}): {exc}"
                )
                emit_status(
                    "running_order_restore_warning",
                    {"task_id": task_id, "message": message},
                )
                raise HardwareError(message) from exc
            emit_status(
                status,
                {
                    "task_id": task_id,
                    "running_order_index": int(formal_ro_index),
                    "running_order_name": formal_ro_name,
                },
            )

        def emit_focus_warning(message: str) -> None:
            emit_status(
                "focus_diagnostics_warning",
                {"task_id": task_id, "message": str(message)},
            )

        def on_focus_frame(record: FocusFrameRecord) -> None:
            nonlocal focus_diagnostics_enabled, focus_diagnostics_failed
            if not focus_diagnostics_enabled or series_writer is None:
                return
            try:
                check_health = getattr(series_writer, "check_health", None)
                if callable(check_health):
                    check_health()
                series_writer.submit_focus_layer(
                    plane_index=int(record.plane_index),
                    frame=record.frame,
                    requested_z_um=float(record.requested_z_um),
                    measured_z_um=float(record.measured_z_um),
                    sml_score=float(record.score),
                    timestamp=str(record.timestamp),
                    stop_event=stop_event,
                )
                focus_records.append(record)
            except Exception as diagnostic_error:
                focus_diagnostics_enabled = False
                focus_diagnostics_failed = True
                emit_focus_warning(f"Focus diagnostics disabled after submit/health failure: {diagnostic_error}")

        def finalize_focus_diagnostics(outcome: str, message: str = "") -> SeriesWriteResult | None:
            nonlocal focus_writer_terminal, focus_diagnostics_enabled, focus_diagnostics_failed
            if not focus_writer_started or focus_writer_terminal or series_writer is None:
                return None
            focus_writer_terminal = True
            effective_outcome = "failed" if focus_diagnostics_failed and outcome == "complete" else outcome
            best_plane_index: int | None = None
            if effective_outcome == "complete" and focus_records:
                best_record = max(focus_records, key=lambda record: record.score)
                best_plane_index = int(best_record.plane_index)
            try:
                writer_result = _finalize_series_writer(
                    series_writer,
                    effective_outcome,
                    best_plane_index=best_plane_index,
                    message=message,
                )
            except Exception as diagnostic_error:
                focus_diagnostics_enabled = False
                focus_diagnostics_failed = True
                emit_focus_warning(f"Focus diagnostics finalization failed: {diagnostic_error}")
                return None
            focus_diagnostics_enabled = False
            if (
                effective_outcome == "complete"
                and writer_result.outcome == "complete"
                and int(writer_result.completed_layers) == int(writer_result.total_layers)
                and len(writer_result.output_paths) == 2
            ):
                emit_status(
                    "focus_diagnostics_saved",
                    {
                        "task_id": task_id,
                        "output_paths": [str(path) for path in writer_result.output_paths],
                        "completed_layers": int(writer_result.completed_layers),
                    },
                )
            else:
                focus_diagnostics_failed = True
                retained_outputs = ", ".join(str(path) for path in writer_result.output_paths) or "none"
                retained_spool = str(writer_result.spool_path) if writer_result.spool_path is not None else "none"
                retained_staging = ", ".join(str(path) for path in writer_result.staging_paths) or "none"
                detail = str(writer_result.message or "no writer detail")
                emit_focus_warning(
                    "Focus diagnostics writer returned a non-complete terminal result: "
                    f"requested_outcome={effective_outcome}, "
                    f"outcome={writer_result.outcome}, "
                    f"completed={writer_result.completed_layers}/{writer_result.total_layers}; "
                    f"retained outputs={retained_outputs}; "
                    f"retained spool={retained_spool}; "
                    f"retained staging={retained_staging}; detail={detail}."
                )
            return writer_result

        def make_empty_z_stack_result(status: str, message: str) -> ZStackAcquisitionResult:
            return ZStackAcquisitionResult(
                task_id=task_id,
                requested_z_um=z_stack_requested_positions,
                measured_z_um=(),
                completed_layers=0,
                total_layers=len(z_stack_requested_positions),
                layer_shape=None,
                status=status,
                message=str(message),
            )

        def finalize_z_stack_result(
            result: ZStackAcquisitionResult,
            outcome: str,
            message: str = "",
        ) -> tuple[ZStackAcquisitionResult, Exception | None]:
            nonlocal z_stack_terminal_emitted
            terminal_error: Exception | None = None
            merged = replace(result, status=outcome, message=str(message or result.message))
            if z_stack_writer_started and series_writer is not None:
                try:
                    writer_result = _finalize_series_writer(
                        series_writer,
                        outcome,
                        message=str(message or result.message),
                    )
                    merged = _merge_z_stack_write_result(merged, writer_result)
                    if (
                        outcome == "complete"
                        and (
                            merged.status != "complete"
                            or merged.completed_layers != merged.total_layers
                        )
                    ):
                        failure_message = merged.message or (
                            "Series writer did not commit every requested Z-stack layer."
                        )
                        merged = replace(merged, status="failed", message=failure_message)
                except Exception as writer_error:
                    terminal_error = writer_error
                    failure_message = str(message or result.message)
                    if failure_message:
                        failure_message += "\n"
                    failure_message += f"Series writer finalization failed: {writer_error}"
                    merged = replace(merged, status="failed", message=failure_message)
            self.signal_z_stack_result_ready.emit(merged)
            z_stack_terminal_emitted = True
            return merged, terminal_error

        try:
            _raise_if_cancelled(stop_event)
            # 2) 可选阶段 1：硬件初始化。仅当 controller 显式请求时执行；多次启动可跳过。
            if payload.get("initialize_hardware") or z_scan_enabled:
                emit_status("hardware_initializing", {})
                camera.initialize()
                _raise_if_cancelled(stop_event)
                slm.initialize()
                emit_status("hardware_initialized", {})
            # 3) 可选阶段 2：DAQ 配置校验（不直接打开 NI 任务，只确认线位合法）。
            if payload.get("apply_daq_config") or z_scan_enabled:
                validate_daq_line_config(daq_config)
                emit_status("daq_config_applied", {"device_name": daq_config.device_name})
            # 4) 可选阶段 3：把相机配置下发到 DCAM，并把相机回报反写到 task 与时序。
            if payload.get("apply_camera_config") or z_scan_enabled:
                if task.camera.trigger_mode != "external_level":
                    raise HardwareError("Only external_level trigger mode is supported.")
                result = camera.apply_config(task.camera) or {}
                _apply_camera_result_to_config(task.camera, result)
                # 4a) 采用本次 ROI/曝光对应的相机推荐值；仅缺失/非法时回退 50 ms。
                task.timing.inter_frame_gap_us = effective_inter_frame_gap_us(
                    result.get("recommended_inter_frame_gap_us")
                )
                # 4b) 把整段相机配置 + 相机回报合并广播给 GUI，便于显示实际生效参数。
                payload_data = {"camera_config": dict(task.camera.__dict__), **dict(result)}
                emit_status("camera_config_applied", payload_data)
                if bool(result.get("timing_fallback_used")):
                    emit_status(
                        "camera_timing_fallback_warning",
                        {
                            "task_id": task_id,
                            "message": str(
                                result.get("timing_fallback_reason")
                                or "Camera timing properties are unavailable; using 50 ms fallback."
                            ),
                            **payload_data,
                        },
                    )
            if z_scan_enabled:
                # Formal SIM9 feasibility is part of the all-before-motion preflight.
                waveform_builder.build(
                    daq_config=daq_config,
                    timing=task.timing,
                    laser_wavelength_nm=int(task.laser_wavelength_nm),
                    exposure_us=int(task.camera.exposure_us),
                    frame_count=9,
                    include_role_matrix=False,
                )
            # 5) 可选阶段 4：自动选择匹配波长 + 曝光的 Running Order。
            if payload.get("prepare_running_order") or z_scan_enabled:
                running_orders = slm.list_running_orders()
                ro_index, ro_name, warnings = find_best_running_order(
                    running_orders,
                    wavelength_nm=int(task.laser_wavelength_nm),
                    exposure_us=int(task.camera.exposure_us),
                    # 排除找样品 immediate RO（索引集合随 payload 从 controller 带入），
                    # 否则正式 9 帧采集可能误选 ACT_IMMEDIATE RO。
                    exclude_indices=set(payload.get("immediate_ro_indices") or ()),
                )
                if ro_index is None:
                    # 没匹配到 RO → 抛错让 GUI 提示用户检查 SLM 烧录内容。
                    raise HardwareError("; ".join(warnings) or "No matching SLM running order found.")
                result = slm.select_running_order(ro_index)
                # 5a) 规整成统一 PatternPreparationResult，下游 acquisition_core 才能消费。
                pattern_result = _coerce_running_order_pattern_result(result, ro_index, ro_name)
                task.running_order_name = ro_name
                task.pattern_files = list(pattern_result.pattern_files)
                emit_status(
                    "running_order_selected",
                    _running_order_payload(result, pattern_result, ro_index, ro_name, warnings),
                )
            if is_z_stack:
                if z_scan_config is None or not z_scan_enabled:
                    raise ValueError("Z-stack acquisition requires an enabled z_scan_config.")
                if bool(z_scan_config.select_focus_plane):
                    raise ValueError("Z-stack acquisition requires select_focus_plane=False.")
                if series_writer is None:
                    raise RuntimeError("Z-stack acquisition requires a series writer.")
                if stage is None:
                    raise HardwareError("Z-stack acquisition requires a Z stage adapter.")
                if not getattr(stage, "is_connected", False):
                    info = stage.connect()
                    emit_status("z_stage_connected", dict(info or {}))
                _raise_if_cancelled(stop_event)
                stack_config = replace(z_scan_config, start_um=None)
                z_stack_requested_positions = tuple(
                    scan_positions(
                        stack_config,
                        stage_position_um=float(stage.get_position_um()),
                    )
                )
                preflight_z_scan_positions(stage, stack_config)
                output_path = payload.get("z_stack_output_path")
                if output_path is None or not str(output_path).strip():
                    raise ValueError("Z-stack output_path must not be empty.")
                writer_spec = SeriesWriteJob(
                    job_id=task_id,
                    kind="zstack",
                    output_path=Path(output_path),
                    total_layers=len(z_stack_requested_positions),
                    frame_shape=(int(task.camera.roi_height), int(task.camera.roi_width)),
                    wavelength_nm=int(task.laser_wavelength_nm),
                    exposure_us=int(task.camera.exposure_us),
                    running_order=str(
                        task.running_order_name
                        or pattern_result.metadata.get("running_order_name", "")
                    ),
                )
                series_writer.begin_job(writer_spec)
                z_stack_writer_started = True
                emit_status(
                    "z_stack_writer_ready",
                    {
                        "task_id": task_id,
                        "output_path": str(writer_spec.output_path),
                        "total_layers": writer_spec.total_layers,
                        "frame_shape": list(writer_spec.frame_shape),
                    },
                )
                _raise_if_cancelled(stop_event)
                try:
                    stack_result = run_z_stack_acquisition(
                        task=task,
                        daq_config=daq_config,
                        pattern_result=pattern_result,
                        camera=camera,
                        slm=slm,
                        daq=daq,
                        stage_adapter=stage,
                        z_scan_config=z_scan_config,
                        layer_sink=series_writer,
                        waveform_builder=waveform_builder,
                        task_id=task_id,
                        on_status=emit_status,
                        stop_event=stop_event,
                    )
                except ZStackAcquisitionCancelled as stack_cancelled:
                    merged, writer_error = finalize_z_stack_result(
                        stack_cancelled.partial_result,
                        "cancelled",
                        str(stack_cancelled),
                    )
                    if writer_error is not None or merged.status == "failed":
                        self.signal_acquisition_failed.emit(task_id, merged.message)
                    else:
                        emit_status(
                            "acquisition_cancelled",
                            {"task_id": task_id, "message": merged.message},
                        )
                        self.signal_acquisition_cancelled.emit(task_id, merged.message)
                    return
                except ZStackAcquisitionFailed as stack_failed:
                    stack_traceback = traceback.format_exc()
                    merged, _writer_error = finalize_z_stack_result(
                        stack_failed.partial_result,
                        "failed",
                        str(stack_failed),
                    )
                    self.signal_acquisition_failed.emit(
                        task_id,
                        f"{merged.message}\n{stack_traceback}",
                    )
                    return

                merged, writer_error = finalize_z_stack_result(stack_result, "complete")
                if (
                    writer_error is not None
                    or merged.status != "complete"
                    or merged.completed_layers != merged.total_layers
                ):
                    failure_message = merged.message or (
                        "Series writer did not commit every requested Z-stack layer."
                    )
                    if merged.status == "complete":
                        merged = replace(merged, status="failed", message=failure_message)
                    self.signal_acquisition_failed.emit(task_id, failure_message)
                return

            if z_scan_enabled:
                if stage is None:
                    raise HardwareError("Z-scan is enabled but no Z stage adapter is available.")
                if not getattr(stage, "is_connected", False):
                    info = stage.connect()
                    emit_status("z_stage_connected", dict(info or {}))
                preflight_z_scan_positions(stage, z_scan_config)
                running_orders = slm.list_running_orders()
                z_ro_index, z_ro_name, z_warnings = find_z_scan_running_order(
                    running_orders,
                    wavelength_nm=int(task.laser_wavelength_nm),
                    exposure_preset_ms=int(z_scan_config.exposure_preset_ms),
                )
                if z_ro_index is None:
                    raise HardwareError("; ".join(z_warnings) or "No matching z-scan running order found.")
                formal_index_value = pattern_result.metadata.get("running_order_index")
                if formal_index_value is None:
                    raise HardwareError(
                        "Z-scan is enabled but the formal SIM running order has no "
                        "running_order_index."
                    )
                formal_ro_index = int(formal_index_value)
                formal_ro_name = str(pattern_result.metadata.get("running_order_name", ""))
                # The select call may fail after partially changing hardware state,
                # so establish the restore obligation before invoking the adapter.
                formal_restore_pending = True
                z_result = slm.select_running_order(z_ro_index)
                z_scan_pattern_result = _coerce_running_order_pattern_result(z_result, z_ro_index, z_ro_name)
                emit_status(
                    "z_scan_running_order_selected",
                    _running_order_payload(z_result, z_scan_pattern_result, z_ro_index, z_ro_name, z_warnings),
                )
                focus_output_path = payload.get("focus_output_path")
                if focus_output_path is not None:
                    if series_writer is None:
                        emit_focus_warning("Focus diagnostics requested without a series writer.")
                    else:
                        focus_spec = SeriesWriteJob(
                            job_id=f"{task_id}:focus",
                            kind="focus",
                            output_path=Path(focus_output_path),
                            csv_path=(
                                Path(payload["focus_csv_path"])
                                if payload.get("focus_csv_path") is not None
                                else None
                            ),
                            total_layers=int(z_scan_config.num_steps) + 1,
                            frame_shape=(int(task.camera.roi_height), int(task.camera.roi_width)),
                            wavelength_nm=int(task.laser_wavelength_nm),
                            exposure_us=int(z_scan_config.actual_exposure_us),
                            running_order=str(
                                z_scan_pattern_result.metadata.get("running_order_name", "")
                                if z_scan_pattern_result is not None
                                else ""
                            ),
                        )
                        try:
                            series_writer.begin_job(focus_spec)
                        except Exception as diagnostic_error:
                            focus_diagnostics_failed = True
                            emit_focus_warning(f"Focus diagnostics begin failed: {diagnostic_error}")
                        else:
                            focus_writer_started = True
                            focus_diagnostics_enabled = True
                            emit_status(
                                "focus_diagnostics_ready",
                                {
                                    "task_id": task_id,
                                    "output_path": str(focus_spec.output_path),
                                    "total_layers": focus_spec.total_layers,
                                },
                            )
            _raise_if_cancelled(stop_event)
            # 6) prepare-only 路径：到这里就算完成；返回前广播"patterns_prepared"事件。
            if payload.get("prepare_only"):
                restore_formal_running_order()
                prepared_payload = {
                    "task_id": task_id,
                    "running_order_name": task.running_order_name,
                    "pattern_files": list(task.pattern_files),
                    "handles": list(pattern_result.handles),
                    "metadata": dict(pattern_result.metadata),
                }
                emit_status("patterns_prepared", prepared_payload)
                self.signal_prepare_ready.emit(task_id, prepared_payload)
                return
            # 7) 正式采集路径：调 ``run_single_acquisition``，由它负责 9 帧硬件流程。
            batch = run_single_acquisition(
                task=task,
                daq_config=daq_config,
                pattern_result=pattern_result,
                camera=camera,
                slm=slm,
                daq=daq,
                waveform_builder=waveform_builder,
                task_id=task_id,
                on_status=emit_status,
                stop_event=stop_event,
                stage_adapter=stage,
                z_scan_config=z_scan_config if z_scan_enabled else None,
                z_scan_pattern_result=z_scan_pattern_result,
                formal_running_order_restore=restore_formal_running_order,
                on_focus_frame=on_focus_frame if focus_writer_started else None,
            )
            restore_formal_running_order()
            finalize_focus_diagnostics("complete")
            self.signal_acquisition_ready.emit(batch)
            self.signal_acquisition_summary_ready.emit(_acquisition_summary_payload(batch))
        except AcquisitionCancelled as exc:
            # 8a) 取消路径：广播 ``acquisition_cancelled``，不发 failed 信号，
            #     便于 GUI 区分"用户主动停"和"硬件出错"。
            finalize_focus_diagnostics("cancelled", str(exc))
            if is_z_stack and not z_stack_terminal_emitted:
                base = make_empty_z_stack_result("cancelled", str(exc))
                merged, writer_error = finalize_z_stack_result(base, "cancelled", str(exc))
                if writer_error is not None or merged.status == "failed":
                    self.signal_acquisition_failed.emit(task_id, merged.message)
                    return
            try:
                restore_formal_running_order("running_order_restored_after_cancel")
            except Exception as restore_exc:
                message = f"{exc}\n{restore_exc}"
                self.signal_acquisition_failed.emit(task_id, message)
                return
            message = str(exc) or "Acquisition cancelled."
            emit_status("acquisition_cancelled", {"task_id": task_id, "message": message})
            self.signal_acquisition_cancelled.emit(task_id, message)
        except Exception as exc:
            # 8b) 失败路径：附上完整 traceback，让 GUI 弹错误对话框时可让用户复制。
            original_traceback = traceback.format_exc()
            finalize_focus_diagnostics("failed", str(exc))
            if is_z_stack and not z_stack_terminal_emitted:
                base = make_empty_z_stack_result("failed", str(exc))
                merged, _writer_error = finalize_z_stack_result(base, "failed", str(exc))
                self.signal_acquisition_failed.emit(
                    task_id,
                    f"{merged.message}\n{original_traceback}",
                )
                return
            try:
                restore_formal_running_order("running_order_restored_after_failure")
            except Exception as restore_exc:
                self.signal_acquisition_failed.emit(
                    task_id,
                    f"{exc}\n{restore_exc}\n{original_traceback}",
                )
                return
            self.signal_acquisition_failed.emit(task_id, f"{exc}\n{original_traceback}")
        finally:
            if is_z_stack:
                try:
                    camera.disarm()
                except Exception:
                    logger.warning("Failed to disarm camera during Z-stack worker cleanup.", exc_info=True)
                try:
                    daq.set_all_low(daq_config.device_name)
                except Exception:
                    logger.warning("Failed to set DAQ low during Z-stack worker cleanup.", exc_info=True)


def _raise_if_cancelled(stop_event: Any | None) -> None:
    """worker 内部"快速取消"助手；与 ``acquisition_core._raise_if_cancelled`` 语义相同。"""
    if stop_event is not None and stop_event.is_set():
        raise AcquisitionCancelled("Acquisition cancelled.")


def _apply_camera_result_to_config(config: CameraConfig, result: dict[str, Any]) -> None:
    """把相机实际接受的位深和 ROI 回写到任务配置。

    用途：
        DCAM 可能根据传感器边界把 ROI 调整到对齐位置；如果不回写 task.camera，
        后续摘要/UI 显示的会是用户原始请求，与硬件实际生效不一致。
    """
    # 1) bit_depth：DCAM 可能 fallback 到接近的合法值（如 12 → 16）。
    if result.get("applied_bit_depth") is not None:
        config.bit_depth = int(result["applied_bit_depth"])
    # 2) applied_roi：相机回报实际接受的 (x, y, width, height)；逐字段回写。
    applied_roi = result.get("applied_roi")
    if isinstance(applied_roi, dict):
        config.roi_x = int(applied_roi.get("x", config.roi_x))
        config.roi_y = int(applied_roi.get("y", config.roi_y))
        config.roi_width = int(applied_roi.get("width", config.roi_width))
        config.roi_height = int(applied_roi.get("height", config.roi_height))


def _matching_camera_timing_recommendation(
    runtime_timing: dict[str, Any] | None,
    camera_config: CameraConfig,
) -> Any | None:
    """只返回与当前相机配置完整签名一致的运行时 recommendation。"""
    if not isinstance(runtime_timing, dict) or not isinstance(
        runtime_timing.get("camera_config"), dict
    ):
        return None
    try:
        if camera_config_signature(runtime_timing) != camera_config_signature(camera_config):
            return None
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return None
    return runtime_timing.get("recommended_inter_frame_gap_us")


def _coerce_running_order_pattern_result(
    result: dict[str, Any],
    ro_index: int,
    ro_name: str,
) -> PatternPreparationResult:
    """把 SLM RO 选择结果规整成统一的 ``PatternPreparationResult``。

    用途：
        ``select_running_order`` 不同 adapter 实现可能直接返回
        ``PatternPreparationResult``，也可能只返回元数据字典；本函数把后者补全
        为标准 PatternPreparationResult，下游 acquisition_core 才能直接消费。
    """
    # 1) 如果 adapter 已直接返回 PatternPreparationResult，直接转交。
    pattern_result = result.get("pattern_result")
    if isinstance(pattern_result, PatternPreparationResult):
        return pattern_result
    # 2) 否则用 (ro_index, ro_name) 自行构造一个：handles=[-1] 表示 RO 模式无显式句柄。
    return PatternPreparationResult(
        pattern_files=[ro_name] * 9,
        handles=[-1],
        prepared_at=0.0,
        metadata={
            "mode": "running_order",
            "running_order_index": ro_index,
            "running_order_name": ro_name,
        },
    )


def _running_order_payload(
    result: dict[str, Any],
    pattern_result: PatternPreparationResult,
    ro_index: int,
    ro_name: str,
    warnings: list[str],
) -> dict[str, Any]:
    """把 Running Order 选择结果整理成 GUI 状态信号可直接显示的 payload 字典。"""
    return {
        **dict(result),
        "running_order_index": ro_index,
        "running_order_name": ro_name,
        "warnings": list(warnings),
        "handles": list(pattern_result.handles),
        "prepared_at": pattern_result.prepared_at,
    }


# Controller 是 GUI 与 worker 的桥：负责组装 payload、管理 stop_event 和转发结果信号。
class SimAcquisitionController(QObject):
    """采集控制门面，向 GUI 提供 prepare、run、stop 等异步操作。

    职责：
        - 创建并管理一对 ``QThread`` + ``SimAcquisitionWorker``。
        - 持有三个 adapter（相机/SLM/DAQ），让 SimSettingsDialog 与集成主界面
          共享同一份硬件连接。
        - 维护 ``stop_event`` 字典，把 ``stop()`` 转发到所有活跃 task。
        - 把 worker 的状态/结果/失败/取消信号转发到 GUI。
        - 提供 prepare-only 与正式采集两套入口。

    协作：
        - 由 GUI 顶层 SimControlWindow 或 ``control_wangbo/main.py`` 单例持有。
        - 通过 ``backend.simulation_mode`` 决定 ``_create_adapters`` 走真实
          还是仿真路径。
    """
    signal_status_changed = pyqtSignal(str, dict)
    signal_acquisition_ready = pyqtSignal(object)
    signal_acquisition_summary_ready = pyqtSignal(dict)
    signal_acquisition_failed = pyqtSignal(str, str)
    signal_acquisition_cancelled = pyqtSignal(str, str)
    signal_z_scan_progress = pyqtSignal(int, int, float, float)
    signal_z_scan_complete = pyqtSignal(float, object)
    signal_z_stack_progress = pyqtSignal(dict)
    signal_z_stack_result_ready = pyqtSignal(object)
    # 跨线程信号：把 payload 投递给 worker 的 ``slot_start``。
    signal_start_worker = pyqtSignal(object)

    def __init__(
        self,
        backend: BackendConfig | None = None,
        parent: QObject | None = None,
        *,
        red_laser_nm: int = DEFAULT_RED_LASER_NM,
        z_stack_writer: Any | None = None,
        z_stack_writer_factory: Callable[[], Any] | None = None,
    ):
        # 1) 调父类构造；让 controller 挂在 parent 上便于自动清理。
        super().__init__(parent)
        # 2) backend 缺省 → 用默认配置（非仿真模式 + 空 SDK 路径）。
        self.backend = backend or BackendConfig()
        # 机器红光身份必须来自 AppConfig，不能由 task 波长反推；启动任务时
        # 原样写入 validation AppConfig，让统一配置校验负责拒绝 638/647 机器身份冲突。
        self.red_laser_nm = int(red_laser_nm)
        # 3) 单实例波形 builder，避免每次采集都重建。
        self.waveform_builder = NIDaqWaveformBuilder()
        # 4) 创建 3 个 adapter（真实 or 仿真，由 backend.simulation_mode 决定）。
        (
            self.camera_adapter,
            self.slm_adapter,
            self.daq_adapter,
            self.stage_adapter,
        ) = self._create_adapters(self.backend)
        # 5) 默认 DAQ/相机/图案配置；后续 GUI 会通过 ``apply_*`` 覆盖。
        self.daq_config = DaqLineConfig()
        self.camera_config = CameraConfig()
        self.z_scan_config = ZScanConfig()
        self.reconstruction_config = ReconstructionConfig()
        self.pattern_result = PatternPreparationResult()
        # 6) 记录相机最近一次回报的 timing 信息（含 recommended_inter_frame_gap_us）。
        self._latest_camera_timing: dict[str, Any] = {}
        # 7) shutdown 与 stop_event 簿记：``_active_stop_events`` 按 task_id 索引，
        #    ``_current_stop_event`` 是最近一次启动的 event 引用。
        self._shutdown_requested = False
        self._active_stop_events: dict[str, threading.Event] = {}
        self._current_stop_event: threading.Event | None = None
        self._series_writer = z_stack_writer
        self._series_writer_factory = z_stack_writer_factory or ZStackAsyncWriter
        # 7a) immediate-live（找样品）运行态——仅 controller 私有，不落盘（见决策）。
        #     `_immediate_ro_scan` 缓存连接时一次性扫描的全部 RO+激活类型；
        #     `_immediate_ro_indices` 是其中 ACT_IMMEDIATE 的索引集合，供正式选择排除；
        #     `_immediate_live_ro_indices` 进一步收口到 `*_imm_f1..f9/3dir` 命名，供找样品点灯。
        #     `_immediate_live_active` / `_immediate_live_line` 跟踪激光线状态：
        #     先记 line（arming）再 set_line 高，任何失败都能据此 best-effort 拉低。
        self._immediate_ro_scan: list[dict[str, Any]] = []
        self._immediate_ro_indices: set[int] = set()
        self._immediate_live_ro_indices: set[int] = set()
        self._immediate_live_active = False
        self._immediate_live_line: tuple[str, int] | None = None

        # 8) 创建独立 QThread + worker，绑定一组信号路由，再启动线程。
        self._thread = QThread(self)
        self._worker = SimAcquisitionWorker()
        self._worker.moveToThread(self._thread)
        # 先在 controller 主线程更新签名绑定的 timing 缓存，再向 GUI 转发同一状态。
        self._worker.signal_status_changed.connect(self._handle_worker_status)
        self._worker.signal_acquisition_ready.connect(self.signal_acquisition_ready)
        self._worker.signal_acquisition_summary_ready.connect(self.signal_acquisition_summary_ready)
        self._worker.signal_acquisition_failed.connect(self.signal_acquisition_failed)
        self._worker.signal_acquisition_cancelled.connect(self.signal_acquisition_cancelled)
        self._worker.signal_z_scan_progress.connect(self.signal_z_scan_progress)
        self._worker.signal_z_scan_complete.connect(self.signal_z_scan_complete)
        self._worker.signal_z_stack_progress.connect(self.signal_z_stack_progress)
        self._worker.signal_z_stack_result_ready.connect(self.signal_z_stack_result_ready)
        # 8a) 三个清理钩子：prepare_ready/ready/failed/cancelled 都要从字典里移除 stop_event。
        self._worker.signal_prepare_ready.connect(self._clear_stop_event_for_task)
        self._worker.signal_acquisition_summary_ready.connect(self._clear_stop_event_for_summary)
        self._worker.signal_acquisition_failed.connect(self._clear_stop_event_for_task)
        self._worker.signal_acquisition_cancelled.connect(self._clear_stop_event_for_task)
        self._worker.signal_z_stack_result_ready.connect(self._clear_stop_event_for_z_stack_result)
        # 8b) ``signal_start_worker`` 触发 worker.slot_start；跨线程投递。
        self.signal_start_worker.connect(self._worker.slot_start)
        self._thread.start()

    @pyqtSlot(str, dict)
    def _handle_worker_status(self, status: str, payload: dict[str, Any]) -> None:
        """在转发 worker 状态前同步本次实际相机 timing 缓存。"""
        payload_copy = dict(payload or {})
        if status == "camera_config_applied":
            camera_config = payload_copy.get("camera_config")
            if isinstance(camera_config, dict):
                self._latest_camera_timing = payload_copy
        self.signal_status_changed.emit(str(status), payload_copy)

    @staticmethod
    def _create_adapters(backend: BackendConfig):
        """根据 ``backend.simulation_mode`` 创建真实或仿真 adapter 四件套。

        返回：
            ``(camera_adapter, slm_adapter, daq_adapter, stage_adapter)`` 元组。
        """
        bundle = create_adapter_bundle(backend)
        return bundle.camera, bundle.slm, bundle.daq, bundle.stage

    def _get_series_writer(self) -> Any:
        """Create the shared asynchronous series writer only when diagnostics need it."""
        if self._series_writer is None:
            self._series_writer = self._series_writer_factory()
        return self._series_writer

    def shutdown(self) -> None:
        """停止后台 worker、按 Qt 生命周期顺序断开相机、SLM 与 DAQ。"""
        # 1) 标记 shutdown，使其它槽函数知道不要再启动新任务。
        self._shutdown_requested = True
        # 2) 取消所有活跃 stop_event；忽略异常，确保后续清理动作不被中断。
        try:
            self.stop()
        except Exception:
            logger.warning("Failed to request stop during controller shutdown.", exc_info=True)
        # 3) 退出 QThread。``wait(2000)`` 给 worker 2 秒优雅退出窗口。
        self._thread.quit()
        if not self._thread.wait(2000):
            # 取舍（审查报告 #21）：worker 在 2 秒内未退出（可能卡在某个硬件调用）时，
            # 这里只广播 shutdown_timeout 并提前返回，**不**强行断开 camera/SLM/stage——
            # 卡住的 worker 很可能仍持有同一硬件句柄，并发断开会与之竞争、可能损坏
            # R11 WinUSB / DCAM 会话或句柄状态；句柄交由进程退出兜底回收。真机复现
            # shutdown 超时、确认卡住时句柄状态后，再评估是否改为 best-effort 断开。
            self.signal_status_changed.emit("shutdown_timeout", {})
            return
        # 4) Worker 已退出后才可有界关闭共享 writer，避免与仍在 submit 的 worker 竞争。
        if self._series_writer is not None:
            try:
                self._series_writer.shutdown(timeout=2.0)
            except SeriesWriteTimeout as timeout_error:
                try:
                    self._series_writer.wait_for_terminal(timeout=2.0)
                except Exception:
                    logger.warning(
                        "Series writer shutdown timed out and terminal recovery failed: %s",
                        timeout_error,
                        exc_info=True,
                    )
            except RuntimeError:
                # A finalize command may already be pending after its caller timed out.
                try:
                    self._series_writer.wait_for_terminal(timeout=2.0)
                    self._series_writer.shutdown(timeout=2.0)
                except Exception:
                    logger.warning("Failed to close series writer during shutdown.", exc_info=True)
            except Exception:
                logger.warning("Failed to close series writer during shutdown.", exc_info=True)
        # 5) 主动断相机和 SLM；DAQ 没有显式 disconnect 接口。
        try:
            self.camera_adapter.disconnect()
        except Exception:
            logger.warning("Failed to disconnect camera during controller shutdown.", exc_info=True)
        try:
            self.slm_adapter.disconnect()
        except Exception:
            logger.warning("Failed to disconnect SLM during controller shutdown.", exc_info=True)
        try:
            self.stage_adapter.disconnect()
        except Exception:
            logger.warning("Failed to disconnect stage during controller shutdown.", exc_info=True)

    def initialize_hardware(self) -> None:
        """同步初始化相机和 SLM，并广播状态。注：真实硬件可能阻塞数秒。"""
        self.signal_status_changed.emit("hardware_initializing", {})
        self.camera_adapter.initialize()
        self.slm_adapter.initialize()
        self.signal_status_changed.emit("hardware_initialized", {})

    def initialize_camera(self) -> None:
        """仅初始化相机（用于 GUI 中"重连相机"按钮的轻量入口）。"""
        self.camera_adapter.initialize()
        self.signal_status_changed.emit("camera_initialized", {})

    def refresh_available_camera_devices(self) -> list[dict[str, Any]]:
        """刷新 GUI 相机下拉框：返回当前可见的相机列表（不更改连接状态）。"""
        return self.camera_adapter.list_devices()

    def connect_camera(self, device_index: int | None = None, device_label: str = "") -> dict[str, Any]:
        """连接相机并在成功后广播 ``camera_connected`` 状态。"""
        self._latest_camera_timing = {}
        info = self.camera_adapter.connect(device_index=device_index, device_label=device_label)
        self.signal_status_changed.emit("camera_connected", info)
        return info

    def disconnect_camera(self) -> None:
        """断开相机并广播 ``camera_disconnected`` 状态，GUI 据此切换按钮可用性。"""
        try:
            self.camera_adapter.disconnect()
        finally:
            # 即使 SDK 断连报错，也不能再复用与旧连接绑定的 timing。
            self._latest_camera_timing = {}
        self.signal_status_changed.emit("camera_disconnected", {})

    def camera_connection_info(self) -> dict[str, Any]:
        return self.camera_adapter.connection_info()

    def arm_camera(self, frame_count: int = 9) -> None:
        """把相机切到 ``armed`` 状态；正式 SIM9 采集前必须为 9 帧。"""
        self.camera_adapter.arm(frame_count=frame_count)
        self.signal_status_changed.emit("camera_armed", {"frame_count": frame_count})

    def disarm_camera(self) -> None:
        """从 ``armed`` 状态退出；GUI 调试或采集结束后调用。"""
        self.camera_adapter.disarm()
        self.signal_status_changed.emit("camera_disarmed", {})

    def refresh_available_daq_devices(self) -> list[str]:
        """刷新 DAQ 设备下拉：返回 NI MAX 中识别到的设备名列表。"""
        return self.daq_adapter.list_devices(default_device=self.daq_config.device_name)

    def refresh_available_lines(self, device_name: str | None = None) -> list[str]:
        """刷新某 DAQ 设备 port0 的 16 条 line 名，GUI 用于线位下拉的候选项。"""
        selected_device = device_name or self.daq_config.device_name
        return self.daq_adapter.list_port0_lines(device_name=selected_device, default_device=self.daq_config.device_name)

    def apply_daq_config(self, config: DaqLineConfig) -> None:
        """校验并保存 DAQ 线位配置，广播 ``daq_config_applied``。

        副作用：
            ``validate_daq_line_config`` 失败时直接抛 ValueError，GUI 应当捕获并提示用户。
        """
        validate_daq_line_config(config)
        self.daq_config = config
        self.signal_status_changed.emit("daq_config_applied", {"device_name": config.device_name})

    def apply_camera_config(self, config: CameraConfig) -> dict[str, Any]:
        """把当前 UI 相机参数下发到 adapter，并回传"实际生效"的配置 dict。

        副作用：
            - 调 ``camera.apply_config`` 触发 DCAM 写属性，可能阻塞数十 ms。
            - 把 adapter 回报的 timing 缓存到 ``_latest_camera_timing``，供下次启动 worker 计算有效帧间隔。
        """
        # 1) 触发模式限制：项目目前只支持 external_level。
        if config.trigger_mode != "external_level":
            raise HardwareError("Only external_level trigger mode is supported.")
        # 2) 缓存配置 → 下发 → 回写 → 广播。
        self.camera_config = config
        result = self.camera_adapter.apply_config(config) or {}
        _apply_camera_result_to_config(config, result)
        payload = {"camera_config": dict(config.__dict__), **dict(result)}
        self._latest_camera_timing = dict(payload)
        self.signal_status_changed.emit("camera_config_applied", payload)
        if bool(result.get("timing_fallback_used")):
            self.signal_status_changed.emit(
                "camera_timing_fallback_warning",
                {
                    "message": str(
                        result.get("timing_fallback_reason")
                        or "Camera timing properties are unavailable; using 50 ms fallback."
                    ),
                    **dict(payload),
                },
            )
        return dict(result)

    def refresh_available_slm_devices(self) -> list[dict[str, str]]:
        """刷新 SLM 设备下拉：返回 WinUSB 总线上可见的 R11 设备列表。"""
        return self.slm_adapter.list_devices()

    def connect_slm(self, device_path: str | None = None) -> dict[str, Any]:
        """连接 SLM；成功后广播 ``slm_connected`` 状态。"""
        info = self.slm_adapter.connect(device_path=device_path)
        self.signal_status_changed.emit("slm_connected", info)
        return info

    def disconnect_slm(self) -> None:
        """断开 SLM；GUI 据此切按钮颜色 + 阻止后续 RO 选择。"""
        # 先关找样品激光（no-op 安全），再清缓存的 immediate 扫描结果——否则陈旧 immediate
        # index 会继续喂给正式选择的 exclude_indices（即便 GUI 也会清，这里保证 controller 自洽）。
        # 关光失败（DAQ 拉低异常）不应阻断 SLM 断开（激光线在 DAQ 侧，断 SLM 不影响它）；
        # 记录后继续，stop_immediate_live 内部已保留状态供后续入口重试拉低。
        try:
            self.stop_immediate_live()
        except Exception as stop_error:  # noqa: BLE001
            logger.warning("stop_immediate_live failed during SLM disconnect (laser may still be ON): %s", stop_error)
        self._immediate_ro_scan = []
        self._immediate_ro_indices = set()
        self._immediate_live_ro_indices = set()
        self.slm_adapter.disconnect()
        self.signal_status_changed.emit("slm_disconnected", {})

    def slm_connection_info(self) -> dict[str, Any]:
        return self.slm_adapter.connection_info()

    def connect_stage(self) -> dict[str, Any]:
        """Connect the Nikon Ti2 ZDrive adapter and broadcast the current position."""
        info = self.stage_adapter.connect()
        self.signal_status_changed.emit("z_stage_connected", dict(info or {}))
        return info

    def disconnect_stage(self) -> None:
        self.stage_adapter.disconnect()
        self.signal_status_changed.emit("z_stage_disconnected", {})

    def get_stage_position_um(self) -> float:
        return float(self.stage_adapter.get_position_um())

    def move_stage_to_um(self, target_um: float) -> None:
        self.stage_adapter.move_z_um(float(target_um))
        self.signal_status_changed.emit("z_stage_moved", {"z_um": float(target_um)})

    def prepare_patterns(self, pattern_files: list[str], device_path: str | None = None) -> PatternPreparationResult:
        """把文件型 9 帧 pattern 编程到 SLM；正式采集应优先用 Running Order 路径。"""
        # 1) 调 adapter.program_patterns，得到包含 handles 的 PatternPreparationResult。
        self.pattern_result = self.slm_adapter.program_patterns(pattern_files, device_path=device_path)
        # 2) 广播状态，便于 GUI 显示 handle 数与准备时间。
        self.signal_status_changed.emit(
            "patterns_prepared",
            {
                "handles": list(self.pattern_result.handles),
                "prepared_at": self.pattern_result.prepared_at,
            },
        )
        return self.pattern_result

    def select_running_order_for_task(self, wavelength_nm: int, exposure_us: int) -> dict[str, Any]:
        """按任务波长 + 曝光自动选择预烧录 Running Order 并写入 controller。

        抛出：
            ``HardwareError``：没匹配到 RO 或 SLM 未连接时；GUI 据此弹错误对话框。
        """
        # 1) 枚举 SLM 上烧录的 RO 列表 → 调命名解析器找出最佳匹配；显式排除找样品 immediate RO。
        running_orders = self.slm_adapter.list_running_orders()
        ro_index, ro_name, warnings = find_best_running_order(
            running_orders,
            wavelength_nm=int(wavelength_nm),
            exposure_us=int(exposure_us),
            exclude_indices=set(self._immediate_ro_indices),
        )
        if ro_index is None:
            raise HardwareError("; ".join(warnings) or "No matching SLM running order found.")
        # 2) 把选中的 RO 写到 SLM；返回值可能包含 PatternPreparationResult 或仅元数据。
        result = self.slm_adapter.select_running_order(ro_index)
        # 3) 规整成统一 PatternPreparationResult；下游 acquisition_core 才能消费。
        pattern_result = _coerce_running_order_pattern_result(result, ro_index, ro_name)
        self.pattern_result = pattern_result
        # 4) 打包 GUI 显示用 payload 并广播。
        payload = _running_order_payload(result, self.pattern_result, ro_index, ro_name, warnings)
        self.signal_status_changed.emit("running_order_selected", payload)
        return payload

    # ---- immediate-live（找样品）支持 -------------------------------------
    # 注意：以下 4 个方法只驱动「找样品」用的 immediate RO + 激光线，绝不能与正式
    # 采集/诊断波形并存——``NIDaqAdapter.set_line`` 是整 port 写入（拉高一路会把其余
    # SIM TTL 写 0）。调用方（主 GUI）负责在采集/诊断入口先 ``stop_immediate_live``。

    def refresh_immediate_running_orders(self) -> list[dict[str, Any]]:
        """连接后扫描全部 RO+激活类型并缓存；返回可用于找样品的 ``ACT_IMMEDIATE`` 列表。

        只在后台连接 worker 里调用（逐个 select 读 activation type，有 USB 往返）。
        缓存供 GUI 线程纯读（``list_immediate_running_orders``）与正式选择排除使用。
        """
        scan = list(self.slm_adapter.list_running_orders_with_activation())
        self._immediate_ro_scan = scan
        self._immediate_ro_indices = {
            int(item["index"]) for item in scan if item.get("is_immediate")
        }
        self._immediate_live_ro_indices = {
            int(item["index"])
            for item in scan
            if item.get("is_immediate")
            and _parse_immediate_live_ro_name(str(item.get("name", ""))) is not None
        }
        return self._immediate_running_orders_from_cache()

    def _preview_ro_item_from_scan(self, item: dict[str, Any]) -> dict[str, Any] | None:
        name = str(item.get("name", ""))
        parsed = _parse_immediate_live_ro_name(name)
        if parsed is None:
            return None
        enriched = dict(item)
        enriched["parsed_wavelength_nm"] = int(parsed["wavelength_nm"])
        enriched["preview_slot"] = str(parsed["slot"])
        enriched["selectable"] = bool(item.get("is_immediate"))
        if not item.get("is_immediate"):
            enriched["diagnostic"] = (
                f"{name}: activation_type={_activation_type_label(item.get('activation_type'))}, "
                "not ACT_IMMEDIATE; set this Running Order to immediate in MetroCon, "
                "then compile and Send to board."
            )
        return enriched

    def _immediate_running_orders_from_cache(self, *, include_inactive_preview: bool = False) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for item in self._immediate_ro_scan:
            enriched = self._preview_ro_item_from_scan(item)
            if enriched is None:
                continue
            if not include_inactive_preview and not item.get("is_immediate"):
                continue
            items.append(enriched)
        return items

    def list_immediate_running_orders(self, wavelength_nm: int | None = None) -> list[dict[str, Any]]:
        """纯读缓存返回可用于找样品的 immediate RO 列表；不在此重新访问 SLM。

        每项附 ``parsed_wavelength_nm``（前导波长，解析不出为 None）。传 ``wavelength_nm``
        时附 ``selectable``（当前波长匹配才为 True；638 可接受旧 647 RO），由 GUI 决定 enable/disable；
        解析不出波长的项 ``selectable=False``（不可激活，避免错配激光线）。
        """
        items = self._immediate_running_orders_from_cache()
        if wavelength_nm is None:
            return items
        target = int(wavelength_nm)
        for item in items:
            parsed = item.get("parsed_wavelength_nm")
            item["selectable"] = bool(item.get("is_immediate")) and immediate_live_wavelength_matches(
                parsed,
                target,
            )
        return items

    def list_immediate_dropdown_running_orders(self) -> list[dict[str, Any]]:
        """返回找样品下拉的展示项，包含命名正确但未烧成 immediate 的诊断项。"""
        return self._immediate_running_orders_from_cache(include_inactive_preview=True)

    def immediate_running_order_warnings(self, wavelength_nm: int | None = None) -> list[str]:
        """列出命名正确但当前不可用于找样品的 RO，供 GUI 连接成功后提示。"""
        warnings: list[str] = []
        target = int(wavelength_nm) if wavelength_nm is not None else None
        for item in self.list_immediate_dropdown_running_orders():
            parsed = item.get("parsed_wavelength_nm")
            if target is not None and not immediate_live_wavelength_matches(parsed, target):
                continue
            diagnostic = item.get("diagnostic")
            if diagnostic:
                warnings.append(str(diagnostic))
        return warnings

    @property
    def is_immediate_live_active(self) -> bool:
        return bool(self._immediate_live_active)

    @property
    def is_immediate_live_engaged(self) -> bool:
        """controller 是否仍可能持有"已写高/已 arming"的找样品激光线（fail-safe 权威判定）。

        = 激活态 ``OR`` 仍记着 arming line。后者覆盖"激活失败但清理拉低也失败、保留 line 待
        重试"的情形：此时 ``_immediate_live_active`` 仍为 False，但激光线可能已写高且未拉低。
        正式采集互锁必须据本判定 fail-safe 拦截（GUI active 标志不足以反映该残留态）。
        等价于 ``stop_immediate_live`` no-op 守卫条件的逻辑反面。
        """
        return bool(self._immediate_live_active or self._immediate_live_line is not None)

    def activate_immediate_running_order(self, ro_index: int, wavelength_nm: int) -> dict[str, Any]:
        """选中并激活 immediate RO，并把对应波长激光线拉高（持续照明找样品）。

        controller 级校验（不依赖 GUI 过滤）：``ro_index`` 必须在命名正确且缓存确认为
        immediate-live 的索引内，且其前导波长必须与 ``wavelength_nm`` 兼容（638 可用旧 647 RO）。选中 RO 后会
        再读取本次 ``activation_type``，确认仍为 ``ACT_IMMEDIATE`` 才点灯。先记 line
        （arming）再 set_line 高；任何失败都 best-effort 拉低并清状态后再抛出，杜绝
        「激光开着但状态没记上」。
        """
        ro_index = int(ro_index)
        wavelength_nm = int(wavelength_nm)
        if ro_index not in self._immediate_live_ro_indices:
            raise HardwareError(f"RO index {ro_index} is not an immediate running order.")
        entry = next(
            (item for item in self._immediate_ro_scan if int(item.get("index", -1)) == ro_index),
            None,
        )
        ro_name = str((entry or {}).get("name", ""))
        parsed = _parse_immediate_live_ro_name(ro_name)
        parsed_wavelength = None if parsed is None else int(parsed["wavelength_nm"])
        if not immediate_live_wavelength_matches(parsed_wavelength, wavelength_nm):
            raise HardwareError(
                f"Immediate RO '{ro_name}' wavelength ({parsed_wavelength}) does not match "
                f"selected {wavelength_nm} nm; refusing to drive laser."
            )
        role = LASER_ROLE_MAP.get(wavelength_nm)
        if role is None:
            raise HardwareError(f"Unsupported laser wavelength: {wavelength_nm} nm.")
        device, _port, line_index = parse_line_name(getattr(self.daq_config, role))
        result = self.slm_adapter.select_running_order(ro_index)
        selected_name = str(result.get("running_order_name", ro_name))
        if selected_name != ro_name:
            self._immediate_live_ro_indices.discard(ro_index)
            raise HardwareError(
                f"Immediate RO index {ro_index} changed from '{ro_name}' to '{selected_name}'; "
                "refresh SLM Running Orders before driving laser."
            )
        activation_type = result.get("activation_type")
        try:
            activation_type_value = int(activation_type) if activation_type is not None else None
        except (TypeError, ValueError):
            activation_type_value = None
        if not is_immediate_activation_type(activation_type_value):
            self._immediate_ro_indices.discard(ro_index)
            self._immediate_live_ro_indices.discard(ro_index)
            if entry is not None:
                entry["activation_type"] = activation_type_value
                entry["is_immediate"] = False
            raise HardwareError(
                f"Immediate RO '{ro_name}' is no longer ACT_IMMEDIATE "
                f"(activation_type={_activation_type_label(activation_type_value)}); "
                "set it to immediate in MetroCon, compile, Send to board, then reconnect SLM."
            )
        # 先记意图（arming）：即使 set_line 写高后抛错，stop_immediate_live 也能据此拉低。
        self._immediate_live_line = (device, int(line_index))
        try:
            self.slm_adapter.activate_prepared_patterns()
            self.daq_adapter.set_line(device, int(line_index), high=True)
        except Exception:
            # best-effort 拉低（整 port），再清状态，最后上抛让 GUI 回退下拉。
            reset_ok = False
            try:
                self.daq_adapter.set_all_low(device)
                reset_ok = True
            except Exception as reset_error:  # noqa: BLE001 - 收尾失败只记日志
                logger.error(
                    "Failed to reset DAQ low after immediate activate error; keeping arming line for retry: %s",
                    reset_error,
                )
            self._immediate_live_active = False
            # 关键安全：仅在确认拉低成功后才清 arming line；拉低失败时保留 _immediate_live_line，
            # 使后续 stop_immediate_live 能据此重试拉低（激光线可能已写高，不能丢失该信息）。
            if reset_ok:
                self._immediate_live_line = None
            raise
        self._immediate_live_active = True
        self.signal_status_changed.emit(
            "immediate_live_activated",
            {"running_order_index": ro_index, "running_order_name": ro_name, "wavelength_nm": wavelength_nm},
        )
        return {"running_order_index": ro_index, "running_order_name": ro_name, "wavelength_nm": wavelength_nm}

    def stop_immediate_live(self) -> None:
        """关掉 immediate-live 激光（整 port 拉低）并清状态；未 active 且无 arming line 时严格 no-op。

        no-op 很关键：正式采集/诊断入口会无条件调本方法，若此时并无 immediate-live，
        绝不能写 port（否则会干扰即将/正在播放的波形）。
        """
        if not self._immediate_live_active and self._immediate_live_line is None:
            return
        device = self._immediate_live_line[0] if self._immediate_live_line else self.daq_config.device_name
        try:
            self.daq_adapter.set_all_low(device)
        except Exception as reset_error:  # noqa: BLE001
            # 关键安全：拉低失败时绝不清状态——否则下次 stop 撞 no-op、无法重试，激光可能卡高。
            # 保留 active/line 供后续入口重试，并以 HardwareError 向上暴露硬件风险。
            logger.error(
                "Failed to set DAQ low while stopping immediate live; keeping state for retry: %s",
                reset_error,
            )
            raise HardwareError(
                f"Failed to pull laser line low while stopping immediate live: {reset_error}"
            ) from reset_error
        self._immediate_live_active = False
        self._immediate_live_line = None
        self.signal_status_changed.emit("immediate_live_stopped", {})

    def reset_all_daq_low(self) -> None:
        """把 DAQ 全 port 拉低（冷启动安全复位）；调用方负责 best-effort 包裹与空闲判定。"""
        self.daq_adapter.set_all_low(self.daq_config.device_name)

    def start_single_acquisition(
        self,
        task: SimTaskConfig,
        *,
        prepare_running_order: bool = False,
        initialize_hardware: bool = False,
        apply_daq_config: bool = False,
        apply_camera_config: bool = True,
        z_scan_config: ZScanConfig | None = None,
        z_scan_enabled: bool | None = None,
        reconstruction_config: ReconstructionConfig | None = None,
        focus_output_path: str | Path | None = None,
        focus_csv_path: str | Path | None = None,
    ) -> str:
        """启动单次 SIM9 正式采集；返回新分配的 ``task_id``。"""
        selected_z_scan_config = z_scan_config or self.z_scan_config
        selected_z_scan_enabled = (
            bool(selected_z_scan_config.enabled)
            if z_scan_enabled is None
            else bool(z_scan_enabled)
        )
        if selected_z_scan_enabled and (
            z_scan_config is not None
            or z_scan_enabled is not None
            or focus_output_path is not None
        ):
            # Autofocus is safety-sensitive: every hardware/config/RO preflight is
            # mandatory regardless of legacy caller flags.
            prepare_running_order = True
            initialize_hardware = True
            apply_daq_config = True
            apply_camera_config = True
        if not apply_camera_config:
            raise ValueError(
                "Formal SIM9 acquisition requires apply_camera_config=True "
                "so current camera timing is read before waveform construction."
            )
        return self._start_worker_task(
            task,
            prepare_running_order=prepare_running_order,
            initialize_hardware=initialize_hardware,
            apply_daq_config=apply_daq_config,
            apply_camera_config=apply_camera_config,
            prepare_only=False,
            z_scan_config=z_scan_config,
            z_scan_enabled=z_scan_enabled,
            reconstruction_config=reconstruction_config,
            focus_output_path=focus_output_path,
            focus_csv_path=focus_csv_path,
        )

    def start_z_stack_acquisition(
        self,
        task: SimTaskConfig,
        *,
        output_path: str | Path,
        z_scan_config: ZScanConfig,
        prepare_running_order: bool = True,
        initialize_hardware: bool = True,
        apply_daq_config: bool = True,
        apply_camera_config: bool = True,
    ) -> str:
        """Start raw-only formal SIM9 acquisition at every configured Z position."""
        if not bool(z_scan_config.enabled):
            raise ValueError("Z-stack acquisition requires z_scan_config.enabled=True.")
        if bool(z_scan_config.select_focus_plane):
            raise ValueError("Z-stack acquisition requires select_focus_plane=False.")
        if not str(output_path).strip():
            raise ValueError("Z-stack output_path must not be empty.")
        if not all(
            (
                prepare_running_order,
                initialize_hardware,
                apply_daq_config,
                apply_camera_config,
            )
        ):
            raise ValueError("Z-stack acquisition requires every hardware preflight flag to be True.")
        return self._start_worker_task(
            task,
            prepare_running_order=bool(prepare_running_order),
            initialize_hardware=bool(initialize_hardware),
            apply_daq_config=bool(apply_daq_config),
            apply_camera_config=bool(apply_camera_config),
            prepare_only=False,
            z_scan_config=z_scan_config,
            z_scan_enabled=True,
            reconstruction_config=ReconstructionConfig(enabled=False),
            acquisition_mode="z_stack",
            z_stack_output_path=output_path,
        )

    def start_prepare_experiment(
        self,
        task: SimTaskConfig,
        *,
        prepare_running_order: bool = True,
        initialize_hardware: bool = True,
        apply_daq_config: bool = True,
        apply_camera_config: bool = True,
    ) -> str:
        """启动"准备实验"路径：默认开启全部预备阶段，不真正触发 9 帧采集。"""
        return self._start_worker_task(
            task,
            prepare_running_order=prepare_running_order,
            initialize_hardware=initialize_hardware,
            apply_daq_config=apply_daq_config,
            apply_camera_config=apply_camera_config,
            prepare_only=True,
        )

    def _start_worker_task(
        self,
        task: SimTaskConfig,
        *,
        prepare_running_order: bool,
        initialize_hardware: bool,
        apply_daq_config: bool,
        apply_camera_config: bool,
        prepare_only: bool,
        z_scan_config: ZScanConfig | None = None,
        z_scan_enabled: bool | None = None,
        reconstruction_config: ReconstructionConfig | None = None,
        acquisition_mode: str = "single",
        z_stack_output_path: str | Path | None = None,
        focus_output_path: str | Path | None = None,
        focus_csv_path: str | Path | None = None,
    ) -> str:
        """``start_*`` 系列的统一实现：组装 payload、做配置校验、生成 task_id、投递信号。"""
        # 1) 只有完整配置签名匹配时才复用缓存；preview 的固定 1 ms timing、旧 ROI
        #    或断线前结果都不能影响本次正式 task。worker 若重新 apply，会在 build 前
        #    用本次硬件回报再次覆盖。
        task.timing.inter_frame_gap_us = effective_inter_frame_gap_us(
            _matching_camera_timing_recommendation(self._latest_camera_timing, task.camera)
        )
        selected_z_scan_config = z_scan_config or self.z_scan_config
        selected_z_scan_enabled = (
            bool(selected_z_scan_config.enabled) if z_scan_enabled is None else bool(z_scan_enabled)
        )
        if prepare_only:
            selected_z_scan_enabled = False
        elif selected_z_scan_enabled:
            # Formal Z-scan modes always begin at the stage position sampled at
            # task execution time.  Keep ``start_um`` only as a legacy schema
            # field; never dispatch a persisted machine coordinate to hardware.
            selected_z_scan_config = replace(selected_z_scan_config, start_um=None)
        if acquisition_mode not in {"single", "z_stack"}:
            raise ValueError(f"Unsupported acquisition mode: {acquisition_mode!r}")
        if (
            acquisition_mode == "single"
            and not prepare_only
            and selected_z_scan_enabled
            and not bool(selected_z_scan_config.select_focus_plane)
        ):
            raise ValueError(
                "Z-scan with select_focus_plane=False must use start_z_stack_acquisition()."
            )
        if acquisition_mode == "z_stack":
            if not selected_z_scan_enabled or not bool(selected_z_scan_config.enabled):
                raise ValueError("Z-stack acquisition requires an enabled z_scan_config.")
            if bool(selected_z_scan_config.select_focus_plane):
                raise ValueError("Z-stack acquisition requires select_focus_plane=False.")
            if z_stack_output_path is None or not str(z_stack_output_path).strip():
                raise ValueError("Z-stack output_path must not be empty.")
        if focus_csv_path is not None and focus_output_path is None:
            raise ValueError("focus_csv_path requires focus_output_path.")
        if focus_output_path is not None:
            if not str(focus_output_path).strip():
                raise ValueError("focus_output_path must not be empty.")
            if (
                acquisition_mode != "single"
                or not selected_z_scan_enabled
                or not bool(selected_z_scan_config.select_focus_plane)
            ):
                raise ValueError("Focus diagnostics require enabled autofocus Z-scan mode.")
        # 2) 防御：模式语义明确后，非 RO 路径才要求既有 pattern handles。
        if not prepare_running_order and not self.pattern_result.handles:
            raise HardwareError("Patterns must be prepared before acquisition.")
        selected_reconstruction_config = reconstruction_config or self.reconstruction_config
        # 3) 选定的 pattern_result：RO 路径稍后由 worker 重新生成；非 RO 路径用 controller 持有的。
        pattern_result = self.pattern_result if not prepare_running_order else PatternPreparationResult()
        pattern_files = list(task.pattern_files)
        selected_running_order = task.running_order_name
        # 3a) 如果 controller 现持有的就是 RO 模式 pattern_result，则把它的 RO 名/pattern 同步到 task。
        if pattern_result.metadata.get("mode") == "running_order":
            selected_running_order = selected_running_order or str(
                pattern_result.metadata.get("running_order_name", "")
            )
            pattern_files = list(pattern_result.pattern_files)
            task.running_order_name = selected_running_order
        # 4) 把所有相关字段拼成 AppConfig，并跑通用配置校验；任何失败立即拒绝启动。
        validation_config = AppConfig(
            daq=self.daq_config,
            camera=task.camera,
            timing=task.timing,
            backend=self.backend,
            z_scan=selected_z_scan_config,
            reconstruction=selected_reconstruction_config,
            pattern_files=pattern_files,
            selected_running_order=selected_running_order,
            selected_laser_nm=task.laser_wavelength_nm,
            red_laser_nm=int(self.red_laser_nm),
        )
        validation_errors = validate_app_config(validation_config)
        if validation_errors:
            raise ValueError("Invalid SIM acquisition config: " + "; ".join(validation_errors))
        series_writer = None
        if acquisition_mode == "z_stack" or focus_output_path is not None:
            series_writer = self._get_series_writer()
        # 5) 生成 task_id 与 stop_event，把 stop_event 写入两个簿记字段。
        task_id = new_task_id()
        stop_event = threading.Event()
        self._active_stop_events[task_id] = stop_event
        self._current_stop_event = stop_event
        # 6) 快照 task / daq_config / z_scan_config：防止 GUI 线程在 worker 运行期间
        #    修改同一个可变对象（竞态）。adapter 引用是共享句柄，不拷贝（设计约束）。
        task_snap = copy.copy(task)
        task_snap.timing = copy.copy(task.timing)
        task_snap.camera = copy.copy(task.camera)
        daq_snap = copy.copy(self.daq_config)
        z_scan_snap = copy.copy(selected_z_scan_config)
        # 7) 组装 payload；adapter 引用从 controller 自身取，保证 SLM/相机共享同一连接。
        payload = {
            "task_id": task_id,
            "task": task_snap,
            "daq_config": daq_snap,
            "pattern_result": pattern_result,
            "waveform_builder": self.waveform_builder,
            "daq_adapter": self.daq_adapter,
            "camera_adapter": self.camera_adapter,
            "slm_adapter": self.slm_adapter,
            "stage_adapter": self.stage_adapter,
            "z_scan_config": z_scan_snap,
            "z_scan_enabled": selected_z_scan_enabled,
            "stop_event": stop_event,
            "prepare_running_order": bool(prepare_running_order),
            "initialize_hardware": bool(initialize_hardware),
            "apply_daq_config": bool(apply_daq_config),
            "apply_camera_config": bool(apply_camera_config),
            "prepare_only": bool(prepare_only),
            "acquisition_mode": acquisition_mode,
            "z_stack_output_path": (
                Path(z_stack_output_path) if z_stack_output_path is not None else None
            ),
            "focus_output_path": (
                Path(focus_output_path) if focus_output_path is not None else None
            ),
            "focus_csv_path": Path(focus_csv_path) if focus_csv_path is not None else None,
            "series_writer": series_writer,
            # immediate（找样品）RO 索引快照：worker 正式选 FINISH RO 时据此排除。
            "immediate_ro_indices": set(self._immediate_ro_indices),
        }
        # 7) 通过跨线程信号投递给 worker；本函数立即返回，GUI 不会被阻塞。
        self.signal_start_worker.emit(payload)
        return task_id

    @property
    def is_busy(self) -> bool:
        """True when at least one acquisition task is active (started but not yet cleared)."""
        return bool(self._active_stop_events)

    def stop(self) -> None:
        """取消当前 / 所有活跃 worker；置位 stop_event 让采集核心尽快退出。"""
        # 1) 当前 task 优先：先置位最近一次的 stop_event，提示 acquisition_core 立即取消。
        if self._current_stop_event is not None:
            self._current_stop_event.set()
        # 2) 兜底：遍历所有活跃 stop_event 全部置位（防御多 task 排队的情况）。
        for stop_event in list(self._active_stop_events.values()):
            stop_event.set()
        self.signal_status_changed.emit("stop_requested", {})

    def _clear_stop_event_for_summary(self, payload: dict[str, Any]) -> None:
        """signal_acquisition_summary_ready 的清理回调；避免 controller 接收 raw stack。"""
        task_id = str((payload or {}).get("task_id", ""))
        self._clear_stop_event_for_task(task_id, "")

    def _clear_stop_event_for_z_stack_result(self, result: Any) -> None:
        """Clear a parent task after its lightweight Z-stack terminal result arrives."""
        self._clear_stop_event_for_task(str(getattr(result, "task_id", "")), "")

    def _clear_stop_event_for_task(self, task_id: str, _message: str = "") -> None:
        """从 ``_active_stop_events`` 字典里移除已完成的 task；并在字典空时清 _current。"""
        # 1) 已知 task_id 直接 pop；缺失也安全。
        if task_id:
            self._active_stop_events.pop(str(task_id), None)
        # 2) 字典空 → 没有 active task；清 ``_current_stop_event`` 防止误用。
        if not self._active_stop_events:
            self._current_stop_event = None

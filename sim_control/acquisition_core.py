"""脱离 Qt 的单次 SIM 9 帧采集核心。

作用：
    本文件提供 ``run_single_acquisition()``：一段**不依赖 PyQt**的纯 Python 流程，
    把 SIM9 采集拆成"校验取消 → 应用相机配置 → arm → 激活 SLM 图案 → 播放 DAQ
    波形 → 读取 9 帧 stack → 校验 stack/timestamps"七个阶段，并在 ``finally``
    中**保证**相机 disarm 与 DAQ 全低（不论是否异常）。控制器（GUI 侧）和测试
    都直接复用本函数，因此硬件动作必须通过 adapter 协议注入。

协作关系：
    上游：``controller.SimAcquisitionWorker`` 在 QThread 中调用本函数。
    下游：依赖 ``protocols.CameraAdapter`` / ``SlmAdapter`` / ``DaqAdapter`` 三个
          Protocol；具体实现可以是真实 adapter（``adapters.py``）或仿真
          adapter（``sim_adapters.py``）。
    相关：``waveform.NIDaqWaveformBuilder`` 构建一次采集所需的 ``WaveformPlan``。

关键概念：
    - ``AcquisitionCancelled``：表示采集被用户/stop_event 主动取消，与
      ``HardwareError`` 严格区分；GUI 应根据异常类型决定是否提示"硬件故障"。
    - ``stop_event``：``threading.Event`` 风格的可选取消信号；本模块每一步都
      会检查它，以最大限度地缩短 stop 响应时间。
    - ``StatusCallback`` / ``FrameCallback``：纯函数风格状态回调，让本模块对
      Qt 完全无知，便于在测试中收集事件。

维护要点：
    - **必须**保留 ``finally`` 中的 disarm + ``set_all_low``，否则硬件会停在
      触发高电平或激光高电平状态。
    - 修改 9 帧约束前，请同步 ``waveform.NIDaqWaveformBuilder.build()`` 的
      ``frame_count=9`` 默认与所有调用方约定。
    - 不要在本文件做磁盘 IO、GUI 调用、相机预览启停；这些属于上层职责。
"""

from __future__ import annotations

from typing import Any, Callable

import numpy as np

from .adapters import HardwareError
from .models import (
    AcquisitionBatch,
    CameraConfig,
    DaqLineConfig,
    PatternPreparationResult,
    SimTaskConfig,
    ZScanConfig,
    new_task_id,
)
from .protocols import CameraAdapter, DaqAdapter, SlmAdapter
from .waveform import NIDaqWaveformBuilder, validate_daq_line_config
from .z_scan_core import ZScanCancelled, ZScanResult, run_z_scan

# 状态回调签名：``(status_name, payload_dict) -> None``。
# 用 ``Callable`` 而不是直接绑定 Qt signal，保持本模块 Qt 无感。
StatusCallback = Callable[[str, dict[str, Any]], None]
# 帧捕获回调签名：``(frame_index, timestamp) -> None``；通常由 worker 转发到 GUI。
FrameCallback = Callable[[int, float], None]


class AcquisitionCancelled(RuntimeError):
    """采集流程被用户/stop_event 主动取消的领域异常。

    与 ``HardwareError`` 严格区分：GUI 据此可以静默处理（不弹"硬件错误"），
    而真正的硬件失败应当让用户看到原因。
    """
    pass


def _noop_status(status: str, payload: dict[str, Any]) -> None:
    """默认状态回调占位：在没有 GUI 时让采集核心仍可运行。"""
    pass


def _stop_requested(stop_event: Any | None) -> bool:
    """统一检查 stop 信号，兼容 ``threading.Event`` 与测试里的轻量替身。"""
    # ``stop_event`` 可能是 None（未传）；先判断引用再调 ``is_set``，避免 NoneType.is_set AttributeError。
    return bool(stop_event is not None and stop_event.is_set())


def _raise_if_cancelled(stop_event: Any | None) -> None:
    """如果 stop_event 已置位，立即抛出 ``AcquisitionCancelled``。

    用途：
        在每个长阶段之间调用一次，让取消请求尽快被传播到 ``finally`` 收尾路径。
    """
    if _stop_requested(stop_event):
        raise AcquisitionCancelled("Acquisition cancelled.")


def _validate_acquisition_result(stack: Any, timestamps: list[float], expected_frames: int) -> np.ndarray:
    """校验相机返回的 stack/timestamps 是否符合 SIM9 输出协议。

    协议：
        - stack 必须是 ``np.ndarray``、3 维、形状 ``(frame_count, H, W)``、dtype ``uint16``。
        - timestamps 长度必须等于 ``expected_frames``。

    抛出：
        ``HardwareError``：任一校验失败都包装成硬件错误，由 worker 上抛到 GUI。
    """
    # 1) 类型检查：必须是 NumPy 数组，否则后续 pipeline 接口都不成立。
    if not isinstance(stack, np.ndarray):
        raise HardwareError("Camera returned a non-NumPy acquisition stack.")
    # 2) 维度检查：必须是 (frame_count, H, W) 三维。
    if stack.ndim != 3:
        raise HardwareError(f"Camera returned stack with ndim={stack.ndim}; expected 3.")
    # 3) 帧数检查：必须等于 expected_frames（SIM9 路径为 9）。
    if stack.shape[0] != expected_frames:
        raise HardwareError(f"Camera returned {stack.shape[0]} frames; expected {expected_frames}.")
    # 4) 尺寸检查：H、W 必须 ≥1，避免 0×0 帧造成下游崩溃。
    if stack.shape[1] <= 0 or stack.shape[2] <= 0:
        raise HardwareError(f"Camera returned invalid frame shape {stack.shape[1:]}.")
    # 5) dtype 检查：必须 uint16，与项目协议链路一致。
    if stack.dtype != np.uint16:
        raise HardwareError(f"Camera returned stack dtype {stack.dtype}; expected uint16.")
    # 6) timestamps 长度检查：与 frame 数一致，避免错位。
    if len(timestamps) != expected_frames:
        raise HardwareError(f"Camera returned {len(timestamps)} timestamps; expected {expected_frames}.")
    return stack


# 核心采集流程保持脱离 Qt，便于 GUI worker、测试和未来自动化入口复用同一套硬件顺序。
def run_single_acquisition(
    task: SimTaskConfig,
    daq_config: DaqLineConfig,
    pattern_result: PatternPreparationResult,
    camera: CameraAdapter,
    slm: SlmAdapter,
    daq: DaqAdapter,
    waveform_builder: NIDaqWaveformBuilder | None = None,
    task_id: str | None = None,
    on_status: StatusCallback = _noop_status,
    stop_event: Any | None = None,
    stage_adapter: Any | None = None,
    z_scan_config: ZScanConfig | None = None,
    z_scan_pattern_result: PatternPreparationResult | None = None,
) -> AcquisitionBatch:
    """同步运行一次 9 帧 SIM 采集；不依赖 Qt。

    失败时抛出领域异常（``AcquisitionCancelled`` 或来自 adapter 的
    ``HardwareError``），并在 ``finally`` 中始终 disarm 相机、把 DAQ 置低。

    参数：
        task: SIM 任务配置（波长、9 帧 pattern、相机/时序）。
        daq_config: DAQ 线位配置（已经过 GUI 校验）。
        pattern_result: SLM 图案/Running Order 准备结果（必须先经
            ``SlmAdapter.program_patterns()`` 或 ``select_running_order()``）。
        camera/slm/daq: 注入的 adapter，决定真实/仿真行为。
        waveform_builder: 可选，缺省时本函数自建一个。
        task_id: 可选；缺省自动生成。
        on_status: 状态回调（status, payload）。
        stop_event: 可选取消信号。

    返回：
        ``AcquisitionBatch``：包含 ``stack``（``(9, H, W)`` uint16）、时间戳列表、
        相关元数据。
    """
    # 1) 兜底实参：调用方未传时本函数负责创建波形 builder 与 task id。
    if waveform_builder is None:
        waveform_builder = NIDaqWaveformBuilder()
    if task_id is None:
        task_id = new_task_id()

    # 2) 广播"采集开始"事件，便于 GUI 切换状态显示。
    on_status("acquisition_starting", {"task_id": task_id, "laser_wavelength_nm": task.laser_wavelength_nm})

    # 3) 构建 9 帧 SIM 波形计划。生产路径不需要 role_matrix 以节省内存。
    plan = waveform_builder.build(
        daq_config=daq_config,
        timing=task.timing,
        laser_wavelength_nm=task.laser_wavelength_nm,
        exposure_us=task.camera.exposure_us,
        frame_count=9,
        include_role_matrix=False,
    )
    on_status("waveform_ready", {"task_id": task_id, "sample_count": plan.sample_count, "duration_s": plan.duration_s})
    # 4) builder 收集的告警逐条广播；上层日志/GUI 据此提示用户。
    for warning in getattr(plan, "warnings", []):
        on_status("waveform_warning", {"task_id": task_id, "message": warning})

    # 5) 记录"已上报的帧索引"集合，避免 finally 路径补发已发过的 frame_captured 事件。
    emitted_frames: set[int] = set()
    z_scan_result: ZScanResult | None = None

    def emit_frame_captured(frame_index: int, timestamp: float) -> None:
        emitted_frames.add(int(frame_index))
        on_status("frame_captured", {"task_id": task_id, "frame_index": int(frame_index), "timestamp": float(timestamp)})

    def restore_formal_running_order_after_z_scan(status: str = "running_order_restored") -> None:
        formal_ro_index = pattern_result.metadata.get("running_order_index")
        if formal_ro_index is not None:
            slm.select_running_order(int(formal_ro_index))
            on_status(
                status,
                {
                    "task_id": task_id,
                    "running_order_index": int(formal_ro_index),
                    "running_order_name": pattern_result.metadata.get("running_order_name", ""),
                },
            )
        elif pattern_result.metadata.get("mode") == "running_order":
            raise HardwareError("Cannot restore formal SIM running order after z-scan: missing running_order_index.")

    def try_restore_formal_running_order_after_z_scan_failure() -> str | None:
        try:
            restore_formal_running_order_after_z_scan("running_order_restored_after_z_scan_failure")
            return None
        except Exception as restore_exc:
            warning = (
                "Z-scan failed and formal SIM running order could not be restored; "
                f"SLM may still be on z-scan RO: {restore_exc}"
            )
            on_status(
                "running_order_restore_warning",
                {
                    "task_id": task_id,
                    "message": warning,
                },
            )
            return warning

    if z_scan_config is not None and z_scan_config.enabled:
        if stage_adapter is None:
            raise HardwareError("Z-scan is enabled but no Z stage adapter is available.")
        if z_scan_pattern_result is None:
            raise HardwareError("Z-scan is enabled but no z-scan running order is selected.")
        on_status(
            "z_scan_starting",
            {
                "task_id": task_id,
                "exposure_preset_ms": int(z_scan_config.exposure_preset_ms),
                "z_scan_running_order_name": z_scan_pattern_result.metadata.get("running_order_name", ""),
            },
        )
        try:
            z_scan_result = run_z_scan(
                stage_adapter=stage_adapter,
                camera_adapter=camera,
                slm_adapter=slm,
                daq_adapter=daq,
                daq_config=daq_config,
                camera_config=task.camera,
                timing=task.timing,
                z_scan_config=z_scan_config,
                waveform_builder=waveform_builder,
                stop_event=stop_event,
                on_status=on_status,
            )
        except ZScanCancelled as exc:
            restore_warning = try_restore_formal_running_order_after_z_scan_failure()
            if restore_warning:
                raise HardwareError(f"Acquisition cancelled.\n{restore_warning}") from exc
            raise AcquisitionCancelled("Acquisition cancelled.") from exc
        except Exception as exc:
            restore_warning = try_restore_formal_running_order_after_z_scan_failure()
            if restore_warning:
                raise HardwareError(f"{exc}\n{restore_warning}") from exc
            raise

        restore_formal_running_order_after_z_scan()

    # 6) 主流程：arm → 激活 → 播波形 → 读帧。任何阶段抛错或取消都要走 finally 收尾。
    try:
        _raise_if_cancelled(stop_event)
        # 6a) 把 task.camera 参数下发给相机（曝光、ROI、bit depth、超时）。
        camera.apply_config(task.camera)
        _raise_if_cancelled(stop_event)
        # 6b) arm：相机进入"等待外触发"状态，frame_count 必须与 9 一致。
        camera.arm(frame_count=9)
        _raise_if_cancelled(stop_event)
        # 6c) 激活 SLM 图案或 Running Order。
        slm.activate_prepared_patterns()
        _raise_if_cancelled(stop_event)
        # 6d) 播放 DAQ 波形：USB-6423 输出同步 TTL；播放期间相机/SLM 协同工作。
        try:
            daq.play_waveform(daq_config.device_name, plan, stop_event=stop_event)
        except Exception as exc:
            # stop_event 已置位时，把任意异常都视为取消（DAQ 抛 TaskAbort 等情况）。
            if _stop_requested(stop_event):
                raise AcquisitionCancelled("Acquisition cancelled.") from exc
            raise
        _raise_if_cancelled(stop_event)
        # 6e) 从相机读取 9 帧 stack 与时间戳；取消语义同上。
        try:
            stack, timestamps = camera.read_frame_sequence(
                frame_count=9,
                pattern_files=pattern_result.pattern_files,
                laser_wavelength_nm=task.laser_wavelength_nm,
                frame_callback=emit_frame_captured,
                stop_event=stop_event,
            )
        except Exception as exc:
            if _stop_requested(stop_event):
                raise AcquisitionCancelled("Acquisition cancelled.") from exc
            raise
        _raise_if_cancelled(stop_event)
    finally:
        # 7) 不论是否异常都执行 disarm + set_all_low：避免相机停在 arm、DAQ 停在高电平。
        try:
            camera.disarm()
        except Exception:
            pass
        try:
            daq.set_all_low(daq_config.device_name)
        except Exception:
            pass

    # 8) stack/timestamps 必须满足项目协议；任意不符立即抛 HardwareError。
    stack = _validate_acquisition_result(stack, timestamps, expected_frames=9)

    # 9) 部分相机 SDK 不会逐帧回调 frame_callback，这里"补发"缺漏的事件，
    #    保证 GUI 永远收到 9 个 frame_captured 信号。
    for index, timestamp in enumerate(timestamps, start=1):
        if index not in emitted_frames:
            emit_frame_captured(index, timestamp)

    # 10) 广播"采集完成"事件，并返回打包好的 AcquisitionBatch。
    on_status("acquisition_complete", {"task_id": task_id, "stack_shape": list(stack.shape)})

    metadata = {
        "pattern_handles": list(pattern_result.handles),
        "pattern_mode": pattern_result.metadata.get("mode", ""),
        "running_order_name": task.running_order_name
        or str(pattern_result.metadata.get("running_order_name", "")),
        "waveform": plan.metadata,
        "daq_device": daq_config.device_name,
    }
    if z_scan_result is not None:
        metadata["z_scan"] = {
            "best_z_um": z_scan_result.best_z_um,
            "focus_curve": [(point.z_um, point.focus_score) for point in z_scan_result.focus_curve],
            "exposure_actual_us": z_scan_result.exposure_actual_us,
            "exposure_preset_ms": int(z_scan_config.exposure_preset_ms) if z_scan_config else None,
        }

    return AcquisitionBatch(
        task_id=task_id,
        stack=stack,
        timestamps=timestamps,
        laser_wavelength_nm=task.laser_wavelength_nm,
        exposure_us=task.camera.exposure_us,
        pattern_files=list(pattern_result.pattern_files),
        metadata=metadata,
    )

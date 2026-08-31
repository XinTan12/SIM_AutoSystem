"""NI USB-6423 SIM9 同步 TTL 波形生成。

作用：
    本文件实现 SIM9 采集的"软件级 DAQ 时序设计"：
        1. 解析配置里的 ``DevX/port0/lineN`` 文本线名为结构化 ``(device, port, line_index)``。
        2. 校验 8 个 DAQ 角色之间没有线号冲突、都在 port0、都在同一设备、line_index 在 0..15 范围。
        3. 按相机曝光、SLM enable / trigger / finish、激光 TTL 和帧间隔生成一段
           ``packed_port_values``（``uint32`` 端口字节级波形），让
           ``NIDaqAdapter.play_waveform()`` 直接写到 USB-6423 port0。
    波形 builder **只生成数据计划，不直接持有 NI 任务**，因此可以在主线程或测试里调
    用而不引起硬件副作用。

协作关系：
    上游：``controller.py`` / ``acquisition_core.py`` / ``adapters.py``
          （触发波形构建与播放）、``tests/test_waveform.py``。
    下游：``models.DAQ_ROLE_ORDER``、``models.DaqLineConfig``、
          ``models.LASER_ROLE_MAP``、``models.TimingConfig``。

关键概念：
    - ``LINE_PATTERN``：解析 ``"Dev1/port0/line5"`` 为正则命名捕获。
    - ``role_matrix``：诊断模式下生成的"每个 role 一个 uint8 数组"，便于打印/可视化。
    - ``packed_port_values``：实际写到 NI port0 的 ``uint32`` 波形（位 N=1 表示
      line N 在该采样点为高）。
    - 一次 SIM9 采集波形结构：
        ``guard | (exposure + gap) × 9 | guard``
      在曝光窗口内同时拉高 ``camera_trigger`` 与当前激光线；曝光起点拉高
      ``slm_trigger``（持续 ``edge_pulse_us``），曝光终点拉高 ``slm_finish``
      （持续 ``edge_pulse_us``）。

维护要点：
    - 校验函数中的所有异常都用 ``ValueError`` 抛出，调用方在 GUI 里把它转成可读的对话框。
    - 修改任一时序参数前请同步更新 ``summary.py`` 的总时长估算公式，否则界面预估会失真。
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Any

import numpy as np

from .models import (
    DAQ_ROLE_ORDER,
    DaqLineConfig,
    LASER_ROLE_MAP,
    R11_HARDWARE_ACTIVATION_MAX_US,
    TimingConfig,
)


# 正则解析 ``Dev1/port0/line5`` 形式的 NI 线名；命名组让调用方按名取值。
LINE_PATTERN = re.compile(r"^(?P<device>[^/]+)/port(?P<port>\d+)/line(?P<line>\d+)$")


@dataclass
class WaveformPlan:
    """``NIDaqWaveformBuilder.build()`` 的返回结构。

    职责：
        - ``packed_port_values``：写到 NI port0 的 ``uint32`` 波形数组（位 N = line N）。
        - ``role_matrix``：仅诊断模式下生成的逐角色 uint8 数组，便于绘图/测试断言。
        - ``sample_rate_hz``/``sample_count``/``duration_s``：播放时使用的核心元数据。
        - ``metadata``：帧起止 sample index、激光角色、各时序换算结果。
        - ``warnings``：构建期检测到但未致命的隐患（脉冲被截断、曝光太短等）。

    维护要点：
        ``role_matrix`` 在 ``include_role_matrix=False`` 时为空 dict，避免为
        生产路径占用 ``sample_count × 8 × uint8`` 的内存。
    """
    line_order: list[str]
    packed_port_values: np.ndarray
    role_matrix: dict[str, np.ndarray]
    sample_rate_hz: int
    sample_count: int
    duration_s: float
    metadata: dict[str, Any]
    warnings: list[str]


def parse_line_name(line_name: str) -> tuple[str, int, int]:
    """把 ``"Dev1/port0/line5"`` 文本解析为 ``(device, port, line_index)``。

    抛出：
        ``ValueError``：当输入不符合正则模式（缺少 port、缺少 line 或格式不对）。
    """
    # 1) 用预编译正则匹配；不匹配立即抛错避免无声继续。
    match = LINE_PATTERN.match(line_name)
    if not match:
        raise ValueError(f"Invalid NI line name: {line_name}")
    # 2) 命名组拆解；device 保留字符串，port/line 强转为 int 便于后续位运算。
    return match.group("device"), int(match.group("port")), int(match.group("line"))


def validate_daq_line_config(config: DaqLineConfig) -> None:
    """集中校验 DAQ 8 角色配置：同设备、同 port0、line 不重叠、line 在 0..15。

    抛出：
        ``ValueError``：校验未通过时立即抛出，错误消息可直接展示给用户。
    """
    # 1) 累计扫描得到的设备名、port、已用 line 集合，便于后续断言。
    devices = set()
    ports = set()
    seen_lines = set()
    # 2) 遍历 8 个角色，逐条解析并校验：line 索引必须 0..15、不能重复出现。
    for role in DAQ_ROLE_ORDER:
        line_name = getattr(config, role)
        device, port, line_index = parse_line_name(line_name)
        devices.add(device)
        ports.add(port)
        if line_name in seen_lines:
            raise ValueError(f"DAQ line reused: {line_name}")
        seen_lines.add(line_name)
        if not 0 <= line_index <= 15:
            raise ValueError(f"Line index must be between 0 and 15: {line_name}")
    # 3) 所有 8 条线必须落到同一个 USB-6423 设备上。
    if len(devices) != 1:
        raise ValueError("All DAQ lines must belong to the same device.")
    # 4) 项目固定使用 port0；其它端口需要 NI 任务额外通道，违反约定立即拦截。
    if ports != {0}:
        raise ValueError("All DAQ lines must belong to port0.")
    # 5) 配置中的设备名必须与解析出的设备一致，避免拼写错误造成日后调试痛苦。
    if config.device_name not in devices:
        raise ValueError("DAQ device_name does not match selected lines.")


# 波形 builder 只生成数据计划，不直接操作 NI 任务；真实播放在 NIDaqAdapter。
class NIDaqWaveformBuilder:
    """根据 SIM9 时序和线位配置生成 USB-6423 可播放的 ``port0`` 数字波形。

    职责：
        - 校验输入（线位/波长/采样率/曝光）。
        - 把微秒级时序参数折算成采样点数。
        - 在每个曝光窗口拉高对应 line，并在曝光起/止瞬间拉 SLM trigger/finish。
        - 返回 ``WaveformPlan`` 给真实/仿真 DAQ adapter 播放。

    协作：
        被 ``controller.py`` 与 ``acquisition_core.run_single_acquisition`` 直接
        调用；测试中由 ``tests/test_waveform.py`` 全面覆盖。
    """
    def build(
        self,
        daq_config: DaqLineConfig,
        timing: TimingConfig,
        laser_wavelength_nm: int,
        exposure_us: int,
        frame_count: int = 9,
        include_role_matrix: bool = True,
    ) -> WaveformPlan:
        """生成一轮 SIM9 采集的 packed port 波形和诊断元数据。

        参数：
            daq_config: DAQ 线位配置，必须先经 ``validate_daq_line_config`` 通过。
            timing: 采样率、脉冲宽度、帧间隔、保护时间。
            laser_wavelength_nm: 必须在 ``LASER_ROLE_MAP`` 中，否则抛 ValueError。
                第四路红光可为 638 或 647（各机器红光身份不同），两者都映射到中性的
                ``laser_red_line``（line 12）；其余三档固定为 405/488/561。
            exposure_us: 单帧曝光，必须为正。
            frame_count: 默认 9，SIM9 项目固定值。
            include_role_matrix: True 时额外生成每条 line 的 uint8 数组，方便测试和诊断；
                生产路径设 False 以省内存。

        抛出：
            ``ValueError``：DAQ 校验、波长不支持、参数为非正、曝光折算样本为 0。
        """
        # 1) 校验 DAQ 线位、波长、frame_count、曝光、采样率四类输入；任意失败即报错。
        validate_daq_line_config(daq_config)
        if laser_wavelength_nm not in LASER_ROLE_MAP:
            raise ValueError(f"Unsupported laser wavelength: {laser_wavelength_nm}")
        if frame_count <= 0:
            raise ValueError("frame_count must be positive.")
        if exposure_us <= 0:
            raise ValueError("exposure_us must be positive.")
        if timing.sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be positive.")

        # 2) 把四类时长（边沿脉冲 / 帧间隔 / 曝光 / SLM enable guard）一起折算成样本数。
        edge_pulse_samples = self._us_to_samples(timing.edge_pulse_us, timing.sample_rate_hz)
        gap_samples = self._us_to_samples(timing.inter_frame_gap_us, timing.sample_rate_hz)
        exposure_samples = self._us_to_samples(exposure_us, timing.sample_rate_hz)
        guard_samples = self._us_to_samples(timing.slm_enable_guard_us, timing.sample_rate_hz)
        warnings: list[str] = []

        # 3) 曝光折算后必须 >0；过短样本（<10）发警告但仍继续，让真机给出最终判断。
        if exposure_samples <= 0:
            raise ValueError("Exposure converts to zero samples. Increase exposure or sample rate.")
        if exposure_samples < 10:
            warnings.append(
                f"exposure_us={exposure_us} converts to only {exposure_samples} samples at {timing.sample_rate_hz} Hz."
            )
        # 4) 帧间隔小于 1 ms 是保守阈值，提醒用户检查相机读出余量。
        if timing.inter_frame_gap_us < 1000:
            warnings.append(
                f"inter_frame_gap_us={timing.inter_frame_gap_us} is below the conservative 1000 us readout margin."
            )
        # 4b) guard 不超过 R11 tHWAT 上限（500 µs，PD0011CA Table 7-1）时，
        #     [HWA h] RO 可能尚未激活完成，首个 slm_trigger 会被丢弃。
        if timing.slm_enable_guard_us <= R11_HARDWARE_ACTIVATION_MAX_US:
            warnings.append(
                f"slm_enable_guard_us={timing.slm_enable_guard_us} is at or below the R11 hardware "
                f"activation worst case ({R11_HARDWARE_ACTIVATION_MAX_US} us tHWAT); the first SLM "
                "trigger may be lost before the running order becomes active."
            )

        # 5) 总样本数 = 两端 guard + 9 × (曝光 + 帧间隔)；提前一次性算出便于分配数组。
        per_frame_span = exposure_samples + gap_samples
        sample_count = (guard_samples * 2) + (frame_count * per_frame_span)
        # 6) ``role_matrix`` 仅在诊断模式下分配；生产路径直接累加到 packed 数组。
        matrix = (
            {role: np.zeros(sample_count, dtype=np.uint8) for role in DAQ_ROLE_ORDER}
            if include_role_matrix
            else {}
        )
        # 7) packed 数组：每个 sample 用 uint32 表达 port0 所有 16 line 状态，初始全 0。
        packed = np.zeros(sample_count, dtype=np.uint32)
        # 8) 把每个 role 对应的"位掩码"提前算好（``1 << line_index``），减少循环内重复解析。
        line_bits = {
            role: np.uint32(1 << parse_line_name(getattr(daq_config, role))[2])
            for role in DAQ_ROLE_ORDER
        }

        # 9) 选择当前波长对应的激光 role；SLM enable 在整段波形里都保持高电平。
        #    红光 638/647 都经 LASER_ROLE_MAP 落到中性 ``laser_red_line``（line 12）。
        active_laser_role = LASER_ROLE_MAP[laser_wavelength_nm]
        if include_role_matrix:
            matrix["slm_enable_line"][:] = 1
        else:
            packed[:] |= line_bits["slm_enable_line"]

        # 10) 记录每帧 start/end sample index，便于元数据与时序分析。
        frame_starts = []
        frame_ends = []

        # 11) 主循环：对每一帧分别填充曝光段（cam+laser）与 trigger/finish 单脉冲。
        for frame_index in range(frame_count):
            frame_start = guard_samples + (frame_index * per_frame_span)
            frame_end = frame_start + exposure_samples
            frame_starts.append(frame_start)
            frame_ends.append(frame_end)

            # 11a) 曝光窗口内同时拉高 camera trigger 与当前激光线。
            if include_role_matrix:
                matrix["camera_trigger_line"][frame_start:frame_end] = 1
                matrix[active_laser_role][frame_start:frame_end] = 1
            else:
                packed[frame_start:frame_end] |= (
                    line_bits["camera_trigger_line"] | line_bits[active_laser_role]
                )

            # 11b) SLM trigger / finish 单脉冲；``min`` 防止脉冲超过 sample_count 边界。
            trigger_end = min(frame_start + edge_pulse_samples, sample_count)
            finish_end = min(frame_end + edge_pulse_samples, sample_count)
            # 11c) 当 finish 脉冲长度超过帧间间隔，下一帧的 trigger 会和上一帧 finish 重叠 → 警告。
            if frame_index < frame_count - 1 and edge_pulse_samples > gap_samples:
                warnings.append(
                    "slm_finish pulse extends beyond the inter-frame gap before the next frame."
                )
            # 11d) 末帧 finish 脉冲若超过总样本数会被截断 → 警告，便于排查时序异常。
            if frame_end + edge_pulse_samples > sample_count:
                warnings.append("slm_finish pulse is clipped at the end of the waveform.")
            # 11e) 把 trigger / finish 高电平写入对应数组（诊断或生产路径分别处理）。
            if include_role_matrix:
                matrix["slm_trigger_line"][frame_start:trigger_end] = 1
                matrix["slm_finish_line"][frame_end:finish_end] = 1
            else:
                packed[frame_start:trigger_end] |= line_bits["slm_trigger_line"]
                packed[frame_end:finish_end] |= line_bits["slm_finish_line"]

        # 12) 诊断模式下用 role_matrix 重新打包 packed 数组，保证两路语义一致。
        if include_role_matrix:
            packed = self._pack_port_values(daq_config, matrix, sample_count)
        # 13) 计算播放总时长（秒）并把构建期上下文打包到 metadata。
        duration_s = sample_count / float(timing.sample_rate_hz)
        metadata = {
            "frame_count": frame_count,
            "exposure_us": exposure_us,
            "sample_rate_hz": timing.sample_rate_hz,
            "edge_pulse_samples": edge_pulse_samples,
            "inter_frame_gap_samples": gap_samples,
            "slm_enable_guard_samples": guard_samples,
            "frame_start_samples": frame_starts,
            "frame_end_samples": frame_ends,
            "active_laser_role": active_laser_role,
        }
        # 14) 警告列表去重 + 排序，使后续 ``on_status("waveform_warning", ...)`` 顺序稳定。
        return WaveformPlan(
            line_order=list(DAQ_ROLE_ORDER),
            packed_port_values=packed,
            role_matrix=matrix,
            sample_rate_hz=timing.sample_rate_hz,
            sample_count=sample_count,
            duration_s=duration_s,
            metadata=metadata,
            warnings=sorted(set(warnings)),
        )

    def build_z_scan(
        self,
        daq_config: DaqLineConfig,
        timing: TimingConfig,
        exposure_us: int,
        laser_wavelength_nm: int = 488,
        include_role_matrix: bool = True,
    ) -> WaveformPlan:
        """Generate the single-frame z-scan TTL waveform.

        ``slm_enable`` is asserted at sample 0, then after
        ``timing.slm_enable_guard_us`` the SLM trigger edge, camera trigger and
        requested laser window start together. The z-scan RO is not FINISH-looped,
        so this waveform deliberately leaves ``slm_finish`` low.
        """
        validate_daq_line_config(daq_config)
        if exposure_us <= 0:
            raise ValueError("exposure_us must be positive.")
        if timing.sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be positive.")
        laser_wavelength_nm = int(laser_wavelength_nm)
        if laser_wavelength_nm not in LASER_ROLE_MAP:
            raise ValueError(f"Unsupported laser wavelength: {laser_wavelength_nm} nm.")

        edge_pulse_samples = self._us_to_samples(timing.edge_pulse_us, timing.sample_rate_hz)
        exposure_samples = self._us_to_samples(exposure_us, timing.sample_rate_hz)
        guard_samples = self._us_to_samples(timing.slm_enable_guard_us, timing.sample_rate_hz)
        sample_count = (guard_samples * 2) + exposure_samples
        frame_start = guard_samples
        frame_end = frame_start + exposure_samples
        trigger_end = min(frame_start + edge_pulse_samples, sample_count)
        warnings: list[str] = []
        if exposure_samples < edge_pulse_samples:
            warnings.append("z-scan exposure is shorter than the trigger pulse width.")
        # guard 不超过 R11 tHWAT 上限（500 µs）时，z-scan 单帧 trigger 同样可能在
        # RO 硬件激活完成前被丢弃；与 ``build()`` 的告警保持一致措辞。
        if timing.slm_enable_guard_us <= R11_HARDWARE_ACTIVATION_MAX_US:
            warnings.append(
                f"slm_enable_guard_us={timing.slm_enable_guard_us} is at or below the R11 hardware "
                f"activation worst case ({R11_HARDWARE_ACTIVATION_MAX_US} us tHWAT); the first SLM "
                "trigger may be lost before the running order becomes active."
            )

        matrix = (
            {role: np.zeros(sample_count, dtype=np.uint8) for role in DAQ_ROLE_ORDER}
            if include_role_matrix
            else {}
        )
        packed = np.zeros(sample_count, dtype=np.uint32)
        line_bits = {
            role: np.uint32(1 << parse_line_name(getattr(daq_config, role))[2])
            for role in DAQ_ROLE_ORDER
        }
        active_laser_role = LASER_ROLE_MAP[laser_wavelength_nm]

        if include_role_matrix:
            matrix["slm_enable_line"][:frame_end] = 1
            matrix["slm_trigger_line"][frame_start:trigger_end] = 1
            matrix["camera_trigger_line"][frame_start:frame_end] = 1
            matrix[active_laser_role][frame_start:frame_end] = 1
            packed = self._pack_port_values(daq_config, matrix, sample_count)
        else:
            packed[:frame_end] |= line_bits["slm_enable_line"]
            packed[frame_start:trigger_end] |= line_bits["slm_trigger_line"]
            packed[frame_start:frame_end] |= line_bits["camera_trigger_line"] | line_bits[active_laser_role]

        duration_s = sample_count / float(timing.sample_rate_hz)
        metadata = {
            "z_scan": True,
            "frame_count": 1,
            "exposure_us": exposure_us,
            "sample_rate_hz": timing.sample_rate_hz,
            "edge_pulse_samples": edge_pulse_samples,
            "slm_enable_guard_samples": guard_samples,
            "frame_start_samples": [frame_start],
            "frame_end_samples": [frame_end],
            "laser_wavelength_nm": laser_wavelength_nm,
            "active_laser_role": active_laser_role,
        }
        return WaveformPlan(
            line_order=list(DAQ_ROLE_ORDER),
            packed_port_values=packed,
            role_matrix=matrix,
            sample_rate_hz=timing.sample_rate_hz,
            sample_count=sample_count,
            duration_s=duration_s,
            metadata=metadata,
            warnings=sorted(set(warnings)),
        )

    @staticmethod
    def _us_to_samples(microseconds: int, sample_rate_hz: int) -> int:
        """把微秒时长换算成 DAQ 采样点数，并保证非零脉冲至少占一个点。

        与 ``summary.py`` 的同名换算函数必须保持一致，否则界面预估总时长会失真。
        """
        # ``math.ceil`` 保证亚采样的时长仍能产生 ≥1 个样本点。
        return max(1, int(math.ceil((microseconds / 1_000_000.0) * sample_rate_hz)))

    @staticmethod
    def _pack_port_values(
        daq_config: DaqLineConfig,
        matrix: dict[str, np.ndarray],
        sample_count: int,
    ) -> np.ndarray:
        """把每个时间片的 role 高低电平打包成 USB-6423 port0 的 uint32 值。"""
        # 1) 初始化 uint32 数组：每个 sample 一位代表一条 line 的电平。
        packed = np.zeros(sample_count, dtype=np.uint32)
        # 2) 依次把每个 role 的高电平段 OR 到对应 bit 位上。
        for role in DAQ_ROLE_ORDER:
            _, _, line_index = parse_line_name(getattr(daq_config, role))
            packed[matrix[role] != 0] |= np.uint32(1 << line_index)
        return packed

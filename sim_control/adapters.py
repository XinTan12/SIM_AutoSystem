"""真实硬件适配层和厂商 SDK 扩展点。

作用：
    本文件把 SIM 系统三大硬件包装成统一 adapter，对 controller 暴露同一组方法
    （由 ``protocols.py`` 的 Protocol 描述），同时把厂商 DLL、ctypes 签名、
    Running Order 选择、波形播放等细节全部隔离在本文件内：
        - ``FusionBtCameraAdapter``：Hamamatsu ORCA-Fusion BT，走 DCAM-API /
          DCAM-SDK4 的 Python 绑定（``dcam.py`` / ``dcamapi4.py``）。
        - ``KopinSlmAdapter``：Kopin / Forth Dimension Displays ``QXGA-R11-STR``
          SLM，通过 ``R11CommLib`` over WinUSB 厂商栈控制。
        - ``NIDaqAdapter``：NI USB-6423 数字输出，通过 ``nidaqmx`` Python 包播放
          ``WaveformPlan``、保护性 ``set_all_low``、做单线 ``pulse_line`` 测试。
    仿真实现位于 ``sim_adapters.py``；**GUI 不应直接调用厂商 SDK**，只走 adapter。

协作关系：
    上游：``controller.SimAcquisitionController`` 与 ``acquisition_core.run_single_acquisition``。
    下游：``CameraConfig``、``PatternPreparationResult``、``WaveformPlan`` 等数据类，
          ``sim_camera_presets.normalize_sim_camera_roi`` 与 ``build_sim_camera_size_presets``。
    相关：``tests/test_sim_camera_adapter.py``、``test_running_orders.py``、
          ``test_sim_daq_testing.py`` 等覆盖本文件主要分支。

关键概念：
    - ``HardwareError``：本文件唯一对外暴露的领域异常类型，便于 GUI 用 ``except``
      过滤"硬件失败"与其它逻辑错误。
    - Running Order 命名约定：``{波长}_{pitch}_{mode}_{曝光}ms[_ang0]``；正式
      SIM 采集只选 ``_ang0`` 之外、``3.5/2d``、与当前曝光桶一致的 RO。
    - DCAM transfer count 路径：``read_frame_sequence`` 先读 ``cap_transferinfo``
      消费已到 buffer 的帧，再 wait 等待剩余帧到达，避免在已到帧时还 wait timeout。
    - R11 WinUSB GUID 与 page/block 几何常量在文件顶部定义，``_R11CommLib`` 是
      ctypes 薄包装。

维护要点：
    - SDK 路径解析优先级：``BackendConfig`` 中的字符串 → 本文件默认的 ``SDK/``
      子目录 → 抛 ``HardwareError`` 给上层。
    - 任何 DCAM / R11 / nidaqmx 失败都包装成 ``HardwareError``，保留 SDK 原始错误名/码。
    - 不要在本文件做 GUI 调用、QTimer 启动或磁盘 IO（除了 SDK 加载和 R11 bitplane 文件读取）。
    - 修改 Running Order 命名约定前请同步 ``parse_running_order_name`` 正则与
      ``find_best_running_order`` 的筛选条件，并跟踪决策日志 2026-04-26 条目。
"""

from __future__ import annotations

import ctypes
import importlib
import logging
import math
import os
import re
import struct
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np

from .models import CameraConfig, PatternPreparationResult, Z_SCAN_EXPOSURE_PRESETS_MS
from .sim_camera_presets import DEFAULT_SIM_CAMERA_SIZE, SIM_CAMERA_ROI_STEP_PX, build_sim_camera_size_presets
from .waveform import WaveformPlan

logger = logging.getLogger(__name__)

# nidaqmx 在没有 NI runtime 的开发机器上可能 import 失败；用 try/except 兜底，让本模块仍可加载。
# 真正调用 NI 时会通过 ``self._available`` 显式拒绝并抛 HardwareError。
try:
    import nidaqmx
    from nidaqmx.constants import AcquisitionType, LineGrouping
    from nidaqmx.stream_writers import DigitalSingleChannelWriter
except Exception:  # pragma: no cover
    nidaqmx = None
    AcquisitionType = None
    LineGrouping = None
    DigitalSingleChannelWriter = None


class HardwareError(RuntimeError):
    """本文件唯一暴露的领域异常类型。

    用途：
        所有真实硬件、SDK 调用、设备状态相关错误都通过此类抛出；GUI 据此和
        ``AcquisitionCancelled`` 区分。控制器/worker 把它包到 ``signal_acquisition_failed``。
    """
    pass


# 项目根目录（``sim_control/`` 的上一级）。SDK 默认搜索路径以它为基准。
APP_ROOT = Path(__file__).resolve().parent.parent
# R11 WinUSB GUID：``FDD_DevEnumerateWinUSB`` 用它过滤设备总线上的 R11 SLM。
R11_WINUSB_GUID = b"54ED7AC9-CC23-4165-BE32-79016BAFB950"
# QXGA-R11-STR 光栅尺寸：图案上传的 row × col 几何约束。
R11_QXGA_WIDTH = 2048
R11_QXGA_HEIGHT = 1536
# Flash 写入颗粒度：page 2048 字节、block 64 page；erase 必须按 block 对齐。
R11_PAGE_SIZE = 2048
R11_PAGES_PER_BLOCK = 64
# R11 内部图像 flash 起始地址；写入位平面时按 page 编号累加。
R11_IMAGE_BASE = 0x01000000
# 一个位平面占用的字节数（每 8 行像素打成 1 字节）。
R11_BITPLANE_BYTES = (R11_QXGA_WIDTH // 8) * R11_QXGA_HEIGHT
R11_BITPLANE_PAGES = R11_BITPLANE_BYTES // R11_PAGE_SIZE
# 8 位反转查找表：R11 烧录要求把每字节的位顺序反转后再写入。预计算可省一次循环。
_REVERSE_BITS_LUT = bytes(int(f"{index:08b}"[::-1], 2) for index in range(256))
# Fusion BT 实际支持的位深枚举值。我们在 ``_resolve_bit_depth`` 中按此筛选。
_SUPPORTED_CAMERA_BIT_DEPTHS = (8, 10, 12, 14, 16)
# Running Order 名称是预烧录 SLM 序列的唯一可读索引，选择逻辑依赖这个命名约定。
_RUNNING_ORDER_NAME_RE = re.compile(
    r"^(?P<wavelength>\d+)_(?P<pitch>\d+(?:\.\d+)?)_(?P<mode>[A-Za-z0-9]+)_"
    r"(?P<exposure>\d+)ms(?P<single_angle>_ang0)?$"
)
_Z_SCAN_RUNNING_ORDER_NAME_RE = re.compile(
    r"^(?P<wavelength>\d+)_(?P<pitch>\d+(?:\.\d+)?)_(?P<mode>[A-Za-z0-9]+)_"
    r"zscan3p_(?P<exposure>\d+)ms$"
)


def parse_running_order_name(name: str) -> dict[str, Any] | None:
    """把 R11 Running Order 名称解析为结构化字段字典。

    输入示例：
        ``"488_3.5_2d_10ms"`` → ``{wavelength_nm: 488, pitch: "3.5",
        mode: "2d", exposure_ms: 10, single_angle: False}``。
        ``"647_3.5_2d_1ms_ang0"`` → ``single_angle=True``，会被 SIM 选择逻辑排除。

    返回：
        正确解析时返回 dict；命名不符合约定时返回 ``None`` 让调用方跳过。
    """
    # 1) 去首尾空白后跑正则；不匹配立即返回 None。
    match = _RUNNING_ORDER_NAME_RE.match(str(name).strip())
    if match is None:
        return None
    try:
        # 2) 命名组拆解：``wavelength`` 与 ``exposure`` 转 int，``mode`` 统一小写。
        return {
            "wavelength_nm": int(match.group("wavelength")),
            "pitch": match.group("pitch"),
            "mode": match.group("mode").lower(),
            "exposure_ms": int(match.group("exposure")),
            "single_angle": bool(match.group("single_angle")),
        }
    except (TypeError, ValueError):
        # 3) 整数转换失败时同样返回 None，让调用方静默跳过非法名字。
        return None


def parse_z_scan_running_order_name(name: str) -> dict[str, Any] | None:
    """Parse a z-scan dedicated Running Order name."""
    match = _Z_SCAN_RUNNING_ORDER_NAME_RE.match(str(name).strip())
    if match is None:
        return None
    try:
        return {
            "wavelength_nm": int(match.group("wavelength")),
            "pitch": match.group("pitch"),
            "mode": match.group("mode").lower(),
            "exposure_preset_ms": int(match.group("exposure")),
        }
    except (TypeError, ValueError):
        return None


def _target_running_order_exposure_ms(exposure_us: int) -> int:
    """把任务曝光时间归入 SLM repertoire 支持的 1 / 10 / 50 ms RO 桶。

    规则：
        - exposure < 10 ms → 选 1 ms RO 桶（让 RO 在曝光内循环多次）。
        - 10 ms ≤ exposure < 50 ms → 选 10 ms 桶。
        - exposure ≥ 50 ms → 选 50 ms 桶。
    """
    # 显式转 int 容忍 numpy 整数类型，避免 1.0 / 0.0 等浮点边界。
    exposure_us = int(exposure_us)
    if exposure_us < 10_000:
        return 1
    if exposure_us < 50_000:
        return 10
    return 50


def find_z_scan_running_order(
    running_orders: list[tuple[int, str]],
    exposure_preset_ms: int,
) -> tuple[int | None, str, list[str]]:
    """Select the fixed 488 nm three-phase z-scan Running Order."""
    preset = int(exposure_preset_ms)
    warnings: list[str] = []
    if preset not in Z_SCAN_EXPOSURE_PRESETS_MS:
        return None, "", [f"Unsupported z-scan exposure preset: {preset} ms."]

    for index, name in running_orders:
        parsed = parse_z_scan_running_order_name(name)
        if parsed is None:
            continue
        if parsed["wavelength_nm"] != 488:
            continue
        if parsed["pitch"] != "3.5":
            continue
        if parsed["mode"] != "2d":
            continue
        if parsed["exposure_preset_ms"] != preset:
            continue
        return int(index), str(name), warnings

    warnings.append(
        "No matching z-scan running order found for "
        f"488 nm, 3.5/2d, zscan3p, {preset} ms preset."
    )
    return None, "", warnings


def find_best_running_order(
    running_orders: list[tuple[int, str]],
    wavelength_nm: int,
    exposure_us: int,
) -> tuple[int | None, str, list[str]]:
    """按当前波长 + 相机曝光选择最匹配的预烧录 Running Order。

    输入：
        running_orders: SLM ``list_running_orders()`` 返回的 ``(index, name)`` 列表。
        wavelength_nm: 当前任务波长，必须在 405/488/561/647 中。
        exposure_us: 当前任务曝光（微秒），决定走 1ms/10ms/50ms 桶。

    返回：
        ``(ro_index, ro_name, warnings)``：未匹配时 index=None、name 为空、
        warnings 含至少一条说明。
    """
    # 1) 归类目标曝光桶；后续筛选都用这一档 ms 比较。
    target_exposure_ms = _target_running_order_exposure_ms(exposure_us)
    warnings: list[str] = []
    candidates: list[tuple[int, str]] = []
    # 2) 遍历所有 RO，按命名解析逐条筛选：必须是 ``_ang0`` 之外、3.5/2d、波长匹配、曝光桶匹配。
    for index, name in running_orders:
        parsed = parse_running_order_name(name)
        if parsed is None:
            continue
        if parsed["single_angle"]:
            continue
        if parsed["wavelength_nm"] != int(wavelength_nm):
            continue
        if parsed["pitch"] != "3.5":
            continue
        if parsed["mode"] != "2d":
            continue
        if parsed["exposure_ms"] != target_exposure_ms:
            continue
        candidates.append((int(index), str(name)))

    # 3) 若有候选，直接取第一个（同一组多个候选时按 RO 编号顺序）。
    if candidates:
        return candidates[0][0], candidates[0][1], warnings

    # 4) 没有候选 → 把"找不到 RO"作为 warning 返回，让上层翻译成对话框文本。
    warnings.append(
        "No matching SLM running order found for "
        f"{int(wavelength_nm)} nm, {target_exposure_ms} ms bucket, pitch 3.5, mode 2d."
    )
    return None, "", warnings


def _resolve_r11_dll_path(user_path: str = "") -> Path:
    """按配置路径和 SDK 默认目录定位 ``R11CommLib`` 动态库。

    搜索顺序：
        1. 用户在配置中提供的 ``user_path``：
           a. 若是目录 → 在其中找 ``R11CommLib-1.8-x64.dll`` / ``-x86.dll``。
           b. 若是文件 → 直接使用。
        2. 仓库内默认子目录 ``SDK/R11 CD Bundle Mar 2020/.../lib``。
        3. 都找不到 → 抛 ``HardwareError``，提示用户检查 SDK 安装。
    """
    # 1) 用户路径优先；目录则按位数选 dll 名，文件则直接返回。
    if user_path:
        candidate = Path(user_path)
        if candidate.is_dir():
            dll_name = "R11CommLib-1.8-x64.dll" if sys.maxsize > 2**32 else "R11CommLib-1.8-x86.dll"
            dll_path = candidate / dll_name
            if dll_path.exists():
                return dll_path
        if candidate.exists():
            return candidate

    # 2) 回落到仓库默认路径，路径串和原文件一致；不要改动以免误指。
    default_dir = (
        APP_ROOT
        / "SDK"
        / "R11 CD Bundle Mar 2020"
        / "2020-03"
        / "Software"
        / "R11CommLib"
        / "R11CommLib-1.8.189.118"
        / "examples"
        / "msvc"
        / "lib"
    )
    dll_name = "R11CommLib-1.8-x64.dll" if sys.maxsize > 2**32 else "R11CommLib-1.8-x86.dll"
    dll_path = default_dir / dll_name
    if dll_path.exists():
        return dll_path
    # 3) 未找到：抛错并附上检查过的路径，便于用户判断 SDK 是否安装。
    raise HardwareError(f"R11CommLib DLL not found. Checked: {dll_path}")


def _resolve_dcam_python_dir(user_path: str = "") -> Path:
    """按配置路径和 SDK 默认目录定位 Hamamatsu DCAM Python 示例目录。

    搜索顺序：
        1. ``user_path`` 指向 ``dcam.py`` 文件：返回其父目录。
        2. ``user_path`` 指向目录：直接查找或 ``samples/python`` 子目录。
        3. 仓库内默认路径 ``SDK/Hamamatsu_DCAMSDK4_v25056964/dcamsdk4/samples/python``。
    """
    # 1) 用户路径优先：先认 dcam.py 文件，再认 candidate / samples/python 兜底。
    if user_path:
        candidate = Path(user_path)
        if candidate.is_file() and candidate.name.lower() == "dcam.py":
            return candidate.parent
        if candidate.is_dir():
            if (candidate / "dcam.py").exists():
                return candidate
            nested = candidate / "samples" / "python"
            if (nested / "dcam.py").exists():
                return nested

    # 2) 回落仓库默认目录；若包含 dcam.py 则返回。
    default_dir = APP_ROOT / "SDK" / "Hamamatsu_DCAMSDK4_v25056964" / "dcamsdk4" / "samples" / "python"
    if (default_dir / "dcam.py").exists():
        return default_dir
    # 3) 未找到：抛错以促使用户检查 SDK 路径或重装。
    raise HardwareError(f"DCAM Python sample directory not found. Checked: {default_dir}")


# R11 仍支持手动上传 bitplane；正式采集优先使用预烧录 Running Order。
def _load_r11_bitplane_file(path: Path) -> bytes:
    """读取 R11 位平面文件，并返回与 flash page 对齐的 bytes。

    支持两种来源：
        - ``.bmp``：1-bit 单色 BMP，要求尺寸 2048×1536；本函数解析 BMP header、
          读取数据段并把上下颠倒的行序翻正。
        - 其它后缀：必须正好 ``R11_BITPLANE_BYTES`` 字节的原始 bitplane payload。

    抛出：
        ``HardwareError``：BMP header 异常、尺寸不符、位深 != 1 或字节数不匹配时。
    """
    # 1) BMP 分支：手工解析 BMP header（避免依赖 PIL）。
    if path.suffix.lower() == ".bmp":
        with path.open("rb") as handle:
            file_header = handle.read(14)
            info_header = handle.read(40)
            # 1a) header 长度必须正好对齐；不足说明文件不完整。
            if len(file_header) != 14 or len(info_header) != 40:
                raise HardwareError(f"Invalid BMP header: {path}")
            bf_type, _, _, _, bf_off_bits = struct.unpack("<HIHHI", file_header)
            # 1b) BMP 标志 ``BM`` = 0x4D42；不是 BMP 直接拒绝。
            if bf_type != 0x4D42:
                raise HardwareError(f"Unsupported BMP signature: {path}")
            (
                _bi_size,
                bi_width,
                bi_height,
                bi_planes,
                bi_bit_count,
                _bi_compression,
                _bi_size_image,
                _bi_xppm,
                _bi_yppm,
                _bi_clr_used,
                _bi_clr_important,
            ) = struct.unpack("<IIIHHIIIIII", info_header)
            # 1c) R11 期待 2048×1536 1-bit；不符立刻报错并附上实际值。
            if bi_width != R11_QXGA_WIDTH or bi_height != R11_QXGA_HEIGHT:
                raise HardwareError(
                    f"R11 BMP must be {R11_QXGA_WIDTH}x{R11_QXGA_HEIGHT}, got {bi_width}x{bi_height}: {path}"
                )
            if bi_planes != 1 or bi_bit_count != 1:
                raise HardwareError(f"R11 BMP must be 1-bit monochrome: {path}")
            # 1d) 跳到数据段并读取 bitplane 字节；长度必须等于 R11_BITPLANE_BYTES。
            handle.seek(bf_off_bits)
            data = handle.read(R11_BITPLANE_BYTES)
            if len(data) != R11_BITPLANE_BYTES:
                raise HardwareError(f"Unexpected BMP payload size: {path}")
        # 1e) BMP 数据按"自下而上"存储，R11 期望"自上而下"；切行后 reverse 翻正。
        rows = [
            data[row_index * (R11_QXGA_WIDTH // 8):(row_index + 1) * (R11_QXGA_WIDTH // 8)]
            for row_index in range(R11_QXGA_HEIGHT)
        ]
        rows.reverse()
        return b"".join(rows)

    # 2) 非 BMP：必须是原始 bitplane 字节流，长度严格相等。
    payload = path.read_bytes()
    if len(payload) != R11_BITPLANE_BYTES:
        raise HardwareError(
            f"R11 pattern must contain exactly {R11_BITPLANE_BYTES} bytes or be a 1-bit BMP: {path}"
        )
    return payload


def _iter_r11_flash_pages(bitplane_data: bytes):
    """把位平面数据切成 R11 flash page，逐 page 产出。

    布局：
        - R11 每行 ``R11_QXGA_WIDTH/8 = 256`` 字节。
        - 每个 page 是 ``R11_PAGE_SIZE = 2048`` 字节；按"4 路交错"的方式从行内取字节。
        - 取字节时用 ``_REVERSE_BITS_LUT`` 反转每个字节的位序（R11 烧录约定）。

    抛出：
        ``HardwareError``：bitplane 字节布局不能整除产生 partial page 时。
    """
    # 1) 每行字节数；R11 全幅每行 256 字节。
    byte_width = R11_QXGA_WIDTH // 8
    page = bytearray()
    # 2) 遍历所有行，每行按 64 字节分 4 组取列（``col + 0x00 / 0x40 / 0x80 / 0xC0``）。
    for row in range(R11_QXGA_HEIGHT):
        row_data = bitplane_data[row * byte_width:(row + 1) * byte_width]
        for col in range(byte_width // 4):
            # 2a) 把 4 路反转位字节顺序写入 page；满 2048 字节立即 yield 并清缓冲。
            page.append(_REVERSE_BITS_LUT[row_data[col + 0x00]])
            page.append(_REVERSE_BITS_LUT[row_data[col + 0x40]])
            page.append(_REVERSE_BITS_LUT[row_data[col + 0x80]])
            page.append(_REVERSE_BITS_LUT[row_data[col + 0xC0]])
            if len(page) == R11_PAGE_SIZE:
                yield bytes(page)
                page.clear()
    # 3) 末尾若仍有残余 page，说明布局假设错位，立刻报错避免烧录乱码。
    if page:
        raise HardwareError("Unexpected partial R11 flash page produced from bitplane payload.")


# 这一层只包装 R11CommLib 的 ctypes 调用，让上层 KopinSlmAdapter 不直接处理函数签名。
class _R11CommLib:
    """``R11CommLib`` 的 ctypes 薄包装。

    职责：
        - 加载 DLL、绑定函数签名（``argtypes`` / ``restype``）。
        - 统一错误码处理（``FDD_SUCCESS`` 与 ``FDD_SLAVE_EXCEPTION``）。
        - 暴露 ``open_winusb`` / ``set_selected_running_order`` / ``activate_running_order``
          / ``erase_block`` / ``write_flash_page`` / ``burn_flash_page`` 等高层方法。

    维护要点：
        - DLL 路径必须用 ``os.add_dll_directory`` 注册到搜索路径，否则同目录下的
          依赖 DLL 加载会失败。
        - 非 0 返回码统一抛 ``HardwareError``，并尽量附带 SDK 返回的异常文本。
    """
    # SDK 约定的两种返回码：0 表示成功，0x12 表示"从机异常"（需要进一步取消息）。
    FDD_SUCCESS = 0
    FDD_SLAVE_EXCEPTION = 0x12

    def __init__(self, dll_path: Path):
        # 1) 保存 DLL 路径用于后续日志；同时把 DLL 所在目录注册给 Windows 加载器。
        self.dll_path = dll_path
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(str(dll_path.parent))
        # 2) ``WinDLL`` 走 Windows stdcall 约定；R11CommLib 使用该约定。
        self.dll = ctypes.WinDLL(str(dll_path))
        # 3) 把所有函数的 ``argtypes/restype`` 绑定到正确签名，避免 ctypes 走默认猜测。
        self._bind()

    def _bind(self) -> None:
        # 把每个对外函数的参数 / 返回类型逐条绑定；逐条注册而不是宏化以便 grep 出错时定位。
        self.dll.FDD_LibGetVersion.argtypes = [ctypes.c_char_p, ctypes.c_uint8]
        self.dll.FDD_LibGetVersion.restype = ctypes.c_int
        self.dll.FDD_ExcGetMsg.argtypes = [ctypes.POINTER(ctypes.c_char_p)]
        self.dll.FDD_ExcGetMsg.restype = ctypes.c_int
        self.dll.FDD_DevEnumerateWinUSB.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p]
        self.dll.FDD_DevEnumerateWinUSB.restype = ctypes.c_int
        self.dll.FDD_DevGetFirst.argtypes = [ctypes.POINTER(ctypes.c_char_p)]
        self.dll.FDD_DevGetFirst.restype = ctypes.c_int
        self.dll.FDD_DevGetNext.argtypes = [ctypes.POINTER(ctypes.c_char_p)]
        self.dll.FDD_DevGetNext.restype = ctypes.c_int
        self.dll.FDD_DevOpenWinUSB.argtypes = [ctypes.c_char_p, ctypes.c_uint16]
        self.dll.FDD_DevOpenWinUSB.restype = ctypes.c_int
        self.dll.FDD_DevClose.argtypes = []
        self.dll.FDD_DevClose.restype = ctypes.c_int
        self.dll.R11_LibGetVersion.argtypes = [ctypes.c_char_p, ctypes.c_uint8]
        self.dll.R11_LibGetVersion.restype = ctypes.c_int
        self.dll.R11_RpcSysGetSerialNum.argtypes = [ctypes.POINTER(ctypes.c_uint32)]
        self.dll.R11_RpcSysGetSerialNum.restype = ctypes.c_int
        self.dll.R11_RpcSysGetRepertoireName.argtypes = [ctypes.c_char_p, ctypes.c_uint8]
        self.dll.R11_RpcSysGetRepertoireName.restype = ctypes.c_int
        self.dll.R11_RpcSysGetBitplaneCount.argtypes = [ctypes.POINTER(ctypes.c_uint32)]
        self.dll.R11_RpcSysGetBitplaneCount.restype = ctypes.c_int
        self.dll.R11_RpcSysReloadRepertoire.argtypes = []
        self.dll.R11_RpcSysReloadRepertoire.restype = ctypes.c_int
        self.dll.R11_RpcRoGetCount.argtypes = [ctypes.POINTER(ctypes.c_uint16)]
        self.dll.R11_RpcRoGetCount.restype = ctypes.c_int
        self.dll.R11_RpcRoGetName.argtypes = [ctypes.c_uint16, ctypes.c_char_p, ctypes.c_uint8]
        self.dll.R11_RpcRoGetName.restype = ctypes.c_int
        self.dll.R11_RpcRoGetSelected.argtypes = [ctypes.POINTER(ctypes.c_uint16)]
        self.dll.R11_RpcRoGetSelected.restype = ctypes.c_int
        self.dll.R11_RpcRoSetSelected.argtypes = [ctypes.c_uint16]
        self.dll.R11_RpcRoSetSelected.restype = ctypes.c_int
        # ``R11_RpcRoGetActivationType`` 是新版 R11CommLib 才有的函数；旧版本不绑。
        if hasattr(self.dll, "R11_RpcRoGetActivationType"):
            self.dll.R11_RpcRoGetActivationType.argtypes = [ctypes.POINTER(ctypes.c_uint8)]
            self.dll.R11_RpcRoGetActivationType.restype = ctypes.c_int
        # ``R11_RpcRoGetActivationState`` 同为新版函数（AN0027AD §3.26）；旧版本不绑。
        if hasattr(self.dll, "R11_RpcRoGetActivationState"):
            self.dll.R11_RpcRoGetActivationState.argtypes = [ctypes.POINTER(ctypes.c_uint8)]
            self.dll.R11_RpcRoGetActivationState.restype = ctypes.c_int
        self.dll.R11_RpcRoActivate.argtypes = []
        self.dll.R11_RpcRoActivate.restype = ctypes.c_int
        self.dll.R11_RpcRoDeactivate.argtypes = []
        self.dll.R11_RpcRoDeactivate.restype = ctypes.c_int
        self.dll.R11_DevGetProgress.argtypes = [ctypes.POINTER(ctypes.c_uint8)]
        self.dll.R11_DevGetProgress.restype = ctypes.c_int
        self.dll.R11_RpcFlashEraseBlock.argtypes = [ctypes.c_uint32]
        self.dll.R11_RpcFlashEraseBlock.restype = ctypes.c_int
        self.dll.R11_FlashWrite.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint16]
        self.dll.R11_FlashWrite.restype = ctypes.c_int
        self.dll.R11_FlashBurn.argtypes = [ctypes.c_uint32]
        self.dll.R11_FlashBurn.restype = ctypes.c_int

    def _exception_message(self) -> str:
        """从 SDK 取最近一次异常文本；任何异常都安全返回空串。"""
        message = ctypes.c_char_p()
        try:
            self.dll.FDD_ExcGetMsg(ctypes.byref(message))
        except Exception:
            return ""
        return message.value.decode("utf-8", errors="ignore") if message.value else ""

    def _check(self, result: int, function_name: str) -> None:
        """SDK 返回码统一检查：0 通过，非 0 抛 ``HardwareError`` 并附上函数名。"""
        if result == self.FDD_SUCCESS:
            return
        # 当返回 SLAVE_EXCEPTION 时，再向 SDK 要一段更具体的错误文本以便诊断。
        extra = ""
        if result == self.FDD_SLAVE_EXCEPTION:
            message = self._exception_message()
            if message:
                extra = f": {message}"
        raise HardwareError(f"{function_name} failed with code 0x{result:02X}{extra}")

    def get_versions(self) -> dict[str, str]:
        """返回 ``comm_lib`` 与 ``r11_comm_lib`` 两个 DLL 的版本字符串。"""
        comm = ctypes.create_string_buffer(128)
        r11 = ctypes.create_string_buffer(128)
        self._check(self.dll.FDD_LibGetVersion(comm, len(comm)), "FDD_LibGetVersion")
        self._check(self.dll.R11_LibGetVersion(r11, len(r11)), "R11_LibGetVersion")
        return {
            "comm_lib": comm.value.decode("utf-8", errors="ignore"),
            "r11_comm_lib": r11.value.decode("utf-8", errors="ignore"),
        }

    def enumerate_winusb_devices(self) -> list[dict[str, str]]:
        """枚举 R11 WinUSB 设备总线，把 ``id:serial`` 字符串拆成 ``{id, path, serial}`` 字典。"""
        # 1) 触发一次枚举（SDK 内部会扫总线）。
        self._check(self.dll.FDD_DevEnumerateWinUSB(R11_WINUSB_GUID, None, None), "FDD_DevEnumerateWinUSB")
        devices: list[dict[str, str]] = []
        current = ctypes.c_char_p()
        # 2) ``GetFirst`` 取第一个设备，循环 ``GetNext`` 直到 value 为空。
        self._check(self.dll.FDD_DevGetFirst(ctypes.byref(current)), "FDD_DevGetFirst")
        while current.value:
            raw = current.value.decode("utf-8", errors="ignore")
            # 2a) 形如 ``\\?\USB#...:SERIAL``；用 ``:`` 切，左为设备路径、右为序列号。
            device_path, serial = (raw.split(":", 1) + [""])[:2] if ":" in raw else (raw, "")
            devices.append({"id": raw, "path": device_path, "serial": serial})
            current = ctypes.c_char_p()
            self._check(self.dll.FDD_DevGetNext(ctypes.byref(current)), "FDD_DevGetNext")
        return devices

    def open_winusb(self, device_path: str, timeout_ms: int = 1000) -> None:
        """打开指定 device_path 的 R11 WinUSB 设备；阻塞最长 ``timeout_ms``。"""
        self._check(self.dll.FDD_DevOpenWinUSB(device_path.encode("utf-8"), timeout_ms), "FDD_DevOpenWinUSB")

    def close(self) -> None:
        """关闭当前打开的 R11 设备；释放 WinUSB 句柄。"""
        self._check(self.dll.FDD_DevClose(), "FDD_DevClose")

    def get_serial_number(self) -> int:
        """读取已连接 R11 的序列号（数字）。"""
        value = ctypes.c_uint32()
        self._check(self.dll.R11_RpcSysGetSerialNum(ctypes.byref(value)), "R11_RpcSysGetSerialNum")
        return int(value.value)

    def get_bitplane_count(self) -> int:
        """读取 repertoire 暴露的 bitplane 总数（决定 ``program_patterns`` 是否合法）。"""
        value = ctypes.c_uint32()
        self._check(self.dll.R11_RpcSysGetBitplaneCount(ctypes.byref(value)), "R11_RpcSysGetBitplaneCount")
        return int(value.value)

    def get_repertoire_name(self) -> str:
        """读取当前 repertoire 名称（``.repz11`` 文件名）。"""
        buffer = ctypes.create_string_buffer(128)
        self._check(self.dll.R11_RpcSysGetRepertoireName(buffer, len(buffer)), "R11_RpcSysGetRepertoireName")
        return buffer.value.decode("utf-8", errors="ignore")

    def get_running_order_count(self) -> int:
        """返回 repertoire 中 Running Order 个数。"""
        value = ctypes.c_uint16()
        self._check(self.dll.R11_RpcRoGetCount(ctypes.byref(value)), "R11_RpcRoGetCount")
        return int(value.value)

    def get_selected_running_order(self) -> int:
        """返回当前被选中的 Running Order 索引。"""
        value = ctypes.c_uint16()
        self._check(self.dll.R11_RpcRoGetSelected(ctypes.byref(value)), "R11_RpcRoGetSelected")
        return int(value.value)

    def set_selected_running_order(self, index: int) -> None:
        """把指定 RO 选中（仍需 ``activate_running_order`` 才会真正运行）。"""
        self._check(self.dll.R11_RpcRoSetSelected(ctypes.c_uint16(int(index))), "R11_RpcRoSetSelected")

    def get_running_order_name(self, index: int) -> str:
        """读取第 ``index`` 个 RO 的可读名。"""
        buffer = ctypes.create_string_buffer(128)
        self._check(self.dll.R11_RpcRoGetName(index, buffer, len(buffer)), "R11_RpcRoGetName")
        return buffer.value.decode("utf-8", errors="ignore")

    def get_running_order_activation_type(self) -> int | None:
        """读取当前 RO 激活方式；旧版本 SDK 无此函数则返回 None。"""
        if not hasattr(self.dll, "R11_RpcRoGetActivationType"):
            return None
        value = ctypes.c_uint8()
        self._check(self.dll.R11_RpcRoGetActivationType(ctypes.byref(value)), "R11_RpcRoGetActivationType")
        return int(value.value)

    def get_activation_state(self) -> int | None:
        """读取 repertoire/RO 当前激活状态码（AN0027AD §3.26）；旧版本 SDK 返回 None。

        [HWA h] RO 在软件 ``activate_running_order`` 后、EXT_RUN 拉高前预期为
        0x54 MHW（Maintenance – Hardware deactivated）；EXT_RUN 拉高且 tHWAT
        （最长 500 µs）过后应变为 0x56 ACT。
        """
        if not hasattr(self.dll, "R11_RpcRoGetActivationState"):
            return None
        value = ctypes.c_uint8()
        self._check(self.dll.R11_RpcRoGetActivationState(ctypes.byref(value)), "R11_RpcRoGetActivationState")
        return int(value.value)

    def deactivate_running_order(self) -> None:
        """关闭当前 RO，让 SLM 处于待重新配置的状态。"""
        self._check(self.dll.R11_RpcRoDeactivate(), "R11_RpcRoDeactivate")

    def activate_running_order(self) -> None:
        """让 SLM 真正运行当前选定的 Running Order。"""
        self._check(self.dll.R11_RpcRoActivate(), "R11_RpcRoActivate")

    def reload_repertoire(self) -> None:
        """重新加载 repertoire（烧录完位平面后必须调用）。"""
        self._check(self.dll.R11_RpcSysReloadRepertoire(), "R11_RpcSysReloadRepertoire")

    def get_progress(self) -> int:
        """读取上一次后台操作的进度百分比（0..100）。"""
        value = ctypes.c_uint8()
        self._check(self.dll.R11_DevGetProgress(ctypes.byref(value)), "R11_DevGetProgress")
        return int(value.value)

    def erase_block(self, page_address: int) -> None:
        """擦除 page_address 所在的 R11 flash block；后续写 page 才能成功。"""
        self._check(self.dll.R11_RpcFlashEraseBlock(page_address), "R11_RpcFlashEraseBlock")

    def write_flash_page(self, payload: bytes) -> None:
        """把一个 ``R11_PAGE_SIZE`` 字节的 page 写入 SDK 内部 buffer。"""
        if len(payload) != R11_PAGE_SIZE:
            raise HardwareError(f"R11 flash writes require {R11_PAGE_SIZE}-byte pages.")
        # ``from_buffer_copy`` 复制一份 ctypes 可读 buffer，避免 GC 提前回收 payload。
        buffer = (ctypes.c_uint8 * len(payload)).from_buffer_copy(payload)
        self._check(self.dll.R11_FlashWrite(buffer, 0, len(payload)), "R11_FlashWrite")

    def burn_flash_page(self, page_address: int) -> None:
        """把 SDK 内部 buffer 的 page 真正烧到目标 page_address。"""
        self._check(self.dll.R11_FlashBurn(page_address), "R11_FlashBurn")


# AN0027AD §3.26 (p.33) R11_RpcRoGetActivationState 状态码表。
# 0x50/0x51/0x55 是瞬态（transitional），其余为稳态；0x56 ACT 表示 RO 正在执行。
R11_ACTIVATION_STATES = {
    0x50: "RLD(repertoire loading)",
    0x51: "STA(starting)",
    0x52: "MSW(maintenance, software deactivated)",
    0x53: "MHD(maintenance, hardware+software deactivated)",
    0x54: "MHW(maintenance, hardware deactivated)",
    0x55: "PAC(activating)",
    0x56: "ACT(active)",
    0x57: "NRP(no repertoire available)",
}
# 0x56 ACT：诊断/轮询路径用它判断 RO 已真正进入 Active Mode。
R11_ACTIVATION_STATE_ACTIVE = 0x56


def r11_activation_state_name(code: int | None) -> str:
    """把激活状态码翻译为可读名；None 表示旧版 SDK 不支持该查询。"""
    if code is None:
        return "unsupported(R11CommLib lacks R11_RpcRoGetActivationState)"
    return R11_ACTIVATION_STATES.get(int(code), f"unknown(0x{int(code):02X})")


# DAQ adapter 是 USB-6423 的真实输出边界，负责把 WaveformPlan 播放到 port0 数字线。
class NIDaqAdapter:
    """NI USB-6423 数字输出适配器。

    职责：
        - 枚举 NI 设备 / port0 16 条 line。
        - 播放 ``WaveformPlan.packed_port_values`` 到 port0（``CHAN_FOR_ALL_LINES``）。
        - ``set_all_low``：把整个 port0 写 0，确保停止后所有 SIM TTL 归位低。
        - ``set_line``：单线静态置高/低（整端口写），供诊断路径使用。
        - ``pulse_line``：单线短脉冲（复用 ``set_line``），用于 DAQ 设置弹窗诊断按钮。

    维护要点：
        - 环境中没有 ``nidaqmx`` 时 ``self._available=False``，所有方法都立即抛 ``HardwareError``。
        - 播放波形时通过 ``is_task_done`` + ``time.sleep(20 ms)`` 短轮询等待，结合
          ``stop_event`` 让 ``stop()`` 可以尽快取消。
    """
    def __init__(self):
        # ``_available`` 决定后续每个方法的可用性；启动开发机不装 NI 时仍可加载模块。
        self._available = nidaqmx is not None

    def list_devices(self, default_device: str = "Dev1") -> list[str]:
        # NI 不可用 → 直接返回空列表，让 GUI 显示"未检测到设备"。
        if not self._available:
            return []
        try:
            system = nidaqmx.system.System.local()
            return [device.name for device in system.devices]
        except Exception:
            return []

    def list_port0_lines(self, device_name: str | None = None, default_device: str | None = None) -> list[str]:
        # 1) 解析最终设备名：参数 > 默认参数 > ``"Dev1"``。
        selected_device = (device_name or default_device or "Dev1").strip()
        if not selected_device:
            return []
        try:
            # 2) 仅当 ``list_devices`` 报告该设备存在时才返回 16 条 line 名。
            devices = self.list_devices(default_device=selected_device)
            if selected_device not in devices:
                return []
            return [f"{selected_device}/port0/line{i}" for i in range(16)]
        except Exception:
            return []

    def play_waveform(self, device_name: str, plan: WaveformPlan, stop_event: Any | None = None) -> None:
        """用 NI-DAQmx 输出 packed port0 uint32 波形。

        副作用：
            - 打开一个 NI 任务，配置 port0 / 采样率 / FINITE 模式；
            - 用 ``DigitalSingleChannelWriter`` 写入整段波形；
            - ``task.start()`` 后用 ``is_task_done`` + ``time.sleep(20 ms)`` 短轮询，
              ``stop_event`` 被置位时立即 ``task.stop()`` 并 return；
            - 总等待超时 = ``max(5 s, duration+2 s)``，超过则抛 HardwareError。
        """
        if not self._available:
            raise HardwareError("nidaqmx is not available; cannot drive NI hardware.")
        try:
            # 1) ``with nidaqmx.Task()`` 保证任务在退出代码块时被正确清理（停止 + 关闭）。
            with nidaqmx.Task() as task:
                # 2) 把 port0 作为一个通道整体加入，``CHAN_FOR_ALL_LINES`` 让 16 line 同步播放。
                task.do_channels.add_do_chan(
                    f"{device_name}/port0",
                    line_grouping=LineGrouping.CHAN_FOR_ALL_LINES,
                )
                # 3) 配置采样时钟：固定 ``FINITE``，样本数 = WaveformPlan.sample_count。
                task.timing.cfg_samp_clk_timing(
                    rate=plan.sample_rate_hz,
                    sample_mode=AcquisitionType.FINITE,
                    samps_per_chan=plan.sample_count,
                )
                # 4) 单通道 stream writer：禁用 auto_start，必须显式 ``task.start()`` 才输出。
                writer = DigitalSingleChannelWriter(task.out_stream, auto_start=False)
                writer.write_many_sample_port_uint32(plan.packed_port_values.astype(np.uint32, copy=False))
                task.start()
                # 5) 计算等待超时：至少 5 秒，且不少于波形时长 + 2 秒安全余量。
                deadline = time.monotonic() + max(5.0, plan.duration_s + 2.0)
                while True:
                    # 5a) 每次循环先看 stop_event：被置位则立即 stop 并 return，便于 GUI 取消响应。
                    if stop_event is not None and stop_event.is_set():
                        try:
                            task.stop()
                        except Exception:
                            logger.warning("Failed to stop NI task after stop_event was set.", exc_info=True)
                        return
                    # 5b) 任务正常结束 → 直接 return。
                    if task.is_task_done():
                        return
                    remaining_s = deadline - time.monotonic()
                    # 5c) 总超时 → 抛 HardwareError 给上层。
                    if remaining_s <= 0:
                        raise HardwareError(f"Timed out waiting for NI waveform after {max(5.0, plan.duration_s + 2.0):.3f}s.")
                    # 5d) 短睡眠（最长 20 ms）让 CPU 空闲；既保证响应又避免空转。
                    time.sleep(min(0.02, remaining_s))
        except Exception as exc:
            # 6) 内部抛的 HardwareError 直接上抛；其它异常包装成 HardwareError，附原始消息。
            if isinstance(exc, HardwareError):
                raise
            raise HardwareError(f"Failed to play NI waveform: {exc}") from exc

    def set_all_low(self, device_name: str) -> None:
        """把 port0 一次性写 0，确保所有 SIM TTL 归位低电平。

        用途：
            ``acquisition_core`` 的 finally 路径以及 ``stop()`` 后均调用本方法，
            避免相机/SLM/激光因为停在高电平导致硬件不安全状态。
        """
        if not self._available:
            raise HardwareError("nidaqmx is not available; cannot reset NI outputs.")
        try:
            with nidaqmx.Task() as task:
                task.do_channels.add_do_chan(
                    f"{device_name}/port0",
                    line_grouping=LineGrouping.CHAN_FOR_ALL_LINES,
                )
                # 立即写 0 并 auto_start：一次性脉冲，无需后续控制。
                task.write(0, auto_start=True)
        except Exception as exc:
            raise HardwareError(f"Failed to reset NI outputs: {exc}") from exc

    def set_line(self, device_name: str, line_index: int, high: bool) -> None:
        """把单条 port0 line 静态置为高/低。

        注意：
            - 写整端口：除目标 line 外端口上其余所有 line 同时被写 0；当前仅用于
              诊断路径（如 SLM 激活时序测试拉高 ``slm_enable``），不要在正式
              采集波形播放期间调用。
            - NI 静态 DO 在任务关闭后保持最后写入的电平，因此 ``set_line(high=True)``
              返回后线会一直保持高，直到下一次写入或 ``set_all_low``。
        """
        if not self._available:
            raise HardwareError("nidaqmx is not available; cannot set NI outputs.")
        if not 0 <= int(line_index) <= 31:
            raise HardwareError(f"Invalid NI line index: {line_index}")
        line_mask = int(1 << int(line_index)) if high else 0
        try:
            with nidaqmx.Task() as task:
                task.do_channels.add_do_chan(
                    f"{device_name}/port0",
                    line_grouping=LineGrouping.CHAN_FOR_ALL_LINES,
                )
                task.write(line_mask, auto_start=True)
        except Exception as exc:
            raise HardwareError(f"Failed to set NI line {line_index} on {device_name}: {exc}") from exc

    def pulse_line(self, device_name: str, line_index: int, duration_s: float) -> None:
        """单线短脉冲：``0 → 1 → sleep → 0``，主要供 DAQ 测试按钮使用。

        参数校验：
            - ``line_index`` 必须在 [0, 31]；超出范围立刻抛 HardwareError。
            - ``duration_s`` 必须 > 0；否则即"零脉冲"无意义。
        """
        if not self._available:
            raise HardwareError("nidaqmx is not available; cannot pulse NI outputs.")
        if not 0 <= int(line_index) <= 31:
            raise HardwareError(f"Invalid NI line index: {line_index}")
        if duration_s <= 0:
            raise HardwareError(f"Pulse duration must be positive: {duration_s}")

        try:
            # 1) 先把整 port 写 0，确保起点干净。
            self.set_line(device_name, line_index, high=False)
            try:
                # 2) 拉高 → sleep → finally 拉低：完整 0/1/0 脉冲。
                self.set_line(device_name, line_index, high=True)
                time.sleep(duration_s)
            finally:
                self.set_line(device_name, line_index, high=False)
        except HardwareError:
            raise
        except Exception as exc:
            raise HardwareError(f"Failed to pulse NI line {line_index} on {device_name}: {exc}") from exc


# 相机 adapter 同时支持仿真占位和 DCAM SDK 扩展，GUI 只通过统一方法调用它。
class FusionBtCameraAdapter:
    """Hamamatsu ORCA-Fusion BT 相机适配器。

    职责：
        - 封装 DCAM-API / DCAM-SDK4 的初始化、设备枚举、连接 / 断开。
        - 通过 ``apply_config`` 把 ``CameraConfig`` 翻译成 DCAM 属性集：
          ROI、曝光、位深、触发模式、触发极性、Readout speed。
        - 提供 ``start_preview`` / ``read_preview_frame`` / ``stop_preview`` 三件
          套（内部触发 + EDGE）。
        - 提供 ``arm`` / ``read_frame_sequence`` / ``disarm``（外部 LEVEL 触发，
          供 9 帧采集使用）。
        - ``get_supported_bit_depths`` 让 GUI 仅展示真实可用的位深档位。

    协作：
        被 ``SimAcquisitionController`` / ``SimPreviewController`` 共享持有；
        ``SimSettingsDialog`` 也必须复用主窗口持有的实例，避免重复打开 DCAM。
    """
    def __init__(self, sdk_path: str = ""):
        # 1) ``sdk_path`` 来自 ``BackendConfig.fusion_bt_sdk_path``，可为空字符串。
        self.sdk_path = sdk_path
        # 2) 一组运行时字段：DCAM 模块对象、相机句柄、当前状态。
        self._sdk = None
        self._initialized = False
        self._armed = False
        self._frame_count = 0
        self._camera_config = CameraConfig()
        self._dcam = None
        self._dcamapi4 = None
        self._dcam_camera = None
        self._module_dir: Path | None = None
        self._preview_active = False
        self._preview_frame_counter = 0
        self._device_open = False
        self._connected_device_index = 0
        self._connected_device_label = ""
        self._connection_info: dict[str, Any] = {}

    def _initialize_dcam_api(self) -> None:
        """加载 DCAM Python 模块并初始化 SDK；若已初始化则容忍 ``ALREADYINITIALIZED``。"""
        # 1) 把 ``dcam.py``/``dcamapi4.py`` 所在目录加入 sys.path，再 import。
        self._load_dcam_modules()
        # 2) 调 ``Dcamapi.init()``；已初始化时 SDK 返回特定错误名，本函数选择吞掉。
        if not self._dcam.Dcamapi.init():
            last_error = self._dcam.Dcamapi.lasterr()
            error_name = getattr(last_error, "name", str(last_error))
            if "ALREADYINITIALIZED" not in str(error_name):
                raise HardwareError(f"DCAM initialization failed: {error_name}")

    def _camera_string(self, camera: Any, key: Any) -> str:
        """读取 DCAM ``dev_getstring`` 字段，未拿到则返回空串以方便日志拼接。"""
        try:
            value = camera.dev_getstring(key)
        except Exception:
            return ""
        return "" if value is False or value is None else str(value)

    def _close_camera(self) -> None:
        """关闭当前打开的相机，按 preview → disarm → close 顺序清理。"""
        if self._dcam_camera is None:
            return
        # 1) preview 与 arm 状态都尽量先关掉；忽略失败避免阻塞 close。
        try:
            self.stop_preview()
        except Exception:
            logger.warning("Failed to stop DCAM preview while closing camera.", exc_info=True)
        try:
            self.disarm()
        except Exception:
            logger.warning("Failed to disarm DCAM camera while closing camera.", exc_info=True)
        # 2) 调 SDK ``dev_close``；finally 中无条件清运行时字段，让下次 connect 走全新流程。
        try:
            self._dcam_camera.dev_close()
        except Exception:
            logger.warning("Failed to close DCAM camera device.", exc_info=True)
        finally:
            self._dcam_camera = None
            self._device_open = False
            self._connection_info = {}
            self._connected_device_label = ""

    def _build_device_descriptor(self, camera_index: int, camera: Any) -> dict[str, Any]:
        """构造一台相机的 metadata 字典（GUI 下拉显示用）。"""
        idstr = self._dcamapi4.DCAM_IDSTR
        # 把 DCAM 拿到的 model/ID/driver 整理成可读形态；缺失字段不影响下游。
        model = self._camera_string(camera, idstr.MODEL) or "Unknown Camera"
        camera_id = self._camera_string(camera, idstr.CAMERAID) or f"CAM-{camera_index:03d}"
        driver_version = self._camera_string(camera, idstr.DRIVERVERSION)
        display = f"{camera_index}: {model} [{camera_id}]"
        descriptor = {
            "index": camera_index,
            "model": model,
            "camera_id": camera_id,
            "driver_version": driver_version,
            "display": display,
        }
        if self._module_dir is not None:
            descriptor["module_dir"] = str(self._module_dir)
        return descriptor

    def _ensure_camera_open(self, device_index: int | None = None, device_label: str = "") -> dict[str, Any]:
        """如果指定的相机还未打开则打开它，并返回最新的 connection_info。

        幂等：
            同索引相机已打开时直接返回，仅在请求换设备时关掉旧相机再 open 新的。
        """
        # 1) 把 initialize 提前；后续 ``Dcam(...)`` 才能正常工作。
        self.initialize()
        selected_index = int(self._camera_config.device_index if device_index is None else device_index)
        # 2) 已是请求的相机：仅更新 display 字段；避免反复关闭/打开同一设备。
        if self._device_open and self._dcam_camera is not None and self._connected_device_index == selected_index:
            if device_label:
                self._connected_device_label = device_label
                self._connection_info["display"] = device_label
            return dict(self._connection_info)

        # 3) 切换设备：先关旧相机，再 open 新的。
        self._close_camera()
        camera = self._dcam.Dcam(selected_index)
        if not camera.dev_open():
            last_error = camera.lasterr()
            error_name = getattr(last_error, "name", str(last_error))
            raise HardwareError(f"Failed to open DCAM camera {selected_index}: {error_name}")
        # 4) 构造 descriptor，把状态字段全部更新到当前打开的相机。
        descriptor = self._build_device_descriptor(selected_index, camera)
        if device_label:
            descriptor["display"] = device_label
        self._dcam_camera = camera
        self._device_open = True
        self._connected_device_index = selected_index
        self._connected_device_label = descriptor["display"]
        self._connection_info = dict(descriptor)
        self._sdk = dict(descriptor)
        return dict(self._connection_info)

    def _load_dcam_modules(self) -> None:
        """把 DCAM Python 示例目录加入 sys.path，import ``dcam.py``/``dcamapi4.py``。"""
        # 1) 解析模块所在目录（用户配置 → 仓库默认 → 抛错）。
        module_dir = _resolve_dcam_python_dir(self.sdk_path)
        # 2) Windows 上注册 DLL 搜索路径，便于 dcamapi.dll 能被解析。
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(str(module_dir))
            if self.sdk_path:
                candidate = Path(self.sdk_path)
                if candidate.is_dir():
                    os.add_dll_directory(str(candidate))
        # 3) 把示例目录前插到 sys.path[0]，避免被第三方同名模块抢占。
        if str(module_dir) not in sys.path:
            sys.path.insert(0, str(module_dir))
        # 4) 真正 import；失败时抛带提示的 HardwareError。
        try:
            self._dcamapi4 = importlib.import_module("dcamapi4")
            self._dcam = importlib.import_module("dcam")
        except Exception as exc:
            raise HardwareError(
                "Failed to import Hamamatsu DCAM Python modules. "
                "Confirm the DCAM driver is installed and dcamapi.dll is available."
            ) from exc
        self._module_dir = module_dir

    def _set_property(self, prop_id: int, value: float) -> float:
        """写 DCAM 属性；返回 SDK 实际接受的值（可能因步进对齐而调整）。"""
        actual = self._dcam_camera.prop_setgetvalue(prop_id, value)
        if actual is False:
            raise HardwareError(
                f"Failed to set DCAM property {prop_id}: {self._dcam_camera.lasterr().name}"
            )
        return float(actual)

    def _get_property(self, prop_id: int) -> float:
        """读 DCAM 属性；失败时抛 ``HardwareError``。"""
        value = self._dcam_camera.prop_getvalue(prop_id)
        if value is False:
            raise HardwareError(
                f"Failed to query DCAM property {prop_id}: {self._dcam_camera.lasterr().name}"
            )
        return float(value)

    def _try_get_property(self, prop_id: int) -> float | None:
        """``_get_property`` 的容忍版：失败时返回 None，便于读 optional 字段。"""
        try:
            return self._get_property(prop_id)
        except Exception:
            return None

    def _property_text(self, prop_id: int, value: float) -> str:
        """把属性的数值转成 SDK 提供的文本描述（如 readout speed 名称）。"""
        try:
            text = self._dcam_camera.prop_getvaluetext(prop_id, value)
        except Exception:
            return ""
        return "" if text is False or text is None else str(text)

    def _get_property_attr(self, prop_id: int) -> Any | None:
        """读取属性的元信息（min/max/step），便于做 ROI 对齐。"""
        if not hasattr(self._dcam_camera, "prop_getattr"):
            return None
        try:
            attr = self._dcam_camera.prop_getattr(prop_id)
        except Exception:
            return None
        return None if attr is False or attr is None else attr

    @staticmethod
    def _attr_int(attr: Any | None, name: str, default: int) -> int:
        """从 attr 对象上取整数字段；缺失或非法时返回 default。"""
        if attr is None:
            return int(default)
        try:
            return int(round(float(getattr(attr, name))))
        except Exception:
            return int(default)

    @staticmethod
    def _align_long_value(value: int, minimum: int, maximum: int, step: int) -> int:
        """把数值夹到 [minimum, maximum] 后向下对齐到 ``step`` 的整数倍。"""
        minimum = int(minimum)
        maximum = max(minimum, int(maximum))
        step = max(1, int(step))
        clamped = min(max(int(value), minimum), maximum)
        return minimum + ((clamped - minimum) // step) * step

    def _roi_axis_limits(self, offset_prop: int, size_prop: int, fallback_sensor_size: int) -> dict[str, int]:
        """读取一个 ROI 轴（水平或垂直）的 SDK 限制：传感器尺寸、最小尺寸、步进、最大偏移。"""
        offset_attr = self._get_property_attr(offset_prop)
        size_attr = self._get_property_attr(size_prop)
        # 1) 取 size 的 max（即传感器尺寸）和 min；缺失时用 fallback。
        sensor_size = max(1, self._attr_int(size_attr, "valuemax", fallback_sensor_size))
        min_size = max(1, self._attr_int(size_attr, "valuemin", 1))
        # 2) size 步进与 offset 步进通常相同；offset 步进缺失时默认与 size 一致。
        size_step = max(1, self._attr_int(size_attr, "valuestep", SIM_CAMERA_ROI_STEP_PX))
        offset_step = max(1, self._attr_int(offset_attr, "valuestep", size_step))
        max_offset = max(0, self._attr_int(offset_attr, "valuemax", sensor_size - min_size))
        return {
            "sensor_size": sensor_size,
            "min_size": min(min_size, sensor_size),
            "size_step": size_step,
            "offset_step": offset_step,
            "max_offset": max_offset,
        }

    def _resolve_roi_axis(self, requested_offset: int, requested_size: int, limits: dict[str, int]) -> tuple[int, int]:
        """把请求的 (offset, size) 对齐到 SDK 限制；先 size 再 offset。"""
        sensor_size = int(limits["sensor_size"])
        # 1) size 不能超出传感器；按 size_step 向下对齐。
        size = self._align_long_value(
            requested_size,
            limits["min_size"],
            sensor_size,
            limits["size_step"],
        )
        # 2) offset 上界 = 传感器尺寸 - size 与 SDK 给的 max_offset 取较小。
        max_offset = max(0, min(int(limits["max_offset"]), sensor_size - size))
        offset = self._align_long_value(
            requested_offset,
            0,
            max_offset,
            limits["offset_step"],
        )
        return offset, size

    def _resolve_roi_config(self, config: CameraConfig) -> dict[str, Any]:
        """把 ``CameraConfig.roi_*`` 翻译成"applied_roi + 传感器/步进/预设"汇总字典。"""
        dcamapi4 = self._dcamapi4
        fallback_width, fallback_height = DEFAULT_SIM_CAMERA_SIZE
        # 1) 分别读取水平、垂直轴的 SDK 限制（含传感器尺寸、步进）。
        h_limits = self._roi_axis_limits(
            dcamapi4.DCAM_IDPROP.SUBARRAYHPOS,
            dcamapi4.DCAM_IDPROP.SUBARRAYHSIZE,
            fallback_sensor_size=fallback_width,
        )
        v_limits = self._roi_axis_limits(
            dcamapi4.DCAM_IDPROP.SUBARRAYVPOS,
            dcamapi4.DCAM_IDPROP.SUBARRAYVSIZE,
            fallback_sensor_size=fallback_height,
        )
        # 2) 对请求 ROI 做对齐：先 size 再 offset，水平/垂直独立完成。
        roi_x, roi_width = self._resolve_roi_axis(config.roi_x, config.roi_width, h_limits)
        roi_y, roi_height = self._resolve_roi_axis(config.roi_y, config.roi_height, v_limits)
        # 3) 把整组结果打包：applied_roi + 传感器尺寸 + 步进 + 预设列表。
        sensor_width = int(h_limits["sensor_size"])
        sensor_height = int(v_limits["sensor_size"])
        roi_step_px = max(1, int(max(h_limits["offset_step"], v_limits["offset_step"])))
        return {
            "applied_roi": {
                "x": int(roi_x),
                "y": int(roi_y),
                "width": int(roi_width),
                "height": int(roi_height),
            },
            "sensor_width": sensor_width,
            "sensor_height": sensor_height,
            "roi_step_px": roi_step_px,
            "roi_size_presets": list(build_sim_camera_size_presets(sensor_width, sensor_height)),
        }

    def _apply_roi_axis(self, offset_prop: int, size_prop: int, offset: int, size: int) -> None:
        """写 ROI 的一根轴；当新 size 比当前小时先写 size 再写 offset，避免越界。"""
        current_size = self._try_get_property(size_prop)
        # 缩小路径：先写更小的 size，再写 offset，避免 SDK 拒绝"offset+size 越界"。
        if current_size is not None and int(size) < int(round(current_size)):
            self._set_property(size_prop, size)
            self._set_property(offset_prop, offset)
            return
        # 扩大或不变路径：先写 offset 再写 size，避免新 size 因为旧 offset 太大而被裁。
        self._set_property(offset_prop, offset)
        self._set_property(size_prop, size)

    def _apply_roi_config(self, roi_summary: dict[str, Any]) -> None:
        """根据 ``_resolve_roi_config`` 结果把 ROI 写入相机。"""
        dcamapi4 = self._dcamapi4
        applied_roi = roi_summary["applied_roi"]
        # 1) 写 ROI 前先关 SUBARRAY 模式，避免在切换过程中 SDK 拒绝。
        self._set_property(dcamapi4.DCAM_IDPROP.SUBARRAYMODE, dcamapi4.DCAMPROP.MODE.OFF)
        # 2) 分别写水平 / 垂直轴的 offset & size。
        self._apply_roi_axis(
            dcamapi4.DCAM_IDPROP.SUBARRAYHPOS,
            dcamapi4.DCAM_IDPROP.SUBARRAYHSIZE,
            applied_roi["x"],
            applied_roi["width"],
        )
        self._apply_roi_axis(
            dcamapi4.DCAM_IDPROP.SUBARRAYVPOS,
            dcamapi4.DCAM_IDPROP.SUBARRAYVSIZE,
            applied_roi["y"],
            applied_roi["height"],
        )
        # 3) 再次启用 SUBARRAY 模式，让上面写入的 ROI 生效。
        self._set_property(dcamapi4.DCAM_IDPROP.SUBARRAYMODE, dcamapi4.DCAMPROP.MODE.ON)

    def _camera_signature(self) -> str:
        """把 model / camera_id / display 拼成一个大写串，便于 ``_fixed_readout_speed_value`` 做型号识别。"""
        parts = [
            str(self._connection_info.get("model", "")),
            str(self._connection_info.get("camera_id", "")),
            str(self._connected_device_label or ""),
        ]
        return " ".join(part for part in parts if part).upper()

    def _fixed_readout_speed_value(self) -> float:
        """根据相机型号挑选合适的 readout speed 档位。

        策略：
            - C13440 / Flash4.0 → 档位 2。
            - C15440 / Fusion BT → 档位 3。
            - 其它 → SDK 提供的 FASTEST 默认值。
        若 SDK 不认账请求档位，会回退到 FASTEST。
        """
        dcamapi4 = self._dcamapi4
        signature = self._camera_signature()
        requested = None
        # 已知型号优先：根据 signature 关键词锁定档位。
        if "C13440" in signature or "FLASH4.0" in signature or "FLASH 4.0" in signature:
            requested = 2
        elif "C15440" in signature or "FUSION BT" in signature:
            requested = 3
        # 未识别型号回退到 FASTEST：让 SDK 自己挑最快档。
        if requested is None:
            requested = dcamapi4.DCAMPROP.READOUTSPEED.FASTEST
        # 用 prop_queryvalue 验证 SDK 是否接受该档位；接受则用 queried，否则回退 requested。
        try:
            queried = self._dcam_camera.prop_queryvalue(dcamapi4.DCAM_IDPROP.READOUTSPEED, requested)
        except Exception:
            queried = False
        if queried is False:
            return float(requested)
        return float(queried)

    def _supported_bit_depths(self) -> list[int]:
        """查询当前相机实际支持的位深档位（用 ``prop_queryvalue`` 反向验证）。"""
        prop_id = self._dcamapi4.DCAM_IDPROP.BITSPERCHANNEL
        supported: list[int] = []
        # 1) 逐档位 query：SDK 返回值等于请求值则视为支持。
        for candidate in _SUPPORTED_CAMERA_BIT_DEPTHS:
            try:
                queried = self._dcam_camera.prop_queryvalue(prop_id, candidate)
            except Exception:
                queried = False
            if queried is False:
                continue
            try:
                queried_value = int(round(float(queried)))
            except Exception:
                continue
            if queried_value == candidate and candidate not in supported:
                supported.append(candidate)
        # 2) 若 query 全部失败，至少把当前值作为备选返回，避免空集合让上层崩溃。
        if not supported:
            current_value = self._try_get_property(prop_id)
            if current_value is not None:
                supported.append(int(round(current_value)))
        return sorted({int(value) for value in supported})

    def get_supported_bit_depths(self) -> list[int]:
        """对外暴露相机当前可用的位深列表；相机未打开时回退到 [16]。"""
        if not self._device_open or self._dcam_camera is None:
            return [16]
        supported = self._supported_bit_depths()
        return supported or [16]

    def _resolve_bit_depth(self, requested_bit_depth: int) -> tuple[list[int], int]:
        """把请求位深修正到 SDK 实际支持的档位。优先用请求值，其次 16，再其次最大值。"""
        supported_bit_depths = self._supported_bit_depths()
        if not supported_bit_depths:
            supported_bit_depths = [16]
        # 1) 请求值合法 → 直接使用。
        if int(requested_bit_depth) in supported_bit_depths:
            return supported_bit_depths, int(requested_bit_depth)
        # 2) 否则优先回退到 16（最常用且兼容）。
        if 16 in supported_bit_depths:
            return supported_bit_depths, 16
        # 3) 没有 16 → 用最大档位（最大动态范围）。
        return supported_bit_depths, max(supported_bit_depths)

    def _apply_bit_depth(self, bit_depth: int) -> None:
        """把选定 bit_depth 写入相机：BITSPERCHANNEL 与 IMAGE_PIXELTYPE 同步设置。"""
        dcamapi4 = self._dcamapi4
        # 1) DCAM 用 ``_8/_10/_12/_14/_16`` 这种枚举名表示档位；找不到时退回 raw int。
        bits_enum_name = f"_{int(bit_depth)}"
        bits_value = getattr(dcamapi4.DCAMPROP.BITSPERCHANNEL, bits_enum_name, int(bit_depth))
        # 2) 8 位用 MONO8 像素类型，其它用 MONO16；与 ``read_frame_sequence`` 的 dtype 一致。
        pixel_type = dcamapi4.DCAM_PIXELTYPE.MONO8 if int(bit_depth) <= 8 else dcamapi4.DCAM_PIXELTYPE.MONO16
        self._set_property(dcamapi4.DCAM_IDPROP.BITSPERCHANNEL, bits_value)
        self._set_property(dcamapi4.DCAM_IDPROP.IMAGE_PIXELTYPE, pixel_type)

    def _read_timing_summary(
        self,
        config: CameraConfig,
        supported_bit_depths: list[int],
        applied_bit_depth: int,
        applied_readout_speed_value: float,
    ) -> dict[str, Any]:
        """汇总相机的 timing 信息（readout time / 触发周期 / blanking / 推荐帧间隔）。

        关键字段：
            - ``timing_readout_time_s``：相机读出一帧需要的时间（秒）。
            - ``recommended_inter_frame_gap_us``：基于读出 + blanking 计算的建议帧间隔。
              ``effective_inter_frame_gap_us`` 用它决定是否覆盖默认 50 ms。
        """
        dcamapi4 = self._dcamapi4
        # 1) 读取三个 timing 字段（部分相机不支持某项，此处 try_get 容错）。
        timing_readout_time_s = self._try_get_property(dcamapi4.DCAM_IDPROP.TIMING_READOUTTIME)
        timing_cyclic_trigger_period_s = self._try_get_property(dcamapi4.DCAM_IDPROP.TIMING_CYCLICTRIGGERPERIOD)
        timing_min_trigger_blanking_s = self._try_get_property(dcamapi4.DCAM_IDPROP.TIMING_MINTRIGGERBLANKING)
        # 2) 把读出时间夹到 ≥0，并把 None 转 0.0，便于后面 ``int(ceil(...))``。
        readout_s = None if timing_readout_time_s is None else max(0.0, float(timing_readout_time_s))
        min_tb_s = max(0.0, float(timing_min_trigger_blanking_s or 0.0))
        # 3) 拼装基础摘要字典（GUI 摘要、controller 缓存都依赖这里的字段名）。
        summary = {
            "camera_model": str(self._connection_info.get("model", "")),
            "camera_id": str(self._connection_info.get("camera_id", "")),
            "applied_readout_speed_value": int(round(float(applied_readout_speed_value))),
            "applied_readout_speed_text": self._property_text(
                dcamapi4.DCAM_IDPROP.READOUTSPEED,
                applied_readout_speed_value,
            ),
            "supported_bit_depths": list(supported_bit_depths),
            "applied_bit_depth": int(applied_bit_depth),
            "timing_readout_time_s": timing_readout_time_s,
            "timing_cyclic_trigger_period_s": timing_cyclic_trigger_period_s,
            "timing_min_trigger_blanking_s": timing_min_trigger_blanking_s,
        }
        # 4) 若 readout 已知，再加 1 ms 安全余量给出推荐帧间隔（微秒）。
        if readout_s is not None:
            summary["recommended_inter_frame_gap_us"] = (
                int(math.ceil(readout_s * 1_000_000.0))
                + int(math.ceil(min_tb_s * 1_000_000.0))
                + 1000
            )
        return summary

    def initialize(self) -> None:
        """初始化 DCAM SDK 并校验至少有一台相机；多次调用幂等。"""
        if self._initialized:
            return
        # 1) 加载并初始化 DCAM API。
        self._initialize_dcam_api()
        # 2) 至少要有 1 台 DCAM 相机，否则项目无法运行。
        device_count = self._dcam.Dcamapi.get_devicecount()
        if not device_count:
            raise HardwareError("No Hamamatsu DCAM-compatible camera detected.")
        # 3) 记下 SDK 路径与设备数，便于 GUI 显示状态。
        self._sdk = {"module_dir": str(self._module_dir), "device_count": int(device_count)}
        self._initialized = True

    def list_devices(self) -> list[dict[str, Any]]:
        """枚举系统中所有 DCAM 相机，返回 descriptor 列表。"""
        # 1) ``_initialize_dcam_api`` 是幂等的；调一次确保后面 ``Dcam(i)`` 可用。
        self._initialize_dcam_api()
        device_count = self._dcam.Dcamapi.get_devicecount()
        if device_count is False:
            raise HardwareError("DCAM API is not initialized; failed to enumerate Hamamatsu cameras.")
        devices: list[dict[str, Any]] = []
        # 2) 逐个 open/读描述/close；失败立刻抛错避免遗留打开句柄。
        for index in range(int(device_count)):
            camera = self._dcam.Dcam(index)
            if not camera.dev_open():
                last_error = camera.lasterr()
                error_name = getattr(last_error, "name", str(last_error))
                raise HardwareError(f"Failed to enumerate DCAM camera {index}: {error_name}")
            try:
                devices.append(self._build_device_descriptor(index, camera))
            finally:
                try:
                    camera.dev_close()
                except Exception:
                    logger.warning("Failed to close DCAM camera while listing devices.", exc_info=True)
        return devices

    def is_connected(self) -> bool:
        return self._device_open

    def connection_info(self) -> dict[str, Any]:
        """返回当前 connection_info 的浅拷贝，附加 supported_bit_depths（若已打开）。"""
        info = dict(self._connection_info)
        if self._device_open and self._dcam_camera is not None:
            info["supported_bit_depths"] = self.get_supported_bit_depths()
        return info

    def connect(self, device_index: int | None = None, device_label: str = "") -> dict[str, Any]:
        """打开指定相机并返回带 supported_bit_depths 的 connection_info。"""
        # 1) 记忆下用户选择，便于后续 ``apply_config`` 默认使用同一台相机。
        selected_index = self._camera_config.device_index if device_index is None else int(device_index)
        self._camera_config.device_index = selected_index
        if device_label:
            self._camera_config.device_label = device_label
        # 2) 打开相机（带 label 显示），并把 supported_bit_depths 合入 info。
        info = self._ensure_camera_open(device_index=selected_index, device_label=device_label or self._camera_config.device_label)
        info["supported_bit_depths"] = self.get_supported_bit_depths()
        self._connection_info["supported_bit_depths"] = list(info["supported_bit_depths"])
        return info

    def disconnect(self) -> None:
        """关闭当前相机，等价于 ``_close_camera``。"""
        self._close_camera()

    def apply_config(self, config: CameraConfig) -> dict[str, Any]:
        """把 ``CameraConfig`` 翻译并写入 DCAM；用于正式 9 帧采集（external_level 触发）。

        失败处理：
            ``_configure_camera`` 抛错时主动关闭相机，避免遗留半配置状态。
        """
        # 1) 必须先 initialize，确保 ``self._dcamapi4`` 可用。
        if not self._initialized:
            self.initialize()
        # 2) external_level 是项目唯一支持的触发模式；其它模式直接拒绝。
        if config.trigger_mode != "external_level":
            raise HardwareError("Fusion BT adapter only supports external_level trigger mode.")
        # 3) 记忆配置 + 确保相机已打开，再走通用 ``_configure_camera``。
        self._camera_config = config
        self._ensure_camera_open(device_index=config.device_index, device_label=config.device_label)
        try:
            result = self._configure_camera(
                config,
                trigger_source=self._dcamapi4.DCAMPROP.TRIGGERSOURCE.EXTERNAL,
                trigger_active=self._dcamapi4.DCAMPROP.TRIGGERACTIVE.LEVEL,
            )
        except Exception:
            # 4) 任何配置失败都关相机，防止下次 apply_config 继承坏状态。
            self._close_camera()
            raise
        return result

    def _configure_camera(self, config: CameraConfig, trigger_source: int, trigger_active: int) -> dict[str, Any]:
        """``apply_config`` 与 ``start_preview`` 共享的核心写属性序列。"""
        self._camera_config = config
        dcamapi4 = self._dcamapi4
        # 1) 触发相关四件套：source / active 由调用方传入，mode/polarity 固定。
        self._set_property(dcamapi4.DCAM_IDPROP.TRIGGERSOURCE, trigger_source)
        self._set_property(dcamapi4.DCAM_IDPROP.TRIGGERACTIVE, trigger_active)
        self._set_property(dcamapi4.DCAM_IDPROP.TRIGGER_MODE, dcamapi4.DCAMPROP.TRIGGER_MODE.NORMAL)
        self._set_property(dcamapi4.DCAM_IDPROP.TRIGGERPOLARITY, dcamapi4.DCAMPROP.TRIGGERPOLARITY.POSITIVE)
        # 2) Readout speed：按型号挑档位；SDK 返回实际接受值。
        applied_readout_speed_value = self._set_property(
            dcamapi4.DCAM_IDPROP.READOUTSPEED,
            self._fixed_readout_speed_value(),
        )
        # 3) 位深：先解析支持列表，再写入硬件。``config.bit_depth`` 回写实际生效值。
        supported_bit_depths, applied_bit_depth = self._resolve_bit_depth(config.bit_depth)
        self._apply_bit_depth(applied_bit_depth)
        config.bit_depth = int(applied_bit_depth)
        # 4) ROI：先求出对齐结果，再写入相机；同时把实际 ROI 回写 config。
        roi_summary = self._resolve_roi_config(config)
        self._apply_roi_config(roi_summary)
        applied_roi = roi_summary["applied_roi"]
        config.roi_x = int(applied_roi["x"])
        config.roi_y = int(applied_roi["y"])
        config.roi_width = int(applied_roi["width"])
        config.roi_height = int(applied_roi["height"])
        # 5) 曝光：转换 us → s，写入 DCAM EXPOSURETIME。
        exposure_s = config.exposure_us / 1_000_000.0
        self._set_property(dcamapi4.DCAM_IDPROP.EXPOSURETIME, exposure_s)
        # 6) 读 timing 摘要，合并 roi_summary 一并返回，供 controller 缓存。
        summary = self._read_timing_summary(
            config,
            supported_bit_depths=supported_bit_depths,
            applied_bit_depth=applied_bit_depth,
            applied_readout_speed_value=applied_readout_speed_value,
        )
        summary.update(roi_summary)
        # 7) 把支持位深更新到 connection_info，让 GUI 在断开预览前能拿到一致数据。
        self._connection_info["supported_bit_depths"] = list(supported_bit_depths)
        return summary

    @property
    def preview_active(self) -> bool:
        return self._preview_active

    def start_preview(self, config: CameraConfig, frame_buffer_count: int = 3) -> None:
        """启动 live 预览（内部触发 + EDGE）。"""
        if not self._initialized:
            self.initialize()
        if frame_buffer_count <= 0:
            raise HardwareError("frame_buffer_count must be positive.")
        # 1) 防御性收尾旧状态：把 preview/arm 都先关掉。
        self.stop_preview()
        self.disarm()
        self._camera_config = config
        self._preview_frame_counter = 0
        # 2) 打开相机 + 配置（触发改为 internal/edge，与正式采集的 external/level 区分）。
        self._ensure_camera_open(device_index=config.device_index, device_label=config.device_label)
        self._configure_camera(
            config,
            trigger_source=self._dcamapi4.DCAMPROP.TRIGGERSOURCE.INTERNAL,
            trigger_active=self._dcamapi4.DCAMPROP.TRIGGERACTIVE.EDGE,
        )
        # 3) 分配预览 buffer；失败立刻抛错。
        if not self._dcam_camera.buf_alloc(frame_buffer_count):
            raise HardwareError(f"Failed to allocate DCAM preview buffer: {self._dcam_camera.lasterr().name}")
        # 4) 启动连续拍照；``cap_start(True)`` 表示 continuous 模式。
        if not self._dcam_camera.cap_start(True):
            try:
                self._dcam_camera.buf_release()
            except Exception:
                logger.warning("Failed to release DCAM preview buffer after cap_start failure.", exc_info=True)
            raise HardwareError(f"Failed to start DCAM preview capture: {self._dcam_camera.lasterr().name}")
        self._preview_active = True

    def read_preview_frame(self, timeout_ms: int = 100) -> np.ndarray:
        """从 DCAM 取最新一帧预览数据，转成 ``uint16`` NumPy 数组返回。"""
        if not self._preview_active:
            raise HardwareError("Preview must be started before reading frames.")
        # 1) wait FRAMEREADY 事件；超时即抛错（GUI 会决定是否打印日志）。
        if not self._dcam_camera.wait_capevent_frameready(timeout_ms):
            raise HardwareError(f"DCAM preview wait failed: {self._dcam_camera.lasterr().name}")
        # 2) 取最新一帧；SDK 直接返回 NumPy 视图。
        frame = self._dcam_camera.buf_getlastframedata()
        if frame is False:
            raise HardwareError(f"Failed to fetch DCAM preview frame: {self._dcam_camera.lasterr().name}")
        # 3) 累计预览帧计数，便于 GUI 显示 FPS。``np.asarray(..., uint16)`` 避免额外复制。
        self._preview_frame_counter += 1
        return np.asarray(frame, dtype=np.uint16)

    def stop_preview(self) -> None:
        """停止 live 预览：先切 flag，再关 cap、释放 buffer；失败被静默吞掉。"""
        if not self._preview_active:
            return
        self._preview_active = False
        if not self._initialized:
            return
        try:
            self._dcam_camera.cap_stop()
        except Exception:
            logger.warning("Failed to stop DCAM preview capture.", exc_info=True)
        try:
            self._dcam_camera.buf_release()
        except Exception:
            logger.warning("Failed to release DCAM preview buffer.", exc_info=True)

    def arm(self, frame_count: int) -> None:
        """让相机进入 snapshot 模式等待外部触发；分配 ``frame_count`` 大小的 buffer。"""
        if not self._initialized:
            self.initialize()
        # 1) 如果当前还在预览中先停掉；snapshot 与 continuous 模式互斥。
        if self._preview_active:
            self.stop_preview()
        self._frame_count = frame_count
        self._armed = True
        if frame_count <= 0:
            raise HardwareError("frame_count must be positive.")
        # 2) 确保相机已打开（apply_config 之后调 arm 也是合法路径）。
        self._ensure_camera_open(
            device_index=self._camera_config.device_index,
            device_label=self._camera_config.device_label,
        )
        # 3) 分配 buffer + 启动 snapshot 捕获；任一失败抛错。
        if not self._dcam_camera.buf_alloc(frame_count):
            raise HardwareError(f"Failed to allocate DCAM frame buffer: {self._dcam_camera.lasterr().name}")
        if not self._dcam_camera.cap_snapshot():
            raise HardwareError(f"Failed to arm DCAM snapshot capture: {self._dcam_camera.lasterr().name}")

    def disarm(self) -> None:
        """退出 snapshot 模式；与 ``arm`` 对称。"""
        self._armed = False
        self._frame_count = 0
        if not self._initialized:
            return
        try:
            self._dcam_camera.cap_stop()
        except Exception:
            logger.warning("Failed to stop DCAM snapshot capture during disarm.", exc_info=True)
        try:
            self._dcam_camera.buf_release()
        except Exception:
            logger.warning("Failed to release DCAM snapshot buffer during disarm.", exc_info=True)

    def read_frame_sequence(
        self,
        frame_count: int,
        pattern_files: list[str],
        laser_wavelength_nm: int,
        frame_callback: Any | None = None,
        stop_event: Any | None = None,
    ) -> tuple[np.ndarray, list[float]]:
        """从 DCAM 环形 buffer 读取 SIM9 图像序列。

        策略（避免 TIMEOUT 误报）：
            1. 先读 ``cap_transferinfo``，把已经到达 buffer 的帧逐个 emit 给 frame_callback。
            2. 没有新增帧时再 wait FRAMEREADY；超时分片为 50 ms，stop_event 可立即取消。
            3. 总等待时间 = ``max(timeout_ms, 9 × (exposure_ms + 50) + 1000)``。
            4. wait 返回 TIMEOUT 时再次读 transferinfo，若有新增帧就继续循环；
               否则只有在总超时到达时才抛 HardwareError。

        异常：
            ``HardwareError``：超时或 SDK 返回非 TIMEOUT 错误时；附带 ``captured X/Y``
            诊断便于排查触发 TTL/触发模式/接线问题。
        """
        if not self._armed:
            raise HardwareError("Camera must be armed before reading frame sequence.")
        timestamps: list[float] = []
        captured = 0
        # 1) 计算总等待时长：取用户超时与"9 × (曝光+50ms) + 1s"中较大的一个。
        overall_timeout_ms = max(
            self._camera_config.timeout_ms,
            int(frame_count * max(self._camera_config.exposure_us / 1000.0 + 50.0, 10.0) + 1000.0),
        )
        started_at = time.time()
        # 2) 主循环：直到拿够 frame_count 帧。
        while captured < frame_count:
            # 2a) stop_event 优先；任何位置取消都立刻抛错走 finally 收尾。
            if stop_event is not None and stop_event.is_set():
                raise HardwareError("Acquisition cancelled while waiting for Fusion BT frames.")
            # 2b) 读 transfer 计数：如果 buffer 里已有未消费帧，先把它们 emit 出去。
            transfer = self._dcam_camera.cap_transferinfo()
            if transfer is False:
                raise HardwareError(f"Failed to query DCAM transfer info: {self._dcam_camera.lasterr().name}")
            transferred = min(max(int(transfer.nFrameCount), 0), frame_count)
            if transferred > captured:
                while captured < transferred:
                    captured += 1
                    timestamp = time.time()
                    timestamps.append(timestamp)
                    if frame_callback is not None:
                        frame_callback(captured, timestamp)
                continue

            # 2c) 没有新增帧 → 看总超时是否到。
            elapsed_ms = int((time.time() - started_at) * 1000.0)
            remaining_ms = overall_timeout_ms - elapsed_ms
            if remaining_ms <= 0:
                raise HardwareError(
                    f"DCAM frame wait failed: {self._dcam_camera.lasterr().name}; "
                    f"captured {captured}/{frame_count} frames. Confirm camera trigger TTL, trigger mode, "
                    "and DAQ camera_trigger_line wiring."
                )
            # 2d) 分片 wait（最多 50 ms），便于 stop_event 与 transferinfo 间歇复查。
            wait_ms = max(1, min(remaining_ms, 50))
            if not self._dcam_camera.wait_capevent_frameready(wait_ms):
                # 2e) wait 失败：先检查是否是用户取消。
                if stop_event is not None and stop_event.is_set():
                    raise HardwareError("Acquisition cancelled while waiting for Fusion BT frames.")
                # 2f) 非 TIMEOUT 错误立即抛错；TIMEOUT 还要再 transferinfo 一次确认是否漏读帧。
                error_name = str(self._dcam_camera.lasterr().name)
                if error_name.upper() != "TIMEOUT":
                    raise HardwareError(
                        f"DCAM frame wait failed: {error_name}; "
                        f"captured {captured}/{frame_count} frames. Confirm camera trigger TTL, trigger mode, "
                        "and DAQ camera_trigger_line wiring."
                    )
                transfer = self._dcam_camera.cap_transferinfo()
                if transfer is False:
                    raise HardwareError(f"Failed to query DCAM transfer info: {self._dcam_camera.lasterr().name}")
                transferred = min(max(int(transfer.nFrameCount), 0), frame_count)
                # 2g) 期间已有新帧到达 → 继续主循环 emit 它们。
                if transferred > captured:
                    continue
                elapsed_ms = int((time.time() - started_at) * 1000.0)
                # 2h) 仍未到总超时 → 再循环一次（避免单次 TIMEOUT 立即放弃）。
                if elapsed_ms < overall_timeout_ms:
                    continue
                # 2i) 真的超时 → 抛错告知。
                raise HardwareError(
                    f"DCAM frame wait failed: {self._dcam_camera.lasterr().name}; "
                    f"captured {captured}/{frame_count} frames. Confirm camera trigger TTL, trigger mode, "
                    "and DAQ camera_trigger_line wiring."
                )

        # 3) 拿够 frame_count 帧后，从 buffer 中逐帧拷贝出 NumPy 数组拼成 (N, H, W) uint16。
        frames = np.empty((frame_count, self._camera_config.roi_height, self._camera_config.roi_width), dtype=np.uint16)
        for index in range(frame_count):
            frame = self._dcam_camera.buf_getframedata(index)
            if frame is False:
                raise HardwareError(f"Failed to read DCAM frame {index}: {self._dcam_camera.lasterr().name}")
            frames[index] = np.asarray(frame, dtype=np.uint16)
        return frames, timestamps


# SLM adapter 负责连接 R11、枚举/选择 Running Order，并保留手动 pattern 上传扩展点。
class KopinSlmAdapter:
    """Kopin / FDD R11 SLM 适配器。

    职责：
        - 加载 ``R11CommLib`` DLL（通过 ``_R11CommLib``）。
        - 枚举 R11 WinUSB 设备 / 打开 / 关闭。
        - 列举 / 选择预烧录 Running Order；正式 SIM 采集走这条路径。
        - 保留 ``program_patterns`` 手动烧录 9 帧位平面的兼容入口（当前正式流程不用）。

    维护要点：
        - SLM 生命周期由 controller 统一持有；``SimSettingsDialog`` 不应另起一份。
        - ``activate_prepared_patterns`` 是采集前的必要步骤，否则 SLM 不会真正循环 RO。
    """
    def __init__(self, sdk_path: str = ""):
        self.sdk_path = sdk_path
        self._sdk: _R11CommLib | None = None
        self._initialized = False
        self._prepared = PatternPreparationResult()
        self._device_info: dict[str, Any] = {}
        self._base_info: dict[str, Any] = {}
        self._device_open = False
        self._connected_device_path = ""

    def list_devices(self) -> list[dict[str, str]]:
        """枚举所有 R11 WinUSB 设备，附加 display 字段供 GUI 下拉显示。"""
        if not self._initialized:
            self.initialize()
        devices = self._sdk.enumerate_winusb_devices()
        formatted: list[dict[str, str]] = []
        for index, device in enumerate(devices, start=1):
            # 优先用序列号作为 display；其次用 path；最后兜底 ``R11 Device N``。
            label = device["serial"] or f"R11 Device {index}"
            if not device["serial"] and device["path"]:
                label = device["path"]
            formatted.append({**device, "display": label})
        return formatted

    def is_connected(self) -> bool:
        return self._device_open

    def connection_info(self) -> dict[str, Any]:
        return dict(self._device_info)

    def connect(self, device_path: str | None = None) -> dict[str, Any]:
        """连接 R11 设备；同 device_path 已连接直接返回，避免重复打开。"""
        if not self._initialized:
            self.initialize()
        # 1) 已连接同一设备 → 直接返回 info；避免重复 WinUSB open。
        if self._device_open and (not device_path or device_path == self._connected_device_path):
            return self.connection_info()
        # 2) 用户请求换设备 → 先 disconnect 当前的。
        if self._device_open and device_path and device_path != self._connected_device_path:
            self.disconnect()
        # 3) 枚举设备；空列表抛错提示驱动 / USB 状态。
        devices = self.list_devices()
        if not devices:
            raise HardwareError("No R11 WinUSB devices found. Confirm the R11 driver is installed and the device is connected.")
        device = devices[0]
        # 4) 指定 path → 在设备列表里找匹配项；找不到抛错。
        if device_path:
            matching = next((item for item in devices if item["path"] == device_path or item["id"] == device_path), None)
            if matching is None:
                raise HardwareError(f"Selected R11 device not found: {device_path}")
            device = matching
        # 5) 打开 WinUSB → 写运行时状态 → 读取一组 metadata（序列号、repertoire 名）。
        self._sdk.open_winusb(device["path"])
        self._connected_device_path = device["path"]
        self._device_info = {
            **self._base_info,
            "device_id": device["id"],
            "device_path": device["path"],
            "device_serial_hint": device["serial"],
            "serial_number": self._sdk.get_serial_number(),
            "repertoire_name": self._sdk.get_repertoire_name(),
        }
        self._device_open = True
        return self.connection_info()

    def disconnect(self) -> None:
        """关闭 R11 WinUSB 句柄并把 device_info 恢复为基线。"""
        if not self._device_open:
            return
        try:
            self._sdk.close()
        finally:
            # ``finally`` 保证就算 close 抛错，本地状态字段也被清零。
            self._device_open = False
            self._connected_device_path = ""
            self._device_info = dict(self._base_info)

    def _connect(self, device_path: str | None = None) -> None:
        """内部连接快捷方式，主要在 ``program_patterns`` / ``activate_prepared_patterns`` 中调用。"""
        self.connect(device_path=device_path)

    def list_running_orders(self) -> list[tuple[int, str]]:
        """列出当前 repertoire 暴露的 (index, name) 列表；SLM 必须先连接。"""
        if not self._initialized:
            self.initialize()
        if not self._device_open:
            raise HardwareError("Connect to an SLM before listing running orders.")
        # 依次读取 RO 个数与每个 RO 的名称。
        running_order_count = self._sdk.get_running_order_count()
        return [
            (index, self._sdk.get_running_order_name(index))
            for index in range(running_order_count)
        ]

    def select_running_order(self, ro_index: int) -> dict[str, Any]:
        """选择预烧录 Running Order，并返回采集核心需要的 PatternPreparationResult。"""
        if not self._initialized:
            self.initialize()
        if not self._device_open:
            raise HardwareError("Connect to an SLM before selecting a running order.")
        ro_index = int(ro_index)
        # 1) 把目标 RO 写到 SDK；后续 ``activate_running_order`` 才会让 SLM 真正运行。
        self._sdk.set_selected_running_order(ro_index)
        ro_name = self._sdk.get_running_order_name(ro_index)
        activation_type = self._sdk.get_running_order_activation_type()
        # 2) 用统一的 PatternPreparationResult 表达"RO 模式"：handles=[-1]，metadata 标 mode。
        self._prepared = PatternPreparationResult(
            pattern_files=[ro_name] * 9,
            handles=[-1],
            prepared_at=time.time(),
            metadata={
                "mode": "running_order",
                "running_order_index": ro_index,
                "running_order_name": ro_name,
                "activation_type": activation_type,
                **self._device_info,
            },
        )
        return {
            "running_order_index": ro_index,
            "running_order_name": ro_name,
            "activation_type": activation_type,
            "pattern_result": self._prepared,
        }

    def get_running_order_activation_state(self) -> dict[str, Any]:
        """读取当前 repertoire/RO 激活状态，返回 ``{"code", "name"}``。

        用途：
            诊断 [HWA h] RO 的 EXT_RUN 门控时序：软件 activate 后预期 0x54 MHW，
            ``slm_enable``（EXT_RUN）拉高且 tHWAT（≤500 µs）过后预期 0x56 ACT。
            旧版 R11CommLib 缺少该函数时 ``code`` 为 None。
        """
        if not self._initialized:
            self.initialize()
        if not self._device_open:
            raise HardwareError("Connect to an SLM before querying activation state.")
        code = self._sdk.get_activation_state()
        return {"code": code, "name": r11_activation_state_name(code)}

    def _upload_pattern(self, bitplane_index: int, pattern_path: Path) -> None:
        """把单个位平面文件烧录到指定 bitplane_index 对应的 R11 flash 位置。"""
        # 1) 读取 + 对齐 + 字节反转 bitplane payload。
        payload = _load_r11_bitplane_file(pattern_path)
        page_base = bitplane_index * R11_BITPLANE_PAGES
        last_block_num = None
        # 2) 逐 page 写入：跨 block 边界时先 erase 整个 block。
        for page_offset, page_payload in enumerate(_iter_r11_flash_pages(payload)):
            page_num = page_base + page_offset
            block_num = page_num // R11_PAGES_PER_BLOCK
            page_address = R11_IMAGE_BASE + page_num
            if block_num != last_block_num:
                self._sdk.erase_block(page_address)
                last_block_num = block_num
            # 3) 先 write 到 SDK 内部 buffer，再 burn 真正烧到 flash。
            self._sdk.write_flash_page(page_payload)
            self._sdk.burn_flash_page(page_address)

    def initialize(self) -> None:
        """加载 R11 DLL，记录版本信息，初始化运行时字段。"""
        if self._initialized:
            return
        # 1) 解析 DLL 路径 → 实例化 ``_R11CommLib`` → 读版本。
        dll_path = _resolve_r11_dll_path(self.sdk_path)
        self._sdk = _R11CommLib(dll_path)
        versions = self._sdk.get_versions()
        # 2) ``_base_info`` 是任何 device_info 的基础；disconnect 时回退到它。
        self._base_info = {
            "dll_path": str(dll_path),
            "comm_lib_version": versions["comm_lib"],
            "r11_comm_lib_version": versions["r11_comm_lib"],
        }
        self._device_info = dict(self._base_info)
        self._initialized = True

    def program_patterns(self, pattern_files: list[str], device_path: str | None = None) -> PatternPreparationResult:
        """手动烧录 9 帧位平面到 SLM（兼容旧调试路径，正式采集已用 Running Order）。

        过程：
            1. 校验 9 个文件都存在。
            2. 连接 SLM → 关闭当前 RO → 检查 bitplane 容量。
            3. 逐个 ``_upload_pattern``，再 ``reload_repertoire``。
            4. 轮询 ``get_progress`` 直到 100%（最长 30 秒）。
            5. 读 RO 列表 + 选中 RO，更新 ``self._prepared``。
        """
        if not self._initialized:
            self.initialize()
        # 1) 必须 9 个 pattern；任何缺失文件立刻报错。
        if len(pattern_files) != 9:
            raise HardwareError("Exactly 9 pattern files are required.")
        missing = [path for path in pattern_files if not path or not Path(path).exists()]
        if missing:
            raise HardwareError(f"Pattern files not found: {missing}")

        # 2) 连接 SLM；关闭当前 RO，准备烧录新位平面。
        self._connect(device_path=device_path)
        self._sdk.deactivate_running_order()
        bitplane_count = self._sdk.get_bitplane_count()
        if bitplane_count < len(pattern_files):
            raise HardwareError(
                f"R11 repertoire exposes only {bitplane_count} bitplanes, but {len(pattern_files)} pattern files were provided."
            )
        # 3) 逐文件烧录。
        for index, pattern_file in enumerate(pattern_files):
            self._upload_pattern(index, Path(pattern_file))
        # 4) Reload repertoire 让烧录的内容生效。
        self._sdk.reload_repertoire()
        # 5) 轮询进度直到 100；30 秒未完成抛错。
        started_at = time.time()
        progress = 0
        while progress < 100:
            if time.time() - started_at > 30.0:
                raise HardwareError("Timed out waiting for R11 repertoire reload to complete.")
            progress = self._sdk.get_progress()
            time.sleep(0.05)
        # 6) 读 RO 列表 + 选中 RO，整理为 ``_prepared``，让后续 ``activate_prepared_patterns`` 可用。
        running_order_count = self._sdk.get_running_order_count()
        selected_ro = self._sdk.get_selected_running_order()
        running_orders = [
            self._sdk.get_running_order_name(index)
            for index in range(running_order_count)
        ]
        self._prepared = PatternPreparationResult(
            pattern_files=list(pattern_files),
            handles=list(range(len(pattern_files))),
            prepared_at=time.time(),
            metadata={
                "mode": "r11_winusb",
                "bitplane_count": bitplane_count,
                "selected_running_order": selected_ro,
                "running_orders": running_orders,
                **self._device_info,
            },
        )
        return self._prepared

    def activate_prepared_patterns(self) -> None:
        """让 SLM 真正运行已准备好的 RO；未准备过 → 抛错。"""
        if not self._prepared.handles:
            raise HardwareError("No patterns or running order prepared on SLM.")
        # 重新确认连接；正式采集前可能 controller 断过线，此处幂等地补连。
        self._connect(device_path=self._connected_device_path or None)
        self._sdk.activate_running_order()

    def prepared_summary(self) -> dict[str, Any]:
        """返回当前 ``_prepared`` 的字典形态；GUI 用它显示"上次准备结果"。"""
        return asdict(self._prepared)

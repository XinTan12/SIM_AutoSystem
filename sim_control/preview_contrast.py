"""uint16 相机帧到 8 位预览图的灰度映射工具集。

作用：
    采集链路保留 ``uint16`` 原始数据（送给重建/特征 pipeline），但 GUI 显示
    需要 ``uint8``。本文件提供两条快速路径来完成 ``uint16 → uint8``：
        1. ``manual`` 路径：用户指定灰度上限 ``gray_max``，按线性窗口映射。
        2. ``auto`` 路径：基于百分位采样动态估计窗口，并用一阶低通平滑避免预览
           亮度逐帧跳变。
    两种路径都先按显示尺寸 resize（采集帧通常 1152×1152 或 2304×2304，远大于
    预览区域），再走 LUT 映射，从而显著降低 CPU 负担。

协作关系：
    上游：``sim_control/preview.py``、``sim_control/gui.py`` 与
          ``control_wangbo/main.py`` 的 SIM 预览模块。
    下游：仅依赖 ``numpy``、``cv2``（仅 resize 时按需 import）与标准库 ``math``。
    相关：``tests/test_preview_contrast.py``、``tests/test_efficiency_optimizations.py``。

关键概念：
    - ``AutoContrastState``：保存上一帧估计出的 ``(lo, hi)`` 与帧形状，供平滑公式
      ``state.lo = (1-α)·prev + α·new`` 使用。重置时只清 ``lo/hi/frame_shape``。
    - LUT 缓存：``_manual_lut`` 用 ``@lru_cache`` 缓存最近 32 个不同 ``gray_max``
      的查找表，避免每帧重建 65536 项的查表数组。

维护要点：
    - 这里禁止做磁盘 IO 或调用相机 SDK；所有函数都应是纯 NumPy 计算。
    - 修改 LUT 缓存上限或采样策略前，请先看 ``test_efficiency_optimizations.py``
      的性能阈值；本文件的微秒级抖动会被 SIM 预览的 latest-frame-wins 模型放大。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import math
from typing import Any

import numpy as np


@dataclass
class AutoContrastState:
    """自动对比度的平滑状态，避免预览灰度窗口逐帧剧烈跳变。

    职责：
        - 暴露百分位上下限（``low_percentile``、``high_percentile``）与平滑系数。
        - 缓存上一帧估计的 ``lo``/``hi`` 与上一帧形状 ``frame_shape``。
        - ROI 切换或重启预览时调 ``reset()`` 清掉状态，避免老 ``lo/hi`` 污染新帧。

    协作：
        由 GUI 预览循环创建，作为 ``fast_auto_preview_uint16_to_uint8`` 的状态载体。
    """
    low_percentile: float = 0.5
    high_percentile: float = 99.5
    smoothing_alpha: float = 0.25
    max_sample_pixels: int = 262_144
    lo: float | None = None
    hi: float | None = None
    frame_shape: tuple[int, ...] | None = None

    def reset(self) -> None:
        """清空缓存的 lo/hi/frame_shape，让下一帧从零开始重新估计。"""
        self.lo = None
        self.hi = None
        self.frame_shape = None


def manual_uint16_to_uint8(frame: Any, gray_max: int | float) -> np.ndarray:
    """按用户指定上限 ``gray_max`` 把 uint16 帧线性映射到 uint8。

    用途：
        简单显示场景；调用方自己拿 ``gray_max`` 不需要做缩放，本函数走原尺寸路径。

    参数：
        frame: 任意可被 ``np.asarray`` 解析的二维数组。
        gray_max: 灰度窗口上限；小于 1 会被夹到 1，避免除零。
    """
    # 1) 把 gray_max 夹到 ≥1 防止除零，并显式转成 float 加快矢量化乘法。
    gray_max_value = max(1.0, float(gray_max))
    # 2) 转 float32 做乘法：avoid uint16 溢出，同时为 LUT 替代实现提供基线。
    frame_array = np.asarray(frame, dtype=np.float32)
    scaled = frame_array * (255.0 / gray_max_value)
    # 3) 夹到 [0, 255] 后转 uint8，得到可直接交给 QPixmap/QImage 的显示数组。
    return np.clip(scaled, 0, 255).astype(np.uint8)


def fast_preview_uint16_to_uint8(
    frame: Any,
    output_size: tuple[int, int],
    *,
    gray_max: int | float = 65535,
    auto_contrast: bool = False,
    auto_state: AutoContrastState | None = None,
) -> np.ndarray:
    """根据 ``auto_contrast`` 分支选择 ``manual`` 或 ``auto`` 路径。

    用途：
        GUI 预览循环的统一入口；调用方只关心"要不要自动对比度"。
    抛出：
        ``ValueError``：当 ``auto_contrast=True`` 但未提供 ``auto_state``。
    """
    if auto_contrast:
        if auto_state is None:
            raise ValueError("auto_state is required when auto_contrast is enabled")
        return fast_auto_preview_uint16_to_uint8(frame, auto_state, output_size)
    return fast_manual_preview_uint16_to_uint8(frame, gray_max, output_size)


def fast_manual_preview_uint16_to_uint8(frame: Any, gray_max: int | float, output_size: tuple[int, int]) -> np.ndarray:
    """先按显示尺寸缩放，再用 LUT 映射 uint16 → uint8（手动窗口路径）。"""
    # 1) 空帧（采集尚未启动或异常返回）直接返回符合输出尺寸的全黑数组，避免后续越界。
    frame_array = np.asarray(frame)
    if frame_array.size == 0:
        return np.zeros(_output_shape(output_size), dtype=np.uint8)

    # 2) 计算 gray_max 参数。
    gray_max_value = max(1.0, float(gray_max))
    high_clip = int(math.ceil(min(65535.0, gray_max_value)))
    # 3) 先 resize 到显示尺寸（大图→小图；后续操作均在小图上进行，节省 clip/LUT 开销）。
    src = frame_array if frame_array.dtype == np.uint16 else frame_array.astype(np.uint16, copy=False)
    resized = _resize_uint16_for_preview(src, output_size)
    # 4) 仅在 gray_max 低于满量程时才对小图 clip（in-place 无额外分配）。
    if high_clip < 65535:
        np.clip(resized, 0, high_clip, out=resized)
    # 5) 用缓存的 LUT 把 uint16 直接索引为 uint8；LUT 形状 (65536,) 与 resized dtype 匹配。
    lut = _manual_lut(gray_max_value)
    return lut[resized]


def fast_auto_preview_uint16_to_uint8(frame: Any, state: AutoContrastState, output_size: tuple[int, int]) -> np.ndarray:
    """自动对比度版本：先按百分位估计窗口，再做 LUT 缩放映射。

    重点优化：
        - 整体缩放被推迟到 LUT 之前。
        - 采样像素数受 ``state.max_sample_pixels`` 限制（默认 262_144），
          百分位估计在抽样上跑，避免 1080p 以上整帧统计。
    """
    # 1) 空帧 → 直接返回输出尺寸的全黑数组。
    frame_array = np.asarray(frame)
    if frame_array.size == 0:
        return np.zeros(_output_shape(output_size), dtype=np.uint8)

    # 2) 帧形状变化（ROI 切换、相机重连）→ 清状态，避免老 lo/hi 影响新帧的预览。
    if tuple(frame_array.shape) != state.frame_shape:
        state.reset()
        state.frame_shape = tuple(frame_array.shape)

    # 3) 抽样后估计 lo/hi 百分位。非有限值或 hi<=lo（极端均值场景）直接走"全黑"分支并尽量更新状态。
    sample = _sample_pixels(frame_array, state.max_sample_pixels)
    lo, hi = _percentile_limits(sample, state.low_percentile, state.high_percentile)
    if not math.isfinite(lo) or not math.isfinite(hi) or hi <= lo:
        state.lo = lo if math.isfinite(lo) else state.lo
        state.hi = hi if math.isfinite(hi) else state.hi
        return np.zeros(_output_shape(output_size), dtype=np.uint8)

    # 4) 一阶低通平滑：第一帧直接采用估计值；后续帧按 alpha 加权融合旧/新值。
    if state.lo is None or state.hi is None:
        state.lo = lo
        state.hi = hi
    else:
        alpha = min(1.0, max(0.0, float(state.smoothing_alpha)))
        state.lo = ((1.0 - alpha) * state.lo) + (alpha * lo)
        state.hi = ((1.0 - alpha) * state.hi) + (alpha * hi)

    # 5) 平滑后仍出现窗口退化（hi<=lo） → 当帧返回全黑，保护除零。
    if state.hi <= state.lo:
        return np.zeros(_output_shape(output_size), dtype=np.uint8)

    # 6) 先 resize（INTER_AREA 在 uint16 上执行），再 LUT 映射；LUT 本身处理 lo/hi 边界夹紧，
    #    与 manual 路径保持一致（resize-before-LUT）。
    resized = _resize_uint16_for_preview(
        np.asarray(frame_array).astype(np.uint16, copy=False), output_size
    )
    lut = _window_lut(float(state.lo), float(state.hi))
    return lut[resized]


def auto_uint16_to_uint8(frame: Any, state: AutoContrastState) -> np.ndarray:
    """不带缩放的自动对比度版本，用于全尺寸保存或调试。

    与 ``fast_auto_preview_uint16_to_uint8`` 的差异：
        - 不做 resize，输出与输入同尺寸。
        - 不依赖 LUT 缓存，直接做 float 运算（避免 LUT 命中失败时反复构建 65536 元素表）。
    """
    # 1) 空帧 → 返回与输入同 shape 的全黑 uint8。
    frame_array = np.asarray(frame)
    if frame_array.size == 0:
        return np.zeros(frame_array.shape, dtype=np.uint8)

    # 2) 帧形状变化 → 清状态。
    if tuple(frame_array.shape) != state.frame_shape:
        state.reset()
        state.frame_shape = tuple(frame_array.shape)

    # 3) 与 ``fast_auto_*`` 同样的百分位估计 + 异常分支处理。
    sample = _sample_pixels(frame_array, state.max_sample_pixels)
    lo, hi = _percentile_limits(sample, state.low_percentile, state.high_percentile)
    if not math.isfinite(lo) or not math.isfinite(hi) or hi <= lo:
        state.lo = lo if math.isfinite(lo) else state.lo
        state.hi = hi if math.isfinite(hi) else state.hi
        return np.zeros(frame_array.shape, dtype=np.uint8)

    # 4) 一阶低通平滑（与 fast 路径完全一致）。
    if state.lo is None or state.hi is None:
        state.lo = lo
        state.hi = hi
    else:
        alpha = min(1.0, max(0.0, float(state.smoothing_alpha)))
        state.lo = ((1.0 - alpha) * state.lo) + (alpha * lo)
        state.hi = ((1.0 - alpha) * state.hi) + (alpha * hi)

    if state.hi <= state.lo:
        return np.zeros(frame_array.shape, dtype=np.uint8)

    # 5) 不缩放：直接做 float32 线性映射 + clip。
    display = (np.asarray(frame_array, dtype=np.float32) - float(state.lo)) * (255.0 / (float(state.hi) - float(state.lo)))
    return np.clip(display, 0, 255).astype(np.uint8)


def _sample_pixels(frame: np.ndarray, max_sample_pixels: int) -> np.ndarray:
    """对整帧像素做等步长抽样，避免百分位计算扫全图。"""
    # 1) ``ravel`` 给出一维视图（不复制）。
    flat = np.ravel(frame)
    # 2) 抽样上限至少为 1，避免后续 ``ceil`` 得到 0 步长。
    max_pixels = max(1, int(max_sample_pixels))
    # 3) 像素数小于上限直接整帧返回，否则按等步长切片采样。
    if flat.size <= max_pixels:
        return flat
    stride = int(math.ceil(flat.size / max_pixels))
    return flat[::stride]


def _percentile_limits(sample: np.ndarray, low_percentile: float, high_percentile: float) -> tuple[float, float]:
    """计算抽样像素的 low/high 百分位值，并保证 low ≤ high。"""
    # 1) 把输入百分位都夹到 [0, 100] 闭区间，防御用户在 GUI 里填了非法值。
    low = min(100.0, max(0.0, float(low_percentile)))
    high = min(100.0, max(0.0, float(high_percentile)))
    # 2) 如果用户把 low/high 填反了，自动交换，保证后续 hi-lo>0 的语义。
    if high < low:
        low, high = high, low
    # 3) ``np.percentile`` 一次返回数组；强制 float32 减少与 uint16 类型间转换开销。
    values = np.percentile(sample.astype(np.float32, copy=False), [low, high])
    return float(values[0]), float(values[1])


def _resize_uint16_for_preview(frame: np.ndarray, output_size: tuple[int, int]) -> np.ndarray:
    """把 uint16 帧 resize 到预览尺寸，保持 dtype 不变。"""
    # 1) 把 output_size 夹到 ≥1 并比较：若已是目标尺寸直接返回连续视图，避免无谓 resize。
    width, height = _normalized_output_size(output_size)
    if frame.shape[:2] == (height, width):
        return np.ascontiguousarray(frame.astype(np.uint16, copy=False))
    # 2) 仅在需要 resize 时才 import cv2，降低 ``preview_contrast`` 在不需要 OpenCV 的测试中的导入成本。
    import cv2

    # 3) 缩小用 ``INTER_AREA``（更好抗锯齿），放大用 ``INTER_LINEAR``（速度可接受）。
    interpolation = cv2.INTER_AREA if width < frame.shape[1] or height < frame.shape[0] else cv2.INTER_LINEAR
    resized = cv2.resize(frame, (width, height), interpolation=interpolation)
    # 4) ``cv2.resize`` 在某些 numpy 版本下返回非连续 uint16，统一调整为连续 uint16。
    return np.ascontiguousarray(resized.astype(np.uint16, copy=False))


@lru_cache(maxsize=32)
def _manual_lut(gray_max_value: float) -> np.ndarray:
    """构造 [0, 65535] → uint8 的查找表，并按 ``gray_max_value`` 做缓存。

    缓存策略：
        最多保存 32 个不同 ``gray_max_value`` 的 LUT；GUI 上滑动 gray_max 时
        命中率高，避免重建 65536 项的数组。

    维护要点：
        返回的 LUT 设为只读（``setflags(write=False)``），防止下游意外原位修改影响其它帧。
    """
    # 1) 用 float32 输入避免 uint16 溢出，再夹到 [0,255] 并转 uint8。
    values = np.arange(65536, dtype=np.float32)
    lut = np.clip(values * (255.0 / gray_max_value), 0, 255).astype(np.uint8)
    # 2) 标为只读，保证缓存命中后不会被调用方误修改。
    lut.setflags(write=False)
    return lut


def _window_lut(lo: float, hi: float) -> np.ndarray:
    """构造 [lo, hi] 窗口的 uint16→uint8 LUT；自动对比度路径每帧重建。"""
    # 自动模式下 ``lo/hi`` 每帧都在变，缓存命中率低；这里不缓存以避免 LRU 频繁淘汰。
    values = np.arange(65536, dtype=np.float32)
    return np.clip((values - lo) * (255.0 / (hi - lo)), 0, 255).astype(np.uint8)


def _normalized_output_size(output_size: tuple[int, int]) -> tuple[int, int]:
    """把 ``(width, height)`` 元组规整为正整数。"""
    width, height = output_size
    return max(1, int(width)), max(1, int(height))


def _output_shape(output_size: tuple[int, int]) -> tuple[int, int]:
    """把 ``(width, height)`` 转成 numpy 风格的 ``(height, width)`` 形状。"""
    width, height = _normalized_output_size(output_size)
    return height, width

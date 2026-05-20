"""SIM 相机 ROI 尺寸预设与对齐规则。

作用：
    本文件把"SIM 相机可选的 ROI 尺寸有哪些"、"标签字符串怎么转回宽高"、
    "ROI 原点怎么对齐到相机步进"等规则集中到一处，供 GUI（``gui.py``）、
    真实硬件适配器（``adapters.py``）和测试同时复用。
    把规则集中化是为了避免 GUI 显示出的 ROI 与 adapter 真正发给相机的 ROI
    走不同的对齐/校验路径。

协作关系：
    上游：``sim_control/gui.py``（界面下拉框）、``sim_control/adapters.py``
          （``FusionBtCameraAdapter`` 写 DCAM ROI 前用本文件做对齐）、
          ``tests/test_sim_camera_presets.py``、``tests/test_sim_camera_adapter.py``。
    下游：纯函数实现，仅依赖标准库 ``collections.abc.Sequence`` 与 ``typing.Final``。

关键概念：
    - ROI 预设：全幅 ``2304×2304`` 与按 2/4 分倍率得到的 ``1152×1152``、
      ``576×576``。它们是 Hamamatsu ORCA-Fusion BT 在 SIM 模式下常用的尺寸。
    - 标签字符串：形如 ``"2304 x 2304"``，GUI 下拉显示与配置文件存储都使用此格式。
    - ROI 对齐步进 ``SIM_CAMERA_ROI_STEP_PX = 4``：相机要求 ROI 原点
      与边长按 4 像素对齐，否则 SDK 会拒绝设置。

维护要点：
    - 新增预设要保证 ``(width, height)`` 都是 ``SIM_CAMERA_ROI_STEP_PX`` 的整数倍。
    - 不要在这里写 SDK 调用或硬件 IO；本文件必须是纯函数库，可被任意线程调用。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final


# 默认预设元组：(width, height) 组成 SIM 模式下常用的全幅/半幅/四分幅 ROI 尺寸。
# 顺序从大到小，便于 GUI 默认选中第一个（全幅）作为最稳妥起点。
SIM_CAMERA_SIZE_PRESETS: Final[tuple[tuple[int, int], ...]] = (
    (2304, 2304),
    (1152, 1152),
    (576, 576),
)
# 默认 ROI 尺寸：取列表首项（全幅）。当配置缺失或非法时回落到此值。
DEFAULT_SIM_CAMERA_SIZE: Final[tuple[int, int]] = SIM_CAMERA_SIZE_PRESETS[0]
# 传感器全幅尺寸：Hamamatsu ORCA-Fusion BT 的有效像素区与 ``DEFAULT_SIM_CAMERA_SIZE`` 相同。
# 单独定义是为了在以后传感器换型时仅改这里。
SIM_CAMERA_SENSOR_SIZE: Final[tuple[int, int]] = DEFAULT_SIM_CAMERA_SIZE
# ROI 步进约束：相机要求 ROI 原点和边长都对齐到此步进的整数倍。
SIM_CAMERA_ROI_STEP_PX: Final[int] = 4
# 把预设宽高拼成 GUI 显示用的字符串标签，存档为元组保证不可变。
SIM_CAMERA_SIZE_PRESET_LABELS: Final[tuple[str, ...]] = tuple(
    f"{width} x {height}" for width, height in SIM_CAMERA_SIZE_PRESETS
)

# 标签 → 尺寸 反向查表，用于把 GUI 选中的字符串还原成 ``(width, height)``。
_LABEL_TO_SIZE: Final[dict[str, tuple[int, int]]] = {
    label: size for label, size in zip(SIM_CAMERA_SIZE_PRESET_LABELS, SIM_CAMERA_SIZE_PRESETS)
}
# 尺寸 → 标签 正向查表，用于把配置里的整数对还原成 GUI 应当高亮的标签。
_SIZE_TO_LABEL: Final[dict[tuple[int, int], str]] = {
    size: label for label, size in _LABEL_TO_SIZE.items()
}


def _normalize_size_presets(
    presets: Sequence[tuple[int, int]] | None = None,
) -> tuple[tuple[int, int], ...]:
    """把用户传入的预设列表规整成本模块期待的元组形态。

    用途：
        多个公共函数都接受 ``presets`` 参数；它可能是 ``None``（用默认）、
        列表、生成器，甚至元素类型不严格。这里集中做"转 int + 转元组 + 回落"
        三件事，保持下游逻辑简单。

    返回：
        非空的预设元组；若用户传空，回落到 ``SIM_CAMERA_SIZE_PRESETS``。
    """
    # 1) 把用户传入的可迭代对象逐项转成 (int, int) 元组，再整体打包为元组。
    normalized = tuple((int(width), int(height)) for width, height in (presets or SIM_CAMERA_SIZE_PRESETS))
    # 2) 空预设视为非法，强制回落到默认列表，避免后续 ``[0]`` 索引出错。
    return normalized or SIM_CAMERA_SIZE_PRESETS


def build_sim_camera_size_presets(sensor_width: int, sensor_height: int) -> tuple[tuple[int, int], ...]:
    """按传感器全幅尺寸生成「全幅、半幅、四分之一幅」三档 ROI 预设。

    用途：
        真实相机可能不是 ``2304×2304``（例如更换为不同型号），此函数让 GUI
        可以根据 adapter 报告的传感器尺寸动态生成预设，而不写死。

    参数：
        sensor_width / sensor_height: 整数像素数；小于 1 会被夹到 1，避免除零。

    返回：
        含 1×、1/2×、1/4× 三档的元组（去重），顺序从大到小。
    """
    # 1) 把传感器尺寸夹到至少 1 像素，避免后续 ``// divisor`` 得到 0。
    sensor_width = max(1, int(sensor_width))
    sensor_height = max(1, int(sensor_height))
    # 2) 顺序累加全幅、半幅、四分幅三档，遇到重复尺寸（极小传感器）跳过。
    presets: list[tuple[int, int]] = []
    for divisor in (1, 2, 4):
        size = (max(1, sensor_width // divisor), max(1, sensor_height // divisor))
        if size not in presets:
            presets.append(size)
    # 3) 转为不可变元组返回，保持与模块级常量类型一致。
    return tuple(presets)


def labels_for_sim_camera_size_presets(
    presets: Sequence[tuple[int, int]] | None = None,
) -> tuple[str, ...]:
    """把一组 ROI 预设转成 GUI 下拉显示用的字符串元组。"""
    return tuple(f"{width} x {height}" for width, height in _normalize_size_presets(presets))


def coerce_sim_camera_size(
    width: int,
    height: int,
    presets: Sequence[tuple[int, int]] | None = None,
) -> tuple[int, int]:
    """把任意 (width, height) 修正为预设列表里最接近、且合法的一项。

    用途：
        GUI 或配置文件可能存了已废弃或自定义的尺寸；本函数提供"严格匹配 →
        否则回落首项"的最简策略，确保下游永远拿到合法预设。
    """
    # 1) 规整预设并把输入转成 (int, int)，便于做相等比较。
    normalized_presets = _normalize_size_presets(presets)
    size = (int(width), int(height))
    # 2) 命中预设直接返回；未命中则回落到第一项（全幅）作为安全默认。
    if size in normalized_presets:
        return size
    return normalized_presets[0]


def label_for_sim_camera_size(
    width: int,
    height: int,
    presets: Sequence[tuple[int, int]] | None = None,
) -> str:
    """根据 (width, height) 生成「与预设匹配后的」GUI 标签字符串。"""
    # 先把尺寸规整到合法预设，再拼成 ``"W x H"`` 文本。
    roi_width, roi_height = coerce_sim_camera_size(width, height, presets)
    return f"{roi_width} x {roi_height}"


def size_from_sim_camera_label(
    label: str,
    presets: Sequence[tuple[int, int]] | None = None,
) -> tuple[int, int]:
    """把 GUI 标签字符串反向解析回 (width, height) 元组。"""
    # 1) 规整预设列表，并由它动态构造「标签 → 尺寸」查表。
    normalized_presets = _normalize_size_presets(presets)
    label_to_size = {
        f"{width} x {height}": (width, height)
        for width, height in normalized_presets
    }
    # 2) 命中标签返回对应尺寸；未知标签回落到首项预设，保证调用方拿到合法尺寸。
    return label_to_size.get(str(label).strip(), normalized_presets[0])


def preset_index_for_sim_camera_size(
    width: int,
    height: int,
    presets: Sequence[tuple[int, int]] | None = None,
) -> int:
    """返回 (width, height) 在预设列表中的下标，便于 GUI 设置下拉默认项。"""
    # 1) 先规整预设，再做 coerce（不匹配时已退到首项）。
    normalized_presets = _normalize_size_presets(presets)
    coerced = coerce_sim_camera_size(width, height, normalized_presets)
    # 2) ``index`` 在 coerce 后必然成功，避免 ValueError。
    return normalized_presets.index(coerced)


def is_full_frame_sim_camera_size(
    width: int,
    height: int,
    sensor_size: tuple[int, int] | None = None,
    presets: Sequence[tuple[int, int]] | None = None,
) -> bool:
    """判断 (width, height) 是否表示全幅 ROI。"""
    # 把尺寸规整后与传感器全幅尺寸做相等比较；未指定 sensor_size 时使用模块默认。
    return coerce_sim_camera_size(width, height, presets) == tuple(sensor_size or SIM_CAMERA_SENSOR_SIZE)


def sim_camera_roi_origin_bounds(
    width: int,
    height: int,
    sensor_size: tuple[int, int] | None = None,
    presets: Sequence[tuple[int, int]] | None = None,
) -> tuple[int, int]:
    """给定 ROI 宽高，计算 ROI 原点 (x, y) 在传感器内的合法上界。

    返回：
        ``(max_x, max_y)``：ROI 原点 x/y 最大可以放到这里，再加上 ROI 边长不会超出传感器。
    """
    # 1) 规整 ROI 尺寸（不合法尺寸会被回落到预设首项）。
    roi_width, roi_height = coerce_sim_camera_size(width, height, presets)
    # 2) 与传感器尺寸相减，得到 ROI 原点的允许上界；小于 0 时夹到 0。
    sensor_width, sensor_height = sensor_size or SIM_CAMERA_SENSOR_SIZE
    return (
        max(0, sensor_width - roi_width),
        max(0, sensor_height - roi_height),
    )


def _align_sim_camera_roi_origin(value: int, max_value: int, step_px: int = SIM_CAMERA_ROI_STEP_PX) -> int:
    """把 ROI 原点坐标向下对齐到步进，并裁剪到 [0, max_value] 范围内。"""
    # 1) 先夹到合法范围，避免负数或越界。
    clamped = min(max(int(value), 0), int(max_value))
    # 2) 把步进至少夹到 1，避免除零。
    step_px = max(1, int(step_px))
    # 3) 取整除再乘回去，得到对齐到 step_px 的位置（始终向下取整以不越界）。
    return int(clamped // step_px) * step_px


def normalize_sim_camera_roi(
    width: int,
    height: int,
    roi_x: int,
    roi_y: int,
    sensor_size: tuple[int, int] | None = None,
    step_px: int = SIM_CAMERA_ROI_STEP_PX,
    presets: Sequence[tuple[int, int]] | None = None,
) -> tuple[int, int, int, int]:
    """把任意 (width, height, roi_x, roi_y) 修正为合法 ROI 四元组。

    返回：
        ``(roi_width, roi_height, roi_x, roi_y)``，宽高已合法化，
        原点已按 ``step_px`` 对齐并夹到允许范围。

    用途：
        GUI 提交配置或加载旧配置时，先经此函数；下游 adapter 可放心把它
        交给 DCAM-SDK，不会因为越界或未对齐被拒绝。
    """
    # 1) 把宽高合法化（不在预设里就回落到默认）。
    roi_width, roi_height = coerce_sim_camera_size(width, height, presets)
    # 2) 全幅 ROI 直接锚定到 (0, 0)，绕过对齐计算。
    if is_full_frame_sim_camera_size(roi_width, roi_height, sensor_size=sensor_size, presets=presets):
        return roi_width, roi_height, 0, 0

    # 3) 非全幅：算上界、对齐 x、对齐 y，最终四元组同时满足 SDK 步进与边界条件。
    max_x, max_y = sim_camera_roi_origin_bounds(roi_width, roi_height, sensor_size=sensor_size, presets=presets)
    normalized_x = _align_sim_camera_roi_origin(roi_x, max_x, step_px=step_px)
    normalized_y = _align_sim_camera_roi_origin(roi_y, max_y, step_px=step_px)
    return roi_width, roi_height, normalized_x, normalized_y


def fit_image_size_to_bounds(
    image_width: int,
    image_height: int,
    bounds_width: int,
    bounds_height: int,
) -> tuple[int, int]:
    """按等比缩放把图像尺寸塞进给定的矩形 bounds，返回缩放后的 (width, height)。

    用途：
        预览窗口要让相机帧"完整可见且不变形"，调用本函数得到合适的缩放尺寸；
        相比直接除算，本函数保证宽高至少为 1，并夹到 bounds 内防越界。
    """
    # 1) 把所有输入夹到 ≥1，避免除零和负数缩放。
    image_width = max(int(image_width), 1)
    image_height = max(int(image_height), 1)
    bounds_width = max(int(bounds_width), 1)
    bounds_height = max(int(bounds_height), 1)

    # 2) 取宽/高方向缩放比例的较小值，保证图像在两个方向都不超过 bounds。
    scale = min(bounds_width / image_width, bounds_height / image_height)
    # 3) 把缩放后的宽高夹回 bounds，并至少为 1，避免渲染层出现 0 尺寸。
    scaled_width = min(bounds_width, max(1, int(round(image_width * scale))))
    scaled_height = min(bounds_height, max(1, int(round(image_height * scale))))
    return scaled_width, scaled_height

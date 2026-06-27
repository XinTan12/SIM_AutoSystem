"""SIM 相机 ROI 预设与对齐工具测试。

作用：
    覆盖 ``sim_camera_presets`` 提供的预设/对齐工具：
        1. 默认预设三档（2304/1152/576）与标签字符串约定。
        2. 旧自定义尺寸（如 512×400）回落到默认全幅。
        3. ``fit_image_size_to_bounds`` 保持长宽比 + 不超 bounds。
        4. ``normalize_sim_camera_roi``：
           a. 全幅强制 (0, 0)；
           b. 任意 ROI 原点夹到 ``sensor_size - roi_size`` 范围；
           c. 部分帧时按 4 像素步进向下对齐。

协作关系：
    上游：``unittest``。
    下游：``sim_control.sim_camera_presets`` 的常量与 helper。

维护要点：
    - 预设元组与标签字符串约定写在 ``sim_camera_presets.py``；更改时同步本测试期望。
    - Step px 默认 4：若 Fusion BT 换用其它对齐步进，需要更新这里所有"对齐 4"的断言。
"""

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class SimCameraPresetTests(unittest.TestCase):
    """覆盖 SIM 相机尺寸预设、标签与 ROI 对齐规则。"""

    def test_supported_sizes_round_trip_without_fallback(self):
        """默认三档预设与标签应一一对应；coerce 命中预设直接返回。"""
        from sim_control.sim_camera_presets import (
            DEFAULT_SIM_CAMERA_SIZE,
            SIM_CAMERA_SIZE_PRESET_LABELS,
            coerce_sim_camera_size,
            label_for_sim_camera_size,
        )

        # 1) 默认与预设标签锁死；这些字符串会在 GUI 下拉显示。
        self.assertEqual(DEFAULT_SIM_CAMERA_SIZE, (2304, 2304))
        self.assertEqual(SIM_CAMERA_SIZE_PRESET_LABELS, ("2304 x 2304", "1152 x 1152", "576 x 576"))
        # 2) 三档尺寸都应原样返回。
        self.assertEqual(coerce_sim_camera_size(2304, 2304), (2304, 2304))
        self.assertEqual(coerce_sim_camera_size(1152, 1152), (1152, 1152))
        self.assertEqual(coerce_sim_camera_size(576, 576), (576, 576))
        # 3) ``label_for_sim_camera_size`` 命中预设 → 拼接成 ``"W x H"``。
        self.assertEqual(label_for_sim_camera_size(1152, 1152), "1152 x 1152")

    def test_legacy_custom_size_falls_back_to_default(self):
        """非预设尺寸（旧自定义 ROI）应回落到全幅默认。"""
        from sim_control.sim_camera_presets import coerce_sim_camera_size, label_for_sim_camera_size

        # 512×400 / 608×304 都不在预设里；应回落到 2304×2304。
        self.assertEqual(coerce_sim_camera_size(512, 400), (2304, 2304))
        self.assertEqual(coerce_sim_camera_size(608, 304), (2304, 2304))
        self.assertEqual(label_for_sim_camera_size(512, 400), "2304 x 2304")

    def test_fit_image_size_to_bounds_keeps_aspect_ratio_inside_box(self):
        """各种长宽比下的图像缩放都应保持比例且不超过 bounds。"""
        from sim_control.sim_camera_presets import fit_image_size_to_bounds

        # 1) 正方形 → 直接填满 bounds。
        self.assertEqual(fit_image_size_to_bounds(2304, 2304, 460, 460), (460, 460))
        # 2) 横向长方形 → 受宽限制，高度按比例缩。
        self.assertEqual(fit_image_size_to_bounds(2304, 1152, 460, 460), (460, 230))
        # 3) 纵向长方形 → 受高限制，宽度按比例缩。
        self.assertEqual(fit_image_size_to_bounds(1152, 2304, 460, 460), (230, 460))

    def test_normalize_sim_camera_roi_for_full_frame_forces_zero_origin(self):
        """全幅 ROI 必须把原点强制写回 (0, 0)，避免用户输入造成越界。"""
        from sim_control.sim_camera_presets import normalize_sim_camera_roi

        # 即使用户填了 (270, 245)，全幅时也应回零。
        self.assertEqual(
            normalize_sim_camera_roi(2304, 2304, 270, 245),
            (2304, 2304, 0, 0),
        )

    def test_normalize_sim_camera_roi_clamps_1152_origin_to_sensor_bounds(self):
        """1152×1152 ROI 原点应被夹到 ``sensor_size - roi_size = 1152``。"""
        from sim_control.sim_camera_presets import normalize_sim_camera_roi

        # 用户填 (2000, 1500) 越界 → 被夹到 (1152, 1152)。
        self.assertEqual(
            normalize_sim_camera_roi(1152, 1152, 2000, 1500),
            (1152, 1152, 1152, 1152),
        )

    def test_normalize_sim_camera_roi_clamps_576_origin_to_sensor_bounds(self):
        """576×576 ROI：x 越界 / y 负值都应夹到合法区间。"""
        from sim_control.sim_camera_presets import normalize_sim_camera_roi

        # x=2000 越界 → 夹到 ``2304-576=1728``；y=-10 负值 → 夹到 0。
        self.assertEqual(
            normalize_sim_camera_roi(576, 576, 2000, -10),
            (576, 576, 1728, 0),
        )

    def test_normalize_sim_camera_roi_snaps_partial_frame_origin_to_four_pixel_grid(self):
        """部分帧 ROI 原点必须向下对齐到 4 像素步进。"""
        from sim_control.sim_camera_presets import normalize_sim_camera_roi

        # (270, 245) → 向下对齐到 (268, 244)，保证 DCAM SUBARRAY_HPOS 不被 SDK 拒。
        self.assertEqual(
            normalize_sim_camera_roi(1152, 1152, 270, 245),
            (1152, 1152, 268, 244),
        )

    def test_centered_sim_camera_roi_origin_uses_runtime_sensor_size(self):
        """用户选择非全幅 ROI 时，默认原点应落在当前完整视场中心。"""
        from sim_control.sim_camera_presets import centered_sim_camera_roi_origin

        presets = ((2048, 2048), (1024, 1024), (512, 512))

        self.assertEqual(
            centered_sim_camera_roi_origin(2048, 2048, sensor_size=(2048, 2048), presets=presets),
            (0, 0),
        )
        self.assertEqual(
            centered_sim_camera_roi_origin(1024, 1024, sensor_size=(2048, 2048), presets=presets),
            (512, 512),
        )
        self.assertEqual(
            centered_sim_camera_roi_origin(512, 512, sensor_size=(2048, 2048), presets=presets),
            (768, 768),
        )


if __name__ == "__main__":
    unittest.main()

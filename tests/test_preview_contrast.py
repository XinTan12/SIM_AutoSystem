"""``preview_contrast`` 模块的 uint16→uint8 灰度映射测试。

作用：
    覆盖 SIM live 预览灰度链路的关键路径：
        1. 手动灰度上限路径（``manual_uint16_to_uint8``）。
        2. 自动百分位对比度（``auto_uint16_to_uint8``、``AutoContrastState``）。
        3. fast 路径：先 clip → resize → LUT 与"先 LUT → resize"两种顺序结果差异 ≤1。
        4. ``AutoContrastState`` 在帧形状变化时被重置；在常量帧上不崩溃返回全黑。
        5. ``_manual_lut`` 缓存命中、只读且不影响后续帧。

协作关系：
    上游：``unittest``、``numpy``、``opencv-python``。
    下游：``sim_control.preview_contrast``。

维护要点：
    - 用例中包含与 OpenCV 路径的差异断言（≤1 灰度级）；ROI / LUT 算法改动后可能需要放宽。
    - ``AutoContrastState`` 默认百分位窗口 (0.5, 99.5)，调整需同步用例中的预期值。
"""

import unittest

import numpy as np

from sim_control.preview_contrast import (
    AutoContrastState,
    auto_uint16_to_uint8,
    fast_auto_preview_uint16_to_uint8,
    fast_manual_preview_uint16_to_uint8,
    _manual_lut,
    manual_uint16_to_uint8,
)


class PreviewContrastTests(unittest.TestCase):
    """覆盖 uint16→uint8 预览映射的手动 / 自动 / fast 三套路径。"""

    def test_manual_conversion_matches_existing_gray_max_scaling(self):
        """0/1000/5000/70000 在 gray_max=5000 时映射到 0/51/255/255。"""
        # 取一个含越界值 70000 的样本，验证 ``clip + cast`` 正确把超过上限的像素夹到 255。
        frame = np.array([[0, 1000, 5000, 70000]], dtype=np.uint32)

        result = manual_uint16_to_uint8(frame, gray_max=5000)

        np.testing.assert_array_equal(result, np.array([[0, 51, 255, 255]], dtype=np.uint8))

    def test_auto_contrast_maps_percentile_range_to_full_display_range(self):
        """0..100 百分位 + alpha=1.0 时，0..999 → 0..255 全量映射。"""
        # 0~999 等差序列；smoothing_alpha=1.0 让"第一帧"直接采用估计值。
        frame = np.arange(1000, dtype=np.uint16).reshape(20, 50)
        state = AutoContrastState(low_percentile=0.0, high_percentile=100.0, smoothing_alpha=1.0)

        result = auto_uint16_to_uint8(frame, state)

        self.assertEqual(int(result.min()), 0)
        self.assertEqual(int(result.max()), 255)
        self.assertAlmostEqual(state.lo, 0.0)
        self.assertAlmostEqual(state.hi, 999.0)

    def test_auto_contrast_ignores_single_bright_outlier_with_percentile_limits(self):
        """单像素 65000 高亮异常不应把整体窗口拉到饱和。"""
        # 主体亮度 1000~2000；单像素 65000 是离群点，百分位窗口 (0.5, 99.5) 应剔除它。
        frame = np.full((100, 100), 1000, dtype=np.uint16)
        frame[:, 50:] = 2000
        frame[0, 0] = 65000
        state = AutoContrastState(low_percentile=0.5, high_percentile=99.5, smoothing_alpha=1.0)

        result = auto_uint16_to_uint8(frame, state)

        # 窗口高位应远小于 65000（被百分位裁剪），离群点仍显示为饱和 255。
        self.assertLess(state.hi, 65000)
        self.assertEqual(int(result[0, 0]), 255)
        self.assertEqual(int(result[50, 75]), 255)

    def test_auto_contrast_constant_frame_returns_black_without_crashing(self):
        """常量帧 hi==lo 时返回全黑而非抛异常。"""
        # 全 1234 的常量帧；百分位窗口必然 hi<=lo，函数应安全返回 zeros 而非除零崩溃。
        frame = np.full((8, 8), 1234, dtype=np.uint16)
        state = AutoContrastState()

        result = auto_uint16_to_uint8(frame, state)

        np.testing.assert_array_equal(result, np.zeros((8, 8), dtype=np.uint8))

    def test_auto_contrast_smooths_hi_lo_across_frames(self):
        """连续两帧 + smoothing_alpha=0.25 时，第二帧 lo/hi 应按 (1-α)·prev + α·new 融合。"""
        # 首帧 lo=0, hi=100；第二帧 lo=1000, hi=2000；α=0.25 → 期望 lo=250, hi=575。
        state = AutoContrastState(low_percentile=0.0, high_percentile=100.0, smoothing_alpha=0.25)
        first = np.array([[0, 100]], dtype=np.uint16)
        second = np.array([[1000, 2000]], dtype=np.uint16)

        auto_uint16_to_uint8(first, state)
        auto_uint16_to_uint8(second, state)

        self.assertAlmostEqual(state.lo, 250.0)
        self.assertAlmostEqual(state.hi, 575.0)

    def test_auto_contrast_resets_when_frame_shape_changes(self):
        """帧形状变化时 ``state`` 应被重置：第二帧的 lo/hi 直接用新估计值，不再融合旧值。"""
        state = AutoContrastState(low_percentile=0.0, high_percentile=100.0, smoothing_alpha=0.25)
        # 1) 首帧 (1, 2) shape → 内部记录 frame_shape。
        auto_uint16_to_uint8(np.array([[0, 100]], dtype=np.uint16), state)

        # 2) 第二帧 (2, 1) shape 不同 → ``reset`` 被触发，平滑不发生。
        auto_uint16_to_uint8(np.array([[1000], [2000]], dtype=np.uint16), state)

        # 3) 期望 lo/hi 等于第二帧的极值（未与旧值融合）。
        self.assertAlmostEqual(state.lo, 1000.0)
        self.assertAlmostEqual(state.hi, 2000.0)

    def test_fast_manual_preview_clips_before_resize_to_limit_display_difference(self):
        """fast 手动路径与"原顺序"路径的最大灰度差应 ≤1。"""
        import cv2

        # 构造高对比度条纹帧 64×64，包含 0/65535 两种极值。
        frame = np.zeros((64, 64), dtype=np.uint16)
        frame[:, ::2] = 65535
        gray_max = 10_000
        output_size = (16, 16)

        # 1) "原顺序"路径：先做 LUT 映射，再 cv2.resize 到 16×16。
        current = cv2.resize(
            manual_uint16_to_uint8(frame, gray_max),
            output_size,
            interpolation=cv2.INTER_AREA,
        )
        # 2) fast 路径：先 clip → resize → LUT，理论上结果应几乎一致。
        fast = fast_manual_preview_uint16_to_uint8(frame, gray_max, output_size)

        # 3) 最大差异 ≤1 灰度级；shape 与 dtype 也要严格匹配。
        diff = np.abs(current.astype(np.int16) - fast.astype(np.int16))
        self.assertLessEqual(int(diff.max()), 1)
        self.assertEqual(fast.shape, (16, 16))
        self.assertEqual(fast.dtype, np.uint8)

    def test_manual_lut_reuses_cached_table_for_same_gray_max(self):
        """同一 gray_max 多次调用 ``_manual_lut`` 命中缓存；不同 gray_max 返回不同对象。"""
        # 1) 清缓存，让本测试不受其它用例污染。
        _manual_lut.cache_clear()

        first = _manual_lut(10_000.0)
        second = _manual_lut(10_000.0)
        different = _manual_lut(12_000.0)

        # 2) ``is`` 比较确保缓存命中（返回同一对象引用）。
        self.assertIs(first, second)
        self.assertIsNot(first, different)
        # 3) LUT 设为只读，避免下游意外原位修改影响其它帧。
        self.assertFalse(first.flags.writeable)

    def test_fast_auto_preview_clips_outliers_before_resize_to_limit_display_difference(self):
        """自动对比度 fast 路径与"原顺序"路径在 lo/hi 与显示差异上都应一致。"""
        import cv2

        # 构造含离群点 65535 的 80×80 噪声帧；目标尺寸 20×20。
        rng = np.random.default_rng(123)
        frame = rng.integers(0, 12_000, size=(80, 80), dtype=np.uint16)
        frame[::8, ::8] = 65535
        # 2) 两个独立的 state 副本：保证两条路径互不干扰。
        current_state = AutoContrastState(smoothing_alpha=1.0, max_sample_pixels=10_000)
        fast_state = AutoContrastState(smoothing_alpha=1.0, max_sample_pixels=10_000)
        output_size = (20, 20)

        # 3) "原顺序"路径：先 auto 映射全尺寸，再 cv2.resize。
        current = cv2.resize(
            auto_uint16_to_uint8(frame, current_state),
            output_size,
            interpolation=cv2.INTER_AREA,
        )
        # 4) fast 路径：先估计窗口 → clip → resize → LUT。
        fast = fast_auto_preview_uint16_to_uint8(frame, fast_state, output_size)

        # 5) 比较显示差异（≤1 灰度级）以及两条路径估计出的 lo/hi 是否一致。
        diff = np.abs(current.astype(np.int16) - fast.astype(np.int16))
        self.assertLessEqual(int(diff.max()), 1)
        self.assertAlmostEqual(fast_state.lo, current_state.lo)
        self.assertAlmostEqual(fast_state.hi, current_state.hi)
        self.assertEqual(fast.shape, (20, 20))
        self.assertEqual(fast.dtype, np.uint8)


if __name__ == "__main__":
    unittest.main()

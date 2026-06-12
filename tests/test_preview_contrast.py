"""``preview_contrast`` 模块的 uint16→uint8 灰度映射测试。

作用：
    覆盖 SIM live 预览灰度链路的关键路径：
        1. 手动灰度上限路径（``manual_uint16_to_uint8``）。
        2. 自动百分位对比度（``auto_uint16_to_uint8``、``AutoContrastState``）。
        3. fast 路径（resize-before-LUT）：先 INTER_AREA resize uint16，再 LUT 映射；
           无离群值时与参考路径（先 uint8 转换再 resize）差 ≤1 灰度级。
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

    def test_fast_manual_preview_resize_then_clip_saturates_averaged_pixels(self):
        """fast 路径先 resize 再 clip：下采样后的中间值若超过 gray_max 即饱和输出 255。

        与"原顺序"（LUT → resize）不同：resize-before-clip 先将 0/65535 交替列平均成
        ~32767，再 clip 到 gray_max(10000)，LUT 映射后全部输出 255。这对预览帧是可接受
        的权衡——真实场景中 SIM 帧不会全是饱和条纹。
        """
        # 构造高对比度条纹帧 64×64，包含 0/65535 两种极值。
        frame = np.zeros((64, 64), dtype=np.uint16)
        frame[:, ::2] = 65535
        gray_max = 10_000
        output_size = (16, 16)

        fast = fast_manual_preview_uint16_to_uint8(frame, gray_max, output_size)

        # resize(0/65535) → ~32767 > gray_max → clip → 全部 = gray_max → LUT → 255。
        self.assertEqual(int(fast.max()), 255)
        self.assertEqual(int(fast.min()), 255)
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

    def test_fast_auto_preview_resize_before_lut_matches_reference_for_outlier_free_frames(self):
        """auto 对比度 fast 路径（resize-before-LUT）：无离群值时与参考路径差 ≤1，lo/hi 一致。

        新行为：先 INTER_AREA 下采样 uint16，再 LUT 映射——与 manual 路径对称。
        参考路径：先 auto 全帧映射到 uint8，再 cv2 INTER_AREA resize。
        两条路径在无离群值时输出差 ≤1 灰度级，lo/hi 估计完全一致（均在全尺寸帧估计）。
        """
        import cv2

        # 构造无离群点、均匀分布在 1000..12000 的 100×100 帧。
        rng = np.random.default_rng(42)
        frame = rng.integers(1000, 12_000, size=(100, 100), dtype=np.uint16)
        output_size = (25, 25)

        ref_state = AutoContrastState(smoothing_alpha=1.0, max_sample_pixels=10_000)
        fast_state = AutoContrastState(smoothing_alpha=1.0, max_sample_pixels=10_000)

        # 参考路径：先全帧 auto→uint8，再 cv2 INTER_AREA resize。
        ref = cv2.resize(
            auto_uint16_to_uint8(frame, ref_state),
            output_size,
            interpolation=cv2.INTER_AREA,
        )
        # fast 路径：先 resize uint16，再 LUT（resize-before-LUT）。
        fast = fast_auto_preview_uint16_to_uint8(frame, fast_state, output_size)

        # 1) 输出形状与数据类型。
        self.assertEqual(fast.shape, (25, 25))
        self.assertEqual(fast.dtype, np.uint8)
        # 2) lo/hi 窗口估计一致（百分位估计不受 resize 影响）。
        self.assertAlmostEqual(fast_state.lo, ref_state.lo, delta=1.0)
        self.assertAlmostEqual(fast_state.hi, ref_state.hi, delta=1.0)
        # 3) 无离群值时两路径最大差 ≤1 灰度级。
        diff = np.abs(ref.astype(np.int16) - fast.astype(np.int16))
        self.assertLessEqual(int(diff.max()), 1)


if __name__ == "__main__":
    unittest.main()

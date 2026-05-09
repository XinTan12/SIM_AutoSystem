import unittest

import numpy as np

from sim_control.preview_contrast import (
    AutoContrastState,
    auto_uint16_to_uint8,
    fast_auto_preview_uint16_to_uint8,
    fast_manual_preview_uint16_to_uint8,
    manual_uint16_to_uint8,
)


class PreviewContrastTests(unittest.TestCase):
    def test_manual_conversion_matches_existing_gray_max_scaling(self):
        frame = np.array([[0, 1000, 5000, 70000]], dtype=np.uint32)

        result = manual_uint16_to_uint8(frame, gray_max=5000)

        np.testing.assert_array_equal(result, np.array([[0, 51, 255, 255]], dtype=np.uint8))

    def test_auto_contrast_maps_percentile_range_to_full_display_range(self):
        frame = np.arange(1000, dtype=np.uint16).reshape(20, 50)
        state = AutoContrastState(low_percentile=0.0, high_percentile=100.0, smoothing_alpha=1.0)

        result = auto_uint16_to_uint8(frame, state)

        self.assertEqual(int(result.min()), 0)
        self.assertEqual(int(result.max()), 255)
        self.assertAlmostEqual(state.lo, 0.0)
        self.assertAlmostEqual(state.hi, 999.0)

    def test_auto_contrast_ignores_single_bright_outlier_with_percentile_limits(self):
        frame = np.full((100, 100), 1000, dtype=np.uint16)
        frame[:, 50:] = 2000
        frame[0, 0] = 65000
        state = AutoContrastState(low_percentile=0.5, high_percentile=99.5, smoothing_alpha=1.0)

        result = auto_uint16_to_uint8(frame, state)

        self.assertLess(state.hi, 65000)
        self.assertEqual(int(result[0, 0]), 255)
        self.assertEqual(int(result[50, 75]), 255)

    def test_auto_contrast_constant_frame_returns_black_without_crashing(self):
        frame = np.full((8, 8), 1234, dtype=np.uint16)
        state = AutoContrastState()

        result = auto_uint16_to_uint8(frame, state)

        np.testing.assert_array_equal(result, np.zeros((8, 8), dtype=np.uint8))

    def test_auto_contrast_smooths_hi_lo_across_frames(self):
        state = AutoContrastState(low_percentile=0.0, high_percentile=100.0, smoothing_alpha=0.25)
        first = np.array([[0, 100]], dtype=np.uint16)
        second = np.array([[1000, 2000]], dtype=np.uint16)

        auto_uint16_to_uint8(first, state)
        auto_uint16_to_uint8(second, state)

        self.assertAlmostEqual(state.lo, 250.0)
        self.assertAlmostEqual(state.hi, 575.0)

    def test_auto_contrast_resets_when_frame_shape_changes(self):
        state = AutoContrastState(low_percentile=0.0, high_percentile=100.0, smoothing_alpha=0.25)
        auto_uint16_to_uint8(np.array([[0, 100]], dtype=np.uint16), state)

        auto_uint16_to_uint8(np.array([[1000], [2000]], dtype=np.uint16), state)

        self.assertAlmostEqual(state.lo, 1000.0)
        self.assertAlmostEqual(state.hi, 2000.0)

    def test_fast_manual_preview_clips_before_resize_to_limit_display_difference(self):
        import cv2

        frame = np.zeros((64, 64), dtype=np.uint16)
        frame[:, ::2] = 65535
        gray_max = 10_000
        output_size = (16, 16)

        current = cv2.resize(
            manual_uint16_to_uint8(frame, gray_max),
            output_size,
            interpolation=cv2.INTER_AREA,
        )
        fast = fast_manual_preview_uint16_to_uint8(frame, gray_max, output_size)

        diff = np.abs(current.astype(np.int16) - fast.astype(np.int16))
        self.assertLessEqual(int(diff.max()), 1)
        self.assertEqual(fast.shape, (16, 16))
        self.assertEqual(fast.dtype, np.uint8)

    def test_fast_auto_preview_clips_outliers_before_resize_to_limit_display_difference(self):
        import cv2

        rng = np.random.default_rng(123)
        frame = rng.integers(0, 12_000, size=(80, 80), dtype=np.uint16)
        frame[::8, ::8] = 65535
        current_state = AutoContrastState(smoothing_alpha=1.0, max_sample_pixels=10_000)
        fast_state = AutoContrastState(smoothing_alpha=1.0, max_sample_pixels=10_000)
        output_size = (20, 20)

        current = cv2.resize(
            auto_uint16_to_uint8(frame, current_state),
            output_size,
            interpolation=cv2.INTER_AREA,
        )
        fast = fast_auto_preview_uint16_to_uint8(frame, fast_state, output_size)

        diff = np.abs(current.astype(np.int16) - fast.astype(np.int16))
        self.assertLessEqual(int(diff.max()), 1)
        self.assertAlmostEqual(fast_state.lo, current_state.lo)
        self.assertAlmostEqual(fast_state.hi, current_state.hi)
        self.assertEqual(fast.shape, (20, 20))
        self.assertEqual(fast.dtype, np.uint8)


if __name__ == "__main__":
    unittest.main()

"""DCAM external-level trigger gap calculation regression tests."""

from __future__ import annotations

import math
import unittest

from sim_control.camera_timing import (
    CAMERA_TRIGGER_GAP_SAFETY_MARGIN_US,
    camera_config_signature,
    calculate_camera_trigger_gap,
)
from sim_control.models import CameraConfig


class CameraTriggerGapTests(unittest.TestCase):
    def test_uses_fixed_500_us_safety_margin(self):
        result = calculate_camera_trigger_gap(
            exposure_us=1_000,
            readout_s=0.002534,
            cyclic_s=0.002,
            min_tb_s=0.002583,
        )

        self.assertEqual(CAMERA_TRIGGER_GAP_SAFETY_MARGIN_US, 500)
        self.assertEqual(result["theoretical_min_inter_frame_gap_us"], 2_583)
        self.assertEqual(result["inter_frame_gap_safety_margin_us"], 500)
        self.assertEqual(result["recommended_inter_frame_gap_us"], 3_083)
        self.assertFalse(result["timing_fallback_used"])
        self.assertIsNone(result["timing_fallback_reason"])

    def test_strict_cyclic_constraint_advances_exact_boundary_by_one_microsecond(self):
        result = calculate_camera_trigger_gap(
            exposure_us=10_000,
            readout_s=0.031649,
            cyclic_s=0.0155,
            min_tb_s=0.0,
        )

        self.assertEqual(result["theoretical_min_inter_frame_gap_us"], 5_501)
        self.assertEqual(result["recommended_inter_frame_gap_us"], 6_001)

    def test_min_trigger_blanking_is_rounded_up_and_can_dominate(self):
        result = calculate_camera_trigger_gap(
            exposure_us=1_000,
            readout_s=0.002534,
            cyclic_s=0.0015,
            min_tb_s=0.002583001,
        )

        self.assertEqual(result["theoretical_min_inter_frame_gap_us"], 2_584)
        self.assertEqual(result["recommended_inter_frame_gap_us"], 3_084)

    def test_exposure_equal_to_cyclic_period_only_uses_min_blanking(self):
        result = calculate_camera_trigger_gap(
            exposure_us=3_000,
            readout_s=0.002534,
            cyclic_s=0.003,
            min_tb_s=0.0003001,
        )

        self.assertEqual(result["theoretical_min_inter_frame_gap_us"], 301)
        self.assertEqual(result["recommended_inter_frame_gap_us"], 801)

    def test_exposure_above_cyclic_period_only_uses_min_blanking(self):
        result = calculate_camera_trigger_gap(
            exposure_us=3_001,
            readout_s=0.002534,
            cyclic_s=0.003,
            min_tb_s=0.0003001,
        )

        self.assertEqual(result["theoretical_min_inter_frame_gap_us"], 301)
        self.assertEqual(result["recommended_inter_frame_gap_us"], 801)

    def test_zero_cyclic_period_is_valid_and_means_no_cyclic_constraint(self):
        result = calculate_camera_trigger_gap(
            exposure_us=1_000,
            readout_s=0.002534,
            cyclic_s=0.0,
            min_tb_s=0.0,
        )

        self.assertEqual(result["theoretical_min_inter_frame_gap_us"], 0)
        self.assertEqual(result["recommended_inter_frame_gap_us"], 500)
        self.assertFalse(result["timing_fallback_used"])

    def test_readout_is_validated_but_not_added_to_formal_gap_formula(self):
        short_readout = calculate_camera_trigger_gap(
            exposure_us=10_000,
            readout_s=0.001,
            cyclic_s=0.0155,
            min_tb_s=0.0003,
        )
        long_readout = calculate_camera_trigger_gap(
            exposure_us=10_000,
            readout_s=0.1,
            cyclic_s=0.0155,
            min_tb_s=0.0003,
        )

        self.assertEqual(
            short_readout["theoretical_min_inter_frame_gap_us"],
            long_readout["theoretical_min_inter_frame_gap_us"],
        )
        self.assertEqual(
            short_readout["recommended_inter_frame_gap_us"],
            long_readout["recommended_inter_frame_gap_us"],
        )

    def test_invalid_readout_returns_fallback_metadata(self):
        for invalid_value in (None, 0.0, -0.1, math.nan, math.inf, -math.inf):
            with self.subTest(readout_s=invalid_value):
                result = calculate_camera_trigger_gap(
                    exposure_us=1_000,
                    readout_s=invalid_value,
                    cyclic_s=0.002,
                    min_tb_s=0.0003,
                )

                self._assert_fallback(result, "TIMING_READOUTTIME")

    def test_invalid_cyclic_period_returns_fallback_metadata(self):
        for invalid_value in (None, -0.1, math.nan, math.inf, -math.inf):
            with self.subTest(cyclic_s=invalid_value):
                result = calculate_camera_trigger_gap(
                    exposure_us=1_000,
                    readout_s=0.0025,
                    cyclic_s=invalid_value,
                    min_tb_s=0.0003,
                )

                self._assert_fallback(result, "TIMING_CYCLICTRIGGERPERIOD")

    def test_invalid_min_trigger_blanking_returns_fallback_metadata(self):
        for invalid_value in (None, -0.1, math.nan, math.inf, -math.inf):
            with self.subTest(min_tb_s=invalid_value):
                result = calculate_camera_trigger_gap(
                    exposure_us=1_000,
                    readout_s=0.0025,
                    cyclic_s=0.002,
                    min_tb_s=invalid_value,
                )

                self._assert_fallback(result, "TIMING_MINTRIGGERBLANKING")

    def _assert_fallback(self, result, expected_property_name: str) -> None:
        self.assertIsNone(result["theoretical_min_inter_frame_gap_us"])
        self.assertEqual(result["inter_frame_gap_safety_margin_us"], 500)
        self.assertIsNone(result["recommended_inter_frame_gap_us"])
        self.assertTrue(result["timing_fallback_used"])
        self.assertIn(expected_property_name, result["timing_fallback_reason"])


class CameraConfigSignatureTests(unittest.TestCase):
    def test_camera_config_object_uses_fixed_signature_field_order(self):
        config = CameraConfig(
            device_index=2,
            device_label="2: ORCA-Fusion BT [CAM-002]",
            roi_x=568,
            roi_y=764,
            roi_width=512,
            roi_height=512,
            exposure_us=3_000,
            bit_depth=16,
            trigger_mode="external_level",
        )

        self.assertEqual(
            camera_config_signature(config),
            (
                2,
                "2: ORCA-Fusion BT [CAM-002]",
                568,
                764,
                512,
                512,
                3_000,
                16,
                "external_level",
            ),
        )

    def test_mapping_and_camera_config_produce_the_same_signature(self):
        values = {
            "device_index": 1,
            "device_label": "1: ORCA-Fusion BT [CAM-001]",
            "roi_x": 4,
            "roi_y": 8,
            "roi_width": 1_152,
            "roi_height": 1_152,
            "exposure_us": 10_000,
            "bit_depth": 12,
            "trigger_mode": "external_level",
        }
        config = CameraConfig(**values)
        payload = {**values, "recommended_inter_frame_gap_us": 6_001}

        self.assertEqual(camera_config_signature(payload), camera_config_signature(config))


if __name__ == "__main__":
    unittest.main()

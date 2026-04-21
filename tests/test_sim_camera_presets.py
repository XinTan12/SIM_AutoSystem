import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class SimCameraPresetTests(unittest.TestCase):
    def test_supported_sizes_round_trip_without_fallback(self):
        from sim_control.sim_camera_presets import (
            DEFAULT_SIM_CAMERA_SIZE,
            SIM_CAMERA_SIZE_PRESET_LABELS,
            coerce_sim_camera_size,
            label_for_sim_camera_size,
        )

        self.assertEqual(DEFAULT_SIM_CAMERA_SIZE, (2304, 2304))
        self.assertEqual(SIM_CAMERA_SIZE_PRESET_LABELS, ("2304 x 2304", "1152 x 1152", "576 x 576"))
        self.assertEqual(coerce_sim_camera_size(2304, 2304), (2304, 2304))
        self.assertEqual(coerce_sim_camera_size(1152, 1152), (1152, 1152))
        self.assertEqual(coerce_sim_camera_size(576, 576), (576, 576))
        self.assertEqual(label_for_sim_camera_size(1152, 1152), "1152 x 1152")

    def test_legacy_custom_size_falls_back_to_default(self):
        from sim_control.sim_camera_presets import coerce_sim_camera_size, label_for_sim_camera_size

        self.assertEqual(coerce_sim_camera_size(512, 400), (2304, 2304))
        self.assertEqual(coerce_sim_camera_size(608, 304), (2304, 2304))
        self.assertEqual(label_for_sim_camera_size(512, 400), "2304 x 2304")

    def test_fit_image_size_to_bounds_keeps_aspect_ratio_inside_box(self):
        from sim_control.sim_camera_presets import fit_image_size_to_bounds

        self.assertEqual(fit_image_size_to_bounds(2304, 2304, 460, 460), (460, 460))
        self.assertEqual(fit_image_size_to_bounds(2304, 1152, 460, 460), (460, 230))
        self.assertEqual(fit_image_size_to_bounds(1152, 2304, 460, 460), (230, 460))

    def test_normalize_sim_camera_roi_for_full_frame_forces_zero_origin(self):
        from sim_control.sim_camera_presets import normalize_sim_camera_roi

        self.assertEqual(
            normalize_sim_camera_roi(2304, 2304, 270, 245),
            (2304, 2304, 0, 0),
        )

    def test_normalize_sim_camera_roi_clamps_1152_origin_to_sensor_bounds(self):
        from sim_control.sim_camera_presets import normalize_sim_camera_roi

        self.assertEqual(
            normalize_sim_camera_roi(1152, 1152, 2000, 1500),
            (1152, 1152, 1152, 1152),
        )

    def test_normalize_sim_camera_roi_clamps_576_origin_to_sensor_bounds(self):
        from sim_control.sim_camera_presets import normalize_sim_camera_roi

        self.assertEqual(
            normalize_sim_camera_roi(576, 576, 2000, -10),
            (576, 576, 1728, 0),
        )

    def test_normalize_sim_camera_roi_snaps_partial_frame_origin_to_four_pixel_grid(self):
        from sim_control.sim_camera_presets import normalize_sim_camera_roi

        self.assertEqual(
            normalize_sim_camera_roi(1152, 1152, 270, 245),
            (1152, 1152, 268, 244),
        )


if __name__ == "__main__":
    unittest.main()

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class SimSettingsSummaryTests(unittest.TestCase):
    def test_build_sim_settings_summary_omits_backend_block(self):
        from sim_control.models import AppConfig, BackendConfig, CameraConfig, DaqLineConfig
        from sim_control.summary import build_sim_settings_summary

        config = AppConfig(
            selected_laser_nm=488,
            camera=CameraConfig(
                device_index=0,
                device_label="0: ORCA-Fusion BT [CAM-001]",
                roi_x=10,
                roi_y=20,
                roi_width=512,
                roi_height=512,
                exposure_us=10_000,
            ),
            daq=DaqLineConfig(
                device_name="Dev2",
                slm_enable_line="Dev2/port0/line0",
                slm_trigger_line="Dev2/port0/line1",
                slm_finish_line="Dev2/port0/line2",
                camera_trigger_line="Dev2/port0/line3",
                laser_405_line="Dev2/port0/line4",
                laser_488_line="Dev2/port0/line5",
                laser_561_line="Dev2/port0/line6",
                laser_640_line="Dev2/port0/line7",
            ),
            backend=BackendConfig(
                fusion_bt_sdk_path="E:/sdk/dcam",
                slm_sdk_path="E:/sdk/r11",
            ),
        )

        summary = build_sim_settings_summary(config)

        self.assertIn("Laser: 488 nm", summary)
        self.assertIn("camera_trigger_line: Dev2/port0/line3", summary)
        self.assertNotIn("Backend:", summary)
        self.assertNotIn("fusion_bt_sdk_path", summary)
        self.assertNotIn("slm_sdk_path", summary)

    def test_build_sim_settings_summary_shows_bit_depth_and_runtime_timing(self):
        from sim_control.models import AppConfig, CameraConfig, TimingConfig
        from sim_control.summary import build_sim_settings_summary

        config = AppConfig(
            camera=CameraConfig(
                device_index=0,
                device_label="0: ORCA-Fusion BT [CAM-001]",
                exposure_us=20_000,
                bit_depth=12,
            ),
            timing=TimingConfig(inter_frame_gap_us=10_000),
        )

        summary = build_sim_settings_summary(
            config,
            runtime_timing={
                "timing_readout_time_s": 0.00561,
                "recommended_inter_frame_gap_us": 6500,
            },
        )

        self.assertIn("Bit Depth: 12-bit", summary)
        self.assertIn("TIMING_READOUTTIME: 5.610 ms", summary)
        self.assertIn("Actual Inter Frame Gap: 6500 us", summary)

    def test_build_sim_settings_summary_falls_back_to_config_gap_without_runtime_timing(self):
        from sim_control.models import AppConfig, CameraConfig, TimingConfig
        from sim_control.summary import build_sim_settings_summary

        config = AppConfig(
            camera=CameraConfig(bit_depth=16),
            timing=TimingConfig(inter_frame_gap_us=15000),
        )

        summary = build_sim_settings_summary(config)

        self.assertIn("Bit Depth: 16-bit", summary)
        self.assertIn("TIMING_READOUTTIME: -", summary)
        self.assertIn("Actual Inter Frame Gap: 15000 us", summary)


if __name__ == "__main__":
    unittest.main()

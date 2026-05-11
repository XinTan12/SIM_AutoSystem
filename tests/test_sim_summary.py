import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class SimSettingsSummaryTests(unittest.TestCase):
    def test_build_sim_settings_summary_omits_backend_block(self):
        from sim_control.config_store import app_config_from_dict
        from sim_control.summary import build_sim_settings_summary

        config = app_config_from_dict(
            {
                "selected_laser_nm": 488,
                "camera": {
                    "device_index": 0,
                    "device_label": "0: ORCA-Fusion BT [CAM-001]",
                    "roi_x": 10,
                    "roi_y": 20,
                    "roi_width": 512,
                    "roi_height": 512,
                    "exposure_us": 10_000,
                },
                "daq": {
                    "device_name": "Dev2",
                    "slm_enable_line": "Dev2/port0/line0",
                    "slm_trigger_line": "Dev2/port0/line1",
                    "slm_finish_line": "Dev2/port0/line2",
                    "camera_trigger_line": "Dev2/port0/line5",
                    "laser_405_line": "Dev2/port0/line8",
                    "laser_488_line": "Dev2/port0/line6",
                    "laser_561_line": "Dev2/port0/line7",
                    "laser_640_line": "Dev2/port0/line9",
                },
                "backend": {
                    "fusion_bt_sdk_path": "E:/sdk/dcam",
                    "slm_sdk_path": "E:/sdk/r11",
                },
            }
        )

        summary = build_sim_settings_summary(config)

        self.assertIn("Laser: 488 nm", summary)
        self.assertIn("Pattern RO: (SLM 未连接)", summary)
        self.assertIn("cam_trigger_line: Dev2/port0/line5", summary)
        self.assertNotIn("camera_trigger_line", summary)
        self.assertIn("laser_647_line: Dev2/port0/line9", summary)
        self.assertNotIn("laser_640_line", summary)
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
            timing=TimingConfig(
                sample_rate_hz=1_000_000,
                edge_pulse_us=50,
                inter_frame_gap_us=50_000,
                slm_enable_guard_us=50,
            ),
        )

        summary = build_sim_settings_summary(
            config,
            runtime_timing={
                "timing_readout_time_s": 0.031649,
                "recommended_inter_frame_gap_us": 32649,
            },
        )

        self.assertIn("Bit Depth: 12-bit", summary)
        self.assertIn("Pattern RO: (SLM 未连接)", summary)
        self.assertIn("TIMING_READOUTTIME: 31.649 ms", summary)
        self.assertTrue(
            summary.endswith(
                "\n".join(
                    [
                        "Timing:",
                        "  sample_rate_hz: 1000000",
                        "  edge_pulse_us: 50",
                        "  inter_frame_gap_us: 32649",
                        "  slm_enable_guard_us: 50",
                    ]
                )
            )
        )

    def test_build_sim_settings_summary_uses_default_gap_without_runtime_timing(self):
        from sim_control.models import AppConfig, CameraConfig, TimingConfig
        from sim_control.summary import build_sim_settings_summary

        config = AppConfig(
            camera=CameraConfig(bit_depth=16),
            timing=TimingConfig(inter_frame_gap_us=15000),
        )

        summary = build_sim_settings_summary(config)

        self.assertIn("Bit Depth: 16-bit", summary)
        self.assertIn("TIMING_READOUTTIME: -", summary)
        self.assertIn("  inter_frame_gap_us: 50000", summary)

    def test_build_sim_settings_summary_ignores_calculated_gap_at_or_above_default(self):
        from sim_control.models import AppConfig, TimingConfig
        from sim_control.summary import build_sim_settings_summary

        config = AppConfig(timing=TimingConfig(inter_frame_gap_us=15_000))

        for recommended_gap_us in (50_000, 65_000):
            with self.subTest(recommended_gap_us=recommended_gap_us):
                summary = build_sim_settings_summary(
                    config,
                    runtime_timing={"recommended_inter_frame_gap_us": recommended_gap_us},
                )

                self.assertIn("  inter_frame_gap_us: 50000", summary)

    def test_build_sim_settings_summary_shows_selected_running_order(self):
        from sim_control.models import AppConfig
        from sim_control.summary import build_sim_settings_summary

        config = AppConfig(selected_running_order="488_3.5_2d_1ms")

        summary = build_sim_settings_summary(config)

        self.assertIn("Pattern RO: 488_3.5_2d_1ms", summary)


if __name__ == "__main__":
    unittest.main()

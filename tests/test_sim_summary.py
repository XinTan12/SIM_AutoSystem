"""主界面 SIM 设置摘要文本测试。

作用：
    覆盖 ``summary.build_sim_settings_summary`` 五类场景：
        1. ``backend`` 段不出现在摘要里（避免暴露本机 SDK 绝对路径）；旧 640nm
           配置经迁移后必须显示 ``cam_trigger_line`` 与 ``laser_647_line``。
        2. 给定 runtime_timing 时，``Bit Depth``、``TIMING_READOUTTIME``、
           ``SIM9_ESTIMATED_TOTAL_TIME`` 都按相机回报的实际值显示，且最后一段是
           完整的 Timing 只读区块。
        3. 没有 runtime_timing 时使用默认 50ms 帧间隔；总时长公式不变。
        4. 总时长会随相机曝光变化（10ms → 550.100 ms vs 20ms → 640.100 ms）。
        5. 相机推荐间隔 ≥ 50ms 时仍使用 50ms 默认（不允许放慢默认时序）。
        6. ``selected_running_order`` 非空时摘要显示具体 RO 名而非"SLM 未连接"。

协作关系：
    上游：``unittest``。
    下游：``sim_control.summary.build_sim_settings_summary``、``sim_control.models``、
          ``sim_control.config_store.app_config_from_dict``。

维护要点：
    - 显示行顺序和精确文本与本测试耦合；修改摘要格式必须同步更新。
    - SIM9 总时长公式：``2 × guard + 9 × (exposure + gap)`` + 10 ms 整理开销；
      改公式会导致数字（如 550.100 / 640.100 / 483.941）全部失效。
"""

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class SimSettingsSummaryTests(unittest.TestCase):
    """覆盖主界面 SIM 设置摘要的字段顺序、命名迁移与总时长估算。"""

    def test_build_sim_settings_summary_omits_backend_block(self):
        """旧 640 配置迁移后，摘要使用 647 命名；``backend`` 段完全不出现。"""
        from sim_control.config_store import app_config_from_dict
        from sim_control.summary import build_sim_settings_summary

        # 1) 构造一份含 ``laser_640_line`` 与 ``backend`` SDK 路径的旧配置；
        #    经 ``app_config_from_dict`` 迁移后会变成 647 命名。
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

        # 2) 关键字段都按期望出现（命名、设备、行别名）。
        self.assertIn("Laser: 488 nm", summary)
        self.assertIn("Pattern RO: (SLM 未连接)", summary)
        # 3) ``cam_trigger_line`` 别名（GUI 显示简写）替代原始字段名。
        self.assertIn("cam_trigger_line: Dev2/port0/line5", summary)
        self.assertNotIn("camera_trigger_line", summary)
        # 4) 旧 640 / 新 647 命名必须互斥。
        self.assertIn("laser_647_line: Dev2/port0/line9", summary)
        self.assertNotIn("laser_640_line", summary)
        # 5) backend SDK 路径绝不出现：避免暴露本机敏感路径。
        self.assertNotIn("Backend:", summary)
        self.assertNotIn("fusion_bt_sdk_path", summary)
        self.assertNotIn("slm_sdk_path", summary)

    def test_build_sim_settings_summary_shows_bit_depth_and_runtime_timing(self):
        """``runtime_timing`` 给出读出时间与推荐间隔时，摘要应显示具体值。"""
        from sim_control.models import AppConfig, CameraConfig, TimingConfig
        from sim_control.summary import build_sim_settings_summary

        # 1) 配置 20 ms 曝光 / 12-bit / 标准 SIM9 时序。
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

        # 2) 模拟相机回报：31.649 ms 读出 + 32649 µs 推荐间隔（< 50 ms 触发覆盖）。
        summary = build_sim_settings_summary(
            config,
            runtime_timing={
                "timing_readout_time_s": 0.031649,
                "recommended_inter_frame_gap_us": 32649,
            },
        )

        # 3) 关键字段：位深 / 读出时间 / 总时长 / RO 状态 / 行顺序。
        self.assertIn("Bit Depth: 12-bit", summary)
        self.assertIn("Pattern RO: (SLM 未连接)", summary)
        self.assertIn("TIMING_READOUTTIME: 31.649 ms", summary)
        self.assertIn("SIM9_ESTIMATED_TOTAL_TIME: 483.941 ms", summary)
        # 4) ``TIMING_READOUTTIME`` 必须紧邻 ``SIM9_ESTIMATED_TOTAL_TIME``。
        lines = summary.splitlines()
        readout_index = lines.index("TIMING_READOUTTIME: 31.649 ms")
        self.assertEqual(lines[readout_index + 1], "SIM9_ESTIMATED_TOTAL_TIME: 483.941 ms")
        # 5) 末尾固定是只读 Timing 区块，含 4 个字段；``inter_frame_gap_us`` 使用推荐值。
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
        """缺失 runtime_timing → 帧间隔退回默认 50 ms；总时长按 50 ms 计算。"""
        from sim_control.models import AppConfig, CameraConfig, TimingConfig
        from sim_control.summary import build_sim_settings_summary

        # 1) 故意把 inter_frame_gap_us 设为 15 000（< 50 ms）；预期摘要仍显示 50 000，
        #    因为没有 runtime_timing 时 ``effective_inter_frame_gap_us(None)`` = 50_000。
        config = AppConfig(
            camera=CameraConfig(bit_depth=16),
            timing=TimingConfig(inter_frame_gap_us=15000),
        )

        summary = build_sim_settings_summary(config)

        self.assertIn("Bit Depth: 16-bit", summary)
        self.assertIn("TIMING_READOUTTIME: -", summary)
        # 2) 总时长 = 2 × 50 µs guard + 9 × (10 ms exposure + 50 ms gap) + 10 ms 整理 = 550.100 ms。
        self.assertIn("SIM9_ESTIMATED_TOTAL_TIME: 550.100 ms", summary)
        # 3) 默认间隔显示为 50000 µs；用户在 timing 字段填的 15000 不生效。
        self.assertIn("  inter_frame_gap_us: 50000", summary)

    def test_build_sim_settings_summary_estimate_changes_with_exposure(self):
        """曝光从 10 ms 增到 20 ms 时，总时长应严格按 9 帧差异增加。"""
        from sim_control.models import AppConfig, CameraConfig, TimingConfig
        from sim_control.summary import build_sim_settings_summary

        # 1) 两组配置只差曝光时间；总时长应相差 9 × 10 ms = 90 ms。
        ten_ms_config = AppConfig(
            camera=CameraConfig(exposure_us=10_000),
            timing=TimingConfig(sample_rate_hz=1_000_000, slm_enable_guard_us=50),
        )
        twenty_ms_config = AppConfig(
            camera=CameraConfig(exposure_us=20_000),
            timing=TimingConfig(sample_rate_hz=1_000_000, slm_enable_guard_us=50),
        )

        ten_ms_summary = build_sim_settings_summary(ten_ms_config)
        twenty_ms_summary = build_sim_settings_summary(twenty_ms_config)

        # 2) 数字严格相等：10ms → 550.100 ms；20ms → 640.100 ms。
        self.assertIn("SIM9_ESTIMATED_TOTAL_TIME: 550.100 ms", ten_ms_summary)
        self.assertIn("SIM9_ESTIMATED_TOTAL_TIME: 640.100 ms", twenty_ms_summary)

    def test_build_sim_settings_summary_ignores_calculated_gap_at_or_above_default(self):
        """相机推荐间隔 ≥ 50 ms 时摘要继续显示 50 ms，不允许放慢默认时序。"""
        from sim_control.models import AppConfig, TimingConfig
        from sim_control.summary import build_sim_settings_summary

        config = AppConfig(timing=TimingConfig(inter_frame_gap_us=15_000))

        # 50_000 与 65_000 都属于"≥ 50 ms"分支；摘要必须显示 50000。
        for recommended_gap_us in (50_000, 65_000):
            with self.subTest(recommended_gap_us=recommended_gap_us):
                summary = build_sim_settings_summary(
                    config,
                    runtime_timing={"recommended_inter_frame_gap_us": recommended_gap_us},
                )

                self.assertIn("  inter_frame_gap_us: 50000", summary)

    def test_build_sim_settings_summary_shows_selected_running_order(self):
        """``selected_running_order`` 非空时摘要显示具体 RO 名而非"SLM 未连接"。"""
        from sim_control.models import AppConfig
        from sim_control.summary import build_sim_settings_summary

        config = AppConfig(selected_running_order="488_3.5_2d_1ms")

        summary = build_sim_settings_summary(config)

        self.assertIn("Pattern RO: 488_3.5_2d_1ms", summary)


    def test_build_sim_settings_summary_includes_enabled_z_scan_block(self):
        """启用 z-scan 时摘要应显示 UI preset 和实际曝光时间。"""
        from sim_control.models import AppConfig, ZScanConfig
        from sim_control.summary import build_sim_settings_summary

        config = AppConfig(
            z_scan=ZScanConfig(
                enabled=True,
                start_um=12.5,
                direction="negative_z",
                step_um=0.4,
                num_steps=12,
                exposure_preset_ms=14,
            )
        )

        summary = build_sim_settings_summary(config)

        self.assertIn("Z-Scan:", summary)
        self.assertIn("  enabled: True", summary)
        self.assertIn("  start_um: 12.500", summary)
        self.assertIn("  direction: negative_z", summary)
        self.assertNotIn("  step_um:", summary)
        self.assertNotIn("  num_steps:", summary)
        self.assertIn("  scan_gap_nm: 400", summary)
        self.assertIn("  scan_moves: 12", summary)
        self.assertIn("  image_layers: 13", summary)
        self.assertIn("  total_distance_um: 4.800", summary)
        self.assertIn("  estimated_scan_time_ms: 506.792", summary)
        self.assertIn("  exposure_preset_ms: 14", summary)
        self.assertIn("  actual_exposure_us: 13884", summary)


if __name__ == "__main__":
    unittest.main()

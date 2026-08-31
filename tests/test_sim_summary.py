"""主界面 SIM 设置摘要文本测试。

作用：
    覆盖 ``summary.build_sim_settings_summary`` 五类场景：
        1. ``backend`` 段不出现在摘要里（避免暴露本机 SDK 绝对路径）；旧 640nm
           配置经迁移后，DAQ 线位等主 GUI 已显示字段也不再出现在摘要里。
        2. 给定且相机配置签名匹配的 runtime_timing 时，完整显示三个原始 timing、
           理论最小 gap、安全余量、最终有效 gap 与 fallback 状态。
        3. 没有 runtime_timing 时使用默认 50ms 帧间隔；总时长公式不变。
        4. 总时长会随相机曝光变化（10ms → 550.100 ms vs 20ms → 640.100 ms）。
        5. 相机推荐间隔 ≥ 50ms 时仍使用相机给出的合法值，不得截短为 50ms。
        6. preview 等旧 runtime_timing 的相机配置签名不匹配时，全部运行时值显示未知，
           并用 50ms fallback 估时。
        7. ``selected_running_order`` 非空时摘要显示具体 RO 名而非"SLM 未连接"。

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

    @staticmethod
    def _runtime_camera_config(camera):
        """返回 summary 用于拒绝 stale preview timing 的完整相机配置签名。"""
        return {
            "device_index": camera.device_index,
            "device_label": camera.device_label,
            "roi_x": camera.roi_x,
            "roi_y": camera.roi_y,
            "roi_width": camera.roi_width,
            "roi_height": camera.roi_height,
            "exposure_us": camera.exposure_us,
            "bit_depth": camera.bit_depth,
            "trigger_mode": camera.trigger_mode,
        }

    def test_build_sim_settings_summary_omits_backend_and_repeated_gui_config(self):
        """摘要不暴露 backend，也不重复显示主 GUI DAQ/相机/激光配置。"""
        from sim_control.config_store import app_config_from_dict
        from sim_control.summary import build_sim_settings_summary

        # 1) 构造一份含 ``laser_640_line`` 与 ``backend`` SDK 路径的旧配置；
        #    经 ``app_config_from_dict`` 迁移后会变成 638 命名。
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
                    "device_name": "Dev1",
                    "slm_enable_line": "Dev1/port0/line0",
                    "slm_trigger_line": "Dev1/port0/line1",
                    "slm_finish_line": "Dev1/port0/line2",
                    "camera_trigger_line": "Dev1/port0/line8",
                    "laser_405_line": "Dev1/port0/line9",
                    "laser_488_line": "Dev1/port0/line10",
                    "laser_561_line": "Dev1/port0/line11",
                    "laser_640_line": "Dev1/port0/line12",
                },
                "backend": {
                    "fusion_bt_sdk_path": "E:/sdk/dcam",
                    "slm_sdk_path": "E:/sdk/r11",
                },
            }
        )

        summary = build_sim_settings_summary(config)

        # 2) 只保留正式 RO 与派生/运行时信息；激光和 DAQ 线位已由主 GUI 模块显示。
        self.assertIn("Pattern RO: (SLM 未连接)", summary)
        self.assertNotIn("Laser: 488 nm", summary)
        self.assertNotIn("Selected SIM Camera", summary)
        self.assertNotIn("Exposure:", summary)
        self.assertNotIn("Bit Depth:", summary)
        self.assertNotIn("ROI:", summary)
        self.assertNotIn("DAQ:", summary)
        self.assertNotIn("device_name: Dev1", summary)
        self.assertNotIn("slm_enable_line", summary)
        self.assertNotIn("slm_trigger_line", summary)
        self.assertNotIn("slm_finish_line", summary)
        self.assertNotIn("cam_trigger_line", summary)
        self.assertNotIn("camera_trigger_line", summary)
        self.assertNotIn("laser_405_line", summary)
        self.assertNotIn("laser_488_line", summary)
        self.assertNotIn("laser_561_line", summary)
        self.assertNotIn("laser_red_line", summary)
        self.assertNotIn("laser_640_line", summary)
        self.assertNotIn("laser_647_line", summary)
        self.assertNotIn("laser_638_line", summary)
        # 3) backend SDK 路径绝不出现：避免暴露本机敏感路径。
        self.assertNotIn("Backend:", summary)
        self.assertNotIn("fusion_bt_sdk_path", summary)
        self.assertNotIn("slm_sdk_path", summary)

    def test_build_sim_settings_summary_shows_runtime_timing_without_repeated_camera_config(self):
        """``runtime_timing`` 给出读出时间与推荐间隔时，摘要应显示具体值。"""
        from sim_control.models import AppConfig, CameraConfig, TimingConfig
        from sim_control.summary import build_sim_settings_summary

        # 1) 配置 20 ms 曝光 / 12-bit / 标准 SIM9 时序；相机字段只参与总时长计算。
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

        # 2) 模拟与当前正式配置完全匹配的相机回报；严格不等式下理论最小
        #    gap=5.501 ms，固定安全余量=0.5 ms，最终有效 gap=6.001 ms。
        summary = build_sim_settings_summary(
            config,
            runtime_timing={
                "camera_config": self._runtime_camera_config(config.camera),
                "timing_readout_time_s": 0.031649,
                "timing_cyclic_trigger_period_s": 0.0255,
                "timing_min_trigger_blanking_s": 0.0003,
                "theoretical_min_inter_frame_gap_us": 5501,
                "inter_frame_gap_safety_margin_us": 500,
                "recommended_inter_frame_gap_us": 6001,
                "timing_fallback_used": False,
                "timing_fallback_reason": "",
            },
        )

        # 3) 关键字段：读出时间 / 总时长 / RO 状态 / 行顺序。
        self.assertNotIn("Bit Depth: 12-bit", summary)
        self.assertNotIn("Selected SIM Camera", summary)
        self.assertNotIn("Exposure:", summary)
        self.assertNotIn("ROI:", summary)
        self.assertIn("Pattern RO: (SLM 未连接)", summary)
        self.assertIn("TIMING_READOUTTIME: 31.649 ms", summary)
        self.assertIn("TIMING_CYCLICTRIGGERPERIOD: 25.500 ms", summary)
        self.assertIn("TIMING_MINTRIGGERBLANKING: 0.300 ms", summary)
        self.assertIn("TIMING_THEORETICAL_MIN_INTER_FRAME_GAP: 5.501 ms", summary)
        self.assertIn("TIMING_INTER_FRAME_GAP_SAFETY_MARGIN: 0.500 ms", summary)
        self.assertIn("TIMING_EFFECTIVE_INTER_FRAME_GAP: 6.001 ms", summary)
        self.assertIn("TIMING_FALLBACK_USED: no", summary)
        self.assertIn("TIMING_FALLBACK_REASON: -", summary)
        self.assertIn("SIM9_ESTIMATED_TOTAL_TIME: 244.109 ms", summary)
        # 4) 全部 timing 明细后才紧接 ``SIM9_ESTIMATED_TOTAL_TIME``。
        lines = summary.splitlines()
        effective_index = lines.index("TIMING_EFFECTIVE_INTER_FRAME_GAP: 6.001 ms")
        self.assertEqual(lines[effective_index + 3], "SIM9_ESTIMATED_TOTAL_TIME: 244.109 ms")
        # 5) 末尾固定是只读 Timing 区块，``inter_frame_gap_us`` 使用最终有效值。
        self.assertTrue(
            summary.endswith(
                "\n".join(
                    [
                        "Timing:",
                        "  sample_rate_hz: 1000000",
                        "  edge_pulse_us: 50",
                        "  inter_frame_gap_us: 6001",
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

        self.assertNotIn("Bit Depth: 16-bit", summary)
        self.assertIn("TIMING_READOUTTIME: -", summary)
        self.assertIn("TIMING_CYCLICTRIGGERPERIOD: -", summary)
        self.assertIn("TIMING_MINTRIGGERBLANKING: -", summary)
        self.assertIn("TIMING_THEORETICAL_MIN_INTER_FRAME_GAP: -", summary)
        self.assertIn("TIMING_INTER_FRAME_GAP_SAFETY_MARGIN: -", summary)
        self.assertIn("TIMING_EFFECTIVE_INTER_FRAME_GAP: 50.000 ms", summary)
        self.assertIn("TIMING_FALLBACK_USED: yes", summary)
        self.assertIn("TIMING_FALLBACK_REASON: runtime_timing_unavailable", summary)
        # 2) 总时长 = 2 × 1000 µs 默认 guard + 9 × (10 ms exposure + 50 ms gap) + 10 ms 整理 = 552.000 ms。
        self.assertIn("SIM9_ESTIMATED_TOTAL_TIME: 552.000 ms", summary)
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

    def test_build_sim_settings_summary_uses_valid_calculated_gap_at_or_above_default(self):
        """签名匹配的合法推荐值即使 ≥50 ms 也不得被截短。"""
        from sim_control.models import AppConfig, CameraConfig, TimingConfig
        from sim_control.summary import build_sim_settings_summary

        config = AppConfig(
            camera=CameraConfig(exposure_us=10_000),
            timing=TimingConfig(inter_frame_gap_us=15_000),
        )

        # 50_000 与 65_000 都属于合法计算结果；摘要必须保留原值。
        for recommended_gap_us, expected_total_ms in ((50_000, 552.0), (65_000, 687.0)):
            with self.subTest(recommended_gap_us=recommended_gap_us):
                summary = build_sim_settings_summary(
                    config,
                    runtime_timing={
                        "camera_config": self._runtime_camera_config(config.camera),
                        "timing_readout_time_s": 0.005,
                        "timing_cyclic_trigger_period_s": 0.074,
                        "timing_min_trigger_blanking_s": 0.001,
                        "theoretical_min_inter_frame_gap_us": recommended_gap_us - 500,
                        "inter_frame_gap_safety_margin_us": 500,
                        "recommended_inter_frame_gap_us": recommended_gap_us,
                        "timing_fallback_used": False,
                        "timing_fallback_reason": "",
                    },
                )

                self.assertIn(f"  inter_frame_gap_us: {recommended_gap_us}", summary)
                self.assertIn(
                    f"TIMING_EFFECTIVE_INTER_FRAME_GAP: {recommended_gap_us / 1000.0:.3f} ms",
                    summary,
                )
                self.assertIn(f"SIM9_ESTIMATED_TOTAL_TIME: {expected_total_ms:.3f} ms", summary)

    def test_build_sim_settings_summary_rejects_stale_preview_timing_signature(self):
        """preview 1 ms timing 不得被 10 ms 正式采集摘要复用。"""
        from sim_control.models import AppConfig, CameraConfig, TimingConfig
        from sim_control.summary import build_sim_settings_summary

        config = AppConfig(
            camera=CameraConfig(
                device_index=0,
                device_label="0: ORCA-Fusion BT [CAM-001]",
                roi_x=568,
                roi_y=764,
                roi_width=512,
                roi_height=512,
                exposure_us=10_000,
                bit_depth=16,
                trigger_mode="external_level",
            ),
            timing=TimingConfig(sample_rate_hz=1_000_000, slm_enable_guard_us=1000),
        )
        stale_camera_config = self._runtime_camera_config(config.camera)
        stale_camera_config["exposure_us"] = 1000

        summary = build_sim_settings_summary(
            config,
            runtime_timing={
                "camera_config": stale_camera_config,
                "timing_readout_time_s": 0.002534,
                "timing_cyclic_trigger_period_s": 0.0035,
                "timing_min_trigger_blanking_s": 0.002583,
                "theoretical_min_inter_frame_gap_us": 2583,
                "inter_frame_gap_safety_margin_us": 1000,
                "recommended_inter_frame_gap_us": 3583,
                "timing_fallback_used": False,
                "timing_fallback_reason": "",
            },
        )

        for label in (
            "TIMING_READOUTTIME",
            "TIMING_CYCLICTRIGGERPERIOD",
            "TIMING_MINTRIGGERBLANKING",
            "TIMING_THEORETICAL_MIN_INTER_FRAME_GAP",
            "TIMING_INTER_FRAME_GAP_SAFETY_MARGIN",
        ):
            self.assertIn(f"{label}: -", summary)
        self.assertIn("TIMING_EFFECTIVE_INTER_FRAME_GAP: 50.000 ms", summary)
        self.assertIn("TIMING_FALLBACK_USED: yes", summary)
        self.assertIn("TIMING_FALLBACK_REASON: camera_config_mismatch", summary)
        self.assertIn("SIM9_ESTIMATED_TOTAL_TIME: 552.000 ms", summary)
        self.assertIn("  inter_frame_gap_us: 50000", summary)

    def test_build_sim_settings_summary_requires_every_camera_signature_field_to_match(self):
        """9 个时序相关相机字段任一变化都必须拒绝 stale timing。"""
        from sim_control.models import AppConfig, CameraConfig
        from sim_control.summary import build_sim_settings_summary

        config = AppConfig(
            camera=CameraConfig(
                device_index=1,
                device_label="1: ORCA-Fusion BT [CAM-002]",
                roi_x=12,
                roi_y=24,
                roi_width=512,
                roi_height=256,
                exposure_us=3000,
                bit_depth=12,
                trigger_mode="external_level",
            )
        )
        replacements = {
            "device_index": 0,
            "device_label": "0: other",
            "roi_x": 16,
            "roi_y": 28,
            "roi_width": 1024,
            "roi_height": 512,
            "exposure_us": 5000,
            "bit_depth": 16,
            "trigger_mode": "internal",
        }

        for field_name, replacement in replacements.items():
            with self.subTest(field_name=field_name):
                stale_camera_config = self._runtime_camera_config(config.camera)
                stale_camera_config[field_name] = replacement
                summary = build_sim_settings_summary(
                    config,
                    runtime_timing={
                        "camera_config": stale_camera_config,
                        "recommended_inter_frame_gap_us": 4000,
                    },
                )

                self.assertIn("TIMING_READOUTTIME: -", summary)
                self.assertIn("TIMING_EFFECTIVE_INTER_FRAME_GAP: 50.000 ms", summary)
                self.assertIn("TIMING_FALLBACK_USED: yes", summary)
                self.assertIn("TIMING_FALLBACK_REASON: camera_config_mismatch", summary)

    def test_build_sim_settings_summary_shows_selected_running_order(self):
        """``selected_running_order`` 非空时摘要显示具体 RO 名而非"SLM 未连接"。"""
        from sim_control.models import AppConfig
        from sim_control.summary import build_sim_settings_summary

        config = AppConfig(selected_running_order="488_3.5_2d_1ms")

        summary = build_sim_settings_summary(config)

        self.assertIn("Pattern RO: 488_3.5_2d_1ms", summary)


    def test_build_sim_settings_summary_includes_enabled_z_scan_block(self):
        """启用 z-scan 时摘要只显示主 GUI 没有的派生信息。"""
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

        summary = build_sim_settings_summary(config, z_scan_timing_records=())

        self.assertIn("Z-Scan:", summary)
        self.assertNotIn("  enabled:", summary)
        self.assertIn("  start_um: 12.500", summary)
        self.assertNotIn("  direction:", summary)
        self.assertNotIn("  step_um:", summary)
        self.assertNotIn("  num_steps:", summary)
        self.assertNotIn("  scan_gap_nm:", summary)
        self.assertNotIn("  scan_moves:", summary)
        self.assertIn("  image_layers: 13", summary)
        self.assertIn("  total_distance_um: 4.800", summary)
        self.assertIn("  estimated_move_only_time_ms: 325.000", summary)
        # 默认 capture 模型每层波形含 2×1000 µs guard；guard 默认值改动会同步影响该值。
        self.assertIn("  estimated_move_capture_time_ms: 531.492", summary)
        self.assertNotIn("  estimated_scan_time_ms:", summary)
        self.assertNotIn("  exposure_preset_ms:", summary)
        self.assertIn("  actual_exposure_us: 13884", summary)

    def test_build_sim_settings_summary_uses_z_scan_history_records(self):
        """Z-scan summary ETA should use provided timing history when available."""
        from sim_control.models import AppConfig, ZScanConfig
        from sim_control.summary import build_sim_settings_summary
        from sim_control.z_scan_timing_history import (
            ZScanTimingRunRecord,
            estimate_z_scan_capture_test_total_time_ms,
            estimate_z_scan_move_only_total_time_ms,
        )

        config = AppConfig(
            z_scan=ZScanConfig(
                enabled=True,
                start_um=12.5,
                direction="positive_z",
                step_um=0.4,
                num_steps=2,
                exposure_preset_ms=14,
            )
        )
        records = [
            ZScanTimingRunRecord.for_test(
                mode="zscan_stage_only",
                scan_gap_nm=400.0,
                num_steps=2,
                exposure_preset_ms=14,
                total_duration_ms=200.0,
                initial_position_ms=1.0,
                scan_move_ms_sum=160.0,
                scan_move_ms_mean=80.0,
                scan_move_count=2,
                restore_ms=30.0,
                fixed_overhead_ms=9.0,
            ),
            ZScanTimingRunRecord.for_test(
                mode="zscan_stage_plus_capture",
                scan_gap_nm=400.0,
                num_steps=2,
                exposure_preset_ms=14,
                total_duration_ms=320.0,
                initial_position_ms=1.0,
                scan_move_ms_sum=160.0,
                scan_move_ms_mean=80.0,
                scan_move_count=2,
                capture_nonmove_ms_sum=90.0,
                capture_nonmove_ms_mean=30.0,
                best_focus_move_ms=5.0,
                tiff_write_ms=7.0,
                restore_ms=30.0,
                fixed_overhead_ms=27.0,
            ),
        ]

        summary = build_sim_settings_summary(config, z_scan_timing_records=records)
        expected_move_ms = estimate_z_scan_move_only_total_time_ms(
            config.z_scan,
            records=records,
        )
        expected_capture_ms = estimate_z_scan_capture_test_total_time_ms(
            config.z_scan,
            daq_config=config.daq,
            timing=config.timing,
            records=records,
        )

        self.assertIn(f"  estimated_move_only_time_ms: {expected_move_ms:.3f}", summary)
        self.assertIn(f"  estimated_move_capture_time_ms: {expected_capture_ms:.3f}", summary)


if __name__ == "__main__":
    unittest.main()

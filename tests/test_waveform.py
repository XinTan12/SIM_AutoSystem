"""NI USB-6423 SIM9 波形生成测试。

作用：
    覆盖 ``NIDaqWaveformBuilder.build`` 的边界与正确性：
        1. 短曝光 + 零帧间隔 → 报告告警（不致命，但提示用户检查）。
        2. SLM finish 脉冲超出帧间隔 → 报告告警。
        3. 标准 SIM9 配置（50 ms gap，500 ms 曝光）下，帧起止 sample index 差异
           严格等于 50 000 sample；SLM trigger / finish 边沿位置准确。
        4. ``include_role_matrix=False`` 生产路径与默认全矩阵路径产生**等价**的
           packed port 波形（用于性能优化路径校验）。

协作关系：
    上游：``unittest``、``numpy``。
    下游：``sim_control.models.DaqLineConfig`` / ``TimingConfig``、
          ``sim_control.waveform.NIDaqWaveformBuilder``。

维护要点：
    - 时序换算用 ``math.ceil``；测试中对单帧间隔 50_000 sample 的精确等价依赖这一点。
    - ``role_matrix`` 在 ``include_role_matrix=False`` 时为空 dict；如果改成 None 需同步测试。
"""

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class WaveformValidationTests(unittest.TestCase):
    """覆盖 SIM9 DAQ 波形生成、告警与 packed-only 输出一致性。"""

    def test_waveform_plan_exposes_warnings_for_short_exposure_and_gap(self):
        """1 µs 曝光 + 0 µs 帧间隔 应在 warnings 中提示。"""
        from sim_control.models import DaqLineConfig, TimingConfig
        from sim_control.waveform import NIDaqWaveformBuilder

        # 1) 故意构造一组"过小"的时序参数；波形仍能 build，但应有 2 条 warning。
        plan = NIDaqWaveformBuilder().build(
            daq_config=DaqLineConfig(),
            timing=TimingConfig(
                sample_rate_hz=1_000,
                edge_pulse_us=50,
                inter_frame_gap_us=0,
                slm_enable_guard_us=50,
            ),
            laser_wavelength_nm=488,
            exposure_us=1,
            frame_count=9,
        )

        # 2) 用单串多行字符串包住所有告警，便于子串匹配。
        joined = "\n".join(plan.warnings)
        self.assertIn("exposure", joined)
        self.assertIn("inter_frame_gap_us", joined)

    def test_waveform_warns_when_slm_finish_pulse_extends_beyond_frame_gap(self):
        """SLM finish 脉冲 > inter_frame_gap 应触发"finish 延伸超 gap"警告。"""
        from sim_control.models import DaqLineConfig, TimingConfig
        from sim_control.waveform import NIDaqWaveformBuilder

        # finish 脉冲 100 µs > gap 50 µs；按规则应有 warning（无 50 µs 帧间隔时的 ``slm_finish`` 越界）。
        plan = NIDaqWaveformBuilder().build(
            daq_config=DaqLineConfig(),
            timing=TimingConfig(
                sample_rate_hz=1_000_000,
                edge_pulse_us=100,
                inter_frame_gap_us=50,
                slm_enable_guard_us=50,
            ),
            laser_wavelength_nm=488,
            exposure_us=1_000,
            frame_count=9,
        )

        self.assertTrue(any("slm_finish" in warning for warning in plan.warnings))

    def test_standard_sim9_waveform_has_50ms_gap_from_finish_to_next_trigger(self):
        """标准 SIM9（50ms gap + 500ms 曝光）下，相邻帧 finish→next trigger 间距 = 50 000 sample。"""
        from sim_control.models import DaqLineConfig, TimingConfig
        from sim_control.waveform import NIDaqWaveformBuilder

        # 1) 1 MHz 采样率 → 1 sample = 1 µs；50 ms gap = 50 000 sample。
        plan = NIDaqWaveformBuilder().build(
            daq_config=DaqLineConfig(),
            timing=TimingConfig(
                sample_rate_hz=1_000_000,
                edge_pulse_us=50,
                inter_frame_gap_us=50_000,
                slm_enable_guard_us=50,
            ),
            laser_wavelength_nm=488,
            exposure_us=500_000,
            frame_count=9,
        )

        # 2) 元数据里的 frame_start / frame_end 必须相邻差 50_000 sample。
        frame_starts = plan.metadata["frame_start_samples"]
        frame_ends = plan.metadata["frame_end_samples"]
        for frame_index in range(8):
            self.assertEqual(frame_starts[frame_index + 1] - frame_ends[frame_index], 50_000)

        # 3) trigger 应在下一帧起点拉高、上一帧终点（即本帧 finish 位置）保持低；finish 反之。
        slm_trigger = plan.role_matrix["slm_trigger_line"]
        slm_finish = plan.role_matrix["slm_finish_line"]
        self.assertEqual(int(slm_trigger[frame_starts[1]]), 1)
        self.assertEqual(int(slm_finish[frame_ends[0]]), 1)
        self.assertEqual(int(slm_trigger[frame_ends[0]]), 0)
        self.assertEqual(int(slm_finish[frame_starts[1]]), 0)

    def test_packed_only_waveform_matches_full_matrix_plan_without_role_matrix(self):
        """``include_role_matrix=False`` 路径与默认完整路径产出相同 packed 波形。"""
        import numpy as np

        from sim_control.models import DaqLineConfig, TimingConfig
        from sim_control.waveform import NIDaqWaveformBuilder

        builder = NIDaqWaveformBuilder()
        daq_config = DaqLineConfig()
        timing = TimingConfig(
            sample_rate_hz=1_000_000,
            edge_pulse_us=50,
            inter_frame_gap_us=50_000,
            slm_enable_guard_us=50,
        )

        # 1) 两条路径分别构建；packed 波形应严格相等，metadata 和 warnings 一致。
        full_plan = builder.build(
            daq_config=daq_config,
            timing=timing,
            laser_wavelength_nm=488,
            exposure_us=500_000,
            frame_count=9,
        )
        packed_only_plan = builder.build(
            daq_config=daq_config,
            timing=timing,
            laser_wavelength_nm=488,
            exposure_us=500_000,
            frame_count=9,
            include_role_matrix=False,
        )

        # 2) packed 数组逐元素相等。
        np.testing.assert_array_equal(packed_only_plan.packed_port_values, full_plan.packed_port_values)
        # 3) packed-only 路径不应分配 role_matrix（生产路径省内存）。
        self.assertEqual(packed_only_plan.role_matrix, {})
        # 4) metadata 与 warnings 与默认路径相同，避免 GUI 显示差异。
        self.assertEqual(packed_only_plan.metadata, full_plan.metadata)
        self.assertEqual(packed_only_plan.warnings, full_plan.warnings)

    def test_z_scan_waveform_guards_slm_enable_and_omits_finish(self):
        """Z-scan 单帧波形应先拉高 enable，再同步 trigger/camera/488，且不使用 finish。"""
        from sim_control.models import DaqLineConfig, TimingConfig
        from sim_control.waveform import NIDaqWaveformBuilder

        daq_config = DaqLineConfig()
        timing = TimingConfig(
            sample_rate_hz=1_000_000,
            edge_pulse_us=50,
            inter_frame_gap_us=50_000,
            slm_enable_guard_us=50,
        )

        plan = NIDaqWaveformBuilder().build_z_scan(
            daq_config=daq_config,
            timing=timing,
            exposure_us=7_884,
            include_role_matrix=True,
        )

        frame_start = plan.metadata["frame_start_samples"][0]
        frame_end = plan.metadata["frame_end_samples"][0]
        self.assertEqual(frame_start, 50)
        self.assertEqual(frame_end - frame_start, 7_884)
        self.assertEqual(int(plan.role_matrix["slm_enable_line"][0]), 1)
        self.assertEqual(int(plan.role_matrix["slm_trigger_line"][0]), 0)
        self.assertEqual(int(plan.role_matrix["camera_trigger_line"][0]), 0)
        self.assertEqual(int(plan.role_matrix["laser_488_line"][0]), 0)
        self.assertEqual(int(plan.role_matrix["slm_trigger_line"][frame_start]), 1)
        self.assertEqual(int(plan.role_matrix["camera_trigger_line"][frame_start]), 1)
        self.assertEqual(int(plan.role_matrix["laser_488_line"][frame_start]), 1)
        self.assertEqual(int(plan.role_matrix["camera_trigger_line"][frame_end]), 0)
        self.assertEqual(int(plan.role_matrix["laser_488_line"][frame_end]), 0)
        self.assertEqual(int(plan.role_matrix["slm_enable_line"][frame_end]), 0)
        self.assertEqual(int(plan.role_matrix["slm_finish_line"].sum()), 0)
        self.assertEqual(int(sum(role[-1] for role in plan.role_matrix.values())), 0)

    def test_waveform_warns_when_guard_at_or_below_r11_activation_max(self):
        """guard <= 500 µs（R11 tHWAT 上限）时 build / build_z_scan 都应告警。"""
        from sim_control.models import DaqLineConfig, TimingConfig
        from sim_control.waveform import NIDaqWaveformBuilder

        builder = NIDaqWaveformBuilder()
        timing = TimingConfig(
            sample_rate_hz=1_000_000,
            edge_pulse_us=50,
            inter_frame_gap_us=50_000,
            slm_enable_guard_us=500,
        )

        plan = builder.build(
            daq_config=DaqLineConfig(),
            timing=timing,
            laser_wavelength_nm=488,
            exposure_us=10_000,
            frame_count=9,
        )
        z_plan = builder.build_z_scan(
            daq_config=DaqLineConfig(),
            timing=timing,
            exposure_us=7_884,
        )

        # 两条构建路径都必须出现同一措辞的 tHWAT 告警，提示首 trigger 可能丢失。
        for current_plan in (plan, z_plan):
            joined = "\n".join(current_plan.warnings)
            self.assertIn("slm_enable_guard_us=500", joined)
            self.assertIn("tHWAT", joined)
            self.assertIn("first SLM trigger may be lost", joined)

    def test_waveform_default_guard_emits_no_activation_warning(self):
        """默认 TimingConfig（guard=1000 µs）不应触发 tHWAT 告警。"""
        from sim_control.models import DaqLineConfig, TimingConfig
        from sim_control.waveform import NIDaqWaveformBuilder

        plan = NIDaqWaveformBuilder().build(
            daq_config=DaqLineConfig(),
            timing=TimingConfig(),
            laser_wavelength_nm=488,
            exposure_us=10_000,
            frame_count=9,
        )

        self.assertFalse(any("tHWAT" in warning for warning in plan.warnings))


if __name__ == "__main__":
    unittest.main()

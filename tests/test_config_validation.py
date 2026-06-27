"""配置校验与 controller 阻断行为测试。

作用：
    覆盖两条防御链路：
        1. ``validate_app_config`` 能列出多种非法字段的错误（曝光、ROI、采样率、
           边沿脉冲、激光波长）。
        2. ``SimAcquisitionController`` 在 ``start_single_acquisition`` 前必须先
           跑同一套校验，让 worker 永远拿不到非法 payload。
    同时验证：``selected_running_order`` 已选中时允许空 ``pattern_files``（RO 模式下
    pattern 列表是占位的）。

协作关系：
    上游：``unittest``。
    下游：``sim_control.config_store.validate_app_config``、
          ``sim_control.controller.SimAcquisitionController``、``sim_control.models``。

维护要点：
    - 新增 AppConfig 字段时，应同步在 ``validate_app_config`` 添加校验并在此处加用例。
    - 用例必须验证"controller 拒绝时不发 worker 信号"，否则后台 worker 可能跑非法配置。
"""

import sys
import unittest
from pathlib import Path


# 把项目根加入 sys.path，避免本测试在不同工作目录下 import 失败。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class ConfigValidationTests(unittest.TestCase):
    """覆盖 ``validate_app_config`` 与 controller 启动前阻断行为。"""

    def test_validate_app_config_reports_invalid_camera_and_timing_values(self):
        """5 个非法字段（曝光 0、ROI 负、采样率过低、脉冲 0、波长非法）都应报错。"""
        from sim_control.config_store import validate_app_config
        from sim_control.models import AppConfig

        # 1) 构造一个包含 5 类问题的极端配置：每条都应出现在错误列表里。
        config = AppConfig()
        config.camera.exposure_us = 0
        config.camera.roi_width = -1
        config.timing.sample_rate_hz = 999
        config.timing.edge_pulse_us = 0
        config.selected_laser_nm = 640

        errors = validate_app_config(config)

        # 2) 用 ``any(...)`` 而非完全相等的字符串匹配，让校验文案小幅调整不破坏测试。
        self.assertTrue(any("exposure_us" in error for error in errors))
        self.assertTrue(any("roi_width" in error for error in errors))
        self.assertTrue(any("sample_rate_hz" in error for error in errors))
        self.assertTrue(any("edge_pulse_us" in error for error in errors))
        self.assertTrue(any("selected_laser_nm" in error for error in errors))

    def test_validate_app_config_allows_empty_legacy_pattern_files_when_running_order_is_selected(self):
        """RO 模式下 ``pattern_files=[]`` 合法（pattern 列表只是占位）。"""
        from sim_control.config_store import validate_app_config
        from sim_control.models import AppConfig

        # 显式 ``selected_running_order`` 非空，校验应跳过 pattern_files 长度检查。
        config = AppConfig(pattern_files=[], selected_running_order="488_3.5_2d_1ms")

        errors = validate_app_config(config)

        self.assertFalse(any("pattern_files" in error for error in errors))

    def test_validate_app_config_rejects_legacy_647_laser_selection(self):
        """647 nm 已迁移为旧值；当前配置只能直接选择 638 nm。"""
        from sim_control.config_store import validate_app_config
        from sim_control.models import AppConfig, ReconstructionConfig

        config = AppConfig(
            selected_laser_nm=647,
            reconstruction=ReconstructionConfig(enabled=False),
        )

        errors = validate_app_config(config)

        self.assertTrue(any("selected_laser_nm" in error for error in errors))

    def test_validate_app_config_requires_current_laser_otf_when_reconstruction_enabled(self):
        """启用真实重建时，当前波长必须配置对应 OTF 路径。"""
        from sim_control.config_store import validate_app_config
        from sim_control.models import AppConfig, ReconstructionConfig

        config = AppConfig(
            selected_laser_nm=488,
            reconstruction=ReconstructionConfig(enabled=True),
        )

        errors = validate_app_config(config)

        self.assertTrue(any("reconstruction.otf_488_path" in error for error in errors))

    def test_validate_app_config_rejects_reconstruction_output_tiff_file_path(self):
        from sim_control.config_store import validate_app_config
        from sim_control.models import AppConfig, ReconstructionConfig

        config = AppConfig(
            selected_laser_nm=488,
            reconstruction=ReconstructionConfig(
                enabled=True,
                otf_488_path="E:/calibration/488_otf.tif",
                output_path="data/reconstruction/sim_reconstruction.tif",
            ),
        )

        errors = validate_app_config(config)

        self.assertTrue(any("reconstruction.output_path must be an output directory" in error for error in errors))

    def test_controller_blocks_invalid_config_before_emitting_worker_start(self):
        """controller 应在 ``start_single_acquisition`` 前抛 ValueError，且不发 worker 信号。"""
        from sim_control.controller import SimAcquisitionController
        from sim_control.models import BackendConfig, SimTaskConfig

        # 1) 用仿真 backend 构造 controller，避免触发真实硬件初始化。
        controller = SimAcquisitionController(BackendConfig(simulation_mode=True))
        emitted_payloads = []
        # 2) 监听 ``signal_start_worker``；预期它在本用例中**不**被触发。
        controller.signal_start_worker.connect(lambda payload: emitted_payloads.append(payload))
        # 3) 模拟"已 prepare patterns"，让 controller 跳过 handles 检查直奔 validate。
        controller.pattern_result.handles = list(range(9))

        try:
            # 4) 故意把 exposure 设为 0，触发 ``validate_app_config`` 抛错。
            task = SimTaskConfig()
            task.camera.exposure_us = 0
            with self.assertRaisesRegex(ValueError, "exposure_us"):
                controller.start_single_acquisition(task)
        finally:
            # 5) 不论成功失败都 shutdown，避免后续测试受 worker 线程影响。
            controller.shutdown()

        # 6) ``signal_start_worker`` 永远不应被触发：controller 必须在 validate 失败时立刻 raise。
        self.assertEqual(emitted_payloads, [])

    def test_controller_blocks_enabled_reconstruction_without_current_laser_otf(self):
        """启用重建但缺当前波长 OTF 时，controller 应在采集前阻断。"""
        from sim_control.controller import SimAcquisitionController
        from sim_control.models import BackendConfig, ReconstructionConfig, SimTaskConfig

        controller = SimAcquisitionController(BackendConfig(simulation_mode=True))
        emitted_payloads = []
        controller.signal_start_worker.connect(lambda payload: emitted_payloads.append(payload))
        controller.pattern_result.handles = list(range(9))
        controller.reconstruction_config = ReconstructionConfig(enabled=True)

        try:
            task = SimTaskConfig(laser_wavelength_nm=488)
            with self.assertRaisesRegex(ValueError, "reconstruction.otf_488_path"):
                controller.start_single_acquisition(task)
        finally:
            controller.shutdown()

        self.assertEqual(emitted_payloads, [])

    def test_controller_allows_task_level_reconstruction_disabled_override(self):
        """raw-only SIM9 可用任务级配置关闭重建校验，不修改 controller 全局 fail-closed 行为。"""
        from sim_control.controller import SimAcquisitionController
        from sim_control.models import BackendConfig, ReconstructionConfig, SimTaskConfig

        controller = SimAcquisitionController(BackendConfig(simulation_mode=True))
        emitted_payloads = []
        try:
            try:
                controller.signal_start_worker.disconnect()
            except TypeError:
                pass
            controller.signal_start_worker.connect(lambda payload: emitted_payloads.append(payload))
            controller.pattern_result.handles = list(range(9))
            controller.reconstruction_config = ReconstructionConfig(enabled=True)

            task = SimTaskConfig(laser_wavelength_nm=488)
            task_id = controller.start_single_acquisition(
                task,
                reconstruction_config=ReconstructionConfig(enabled=False),
            )
        finally:
            controller.shutdown()

        self.assertEqual(len(emitted_payloads), 1)
        self.assertEqual(emitted_payloads[0]["task_id"], task_id)
        self.assertEqual(emitted_payloads[0]["task"].laser_wavelength_nm, 488)


if __name__ == "__main__":
    unittest.main()

"""仿真模式 adapter 行为测试。

作用：
    在无硬件环境下验证仿真路径仍能跑通端到端 9 帧采集：
        1. ``SimAcquisitionController`` 在 ``simulation_mode=True`` 下使用 ``Simulated*`` 三件套。
        2. ``run_single_acquisition`` 配合仿真 adapter 能产出 ``(9, H, W)`` ``uint16`` stack
           并广播 9 次 ``frame_captured`` 与一次 ``acquisition_complete``。
        3. ``SimulatedCameraAdapter`` 暴露 8/12/16 位深，并把非法位深回落到 16。
        4. ``SimulatedSlmAdapter`` 暴露 24 个 RO（与命名规则一致），且 ``select_running_order``
           能返回 ``handles=[-1]`` 的 PatternPreparationResult。
        5. ``select_running_order_for_task`` 在 controller 路径上也能为 488nm/11ms 选出
           ``"488_3.5_2d_10ms"`` 并广播 ``running_order_selected``。

协作关系：
    上游：``unittest``、``numpy``。
    下游：``sim_control.controller``、``sim_control.acquisition_core``、
          ``sim_control.sim_adapters``、``sim_control.models``。

维护要点：
    - 仿真 RO 列表长度（24）与命名格式由 ``SimulatedSlmAdapter.SIMULATED_RUNNING_ORDERS``
      决定；改动需同步本测试。
    - ``simulation_mode=True`` 是测试夹具默认；改用真实 adapter 时本测试不应被执行。
"""

import sys
import unittest
from pathlib import Path

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class SimulationModeTests(unittest.TestCase):
    """覆盖仿真 adapter 支撑无硬件运行与测试的能力。"""

    def test_controller_uses_simulated_adapters_when_backend_requests_simulation(self):
        """``backend.simulation_mode=True`` 必须导致 controller 选择 Simulated* 三件套。"""
        from sim_control.controller import SimAcquisitionController
        from sim_control.models import BackendConfig
        from sim_control.sim_adapters import SimulatedCameraAdapter, SimulatedDaqAdapter, SimulatedSlmAdapter

        controller = SimAcquisitionController(BackendConfig(simulation_mode=True))
        try:
            self.assertIsInstance(controller.camera_adapter, SimulatedCameraAdapter)
            self.assertIsInstance(controller.slm_adapter, SimulatedSlmAdapter)
            self.assertIsInstance(controller.daq_adapter, SimulatedDaqAdapter)
        finally:
            # 即便断言失败也要 shutdown，避免遗留 worker 线程影响后续用例。
            controller.shutdown()

    def test_simulated_adapters_run_complete_nine_frame_sequence_without_sdk(self):
        """端到端 9 帧 SIM 采集在无 SDK 时也能跑通并产生 ``(9, H, W)`` uint16 stack。"""
        from sim_control.acquisition_core import run_single_acquisition
        from sim_control.models import DaqLineConfig, PatternPreparationResult, SimTaskConfig
        from sim_control.sim_adapters import SimulatedCameraAdapter, SimulatedDaqAdapter, SimulatedSlmAdapter

        # 1) 准备 task + 三个仿真 adapter + 一组 pattern 准备结果。
        task = SimTaskConfig()
        camera = SimulatedCameraAdapter()
        slm = SimulatedSlmAdapter()
        daq = SimulatedDaqAdapter()
        pattern_result = slm.program_patterns([""] * 9)
        statuses = []

        # 2) 跑核心采集流程；on_status 把所有事件收集起来便于断言。
        batch = run_single_acquisition(
            task=task,
            daq_config=DaqLineConfig(),
            pattern_result=pattern_result,
            camera=camera,
            slm=slm,
            daq=daq,
            task_id="sim-test",
            on_status=lambda state, payload: statuses.append((state, payload)),
        )

        # 3) batch 形状 / dtype / task_id 必须符合协议。
        self.assertEqual(batch.task_id, "sim-test")
        self.assertEqual(batch.stack.shape, (9, task.camera.roi_height, task.camera.roi_width))
        self.assertEqual(batch.stack.dtype, np.uint16)
        # 4) frame_captured 事件应该 1..9 全发；最后一条状态是 acquisition_complete。
        self.assertEqual([payload["frame_index"] for state, payload in statuses if state == "frame_captured"], list(range(1, 10)))
        self.assertIn(("acquisition_complete", {"task_id": "sim-test", "stack_shape": list(batch.stack.shape)}), statuses)

    def test_simulated_camera_supports_user_facing_bit_depths_and_limits_8_bit_frames(self):
        """仿真相机应宣告 8/12/16 位深支持，并在 8-bit 模式下生成 max ≤255 的帧。"""
        from sim_control.models import CameraConfig
        from sim_control.sim_adapters import SimulatedCameraAdapter

        camera = SimulatedCameraAdapter()
        connection_info = camera.connect()
        # 1) 8 位深 + 小 ROI → 仿真帧的灰度极值必须 ≤255（即"被位深限制"）。
        config = CameraConfig(roi_width=16, roi_height=8, bit_depth=8)

        result = camera.apply_config(config)
        camera.start_preview(config)
        frame = camera.read_preview_frame()

        # 2) 连接/配置/get 三处都应一致暴露 8/12/16 支持档。
        self.assertEqual(connection_info["supported_bit_depths"], [8, 12, 16])
        self.assertEqual(camera.get_supported_bit_depths(), [8, 12, 16])
        self.assertEqual(result["supported_bit_depths"], [8, 12, 16])
        # 3) ``applied_bit_depth`` 与 config.bit_depth 都应保留请求值。
        self.assertEqual(result["applied_bit_depth"], 8)
        self.assertEqual(config.bit_depth, 8)
        # 4) 帧 dtype 仍为 uint16（仿真容器一致），但取值受 ``2^8-1=255`` 限制。
        self.assertEqual(frame.dtype, np.uint16)
        self.assertLessEqual(int(frame.max()), 255)

    def test_simulated_camera_falls_back_to_16_bit_for_invalid_bit_depth(self):
        """非法 bit_depth=10 应被回落到 16，并把 config 字段也改写。"""
        from sim_control.models import CameraConfig
        from sim_control.sim_adapters import SimulatedCameraAdapter

        camera = SimulatedCameraAdapter()
        config = CameraConfig(bit_depth=10)

        result = camera.apply_config(config)

        self.assertEqual(result["applied_bit_depth"], 16)
        self.assertEqual(config.bit_depth, 16)

    def test_simulated_slm_lists_and_selects_running_orders(self):
        """仿真 SLM 必须暴露正式 SIM 与 z-scan RO，且 ``select_running_order(7)`` 命中预期名字。"""
        from sim_control.sim_adapters import SimulatedSlmAdapter

        slm = SimulatedSlmAdapter()

        # 1) 列表长度由 ``SIMULATED_RUNNING_ORDERS`` 决定；24 个正式 SIM RO + 4 个 z-scan RO。
        running_orders = slm.list_running_orders()
        result = slm.select_running_order(7)

        self.assertEqual(len(running_orders), 28)
        self.assertEqual(running_orders[7][1], "488_3.5_2d_10ms_ang0")
        self.assertIn((25, "488_3.5_2d_zscan3p_8ms"), running_orders)
        self.assertEqual(result["running_order_name"], "488_3.5_2d_10ms_ang0")
        # 2) RO 模式下 handles=[-1] 是协议；下游 acquisition_core 据此识别。
        self.assertEqual(result["pattern_result"].handles, [-1])

    def test_controller_selects_running_order_for_task_in_simulation_mode(self):
        """``select_running_order_for_task(488, 11_000us)`` 应选 ``488_3.5_2d_10ms``。"""
        from sim_control.controller import SimAcquisitionController
        from sim_control.models import BackendConfig

        # 1) 仿真模式 controller + 状态收集器。
        controller = SimAcquisitionController(BackendConfig(simulation_mode=True))
        statuses = []
        controller.signal_status_changed.connect(lambda status, payload: statuses.append((status, payload)))
        try:
            # 2) 11 ms 曝光 → 落到 10 ms 桶，选 ``488_3.5_2d_10ms`` 而非 _ang0 变体。
            result = controller.select_running_order_for_task(488, 11_000)
        finally:
            controller.shutdown()

        # 3) RO 名 / handles / pattern_files 全部由 select 流程产生，状态信号也应抛出。
        self.assertEqual(result["running_order_name"], "488_3.5_2d_10ms")
        self.assertEqual(controller.pattern_result.handles, [-1])
        self.assertEqual(controller.pattern_result.pattern_files, ["488_3.5_2d_10ms"] * 9)
        self.assertTrue(any(status == "running_order_selected" for status, _payload in statuses))


if __name__ == "__main__":
    unittest.main()

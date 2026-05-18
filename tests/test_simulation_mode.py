"""仿真模式 adapter 行为测试。

这些用例确认无硬件时相机、SLM 和 DAQ 仿真对象仍能连接、生成帧、准备图案和记录波形，支持离线 GUI 与流程验证。
"""

import sys
import unittest
from pathlib import Path

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class SimulationModeTests(unittest.TestCase):
    """验证仿真 adapter 能支撑无硬件运行和测试。"""
    def test_controller_uses_simulated_adapters_when_backend_requests_simulation(self):
        from sim_control.controller import SimAcquisitionController
        from sim_control.models import BackendConfig
        from sim_control.sim_adapters import SimulatedCameraAdapter, SimulatedDaqAdapter, SimulatedSlmAdapter

        controller = SimAcquisitionController(BackendConfig(simulation_mode=True))
        try:
            self.assertIsInstance(controller.camera_adapter, SimulatedCameraAdapter)
            self.assertIsInstance(controller.slm_adapter, SimulatedSlmAdapter)
            self.assertIsInstance(controller.daq_adapter, SimulatedDaqAdapter)
        finally:
            controller.shutdown()

    def test_simulated_adapters_run_complete_nine_frame_sequence_without_sdk(self):
        from sim_control.acquisition_core import run_single_acquisition
        from sim_control.models import DaqLineConfig, PatternPreparationResult, SimTaskConfig
        from sim_control.sim_adapters import SimulatedCameraAdapter, SimulatedDaqAdapter, SimulatedSlmAdapter

        task = SimTaskConfig()
        camera = SimulatedCameraAdapter()
        slm = SimulatedSlmAdapter()
        daq = SimulatedDaqAdapter()
        pattern_result = slm.program_patterns([""] * 9)
        statuses = []

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

        self.assertEqual(batch.task_id, "sim-test")
        self.assertEqual(batch.stack.shape, (9, task.camera.roi_height, task.camera.roi_width))
        self.assertEqual(batch.stack.dtype, np.uint16)
        self.assertEqual([payload["frame_index"] for state, payload in statuses if state == "frame_captured"], list(range(1, 10)))
        self.assertIn(("acquisition_complete", {"task_id": "sim-test", "stack_shape": list(batch.stack.shape)}), statuses)

    def test_simulated_camera_supports_user_facing_bit_depths_and_limits_8_bit_frames(self):
        from sim_control.models import CameraConfig
        from sim_control.sim_adapters import SimulatedCameraAdapter

        camera = SimulatedCameraAdapter()
        connection_info = camera.connect()
        config = CameraConfig(roi_width=16, roi_height=8, bit_depth=8)

        result = camera.apply_config(config)
        camera.start_preview(config)
        frame = camera.read_preview_frame()

        self.assertEqual(connection_info["supported_bit_depths"], [8, 12, 16])
        self.assertEqual(camera.get_supported_bit_depths(), [8, 12, 16])
        self.assertEqual(result["supported_bit_depths"], [8, 12, 16])
        self.assertEqual(result["applied_bit_depth"], 8)
        self.assertEqual(config.bit_depth, 8)
        self.assertEqual(frame.dtype, np.uint16)
        self.assertLessEqual(int(frame.max()), 255)

    def test_simulated_camera_falls_back_to_16_bit_for_invalid_bit_depth(self):
        from sim_control.models import CameraConfig
        from sim_control.sim_adapters import SimulatedCameraAdapter

        camera = SimulatedCameraAdapter()
        config = CameraConfig(bit_depth=10)

        result = camera.apply_config(config)

        self.assertEqual(result["applied_bit_depth"], 16)
        self.assertEqual(config.bit_depth, 16)

    def test_simulated_slm_lists_and_selects_running_orders(self):
        from sim_control.sim_adapters import SimulatedSlmAdapter

        slm = SimulatedSlmAdapter()

        running_orders = slm.list_running_orders()
        result = slm.select_running_order(7)

        self.assertEqual(len(running_orders), 24)
        self.assertEqual(running_orders[7][1], "488_3.5_2d_10ms_ang0")
        self.assertEqual(result["running_order_name"], "488_3.5_2d_10ms_ang0")
        self.assertEqual(result["pattern_result"].handles, [-1])

    def test_controller_selects_running_order_for_task_in_simulation_mode(self):
        from sim_control.controller import SimAcquisitionController
        from sim_control.models import BackendConfig

        controller = SimAcquisitionController(BackendConfig(simulation_mode=True))
        statuses = []
        controller.signal_status_changed.connect(lambda status, payload: statuses.append((status, payload)))
        try:
            result = controller.select_running_order_for_task(488, 11_000)
        finally:
            controller.shutdown()

        self.assertEqual(result["running_order_name"], "488_3.5_2d_10ms")
        self.assertEqual(controller.pattern_result.handles, [-1])
        self.assertEqual(controller.pattern_result.pattern_files, ["488_3.5_2d_10ms"] * 9)
        self.assertTrue(any(status == "running_order_selected" for status, _payload in statuses))


if __name__ == "__main__":
    unittest.main()

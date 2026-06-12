"""代码审查报告（2026-06-11）P2/P3 跟进项的纯软件回归测试。

覆盖本轮按审查报告实施的新功能与合同：
    - #5  ``HardwareError`` 抽到 ``errors.py`` 并由 ``adapters`` re-export；仿真
          适配器统一抛 ``HardwareError``；``strict_connection`` 开关的"未连接被拒"语义。
    - #16 ``SimulatedDaqAdapter(realtime=...)``：默认截断 100 ms、realtime 全时长。
    - #11 ``BackendConfig`` 的 Ti2 路径透传到 ``Ti2ZStageAdapter``，空串回落默认。
    - #10 ``(9, H, W)`` ``uint16`` stack 合同集中断言；raw stack 不进 GUI summary
          payload 的负面断言；preview ``latest-frame-wins`` 负载下丢帧。

这些用例不依赖真实硬件，可在仿真路径下稳定运行。
"""

import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class HardwareErrorContractTests(unittest.TestCase):
    """#5：HardwareError 的定义位置、re-export 与继承关系。"""

    def test_hardware_error_is_single_type_across_modules(self):
        from sim_control.adapters import HardwareError as FromAdapters
        from sim_control.errors import HardwareError as FromErrors

        # re-export 必须是同一个类对象，既有 ``from .adapters import HardwareError`` 不破。
        self.assertIs(FromAdapters, FromErrors)

    def test_hardware_error_subclasses_runtime_error(self):
        from sim_control.errors import HardwareError

        # 继承 RuntimeError：既有 ``except RuntimeError`` / ``assertRaises(RuntimeError)`` 仍命中。
        self.assertTrue(issubclass(HardwareError, RuntimeError))

    def test_errors_module_does_not_import_real_adapters(self):
        import ast

        import sim_control.errors as errors_module

        # 仿真层引用 errors 时不应被动拉起 1700+ 行真实适配器模块。
        # 用 AST 只看 import 语句，避免 docstring 里的示例文字被误判。
        tree = ast.parse(Path(errors_module.__file__).read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
            elif isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
        self.assertFalse(any("adapters" in module for module in imported))


class SimulatedStrictConnectionTests(unittest.TestCase):
    """#5：strict_connection 下仿真适配器对"未连接"抛 HardwareError。"""

    def test_strict_slm_rejects_select_running_order_when_disconnected(self):
        from sim_control.errors import HardwareError
        from sim_control.sim_adapters import SimulatedSlmAdapter

        slm = SimulatedSlmAdapter(strict_connection=True)
        with self.assertRaises(HardwareError):
            slm.select_running_order(0)

    def test_strict_slm_rejects_program_patterns_when_disconnected(self):
        from sim_control.errors import HardwareError
        from sim_control.sim_adapters import SimulatedSlmAdapter

        slm = SimulatedSlmAdapter(strict_connection=True)
        with self.assertRaises(HardwareError):
            slm.program_patterns([""] * 9)

    def test_strict_slm_rejects_activate_after_disconnect(self):
        from sim_control.errors import HardwareError
        from sim_control.sim_adapters import SimulatedSlmAdapter

        slm = SimulatedSlmAdapter(strict_connection=True)
        slm.connect()
        slm.program_patterns([""] * 9)  # 已连接，准备成功
        slm.disconnect()
        with self.assertRaises(HardwareError):
            slm.activate_prepared_patterns()

    def test_strict_camera_rejects_preview_read_when_disconnected(self):
        from sim_control.errors import HardwareError
        from sim_control.sim_adapters import SimulatedCameraAdapter

        # 直接读预览帧（未 connect、未 start_preview）：strict 应先因未连接拒绝。
        camera = SimulatedCameraAdapter(strict_connection=True)
        with self.assertRaises(HardwareError):
            camera.read_preview_frame()

    def test_strict_camera_rejects_sequence_read_when_disconnected(self):
        from sim_control.errors import HardwareError
        from sim_control.sim_adapters import SimulatedCameraAdapter

        camera = SimulatedCameraAdapter(strict_connection=True)
        camera.arm(9)  # 注意：未 connect
        with self.assertRaises(HardwareError):
            camera.read_frame_sequence(9, [""] * 9, 488)

    def test_default_slm_still_auto_connects_on_select(self):
        """非 strict（默认）必须保留自动连接，以维持现有仿真测试便利。"""
        from sim_control.sim_adapters import SimulatedSlmAdapter

        slm = SimulatedSlmAdapter()
        result = slm.select_running_order(0)
        self.assertTrue(slm.is_connected())
        self.assertIn("running_order_name", result)


class SimulatedExceptionTypeTests(unittest.TestCase):
    """#5：仿真适配器的硬件状态错误统一为 HardwareError（不再裸 RuntimeError）。"""

    def test_camera_read_without_arm_raises_hardware_error(self):
        from sim_control.errors import HardwareError
        from sim_control.sim_adapters import SimulatedCameraAdapter

        camera = SimulatedCameraAdapter()
        camera.connect()
        with self.assertRaises(HardwareError):
            camera.read_frame_sequence(9, [""] * 9, 488)  # 未 arm

    def test_preview_read_without_start_raises_hardware_error(self):
        from sim_control.errors import HardwareError
        from sim_control.sim_adapters import SimulatedCameraAdapter

        camera = SimulatedCameraAdapter()
        camera.connect()
        with self.assertRaises(HardwareError):
            camera.read_preview_frame()  # 未 start_preview

    def test_slm_running_order_out_of_range_raises_hardware_error(self):
        from sim_control.errors import HardwareError
        from sim_control.sim_adapters import SimulatedSlmAdapter

        slm = SimulatedSlmAdapter()
        slm.connect()
        with self.assertRaises(HardwareError):
            slm.select_running_order(9999)


class SimulatedDaqRealtimeTests(unittest.TestCase):
    """#16：仿真 DAQ 播放时长——默认截断 100 ms，realtime opt-in 走全时长。"""

    def test_default_play_waveform_truncates_long_duration(self):
        from sim_control.sim_adapters import SimulatedDaqAdapter

        daq = SimulatedDaqAdapter()  # realtime=False（默认）
        plan = SimpleNamespace(duration_s=5.0)
        started = time.perf_counter()
        daq.play_waveform("Dev1", plan)
        elapsed = time.perf_counter() - started
        # 5s 波形在默认仿真下被截断到 ~0.1s；给宽松上界避免 CI 抖动误报。
        self.assertLess(elapsed, 0.5)

    def test_realtime_play_waveform_honours_full_duration(self):
        from sim_control.sim_adapters import SimulatedDaqAdapter

        daq = SimulatedDaqAdapter(realtime=True)
        plan = SimpleNamespace(duration_s=0.3)
        started = time.perf_counter()
        daq.play_waveform("Dev1", plan)
        elapsed = time.perf_counter() - started
        # realtime 模式按真实 0.3s 播放，必须明显超过默认 0.1s 截断。
        self.assertGreaterEqual(elapsed, 0.25)

    def test_realtime_play_waveform_still_honours_stop_event(self):
        from sim_control.sim_adapters import SimulatedDaqAdapter

        class _Flag:
            def __init__(self) -> None:
                self._set = True

            def is_set(self) -> bool:
                return self._set

        daq = SimulatedDaqAdapter(realtime=True)
        plan = SimpleNamespace(duration_s=10.0)
        started = time.perf_counter()
        daq.play_waveform("Dev1", plan, stop_event=_Flag())
        elapsed = time.perf_counter() - started
        # 即使 realtime + 10s 波形，已置位的 stop_event 必须让播放立即返回。
        self.assertLess(elapsed, 0.2)


class Ti2PathConfigTests(unittest.TestCase):
    """#11：BackendConfig 的 Ti2 路径透传到 adapter，空串回落内部默认。"""

    def test_factory_passes_configured_ti2_paths_to_adapter(self):
        from sim_control.adapter_factory import create_stage_adapter_for_backend
        from sim_control.models import BackendConfig
        from sim_control.stage_adapter import Ti2ZStageAdapter

        backend = BackendConfig(
            simulation_mode=False,
            ti2_dll_path="C:/custom/Ti2_Mic_Driver.dll",
            ti2_sdk_module_path="E:/proj/z-scan/ti2_sdk.py",
        )
        stage = create_stage_adapter_for_backend(backend)
        self.assertIsInstance(stage, Ti2ZStageAdapter)
        self.assertEqual(stage.dll_path, Path("C:/custom/Ti2_Mic_Driver.dll"))
        self.assertEqual(stage.sdk_module_path, Path("E:/proj/z-scan/ti2_sdk.py"))

    def test_factory_empty_ti2_paths_fall_back_to_adapter_defaults(self):
        from sim_control.adapter_factory import create_stage_adapter_for_backend
        from sim_control.models import BackendConfig

        backend = BackendConfig(simulation_mode=False)  # ti2 路径默认空串
        stage = create_stage_adapter_for_backend(backend)
        # 空串 → None → adapter 内部默认推导：dll 留 None，sdk 推导到 z-scan/ti2_sdk.py。
        self.assertIsNone(stage.dll_path)
        self.assertEqual(stage.sdk_module_path.name, "ti2_sdk.py")


class NineFrameStackContractTests(unittest.TestCase):
    """#10：(9, H, W) uint16 stack 合同的集中断言。"""

    def _run_sim_acquisition(self):
        from sim_control.acquisition_core import run_single_acquisition
        from sim_control.models import DaqLineConfig, SimTaskConfig
        from sim_control.sim_adapters import (
            SimulatedCameraAdapter,
            SimulatedDaqAdapter,
            SimulatedSlmAdapter,
        )

        task = SimTaskConfig()
        slm = SimulatedSlmAdapter()
        pattern_result = slm.program_patterns([""] * 9)
        return run_single_acquisition(
            task=task,
            daq_config=DaqLineConfig(),
            pattern_result=pattern_result,
            camera=SimulatedCameraAdapter(),
            slm=slm,
            daq=SimulatedDaqAdapter(),
            task_id="contract",
            on_status=lambda state, payload: None,
        ), task

    def test_stack_shape_dtype_and_per_frame_contract(self):
        batch, task = self._run_sim_acquisition()
        stack = batch.stack

        # 1) 维度：恰好 9 帧，H/W 与 task 相机 ROI 一致。
        self.assertEqual(stack.ndim, 3)
        self.assertEqual(stack.shape[0], 9)
        self.assertEqual(stack.shape, (9, task.camera.roi_height, task.camera.roi_width))
        # 2) dtype：整栈 uint16。
        self.assertEqual(stack.dtype, np.uint16)
        # 3) 逐帧 dtype 一致，且每帧是二维。
        for frame in stack:
            self.assertEqual(frame.dtype, np.uint16)
            self.assertEqual(frame.ndim, 2)


class RawStackNotInGuiPayloadTests(unittest.TestCase):
    """#10：GUI summary payload 只含轻量字段，不得携带 raw ndarray。"""

    def test_summary_payload_contains_no_ndarray(self):
        from sim_control.controller import _acquisition_summary_payload

        stack = np.zeros((9, 4, 5), dtype=np.uint16)
        batch = SimpleNamespace(
            task_id="t1",
            stack=stack,
            metadata={"running_order_name": "488_3.5_2d_10ms"},
        )

        payload = _acquisition_summary_payload(batch)

        # 合同：payload 任意字段都不是 ndarray（含嵌套 metadata 值）。
        for value in payload.values():
            self.assertNotIsInstance(value, np.ndarray)
        for value in dict(payload.get("metadata", {})).values():
            self.assertNotIsInstance(value, np.ndarray)
        # 形状/类型以轻量描述传递，而非数组本身。
        self.assertEqual(tuple(payload["stack_shape"]), (9, 4, 5))
        self.assertEqual(payload["stack_dtype"], "uint16")


class PreviewLatestFrameWinsTests(unittest.TestCase):
    """#10：preview latest-frame-wins 在"生产快于消费"负载下丢弃中间帧。"""

    def test_take_latest_drops_intermediate_frames_under_load(self):
        from sim_control.preview import SimPreviewWorker

        worker = SimPreviewWorker()
        # 连续发布 100 帧而不消费：单槽位应只保留最新一帧。
        for i in range(1, 101):
            worker.publish_preview_frame(np.full((4, 4), i, dtype=np.uint16), fps=i)

        snapshot = worker.take_latest_frame()
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.sequence, 100)
        self.assertEqual(snapshot.fps, 100)
        self.assertEqual(int(snapshot.frame[0, 0]), 100)
        # 取走即清空：再次 take 没有新帧。
        self.assertIsNone(worker.take_latest_frame())


if __name__ == "__main__":
    unittest.main()

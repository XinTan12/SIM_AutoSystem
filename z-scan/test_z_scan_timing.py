"""``z_scan_timing.py`` 的单元测试（独立工具，与 SIM 主流程解耦）。

作用：
    覆盖 ``build_targets`` / ``parse_args`` / ``summarize_records`` /
    ``validate_targets`` / ``resolve_start_um`` 等纯函数的边界与正常路径。
    所有用例通过 ``importlib.util`` 动态加载 ``z_scan_timing.py`` 与 ``ti2_sdk.py``，
    避免与项目其它模块产生 import 副作用。

协作关系：
    上游：``unittest``、``importlib.util``。
    下游：``z-scan/z_scan_timing.py``、``z-scan/ti2_sdk.py``。

维护要点：
    - 本测试不连接真实 SDK；所有 SDK 相关测试都在 ``z_scan_timing.py`` 纯函数层。
    - ``make_args`` 用 ``SimpleNamespace`` 伪造 ``argparse.Namespace``，便于
      ``run_scan`` 等函数读取字段。
"""

from __future__ import annotations

import importlib.util
import math
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


def load_timing_module():
    """通过 importlib 直接加载 ``z_scan_timing.py``，避免 PYTHONPATH 干扰。"""
    module_path = Path(__file__).with_name("z_scan_timing.py")
    spec = importlib.util.spec_from_file_location("z_scan_timing", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # 把模块注册到 sys.modules，让模块内部的 ``import ti2_sdk`` 等顶层 import 正常生效。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_ti2_module():
    """加载 ``ti2_sdk.py`` 进 sys.modules；用一个唯一别名避免与可能存在的全局名冲突。"""
    module_path = Path(__file__).with_name("ti2_sdk.py")
    spec = importlib.util.spec_from_file_location("ti2_sdk_test", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_args(timing, **overrides):
    """构造一个仿真 ``argparse.Namespace``，便于直接喂给 ``run_scan`` 等函数。"""
    # 默认值都来自 timing 模块自身的常量，避免硬编码漂移。
    values = {
        "start_um": 0.0,
        "step_um": timing.DEFAULT_STEP_UM,
        "steps": timing.DEFAULT_STEPS,
        "direction": "increasing",
        "runs": 1,
        "speed": timing.DEFAULT_SPEED,
        "tolerance": timing.DEFAULT_TOLERANCE,
        "return_to_start": False,
    }
    # ``overrides`` 让单测专注于"某一字段不同时行为是否正确"。
    values.update(overrides)
    return SimpleNamespace(**values)


class ZScanTimingTests(unittest.TestCase):
    """``z_scan_timing.py`` 纯函数（build_targets/summarize/validate 等）的回归测试。"""
    def test_build_targets_uses_incremental_300_nm_moves(self) -> None:
        timing = load_timing_module()

        targets = timing.build_targets(
            start_um=100.0,
            step_um=0.3,
            steps=10,
            direction="increasing",
        )

        self.assertEqual(len(targets), 10)
        self.assertEqual(targets[0], 100.3)
        self.assertEqual(targets[-1], 103.0)

    def test_parse_args_has_only_mic_dataset_return_mode(self) -> None:
        timing = load_timing_module()

        args = timing.parse_args([])

        self.assertFalse(hasattr(args, "poll_interval_s"))
        self.assertFalse(hasattr(args, "settle_samples"))
        self.assertFalse(hasattr(args, "settle_epsilon_um"))
        self.assertFalse(hasattr(args, "post_stable_pause_s"))
        self.assertFalse(hasattr(args, "timeout_s"))

    def test_summarize_uses_sdk_call_ms(self) -> None:
        timing = load_timing_module()

        records = [
            {"sdk_call_ms": 10.0},
            {"sdk_call_ms": 20.0},
            {"sdk_call_ms": 40.0},
        ]

        summary = timing.summarize_records(records)

        self.assertEqual(summary["count"], 3)
        self.assertEqual(summary["min_ms"], 10.0)
        self.assertEqual(summary["median_ms"], 20.0)
        self.assertEqual(summary["max_ms"], 40.0)

    def test_validate_targets_rejects_out_of_range_target(self) -> None:
        timing = load_timing_module()
        z_range = timing.ZRange(lower_um=0.0, upper_um=1.0, lower_dev=0, upper_dev=1000, source="test")

        with self.assertRaises(ValueError):
            timing.validate_targets([0.3, 1.2], z_range)

    def test_resolve_start_um_defaults_to_50_um(self) -> None:
        timing = load_timing_module()
        z_range = timing.ZRange(lower_um=0.0, upper_um=10000.0, lower_dev=0, upper_dev=10000000, source="test")

        start_um = timing.resolve_start_um(SimpleNamespace(start_um=None), z_range)

        self.assertEqual(start_um, 50.0)

    def test_run_scan_records_dataset_return_as_exposure_ready_without_sleep_or_polling(self) -> None:
        timing = load_timing_module()
        captured: list[dict[str, object]] = []
        perf_values = iter([1.000, 1.025])
        original_perf_counter = timing.time.perf_counter
        original_sleep = timing.time.sleep
        original_write_outputs = timing.write_outputs

        class FakeStage:
            def __init__(self) -> None:
                self.current_um = 0.0
                self.read_count = 0

            def move_z_um(self, target_um: float, speed: int = 1, tolerance: int = 0):
                self.current_um = target_um
                return SimpleNamespace(retcode=0, final_um=target_um)

            def read_z_position_um(self) -> float:
                raise AssertionError("run_scan must not call MIC_DataGet")

        stage = FakeStage()

        def fake_write_outputs(records, output_prefix) -> None:
            captured[:] = [dict(row) for row in records]

        try:
            timing.time.perf_counter = lambda: next(perf_values)
            timing.time.sleep = lambda duration: (_ for _ in ()).throw(AssertionError("run_scan must not sleep"))
            timing.write_outputs = fake_write_outputs

            args = make_args(timing, steps=1)
            z_range = timing.ZRange(lower_um=0.0, upper_um=10.0, lower_dev=0, upper_dev=10000, source="test")

            records = timing.run_scan(stage, args, z_range, Path("unused"))
        finally:
            timing.time.perf_counter = original_perf_counter
            timing.time.sleep = original_sleep
            timing.write_outputs = original_write_outputs

        self.assertEqual(stage.read_count, 0)
        self.assertEqual(len(records), 1)
        self.assertEqual(captured, records)
        self.assertEqual(records[0]["sdk_retcode"], 0)
        self.assertEqual(records[0]["sdk_call_ms"], 25.0)
        self.assertEqual(records[0]["command_to_exposure_ready_ms"], 25.0)
        self.assertEqual(records[0]["step_cycle_ms"], 25.0)
        self.assertNotIn("command_to_stable_ms", records[0])
        self.assertNotIn("settle_poll_count", records[0])
        self.assertNotIn("post_stable_pause_ms", records[0])
        self.assertNotIn("stable", records[0])

    def test_run_scan_uses_planned_start_positions_without_data_get(self) -> None:
        timing = load_timing_module()
        original_write_outputs = timing.write_outputs

        class FakeStage:
            def move_z_um(self, target_um: float, speed: int = 1, tolerance: int = 0):
                return SimpleNamespace(retcode=0, final_um=target_um)

            def read_z_position_um(self) -> float:
                raise AssertionError("run_scan must not call MIC_DataGet")

        try:
            timing.write_outputs = lambda records, output_prefix: None
            args = make_args(timing, steps=2)
            z_range = timing.ZRange(lower_um=0.0, upper_um=10.0, lower_dev=0, upper_dev=10000, source="test")

            records = timing.run_scan(FakeStage(), args, z_range, Path("unused"))
        finally:
            timing.write_outputs = original_write_outputs

        self.assertEqual([record["start_um"] for record in records], [0.0, 0.3])
        self.assertEqual([record["target_um"] for record in records], [0.3, 0.6])

    def test_run_scan_writes_record_and_aborts_on_dataset_error(self) -> None:
        timing = load_timing_module()
        captured: list[dict[str, object]] = []

        class FakeStage:
            def __init__(self) -> None:
                self.current_um = 0.0
                self.moves: list[float] = []

            def move_z_um(self, target_um: float, speed: int = 1, tolerance: int = 0):
                self.moves.append(target_um)
                if len(self.moves) == 1:
                    self.current_um = target_um
                    return SimpleNamespace(retcode=0, final_um=target_um)
                return SimpleNamespace(retcode=77, final_um=math.nan)

            def read_z_position_um(self) -> float:
                return self.current_um

        def fake_write_outputs(records, output_prefix) -> None:
            captured[:] = [dict(row) for row in records]

        timing.write_outputs = fake_write_outputs

        args = make_args(timing, steps=1)
        z_range = timing.ZRange(lower_um=0.0, upper_um=10.0, lower_dev=0, upper_dev=10000, source="test")

        with self.assertRaises(timing.Ti2SdkError):
            timing.run_scan(FakeStage(), args, z_range, Path("unused"))

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["sdk_retcode"], 77)
        self.assertIn("MIC_DataSet failed", captured[0]["error"])

    def test_ti2_move_error_does_not_convert_untrusted_output_position(self) -> None:
        ti2 = load_ti2_module()

        class FakeDll:
            def MIC_DataSet(self, data_in, data_out, wait_until_complete) -> int:
                data_out._obj.iZPOSITION = 123456
                return 77

        stage = object.__new__(ti2.Ti2Stage)
        stage._dll = FakeDll()
        stage.um_to_dev = lambda value: 300

        def fail_if_called(value: int) -> float:
            raise AssertionError("dev_to_um should not run after MIC_DataSet returns an error")

        stage.dev_to_um = fail_if_called

        result = ti2.Ti2Stage.move_z_um(stage, 0.3)

        self.assertEqual(result.retcode, 77)
        self.assertEqual(result.final_dev, 123456)
        self.assertTrue(math.isnan(result.final_um))

    def test_last_step_cycle_is_measured_before_output_write(self) -> None:
        timing = load_timing_module()
        perf_values = iter([1.000, 1.020])
        original_perf_counter = timing.time.perf_counter
        original_write_outputs = timing.write_outputs

        class FakeStage:
            def __init__(self) -> None:
                self.current_um = 0.0

            def move_z_um(self, target_um: float, speed: int = 1, tolerance: int = 0):
                self.current_um = target_um
                return SimpleNamespace(retcode=0, final_um=target_um)

            def read_z_position_um(self) -> float:
                return self.current_um

        def fake_write_outputs(records, output_prefix) -> None:
            timing.time.perf_counter = lambda: 9.990

        try:
            timing.time.perf_counter = lambda: next(perf_values)
            timing.write_outputs = fake_write_outputs

            args = make_args(timing, steps=1)
            z_range = timing.ZRange(lower_um=0.0, upper_um=10.0, lower_dev=0, upper_dev=10000, source="test")

            records = timing.run_scan(FakeStage(), args, z_range, Path("unused"))
        finally:
            timing.time.perf_counter = original_perf_counter
            timing.write_outputs = original_write_outputs

        self.assertEqual(records[0]["sdk_call_ms"], 20.0)
        self.assertEqual(records[0]["step_cycle_ms"], 20.0)


if __name__ == "__main__":
    unittest.main()

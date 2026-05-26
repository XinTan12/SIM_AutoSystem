"""``acquisition_core.run_single_acquisition`` 单次采集流程的回归测试。

作用：
    用轻量 FakeCamera / FakeSlm / FakeDaq 类（局部定义）覆盖六个关键场景：
        1. 正常路径：转发 9 个 ``frame_captured`` 事件、最终返回正确形状的 batch。
        2. DAQ ``play_waveform`` 抛错时，相机仍被 ``disarm``、DAQ 也被 ``set_all_low``。
        3. 相机返回非法 stack/timestamps 时拒绝完成、且不广播 ``acquisition_complete``。
        4. 非法 stack 路径下不应"回填" frame_captured 事件（避免误导上层进度）。
        5. ``stop_event`` 在最早期就 set → 抛 ``AcquisitionCancelled``，不 arm 相机、
           不 activate SLM、不 play DAQ；但仍保证 disarm + set_all_low 收尾。
        6. ``stop_event`` 在 DAQ 播放过程中被 set → 相机不读帧，DAQ stop_event 已被传给 adapter。

协作关系：
    上游：``unittest``、``numpy``、``threading``。
    下游：``sim_control.acquisition_core.run_single_acquisition`` 与 ``AcquisitionCancelled``、
          ``sim_control.adapters.HardwareError``、``sim_control.models`` 数据类。

维护要点：
    - 测试使用最简 Fake 类替代 adapter，不依赖仿真 adapter，便于精确断言调用次数。
    - 修改 ``acquisition_core`` 的 finally 路径前，请确保 6 个场景都仍通过。
"""

import sys
import threading
import unittest
from unittest import mock
from pathlib import Path

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class AcquisitionCoreTests(unittest.TestCase):
    """验证单次采集核心在正常、异常和取消路径下都能保持硬件清理顺序。"""

    def test_z_scan_failure_restores_formal_running_order_before_reraising(self):
        from sim_control.acquisition_core import run_single_acquisition
        from sim_control.models import (
            DaqLineConfig,
            PatternPreparationResult,
            SimTaskConfig,
            ZScanConfig,
        )

        class FakeSlm:
            def __init__(self):
                self.selected = []

            def select_running_order(self, index):
                self.selected.append(index)

            def activate_prepared_patterns(self):
                return None

        statuses = []
        slm = FakeSlm()
        with mock.patch("sim_control.acquisition_core.run_z_scan", side_effect=RuntimeError("z scan failed")):
            with self.assertRaisesRegex(RuntimeError, "z scan failed"):
                run_single_acquisition(
                    task=SimTaskConfig(),
                    daq_config=DaqLineConfig(),
                    pattern_result=PatternPreparationResult(
                        handles=[-1],
                        metadata={
                            "mode": "running_order",
                            "running_order_index": 3,
                            "running_order_name": "488_3.5_2d_10ms",
                        },
                    ),
                    camera=mock.Mock(),
                    slm=slm,
                    daq=mock.Mock(),
                    task_id="zscan-restore-success",
                    on_status=lambda state, payload: statuses.append((state, payload)),
                    stage_adapter=mock.Mock(),
                    z_scan_config=ZScanConfig(start_um=0.0, step_um=0.5, num_steps=3),
                    z_scan_pattern_result=PatternPreparationResult(
                        handles=[-1],
                        metadata={"mode": "running_order", "running_order_name": "488_3.5_2d_zscan3p_8ms"},
                    ),
                )

        self.assertEqual(slm.selected, [3])
        self.assertIn("running_order_restored_after_z_scan_failure", [state for state, _payload in statuses])

    def test_z_scan_failure_reports_warning_when_formal_running_order_restore_fails(self):
        from sim_control.acquisition_core import run_single_acquisition
        from sim_control.adapters import HardwareError
        from sim_control.models import (
            DaqLineConfig,
            PatternPreparationResult,
            SimTaskConfig,
            ZScanConfig,
        )

        class FakeSlm:
            def select_running_order(self, index):
                raise RuntimeError(f"cannot select {index}")

            def activate_prepared_patterns(self):
                return None

        statuses = []
        with mock.patch("sim_control.acquisition_core.run_z_scan", side_effect=RuntimeError("z scan failed")):
            with self.assertRaisesRegex(HardwareError, "SLM may still be on z-scan RO"):
                run_single_acquisition(
                    task=SimTaskConfig(),
                    daq_config=DaqLineConfig(),
                    pattern_result=PatternPreparationResult(
                        handles=[-1],
                        metadata={
                            "mode": "running_order",
                            "running_order_index": 3,
                            "running_order_name": "488_3.5_2d_10ms",
                        },
                    ),
                    camera=mock.Mock(),
                    slm=FakeSlm(),
                    daq=mock.Mock(),
                    task_id="zscan-restore-fail",
                    on_status=lambda state, payload: statuses.append((state, payload)),
                    stage_adapter=mock.Mock(),
                    z_scan_config=ZScanConfig(start_um=0.0, step_um=0.5, num_steps=3),
                    z_scan_pattern_result=PatternPreparationResult(
                        handles=[-1],
                        metadata={"mode": "running_order", "running_order_name": "488_3.5_2d_zscan3p_8ms"},
                    ),
                )

        warning_payloads = [payload for state, payload in statuses if state == "running_order_restore_warning"]
        self.assertEqual(len(warning_payloads), 1)
        self.assertIn("SLM may still be on z-scan RO", warning_payloads[0]["message"])

    def test_z_scan_cancel_restore_failure_is_reported_as_hardware_error(self):
        from sim_control.acquisition_core import run_single_acquisition
        from sim_control.adapters import HardwareError
        from sim_control.z_scan_core import ZScanCancelled
        from sim_control.models import (
            DaqLineConfig,
            PatternPreparationResult,
            SimTaskConfig,
            ZScanConfig,
        )

        class FakeSlm:
            def select_running_order(self, index):
                raise RuntimeError(f"cannot select {index}")

            def activate_prepared_patterns(self):
                return None

        statuses = []
        with mock.patch("sim_control.acquisition_core.run_z_scan", side_effect=ZScanCancelled("cancelled")):
            with self.assertRaisesRegex(HardwareError, "SLM may still be on z-scan RO"):
                run_single_acquisition(
                    task=SimTaskConfig(),
                    daq_config=DaqLineConfig(),
                    pattern_result=PatternPreparationResult(
                        handles=[-1],
                        metadata={
                            "mode": "running_order",
                            "running_order_index": 3,
                            "running_order_name": "488_3.5_2d_10ms",
                        },
                    ),
                    camera=mock.Mock(),
                    slm=FakeSlm(),
                    daq=mock.Mock(),
                    task_id="zscan-cancel-restore-fail",
                    on_status=lambda state, payload: statuses.append((state, payload)),
                    stage_adapter=mock.Mock(),
                    z_scan_config=ZScanConfig(start_um=0.0, step_um=0.5, num_steps=3),
                    z_scan_pattern_result=PatternPreparationResult(
                        handles=[-1],
                        metadata={"mode": "running_order", "running_order_name": "488_3.5_2d_zscan3p_8ms"},
                    ),
                )

        self.assertIn("running_order_restore_warning", [state for state, _payload in statuses])

    def test_z_scan_disabled_runs_sim9_at_current_z_without_stage_motion(self):
        from sim_control.acquisition_core import run_single_acquisition
        from sim_control.models import DaqLineConfig, PatternPreparationResult, SimTaskConfig, ZScanConfig

        camera = mock.Mock()
        camera.read_frame_sequence.return_value = (
            np.ones((9, 2, 3), dtype=np.uint16),
            [float(index) for index in range(9)],
        )
        stage = mock.Mock()
        daq = mock.Mock()
        statuses = []

        with mock.patch("sim_control.acquisition_core.run_z_scan") as run_z_scan_mock:
            batch = run_single_acquisition(
                task=SimTaskConfig(),
                daq_config=DaqLineConfig(),
                pattern_result=PatternPreparationResult(pattern_files=["p"] * 9, handles=list(range(9))),
                camera=camera,
                slm=mock.Mock(),
                daq=daq,
                task_id="zscan-disabled",
                on_status=lambda state, payload: statuses.append((state, payload)),
                stage_adapter=stage,
                z_scan_config=ZScanConfig(enabled=False),
                z_scan_pattern_result=None,
            )

        run_z_scan_mock.assert_not_called()
        stage.move_z_um.assert_not_called()
        self.assertEqual(batch.stack.shape, (9, 2, 3))
        self.assertNotIn("z_scan", batch.metadata)

    def test_run_single_acquisition_relays_frame_progress_from_camera_read(self):
        """正常路径：9 个 frame_captured 事件按顺序广播，disarm/set_all_low 各调一次。"""
        from sim_control.acquisition_core import run_single_acquisition
        from sim_control.models import DaqLineConfig, PatternPreparationResult, SimTaskConfig

        # FakeCamera：read_frame_sequence 内部逐帧调 frame_callback；测试断言事件顺序。
        class FakeCamera:
            def __init__(self):
                self.disarmed = False

            def apply_config(self, config):
                return {}

            def arm(self, frame_count):
                return None

            def disarm(self):
                self.disarmed = True

            def read_frame_sequence(
                self,
                frame_count,
                pattern_files,
                laser_wavelength_nm,
                frame_callback,
                stop_event=None,
            ):
                # 逐帧 emit frame_callback，并返回与 frame_count 匹配的 stack 和 timestamps。
                timestamps = []
                for index in range(1, frame_count + 1):
                    timestamp = float(index)
                    timestamps.append(timestamp)
                    frame_callback(index, timestamp)
                return np.ones((frame_count, 2, 3), dtype=np.uint16), timestamps

        class FakeSlm:
            def activate_prepared_patterns(self):
                return None

        # FakeDaq：play_waveform 与 set_all_low 都不真正工作；仅记录调用以做断言。
        class FakeDaq:
            def __init__(self):
                self.reset_devices = []

            def play_waveform(self, device_name, plan, stop_event=None):
                return None

            def set_all_low(self, device_name):
                self.reset_devices.append(device_name)

        camera = FakeCamera()
        daq = FakeDaq()
        statuses = []

        batch = run_single_acquisition(
            task=SimTaskConfig(),
            daq_config=DaqLineConfig(),
            pattern_result=PatternPreparationResult(pattern_files=["p"] * 9, handles=list(range(9))),
            camera=camera,
            slm=FakeSlm(),
            daq=daq,
            task_id="progress-test",
            on_status=lambda state, payload: statuses.append((state, payload)),
        )

        # 1) 返回 batch 形状应与 FakeCamera 返回的相同。
        self.assertEqual(batch.stack.shape, (9, 2, 3))
        # 2) finally 中 disarm 被调用。
        self.assertTrue(camera.disarmed)
        # 3) finally 中 set_all_low 也对默认 Dev1 调过一次。
        self.assertEqual(daq.reset_devices, ["Dev1"])
        # 4) frame_captured 事件应按 1..9 顺序广播。
        self.assertEqual([payload["frame_index"] for state, payload in statuses if state == "frame_captured"], list(range(1, 10)))

    def test_run_single_acquisition_disarms_camera_when_daq_play_fails(self):
        """DAQ play_waveform 抛 RuntimeError 时，相机仍被 disarm，DAQ 仍被 reset。"""
        from sim_control.acquisition_core import run_single_acquisition
        from sim_control.models import DaqLineConfig, PatternPreparationResult, SimTaskConfig

        class FakeCamera:
            def __init__(self):
                self.disarm_calls = 0

            def apply_config(self, config):
                return {}

            def arm(self, frame_count):
                return None

            def disarm(self):
                self.disarm_calls += 1

        class FakeSlm:
            def activate_prepared_patterns(self):
                return None

        # FakeDaq.play_waveform 抛错；本测试要验证此时 finally 路径仍跑。
        class FakeDaq:
            def __init__(self):
                self.reset_calls = 0

            def play_waveform(self, device_name, plan, stop_event=None):
                raise RuntimeError("daq failed")

            def set_all_low(self, device_name):
                self.reset_calls += 1

        camera = FakeCamera()
        daq = FakeDaq()

        # 1) 异常应原样向外抛；上下文 manager 验证错误消息子串。
        with self.assertRaisesRegex(RuntimeError, "daq failed"):
            run_single_acquisition(
                task=SimTaskConfig(),
                daq_config=DaqLineConfig(),
                pattern_result=PatternPreparationResult(pattern_files=["p"] * 9, handles=list(range(9))),
                camera=camera,
                slm=FakeSlm(),
                daq=daq,
            )

        # 2) finally 不能被异常吞掉：disarm 和 set_all_low 各调一次。
        self.assertEqual(camera.disarm_calls, 1)
        self.assertEqual(daq.reset_calls, 1)

    def test_run_single_acquisition_rejects_invalid_stack_or_timestamps_before_completion(self):
        """4 种非法 stack/timestamps 都应抛 HardwareError，且不发 acquisition_complete。"""
        from sim_control.acquisition_core import run_single_acquisition
        from sim_control.adapters import HardwareError
        from sim_control.models import DaqLineConfig, PatternPreparationResult, SimTaskConfig

        # FakeCamera 直接返回构造时给定的 stack/timestamps，模拟硬件协议违规。
        class FakeCamera:
            def __init__(self, stack, timestamps):
                self.stack = stack
                self.timestamps = timestamps

            def apply_config(self, config):
                return {}

            def arm(self, frame_count):
                return None

            def disarm(self):
                return None

            def read_frame_sequence(
                self,
                frame_count,
                pattern_files,
                laser_wavelength_nm,
                frame_callback,
                stop_event=None,
            ):
                return self.stack, self.timestamps

        class FakeSlm:
            def activate_prepared_patterns(self):
                return None

        class FakeDaq:
            def play_waveform(self, device_name, plan, stop_event=None):
                return None

            def set_all_low(self, device_name):
                return None

        # 4 个非法 case：首维 != 9、dtype 非 uint16、timestamps 长度不符、空高度。
        cases = [
            ("wrong first dimension", np.zeros((1, 2, 2), dtype=np.uint16), [float(i) for i in range(9)]),
            ("wrong dtype", np.zeros((9, 2, 2), dtype=np.uint8), [float(i) for i in range(9)]),
            ("missing timestamps", np.zeros((9, 2, 2), dtype=np.uint16), []),
            ("empty height", np.zeros((9, 0, 2), dtype=np.uint16), [float(i) for i in range(9)]),
        ]

        for _label, stack, timestamps in cases:
            statuses = []
            with self.subTest(_label):
                with self.assertRaises(HardwareError):
                    run_single_acquisition(
                        task=SimTaskConfig(),
                        daq_config=DaqLineConfig(),
                        pattern_result=PatternPreparationResult(pattern_files=["p"] * 9, handles=list(range(9))),
                        camera=FakeCamera(stack, timestamps),
                        slm=FakeSlm(),
                        daq=FakeDaq(),
                        on_status=lambda state, payload: statuses.append(state),
                    )
                # 验证未广播 acquisition_complete：失败发生在校验阶段。
                self.assertNotIn("acquisition_complete", statuses)

    def test_run_single_acquisition_does_not_emit_fallback_frames_before_result_validation(self):
        """非法 stack 路径下不应"回填" frame_captured 事件（避免误导上层进度）。"""
        from sim_control.acquisition_core import run_single_acquisition
        from sim_control.adapters import HardwareError
        from sim_control.models import DaqLineConfig, PatternPreparationResult, SimTaskConfig

        class FakeCamera:
            def apply_config(self, config):
                return {}

            def arm(self, frame_count):
                return None

            def disarm(self):
                return None

            def read_frame_sequence(
                self,
                frame_count,
                pattern_files,
                laser_wavelength_nm,
                frame_callback,
                stop_event=None,
            ):
                # 返回首维 != 9 的非法 stack；validate 阶段会抛错。
                return np.zeros((1, 2, 2), dtype=np.uint16), [float(i) for i in range(9)]

        class FakeSlm:
            def activate_prepared_patterns(self):
                return None

        class FakeDaq:
            def play_waveform(self, device_name, plan, stop_event=None):
                return None

            def set_all_low(self, device_name):
                return None

        statuses = []
        with self.assertRaises(HardwareError):
            run_single_acquisition(
                task=SimTaskConfig(),
                daq_config=DaqLineConfig(),
                pattern_result=PatternPreparationResult(pattern_files=["p"] * 9, handles=list(range(9))),
                camera=FakeCamera(),
                slm=FakeSlm(),
                daq=FakeDaq(),
                on_status=lambda state, payload: statuses.append((state, payload)),
            )

        # 关键断言：在失败时不能广播任何 frame_captured，也不能广播 acquisition_complete。
        self.assertFalse([payload for state, payload in statuses if state == "frame_captured"])
        self.assertNotIn("acquisition_complete", [state for state, _payload in statuses])

    def test_run_single_acquisition_cancelled_before_hardware_ops_cleans_up_without_starting_daq(self):
        """stop_event 在最早期就 set → 不 arm 相机、不 activate SLM、不 play DAQ；但仍 disarm + set_all_low。"""
        from sim_control.acquisition_core import AcquisitionCancelled, run_single_acquisition
        from sim_control.models import DaqLineConfig, PatternPreparationResult, SimTaskConfig

        # FakeCamera.arm 故意 raise AssertionError：保证测试在被错误调用时会立即失败。
        class FakeCamera:
            def __init__(self):
                self.apply_calls = 0
                self.disarm_calls = 0

            def apply_config(self, config):
                self.apply_calls += 1

            def arm(self, frame_count):
                raise AssertionError("camera should not arm after cancellation")

            def disarm(self):
                self.disarm_calls += 1

        # FakeSlm.activate 同理 raise，确认它没被调到。
        class FakeSlm:
            def activate_prepared_patterns(self):
                raise AssertionError("SLM should not activate after cancellation")

        class FakeDaq:
            def __init__(self):
                self.play_calls = 0
                self.reset_calls = 0

            def play_waveform(self, device_name, plan, stop_event=None):
                self.play_calls += 1

            def set_all_low(self, device_name):
                self.reset_calls += 1

        # stop_event 在 run 之前就 set；首个 _raise_if_cancelled 检查会立即抛 AcquisitionCancelled。
        stop_event = threading.Event()
        stop_event.set()
        camera = FakeCamera()
        daq = FakeDaq()

        with self.assertRaises(AcquisitionCancelled):
            run_single_acquisition(
                task=SimTaskConfig(),
                daq_config=DaqLineConfig(),
                pattern_result=PatternPreparationResult(pattern_files=["p"] * 9, handles=list(range(9))),
                camera=camera,
                slm=FakeSlm(),
                daq=daq,
                stop_event=stop_event,
            )

        # 1) apply_config 不应被调；arm / activate / play 也不应进入（FakeXxx 内已 assert 过）。
        self.assertEqual(camera.apply_calls, 0)
        # 2) finally 必跑：disarm + set_all_low 各一次。
        self.assertEqual(camera.disarm_calls, 1)
        self.assertEqual(daq.play_calls, 0)
        self.assertEqual(daq.reset_calls, 1)

    def test_run_single_acquisition_cancelled_during_daq_does_not_read_camera(self):
        """stop_event 在 DAQ 播放过程中 set → 不读相机 + DAQ adapter 拿到 stop_event 引用。"""
        from sim_control.acquisition_core import AcquisitionCancelled, run_single_acquisition
        from sim_control.models import DaqLineConfig, PatternPreparationResult, SimTaskConfig

        class FakeCamera:
            def __init__(self):
                self.disarm_calls = 0
                self.read_calls = 0

            def apply_config(self, config):
                return {}

            def arm(self, frame_count):
                return None

            def disarm(self):
                self.disarm_calls += 1

            def read_frame_sequence(self, *args, **kwargs):
                # 不应被调到；进入即测试失败。
                self.read_calls += 1
                raise AssertionError("camera read should not start after DAQ cancellation")

        class FakeSlm:
            def activate_prepared_patterns(self):
                return None

        # FakeDaq.play_waveform 模拟"播放过程中"用户按了 stop：自己 set 一下 stop_event 即可。
        class FakeDaq:
            def __init__(self):
                self.stop_event_seen = None
                self.reset_calls = 0

            def play_waveform(self, device_name, plan, stop_event=None):
                # 记录 stop_event 引用便于测试断言"是同一个对象"。
                self.stop_event_seen = stop_event
                stop_event.set()

            def set_all_low(self, device_name):
                self.reset_calls += 1

        stop_event = threading.Event()
        camera = FakeCamera()
        daq = FakeDaq()

        with self.assertRaises(AcquisitionCancelled):
            run_single_acquisition(
                task=SimTaskConfig(),
                daq_config=DaqLineConfig(),
                pattern_result=PatternPreparationResult(pattern_files=["p"] * 9, handles=list(range(9))),
                camera=camera,
                slm=FakeSlm(),
                daq=daq,
                stop_event=stop_event,
            )

        # 1) DAQ adapter 接到的 stop_event 必须是同一个对象（is 比较）。
        self.assertIs(daq.stop_event_seen, stop_event)
        # 2) 相机 read 不应被进入；finally 中 disarm + set_all_low 仍各跑一次。
        self.assertEqual(camera.read_calls, 0)
        self.assertEqual(camera.disarm_calls, 1)
        self.assertEqual(daq.reset_calls, 1)


if __name__ == "__main__":
    unittest.main()

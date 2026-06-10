"""R11 激活状态查询链路的回归测试。

作用：
    覆盖三层新增的激活状态能力：
        1. ``_R11CommLib.get_activation_state``：fake DLL 路径验证 byte 读取与
           旧版 DLL 缺函数时返回 None。
        2. ``KopinSlmAdapter.get_running_order_activation_state``：状态码到可读
           名的包装、未连接拒绝、旧版 SDK 的 unsupported 报告。
        3. ``acquisition_core.run_single_acquisition``：activate 后 best-effort
           上报 ``slm_activation_state`` 事件；查询失败不影响采集完成。

协作关系：
    上游：``unittest``、``numpy``。
    下游：``sim_control.adapters``、``sim_control.acquisition_core``、
          ``sim_control.models``。

维护要点：
    - 状态码表来自 AN0027AD §3.26 (p.33)：0x54 MHW 表示软件已激活但 EXT_RUN
      未拉高；0x56 ACT 表示 RO 正在执行。修改映射时同步 ``R11_ACTIVATION_STATES``。
    - fake DLL 测试绕过 ``__init__``（不加载真实 R11CommLib），依赖
      ``FDD_SUCCESS`` 为类属性；若改为实例属性需同步本测试。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class R11ActivationStateBindingTests(unittest.TestCase):
    """覆盖 ``_R11CommLib.get_activation_state`` 的 ctypes 绑定行为。"""

    def test_get_activation_state_reads_byte_from_dll(self):
        """fake DLL 写入 0x54 时应返回 84（MHW）。"""
        from sim_control.adapters import _R11CommLib

        class FakeDll:
            @staticmethod
            def R11_RpcRoGetActivationState(byref_value):
                # ctypes.byref 包装对象通过 ``_obj`` 暴露底层 c_uint8。
                byref_value._obj.value = 0x54
                return 0

        sdk = _R11CommLib.__new__(_R11CommLib)
        sdk.dll = FakeDll()

        self.assertEqual(sdk.get_activation_state(), 0x54)

    def test_get_activation_state_returns_none_for_old_dll(self):
        """旧版 R11CommLib 缺少该导出函数时应返回 None 而不是抛错。"""
        from sim_control.adapters import _R11CommLib

        class OldDll:
            pass

        sdk = _R11CommLib.__new__(_R11CommLib)
        sdk.dll = OldDll()

        self.assertIsNone(sdk.get_activation_state())

    def test_activation_state_name_mapping_matches_an0027(self):
        """状态名映射应覆盖 AN0027AD §3.26 全部 8 个码值与未知/不支持回退。"""
        from sim_control.adapters import R11_ACTIVATION_STATES, r11_activation_state_name

        self.assertEqual(len(R11_ACTIVATION_STATES), 8)
        self.assertEqual(r11_activation_state_name(0x56), "ACT(active)")
        self.assertEqual(
            r11_activation_state_name(0x54),
            "MHW(maintenance, hardware deactivated)",
        )
        self.assertTrue(r11_activation_state_name(None).startswith("unsupported"))
        self.assertEqual(r11_activation_state_name(0x99), "unknown(0x99)")


class KopinSlmActivationStateTests(unittest.TestCase):
    """覆盖 ``KopinSlmAdapter.get_running_order_activation_state`` 包装。"""

    def _adapter_with_sdk(self, get_activation_state):
        from sim_control.adapters import KopinSlmAdapter

        adapter = KopinSlmAdapter()
        adapter._initialized = True
        adapter._device_open = True
        adapter._sdk = type(
            "StubSdk",
            (),
            {"get_activation_state": staticmethod(get_activation_state)},
        )()
        return adapter

    def test_wraps_state_code_with_readable_name(self):
        adapter = self._adapter_with_sdk(lambda: 0x56)

        state = adapter.get_running_order_activation_state()

        self.assertEqual(state, {"code": 0x56, "name": "ACT(active)"})

    def test_reports_unsupported_when_sdk_lacks_function(self):
        adapter = self._adapter_with_sdk(lambda: None)

        state = adapter.get_running_order_activation_state()

        self.assertIsNone(state["code"])
        self.assertTrue(state["name"].startswith("unsupported"))

    def test_requires_connected_device(self):
        from sim_control.adapters import HardwareError, KopinSlmAdapter

        adapter = KopinSlmAdapter()
        adapter._initialized = True
        adapter._device_open = False

        with self.assertRaises(HardwareError):
            adapter.get_running_order_activation_state()


class AcquisitionActivationStateEventTests(unittest.TestCase):
    """覆盖采集核心 activate 后的 ``slm_activation_state`` 状态事件。"""

    def _run_acquisition(self, slm):
        from sim_control.acquisition_core import run_single_acquisition
        from sim_control.models import (
            DaqLineConfig,
            PatternPreparationResult,
            SimTaskConfig,
        )

        class FakeCamera:
            def apply_config(self, config):
                self._config = config

            def arm(self, frame_count):
                self._frame_count = frame_count

            def disarm(self):
                return None

            def read_frame_sequence(
                self,
                frame_count,
                pattern_files,
                laser_wavelength_nm,
                frame_callback=None,
                stop_event=None,
            ):
                stack = np.zeros((frame_count, 4, 4), dtype=np.uint16)
                timestamps = [float(index) for index in range(frame_count)]
                return stack, timestamps

        class FakeDaq:
            def play_waveform(self, device_name, plan, stop_event=None):
                return None

            def set_all_low(self, device_name):
                return None

        statuses = []
        run_single_acquisition(
            task=SimTaskConfig(),
            daq_config=DaqLineConfig(),
            pattern_result=PatternPreparationResult(
                handles=[-1],
                metadata={"mode": "running_order", "running_order_name": "488_3.5_2d_50ms"},
            ),
            camera=FakeCamera(),
            slm=slm,
            daq=FakeDaq(),
            task_id="activation-state-task",
            on_status=lambda state, payload: statuses.append((state, payload)),
        )
        return statuses

    def test_status_event_emitted_when_slm_exposes_activation_state(self):
        """SLM 提供激活状态查询时，activate 后应广播一次状态事件。"""

        class FakeSlm:
            def activate_prepared_patterns(self):
                return None

            def get_running_order_activation_state(self):
                return {"code": 0x54, "name": "MHW(maintenance, hardware deactivated)"}

        statuses = self._run_acquisition(FakeSlm())

        events = [payload for state, payload in statuses if state == "slm_activation_state"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["code"], 0x54)
        self.assertIn("MHW", events[0]["name"])
        self.assertEqual(events[0]["task_id"], "activation-state-task")

    def test_activation_state_query_failure_does_not_break_acquisition(self):
        """状态查询抛错只跳过事件，采集仍正常完成。"""

        class FakeSlm:
            def activate_prepared_patterns(self):
                return None

            def get_running_order_activation_state(self):
                raise RuntimeError("usb glitch")

        statuses = self._run_acquisition(FakeSlm())

        states = [state for state, _payload in statuses]
        self.assertNotIn("slm_activation_state", states)
        self.assertIn("acquisition_complete", states)

    def test_legacy_slm_without_state_query_skips_event(self):
        """旧 fake/adapter 没有该方法时按 getattr 守卫静默跳过。"""

        class LegacySlm:
            def activate_prepared_patterns(self):
                return None

        statuses = self._run_acquisition(LegacySlm())

        states = [state for state, _payload in statuses]
        self.assertNotIn("slm_activation_state", states)
        self.assertIn("acquisition_complete", states)


if __name__ == "__main__":
    unittest.main()

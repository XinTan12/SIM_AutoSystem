"""immediate-live（找样品）链路测试。

覆盖范围：
    1. 控制器层（``SimAcquisitionController`` + 仿真适配器，无 GUI）：
       - 仅 ``ACT_IMMEDIATE``(0x01) RO 进 immediate 列表（0x02/0x04 不进）。
       - 正式 RO 选择经 ``exclude_indices`` 排除 immediate（含 ``find_best_running_order`` 直连路径）。
       - ``activate_immediate_running_order`` 拉高对应波长激光线（``SimulatedDaqAdapter.set_line_calls``）。
       - 波长错配 / 非 immediate index 被拒，且不开激光。
       - 激活失败 best-effort 拉低并清状态。
       - ``stop_immediate_live`` 未激活时严格 no-op（不写 port）。
    2. GUI 层（``MainWindow`` 方法 + ``SimpleNamespace`` 假 self + 真 QComboBox）：
       - 相机未连接时选 immediate RO 不开激光（关键激光安全）。
       - 下拉按当前波长和 ACT_IMMEDIATE 过滤，非 immediate 诊断项 disable。

维护要点：
    - 控制器测试用 ``BackendConfig(simulation_mode=True)``，仿真 DAQ 记录 set_line 调用。
    - 仿真 immediate RO 为末尾追加的 ``488_3.5_2d_imm_f1``..``f9`` 和 ``3dir``。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sim_control.adapters import (
    KopinSlmAdapter,
    R11_ACTIVATION_TYPE_HARDWARE,
    R11_ACTIVATION_TYPE_IMMEDIATE,
    R11_ACTIVATION_TYPE_SOFTWARE,
    find_best_running_order,
    is_immediate_activation_type,
    parse_leading_wavelength_nm,
)
from sim_control.controller import SimAcquisitionController, immediate_live_wavelength_matches
from sim_control.errors import HardwareError
from sim_control.models import BackendConfig


def _make_sim_controller() -> SimAcquisitionController:
    controller = SimAcquisitionController(BackendConfig(simulation_mode=True))
    controller.connect_slm()
    return controller


class ImmediateActivationTypeHelperTests(unittest.TestCase):
    def test_only_immediate_constant_is_immediate(self):
        self.assertTrue(is_immediate_activation_type(R11_ACTIVATION_TYPE_IMMEDIATE))
        self.assertFalse(is_immediate_activation_type(R11_ACTIVATION_TYPE_SOFTWARE))
        self.assertFalse(is_immediate_activation_type(R11_ACTIVATION_TYPE_HARDWARE))
        self.assertFalse(is_immediate_activation_type(None))

    def test_constants_match_r11_header(self):
        # rpc.h: ACT_IMMEDIATE=0x01, ACT_SOFTWARE=0x02, ACT_HARDWARE=0x04
        self.assertEqual(R11_ACTIVATION_TYPE_IMMEDIATE, 0x01)
        self.assertEqual(R11_ACTIVATION_TYPE_SOFTWARE, 0x02)
        self.assertEqual(R11_ACTIVATION_TYPE_HARDWARE, 0x04)

    def test_parse_leading_wavelength(self):
        self.assertEqual(parse_leading_wavelength_nm("488_3.5_2d_imm_f1"), 488)
        self.assertEqual(parse_leading_wavelength_nm("561_3.5_2d_imm_f1"), 561)
        self.assertIsNone(parse_leading_wavelength_nm("live_imm_no_wavelength"))

    def test_controller_immediate_wavelength_match_allows_legacy_red_only_for_638(self):
        self.assertTrue(immediate_live_wavelength_matches(405, 405))
        self.assertTrue(immediate_live_wavelength_matches(561, 561))
        self.assertTrue(immediate_live_wavelength_matches(647, 638))
        self.assertFalse(immediate_live_wavelength_matches(647, 561))
        self.assertFalse(immediate_live_wavelength_matches(None, 638))


class KopinSlmImmediateScanTimingTests(unittest.TestCase):
    def test_scan_waits_after_select_before_reading_activation_type(self):
        class LaggingActivationSdk:
            def __init__(self):
                self.selected = 0
                self.settled = False
                self.activation = {
                    0: R11_ACTIVATION_TYPE_HARDWARE,
                    1: R11_ACTIVATION_TYPE_IMMEDIATE,
                }

            def get_running_order_count(self):
                return 2

            def get_running_order_name(self, index):
                return ["488_3.5_2d_10ms", "488_3.5_2d_imm_f1"][index]

            def get_selected_running_order(self):
                return self.selected

            def set_selected_running_order_blind(self, index):
                self.selected = index
                self.settled = False
                return True

            def set_selected_running_order(self, index):
                self.selected = index
                self.settled = False

            def get_running_order_activation_type(self):
                if not self.settled:
                    return R11_ACTIVATION_TYPE_HARDWARE
                return self.activation[self.selected]

        sdk = LaggingActivationSdk()
        adapter = KopinSlmAdapter()
        adapter._initialized = True
        adapter._device_open = True
        adapter._sdk = sdk

        def mark_settled(_duration):
            sdk.settled = True

        with mock.patch("sim_control.adapters.time.sleep", side_effect=mark_settled) as sleep:
            scan = adapter.list_running_orders_with_activation()

        by_name = {item["name"]: item for item in scan}
        self.assertFalse(by_name["488_3.5_2d_10ms"]["is_immediate"])
        self.assertTrue(by_name["488_3.5_2d_imm_f1"]["is_immediate"])
        self.assertGreaterEqual(sleep.call_count, 2)

    def test_select_running_order_waits_before_returning_activation_type(self):
        class LaggingSelectSdk:
            def __init__(self):
                self.selected = 0
                self.settled = False

            def set_selected_running_order(self, index):
                self.selected = index
                self.settled = False

            def get_running_order_name(self, index):
                self.assert_selected = index
                return "488_3.5_2d_imm_f1"

            def get_running_order_activation_type(self):
                if not self.settled:
                    return R11_ACTIVATION_TYPE_HARDWARE
                return R11_ACTIVATION_TYPE_IMMEDIATE

        sdk = LaggingSelectSdk()
        adapter = KopinSlmAdapter()
        adapter._initialized = True
        adapter._device_open = True
        adapter._sdk = sdk

        def mark_settled(_duration):
            sdk.settled = True

        with mock.patch("sim_control.adapters.time.sleep", side_effect=mark_settled) as sleep:
            result = adapter.select_running_order(28)

        self.assertEqual(result["running_order_name"], "488_3.5_2d_imm_f1")
        self.assertEqual(result["activation_type"], R11_ACTIVATION_TYPE_IMMEDIATE)
        self.assertEqual(result["pattern_result"].metadata["activation_type"], R11_ACTIVATION_TYPE_IMMEDIATE)
        sleep.assert_called_once()


class ImmediateRunningOrderScanTests(unittest.TestCase):
    def setUp(self):
        self.controller = _make_sim_controller()

    def tearDown(self):
        self.controller.shutdown()

    def test_scan_classifies_only_immediate(self):
        immediate = self.controller.refresh_immediate_running_orders()
        names = {item["name"] for item in immediate}
        expected = {
            name
            for wavelength in (488, 405, 561, 647)
            for name in [*(f"{wavelength}_3.5_2d_imm_f{i}" for i in range(1, 10)), f"{wavelength}_3.5_2d_imm_3dir"]
        }
        self.assertEqual(names, expected)
        # 全部 immediate 项的 activation_type 必须是 0x01。
        self.assertTrue(all(item["activation_type"] == 0x01 for item in immediate))
        # 普通 / z-scan RO 不得进 immediate 集合。
        self.assertNotIn("488_3.5_2d_10ms", names)
        self.assertNotIn("488_3.5_2d_zscan3p_8ms", names)

    def test_list_immediate_filters_by_wavelength(self):
        self.controller.refresh_immediate_running_orders()
        items_488 = self.controller.list_immediate_running_orders(488)
        by_name = {item["name"]: item for item in items_488}
        self.assertTrue(by_name["488_3.5_2d_imm_f1"]["selectable"])
        self.assertFalse(by_name["561_3.5_2d_imm_f1"]["selectable"])
        items_561 = self.controller.list_immediate_running_orders(561)
        by_name_561 = {item["name"]: item for item in items_561}
        self.assertTrue(by_name_561["561_3.5_2d_imm_f1"]["selectable"])
        self.assertFalse(by_name_561["647_3.5_2d_imm_f1"]["selectable"])
        items_638 = self.controller.list_immediate_running_orders(638)
        by_name_638 = {item["name"]: item for item in items_638}
        self.assertTrue(by_name_638["647_3.5_2d_imm_f1"]["selectable"])

    def test_preview_dropdown_includes_non_immediate_diagnostics_but_live_list_does_not(self):
        custom_scan = [
            {
                "index": 28,
                "name": "488_3.5_2d_imm_f1",
                "activation_type": R11_ACTIVATION_TYPE_HARDWARE,
                "is_immediate": False,
            },
            {
                "index": 29,
                "name": "488_3.5_2d_imm_f2",
                "activation_type": R11_ACTIVATION_TYPE_IMMEDIATE,
                "is_immediate": True,
            },
            {
                "index": 30,
                "name": "488_3.5_2d_live_imm",
                "activation_type": R11_ACTIVATION_TYPE_IMMEDIATE,
                "is_immediate": True,
            },
        ]
        with mock.patch.object(self.controller.slm_adapter, "list_running_orders_with_activation", return_value=custom_scan):
            immediate = self.controller.refresh_immediate_running_orders()

        self.assertEqual([item["name"] for item in immediate], ["488_3.5_2d_imm_f2"])
        dropdown_items = self.controller.list_immediate_dropdown_running_orders()
        self.assertEqual([item["name"] for item in dropdown_items], ["488_3.5_2d_imm_f1", "488_3.5_2d_imm_f2"])
        self.assertFalse(dropdown_items[0]["selectable"])
        self.assertIn("488_3.5_2d_imm_f1", self.controller.immediate_running_order_warnings(488)[0])
        # 全部 ACT_IMMEDIATE 索引仍用于正式采集排除；找样品激活只允许新命名组。
        self.assertEqual(self.controller._immediate_ro_indices, {29, 30})
        self.assertEqual(self.controller._immediate_live_ro_indices, {29})

    def test_legacy_red_non_immediate_warning_is_filtered_as_638_only(self):
        custom_scan = [
            {
                "index": 31,
                "name": "647_3.5_2d_imm_f1",
                "activation_type": R11_ACTIVATION_TYPE_HARDWARE,
                "is_immediate": False,
            },
        ]
        with mock.patch.object(self.controller.slm_adapter, "list_running_orders_with_activation", return_value=custom_scan):
            self.controller.refresh_immediate_running_orders()

        warnings_638 = self.controller.immediate_running_order_warnings(638)
        self.assertEqual(len(warnings_638), 1)
        self.assertIn("647_3.5_2d_imm_f1", warnings_638[0])
        self.assertEqual(self.controller.immediate_running_order_warnings(561), [])

    def test_formal_selection_excludes_immediate_indices(self):
        # 即便构造一个"会被正式正则匹配但落在 exclude 集合"的 index，也必须被跳过。
        running_orders = [
            (0, "488_3.5_2d_10ms"),
            (1, "488_3.5_2d_10ms"),  # 同名，模拟巧合命名的 immediate RO
        ]
        # 不排除：选到 index 0。
        idx, name, _ = find_best_running_order(running_orders, 488, 11_000)
        self.assertEqual(idx, 0)
        # 排除 0：退到 index 1。
        idx, name, _ = find_best_running_order(running_orders, 488, 11_000, exclude_indices={0})
        self.assertEqual(idx, 1)
        # 排除全部：无匹配。
        idx, name, warnings = find_best_running_order(running_orders, 488, 11_000, exclude_indices={0, 1})
        self.assertIsNone(idx)
        self.assertTrue(warnings)

    def test_select_running_order_for_task_skips_immediate(self):
        self.controller.refresh_immediate_running_orders()
        # immediate 名字（488_3.5_2d_imm_f1）本就不撞正式正则，正式选择应选到正式 RO。
        payload = self.controller.select_running_order_for_task(488, 11_000)
        self.assertEqual(payload["running_order_name"], "488_3.5_2d_10ms")

    def test_disconnect_clears_immediate_state(self):
        self.controller.refresh_immediate_running_orders()
        self.assertTrue(self.controller._immediate_ro_indices)
        self.controller.disconnect_slm()
        # 断开后 controller 自洽：immediate 缓存清空，不再喂 exclude_indices。
        self.assertEqual(self.controller._immediate_ro_indices, set())
        self.assertEqual(self.controller.list_immediate_running_orders(), [])
        self.assertFalse(self.controller.is_immediate_live_active)


class ImmediateLaserDriveTests(unittest.TestCase):
    def setUp(self):
        self.controller = _make_sim_controller()
        self.controller.refresh_immediate_running_orders()
        self.daq = self.controller.daq_adapter
        # 488 immediate RO 的 index。
        self.ro_488 = next(
            item["index"]
            for item in self.controller.list_immediate_running_orders()
            if item["name"] == "488_3.5_2d_imm_f1"
        )

    def tearDown(self):
        self.controller.shutdown()

    def _refresh_custom_immediate_scan(self, scan: list[dict[str, object]]) -> None:
        with mock.patch.object(self.controller.slm_adapter, "list_running_orders_with_activation", return_value=scan):
            self.controller.refresh_immediate_running_orders()

    def _activate_custom_ro(self, ro_index: int, ro_name: str, wavelength_nm: int) -> dict[str, object]:
        with mock.patch.object(
            self.controller.slm_adapter,
            "select_running_order",
            return_value={
                "running_order_index": ro_index,
                "running_order_name": ro_name,
                "activation_type": R11_ACTIVATION_TYPE_IMMEDIATE,
            },
        ):
            with mock.patch.object(self.controller.slm_adapter, "activate_prepared_patterns"):
                return self.controller.activate_immediate_running_order(ro_index, wavelength_nm)

    def test_activate_drives_correct_laser_line_high(self):
        self.controller.activate_immediate_running_order(self.ro_488, 488)
        self.assertTrue(self.controller.is_immediate_live_active)
        # 默认 laser_488_line = Dev1/port0/line10 → set_line(("Dev1", 10, True))。
        self.assertIn(("Dev1", 10, True), self.daq.set_line_calls)

    def test_405_and_561_immediate_drive_matching_laser_lines(self):
        custom_scan = [
            {
                "index": 101,
                "name": "405_3.5_2d_imm_f1",
                "activation_type": R11_ACTIVATION_TYPE_IMMEDIATE,
                "is_immediate": True,
            },
            {
                "index": 102,
                "name": "561_3.5_2d_imm_f1",
                "activation_type": R11_ACTIVATION_TYPE_IMMEDIATE,
                "is_immediate": True,
            },
        ]
        self._refresh_custom_immediate_scan(custom_scan)

        items_405 = {item["name"]: item for item in self.controller.list_immediate_running_orders(405)}
        items_561 = {item["name"]: item for item in self.controller.list_immediate_running_orders(561)}
        self.assertTrue(items_405["405_3.5_2d_imm_f1"]["selectable"])
        self.assertTrue(items_561["561_3.5_2d_imm_f1"]["selectable"])

        self._activate_custom_ro(101, "405_3.5_2d_imm_f1", 405)
        self.assertIn(("Dev1", 9, True), self.daq.set_line_calls)
        self.controller.stop_immediate_live()

        self._activate_custom_ro(102, "561_3.5_2d_imm_f1", 561)
        self.assertIn(("Dev1", 11, True), self.daq.set_line_calls)

    def test_647_named_immediate_selectable_for_638_and_drives_638_line(self):
        custom_scan = [
            {
                "index": 201,
                "name": "647_3.5_2d_imm_f1",
                "activation_type": R11_ACTIVATION_TYPE_IMMEDIATE,
                "is_immediate": True,
            },
        ]
        self._refresh_custom_immediate_scan(custom_scan)

        items_638 = {item["name"]: item for item in self.controller.list_immediate_running_orders(638)}
        self.assertTrue(items_638["647_3.5_2d_imm_f1"]["selectable"])

        payload = self._activate_custom_ro(201, "647_3.5_2d_imm_f1", 638)
        self.assertEqual(payload["wavelength_nm"], 638)
        self.assertEqual(payload["running_order_name"], "647_3.5_2d_imm_f1")
        self.assertIn(("Dev1", 12, True), self.daq.set_line_calls)

    def test_647_named_immediate_rejected_for_non_red_without_laser(self):
        custom_scan = [
            {
                "index": 202,
                "name": "647_3.5_2d_imm_f1",
                "activation_type": R11_ACTIVATION_TYPE_IMMEDIATE,
                "is_immediate": True,
            },
        ]
        self._refresh_custom_immediate_scan(custom_scan)

        items_561 = {item["name"]: item for item in self.controller.list_immediate_running_orders(561)}
        self.assertFalse(items_561["647_3.5_2d_imm_f1"]["selectable"])

        before = list(self.daq.set_line_calls)
        with self.assertRaises(HardwareError):
            self.controller.activate_immediate_running_order(202, 561)
        self.assertFalse(self.controller.is_immediate_live_active)
        self.assertEqual(self.daq.set_line_calls, before)

    def test_stop_after_activate_sets_low(self):
        self.controller.activate_immediate_running_order(self.ro_488, 488)
        before = self.daq.set_all_low_calls
        self.controller.stop_immediate_live()
        self.assertFalse(self.controller.is_immediate_live_active)
        self.assertEqual(self.daq.set_all_low_calls, before + 1)

    def test_stop_when_idle_is_noop(self):
        before_lines = list(self.daq.set_line_calls)
        before_low = self.daq.set_all_low_calls
        self.controller.stop_immediate_live()  # 未激活
        self.assertEqual(self.daq.set_line_calls, before_lines)
        self.assertEqual(self.daq.set_all_low_calls, before_low)

    def test_wavelength_mismatch_rejected_no_laser(self):
        before = list(self.daq.set_line_calls)
        with self.assertRaises(HardwareError):
            self.controller.activate_immediate_running_order(self.ro_488, 561)
        self.assertFalse(self.controller.is_immediate_live_active)
        # 没有任何把激光线写高的调用。
        self.assertEqual(self.daq.set_line_calls, before)

    def test_non_immediate_index_rejected(self):
        with self.assertRaises(HardwareError):
            self.controller.activate_immediate_running_order(7, 488)  # 7 是正式/_ang0 RO
        self.assertFalse(self.controller.is_immediate_live_active)

    def test_activation_rechecks_current_activation_type_before_laser(self):
        before = list(self.daq.set_line_calls)
        with mock.patch.object(
            self.controller.slm_adapter,
            "select_running_order",
            return_value={
                "running_order_index": self.ro_488,
                "running_order_name": "488_3.5_2d_imm_f1",
                "activation_type": R11_ACTIVATION_TYPE_HARDWARE,
            },
        ):
            with self.assertRaisesRegex(HardwareError, "no longer ACT_IMMEDIATE"):
                self.controller.activate_immediate_running_order(self.ro_488, 488)
        self.assertEqual(self.daq.set_line_calls, before)
        self.assertFalse(self.controller.is_immediate_live_active)
        self.assertNotIn(self.ro_488, self.controller._immediate_live_ro_indices)

    def test_activation_failure_pulls_low_and_clears(self):
        before_low = self.daq.set_all_low_calls
        with mock.patch.object(
            self.controller.slm_adapter, "activate_prepared_patterns", side_effect=HardwareError("boom")
        ):
            with self.assertRaises(HardwareError):
                self.controller.activate_immediate_running_order(self.ro_488, 488)
        self.assertFalse(self.controller.is_immediate_live_active)
        # 失败路径 best-effort 拉低。
        self.assertEqual(self.daq.set_all_low_calls, before_low + 1)

    def test_stop_raises_and_keeps_state_when_low_fails(self):
        # 硬件安全回归（codex blocking B1）：stop 拉低失败时必须保留状态并抛错，绝不静默清状态，
        # 否则后续 stop 撞 no-op、无法重试，激光可能卡在高电平。
        self.controller.activate_immediate_running_order(self.ro_488, 488)
        self.assertTrue(self.controller.is_immediate_live_active)
        with mock.patch.object(self.daq, "set_all_low", side_effect=RuntimeError("daq down")):
            with self.assertRaises(HardwareError):
                self.controller.stop_immediate_live()
        # 关光失败 → 状态保留，arming line 不丢。
        self.assertTrue(self.controller.is_immediate_live_active)
        self.assertIsNotNone(self.controller._immediate_live_line)
        # DAQ 恢复后再次 stop：成功拉低并清状态（证明可重试，no-op 不会提前拦截）。
        before = self.daq.set_all_low_calls
        self.controller.stop_immediate_live()
        self.assertFalse(self.controller.is_immediate_live_active)
        self.assertIsNone(self.controller._immediate_live_line)
        self.assertEqual(self.daq.set_all_low_calls, before + 1)

    def test_activate_cleanup_keeps_arming_line_when_low_fails(self):
        # 硬件安全回归（codex blocking B1）：激活中途失败且清理拉低也失败时，保留 arming line，
        # 使后续 stop 能据此重试拉低（不能丢失"激光线可能已写高"的信息）。
        with mock.patch.object(
            self.controller.slm_adapter, "activate_prepared_patterns", side_effect=HardwareError("boom")
        ):
            with mock.patch.object(self.daq, "set_all_low", side_effect=RuntimeError("daq down")):
                with self.assertRaises(HardwareError):
                    self.controller.activate_immediate_running_order(self.ro_488, 488)
        self.assertFalse(self.controller.is_immediate_live_active)
        self.assertIsNotNone(self.controller._immediate_live_line)
        # DAQ 恢复后 stop 据保留的 line 重试拉低并清状态。
        self.controller.stop_immediate_live()
        self.assertIsNone(self.controller._immediate_live_line)

    def test_engaged_reflects_residual_arming_line_after_failed_cleanup(self):
        # codex round-2 blocking 回归：激活失败 + 清理拉低也失败 → active 仍 False，但 engaged
        # 必须为 True（保留 arming line、激光可能仍高），供正式采集互锁 fail-safe 拦截。
        with mock.patch.object(
            self.controller.slm_adapter, "activate_prepared_patterns", side_effect=HardwareError("boom")
        ):
            with mock.patch.object(self.daq, "set_all_low", side_effect=RuntimeError("daq down")):
                with self.assertRaises(HardwareError):
                    self.controller.activate_immediate_running_order(self.ro_488, 488)
        self.assertFalse(self.controller.is_immediate_live_active)
        self.assertTrue(self.controller.is_immediate_live_engaged)
        # DAQ 恢复后 stop → 清状态 → engaged 归 False。
        self.controller.stop_immediate_live()
        self.assertFalse(self.controller.is_immediate_live_engaged)


class ImmediateLiveGuiGuardTests(unittest.TestCase):
    """GUI 方法用 SimpleNamespace 假 self 调用，验证关键激光安全护栏。"""

    @classmethod
    def setUpClass(cls):
        from PyQt5 import QtWidgets

        cls._app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])

    def _import_main(self):
        import types as _types

        sys.modules.setdefault("MCUTriggerThread", _types.ModuleType("MCUTriggerThread"))
        sys.modules.setdefault("mvsdk", _types.ModuleType("mvsdk"))
        sys.modules.setdefault("FastCameraThread", _types.ModuleType("FastCameraThread"))
        control_root = PROJECT_ROOT / "control_wangbo"
        if str(control_root) not in sys.path:
            sys.path.insert(0, str(control_root))
        import importlib

        return importlib.import_module("control_wangbo.main")

    def _make_combo(self):
        from PyQt5 import QtWidgets

        return QtWidgets.QComboBox()

    def test_reset_dropdown_only_none(self):
        main = self._import_main()
        combo = self._make_combo()
        window = SimpleNamespace(ui=SimpleNamespace(cmb_SLM_immediateRO=combo))
        main.MainWindow._reset_immediate_ro_dropdown(window)
        self.assertEqual(combo.count(), 1)
        self.assertEqual(combo.currentText(), "(none)")
        self.assertIsNone(combo.currentData())

    def test_refresh_dropdown_disables_mismatched_wavelength(self):
        main = self._import_main()
        combo = self._make_combo()
        window = SimpleNamespace(
            ui=SimpleNamespace(cmb_SLM_immediateRO=combo),
            sim_app_config=SimpleNamespace(selected_laser_nm=488),
            sim_immediate_ro_items=[
                {
                    "index": 28,
                    "name": "488_3.5_2d_imm_f1",
                    "parsed_wavelength_nm": 488,
                    "activation_type": R11_ACTIVATION_TYPE_HARDWARE,
                    "is_immediate": False,
                },
                {
                    "index": 29,
                    "name": "488_3.5_2d_imm_f2",
                    "parsed_wavelength_nm": 488,
                    "activation_type": R11_ACTIVATION_TYPE_IMMEDIATE,
                    "is_immediate": True,
                },
                {
                    "index": 30,
                    "name": "561_3.5_2d_imm_f1",
                    "parsed_wavelength_nm": 561,
                    "activation_type": R11_ACTIVATION_TYPE_IMMEDIATE,
                    "is_immediate": True,
                },
            ],
        )
        main.MainWindow.refresh_immediate_ro_dropdown(window)
        # (none) + f1 非 immediate 诊断项 + f2 可选 + 561 波长不匹配项。
        self.assertEqual(combo.count(), 4)
        model = combo.model()
        self.assertFalse(model.item(1).isEnabled())
        self.assertIsNone(combo.itemData(1))
        self.assertTrue(model.item(2).isEnabled())
        self.assertEqual(combo.itemData(2), 29)
        self.assertFalse(model.item(3).isEnabled())
        self.assertEqual(combo.currentIndex(), 0)  # 复位到 (none)

    def test_select_immediate_without_camera_does_not_activate(self):
        main = self._import_main()
        combo = self._make_combo()
        combo.addItem("(none)", None)
        combo.addItem("488_3.5_2d_imm_f1", 28)
        combo.setCurrentIndex(1)  # 选了一个真实 immediate RO
        controller = SimpleNamespace(activate_immediate_running_order=mock.Mock())
        window = SimpleNamespace(
            ui=SimpleNamespace(cmb_SLM_immediateRO=combo),
            sim_camera_connected=False,  # 关键：相机未连接
            sim_app_config=SimpleNamespace(selected_laser_nm=488),
            sim_acquisition_controller=controller,
            sim_preview_started_confirmed=False,
            sim_preview_active=False,
            stop_immediate_live_mode=mock.Mock(),
            _cancel_immediate_pending=mock.Mock(),
            _begin_immediate_pending=mock.Mock(),
        )
        with mock.patch.object(main.qw.QMessageBox, "information") as info:
            main.MainWindow.on_immediate_ro_changed(window)
        # 未连相机：不得调用激活（不开激光），并提示用户。
        controller.activate_immediate_running_order.assert_not_called()
        window._begin_immediate_pending.assert_not_called()
        window.stop_immediate_live_mode.assert_called_once()
        info.assert_called_once()

    def _stop_mode_window(self, main, stop_side_effect):
        controller = SimpleNamespace(
            stop_immediate_live=mock.Mock(side_effect=stop_side_effect)
        )
        return SimpleNamespace(
            _cancel_immediate_pending=mock.Mock(),
            sim_preview_planned_restart=True,
            sim_acquisition_controller=controller,
            sim_immediate_live_active=True,
            sim_immediate_active_ro_index=28,
            sim_immediate_active_wavelength=488,
            _reset_immediate_ro_dropdown=mock.Mock(),
        )

    def test_stop_immediate_live_mode_keeps_state_when_controller_fails(self):
        # 激光安全状态机回归（codex blocking B3）：controller 关光失败时，GUI 必须保留状态、
        # 不复位下拉，留待后续入口（preview_stopped/error、采集前互锁）重试关光。
        main = self._import_main()
        window = self._stop_mode_window(main, RuntimeError("daq down"))
        main.MainWindow.stop_immediate_live_mode(window)
        self.assertTrue(window.sim_immediate_live_active)
        self.assertEqual(window.sim_immediate_active_ro_index, 28)
        self.assertEqual(window.sim_immediate_active_wavelength, 488)
        window._reset_immediate_ro_dropdown.assert_not_called()
        # 两阶段 pending（激光未开）仍先被取消。
        window._cancel_immediate_pending.assert_called_once()

    def test_stop_immediate_live_mode_clears_state_on_success(self):
        # 正常路径：关光成功则清状态并复位下拉（保证修复未误伤常规行为）。
        main = self._import_main()
        window = self._stop_mode_window(main, None)
        main.MainWindow.stop_immediate_live_mode(window)
        self.assertFalse(window.sim_immediate_live_active)
        self.assertIsNone(window.sim_immediate_active_ro_index)
        self.assertIsNone(window.sim_immediate_active_wavelength)
        window._reset_immediate_ro_dropdown.assert_called_once()

    def test_stop_immediate_live_mode_can_keep_dropdown_on_success(self):
        # SIM9 采集前只需要关找样品激光，不应清空已扫描出的 immediate RO 列表。
        main = self._import_main()
        window = self._stop_mode_window(main, None)
        main.MainWindow.stop_immediate_live_mode(window, reset_dropdown=False)
        self.assertFalse(window.sim_immediate_live_active)
        self.assertIsNone(window.sim_immediate_active_ro_index)
        self.assertIsNone(window.sim_immediate_active_wavelength)
        window._reset_immediate_ro_dropdown.assert_not_called()

    def test_active_or_pending_consults_controller_residual_state(self):
        # codex round-2 blocking 回归：GUI active/pending 都空，但 controller 仍 engaged
        #（激活失败保留 arming line、激光可能高）时，正式采集互锁必须返回 True（fail-safe 拦截）。
        main = self._import_main()
        window = SimpleNamespace(
            sim_immediate_live_active=False,
            _immediate_pending=None,
            sim_acquisition_controller=SimpleNamespace(is_immediate_live_engaged=True),
        )
        self.assertTrue(main.MainWindow._immediate_live_active_or_pending(window))
        # 对照：controller 未 engaged + GUI 也空 → False（正常空闲不误拦正式采集）。
        window.sim_acquisition_controller = SimpleNamespace(is_immediate_live_engaged=False)
        self.assertFalse(main.MainWindow._immediate_live_active_or_pending(window))


if __name__ == "__main__":
    unittest.main()

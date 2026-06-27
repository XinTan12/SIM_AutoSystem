"""主 GUI Z-Scan 模块（位于 SLM 与 DAQ 之间；SIM Runtime 已移至 SIM Configuration 列顶）的回归测试。

复用 test_main_window_sim_footer 的 stub/host 模式：用一个持有真实 Ui 的 QWidget 宿主，
把待测 MainWindow 方法以 ``legacy_main.MainWindow.<m>(host)`` 直接调用，外部依赖打桩。
覆盖：控件存在/选项、插入顺序（SLM<Z-Scan<Runtime, index 2）、不越界、配置初值/回写往返、
blockSignals 防回环、运行分派（选择焦面 ON→autofocus / OFF→stage_only）、采集互斥、取消、轮询让路。
"""

import importlib
import sys
import types
import unittest
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest import mock

from PyQt5 import QtCore, QtWidgets
from PyQt5.QtCore import pyqtSignal

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTROL_ROOT = PROJECT_ROOT / "control_wangbo"
for _p in (PROJECT_ROOT, CONTROL_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

sys.modules.setdefault("MCUTriggerThread", types.ModuleType("MCUTriggerThread"))
sys.modules.setdefault("mvsdk", types.ModuleType("mvsdk"))
sys.modules.setdefault("FastCameraThread", types.ModuleType("FastCameraThread"))

from control_wangbo.CellSorting_ui import Ui_Single_Cell_Sorting  # noqa: E402
from sim_control.models import AppConfig, ZScanConfig, Z_SCAN_EXPOSURE_PRESETS_MS  # noqa: E402

legacy_main = importlib.import_module("control_wangbo.main")


class _ZScanHost(QtWidgets.QWidget):
    """持有真实 Ui 的宿主；Z-Scan 相关方法委托给 MainWindow，外部依赖打桩。"""

    signal_zscan_status = pyqtSignal(str, dict)

    def __init__(self, app_config=None):
        super().__init__()
        self.ui = Ui_Single_Cell_Sorting()
        self.ui.setupUi(self)
        self.sim_app_config = app_config if app_config is not None else AppConfig()
        self.sim_acquisition_controller = None
        self.sim_acquisition_in_progress = False
        self.sim_preview_active = False
        self.sim_preview_stop_in_progress = False
        self.sim_slm_connected = False
        self.sim_camera_connected = False
        self._loading_configure_settings = False
        self.setting_changed_calls = 0
        # 外部依赖默认 no-op，单测按需覆盖。
        self.ensure_sim_runtime = mock.Mock(name="ensure_sim_runtime")
        self.stop_immediate_live_mode = mock.Mock(name="stop_immediate_live_mode")
        self.apply_connected_sim_camera_config = mock.Mock(name="apply_connected_sim_camera_config")
        self.stop_sim_preview = mock.Mock(name="stop_sim_preview", return_value=True)

    # --- 委托被测方法 ---
    def setup_sim_zscan_module(self):
        legacy_main.MainWindow.setup_sim_zscan_module(self)

    def _init_zscan_module_from_config(self):
        legacy_main.MainWindow._init_zscan_module_from_config(self)

    def on_main_zscan_setting_changed(self, *args):
        self.setting_changed_calls += 1
        legacy_main.MainWindow.on_main_zscan_setting_changed(self, *args)

    def _set_zscan_inputs_enabled(self, enabled):
        legacy_main.MainWindow._set_zscan_inputs_enabled(self, enabled)

    def _run_zscan_blocking(self, *args):
        return legacy_main.MainWindow._run_zscan_blocking(self, *args)

    def on_main_zscan_run_clicked(self):
        legacy_main.MainWindow.on_main_zscan_run_clicked(self)

    def _on_zscan_status(self, event, payload):
        legacy_main.MainWindow._on_zscan_status(self, event, payload)

    def _on_zscan_move_success(self, result):
        legacy_main.MainWindow._on_zscan_move_success(self, result)

    def _on_zscan_move_error(self, message):
        legacy_main.MainWindow._on_zscan_move_error(self, message)

    def _on_zscan_move_finished(self):
        legacy_main.MainWindow._on_zscan_move_finished(self)

    def poll_sim_stage_position(self, force=False):
        legacy_main.MainWindow.poll_sim_stage_position(self, force=force)

    def setup_sim_z_position_widgets(self):
        legacy_main.MainWindow.setup_sim_z_position_widgets(self)


class _FakeSignal:
    def __init__(self):
        self._cbs = []

    def connect(self, cb):
        self._cbs.append(cb)

    def emit(self, *args):
        for cb in list(self._cbs):
            cb(*args)


class _FakeThread:
    def __init__(self, *args, **kwargs):
        self.started = _FakeSignal()
        self.start_called = False

    def start(self):
        self.start_called = True  # 故意不触发 started → 不真正跑 worker，状态转换可确定性断言

    def quit(self):
        pass

    def wait(self, *args):
        pass


class _FakeConnectWorker:
    def __init__(self, fn):
        self._fn = fn
        self.signal_success = _FakeSignal()
        self.signal_error = _FakeSignal()
        self.signal_finished = _FakeSignal()

    def moveToThread(self, thread):
        pass

    def run(self):
        try:
            self.signal_success.emit(self._fn())
        except Exception as exc:  # noqa: BLE001
            self.signal_error.emit(str(exc))
        finally:
            self.signal_finished.emit()


class MainWindowZScanModuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    # ---- UI 结构 ----
    def test_module_controls_exist_with_expected_options(self):
        host = _ZScanHost()
        try:
            host.setup_sim_zscan_module()
            ui = host.ui
            self.assertIsNotNone(getattr(ui, "grp_zscan", None))
            for name in (
                "cmb_main_zscan_direction",
                "spb_main_zscan_step_nm",
                "spb_main_zscan_num_steps",
                "cmb_main_zscan_exposure",
                "chk_main_zscan_enabled",
                "chk_main_zscan_capture",
                "btn_main_zscan_run",
                "lbl_main_zscan_status",
            ):
                self.assertTrue(hasattr(ui, name), name)
            directions = [ui.cmb_main_zscan_direction.itemData(i) for i in range(ui.cmb_main_zscan_direction.count())]
            self.assertEqual(directions, ["positive_z", "negative_z"])
            exposures = [ui.cmb_main_zscan_exposure.itemData(i) for i in range(ui.cmb_main_zscan_exposure.count())]
            self.assertEqual(exposures, [int(ms) for ms in Z_SCAN_EXPOSURE_PRESETS_MS])
            exposure_texts = [ui.cmb_main_zscan_exposure.itemText(i) for i in range(ui.cmb_main_zscan_exposure.count())]
            self.assertEqual(exposure_texts, [str(int(ms)) for ms in Z_SCAN_EXPOSURE_PRESETS_MS])
            self.assertEqual(ui.spb_main_zscan_step_nm.suffix(), "")
            self.assertEqual(ui.chk_main_zscan_capture.text(), "Select Focus Plane")
            self.assertEqual(ui.btn_main_zscan_run.text(), "Test Z-Scan")
            self.assertIn("SIM9 acquisition", ui.chk_main_zscan_enabled.toolTip())
            self.assertIn("Test Z-Scan", ui.chk_main_zscan_capture.toolTip())

            grid = ui.grp_zscan.layout()
            # 「标签在上、控件在下」(参考 SIM Camera Settings)：开关置顶 row0、四参数标签 row1、
            # 控件 row2、选择焦面+测试按钮 row3、状态 row4。
            self.assertEqual(grid.getItemPosition(grid.indexOf(ui.chk_main_zscan_enabled))[0], 0)
            self.assertEqual(grid.getItemPosition(grid.indexOf(ui.cmb_main_zscan_direction))[0], 2)
            self.assertEqual(grid.getItemPosition(grid.indexOf(ui.spb_main_zscan_step_nm))[0], 2)
            self.assertEqual(grid.getItemPosition(grid.indexOf(ui.spb_main_zscan_num_steps))[0], 2)
            self.assertEqual(grid.getItemPosition(grid.indexOf(ui.cmb_main_zscan_exposure))[0], 2)
            self.assertEqual(grid.getItemPosition(grid.indexOf(ui.lbl_main_zscan_status))[0], 4)

            # 每个参数控件正上方一格 (row-1, 同列) 应是其文字标签（label-on-top）。
            for control, expected_text in (
                (ui.cmb_main_zscan_direction, "Direction"),
                (ui.spb_main_zscan_step_nm, "Step (nm)"),
                (ui.spb_main_zscan_num_steps, "Steps"),
                (ui.cmb_main_zscan_exposure, "Exposure (ms)"),
            ):
                row, col, _, _ = grid.getItemPosition(grid.indexOf(control))
                item_above = grid.itemAtPosition(row - 1, col)
                self.assertIsNotNone(item_above)
                label_above = item_above.widget()
                self.assertIsInstance(label_above, QtWidgets.QLabel)
                self.assertEqual(label_above.text(), expected_text)
                self.assertEqual(label_above.font().pointSize(), 10)

            # 控件加宽以适配 12pt 字体（原 54/72 在 12pt 下 < minSizeHint 76，会裁切数字）。
            self.assertEqual(ui.cmb_main_zscan_direction.maximumWidth(), 80)
            self.assertEqual(ui.spb_main_zscan_step_nm.maximumWidth(), 90)
            self.assertEqual(ui.spb_main_zscan_num_steps.maximumWidth(), 80)
            self.assertEqual(ui.cmb_main_zscan_exposure.maximumWidth(), 80)
            self.assertEqual(ui.btn_main_zscan_run.maximumWidth(), 100)
            self.assertEqual(ui.btn_main_zscan_run.font().pointSize(), 10)
            # 控件字体 12pt（与 SIM Camera Settings 数值控件一致）
            self.assertEqual(ui.cmb_main_zscan_direction.font().pointSize(), 12)
            self.assertEqual(ui.spb_main_zscan_step_nm.font().pointSize(), 12)
        finally:
            host.close()

    def test_module_order_runtime_first_then_slm_zscan(self):
        host = _ZScanHost()
        try:
            host.setup_sim_zscan_module()
            layout = host.ui.verticalLayout_simConfiguration
            idx_slm = layout.indexOf(host.ui.grp_hardvareConnection_SLM)
            idx_zscan = layout.indexOf(host.ui.grp_zscan)
            idx_runtime = layout.indexOf(host.ui.grp_simRuntime)
            # SIM Runtime 已移到 SIM Configuration 列最顶部（index 0）；其后 SLM→Z-Scan→DAQ→Recon。
            self.assertEqual(idx_runtime, 0)
            self.assertEqual(idx_zscan, 2)
            self.assertLess(idx_runtime, idx_slm)
            self.assertLess(idx_slm, idx_zscan)
        finally:
            host.close()

    def test_module_idempotent(self):
        host = _ZScanHost()
        try:
            host.setup_sim_zscan_module()
            first = host.ui.grp_zscan
            host.setup_sim_zscan_module()  # 二次调用应直接返回，不重建
            self.assertIs(host.ui.grp_zscan, first)
        finally:
            host.close()

    def test_module_fits_panel_width(self):
        host = _ZScanHost()
        try:
            host.setup_sim_zscan_module()
            panel_w = host.ui.layoutWidget_simConfiguration.width()
            self.assertGreaterEqual(panel_w, 280)
            self.assertLessEqual(host.ui.grp_zscan.minimumSizeHint().width(), panel_w)
        finally:
            host.close()

    # ---- 配置初值 / 回写 ----
    def test_init_fills_controls_from_config(self):
        cfg = AppConfig(z_scan=ZScanConfig(
            direction="negative_z", step_um=0.3, num_steps=7, exposure_preset_ms=14, enabled=False,
        ))
        host = _ZScanHost(app_config=cfg)
        try:
            host.setup_sim_zscan_module()  # 末尾调 _init_zscan_module_from_config
            ui = host.ui
            self.assertEqual(ui.cmb_main_zscan_direction.currentData(), "negative_z")
            self.assertAlmostEqual(ui.spb_main_zscan_step_nm.value(), 300.0)
            self.assertEqual(ui.spb_main_zscan_num_steps.value(), 7)
            self.assertEqual(ui.cmb_main_zscan_exposure.currentData(), 14)
            self.assertFalse(ui.chk_main_zscan_enabled.isChecked())
        finally:
            host.close()

    def test_blocksignals_during_init_does_not_trigger_writeback(self):
        host = _ZScanHost()
        try:
            host.setup_sim_zscan_module()
            host.setting_changed_calls = 0
            host.sim_app_config = AppConfig(z_scan=ZScanConfig(
                direction="negative_z", step_um=0.9, num_steps=3, exposure_preset_ms=20, enabled=False,
            ))
            host._init_zscan_module_from_config()
            self.assertEqual(host.setting_changed_calls, 0)
            # 控件确已被填充为新值（证明 _init 真改了控件，只是没触发回写）。
            self.assertEqual(host.ui.spb_main_zscan_num_steps.value(), 3)
        finally:
            host.close()

    def test_setting_change_writes_back_to_config_and_saves(self):
        host = _ZScanHost()
        try:
            host.setup_sim_zscan_module()
            with mock.patch.object(legacy_main, "save_app_config") as save_mock:
                host.ui.cmb_main_zscan_direction.setCurrentIndex(
                    host.ui.cmb_main_zscan_direction.findData("negative_z")
                )
                host.ui.spb_main_zscan_step_nm.setValue(500.0)
                host.ui.spb_main_zscan_num_steps.setValue(4)
                host.ui.cmb_main_zscan_exposure.setCurrentIndex(
                    host.ui.cmb_main_zscan_exposure.findData(20)
                )
                host.ui.chk_main_zscan_enabled.setChecked(True)
            cfg = host.sim_app_config.z_scan
            self.assertEqual(cfg.direction, "negative_z")
            self.assertAlmostEqual(cfg.step_um, 0.5)
            self.assertEqual(cfg.num_steps, 4)
            self.assertEqual(cfg.exposure_preset_ms, 20)
            self.assertTrue(cfg.enabled)
            self.assertTrue(save_mock.called)
        finally:
            host.close()

    def test_setting_change_suppressed_while_loading(self):
        host = _ZScanHost()
        try:
            host.setup_sim_zscan_module()
            host._loading_configure_settings = True
            with mock.patch.object(legacy_main, "save_app_config") as save_mock:
                host.ui.spb_main_zscan_num_steps.setValue(9)
                self.assertFalse(save_mock.called)  # 加载期不回写
        finally:
            host.close()

    # ---- 运行分派（无 Qt 依赖的薄方法）----
    def test_run_blocking_dispatches_stage_only_when_capture_off(self):
        host = _ZScanHost()
        try:
            host.setup_sim_zscan_module()
            stage = SimpleNamespace(is_connected=True)
            cfg = host.sim_app_config.z_scan
            stop_event = Event()
            status = mock.Mock()
            with mock.patch.object(legacy_main, "run_z_scan_stage_only", return_value="R") as fn_mock, \
                    mock.patch.object(legacy_main, "run_z_scan_autofocus") as auto_mock:
                result = host._run_zscan_blocking(False, stage, cfg, stop_event, status)
            self.assertEqual(result, "R")
            auto_mock.assert_not_called()
            fn_mock.assert_called_once()
            kwargs = fn_mock.call_args.kwargs
            self.assertIs(kwargs["stage_adapter"], stage)
            self.assertIs(kwargs["z_scan_config"], cfg)
            self.assertIs(kwargs["stop_event"], stop_event)
            self.assertIs(kwargs["on_status"], status)
        finally:
            host.close()

    def test_run_blocking_dispatches_autofocus_when_capture_on(self):
        host = _ZScanHost()
        try:
            host.setup_sim_zscan_module()
            cam, slm, daq = object(), object(), object()
            host.sim_acquisition_controller = SimpleNamespace(
                camera_adapter=cam, slm_adapter=slm, daq_adapter=daq,
            )
            stage = SimpleNamespace(is_connected=True)
            cfg = host.sim_app_config.z_scan
            stop_event = Event()
            status = mock.Mock()
            with mock.patch.object(legacy_main, "run_z_scan_autofocus", return_value="A") as auto_mock, \
                    mock.patch.object(legacy_main, "run_z_scan_stage_only") as fn_mock:
                result = host._run_zscan_blocking(True, stage, cfg, stop_event, status)
            self.assertEqual(result, "A")
            fn_mock.assert_not_called()
            kwargs = auto_mock.call_args.kwargs
            self.assertIs(kwargs["stage_adapter"], stage)
            self.assertIs(kwargs["camera_adapter"], cam)
            self.assertIs(kwargs["slm_adapter"], slm)
            self.assertIs(kwargs["daq_adapter"], daq)
            self.assertIs(kwargs["daq_config"], host.sim_app_config.daq)
            self.assertIs(kwargs["camera_config"], host.sim_app_config.camera)
            self.assertIs(kwargs["timing"], host.sim_app_config.timing)
            self.assertFalse(kwargs["keep_captured_stack"])
        finally:
            host.close()

    # ---- 运行按钮：互斥 / 取消 / 进入运行态 ----
    def test_run_click_blocked_during_acquisition(self):
        host = _ZScanHost()
        try:
            host.setup_sim_zscan_module()
            host.sim_acquisition_in_progress = True
            with mock.patch.object(legacy_main.qw.QMessageBox, "information") as info_mock:
                host.on_main_zscan_run_clicked()
            self.assertTrue(info_mock.called)
            self.assertFalse(getattr(host, "_zscan_move_in_progress"))
            self.assertIsNone(getattr(host, "_zscan_move_thread"))
        finally:
            host.close()

    def test_run_click_cancels_active_worker(self):
        host = _ZScanHost()
        try:
            host.setup_sim_zscan_module()
            host._zscan_move_thread = object()
            stop_event = Event()
            host._zscan_move_stop_event = stop_event
            host.on_main_zscan_run_clicked()
            self.assertTrue(stop_event.is_set())
            self.assertFalse(host.ui.btn_main_zscan_run.isEnabled())
        finally:
            host.close()

    def test_run_click_enters_running_state_and_starts_thread(self):
        host = _ZScanHost()
        try:
            host.setup_sim_zscan_module()
            host.sim_acquisition_controller = SimpleNamespace(
                stage_adapter=SimpleNamespace(is_connected=True),
                connect_stage=mock.Mock(),
            )
            with mock.patch.object(legacy_main, "QThread", _FakeThread), \
                    mock.patch.object(legacy_main, "_ConnectWorker", _FakeConnectWorker):
                host.on_main_zscan_run_clicked()
            self.assertTrue(host._zscan_move_in_progress)
            self.assertEqual(host.ui.btn_main_zscan_run.text(), "Cancel")
            self.assertFalse(host.ui.cmb_main_zscan_direction.isEnabled())
            self.assertTrue(host._zscan_move_thread.start_called)
        finally:
            host.close()

    def test_finished_slot_resets_state(self):
        host = _ZScanHost()
        try:
            host.setup_sim_zscan_module()
            host._zscan_move_thread = _FakeThread()
            host._zscan_move_worker = object()
            host._zscan_move_stop_event = Event()
            host._zscan_move_in_progress = True
            host.sim_acquisition_controller = SimpleNamespace(
                get_stage_position_um=mock.Mock(return_value=3.0),
            )
            host.setup_sim_z_position_widgets()
            host._on_zscan_move_finished()
            self.assertFalse(host._zscan_move_in_progress)
            self.assertIsNone(host._zscan_move_thread)
            self.assertTrue(host.ui.btn_main_zscan_run.isEnabled())
            self.assertEqual(host.ui.btn_main_zscan_run.text(), "Test Z-Scan")
            self.assertTrue(host.ui.cmb_main_zscan_direction.isEnabled())
        finally:
            host.close()

    # ---- 轮询让路 ----
    def test_poll_skips_during_zscan_move_unless_forced(self):
        host = _ZScanHost()
        try:
            host.setup_sim_zscan_module()
            host.setup_sim_z_position_widgets()
            host.sim_acquisition_controller = SimpleNamespace(
                get_stage_position_um=mock.Mock(return_value=5.0),
            )
            host._zscan_move_in_progress = True
            host.ui.lbl_z_position_value.setText("-- um")
            host.poll_sim_stage_position(force=False)
            self.assertEqual(host.ui.lbl_z_position_value.text(), "-- um")
            host.poll_sim_stage_position(force=True)
            self.assertEqual(host.ui.lbl_z_position_value.text(), "5.00 um")
        finally:
            host.close()


if __name__ == "__main__":
    unittest.main()

"""主 GUI DAQ 模块（位于 Z-Scan 与 Recon 之间）的回归测试。

复用 ``test_main_window_zscan_module`` 的 stub/host 模式：持有真实 Ui 的 QWidget 宿主，把待测
``MainWindow`` 方法以 ``legacy_main.MainWindow.<m>(host)`` 直接调用，外部依赖打桩。

覆盖：控件存在、8 路线位下拉从配置初始化、测试目标下拉（5 脉冲 + SIM采集 + SLM激活时序）、
插入顺序（Save Path < SLM < Z-Scan < DAQ < Recon）、幂等、配置改值往返（写回 + 落盘 + 同步
controller + 刷新摘要）、DAQ 测试按钮的 B1 安全互锁（关 immediate-live + 暂停 preview + 结束恢复）、
与 SIM9 采集互斥。注意：用 ``_PulseTestWorker`` / ``QThread`` 的 fake 替身，断言状态转换而不真正跑硬件。
"""

import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from PyQt5 import QtWidgets

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTROL_ROOT = PROJECT_ROOT / "control_wangbo"
for _p in (PROJECT_ROOT, CONTROL_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

sys.modules.setdefault("MCUTriggerThread", types.ModuleType("MCUTriggerThread"))
sys.modules.setdefault("mvsdk", types.ModuleType("mvsdk"))
sys.modules.setdefault("FastCameraThread", types.ModuleType("FastCameraThread"))

from control_wangbo.CellSorting_ui import Ui_Single_Cell_Sorting  # noqa: E402
from sim_control.models import AppConfig  # noqa: E402

legacy_main = importlib.import_module("control_wangbo.main")


class _FakeSignal:
    def __init__(self):
        self._cbs = []

    def connect(self, cb):
        self._cbs.append(cb)

    def emit(self, *args):
        for cb in list(self._cbs):
            cb(*args)


class _FakeThread:
    """不真正起线程：start() 不触发 started，故 worker.run 不执行，状态转换可确定性断言。"""

    def __init__(self, *args, **kwargs):
        self.started = _FakeSignal()
        self.start_called = False

    def start(self):
        self.start_called = True

    def quit(self):
        pass

    def wait(self, *args):
        pass


class _FakePulseWorker:
    def __init__(self, fn, stop_event, parent=None):
        self._fn = fn
        self._stop_event = stop_event
        self.cancel_called = False
        self.signal_success = _FakeSignal()
        self.signal_error = _FakeSignal()
        self.signal_finished = _FakeSignal()

    def cancel(self):
        self.cancel_called = True
        self._stop_event.set()

    def moveToThread(self, thread):
        pass

    def run(self):
        try:
            self.signal_success.emit(self._fn(self._stop_event))
        except Exception as exc:  # noqa: BLE001
            self.signal_error.emit(str(exc))
        finally:
            self.signal_finished.emit()


class _DaqHost(QtWidgets.QWidget):
    """持有真实 Ui 的宿主；DAQ 模块方法委托给 MainWindow，外部依赖打桩。"""

    def __init__(self, app_config=None):
        super().__init__()
        self.ui = Ui_Single_Cell_Sorting()
        self.ui.setupUi(self)
        self.sim_app_config = app_config if app_config is not None else AppConfig()
        # 测试隔离：配置落盘指向临时文件，避免改值触发的 save_app_config 写到真实 config。
        self.sim_app_config.config_path = str(Path(tempfile.gettempdir()) / "sim_daq_module_test_config.json")
        self.sim_acquisition_controller = None
        self.sim_acquisition_in_progress = False
        self.sim_preview_active = False
        self.sim_preview_stop_in_progress = False
        self.sim_preview_requested = False
        self.sim_camera_connected = False
        self._loading_configure_settings = False
        self.ensure_sim_runtime = mock.Mock(name="ensure_sim_runtime")
        self.stop_immediate_live_mode = mock.Mock(name="stop_immediate_live_mode")
        self.stop_sim_preview = mock.Mock(name="stop_sim_preview", return_value=True)
        self.start_sim_preview = mock.Mock(name="start_sim_preview")
        self.refresh_sim_settings_summary = mock.Mock(name="refresh_sim_settings_summary")

    # --- 委托被测方法 ---
    def setup_sim_daq_module(self):
        legacy_main.MainWindow.setup_sim_daq_module(self)

    def _init_daq_module_from_config(self):
        legacy_main.MainWindow._init_daq_module_from_config(self)

    def _refresh_main_daq_test_targets(self):
        legacy_main.MainWindow._refresh_main_daq_test_targets(self)

    def _daq_test_role_for_wavelength(self, wavelength_nm):
        return legacy_main.MainWindow._daq_test_role_for_wavelength(self, wavelength_nm)

    def _select_daq_test_target_for_wavelength(self):
        legacy_main.MainWindow._select_daq_test_target_for_wavelength(self)

    def on_main_daq_setting_changed(self, *args):
        legacy_main.MainWindow.on_main_daq_setting_changed(self, *args)

    def _persist_main_daq_config_from_ui(self):
        legacy_main.MainWindow._persist_main_daq_config_from_ui(self)

    def _set_main_daq_inputs_enabled(self, enabled):
        legacy_main.MainWindow._set_main_daq_inputs_enabled(self, enabled)

    def _apply_sim_red_laser_options(self, red_laser_nm):
        # Load 回填会先按机器红光重建"按红光"的 UI 选项（纯 UI、全程 blockSignals）。
        legacy_main.MainWindow._apply_sim_red_laser_options(self, red_laser_nm)

    def on_main_daq_test_clicked(self):
        legacy_main.MainWindow.on_main_daq_test_clicked(self)

    def _on_main_daq_test_success(self, message):
        legacy_main.MainWindow._on_main_daq_test_success(self, message)

    def _on_main_daq_test_error(self, message):
        legacy_main.MainWindow._on_main_daq_test_error(self, message)

    def _on_main_daq_test_finished(self):
        legacy_main.MainWindow._on_main_daq_test_finished(self)


def _fake_controller():
    """提供 DAQ 测试需要的共享 adapter + immediate RO 索引。"""
    return types.SimpleNamespace(
        camera_adapter=mock.Mock(name="camera_adapter"),
        slm_adapter=mock.Mock(name="slm_adapter"),
        daq_adapter=mock.Mock(name="daq_adapter"),
        _immediate_ro_indices=set(),
        apply_daq_config=mock.Mock(name="apply_daq_config"),
    )


class MainWindowDaqModuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    # ---- UI 结构 ----
    def test_module_controls_exist(self):
        host = _DaqHost()
        try:
            host.setup_sim_daq_module()
            ui = host.ui
            self.assertIsNotNone(getattr(ui, "grp_daq", None))
            for name in (
                "cmb_main_daq_device", "btn_main_daq_refresh",
                "cmb_main_daq_slm_enable", "cmb_main_daq_slm_trigger", "cmb_main_daq_slm_finish",
                "cmb_main_daq_camera_trigger", "cmb_main_daq_laser_405", "cmb_main_daq_laser_488",
                "cmb_main_daq_laser_561", "cmb_main_daq_laser_red",
                # 红光机器切换下拉（638/647）+ 标签，移入 row 0 设备行 hbox_daq_device。
                "cmb_main_red_laser", "lbl_main_red_laser",
                "cmb_main_daq_test_target", "btn_main_daq_test", "lbl_main_daq_status",
            ):
                self.assertTrue(hasattr(ui, name), name)
            self.assertTrue(host._daq_module_wired)
        finally:
            host.close()

    def test_line_combos_populated_from_config(self):
        host = _DaqHost()
        try:
            host.setup_sim_daq_module()
            ui = host.ui
            daq = host.sim_app_config.daq
            self.assertEqual(ui.cmb_main_daq_slm_enable.currentText(), daq.slm_enable_line)
            self.assertEqual(ui.cmb_main_daq_slm_trigger.currentText(), daq.slm_trigger_line)
            self.assertEqual(ui.cmb_main_daq_slm_finish.currentText(), daq.slm_finish_line)
            self.assertEqual(ui.cmb_main_daq_camera_trigger.currentText(), daq.camera_trigger_line)
            self.assertEqual(ui.cmb_main_daq_laser_405.currentText(), daq.laser_405_line)
            self.assertEqual(ui.cmb_main_daq_laser_red.currentText(), daq.laser_red_line)
            self.assertEqual(ui.cmb_main_daq_device.currentText(), daq.device_name)
        finally:
            host.close()

    def test_test_targets_built_with_expected_order(self):
        host = _DaqHost()
        try:
            host.setup_sim_daq_module()
            combo = host.ui.cmb_main_daq_test_target
            ids = [combo.itemData(i) for i in range(combo.count())]
            # "Camera Trigger + Capture" 项已删除；只剩 4 路激光 + SIM采集 + SLM激活时序。
            self.assertEqual(
                ids,
                ["laser_405_line", "laser_488_line", "laser_561_line",
                 "laser_red_line", "sim_acquisition", "slm_activation_timing"],
            )
            # 默认选中项跟随当前采集波长（_DaqHost 默认 selected_laser_nm=488 → laser_488_line）。
            self.assertEqual(combo.currentData(), "laser_488_line")
        finally:
            host.close()

    def test_default_target_follows_acquisition_wavelength(self):
        """DAQ Test 下拉默认项=当前采集波长；切波长调 _select 后强制跟随（638/647→laser_red_line）。"""
        host = _DaqHost()
        try:
            host.sim_app_config.selected_laser_nm = 561
            host.setup_sim_daq_module()
            combo = host.ui.cmb_main_daq_test_target
            self.assertEqual(combo.currentData(), "laser_561_line")
            # 切到本机红光波长，调联动方法后下拉跟随到红光激光项。
            host.sim_app_config.selected_laser_nm = host.sim_app_config.red_laser_nm
            host._select_daq_test_target_for_wavelength()
            self.assertEqual(combo.currentData(), "laser_red_line")
        finally:
            host.close()

    def test_layout_widths_fonts_columnstretch_and_geometry(self):
        """布局微调：DAQ 8 通道改 label-on-top（标签在上/下拉在下、2 列）+ 删 Device 标签 + SIM 列收窄。"""
        host = _DaqHost()
        try:
            host.setup_sim_daq_module()
            ui = host.ui
            # 8 线位下拉 minWidth 120（容纳完整 "Dev1/port0/line12"）
            for name in (
                "cmb_main_daq_slm_enable", "cmb_main_daq_slm_trigger", "cmb_main_daq_slm_finish",
                "cmb_main_daq_camera_trigger", "cmb_main_daq_laser_405", "cmb_main_daq_laser_488",
                "cmb_main_daq_laser_561", "cmb_main_daq_laser_red",
            ):
                self.assertEqual(getattr(ui, name).minimumWidth(), 120, name)
            # 字体：标签 10pt / 控件 12pt / 按钮 10pt（与 SIM Camera Settings 一致）
            self.assertEqual(ui.lbl_main_daq_slm_enable.font().pointSize(), 10)
            self.assertEqual(ui.cmb_main_daq_slm_enable.font().pointSize(), 12)
            self.assertEqual(ui.btn_main_daq_refresh.font().pointSize(), 10)
            # 2 列 label-on-top：两通道列（左 SLM/Cam-Trig、右 Laser）等分余量、宽度一致
            grid = ui.gridLayout_daq
            self.assertEqual(grid.columnCount(), 2)
            self.assertEqual([grid.columnStretch(i) for i in range(2)], [1, 1])
            # label-on-top（用 itemAtPosition 锁布局）：每个通道下拉正上方一格(row-1, 同列)是其文字标签且 10pt
            for cmb_name, lbl_name in (
                ("cmb_main_daq_slm_enable", "lbl_main_daq_slm_enable"),
                ("cmb_main_daq_laser_405", "lbl_main_daq_laser_405"),
                ("cmb_main_daq_slm_trigger", "lbl_main_daq_slm_trigger"),
                ("cmb_main_daq_laser_488", "lbl_main_daq_laser_488"),
                ("cmb_main_daq_slm_finish", "lbl_main_daq_slm_finish"),
                ("cmb_main_daq_laser_561", "lbl_main_daq_laser_561"),
                ("cmb_main_daq_camera_trigger", "lbl_main_daq_camera_trigger"),
                ("cmb_main_daq_laser_red", "lbl_main_daq_laser_red"),
            ):
                combo = getattr(ui, cmb_name)
                row, col, _, _ = grid.getItemPosition(grid.indexOf(combo))
                item_above = grid.itemAtPosition(row - 1, col)
                self.assertIsNotNone(item_above, cmb_name)
                self.assertIs(item_above.widget(), getattr(ui, lbl_name), cmb_name)
                self.assertEqual(item_above.widget().font().pointSize(), 10)
            # 第一行 "Device" 标签已删除（Device 下拉 + Refresh 仍在跨 2 列的 HBox 内）
            self.assertFalse(hasattr(ui, "lbl_main_daq_device"))
            # Device 行是跨 2 列的嵌套 HBox（设备下拉+Refresh+红光标签/下拉+spacer），防回退为平铺
            self.assertIs(grid.itemAtPosition(0, 0).layout(), ui.hbox_daq_device)
            # 红光机器切换控件（638/647）已并入 row 0 的 hbox_daq_device；test/status 回到 row 9/10。
            self.assertGreaterEqual(ui.hbox_daq_device.indexOf(ui.cmb_main_red_laser), 0)
            self.assertGreaterEqual(ui.hbox_daq_device.indexOf(ui.lbl_main_red_laser), 0)
            self.assertIs(grid.itemAtPosition(9, 0).layout(), ui.hbox_daq_test)
            self.assertIs(grid.itemAtPosition(10, 0).widget(), ui.lbl_main_daq_status)
            # DAQ Test 行：删 "Test" 标签；下拉固定 235、按钮 "Test" 固定 52（同一行完整显示）。
            self.assertFalse(hasattr(ui, "lbl_main_daq_test"))
            self.assertEqual(ui.cmb_main_daq_test_target.maximumWidth(), 235)
            self.assertEqual(ui.btn_main_daq_test.maximumWidth(), 52)
            # 设备行收窄：Refresh 改 "↻" 固定 36、Red(nm) 标签 maxWidth 60、device/Red 下拉微缩。
            self.assertEqual(ui.btn_main_daq_refresh.text(), "↻")
            self.assertEqual(ui.btn_main_daq_refresh.maximumWidth(), 36)
            self.assertEqual(ui.lbl_main_red_laser.maximumWidth(), 60)
            self.assertEqual(ui.cmb_main_daq_device.maximumWidth(), 95)
            self.assertEqual(ui.cmb_main_red_laser.maximumWidth(), 78)
            # 几何：保留 Save Path，但 SIM Configuration 列宽恢复到本轮前的窄面板尺寸。
            self.assertEqual(ui.grp_simConfiguration.geometry().width(), 314)
            self.assertEqual(ui.layoutWidget_simConfiguration.geometry().width(), 302)
            self.assertEqual(ui.grp_simSummary.geometry().x(), 809)
        finally:
            host.close()

    def test_module_inserted_between_zscan_and_recon(self):
        host = _DaqHost()
        try:
            host.setup_sim_daq_module()
            layout = host.ui.verticalLayout_simConfiguration
            idx_zscan = layout.indexOf(host.ui.grp_zscan)
            idx_daq = layout.indexOf(host.ui.grp_daq)
            idx_recon = layout.indexOf(host.ui.grp_recon)
            idx_save_path = layout.indexOf(host.ui.grp_savePath)
            self.assertFalse(hasattr(host.ui, "grp_simRuntime"))
            self.assertEqual(idx_save_path, 0)
            self.assertLess(idx_zscan, idx_daq)
            self.assertLess(idx_daq, idx_recon)
            self.assertEqual(idx_daq, 3)
        finally:
            host.close()

    def test_module_idempotent(self):
        host = _DaqHost()
        try:
            host.setup_sim_daq_module()
            first = host.ui.grp_daq
            host.setup_sim_daq_module()
            self.assertIs(host.ui.grp_daq, first)
        finally:
            host.close()

    # ---- 配置回写 ----
    def test_setting_change_writes_back_to_config_and_syncs(self):
        host = _DaqHost()
        host.sim_acquisition_controller = _fake_controller()
        try:
            host.setup_sim_daq_module()
            # 给某线位下拉新增并切换到一个新值，触发 currentTextChanged → on_main_daq_setting_changed
            combo = host.ui.cmb_main_daq_slm_enable
            combo.addItem("Dev1/port0/line7")
            with mock.patch.object(legacy_main, "save_app_config") as save_cfg:
                combo.setCurrentText("Dev1/port0/line7")
            self.assertEqual(host.sim_app_config.daq.slm_enable_line, "Dev1/port0/line7")
            save_cfg.assert_called()
            host.sim_acquisition_controller.apply_daq_config.assert_called_with(host.sim_app_config.daq)
            host.refresh_sim_settings_summary.assert_called()
        finally:
            host.close()

    # ---- DAQ 测试按钮：B1 安全互锁 ----
    def test_test_click_interlock_stops_immediate_and_preview_then_resumes(self):
        host = _DaqHost()
        host.sim_acquisition_controller = _fake_controller()
        host.sim_camera_connected = True
        host.sim_preview_active = True
        try:
            host.setup_sim_daq_module()
            with mock.patch.object(legacy_main, "_PulseTestWorker", _FakePulseWorker), \
                 mock.patch.object(legacy_main, "QThread", _FakeThread):
                host.on_main_daq_test_clicked()
            # B1：关找样品激光（不重置下拉）+ 暂停 preview，记 resume 标志，按钮转 Cancel。
            host.stop_immediate_live_mode.assert_called_with(reset_dropdown=False)
            host.stop_sim_preview.assert_called_with(wait=True)
            self.assertTrue(host._daq_test_resume_preview)
            self.assertEqual(host.ui.btn_main_daq_test.text(), "Cancel")
            self.assertIsNotNone(host._daq_test_thread)
            # 结束：恢复 preview + 按钮文本 + 清线程引用。
            host._on_main_daq_test_finished()
            host.start_sim_preview.assert_called()
            self.assertIsNone(host._daq_test_thread)
            self.assertEqual(host.ui.btn_main_daq_test.text(), "Test")
        finally:
            host.close()

    def test_test_click_rejected_during_acquisition(self):
        host = _DaqHost()
        host.sim_acquisition_in_progress = True
        try:
            host.setup_sim_daq_module()
            with mock.patch.object(legacy_main.qw, "QMessageBox") as msgbox, \
                 mock.patch.object(legacy_main, "_PulseTestWorker", _FakePulseWorker), \
                 mock.patch.object(legacy_main, "QThread", _FakeThread):
                host.on_main_daq_test_clicked()
            msgbox.information.assert_called()
            self.assertIsNone(getattr(host, "_daq_test_thread", None))
            host.stop_immediate_live_mode.assert_not_called()
        finally:
            host.close()

    # ---- B3：Load Config 回填 DAQ/Recon 并同步 controller/worker ----
    def test_load_payload_refills_daq_and_syncs_controller(self):
        from sim_control.models import AppConfig as _AC, DaqLineConfig as _DLC

        host = _DaqHost()
        host.sim_acquisition_controller = _fake_controller()
        host.sync_sim_camera_controls_from_config = mock.Mock(name="sync_cam_controls")
        host.ensure_sim_reconstruction_worker = mock.Mock(name="ensure_recon_worker")
        host.refresh_sim_settings_summary = mock.Mock(name="refresh_summary")
        # apply_loaded_sim_settings_payload 还会回填 recon/zscan 控件——绑定其委托（不需提前 setup）。
        host._init_recon_module_from_config = lambda: legacy_main.MainWindow._init_recon_module_from_config(host)
        host._init_zscan_module_from_config = lambda: legacy_main.MainWindow._init_zscan_module_from_config(host)
        try:
            host.setup_sim_daq_module()
            # 构造一份 DAQ 线位不同（slm_enable line0→line3）的待加载配置。
            loaded = _AC()
            loaded.daq = _DLC(
                device_name="Dev1",
                slm_enable_line="Dev1/port0/line3",
                slm_trigger_line="Dev1/port0/line1",
                slm_finish_line="Dev1/port0/line2",
                camera_trigger_line="Dev1/port0/line8",
                laser_405_line="Dev1/port0/line9",
                laser_488_line="Dev1/port0/line10",
                laser_561_line="Dev1/port0/line11",
                laser_red_line="Dev1/port0/line12",
            )
            payload = {"sim_control": legacy_main.app_config_to_dict(loaded)}
            with mock.patch.object(legacy_main, "save_app_config"):
                legacy_main.MainWindow.apply_loaded_sim_settings_payload(host, payload)
            # DAQ 控件回填到加载值。
            self.assertEqual(host.ui.cmb_main_daq_slm_enable.currentText(), "Dev1/port0/line3")
            # controller DAQ 同步 + 常驻 recon worker 同步（B6 经 ensure_worker）。
            host.sim_acquisition_controller.apply_daq_config.assert_called_with(host.sim_app_config.daq)
            host.ensure_sim_reconstruction_worker.assert_called()
        finally:
            host.close()

    def test_second_click_cancels_running_test(self):
        host = _DaqHost()
        host.sim_acquisition_controller = _fake_controller()
        host.sim_camera_connected = True
        try:
            host.setup_sim_daq_module()
            with mock.patch.object(legacy_main, "_PulseTestWorker", _FakePulseWorker), \
                 mock.patch.object(legacy_main, "QThread", _FakeThread):
                host.on_main_daq_test_clicked()
                worker = host._daq_test_worker
                self.assertIsNotNone(worker)
                host.on_main_daq_test_clicked()  # 第二次点击 = 取消
            self.assertTrue(worker.cancel_called)
        finally:
            host.close()


if __name__ == "__main__":
    unittest.main()

"""主 GUI Recon 模块（位于 DAQ 之后、列末 spacer 之前；SIM Runtime 已移至 SIM Configuration 列顶）的回归测试。

复用 ``test_main_window_zscan_module`` 的 stub/host 模式。覆盖（含从已删 ``SimSettingsDialog``
迁来的 recon 行为）：控件存在、波长下拉 405/488/561/638、按当前采集波长初始化参数 + OTF、
按波长保存/加载 OTF（不改采集波长 ``selected_laser_nm``）、保留初始非默认 OTF、改值写回
``ReconstructionConfig`` + 落盘 + B6 经 ensure_worker 下发 + 刷新摘要、reader 保留 638 OTF 与
saved-params、以及 B6 硬约束（``ensure_sim_reconstruction_worker`` 经 queued signal→slot 下发
快照，**不**跨线程裸 ``set_reconstruction_config``）。
"""

import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from PyQt5 import QtWidgets
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
from sim_control.models import AppConfig, ReconstructionConfig  # noqa: E402

legacy_main = importlib.import_module("control_wangbo.main")


class _FakeThread:
    def __init__(self, *args, **kwargs):
        self.start_called = False

    def start(self):
        self.start_called = True


class _ReconHost(QtWidgets.QWidget):
    """持有真实 Ui 的宿主；Recon 模块方法委托给 MainWindow，外部依赖打桩。"""

    signal_reconstruction_config_changed = pyqtSignal(object)

    def __init__(self, app_config=None):
        super().__init__()
        self.ui = Ui_Single_Cell_Sorting()
        self.ui.setupUi(self)
        self.sim_app_config = app_config if app_config is not None else AppConfig()
        # 测试隔离：配置落盘指向临时文件，避免改值触发的 save_app_config 写到真实 config。
        self.sim_app_config.config_path = str(Path(tempfile.gettempdir()) / "sim_recon_module_test_config.json")
        self._loading_configure_settings = False
        self.sim_recon_worker = None
        self.sim_recon_thread = None
        self.ensure_sim_reconstruction_worker = mock.Mock(name="ensure_sim_reconstruction_worker")
        self.refresh_sim_settings_summary = mock.Mock(name="refresh_sim_settings_summary")
        self.slot_handle_sim_reconstruction_ready = mock.Mock(name="recon_ready")
        self.slot_handle_sim_reconstruction_failed = mock.Mock(name="recon_failed")

    # --- 委托被测方法 ---
    def setup_sim_recon_module(self):
        legacy_main.MainWindow.setup_sim_recon_module(self)

    def _init_recon_module_from_config(self):
        legacy_main.MainWindow._init_recon_module_from_config(self)

    def _store_main_recon_otf_path(self):
        legacy_main.MainWindow._store_main_recon_otf_path(self)

    def _on_main_recon_wavelength_changed(self, *args):
        legacy_main.MainWindow._on_main_recon_wavelength_changed(self, *args)

    def _persist_main_recon_config_from_ui(self):
        legacy_main.MainWindow._persist_main_recon_config_from_ui(self)

    def on_main_recon_setting_changed(self, *args):
        legacy_main.MainWindow.on_main_recon_setting_changed(self, *args)


class MainWindowReconModuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_module_controls_exist(self):
        host = _ReconHost()
        try:
            host.setup_sim_recon_module()
            for name in (
                "spb_main_recon_wiener", "spb_main_recon_na", "spb_main_recon_pixel_nm",
                "cmb_main_recon_wavelength", "edit_main_recon_otf", "btn_main_recon_browse_otf",
                "edit_main_recon_background", "btn_main_recon_browse_background",
                "edit_main_recon_output", "btn_main_recon_browse_output",
            ):
                self.assertTrue(hasattr(host.ui, name), name)
            self.assertTrue(host._recon_module_wired)
        finally:
            host.close()

    def test_wavelength_combo_items(self):
        host = _ReconHost()
        try:
            host.setup_sim_recon_module()
            combo = host.ui.cmb_main_recon_wavelength
            self.assertEqual([combo.itemData(i) for i in range(combo.count())], [405, 488, 561, 638])
        finally:
            host.close()

    def test_init_loads_params_and_otf_for_selected_wavelength(self):
        cfg = AppConfig()
        cfg.selected_laser_nm = 488
        cfg.reconstruction.wiener = 3.5
        cfg.reconstruction.excitation_na = 1.2
        cfg.reconstruction.pixel_size_nm = 80.0
        cfg.reconstruction.otf_488_path = "C:/otf/488.tif"
        host = _ReconHost(app_config=cfg)
        try:
            host.setup_sim_recon_module()
            ui = host.ui
            self.assertAlmostEqual(ui.spb_main_recon_wiener.value(), 3.5)
            self.assertAlmostEqual(ui.spb_main_recon_na.value(), 1.2)
            self.assertAlmostEqual(ui.spb_main_recon_pixel_nm.value(), 80.0)
            self.assertEqual(ui.cmb_main_recon_wavelength.currentData(), 488)
            self.assertEqual(ui.edit_main_recon_otf.text(), "C:/otf/488.tif")
        finally:
            host.close()

    def test_wavelength_dropdown_switches_otf_without_changing_selected_laser(self):
        # 行为：recon 波长下拉只切换"查看/编辑哪个波长的 OTF"，绝不改采集波长 selected_laser_nm。
        cfg = AppConfig()
        cfg.selected_laser_nm = 488
        cfg.reconstruction.otf_405_path = "C:/otf/405.tif"
        cfg.reconstruction.otf_561_path = "C:/otf/561.tif"
        cfg.reconstruction.otf_638_path = "C:/otf/638.tif"
        host = _ReconHost(app_config=cfg)
        try:
            host.setup_sim_recon_module()
            ui = host.ui
            for nm, path in ((405, "C:/otf/405.tif"), (561, "C:/otf/561.tif"), (638, "C:/otf/638.tif")):
                idx = ui.cmb_main_recon_wavelength.findData(nm)
                ui.cmb_main_recon_wavelength.setCurrentIndex(idx)  # 触发 _on_main_recon_wavelength_changed
                self.assertEqual(ui.cmb_main_recon_wavelength.currentData(), nm)
                self.assertEqual(ui.edit_main_recon_otf.text(), path)
                self.assertEqual(getattr(cfg.reconstruction, f"otf_{nm}_path"), path)
            # 采集波长不被 recon 下拉改写。
            self.assertEqual(cfg.selected_laser_nm, 488)
        finally:
            host.close()

    def test_edit_otf_then_switch_wavelength_persists_each_path(self):
        cfg = AppConfig()
        cfg.selected_laser_nm = 488
        host = _ReconHost(app_config=cfg)
        try:
            host.setup_sim_recon_module()
            ui = host.ui
            # 在 488 下编辑 OTF，再切到 561 编辑，应各自存到对应波长字段。
            ui.edit_main_recon_otf.setText("C:/otf/new488.tif")
            ui.cmb_main_recon_wavelength.setCurrentIndex(ui.cmb_main_recon_wavelength.findData(561))
            ui.edit_main_recon_otf.setText("C:/otf/new561.tif")
            ui.cmb_main_recon_wavelength.setCurrentIndex(ui.cmb_main_recon_wavelength.findData(488))
            host._store_main_recon_otf_path()  # 收尾保存当前(488)
            self.assertEqual(cfg.reconstruction.otf_488_path, "C:/otf/new488.tif")
            self.assertEqual(cfg.reconstruction.otf_561_path, "C:/otf/new561.tif")
        finally:
            host.close()

    def test_setting_change_writes_back_saves_and_syncs_worker(self):
        host = _ReconHost()
        try:
            host.setup_sim_recon_module()
            ui = host.ui
            ui.spb_main_recon_wiener.setValue(4.0)
            ui.spb_main_recon_na.setValue(1.1)
            ui.spb_main_recon_pixel_nm.setValue(70.0)
            ui.edit_main_recon_background.setText("C:/bg.tif")
            ui.edit_main_recon_output.setText("C:/out")
            with mock.patch.object(legacy_main, "save_app_config") as save_cfg:
                host.on_main_recon_setting_changed()
            recon = host.sim_app_config.reconstruction
            self.assertAlmostEqual(recon.wiener, 4.0)
            self.assertAlmostEqual(recon.excitation_na, 1.1)
            self.assertAlmostEqual(recon.pixel_size_nm, 70.0)
            self.assertEqual(recon.background_path, "C:/bg.tif")
            self.assertEqual(recon.output_path, "C:/out")
            save_cfg.assert_called()
            host.ensure_sim_reconstruction_worker.assert_called()  # B6 间接：经既有 worker 同步路径
            host.refresh_sim_settings_summary.assert_called()
        finally:
            host.close()

    def test_persist_preserves_638_otf_and_saved_params(self):
        # 行为：重建配置 reader 保留 638 OTF + saved-params（use_saved_params / estimated_params）。
        cfg = AppConfig()
        cfg.selected_laser_nm = 638
        cfg.reconstruction.use_saved_params = True
        cfg.reconstruction.otf_638_path = "C:/otf/638.tif"
        cfg.reconstruction.estimated_params_638_path = "C:/params/638.mat"
        host = _ReconHost(app_config=cfg)
        try:
            host.setup_sim_recon_module()
            host._persist_main_recon_config_from_ui()
            recon = host.sim_app_config.reconstruction
            self.assertTrue(recon.use_saved_params)
            self.assertEqual(recon.otf_638_path, "C:/otf/638.tif")
            self.assertEqual(recon.estimated_params_638_path, "C:/params/638.mat")
        finally:
            host.close()

    def test_ensure_worker_existing_emits_snapshot_not_direct_setter(self):
        # B6 硬约束：worker 已存在时经 queued signal 下发 snapshot（不裸 set_reconstruction_config）。
        host = _ReconHost()
        host.sim_app_config.reconstruction.use_saved_params = True
        host.sim_app_config.reconstruction.otf_488_path = "C:/otf/488.tif"
        fake_worker = mock.Mock(name="recon_worker")
        host.sim_recon_worker = fake_worker
        received = []
        host.signal_reconstruction_config_changed.connect(received.append)
        legacy_main.MainWindow.ensure_sim_reconstruction_worker(host)
        # 经信号下发了一个 ReconstructionConfig 快照（不是 live 对象、值一致）。
        self.assertEqual(len(received), 1)
        snap = received[0]
        self.assertIsInstance(snap, ReconstructionConfig)
        self.assertIsNot(snap, host.sim_app_config.reconstruction)
        self.assertTrue(snap.use_saved_params)
        self.assertEqual(snap.otf_488_path, "C:/otf/488.tif")
        # 绝不跨线程裸 setter。
        fake_worker.set_reconstruction_config.assert_not_called()

    def test_ensure_worker_creates_and_wires_queued_signal(self):
        # B6：worker 为 None 时创建 worker 并把 signal 连到 slot_update_reconstruction_config。
        host = _ReconHost()
        fake_worker = mock.Mock(name="recon_worker")
        with mock.patch.object(legacy_main, "ReconstructionWorker", return_value=fake_worker), \
             mock.patch.object(legacy_main, "QThread", _FakeThread):
            legacy_main.MainWindow.ensure_sim_reconstruction_worker(host)
        self.assertIs(host.sim_recon_worker, fake_worker)
        # 发信号应路由到新 worker 的 queued slot（证明 ensure 内已 connect）。
        host.signal_reconstruction_config_changed.emit(host.sim_app_config.reconstruction.snapshot())
        fake_worker.slot_update_reconstruction_config.assert_called()

    def test_layout_widths_fonts_and_wiener_para_label(self):
        """布局微调：Wiener→Wiener Para、参数控件限宽 90、列伸缩、字体与 SIM Camera Settings 一致。"""
        host = _ReconHost()
        try:
            host.setup_sim_recon_module()
            ui = host.ui
            # ① Wiener 改名
            self.assertEqual(ui.lbl_main_recon_wiener.text(), "Wiener Para")
            # ② 参数 spinbox/combo 限宽 90（不再被列拉伸过宽）
            for name in (
                "spb_main_recon_wiener", "spb_main_recon_na",
                "spb_main_recon_pixel_nm", "cmb_main_recon_wavelength",
            ):
                self.assertEqual(getattr(ui, name).maximumWidth(), 90, name)
            # ③ 列伸缩：col1=1 让 OTF/Background/Output 的 edit_* 跨列拉伸；col3=0 参数列不拉伸
            grid = ui.gridLayout_recon
            self.assertEqual(grid.columnStretch(1), 1)
            self.assertEqual(grid.columnStretch(3), 0)
            # ④ 路径输入框可拉伸（无宽度上限），browse 按钮限宽 40 → 路径框比按钮宽
            self.assertEqual(ui.btn_main_recon_browse_otf.maximumWidth(), 40)
            self.assertGreater(
                ui.edit_main_recon_otf.maximumWidth(),
                ui.btn_main_recon_browse_otf.maximumWidth(),
            )
            # ⑤ 字体：标签 10pt / 控件 12pt（与 SIM Camera Settings 一致）
            self.assertEqual(ui.lbl_main_recon_wiener.font().pointSize(), 10)
            self.assertEqual(ui.spb_main_recon_wiener.font().pointSize(), 12)
        finally:
            host.close()


if __name__ == "__main__":
    unittest.main()

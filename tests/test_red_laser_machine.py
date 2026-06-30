"""双机红光波长（638 / 647）支持的专项测试。

覆盖："第四路红光波长按机器可配置"的核心契约与主界面安全互锁：
    1. ``supported_lasers_for`` / ``LASER_ROLE_MAP`` / 红光等价集合的数据契约；
    2. 波形 builder 对 647 机器仍拉同一条物理红光线（line 12 = ``laser_red_line``）；
    3. 主界面红光机器切换的统一 helper（``_apply_sim_red_laser_options``）按红光重建波长选项；
    4. 红光切换的忙碌互锁（worker 活动时拒绝并恢复控件、不部分写入）；
    5. 红光切换不提前写 ``selected_laser_nm``，而是委托 ``on_sim_camera_setting_changed``。
"""

import importlib
import sys
import types
import unittest
from pathlib import Path

from PyQt5 import QtWidgets


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTROL_ROOT = PROJECT_ROOT / "control_wangbo"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(CONTROL_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_ROOT))

sys.modules.setdefault("MCUTriggerThread", types.ModuleType("MCUTriggerThread"))
sys.modules.setdefault("mvsdk", types.ModuleType("mvsdk"))
sys.modules.setdefault("FastCameraThread", types.ModuleType("FastCameraThread"))


class RedLaserDataContractTests(unittest.TestCase):
    """数据层契约：638/647 都是合法机器红光身份，共用同一条物理红光线。"""

    def test_supported_lasers_for_switches_fourth_band(self):
        from sim_control.models import DEFAULT_RED_LASER_NM, supported_lasers_for

        self.assertEqual(supported_lasers_for(638), (405, 488, 561, 638))
        self.assertEqual(supported_lasers_for(647), (405, 488, 561, 647))
        # 非法红光波长回落到默认（638），不抛错。
        self.assertEqual(supported_lasers_for(999), (405, 488, 561, DEFAULT_RED_LASER_NM))
        self.assertEqual(supported_lasers_for("bad"), (405, 488, 561, DEFAULT_RED_LASER_NM))

    def test_red_constants(self):
        from sim_control.models import (
            RED_EQUIVALENT_WAVELENGTHS,
            RED_LASER_CHOICES,
            SUPPORTED_LASERS,
        )

        self.assertEqual(RED_LASER_CHOICES, (638, 647))
        self.assertEqual(set(RED_EQUIVALENT_WAVELENGTHS), {638, 647})
        # 模块级 SUPPORTED_LASERS 仍是显式默认四档（不由 LASER_ROLE_MAP.keys() 派生）。
        self.assertEqual(SUPPORTED_LASERS, (405, 488, 561, 638))

    def test_laser_role_map_both_reds_share_neutral_line(self):
        from sim_control.models import DEFAULT_DAQ_LINE_INDICES, LASER_ROLE_MAP

        self.assertEqual(LASER_ROLE_MAP[638], "laser_red_line")
        self.assertEqual(LASER_ROLE_MAP[647], "laser_red_line")
        self.assertEqual(DEFAULT_DAQ_LINE_INDICES["laser_red_line"], 12)


class RedLaserWaveformTests(unittest.TestCase):
    """波形契约：647 机器采集仍驱动同一条物理红光线（line 12）。"""

    def test_647_machine_waveform_pulls_same_red_line_12(self):
        from sim_control.config_store import app_config_from_dict
        from sim_control.models import TimingConfig
        from sim_control.waveform import NIDaqWaveformBuilder

        # 真 647 机器配置：red_laser_nm=647、红光线接 line 12、采集波长选 647。
        config = app_config_from_dict(
            {
                "config_version": 13,
                "red_laser_nm": 647,
                "selected_laser_nm": 647,
                "daq": {
                    "device_name": "Dev1",
                    "slm_enable_line": "Dev1/port0/line0",
                    "slm_trigger_line": "Dev1/port0/line1",
                    "slm_finish_line": "Dev1/port0/line2",
                    "camera_trigger_line": "Dev1/port0/line8",
                    "laser_405_line": "Dev1/port0/line9",
                    "laser_488_line": "Dev1/port0/line10",
                    "laser_561_line": "Dev1/port0/line11",
                    "laser_red_line": "Dev1/port0/line12",
                },
            }
        )
        self.assertEqual(config.red_laser_nm, 647)
        self.assertEqual(config.selected_laser_nm, 647)

        timing = TimingConfig(
            sample_rate_hz=10,
            edge_pulse_us=100_000,
            inter_frame_gap_us=100_000,
            slm_enable_guard_us=100_000,
        )
        plan = NIDaqWaveformBuilder().build(
            config.daq,
            timing,
            laser_wavelength_nm=config.selected_laser_nm,
            exposure_us=100_000,
            frame_count=1,
        )
        # 647 与 638 一样落到中性红光角色，拉同一条 line 12。
        self.assertEqual(plan.metadata["active_laser_role"], "laser_red_line")
        # bit0(SLM enable)+bit1(SLM trigger)+bit8(camera)+bit12(red) = 1+2+256+4096 = 4355。
        self.assertEqual(int(plan.packed_port_values[1]), 4355)


class _RedLaserHost(QtWidgets.QWidget):
    """复用 control_wangbo/main.py 真实方法的轻量 host，专测红光切换接线与互锁。"""

    def __init__(self, legacy_main, red_laser_nm=638, selected_laser_nm=488):
        super().__init__()
        from control_wangbo.CellSorting_ui import Ui_Single_Cell_Sorting

        self._legacy_main = legacy_main
        self.ui = Ui_Single_Cell_Sorting()
        self.ui.setupUi(self)
        self.sim_app_config = types.SimpleNamespace(
            red_laser_nm=red_laser_nm,
            selected_laser_nm=selected_laser_nm,
        )
        self._loading_configure_settings = False
        # 忙碌标志默认空闲。
        self.sim_acquisition_in_progress = False
        self._zscan_move_in_progress = False
        self._daq_test_thread = None
        self._slm_connect_thread = None
        self._cam_connect_thread = None
        self.camera_setting_changed_calls = 0

    # ---- 绑定 main.py 真实方法 ----
    def _apply_sim_red_laser_options(self, red_laser_nm):
        self._legacy_main.MainWindow._apply_sim_red_laser_options(self, red_laser_nm)

    def _sim_runtime_busy(self):
        return self._legacy_main.MainWindow._sim_runtime_busy(self)

    def _on_main_red_laser_changed(self, *args):
        self._legacy_main.MainWindow._on_main_red_laser_changed(self, *args)

    def _build_sim_immediate_and_acquire_controls(self):
        self._legacy_main.MainWindow._build_sim_immediate_and_acquire_controls(self)

    # ---- 桩：记录委托，避免触碰真实 SLM/相机/preview ----
    def on_sim_camera_setting_changed(self, *args):
        self.camera_setting_changed_calls += 1

    def _reset_immediate_ro_dropdown(self):
        self._legacy_main.MainWindow._reset_immediate_ro_dropdown(self)

    def _update_immediate_ro_dropdown_tooltip(self, text=None):
        self._legacy_main.MainWindow._update_immediate_ro_dropdown_tooltip(self, text)

    def on_immediate_ro_changed(self, _index=None):
        pass

    def trigger_sim_raw_9frame_acquisition(self, trigger_source="manual"):
        pass


class RedLaserSwitchGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.legacy_main = importlib.import_module("control_wangbo.main")

    def _wavelength_items(self, combo):
        return [combo.itemData(i) for i in range(combo.count())]

    def test_apply_red_laser_options_rebuilds_dropdowns_and_label(self):
        host = _RedLaserHost(self.legacy_main, red_laser_nm=638)
        try:
            host._build_sim_immediate_and_acquire_controls()
            # 启动默认 638 机器：采集/recon/red 下拉就位。
            self.assertEqual(self._wavelength_items(host.ui.cmb_sCMOS_laser), [405, 488, 561, 638])
            self.assertEqual(self._wavelength_items(host.ui.cmb_main_red_laser), [638, 647])
            # 选中第四档（红光槽位）后切到 647：保持槽位 index，红光档 itemData 变 647。
            host.ui.cmb_sCMOS_laser.setCurrentIndex(3)
            host._apply_sim_red_laser_options(647)
            self.assertEqual(self._wavelength_items(host.ui.cmb_sCMOS_laser), [405, 488, 561, 647])
            self.assertEqual(host.ui.cmb_sCMOS_laser.currentIndex(), 3)
            self.assertEqual(host.ui.cmb_sCMOS_laser.currentData(), 647)
            self.assertEqual(self._wavelength_items(host.ui.cmb_main_recon_wavelength), [405, 488, 561, 647])
            self.assertEqual(host.ui.lbl_main_daq_laser_red.text(), "Laser red")
        finally:
            host.close()

    def test_sim_runtime_busy_detects_each_worker(self):
        host = _RedLaserHost(self.legacy_main)
        try:
            self.assertFalse(host._sim_runtime_busy())
            for flag, value in (
                ("sim_acquisition_in_progress", True),
                ("_zscan_move_in_progress", True),
                ("_daq_test_thread", object()),
                ("_slm_connect_thread", object()),
                ("_cam_connect_thread", object()),
            ):
                setattr(host, flag, value)
                self.assertTrue(host._sim_runtime_busy(), flag)
                # 复位，逐一验证。
                setattr(host, flag, False if isinstance(value, bool) else None)
            self.assertFalse(host._sim_runtime_busy())
        finally:
            host.close()

    def test_red_switch_rejected_when_busy_and_restores_control(self):
        host = _RedLaserHost(self.legacy_main, red_laser_nm=638)
        # 屏蔽模态弹窗。
        original_info = self.legacy_main.qw.QMessageBox.information
        self.legacy_main.qw.QMessageBox.information = staticmethod(lambda *a, **k: None)
        try:
            host._build_sim_immediate_and_acquire_controls()
            host.sim_acquisition_in_progress = True  # worker 忙碌
            # 用户把红光下拉切到 647（blockSignals 模拟程序态，手动调 handler）。
            idx647 = host.ui.cmb_main_red_laser.findData(647)
            host.ui.cmb_main_red_laser.blockSignals(True)
            host.ui.cmb_main_red_laser.setCurrentIndex(idx647)
            host.ui.cmb_main_red_laser.blockSignals(False)
            host._on_main_red_laser_changed()
            # 忙碌拒绝：red_laser_nm 不变、控件恢复到 638、未委托 camera setting changed。
            self.assertEqual(host.sim_app_config.red_laser_nm, 638)
            self.assertEqual(host.ui.cmb_main_red_laser.currentData(), 638)
            self.assertEqual(host.camera_setting_changed_calls, 0)
        finally:
            self.legacy_main.qw.QMessageBox.information = original_info
            host.close()

    def test_red_switch_not_busy_writes_red_only_and_delegates(self):
        host = _RedLaserHost(self.legacy_main, red_laser_nm=638, selected_laser_nm=488)
        try:
            host._build_sim_immediate_and_acquire_controls()
            idx647 = host.ui.cmb_main_red_laser.findData(647)
            host.ui.cmb_main_red_laser.blockSignals(True)
            host.ui.cmb_main_red_laser.setCurrentIndex(idx647)
            host.ui.cmb_main_red_laser.blockSignals(False)
            host._on_main_red_laser_changed()
            # 写机器红光身份 + 重建采集波长选项；委托 on_sim_camera_setting_changed 做安全序列；
            # **不提前写 selected_laser_nm**（仍是 488，由被委托的 handler 经 sync 决定）。
            self.assertEqual(host.sim_app_config.red_laser_nm, 647)
            self.assertEqual(self._wavelength_items(host.ui.cmb_sCMOS_laser), [405, 488, 561, 647])
            self.assertEqual(host.sim_app_config.selected_laser_nm, 488)
            self.assertEqual(host.camera_setting_changed_calls, 1)
        finally:
            host.close()


if __name__ == "__main__":
    unittest.main()

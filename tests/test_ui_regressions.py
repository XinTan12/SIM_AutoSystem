"""PyQt5 UI 布局与生成文件回归测试。

作用：
    覆盖 SIM 设置弹窗与 ``control_wangbo`` 主窗口的 UI 不变量，避免后续 UI 调整时：
        - 误删除已弃用控件后又被恢复（``test_cellsorting_ui_removes_legacy_*``）。
        - SIM 设置弹窗的 ``Camera`` 页、``Timing`` 控件复活（已迁移到主界面摘要）。
        - 字体非法 point size 触发 ``QFont::setPointSize: Point size <= 0`` 警告。
        - DAQ wiring 区控件在最小尺寸下出现重叠或宽度退化。
        - SimControlWindow 的 prepare/acquisition 入口绕过 worker 在 GUI 线程做硬件预备。
        - SLM 未连接时仍能发起采集（必须被阻止）。
        - ``running_order_selected`` 状态信号能反写 ``self.config.selected_running_order``。

协作关系：
    上游：``unittest``、``unittest.mock``、PyQt5 真实 QApplication。
    下游：``control_wangbo.CellSorting_ui``、``sim_control.ui_sim_settings_dialog``（pyuic5 生成）、
          ``sim_control.gui.SimControlWindow``。

维护要点：
    - 直接读取 pyuic5 生成的 Ui_* 类；这些类**绝不能手改**，本测试通过 ``hasattr``
      与几何断言锁定 .ui 设计意图。
    - SimControlWindow 的几个测试都用 ``SimpleNamespace`` mock 整个 controller，
      把"GUI 不应再做的事情"标成 ``AssertionError``；若实现回归会立即失败。
    - 修改 .ui 文件后必须重新生成 ``.py`` 并跑本测试，确认控件名/几何/字体未漂移。
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PyQt5 import QtCore, QtWidgets


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class UiRegressionTests(unittest.TestCase):
    """锁定 PyQt5 设置界面和生成 UI 文件的布局/控件回归行为。"""

    @classmethod
    def setUpClass(cls):
        # 共用一个 QApplication 实例；多个 test 共享避免 Qt 重复初始化。
        cls.app = QtWidgets.QApplication.instance()
        if cls.app is None:
            cls.app = QtWidgets.QApplication([])

    def test_cellsorting_ui_removes_legacy_elasticity_widgets_and_restructures_sim_controls(self):
        """旧 ``Elasticity Measurement`` 控件被移除；新 SIM 相机/SLM 控件以 ``sCMOS_*`` 名字命名。"""
        from control_wangbo.CellSorting_ui import Ui_Single_Cell_Sorting

        # 1) 实例化生成 UI 并 setupUi 到一个 QWidget 上；后续在它上做 findChildren。
        widget = QtWidgets.QWidget()
        ui = Ui_Single_Cell_Sorting()
        ui.setupUi(widget)

        # 2) 旧 Elasticity 相关 GroupBox 必须**不存在**：曾经的字段命名。
        self.assertFalse(hasattr(ui, "grp_collectedCellsROI_2"))
        self.assertFalse(hasattr(ui, "grp_sortROIView_2"))
        self.assertFalse(hasattr(ui, "groupBox_2"))

        # 3) 收集所有 QLabel/QPushButton/QGroupBox 文本，做"不应出现 / 应出现"集合断言。
        texts = {
            child.text()
            for child in widget.findChildren((QtWidgets.QLabel, QtWidgets.QPushButton))
            if hasattr(child, "text")
        }
        titles = {group.title() for group in widget.findChildren(QtWidgets.QGroupBox)}
        all_text = texts | titles

        # 4) 旧 Elasticity / SIM Preview 等字符串必须不出现。
        self.assertNotIn("Elasticity Measurement ROI", all_text)
        self.assertNotIn("Elasticity Measurement ROI View (sCMOS)", all_text)
        self.assertNotIn("Elasticity ROI Live View", all_text)

        self.assertNotIn("ROI Width", all_text)
        self.assertNotIn("ROI Height", all_text)
        self.assertNotIn("Start SIM Preview", all_text)
        self.assertNotIn("SIM Preview", all_text)
        self.assertNotIn("Delay(us)", all_text)
        self.assertNotIn("Save (sCMOS)", all_text)
        self.assertNotIn("#Pre_T", all_text)
        self.assertNotIn("#Post_T", all_text)

        # 5) 新 SIM 相机/SLM 文本必须出现（确认重新设计后的 UI 没漏掉）。
        self.assertIn("ROI_X", all_text)
        self.assertIn("ROI_Y", all_text)
        self.assertIn("Width", all_text)
        self.assertIn("Height", all_text)
        self.assertIn("Hardvare Connection (SIM Camera)", all_text)
        self.assertIn("Hardvare Connection (SLM)", all_text)
        self.assertIn("Connect SIM Camera", all_text)
        self.assertIn("Connect SLM", all_text)
        self.assertIn("Live", all_text)
        self.assertIn("Save", all_text)
        self.assertIn("Pre_T", all_text)
        self.assertIn("Post_T", all_text)

        # 6) 新控件属性应存在（名字与 .ui 设计师中保持一致）。
        self.assertTrue(hasattr(ui, "grp_hardvareConnection_sCMOS"))
        self.assertTrue(hasattr(ui, "grp_hardvareConnection_SLM"))
        self.assertTrue(hasattr(ui, "cmb_sCMOS_camera"))
        self.assertTrue(hasattr(ui, "cmb_SLM_device"))
        self.assertTrue(hasattr(ui, "btn_sCMOS_refresh"))
        self.assertTrue(hasattr(ui, "btn_SLM_refresh"))
        self.assertTrue(hasattr(ui, "btn_SLM_connection"))
        self.assertTrue(hasattr(ui, "lbl_SLM_status"))
        self.assertTrue(hasattr(ui, "btn_sCMOS_live"))
        self.assertTrue(hasattr(ui, "chb_sCMOS_autoContrast"))
        self.assertTrue(hasattr(ui, "spb_sCMOS_ROI_X"))
        self.assertTrue(hasattr(ui, "spb_sCMOS_ROI_Y"))
        self.assertTrue(hasattr(ui, "cmb_sCMOS_imageSize"))
        self.assertTrue(hasattr(ui, "cmb_sCMOS_bitDepth"))
        # 7) 已废弃的旧 sCMOS 字段不应存在。
        self.assertFalse(hasattr(ui, "spb_sCMOS_ringBufferCapacity"))
        self.assertFalse(hasattr(ui, "spb_sCMOS_delayTime"))
        self.assertFalse(hasattr(ui, "spb_sCMOS_pixelWidth"))
        self.assertFalse(hasattr(ui, "spb_sCMOS_pixelHeight"))

        # 8) 类型 / 默认值 / 下拉项内容 / 控件范围逐一锁定。
        self.assertIsInstance(ui.spb_sCMOS_ROI_X, QtWidgets.QSpinBox)
        self.assertIsInstance(ui.spb_sCMOS_ROI_Y, QtWidgets.QSpinBox)
        self.assertIsInstance(ui.spb_sCMOS_exposureTime, QtWidgets.QSpinBox)
        self.assertIsInstance(ui.cmb_sCMOS_imageSize, QtWidgets.QComboBox)
        self.assertIsInstance(ui.cmb_sCMOS_bitDepth, QtWidgets.QComboBox)
        self.assertIsInstance(ui.chb_sCMOS_autoContrast, QtWidgets.QCheckBox)
        self.assertFalse(ui.chb_sCMOS_autoContrast.isChecked())
        self.assertEqual(ui.chb_sCMOS_autoContrast.text(), "Auto Contrast")
        # ROI 尺寸下拉必须列出 3 档预设；位深下拉必须列出 3 个档位。
        self.assertEqual(
            [ui.cmb_sCMOS_imageSize.itemText(index) for index in range(ui.cmb_sCMOS_imageSize.count())],
            ["2304 x 2304", "1152 x 1152", "576 x 576"],
        )
        self.assertEqual(
            [ui.cmb_sCMOS_bitDepth.itemText(index) for index in range(ui.cmb_sCMOS_bitDepth.count())],
            ["8-bit", "12-bit", "16-bit"],
        )
        # 曝光控件单位为毫秒；旧 us 标签不应再出现。
        self.assertEqual(ui.spb_sCMOS_exposureTime.minimum(), 1)
        self.assertEqual(ui.spb_sCMOS_exposureTime.maximum(), 10_000)
        self.assertEqual(ui.spb_sCMOS_exposureTime.value(), 10)
        self.assertIn("Exp(ms)", all_text)
        self.assertNotIn("Exp(us)", all_text)

    def test_cellsorting_main_title_matches_super_resolution_product_name(self):
        """主窗口标题应为产品名 ``Intelligent Super-Resolution Imaging``；``.ui`` 源文件也要含同一字符串。"""
        from control_wangbo.CellSorting_ui import Ui_Single_Cell_Sorting

        widget = QtWidgets.QWidget()
        ui = Ui_Single_Cell_Sorting()
        ui.setupUi(widget)

        # 窗口标题与顶部 label 文本都锁定。
        self.assertEqual(widget.windowTitle(), "Intelligent Super-Resolution Imaging")
        self.assertEqual(ui.label.text(), "Intelligent Super-Resolution Imaging")

        # 同时检查 .ui 源文件是否含该字符串（避免有人只改了生成的 .py 而忘了 .ui）。
        ui_source = PROJECT_ROOT / "control_wangbo" / "CellSorting_ui.ui"
        self.assertIn(
            "<string>Intelligent Super-Resolution Imaging</string>",
            ui_source.read_text(encoding="utf-8"),
        )

    def test_cellsorting_left_column_uses_uniform_lines_between_groups(self):
        """主窗口左列 8 条分隔线必须几何对齐，宽 291、高 1，且紧贴上下 GroupBox。"""
        from control_wangbo.CellSorting_ui import Ui_Single_Cell_Sorting

        widget = QtWidgets.QWidget()
        ui = Ui_Single_Cell_Sorting()
        ui.setupUi(widget)

        # 1) 左列 9 个 GroupBox 与对应 8 条分隔线的顺序锁定。
        column_groups = [
            ui.grp_hardvareConnection,
            ui.grp_hardvareConnection_sCMOS,
            ui.grp_hardvareConnection_2,
            ui.grp_releaseROI,
            ui.grp_captureROI,
            ui.grp_trappedROI,
            ui.grp_sortROI,
            ui.grp_collectedCellsROI,
            ui.grp_flowRateDetection,
        ]
        lines = [
            getattr(ui, name)
            for name in (
                "line_21",
                "line_18",
                "line_16",
                "line_19",
                "line_17",
                "line_22",
                "line_23",
                "line_24",
            )
        ]

        # 2) 顶部 GroupBox 起点 y=50 锁定，避免和工具栏重叠。
        self.assertEqual(ui.grp_hardvareConnection.geometry().y(), 50)

        # 3) 每条分隔线在 (x=10, y=upper.bottom)，宽 291，高 1；下一个 GroupBox 紧接其下。
        for upper, line, lower in zip(column_groups, lines, column_groups[1:]):
            self.assertEqual(line.geometry().x(), 10)
            self.assertEqual(line.geometry().width(), 291)
            self.assertEqual(line.geometry().height(), 1)
            self.assertEqual(line.geometry().y(), upper.geometry().y() + upper.geometry().height())
            self.assertEqual(lower.geometry().y(), line.geometry().y() + line.geometry().height())

        # 4) 整列必须完整在窗口高度之内，避免被裁。
        lowest_bottom = max(group.geometry().y() + group.geometry().height() for group in column_groups)
        self.assertLessEqual(
            lowest_bottom,
            widget.height(),
            "The entire left column should fit inside the main window height.",
        )

    def test_sim_settings_ui_removes_camera_tab_and_timing_controls(self):
        """SIM 设置弹窗只保留 Laser + DAQ 两页；Camera 页与 Timing 控件已迁移到主界面摘要。"""
        from sim_control.ui_sim_settings_dialog import Ui_SimSettingsDialog

        dialog = QtWidgets.QDialog()
        ui = Ui_SimSettingsDialog()
        ui.setupUi(dialog)

        # 1) tab_daq 仍存在；tab_camera 已删除（控件改到主界面摘要）。
        self.assertTrue(hasattr(ui, "tab_daq"))
        self.assertFalse(hasattr(ui, "tab_camera"))
        self.assertFalse(hasattr(ui, "group_camera_main"))

        # 2) 页签顺序必须严格为 ["Laser", "DAQ"]，避免误增加 Camera。
        tab_titles = [ui.tabs.tabText(index) for index in range(ui.tabs.count())]
        self.assertNotIn("Camera", tab_titles)
        self.assertEqual(tab_titles, ["Laser", "DAQ"])
        # 3) Pattern 页 / SLM 设备下拉 / 加载 pattern 按钮：全部已废弃。
        self.assertFalse(hasattr(ui, "tab_patterns"))
        self.assertFalse(hasattr(ui, "combo_slm_device"))
        self.assertFalse(hasattr(ui, "btn_load_patterns"))
        # 4) 测试目标下拉 + Pulse Test 按钮：新功能必须存在。
        self.assertTrue(hasattr(ui, "combo_test_target"))
        self.assertTrue(hasattr(ui, "btn_pulse_test"))
        self.assertIsInstance(ui.combo_test_target, QtWidgets.QComboBox)
        self.assertIsInstance(ui.btn_pulse_test, QtWidgets.QPushButton)
        texts = {
            child.text()
            for child in dialog.findChildren((QtWidgets.QLabel, QtWidgets.QPushButton))
            if hasattr(child, "text")
        }
        self.assertIn("Pulse Test", texts)
        self.assertIn("Test target", texts)
        # ``Validate Wiring`` 按钮被移除（校验改由测试自动跑）。
        self.assertNotIn("Validate Wiring", texts)

        # 5) Timing 控件全部移除；2026-05-11 决策日志要求 Timing 字段只在主摘要显示。
        self.assertFalse(hasattr(ui, "group_daq_timing"))
        for widget_name in (
            "spin_sample_rate",
            "spin_edge_pulse_us",
            "spin_inter_frame_gap_us",
            "spin_slm_enable_guard_us",
        ):
            self.assertFalse(hasattr(ui, widget_name))
        self.assertNotIn("Timing", texts)
        self.assertNotIn("Sample Rate (Hz)", texts)
        self.assertNotIn("Edge Pulse (us)", texts)
        self.assertNotIn("Inter Frame Gap (us)", texts)
        self.assertNotIn("SLM Enable Guard (us)", texts)

    def test_sim_settings_dialog_uses_native_style_without_pattern_specific_rules(self):
        """设置弹窗应保留原生 PyQt5 样式（无自定义 QSS），且不暴露 pattern 相关控件。"""
        from sim_control.ui_sim_settings_dialog import Ui_SimSettingsDialog

        dialog = QtWidgets.QDialog()
        ui = Ui_SimSettingsDialog()
        ui.setupUi(dialog)

        # styleSheet 必须为空字符串 → 保留原生外观，不依赖自定义 QSS。
        self.assertEqual(dialog.styleSheet(), "")
        self.assertFalse(hasattr(ui, "group_pattern_connection"))

    def test_sim_settings_ui_setup_does_not_emit_invalid_font_size_warnings(self):
        """``setupUi`` 期间不应出现 ``QFont::setPointSize: Point size <= 0`` 警告。"""
        from sim_control.ui_sim_settings_dialog import Ui_SimSettingsDialog

        dialog = QtWidgets.QDialog()
        ui = Ui_SimSettingsDialog()
        messages = []

        # ``qInstallMessageHandler`` 捕获所有 Qt 调试输出，便于断言无非法字体警告。
        def message_handler(_msg_type, _context, message):
            messages.append(message)

        previous_handler = QtCore.qInstallMessageHandler(message_handler)
        try:
            ui.setupUi(dialog)
        finally:
            # 恢复原 handler，避免影响其它测试。
            QtCore.qInstallMessageHandler(previous_handler)

        self.assertFalse(
            any("QFont::setPointSize: Point size <= 0" in message for message in messages),
            "SIM settings dialog should not apply invalid font point sizes.",
        )

    def test_sim_settings_dialog_uses_compact_geometry_and_segoe_ui_fonts(self):
        """设置弹窗最小尺寸 920×600；字体 ``Segoe UI``；title/error/laser 几何与字号锁定。"""
        from sim_control.ui_sim_settings_dialog import Ui_SimSettingsDialog

        dialog = QtWidgets.QDialog()
        ui = Ui_SimSettingsDialog()
        ui.setupUi(dialog)
        dialog.resize(980, 640)
        dialog.show()
        self.app.processEvents()

        # 最小尺寸 / 高度 / 字体族 / 各组件几何与字号都锁定，避免 .ui 修改后视觉退化。
        self.assertEqual(dialog.minimumSize().width(), 920)
        self.assertEqual(dialog.minimumSize().height(), 600)
        self.assertEqual(dialog.height(), 640)
        self.assertEqual(dialog.font().family(), "Segoe UI")
        self.assertLessEqual(ui.dialogHeader.geometry().height(), 64)
        self.assertLessEqual(ui.tabs.geometry().y(), 80)
        self.assertEqual(ui.lbl_error.font().pointSize(), 11)
        self.assertGreaterEqual(ui.lbl_error.maximumHeight(), 28)
        self.assertEqual(ui.radio_laser_405.font().pointSize(), 13)
        self.assertEqual(ui.radio_laser_405.minimumSize().height(), 56)

    def test_sim_settings_daq_channel_controls_do_not_overlap_at_minimum_size(self):
        """DAQ 页 8 个线位下拉在最小尺寸下不应有相互重叠。"""
        from sim_control.ui_sim_settings_dialog import Ui_SimSettingsDialog

        dialog = QtWidgets.QDialog()
        ui = Ui_SimSettingsDialog()
        ui.setupUi(dialog)
        dialog.resize(920, 600)
        dialog.show()
        self.app.processEvents()

        # 切到 DAQ 页，让其内部控件被布局到位。
        ui.tabs.setCurrentWidget(ui.tab_daq)
        self.app.processEvents()

        # 1) DAQ 组宽度 ≥820；线位/测试下拉最小宽度满足可读性。
        self.assertGreaterEqual(ui.group_daq.geometry().width(), 820)
        self.assertGreaterEqual(ui.combo_slm_enable_line.geometry().width(), 240)
        self.assertGreaterEqual(ui.combo_laser_647_line.geometry().width(), 240)
        self.assertGreaterEqual(ui.combo_test_target.geometry().width(), 300)

        labels = [
            ui.label_slm_enable_line,
            ui.label_slm_trigger_line,
            ui.label_slm_finish_line,
            ui.label_camera_trigger_line,
            ui.label_laser_405_line,
            ui.label_laser_488_line,
            ui.label_laser_561_line,
            ui.label_laser_647_line,
        ]
        combos = [
            ui.combo_slm_enable_line,
            ui.combo_slm_trigger_line,
            ui.combo_slm_finish_line,
            ui.combo_camera_trigger_line,
            ui.combo_laser_405_line,
            ui.combo_laser_488_line,
            ui.combo_laser_561_line,
            ui.combo_laser_647_line,
        ]

        # 2) 各控件最小高度 ≥20/36，避免文字截断。
        for label in labels:
            self.assertGreaterEqual(label.geometry().height(), 20)
        for combo in combos:
            self.assertGreaterEqual(combo.geometry().height(), 36)

        # 3) 两两组合检查 combo 矩形不相交；若有重叠会直接报错并指出对应控件名。
        for left_index, left_combo in enumerate(combos):
            left_rect = left_combo.geometry()
            for right_combo in combos[left_index + 1 :]:
                self.assertFalse(
                    left_rect.intersects(right_combo.geometry()),
                    f"{left_combo.objectName()} overlaps {right_combo.objectName()}",
                )

    def test_sim_settings_daq_wiring_uses_vertical_space_evenly(self):
        """DAQ wiring 区段必须均匀分布垂直空白：device→lines→test 三段间距 ≥35 px。"""
        from sim_control.ui_sim_settings_dialog import Ui_SimSettingsDialog

        dialog = QtWidgets.QDialog()
        ui = Ui_SimSettingsDialog()
        ui.setupUi(dialog)
        dialog.resize(980, 640)
        dialog.show()
        self.app.processEvents()

        ui.tabs.setCurrentWidget(ui.tab_daq)
        self.app.processEvents()

        line_combos = [
            ui.combo_slm_enable_line,
            ui.combo_slm_trigger_line,
            ui.combo_slm_finish_line,
            ui.combo_camera_trigger_line,
            ui.combo_laser_405_line,
            ui.combo_laser_488_line,
            ui.combo_laser_561_line,
            ui.combo_laser_647_line,
        ]
        # 1) 计算 device 行底部 / 线位行顶 / 底 / 测试行顶 / 底 / 组底。
        device_bottom = ui.combo_daq_device.geometry().bottom()
        lines_top = min(combo.geometry().top() for combo in line_combos)
        lines_bottom = max(combo.geometry().bottom() for combo in line_combos)
        test_top = ui.combo_test_target.geometry().top()
        test_bottom = max(
            ui.combo_test_target.geometry().bottom(),
            ui.btn_pulse_test.geometry().bottom(),
        )
        group_bottom = ui.group_daq.contentsRect().bottom()

        # 2) 三段间距：device→lines ≥35 px，lines→test ≥35 px，test→组底 ≤100 px。
        self.assertGreaterEqual(lines_top - device_bottom, 35)
        self.assertGreaterEqual(test_top - lines_bottom, 35)
        self.assertLessEqual(group_bottom - test_bottom, 100)

    def test_sim_control_window_limits_runtime_log_blocks(self):
        """``SimControlWindow.log_output`` 必须把 maximumBlockCount 锁定为 1000，避免日志吞内存。"""
        import tempfile

        from sim_control.config_store import save_app_config
        from sim_control.gui import SimControlWindow
        from sim_control.models import AppConfig, BackendConfig

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "sim_config.json"
            # 仿真 backend 让 controller 不会真的去连接 NI / DCAM。
            save_app_config(AppConfig(backend=BackendConfig(simulation_mode=True)), config_path)
            window = SimControlWindow(config_path=str(config_path))
            try:
                self.assertEqual(window.log_output.document().maximumBlockCount(), 1000)
            finally:
                window.close()

    def test_sim_control_window_prepare_experiment_uses_running_order_not_manual_patterns(self):
        """``_prepare_experiment`` 必须把 4 个预备开关都置 True，且**不**直接调 ``prepare_patterns``。"""
        import tempfile

        from sim_control.config_store import save_app_config
        from sim_control.gui import SimControlWindow
        from sim_control.models import AppConfig, BackendConfig

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "sim_config.json"
            save_app_config(AppConfig(backend=BackendConfig(simulation_mode=True)), config_path)
            window = SimControlWindow(config_path=str(config_path))
            original_controller = window.controller
            # 用 SimpleNamespace mock 整个 controller：除了 start_prepare_experiment，其它"GUI 旧路径"
            # 都被替成 AssertionError，确认 GUI 不再绕过 worker 做硬件预备。
            controller = SimpleNamespace(
                slm_adapter=SimpleNamespace(is_connected=mock.Mock(return_value=True)),
                start_prepare_experiment=mock.Mock(return_value="prepare-1"),
                apply_daq_config=mock.Mock(side_effect=AssertionError("DAQ preflight should run in worker")),
                initialize_hardware=mock.Mock(side_effect=AssertionError("hardware init should run in worker")),
                apply_camera_config=mock.Mock(side_effect=AssertionError("camera preflight should run in worker")),
                select_running_order_for_task=mock.Mock(side_effect=AssertionError("RO selection should run in worker")),
                prepare_patterns=mock.Mock(side_effect=AssertionError("manual pattern path should not be used")),
            )
            window.controller = controller

            try:
                window._prepare_experiment()
            finally:
                # 还原 controller 保证 closeEvent 能干净退出。
                window.controller = original_controller
                window.close()

        # 1) start_prepare_experiment 必须被调用一次，且 4 个预备开关都为 True。
        controller.start_prepare_experiment.assert_called_once()
        prepare_kwargs = controller.start_prepare_experiment.call_args.kwargs
        self.assertTrue(prepare_kwargs["prepare_running_order"])
        self.assertTrue(prepare_kwargs["initialize_hardware"])
        self.assertTrue(prepare_kwargs["apply_daq_config"])
        self.assertTrue(prepare_kwargs["apply_camera_config"])
        # 2) prepare_patterns 不应被调（GUI 已禁用手动 pattern 编程路径）。
        controller.prepare_patterns.assert_not_called()

    def test_sim_control_window_run_acquisition_defers_preflight_to_worker(self):
        """``_run_single_acquisition`` 必须把所有预备开关都置 True，且不在 GUI 线程做硬件操作。"""
        import tempfile

        from sim_control.config_store import save_app_config
        from sim_control.gui import SimControlWindow
        from sim_control.models import AppConfig, BackendConfig

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "sim_config.json"
            save_app_config(AppConfig(backend=BackendConfig(simulation_mode=True)), config_path)
            window = SimControlWindow(config_path=str(config_path))
            original_controller = window.controller
            controller = SimpleNamespace(
                slm_adapter=SimpleNamespace(is_connected=mock.Mock(return_value=True)),
                start_single_acquisition=mock.Mock(return_value="task-1"),
                apply_daq_config=mock.Mock(side_effect=AssertionError("DAQ preflight should run in worker")),
                initialize_hardware=mock.Mock(side_effect=AssertionError("hardware init should run in worker")),
                apply_camera_config=mock.Mock(side_effect=AssertionError("camera preflight should run in worker")),
                select_running_order_for_task=mock.Mock(side_effect=AssertionError("RO selection should run in worker")),
                prepare_patterns=mock.Mock(side_effect=AssertionError("manual pattern path should not be used")),
            )
            window.controller = controller

            try:
                window._run_single_acquisition()
            finally:
                window.controller = original_controller
                window.close()

        # start_single_acquisition 必须被调一次；4 个预备开关都为 True。
        controller.start_single_acquisition.assert_called_once()
        run_kwargs = controller.start_single_acquisition.call_args.kwargs
        self.assertTrue(run_kwargs["prepare_running_order"])
        self.assertTrue(run_kwargs["initialize_hardware"])
        self.assertTrue(run_kwargs["apply_daq_config"])
        self.assertTrue(run_kwargs["apply_camera_config"])

    def test_sim_control_window_run_acquisition_blocks_when_slm_is_disconnected(self):
        """SLM 未连接时 ``_run_single_acquisition`` 应抛错（或被装饰器吞掉），且不发起采集。"""
        import tempfile

        from sim_control.config_store import save_app_config
        from sim_control.gui import SimControlWindow
        from sim_control.models import AppConfig, BackendConfig

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "sim_config.json"
            save_app_config(AppConfig(backend=BackendConfig(simulation_mode=True)), config_path)
            window = SimControlWindow(config_path=str(config_path))
            original_controller = window.controller
            # is_connected=False → ``_ensure_slm_connected_for_running_order`` 抛 HardwareError；
            # ``_catch_to_error`` 装饰器把错误打到状态标签而不传播。
            controller = SimpleNamespace(
                slm_adapter=SimpleNamespace(is_connected=mock.Mock(return_value=False)),
                start_single_acquisition=mock.Mock(return_value="task-1"),
            )
            window.controller = controller

            try:
                window._run_single_acquisition()
            finally:
                window.controller = original_controller
                window.close()

        # start_single_acquisition 不应被调用：SLM 未连接已经在前置检查时抛错。
        controller.start_single_acquisition.assert_not_called()

    def test_sim_control_window_syncs_running_order_selected_from_worker_status(self):
        """worker 广播 ``running_order_selected`` 状态时，GUI 应把 RO 名同步到 ``self.config``。"""
        import tempfile

        from sim_control.config_store import save_app_config
        from sim_control.gui import SimControlWindow
        from sim_control.models import AppConfig, BackendConfig

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "sim_config.json"
            save_app_config(AppConfig(backend=BackendConfig(simulation_mode=True)), config_path)
            window = SimControlWindow(config_path=str(config_path))
            try:
                # 先把 selected_running_order 清空，再触发状态信号。
                window.config.selected_running_order = ""
                window._handle_status_changed(
                    "running_order_selected",
                    {"running_order_name": "488_3.5_2d_1ms"},
                )
                # GUI 必须把 RO 名写回 config，便于摘要刷新与下次采集复用。
                self.assertEqual(window.config.selected_running_order, "488_3.5_2d_1ms")
            finally:
                window.close()


if __name__ == "__main__":
    unittest.main()

"""PyQt5 UI 布局与生成文件回归测试。

作用：
    覆盖 ``control_wangbo`` 主窗口与 ``sim_control`` 居中下拉/控制窗口的 UI 不变量，避免后续 UI 调整时：
        - 误删除已弃用控件后又被恢复（``test_cellsorting_ui_removes_legacy_*``）。
        - 居中只读下拉的点击展开/幂等配置/索引-数据同步行为退化。
        - SimControlWindow 的 prepare/acquisition 入口绕过 worker 在 GUI 线程做硬件预备。
        - SLM 未连接时仍能发起采集（必须被阻止）。
        - ``running_order_selected`` 状态信号能反写 ``self.config.selected_running_order``。

    注：原 ``SimSettingsDialog`` 设置弹窗（含 ``Ui_SimSettingsDialog`` 的 DAQ/Recon 页）已删除、
    其 DAQ/Recon 功能迁移到 ``control_wangbo`` 主界面一级模块；对应弹窗 UI/行为测试随之移除。

协作关系：
    上游：``unittest``、``unittest.mock``、PyQt5 真实 QApplication。
    下游：``control_wangbo.CellSorting_ui``（pyuic5 生成）、``sim_control.gui.SimControlWindow``。

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

from PyQt5 import QtCore, QtTest, QtWidgets


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
        self.assertFalse(hasattr(ui, "grp_sortROI"))
        self.assertFalse(hasattr(ui, "grp_sortROIView"))
        self.assertFalse(hasattr(ui, "spb_sortROI_X"))
        self.assertFalse(hasattr(ui, "btn_sortROI_view"))

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
        self.assertNotIn("Sort ROI", all_text)
        self.assertNotIn("Sort ROI View", all_text)

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
        self.assertFalse(hasattr(ui, "grp_simRuntime"))
        self.assertTrue(hasattr(ui, "grp_savePath"))
        self.assertTrue(hasattr(ui, "edit_main_savePath_folder"))
        self.assertTrue(hasattr(ui, "edit_main_savePath_prefix"))
        self.assertTrue(hasattr(ui, "spb_main_savePath_startNumber"))
        self.assertTrue(hasattr(ui, "btn_main_savePath_browse"))
        self.assertTrue(hasattr(ui, "lbl_main_savePath_preview"))
        self.assertTrue(ui.edit_main_savePath_folder.isReadOnly())
        self.assertEqual(ui.btn_main_savePath_browse.text(), "...")
        self.assertLessEqual(
            ui.grp_savePath.layout().minimumSize().width(),
            ui.layoutWidget_simConfiguration.width(),
        )
        self.assertEqual(ui.edit_main_savePath_prefix.font().family(), "Arial")
        self.assertEqual(ui.lbl_main_savePath_folder.font().family(), "Arial")
        self.assertEqual(ui.lbl_main_savePath_folder.font().pointSize(), 12)
        self.assertEqual(ui.lbl_main_savePath_prefix.font().pointSize(), 12)
        self.assertEqual(ui.lbl_main_savePath_startNumber.font().pointSize(), 12)
        self.assertEqual(ui.lbl_main_savePath_preview.font().pointSize(), 12)
        self.assertEqual(ui.spb_main_savePath_startNumber.minimum(), 1)
        self.assertEqual(ui.spb_main_savePath_startNumber.maximum(), 99999)
        self.assertEqual(ui.lbl_main_savePath_preview.text(), "Next: SIM9_0001.tif")
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
        self.assertIsInstance(ui.spb_triggerFunction_time, QtWidgets.QDoubleSpinBox)
        self.assertEqual(ui.spb_triggerFunction_time.maximum(), 600_000)
        trigger_time_spinboxes = [
            ui.spb_triggerCapture_time,
            ui.spb_triggerFunction_time,
            ui.spb_triggerRelease_time,
            ui.spb_triggerReleaseSort_time,
        ]
        for spinbox in trigger_time_spinboxes:
            with self.subTest(trigger_time_spinbox=spinbox.objectName()):
                self.assertIsInstance(spinbox, QtWidgets.QDoubleSpinBox)
                self.assertEqual(spinbox.decimals(), 0)
                self.assertEqual(spinbox.minimum(), 1)
                self.assertEqual(spinbox.singleStep(), 1)
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
        """主窗口左列 6 条分隔线必须几何对齐，宽 291、高 1，且紧贴上下 GroupBox。"""
        from control_wangbo.CellSorting_ui import Ui_Single_Cell_Sorting

        widget = QtWidgets.QWidget()
        ui = Ui_Single_Cell_Sorting()
        ui.setupUi(widget)

        # 1) 左列 7 个 GroupBox 与对应 6 条分隔线的顺序锁定。
        column_groups = [
            ui.grp_hardvareConnection,
            ui.grp_hardvareConnection_sCMOS,
            ui.grp_hardvareConnection_2,
            ui.grp_releaseROI,
            ui.grp_captureROI,
            ui.grp_trappedROI,
            ui.grp_collectedCellsROI,
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

    def test_flow_rate_detection_widgets_removed(self):
        """Flow Rate Detection ROI 模块已整体删除：相关控件与分隔线均不存在。"""
        from control_wangbo.CellSorting_ui import Ui_Single_Cell_Sorting

        widget = QtWidgets.QWidget()
        ui = Ui_Single_Cell_Sorting()
        ui.setupUi(widget)

        removed = [
            "grp_flowRateDetection",
            "grp_releaseROIView_2",
            "line_23",
            "spb_flowRateROI_X",
            "spb_flowRateROI_Y",
            "spb_flowRateROI_width",
            "spb_flowRateROI_height",
            "spb_flowRateDetectFramesNumber",
            "spb_flowRateValue",
            "spb_cellSpeedValue",
            "cmb_objective",
            "spb_chipChannel_width",
            "spb_chipChannel_height",
            "btn_flowRateROI_view",
            "btn_flowRateImageProcessing",
            "btn_enterSettingPara_flowRateDetection",
            "lb_flowRateDetectROIView_original_start",
            "lb_flowRateROI_state",
        ]
        for name in removed:
            self.assertFalse(hasattr(ui, name), f"{name} should have been removed")

    def test_cellsorting_background_diff_collected_label_aligns_with_roi(self):
        """Fast Camera 背景差分区不再显示 Sort 标签，Collected 标签与 ROI 图像居中对齐。"""
        from control_wangbo.CellSorting_ui import Ui_Single_Cell_Sorting

        widget = QtWidgets.QWidget()
        ui = Ui_Single_Cell_Sorting()
        ui.setupUi(widget)
        widget.show()
        self.app.processEvents()

        self.assertFalse(hasattr(ui, "lb_cameraState_48"))
        self.assertEqual(ui.lb_cameraState_49.text(), "Collected")
        self.assertEqual(ui.lb_cameraState_49.alignment(), QtCore.Qt.AlignCenter)
        self.assertEqual(ui.lb_cameraState_49.geometry().width(), ui.lb_collectedROIView_BgDiff_Bi.geometry().width())
        self.assertEqual(
            ui.lb_cameraState_49.geometry().center().x(),
            ui.lb_collectedROIView_BgDiff_Bi.geometry().center().x(),
        )

    def test_cellsorting_collected_roi_has_no_angle_control(self):
        """Collected ROI no longer exposes rotation controls."""
        from control_wangbo.CellSorting_ui import Ui_Single_Cell_Sorting

        widget = QtWidgets.QWidget()
        ui = Ui_Single_Cell_Sorting()
        ui.setupUi(widget)
        widget.show()
        self.app.processEvents()

        self.assertFalse(hasattr(ui, "label_collectedROI_angle"))
        self.assertFalse(hasattr(ui, "spb_collectedROI_angle"))
        labels = [
            ui.label_53,
            ui.label_54,
            ui.label_55,
            ui.label_56,
        ]
        spinboxes = [
            ui.spb_collectedROI_X,
            ui.spb_collectedROI_Y,
            ui.spb_collectedROI_width,
            ui.spb_collectedROI_height,
        ]
        for label in labels:
            self.assertEqual(label.geometry().size(), QtCore.QSize(50, 15))
        for spinbox in spinboxes:
            self.assertEqual(spinbox.geometry().size(), QtCore.QSize(50, 25))
        self.assertEqual(ui.btn_collectedROI_view.geometry().size(), QtCore.QSize(55, 25))
        for label, spinbox in zip(labels, spinboxes):
            self.assertEqual(label.geometry().center().x(), spinbox.geometry().center().x())
        self.assertGreater(ui.btn_collectedROI_view.geometry().x(), ui.spb_collectedROI_height.geometry().x())

    def test_sim_test_capture_root_lives_under_data_directory(self):
        from sim_control.gui import TEST_CAPTURE_ROOT

        self.assertEqual(TEST_CAPTURE_ROOT, PROJECT_ROOT / "data" / "test_captures")

    def test_centered_combobox_text_area_click_opens_popup(self):
        """Clicking the read-only centered text area should toggle the combo popup."""
        from sim_control.gui import configure_centered_combobox

        class PopupStateView:
            def __init__(self, combo):
                self._combo = combo

            def isVisible(self):
                return self._combo.popup_visible

        class PopupTrackingCombo(QtWidgets.QComboBox):
            def __init__(self):
                super().__init__()
                self.popup_count = 0
                self.hide_count = 0
                self.popup_visible = False
                self._popup_state_view = PopupStateView(self)

            def view(self):
                return self._popup_state_view

            def showPopup(self):
                self.popup_count += 1
                self.popup_visible = True

            def hidePopup(self):
                self.hide_count += 1
                self.popup_visible = False

        combo = PopupTrackingCombo()
        try:
            combo.addItems(["A", "B"])
            configure_centered_combobox(combo)
            combo.resize(200, 36)
            combo.show()
            self.app.processEvents()

            line_edit = combo.lineEdit()
            self.assertIsNotNone(line_edit)
            QtTest.QTest.mousePress(
                line_edit,
                QtCore.Qt.LeftButton,
                pos=line_edit.rect().center(),
            )
            self.app.processEvents()
            self.assertEqual(combo.popup_count, 0)

            QtTest.QTest.mouseRelease(
                line_edit,
                QtCore.Qt.LeftButton,
                pos=line_edit.rect().center(),
            )
            self.app.processEvents()

            self.assertEqual(combo.popup_count, 1)
            self.assertEqual(combo.hide_count, 0)
            self.assertTrue(combo.popup_visible)

            QtTest.QTest.mousePress(
                line_edit,
                QtCore.Qt.LeftButton,
                pos=line_edit.rect().center(),
            )
            self.app.processEvents()

            QtTest.QTest.mouseRelease(
                line_edit,
                QtCore.Qt.LeftButton,
                pos=line_edit.rect().center(),
            )
            self.app.processEvents()

            self.assertEqual(combo.popup_count, 1)
            self.assertEqual(combo.hide_count, 1)
            self.assertFalse(combo.popup_visible)
        finally:
            combo.close()

    def test_centered_combobox_configuration_is_idempotent(self):
        """Reconfiguring centered combos must not accumulate popup filters."""
        from sim_control.gui import _ComboLineEditPopupFilter, configure_centered_combobox

        combo = QtWidgets.QComboBox()
        try:
            combo.addItems(["A", "B"])
            configure_centered_combobox(combo)
            configure_centered_combobox(combo)

            self.assertEqual(len(combo.findChildren(_ComboLineEditPopupFilter)), 1)
        finally:
            combo.close()

    def test_centered_combobox_text_selection_keeps_index_and_data_in_sync(self):
        """Index-based combo selection prevents stale currentData on centered combos."""
        from sim_control.gui import configure_centered_combobox, set_combobox_current_text

        combo = QtWidgets.QComboBox()
        combo.addItem("A", "data-a")
        combo.addItem("B", "data-b")
        configure_centered_combobox(combo)

        self.assertTrue(set_combobox_current_text(combo, "B"))

        self.assertEqual(combo.currentText(), "B")
        self.assertEqual(combo.currentIndex(), 1)
        self.assertEqual(combo.currentData(), "data-b")

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

    def test_sim_control_window_propagates_machine_red_identity_to_controller(self):
        import tempfile

        from sim_control.config_store import save_app_config
        from sim_control.gui import SimControlWindow
        from sim_control.models import AppConfig, BackendConfig

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "sim_config.json"
            save_app_config(
                AppConfig(
                    backend=BackendConfig(simulation_mode=True),
                    red_laser_nm=647,
                    selected_laser_nm=647,
                ),
                config_path,
            )
            window = SimControlWindow(config_path=str(config_path))
            try:
                self.assertEqual(window.controller.red_laser_nm, 647)
                window.config.red_laser_nm = 638
                window._sync_config_from_widgets()
                self.assertEqual(window.controller.red_laser_nm, 638)
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

    def test_sim_control_window_initialize_camera_runs_in_worker_thread(self):
        """``_initialize_camera`` 必须在 worker 线程调 controller.initialize_camera，不阻塞 GUI 线程。"""
        import tempfile
        import threading
        import time

        from sim_control.config_store import save_app_config
        from sim_control.gui import SimControlWindow
        from sim_control.models import AppConfig, BackendConfig

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "sim_config.json"
            save_app_config(AppConfig(backend=BackendConfig(simulation_mode=True)), config_path)
            window = SimControlWindow(config_path=str(config_path))
            original_controller = window.controller
            call_thread_idents = []
            controller = SimpleNamespace(
                initialize_camera=mock.Mock(side_effect=lambda: call_thread_idents.append(threading.get_ident())),
            )
            window.controller = controller

            try:
                window._initialize_camera()
                # worker 完成后 _on_cam_init_finished（GUI 线程 queued slot）清理 thread 引用。
                deadline = time.monotonic() + 5.0
                while window._cam_init_thread is not None and time.monotonic() < deadline:
                    QtTest.QTest.qWait(10)
                self.assertIsNone(window._cam_init_thread)
                self.assertIsNone(window._cam_init_worker)
            finally:
                window.controller = original_controller
                window.close()

        controller.initialize_camera.assert_called_once()
        # 必须在非 GUI 线程执行（阻塞的 DCAM 初始化不冻结界面）。
        self.assertEqual(len(call_thread_idents), 1)
        self.assertNotEqual(call_thread_idents[0], threading.get_ident())

    def test_sim_control_window_program_patterns_runs_snapshot_in_worker_thread(self):
        """``_program_patterns`` 在 GUI 线程快照 pattern_files，worker 线程调 prepare_patterns。"""
        import tempfile
        import threading
        import time

        from sim_control.config_store import save_app_config
        from sim_control.gui import SimControlWindow
        from sim_control.models import AppConfig, BackendConfig

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "sim_config.json"
            save_app_config(AppConfig(backend=BackendConfig(simulation_mode=True)), config_path)
            window = SimControlWindow(config_path=str(config_path))
            original_controller = window.controller
            call_records = []
            controller = SimpleNamespace(
                prepare_patterns=mock.Mock(
                    side_effect=lambda files: call_records.append((threading.get_ident(), list(files)))
                ),
            )
            window.controller = controller

            try:
                window._program_patterns()
                deadline = time.monotonic() + 5.0
                while window._program_patterns_thread is not None and time.monotonic() < deadline:
                    QtTest.QTest.qWait(10)
                self.assertIsNone(window._program_patterns_thread)
                self.assertIsNone(window._program_patterns_worker)
                expected_files = list(window.config.pattern_files)
            finally:
                window.controller = original_controller
                window.close()

        controller.prepare_patterns.assert_called_once()
        self.assertEqual(len(call_records), 1)
        worker_ident, received_files = call_records[0]
        # 1) 非 GUI 线程执行（阻塞的 SLM 烧录不冻结界面）。
        self.assertNotEqual(worker_ident, threading.get_ident())
        # 2) worker 收到的是 GUI 线程快照的 9 个 pattern 路径列表。
        self.assertEqual(received_files, expected_files)
        self.assertEqual(len(received_files), 9)

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
                is_busy=False,
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

    def test_sim_control_window_handles_acquisition_summary_without_raw_stack(self):
        """GUI 状态槽只消费轻量 summary，不要求拿到 ``AcquisitionBatch.stack``。"""
        import tempfile

        from sim_control.config_store import save_app_config
        from sim_control.gui import SimControlWindow
        from sim_control.models import AppConfig, BackendConfig

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "sim_config.json"
            save_app_config(AppConfig(backend=BackendConfig(simulation_mode=True)), config_path)
            window = SimControlWindow(config_path=str(config_path))
            try:
                summary = {
                    "task_id": "summary-task",
                    "stack_shape": [9, 16, 20],
                    "stack_dtype": "uint16",
                    "metadata": {"running_order_name": "488_3.5_2d_10ms"},
                }

                window._handle_acquisition_ready(summary)

                self.assertEqual(window.pipeline_labels["task_id"].text(), "summary-task")
                self.assertEqual(window.pipeline_labels["stack_shape"].text(), "[9, 16, 20]")
                self.assertEqual(window.pipeline_labels["stack_dtype"].text(), "uint16")
                self.assertEqual(window.pipeline_labels["reconstruction"].text(), "Running")
            finally:
                window.close()

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

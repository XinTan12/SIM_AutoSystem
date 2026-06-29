import importlib
import sys
import types
import unittest
from pathlib import Path

from PyQt5 import QtCore, QtWidgets


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTROL_ROOT = PROJECT_ROOT / "control_wangbo"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(CONTROL_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_ROOT))

sys.modules.setdefault("MCUTriggerThread", types.ModuleType("MCUTriggerThread"))
sys.modules.setdefault("mvsdk", types.ModuleType("mvsdk"))
sys.modules.setdefault("FastCameraThread", types.ModuleType("FastCameraThread"))


class SimFooterHarmonizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_four_bottom_widgets_share_height_24_and_top_y_486(self):
        from control_wangbo.CellSorting_ui import Ui_Single_Cell_Sorting

        host = QtWidgets.QWidget()
        ui = Ui_Single_Cell_Sorting()
        ui.setupUi(host)

        # 灰阶上限容器是 pyuic5 自动命名（layoutWidgetNN）的布局宿主，名字会随 .ui 改动
        # 漂移；通过其稳定子控件 spb_sCMOS_displayGray_max / label_79 的 parentWidget 定位，
        # 避免硬编码漂移自动名（旧写法 ui.layoutWidget27 会在容器重排后指向别的控件）。
        gray_max_host = ui.spb_sCMOS_displayGray_max.parentWidget()
        self.assertIsNotNone(gray_max_host, "spb_sCMOS_displayGray_max 缺少父容器")
        self.assertIs(
            ui.label_79.parentWidget(),
            gray_max_host,
            "label_79 / spb_sCMOS_displayGray_max 应同属 footer 灰阶上限容器",
        )

        widgets = [
            ui.lb_sCMOS_FPSshow,
            ui.lb_FPSshow_7,
            ui.chb_sCMOS_autoContrast,
            ui.lbl_z_position_label,
            ui.lbl_z_position_value,
            gray_max_host,
            ui.label_79,
            ui.spb_sCMOS_displayGray_max,
        ]
        for widget in widgets:
            self.assertEqual(widget.geometry().height(), 24, widget.objectName())

        outer = (
            ui.lb_sCMOS_FPSshow,
            ui.lb_FPSshow_7,
            ui.chb_sCMOS_autoContrast,
            ui.lbl_z_position_label,
            ui.lbl_z_position_value,
            gray_max_host,
        )
        for widget in outer:
            self.assertEqual(widget.geometry().y(), 486, widget.objectName())

        rects = [widget.geometry() for widget in outer]
        for index, rect in enumerate(rects):
            for other in rects[index + 1 :]:
                self.assertFalse(rect.intersects(other))

    def test_sim_camera_settings_host_resolvable_via_gridlayout_parent(self):
        """护栏：main.py 用 gridLayout_32.parentWidget() 取 SIM Camera Settings 宿主。

        pyuic5 自动名漂移曾使 main.py 硬编码的 layoutWidget28 失效（恒为 None），
        静默打断采集波长下拉 cmb_sCMOS_laser 的创建。此处直接验证 main.py 改用的
        稳定取法不会再退化为 None，从而守住该回归。
        """
        from control_wangbo.CellSorting_ui import Ui_Single_Cell_Sorting

        host = QtWidgets.QWidget()
        ui = Ui_Single_Cell_Sorting()
        ui.setupUi(host)

        grid = getattr(ui, "gridLayout_32", None)
        self.assertIsNotNone(grid, "gridLayout_32 缺失；SIM Camera Settings 宿主无法定位")
        sim_camera_host = grid.parentWidget()
        self.assertIsNotNone(
            sim_camera_host,
            "gridLayout_32.parentWidget() 为 None；main.py 波长下拉宿主获取会失效",
        )
        # 把护栏从“宿主非 None”升级为“宿主确属 SIM Camera Settings 组”：仅非 None
        # 不足以保证 .ui 漂移后 gridLayout_32 仍挂在正确容器上。
        self.assertIs(
            sim_camera_host.parentWidget(),
            ui.grp_hardvareConnection_2,
            "gridLayout_32 宿主应位于 SIM Camera Settings 组 grp_hardvareConnection_2 内",
        )
        # 波长标签/下拉现已由 CellSorting_ui 静态放进 (2,1)/(3,1)；显式化该占用，
        # 一旦 .ui 后续移走这两格导致波长下拉丢失，测试会先于运行期报警。
        item_2_1 = grid.itemAtPosition(2, 1)
        item_3_1 = grid.itemAtPosition(3, 1)
        self.assertIsNotNone(item_2_1, "gridLayout_32 (2,1) 应静态放波长标签 lb_sCMOS_laser")
        self.assertIsNotNone(item_3_1, "gridLayout_32 (3,1) 应静态放波长下拉 cmb_sCMOS_laser")
        self.assertIs(item_2_1.widget(), ui.lb_sCMOS_laser)
        self.assertIs(item_3_1.widget(), ui.cmb_sCMOS_laser)

    def test_static_sim9_button_geometry_settings_font_and_wiring(self):
        """SIM9 按钮由 CellSorting_ui 静态定义（setupUi 即创建、几何到位），
        _build_... 仅运行时接线字体与信号；按钮不越界、点击触发采集。"""
        from control_wangbo.CellSorting_ui import Ui_Single_Cell_Sorting

        legacy_main = importlib.import_module("control_wangbo.main")

        class RuntimeControlHost(QtWidgets.QWidget):
            def __init__(self):
                super().__init__()
                self.ui = Ui_Single_Cell_Sorting()
                self.ui.setupUi(self)
                self.raw_acquire_calls = []
                # _build_... 现按本机红光（默认 638）填充波长下拉并接红光切换下拉。
                self.sim_app_config = types.SimpleNamespace(red_laser_nm=638)

            def _reset_immediate_ro_dropdown(self):
                legacy_main.MainWindow._reset_immediate_ro_dropdown(self)

            def _update_immediate_ro_dropdown_tooltip(self, text=None):
                legacy_main.MainWindow._update_immediate_ro_dropdown_tooltip(self, text)

            def _on_main_red_laser_changed(self, *_args):
                pass

            def on_immediate_ro_changed(self, _index=None):
                pass

            def on_sim_camera_setting_changed(self, *_args):
                pass

            def trigger_sim_raw_9frame_acquisition(self, trigger_source="manual"):
                self.raw_acquire_calls.append(trigger_source)

        host = RuntimeControlHost()
        try:
            # setupUi 静态创建按钮、几何到位（接线之前即成立）。
            button = host.ui.btn_sim9_acquire
            self.assertEqual(button.geometry(), QtCore.QRect(1290, 158, 160, 36))

            legacy_main.MainWindow._build_sim_immediate_and_acquire_controls(host)

            # btn_openSimSettings 已随 SIM 设置弹窗移除；btn_sim9_acquire 现用 CellSorting_ui
            # 静态字体（Times New Roman 12pt），不再运行时从已删按钮复制字体。
            self.assertEqual(button.geometry(), QtCore.QRect(1290, 158, 160, 36))
            self.assertEqual(button.font().family(), "Times New Roman")
            self.assertEqual(button.font().pointSize(), 12)
            self.assertLess(
                button.geometry().right(),
                host.ui.grp_simConfiguration.geometry().left(),
            )
            button.click()
            self.assertEqual(host.raw_acquire_calls, ["sim9_button"])
        finally:
            host.close()

    def test_static_sim9_button_parented_into_scroll_content(self):
        """回归：SIM9 按钮父对象必须是滚动内容容器（cellsorting_content_widget），
        而非主窗口——否则缩小窗口时按钮浮出滚动区被裁剪消失（原 bug）。"""
        from control_wangbo.CellSorting_ui import Ui_Single_Cell_Sorting

        legacy_main = importlib.import_module("control_wangbo.main")

        host = QtWidgets.QWidget()
        ui = Ui_Single_Cell_Sorting()
        try:
            _, content_widget = legacy_main.setup_scrollable_cellsorting_ui(host, ui)
            self.assertIs(ui.btn_sim9_acquire.parentWidget(), content_widget)
            # 与历史 UI 控件同属内容容器（对照已知内容控件 grp_rinseChannel）。
            self.assertIs(
                ui.btn_sim9_acquire.parentWidget(),
                ui.grp_rinseChannel.parentWidget(),
            )
        finally:
            host.close()

    def test_static_slm_2x2_layout_and_wiring(self):
        """SLM 连接区 2×2 由 CellSorting_ui 静态定义——第一行 [设备名 110 | Connect SLM 154]、
        第二行 [Refresh 90 | RO 下拉 154，无 "RO:" 标签]；grid spacing(6/4)+边距(6,4,6,4) 静态、
        列宽(col0=110/col1=154)/col2 吸收余量由 _build 运行时补；lbl_SLM_status 移出布局作 layout
        外 hidden 子。先验 setupUi 静态结构（immediateRO 在 (1,1)、status 不在 grid），再验 _build
        接线（列参数、view popup 190+ElideRight、(none) tooltip、信号）。B-1 护栏只用 grid 最小宽
        ≤ 面板宽（控件分占四独立单元格、天然不重叠）；不再硬约束 grp.minimumSizeHint ≤ 面板宽——它
        含 QGroupBox 装饰开销、跨机字体度量波动（284/299/468）、作阈值不可靠。B-2 护栏：view 190 +
        ElideRight 不越界。"""
        from control_wangbo.CellSorting_ui import Ui_Single_Cell_Sorting

        legacy_main = importlib.import_module("control_wangbo.main")
        activated_calls = []

        class RuntimeControlHost(QtWidgets.QWidget):
            def __init__(self):
                super().__init__()
                self.ui = Ui_Single_Cell_Sorting()
                self.ui.setupUi(self)
                # _build_... 现按本机红光（默认 638）填充波长下拉并接红光切换下拉。
                self.sim_app_config = types.SimpleNamespace(red_laser_nm=638)

            def _reset_immediate_ro_dropdown(self):
                legacy_main.MainWindow._reset_immediate_ro_dropdown(self)

            def _update_immediate_ro_dropdown_tooltip(self, text=None):
                legacy_main.MainWindow._update_immediate_ro_dropdown_tooltip(self, text)

            def _on_main_red_laser_changed(self, *_args):
                pass

            def on_immediate_ro_changed(self, index=None):
                activated_calls.append(index)

            def on_sim_camera_setting_changed(self, *_args):
                pass

            def trigger_sim_raw_9frame_acquisition(self, *_args):
                pass

        host = RuntimeControlHost()
        try:
            grid = host.ui.gridLayout_SLMConnection
            # 静态结构（setupUi 后、_build 之前即成立，防“运行时重排回退”漏检）：
            # immediateRO 已静态在 (1,1)、lbl_SLM_status 移出布局（layout 外 hidden 子）。
            self.assertIs(grid.itemAtPosition(1, 1).widget(), host.ui.cmb_SLM_immediateRO)
            self.assertEqual(grid.indexOf(host.ui.lbl_SLM_status), -1)

            legacy_main.MainWindow._build_sim_immediate_and_acquire_controls(host)
            grid.activate()
            self.app.processEvents()

            # 1) 间距 / 边距 / 列宽 / 列伸缩（2×2：col0=110 设备/Refresh、col1=154 Connect/RO）。
            self.assertEqual(grid.horizontalSpacing(), 6)
            self.assertEqual(grid.verticalSpacing(), 4)
            margins = grid.contentsMargins()
            self.assertEqual(
                (margins.left(), margins.top(), margins.right(), margins.bottom()),
                (6, 4, 6, 4),
            )
            self.assertEqual(grid.columnMinimumWidth(0), 110)
            self.assertEqual(grid.columnMinimumWidth(1), 154)
            self.assertEqual(grid.columnStretch(0), 0)
            self.assertEqual(grid.columnStretch(1), 0)
            self.assertEqual(grid.columnStretch(2), 1)  # 余量留最右空列

            # 2) 2×2 控件位置：device(0,0) / connect(0,1) / refresh(1,0) / RO(1,1)。
            self.assertIs(grid.itemAtPosition(0, 0).widget(), host.ui.cmb_SLM_device)
            self.assertIs(grid.itemAtPosition(0, 1).widget(), host.ui.btn_SLM_connection)
            self.assertIs(grid.itemAtPosition(1, 0).widget(), host.ui.btn_SLM_refresh)
            self.assertIs(grid.itemAtPosition(1, 1).widget(), host.ui.cmb_SLM_immediateRO)

            # 3) 不再有 "RO:" 文字标签；隐藏的 lbl_SLM_status 已移出布局（控件本身仍存在）。
            self.assertFalse(hasattr(host.ui, "lbl_SLM_immediateRO"), "不应再创建 RO 文字标签")
            self.assertTrue(hasattr(host.ui, "lbl_SLM_status"))
            self.assertEqual(grid.indexOf(host.ui.lbl_SLM_status), -1, "状态标签应移出栅格布局")

            # 4) 字体 / 固定尺寸：设备缩短 110、Connect 154、Refresh 不变 90、RO 154。
            self.assertEqual(host.ui.cmb_SLM_immediateRO.font().family(), "Times New Roman")
            self.assertEqual(host.ui.cmb_SLM_immediateRO.font().pointSize(), 12)
            self.assertEqual(host.ui.cmb_SLM_device.minimumWidth(), 110)
            self.assertEqual(host.ui.cmb_SLM_device.maximumWidth(), 110)
            self.assertEqual(host.ui.btn_SLM_connection.minimumWidth(), 154)
            self.assertEqual(host.ui.btn_SLM_connection.maximumWidth(), 154)
            self.assertEqual(host.ui.btn_SLM_refresh.minimumWidth(), 90)
            self.assertEqual(host.ui.btn_SLM_refresh.maximumWidth(), 90)
            self.assertEqual(host.ui.cmb_SLM_immediateRO.minimumWidth(), 154)
            self.assertEqual(host.ui.cmb_SLM_immediateRO.maximumWidth(), 154)
            self.assertEqual(host.ui.cmb_SLM_immediateRO.maximumHeight(), 25)

            # 5) B-1 护栏：grid 内容最小宽 ≤ 面板宽（守「内容不压缩 → 不重叠」真根因；控件分占
            #    (0,0)/(0,1)/(1,0)/(1,1) 四独立单元格、天然不重叠，见上 itemAtPosition）。不再硬约束
            #    grp.minimumSizeHint ≤ panel_w——它含 QGroupBox 标题/边框/样式固有装饰开销，跨机字体
            #    度量波动（实测 284/299/468），作硬阈值不可靠。
            panel_w = host.ui.layoutWidget_simConfiguration.width()
            self.assertGreaterEqual(panel_w, 280)  # 面板宽合理性，防 .ui 异常收窄使护栏失真
            self.assertLessEqual(grid.minimumSize().width(), panel_w)

            # 6) B-2 护栏：闭合下拉固定 154（见上）；弹出浮层固定 190 以容纳最长 RO 名+滚动条，
            #    上限限死(min==max==190)防再退化为越界自适应；长诊断项 ElideRight，全文走 ToolTipRole。
            self.assertEqual(host.ui.cmb_SLM_immediateRO.view().minimumWidth(), 190)
            self.assertEqual(host.ui.cmb_SLM_immediateRO.view().maximumWidth(), 190)
            self.assertEqual(
                host.ui.cmb_SLM_immediateRO.view().textElideMode(), QtCore.Qt.ElideRight
            )
            self.assertEqual(
                host.ui.cmb_SLM_immediateRO.itemData(0, QtCore.Qt.ToolTipRole),
                "(none)",
            )

            # 7) 下拉 activated 信号仍接 on_immediate_ro_changed。
            host.ui.cmb_SLM_immediateRO.activated.emit(0)
            self.assertEqual(activated_calls, [0])
        finally:
            host.close()

    def test_immediate_ro_dropdown_allows_647_item_for_638_wavelength_only(self):
        """638 nm 找样品可选旧 647 命名 immediate RO；其它波长仍禁用同一项。"""
        legacy_main = importlib.import_module("control_wangbo.main")

        def _populate_for_wavelength(wavelength):
            combo = QtWidgets.QComboBox()
            host = types.SimpleNamespace(
                ui=types.SimpleNamespace(cmb_SLM_immediateRO=combo),
                sim_app_config=types.SimpleNamespace(selected_laser_nm=wavelength),
                sim_immediate_ro_items=[
                    {
                        "name": "647_3.5_2d_imm_f1",
                        "index": 17,
                        "parsed_wavelength_nm": 647,
                        "is_immediate": True,
                    }
                ],
                _update_immediate_ro_dropdown_tooltip=lambda _text=None: None,
            )
            legacy_main.MainWindow.refresh_immediate_ro_dropdown(host)
            return combo

        combo_638 = _populate_for_wavelength(638)
        self.assertEqual(combo_638.itemText(1), "647_3.5_2d_imm_f1")
        self.assertTrue(combo_638.model().item(1).isEnabled())
        combo_638.setCurrentIndex(1)
        self.assertEqual(combo_638.currentData(), 17)

        combo_561 = _populate_for_wavelength(561)
        self.assertIn("(λ=647)", combo_561.itemText(1))
        self.assertFalse(combo_561.model().item(1).isEnabled())
        combo_561.setCurrentIndex(1)
        self.assertIsNone(combo_561.currentData())

    def test_static_laser_dropdown_populated_and_single_callback(self):
        """波长下拉由 CellSorting_ui 静态定义；_build 填充 405/488/561/638（带 itemData）、
        选 488、接信号（_build 仅 UI_Init 调一次）。验证 count==4、填充期 blockSignals 不触发回调、
        单次切换只回调一次（无重复 connect）。"""
        from control_wangbo.CellSorting_ui import Ui_Single_Cell_Sorting

        legacy_main = importlib.import_module("control_wangbo.main")
        change_calls = []

        class RuntimeControlHost(QtWidgets.QWidget):
            def __init__(self):
                super().__init__()
                self.ui = Ui_Single_Cell_Sorting()
                self.ui.setupUi(self)
                # 638 机器：supported_lasers_for(638) == (405, 488, 561, 638)，与下方断言一致。
                self.sim_app_config = types.SimpleNamespace(red_laser_nm=638)

            def _reset_immediate_ro_dropdown(self):
                legacy_main.MainWindow._reset_immediate_ro_dropdown(self)

            def _update_immediate_ro_dropdown_tooltip(self, text=None):
                legacy_main.MainWindow._update_immediate_ro_dropdown_tooltip(self, text)

            def _on_main_red_laser_changed(self, *_args):
                pass

            def on_immediate_ro_changed(self, _index=None):
                pass

            def on_sim_camera_setting_changed(self, *_args):
                change_calls.append(1)

            def trigger_sim_raw_9frame_acquisition(self, *_args):
                pass

        host = RuntimeControlHost()
        try:
            legacy_main.MainWindow._build_sim_immediate_and_acquire_controls(host)
            combo = host.ui.cmb_sCMOS_laser
            self.assertEqual(combo.count(), 4)
            self.assertEqual(
                [combo.itemData(i) for i in range(combo.count())],
                [405, 488, 561, 638],
            )
            self.assertEqual(combo.currentText(), "488")
            # 填充期 blockSignals，_build 不应触发 on_sim_camera_setting_changed。
            self.assertEqual(change_calls, [])
            # 单次用户切换只回调一次（无重复 connect）。
            combo.setCurrentIndex(0)
            self.assertEqual(len(change_calls), 1)
        finally:
            host.close()


if __name__ == "__main__":
    unittest.main()

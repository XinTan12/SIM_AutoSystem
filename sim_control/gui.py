"""独立 SIM 采集 GUI（``SimControlWindow``）与共享的模块级 SIM 配置 / DAQ helper。

作用：
    - ``SimControlWindow``：独立 SIM 主窗口。承载 DAQ 设置、相机/曝光/ROI、9 帧 pattern
      槽位、激光选择、采集控制、Pipeline 状态显示与滚动日志，供
      ``python -m sim_control.sim_acquisition_app`` 入口单独调试 SIM 采集链路。
    - 模块级 helper：DAQ 线位下拉读写（``read_daq_config_from_line_combos`` /
      ``populate_daq_line_combos``）、Recon 控件读写（``read/write_reconstruction_config_*``）、
      ``clone_app_config`` 等，供 ``SimControlWindow`` 与 ``control_wangbo`` 主界面复用。

历史：
    SIM 设置弹窗 ``SimSettingsDialog`` 的 DAQ / Recon 两页已于 2026-06-26 迁入
    ``control_wangbo`` 主界面（一级 DAQ / Recon 模块），弹窗整体移除；其 DAQ 诊断测试
    逻辑抽到 ``sim_control/daq_testing.py``（``DaqTestRunner`` + ``_PulseTestWorker``），
    本文件从该模块重导出测试常量 / worker / 结果 dataclass，保 ``SimControlWindow`` 与
    既有 ``from sim_control.gui import ...`` 调用点不破。

协作关系：
    上游：``sim_control/sim_acquisition_app.py``、``control_wangbo/main.py``。
    下游：``adapters.*``、``sim_adapters.*``、``controller.SimAcquisitionController``、
          ``pipeline.*``、``config_store``、``models.*``、``daq_testing.*``。

维护要点：
    - 不要在本文件直接调用厂商 SDK；所有硬件动作都通过 adapter / controller。
    - 新的 SIM DAQ 测试逻辑改动一律走 ``sim_control/daq_testing.py``，不要再写进 GUI 类。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import copy
import functools
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
import traceback
from typing import Any

import numpy as np

from PyQt5.QtCore import QEvent, QEventLoop, QObject, Qt, QThread, QTimer, pyqtSignal, pyqtSlot
from PyQt5.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QProgressBar,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
    QComboBox,
)
import tifffile

from .adapter_factory import (
    create_camera_adapter_for_backend,
    create_daq_adapter_for_backend,
    create_slm_adapter_for_backend,
)
from .adapters import (
    HardwareError,
    NIDaqAdapter,
    R11_ACTIVATION_STATE_ACTIVE,
    find_best_running_order,
)
from .config_store import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_RECONSTRUCTION_OUTPUT_DIR,
    app_config_from_dict,
    app_config_to_dict,
    load_app_config,
    save_app_config,
    validate_app_config,
)
from .controller import SimAcquisitionController
from .models import (
    AppConfig,
    CameraConfig,
    DAQ_ROLE_ORDER,
    DaqLineConfig,
    ReconstructionConfig,
    SimTaskConfig,
    SUPPORTED_LASERS,
    TimingConfig,
    default_daq_line_name,
)
from .pipeline import DecisionEngine, FeatureWorker, ReconstructionWorker
from .protocols import CameraAdapter, SlmAdapter
from .led_indicator import LedIndicator
from .waveform import NIDaqWaveformBuilder, parse_line_name, validate_daq_line_config
from .daq_testing import (
    DAQ_PULSE_TEST_ROLES,
    ROLE_LABELS,
    SIM_ACQUISITION_TEST_EXPOSURE_US,
    SIM_ACQUISITION_TEST_ID,
    SIM_ACQUISITION_TEST_INTER_FRAME_GAP_US,
    SLM_ACTIVATION_TIMING_TEST_ID,
    SLM_ACTIVATION_TIMING_TIMEOUT_S,
    TEST_CAPTURE_ROOT,
    DaqTestRunner,
    SimAcquisitionTestResult,
    SlmActivationTimingTestResult,
    _PulseTestWorker,
    build_daq_test_target_items,
)


logger = logging.getLogger(__name__)


# GUI 槽函数统一捕获异常并显示到状态区，避免 PyQt 回调静默失败。
def _catch_to_error(method):
    """装饰器：把被装饰方法内部的异常打到 GUI 错误标签上。

    用途：
        PyQt 默认会把槽函数里的异常吞掉只打印 traceback，用户看不到出错原因。
        本装饰器统一把 ``except`` 路径接到 ``self._set_error(...)``，让用户能立即
        看到失败原因。
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        # 1) 正常路径：直接转发参数返回结果。
        try:
            return method(self, *args, **kwargs)
        except Exception as exc:
            # 2) 失败路径：仅把"异常文本"喂给错误标签，避免影响其它槽。
            self._set_error(str(exc))
    return wrapper


def read_timing_config_from_widgets(
    spin_sample_rate: QSpinBox,
    spin_edge_pulse_us: QSpinBox,
    spin_inter_frame_gap_us: QSpinBox,
    spin_slm_enable_guard_us: QSpinBox,
) -> TimingConfig:
    """从 4 个 SpinBox 控件构造 ``TimingConfig`` 数据类。"""
    # 4 个字段一一对应；构造时不做范围校验（``QSpinBox`` 自身已限制范围）。
    return TimingConfig(
        sample_rate_hz=spin_sample_rate.value(),
        edge_pulse_us=spin_edge_pulse_us.value(),
        inter_frame_gap_us=spin_inter_frame_gap_us.value(),
        slm_enable_guard_us=spin_slm_enable_guard_us.value(),
    )


def write_timing_config_to_widgets(
    config: TimingConfig,
    spin_sample_rate: QSpinBox,
    spin_edge_pulse_us: QSpinBox,
    spin_inter_frame_gap_us: QSpinBox,
    spin_slm_enable_guard_us: QSpinBox,
) -> None:
    """把 ``TimingConfig`` 数据类写回到 4 个 SpinBox 控件。"""
    spin_sample_rate.setValue(config.sample_rate_hz)
    spin_edge_pulse_us.setValue(config.edge_pulse_us)
    spin_inter_frame_gap_us.setValue(config.inter_frame_gap_us)
    spin_slm_enable_guard_us.setValue(config.slm_enable_guard_us)


def read_selected_laser_nm(laser_group: QButtonGroup) -> int:
    """从 ``QButtonGroup`` 读取已选中的激光波长（nm）。

    抛出：
        ``ValueError``：未选中任何波长时（``checkedId() <= 0``）。
    """
    # ``QButtonGroup.checkedId`` 在未选中时返回 -1；我们这里把任意非正值统一报错。
    checked = laser_group.checkedId()
    if checked <= 0:
        raise ValueError("Please select one laser wavelength.")
    return checked


def write_selected_laser_to_widgets(
    laser_nm: int,
    laser_buttons: dict[int, QRadioButton],
) -> None:
    """根据波长选中对应的 ``QRadioButton``；未知波长回落到 488 nm。"""
    # 缺失波长按"默认 488"处理，保证下次读取不抛 ValueError。
    laser_button = laser_buttons.get(laser_nm, laser_buttons[488])
    laser_button.setChecked(True)


RECON_WAVELENGTHS = SUPPORTED_LASERS


def read_daq_config_from_line_combos(
    line_combos: dict[str, QComboBox],
    device_name: str | None = None,
) -> DaqLineConfig:
    """从 8 个 DAQ 线位下拉框构造 ``DaqLineConfig`` 数据类。

    抛出：
        ``ValueError``：``device_name`` 显式传空字符串、或任一线位下拉为空时。
    """
    # 1) 设备名：显式 None 则后续从第一个线位字符串推断；空白字符串视为非法。
    selected_device_name = None
    if device_name is not None:
        selected_device_name = device_name.strip()
        if not selected_device_name:
            raise ValueError("Please select a DAQ device.")

    # 2) 逐角色读控件文本；空文本立刻报错，附带角色可读标签。
    selections = {}
    for role, combo in line_combos.items():
        value = combo.currentText().strip()
        if not value:
            raise ValueError(f"DAQ line not selected for {ROLE_LABELS[role]}")
        selections[role] = value

    # 3) 未显式给 device_name → 从首个 line 字符串解析出来。
    if selected_device_name is None:
        selected_device_name, _, _ = parse_line_name(next(iter(selections.values())))
    return DaqLineConfig(device_name=selected_device_name, **selections)


class _ComboLineEditPopupFilter(QObject):
    def __init__(self, combo: QComboBox):
        super().__init__(combo)
        self._combo = combo
        self._left_press_started = False
        self._popup_visible_on_press = False

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() in (QEvent.MouseButtonPress, QEvent.MouseButtonDblClick):
            if getattr(event, "button", lambda: None)() == Qt.LeftButton:
                self._left_press_started = self._combo.isEnabled()
                self._popup_visible_on_press = False
                if self._combo.isEnabled():
                    self._combo.setFocus(Qt.MouseFocusReason)
                    view = self._combo.view()
                    self._popup_visible_on_press = bool(view is not None and view.isVisible())
                return True
        if event.type() == QEvent.MouseButtonRelease:
            if getattr(event, "button", lambda: None)() == Qt.LeftButton:
                should_toggle = self._left_press_started and self._combo.isEnabled()
                popup_visible_on_press = self._popup_visible_on_press
                self._left_press_started = False
                self._popup_visible_on_press = False
                if should_toggle:
                    if popup_visible_on_press:
                        QTimer.singleShot(0, self._combo.hidePopup)
                    else:
                        QTimer.singleShot(0, self._combo.showPopup)
                return True
        return super().eventFilter(watched, event)


def _contains_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def _set_language_specific_label_text(label: QLabel, text: str) -> None:
    font = label.font()
    font.setFamily("Microsoft YaHei" if _contains_cjk(text) else "Arial")
    font.setBold(True)
    label.setFont(font)
    label.setText(text)


def _center_combobox_items(combo: QComboBox) -> None:
    for index in range(combo.count()):
        combo.setItemData(index, Qt.AlignCenter, Qt.TextAlignmentRole)


def configure_centered_combobox(combo: QComboBox) -> None:
    combo.setEditable(True)
    combo.setInsertPolicy(QComboBox.NoInsert)
    line_edit = combo.lineEdit()
    if line_edit is not None:
        line_edit.setFont(combo.font())
        line_edit.setReadOnly(True)
        line_edit.setAlignment(Qt.AlignCenter)
        line_edit.setFrame(False)
        for old_filter in combo.findChildren(_ComboLineEditPopupFilter):
            line_edit.removeEventFilter(old_filter)
            old_filter.setParent(None)
            old_filter.deleteLater()
        popup_filter = _ComboLineEditPopupFilter(combo)
        line_edit.installEventFilter(popup_filter)
    _center_combobox_items(combo)


def set_combobox_current_text(combo: QComboBox, text: str) -> bool:
    index = combo.findText(text)
    if index < 0:
        return False
    combo.setCurrentIndex(index)
    return True


def populate_daq_line_combos(
    line_combos: dict[str, QComboBox],
    lines: list[str],
    current_values: dict[str, str],
    config: DaqLineConfig,
    device_name: str,
) -> None:
    """把 ``lines`` 候选写入每个角色下拉，并尽量保留用户已选值。

    选择顺序：
        1. 用户当前文本（current_values）；
        2. 配置文件中的对应字段（``getattr(config, role)``）；
        3. 项目默认线位（``default_daq_line_name``）。
        非该 device 前缀的旧值会被强制替换为默认。
    """
    for role, combo in line_combos.items():
        # 1) 优先目标值：当前控件文本 → 配置字段；为空则后续走 fallback。
        target_value = current_values.get(role) or getattr(config, role)
        # 2) 不属于当前 device 的旧值用项目默认替代，避免用户在跨设备切换后看到陌生线位。
        if target_value and not target_value.startswith(f"{device_name}/"):
            target_value = default_daq_line_name(device_name, role)
        fallback_value = default_daq_line_name(device_name, role)

        # 3) 暂屏蔽信号避免 ``setCurrentText`` 触发 ``currentTextChanged`` 死循环。
        combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItems(lines)
            _center_combobox_items(combo)
            # 4) 命中目标值优先；否则回落到项目默认线位；都不在 lines 则保持下拉首项。
            if target_value in lines:
                set_combobox_current_text(combo, target_value)
            elif fallback_value in lines:
                set_combobox_current_text(combo, fallback_value)
        finally:
            combo.blockSignals(False)


def browse_pattern_file(parent: QWidget, index: int) -> str | None:
    """弹出文件选择对话框，让用户挑选第 ``index+1`` 个 SIM pattern 文件。"""
    # 文件过滤器涵盖位图/PNG/TIFF/JPG/二进制；用户取消时返回 None。
    path, _ = QFileDialog.getOpenFileName(
        parent,
        f"Select Pattern {index + 1}",
        str(Path.cwd()),
        "Pattern Files (*.bmp *.png *.tif *.tiff *.jpg *.jpeg *.bin);;All Files (*.*)",
    )
    return path or None


def clone_app_config(config: AppConfig) -> AppConfig:
    """通过 dict 中转的方式深拷贝 ``AppConfig``。

    用途：
        ``SimSettingsDialog`` 编辑配置时拿到的是外部对象的"独立副本"，避免用户
        点击 ``Cancel`` 时已修改的字段污染调用方持有的 AppConfig。
    """
    # 借助现成的 dict 转换路径完成深拷贝；同时跑一次迁移链以确保 schema 一致。
    return app_config_from_dict(app_config_to_dict(config))


# 独立 SIM 窗口用于单独调试采集链路，集成主界面复用同一套 controller 和 adapter。
class SimControlWindow(QMainWindow):
    """独立 SIM 控制窗口。

    职责：
        - 提供 DAQ 设置、相机 ROI/曝光/timing、9 帧 pattern 槽位、激光选择控件。
        - 暴露"初始化硬件 / 准备实验 / 运行 9 帧采集 / 停止 / 清除"控制按钮。
        - 通过 ``SimAcquisitionController`` 把硬件动作排队到独立 worker 线程。
        - 把 controller 信号转给本窗口的状态显示与日志区。
        - 把采集到的 batch 串到占位 pipeline（``ReconstructionWorker``、
          ``FeatureWorker``、``DecisionEngine``），展示重建/特征/决策状态。

    线程模型：
        - GUI 主线程：本窗口与所有控件。
        - SIM 采集 worker：``SimAcquisitionController`` 内部 QThread。
        - 重建 / 特征 worker：各持一个 QThread。
    """
    # GUI -> 重建线程的跨线程信号（queued connection）：不在 GUI 线程直接改 worker 内部状态。
    signal_reconstruction_warmup_requested = pyqtSignal(int)
    signal_reconstruction_config_changed = pyqtSignal(object)
    signal_reconstruction_shutdown_requested = pyqtSignal()

    def __init__(self, config_path: str | None = None, parent: QWidget | None = None):
        # 1) 调父类构造让 QMainWindow 接管生命周期。
        super().__init__(parent)
        # 2) 加载配置；缺失文件 ``load_app_config`` 会自建默认。
        self.config = load_app_config(config_path or DEFAULT_CONFIG_PATH)
        # 3) 创建采集控制器并接管所有硬件 adapter（真实/仿真由 backend 决定）。
        self.controller = SimAcquisitionController(self.config.backend, self)
        self.controller.z_scan_config = self.config.z_scan
        self.controller.reconstruction_config = self.config.reconstruction
        # 4) 占位决策器单实例就够；不需要独立 QThread。
        self.decision_engine = DecisionEngine()
        # 5) 重建 / 特征各起一个 QThread，让 CPU 重负载不阻塞 GUI。
        self.recon_thread = QThread(self)
        self.recon_worker = ReconstructionWorker(self.config.reconstruction)
        self.recon_worker.moveToThread(self.recon_thread)
        self.recon_thread.start()
        # 跨线程信号连到重建 worker 的 slot（自动 queued，在重建线程执行）。
        self.signal_reconstruction_warmup_requested.connect(self.recon_worker.slot_warmup)
        self.signal_reconstruction_config_changed.connect(self.recon_worker.slot_update_reconstruction_config)
        self.signal_reconstruction_shutdown_requested.connect(self.recon_worker.slot_shutdown)
        self.recon_worker.signal_shutdown_finished.connect(self._on_recon_shutdown_finished)
        # 关闭握手状态：防重入 + 等 drain 完成的本地事件循环（仅在 GUI 线程读写）。
        self._closing = False
        self._recon_shutdown_loop: QEventLoop | None = None
        self._recon_shutdown_done = False
        # 启动后在重建线程做一次预热（焐热 import torch/EMD 与 CUDA 上下文；与形状相关的
        # cuFFT plan 仍按首帧建），不阻塞 GUI 线程；缺 torch/CUDA/OTF 时非致命跳过。
        self.signal_reconstruction_warmup_requested.emit(int(self.config.selected_laser_nm))
        self.feature_thread = QThread(self)
        self.feature_worker = FeatureWorker()
        self.feature_worker.moveToThread(self.feature_thread)
        self.feature_thread.start()

        # 6) 控件容器：建立后由 ``_build_ui`` 填充实例。
        self.line_combos: dict[str, QComboBox] = {}
        self.pattern_edits: list[QLineEdit] = []
        self.laser_buttons: dict[int, QRadioButton] = {}
        self.pipeline_labels: dict[str, QLabel] = {}
        self.hardware_leds: dict[str, LedIndicator] = {}
        self.current_task_id = "-"
        self.current_laser_nm = self.config.selected_laser_nm
        self.sim_last_reconstruction_result = None
        # 初始化硬件 / 相机设置 / 相机初始化 / 图案烧录 worker 状态；None 表示没有正在运行的操作。
        self._hw_init_thread: QThread | None = None
        self._hw_init_worker: _PulseTestWorker | None = None
        self._cam_settings_thread: QThread | None = None
        self._cam_settings_worker: _PulseTestWorker | None = None
        self._cam_init_thread: QThread | None = None
        self._cam_init_worker: _PulseTestWorker | None = None
        self._program_patterns_thread: QThread | None = None
        self._program_patterns_worker: _PulseTestWorker | None = None

        # 7) UI 构造 → 信号连接 → 控件初值填充 → 刷新线位下拉 → 日志记录。
        self._build_ui()
        self._wire_signals()
        self._populate_widgets_from_config(self.config)
        self._refresh_device_lines()
        self._log(f"Loaded config: {self.config.config_path}")

    def _build_ui(self) -> None:
        """搭建本窗口的整体 UI 布局：滚动区域 + 4 组顶层控件 + 控制 / pipeline / 日志区。"""
        self.setWindowTitle("SIM Control Window")
        self.resize(1500, 980)

        # 1) 用 QScrollArea 包裹 container，让小屏幕也能完整显示。
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        container = QWidget()
        root_layout = QVBoxLayout(container)
        root_layout.setSpacing(12)

        # 2) 顶部 2×2 网格：DAQ Wiring / 相机 / Pattern / 激光。
        top_layout = QGridLayout()
        top_layout.addWidget(self._create_daq_group(), 0, 0)
        top_layout.addWidget(self._create_camera_group(), 0, 1)
        top_layout.addWidget(self._create_pattern_group(), 1, 0)
        top_layout.addWidget(self._create_laser_group(), 1, 1)
        root_layout.addLayout(top_layout)
        # 3) 中下三段：采集控制 / pipeline 状态 / 滚动日志。
        root_layout.addWidget(self._create_control_group())
        root_layout.addWidget(self._create_pipeline_group())
        root_layout.addWidget(self._create_log_group())
        root_layout.addStretch(1)

        scroll.setWidget(container)
        self.setCentralWidget(scroll)

    def _create_daq_group(self) -> QGroupBox:
        """创建 DAQ 线位选择 + 配置读写 + wiring 校验区域。"""
        group = QGroupBox("DAQ Wiring")
        layout = QVBoxLayout(group)
        # 1) 表单：每个 DAQ 角色一个下拉，按 DAQ_ROLE_ORDER 顺序展示。
        form = QFormLayout()
        for role in DAQ_ROLE_ORDER:
            combo = QComboBox()
            combo.setEditable(False)
            self.line_combos[role] = combo
            form.addRow(ROLE_LABELS[role], combo)
        layout.addLayout(form)

        # 2) 按钮行：4 个动作（加载 / 保存配置、刷新 lines、校验 wiring）。
        button_row = QHBoxLayout()
        self.btn_load_config = QPushButton("Load Config")
        self.btn_save_config = QPushButton("Save Config")
        self.btn_refresh_lines = QPushButton("Refresh Device Lines")
        self.btn_validate_wiring = QPushButton("Validate Wiring")
        button_row.addWidget(self.btn_load_config)
        button_row.addWidget(self.btn_save_config)
        button_row.addWidget(self.btn_refresh_lines)
        button_row.addWidget(self.btn_validate_wiring)
        layout.addLayout(button_row)
        return group

    def _create_camera_group(self) -> QGroupBox:
        """创建相机 ROI / 曝光 / 触发模式 / Timing 设置区域。"""
        group = QGroupBox("Fusion BT Camera")
        layout = QVBoxLayout(group)

        # 1) 相机基础参数表单：ROI 4 个 SpinBox + 曝光 + 超时 + 触发模式。
        form = QFormLayout()
        self.spin_roi_x = QSpinBox()
        self.spin_roi_x.setRange(0, 10000)
        self.spin_roi_y = QSpinBox()
        self.spin_roi_y.setRange(0, 10000)
        self.spin_roi_width = QSpinBox()
        self.spin_roi_width.setRange(1, 10000)
        self.spin_roi_height = QSpinBox()
        self.spin_roi_height.setRange(1, 10000)
        self.spin_exposure_us = QSpinBox()
        self.spin_exposure_us.setRange(1, 2_000_000)
        self.spin_timeout_ms = QSpinBox()
        self.spin_timeout_ms.setRange(100, 120_000)
        # ``trigger_mode`` 当前固定 external_level；下拉禁用是为了让 UI 标识"项目只支持这一档"。
        self.combo_trigger_mode = QComboBox()
        self.combo_trigger_mode.addItem("External Level", "external_level")
        self.combo_trigger_mode.setEnabled(False)

        form.addRow("ROI X", self.spin_roi_x)
        form.addRow("ROI Y", self.spin_roi_y)
        form.addRow("ROI Width", self.spin_roi_width)
        form.addRow("ROI Height", self.spin_roi_height)
        form.addRow("Exposure (us)", self.spin_exposure_us)
        form.addRow("Timeout (ms)", self.spin_timeout_ms)
        form.addRow("Trigger Mode", self.combo_trigger_mode)
        layout.addLayout(form)

        # 2) Timing 子组：sample rate / 边沿脉冲 / 帧间隔 / SLM enable guard。
        timing_group = QGroupBox("Timing")
        timing_form = QFormLayout(timing_group)
        self.spin_sample_rate = QSpinBox()
        self.spin_sample_rate.setRange(1_000, 20_000_000)
        self.spin_edge_pulse_us = QSpinBox()
        self.spin_edge_pulse_us.setRange(1, 100_000)
        self.spin_inter_frame_gap_us = QSpinBox()
        self.spin_inter_frame_gap_us.setRange(0, 2_000_000)
        self.spin_slm_enable_guard_us = QSpinBox()
        self.spin_slm_enable_guard_us.setRange(1, 100_000)
        timing_form.addRow("Sample Rate (Hz)", self.spin_sample_rate)
        timing_form.addRow("Edge Pulse (us)", self.spin_edge_pulse_us)
        timing_form.addRow("Inter Frame Gap (us)", self.spin_inter_frame_gap_us)
        timing_form.addRow("SLM Enable Guard (us)", self.spin_slm_enable_guard_us)
        layout.addWidget(timing_group)

        # 3) 相机调试按钮：初始化 / 应用配置 / arm / disarm。
        button_row = QHBoxLayout()
        self.btn_initialize_camera = QPushButton("Initialize Camera")
        self.btn_apply_camera = QPushButton("Apply Camera Settings")
        self.btn_arm_camera = QPushButton("Arm Camera")
        self.btn_disarm_camera = QPushButton("Disarm Camera")
        button_row.addWidget(self.btn_initialize_camera)
        button_row.addWidget(self.btn_apply_camera)
        button_row.addWidget(self.btn_arm_camera)
        button_row.addWidget(self.btn_disarm_camera)
        layout.addLayout(button_row)
        return group

    def _create_pattern_group(self) -> QGroupBox:
        """创建 9 个 pattern 文件槽位 + 手动编程按钮区域（旧调试路径，正式走 Running Order）。"""
        group = QGroupBox("SLM Pattern Preparation")
        layout = QVBoxLayout(group)
        form = QFormLayout()
        # 1) 9 个 pattern 槽位，每个一行：QLineEdit + Browse 按钮。
        for index in range(9):
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            edit = QLineEdit()
            browse = QPushButton("Browse")
            # ``_checked`` 参数用来吸收 clicked 信号自带的 bool；``idx`` 通过 default 参数绑定。
            browse.clicked.connect(lambda _checked=False, idx=index: self._browse_pattern(idx))
            row_layout.addWidget(edit)
            row_layout.addWidget(browse)
            self.pattern_edits.append(edit)
            form.addRow(f"Pattern {index + 1}", row)
        layout.addLayout(form)
        # 2) "Program Patterns" 按钮：手动烧录 9 帧位平面（仅旧调试路径）。
        self.btn_program_patterns = QPushButton("Program Patterns")
        layout.addWidget(self.btn_program_patterns)
        return group

    def _create_laser_group(self) -> QGroupBox:
        """创建 405 / 488 / 561 / 638 nm 激光单选按钮组。"""
        group = QGroupBox("Laser Selection")
        layout = QVBoxLayout(group)
        self.laser_group = QButtonGroup(self)
        # 4 个 RadioButton + QButtonGroup 关联，便于 checkedId() 读取选中波长。
        for wavelength in SUPPORTED_LASERS:
            button = QRadioButton(f"{wavelength} nm")
            self.laser_group.addButton(button, wavelength)
            self.laser_buttons[wavelength] = button
            layout.addWidget(button)
        layout.addStretch(1)
        return group

    def _create_control_group(self) -> QGroupBox:
        """创建硬件初始化 / 准备 / 采集 / 停止控制 + 状态显示区域。"""
        group = QGroupBox("Acquisition Control")
        layout = QGridLayout(group)
        # 1) 5 个动作按钮：初始化硬件 / 准备实验 / 运行 9 帧 / 停止 / 清除结果。
        self.btn_initialize_hardware = QPushButton("Initialize Hardware")
        self.btn_prepare_experiment = QPushButton("Prepare Experiment")
        self.btn_run_acquisition = QPushButton("Run Single 9-Frame Acquisition")
        self.btn_stop = QPushButton("Stop")
        self.btn_clear_result = QPushButton("Clear Result")
        layout.addWidget(self.btn_initialize_hardware, 0, 0)
        layout.addWidget(self.btn_prepare_experiment, 0, 1)
        layout.addWidget(self.btn_run_acquisition, 0, 2)
        layout.addWidget(self.btn_stop, 0, 3)
        layout.addWidget(self.btn_clear_result, 0, 4)

        # 2) 3 个硬件状态 LED：相机 / SLM / DAQ；起始灰色。
        hardware_row = QHBoxLayout()
        for key, label_text in (("camera", "Camera"), ("slm", "SLM"), ("daq", "DAQ")):
            led = LedIndicator("gray")
            self.hardware_leds[key] = led
            hardware_row.addWidget(led)
            hardware_row.addWidget(QLabel(label_text))
        hardware_row.addStretch(1)
        layout.addLayout(hardware_row, 1, 0, 1, 5)

        # 3) 9 帧进度条；frame_captured 信号会推动它。
        self.progress_acquisition = QProgressBar()
        self.progress_acquisition.setRange(0, 9)
        self.progress_acquisition.setValue(0)
        self.progress_acquisition.setFormat("%v/9")
        layout.addWidget(QLabel("Acquisition Progress"), 2, 0)
        layout.addWidget(self.progress_acquisition, 2, 1, 1, 4)

        # 4) 状态文本标签：状态 / 当前帧 / 当前激光 / 当前 pattern / 错误 / task id。
        self.lbl_current_status = QLabel("Idle")
        self.lbl_current_frame = QLabel("-")
        self.lbl_current_laser = QLabel("-")
        self.lbl_current_pattern = QLabel("-")
        self.lbl_current_error = QLabel("-")
        self.lbl_current_task = QLabel("-")
        self.lbl_current_error.setWordWrap(True)

        layout.addWidget(QLabel("Status"), 3, 0)
        layout.addWidget(self.lbl_current_status, 3, 1)
        layout.addWidget(QLabel("Current Frame"), 3, 2)
        layout.addWidget(self.lbl_current_frame, 3, 3)
        layout.addWidget(QLabel("Current Laser"), 4, 0)
        layout.addWidget(self.lbl_current_laser, 4, 1)
        layout.addWidget(QLabel("Current Pattern"), 4, 2)
        layout.addWidget(self.lbl_current_pattern, 4, 3)
        layout.addWidget(QLabel("Task ID"), 5, 0)
        layout.addWidget(self.lbl_current_task, 5, 1, 1, 3)
        layout.addWidget(QLabel("Last Error"), 6, 0)
        layout.addWidget(self.lbl_current_error, 6, 1, 1, 4)
        return group

    def _create_pipeline_group(self) -> QGroupBox:
        """创建重建 / 特征 / 决策 pipeline 状态显示区域（仅文字摘要）。"""
        group = QGroupBox("Pipeline Status")
        form = QFormLayout(group)
        # 6 个固定标签；后续 ``_handle_*`` 槽函数会更新它们的文本。
        for key, label_text in (
            ("task_id", "Task ID"),
            ("stack_shape", "Stack Shape"),
            ("stack_dtype", "Stack Dtype"),
            ("reconstruction", "Reconstruction"),
            ("features", "Feature Extraction"),
            ("decision", "Decision"),
        ):
            label = QLabel("-")
            label.setWordWrap(True)
            self.pipeline_labels[key] = label
            form.addRow(label_text, label)
        return group

    def _create_log_group(self) -> QGroupBox:
        """创建滚动日志区，承接 controller / pipeline 的状态输出。"""
        group = QGroupBox("Log")
        layout = QVBoxLayout(group)
        # 只读 QPlainTextEdit + 1000 行上限，避免长时间运行后内存占用过大。
        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.document().setMaximumBlockCount(1000)
        layout.addWidget(self.log_output)
        return group

    def _wire_signals(self) -> None:
        """把所有按钮 / controller / pipeline worker 的信号连接到本窗口对应槽。"""
        # 1) 按钮 → 本窗口槽。
        self.btn_load_config.clicked.connect(self._load_config_from_disk)
        self.btn_save_config.clicked.connect(self._save_config_to_disk)
        self.btn_refresh_lines.clicked.connect(self._refresh_device_lines)
        self.btn_validate_wiring.clicked.connect(self._validate_wiring)
        self.btn_initialize_camera.clicked.connect(self._initialize_camera)
        self.btn_apply_camera.clicked.connect(self._apply_camera_settings)
        self.btn_arm_camera.clicked.connect(self._arm_camera)
        self.btn_disarm_camera.clicked.connect(self._disarm_camera)
        self.btn_program_patterns.clicked.connect(self._program_patterns)
        self.btn_initialize_hardware.clicked.connect(self._initialize_hardware)
        self.btn_prepare_experiment.clicked.connect(self._prepare_experiment)
        self.btn_run_acquisition.clicked.connect(self._run_single_acquisition)
        self.btn_stop.clicked.connect(self._stop_acquisition)
        self.btn_clear_result.clicked.connect(self._clear_result)

        # 2) Controller 信号 → 本窗口状态/失败/取消/就绪槽。
        self.controller.signal_status_changed.connect(self._handle_status_changed)
        self.controller.signal_acquisition_failed.connect(self._handle_acquisition_failed)
        self.controller.signal_acquisition_cancelled.connect(self._handle_acquisition_cancelled)
        self.controller.signal_acquisition_summary_ready.connect(self._handle_acquisition_ready)
        # 3) Acquisition ready → 触发重建 worker；重建 ready → 触发特征 worker。
        self.controller.signal_acquisition_ready.connect(self.recon_worker.slot_reconstruct)
        self.recon_worker.signal_reconstruction_ready.connect(self._handle_reconstruction_ready)
        self.recon_worker.signal_reconstruction_ready.connect(self.feature_worker.slot_extract)
        self.recon_worker.signal_reconstruction_failed.connect(self._handle_reconstruction_failed)
        self.feature_worker.signal_features_ready.connect(self._handle_feature_ready)
        self.feature_worker.signal_features_failed.connect(self._handle_feature_failed)

    def _populate_widgets_from_config(self, config: AppConfig) -> None:
        """把 AppConfig 中所有影响 UI 的字段一次性写到控件。"""
        # 1) 相机 ROI / 曝光 / 超时。
        self.spin_roi_x.setValue(config.camera.roi_x)
        self.spin_roi_y.setValue(config.camera.roi_y)
        self.spin_roi_width.setValue(config.camera.roi_width)
        self.spin_roi_height.setValue(config.camera.roi_height)
        self.spin_exposure_us.setValue(config.camera.exposure_us)
        self.spin_timeout_ms.setValue(config.camera.timeout_ms)
        # 2) Timing 4 字段。
        write_timing_config_to_widgets(
            config.timing,
            self.spin_sample_rate, self.spin_edge_pulse_us,
            self.spin_inter_frame_gap_us, self.spin_slm_enable_guard_us,
        )
        # 3) 9 个 pattern 文件路径。
        for index, edit in enumerate(self.pattern_edits):
            edit.setText(config.pattern_files[index])
        # 4) 选中波长。
        write_selected_laser_to_widgets(config.selected_laser_nm, self.laser_buttons)

    def _current_daq_config(self) -> DaqLineConfig:
        """读取 DAQ 控件构造 DaqLineConfig；device_name 从首个 line 推断。"""
        return read_daq_config_from_line_combos(self.line_combos)

    def _current_camera_config(self) -> CameraConfig:
        """从相机控件构造 ``CameraConfig`` 数据类。"""
        # 注意 ``combo_trigger_mode.currentData()`` 永远是 ``"external_level"``（下拉只有一项）。
        return CameraConfig(
            roi_x=self.spin_roi_x.value(),
            roi_y=self.spin_roi_y.value(),
            roi_width=self.spin_roi_width.value(),
            roi_height=self.spin_roi_height.value(),
            exposure_us=self.spin_exposure_us.value(),
            timeout_ms=self.spin_timeout_ms.value(),
            trigger_mode=self.combo_trigger_mode.currentData(),
        )

    def _current_timing_config(self) -> TimingConfig:
        """从 4 个 Timing 控件构造 ``TimingConfig``。"""
        return read_timing_config_from_widgets(
            self.spin_sample_rate, self.spin_edge_pulse_us,
            self.spin_inter_frame_gap_us, self.spin_slm_enable_guard_us,
        )

    def _current_pattern_files(self) -> list[str]:
        """读 9 个 pattern QLineEdit 的当前文本（已 strip）。"""
        return [edit.text().strip() for edit in self.pattern_edits]

    def _selected_laser_nm(self) -> int:
        """读取选中波长；未选时抛 ValueError。"""
        return read_selected_laser_nm(self.laser_group)

    def _sync_config_from_widgets(self) -> None:
        """把所有控件值一次性写回 ``self.config``，让后续保存/启动 worker 拿到一致状态。"""
        self.config.daq = self._current_daq_config()
        self.config.camera = self._current_camera_config()
        self.config.timing = self._current_timing_config()
        self.config.pattern_files = self._current_pattern_files()
        self.config.selected_laser_nm = self._selected_laser_nm()
        self.controller.z_scan_config = self.config.z_scan
        self.controller.reconstruction_config = self.config.reconstruction
        # 跨线程下发配置快照（deepcopy），不在 GUI 线程直接改重建 worker 内部状态。
        self.signal_reconstruction_config_changed.emit(self.config.reconstruction.snapshot())

    def _refresh_device_lines(self) -> None:
        """刷新 DAQ 线位下拉：用 controller 拉 lines，缺失时回落到默认 16 line 列表。"""
        # 1) 记下用户当前选择，让 populate 优先保留。
        current_values = {role: combo.currentText() for role, combo in self.line_combos.items()}
        default_device = self.config.daq.device_name
        # 2) 调 controller 拉 lines；失败回落到一个虚构 16 line 列表。
        try:
            lines = self.controller.refresh_available_lines()
        except Exception:
            lines = [f"{default_device}/port0/line{i}" for i in range(16)]
        # 3) 即使 controller 成功但返回空列表，也走 fallback 保证 UI 始终有候选。
        if not lines:
            lines = [f"{default_device}/port0/line{i}" for i in range(16)]

        # 4) 灌入下拉。
        populate_daq_line_combos(
            self.line_combos,
            lines,
            current_values,
            self.config.daq,
            default_device,
        )
        self._log(f"Loaded {len(lines)} DAQ line options.")

    def _validate_wiring(self) -> None:
        """点击 Validate Wiring 按钮：跑 ``validate_daq_line_config``，弹对话框反馈。"""
        try:
            validate_daq_line_config(self._current_daq_config())
            self._log("DAQ wiring validation passed.")
            QMessageBox.information(self, "DAQ Wiring", "DAQ wiring validation passed.")
        except Exception as exc:
            self._set_error(str(exc))
            QMessageBox.critical(self, "DAQ Wiring", str(exc))

    @_catch_to_error
    def _load_config_from_disk(self) -> None:
        """Load Config 按钮：从磁盘重读配置 → 写回控件 → 刷新线位。"""
        self.config = load_app_config(self.config.config_path or DEFAULT_CONFIG_PATH)
        self._populate_widgets_from_config(self.config)
        self.signal_reconstruction_config_changed.emit(self.config.reconstruction.snapshot())
        self._refresh_device_lines()
        self._log(f"Config loaded from {self.config.config_path}")

    @_catch_to_error
    def _save_config_to_disk(self) -> None:
        """Save Config 按钮：先同步 UI → config，再写盘。"""
        self._sync_config_from_widgets()
        path = save_app_config(self.config, self.config.config_path or DEFAULT_CONFIG_PATH)
        self._log(f"Config saved to {path}")

    def _initialize_hardware(self) -> None:
        """Initialize Hardware 按钮：同步 UI → 写 DAQ 配置 → worker 线程初始化相机/SLM。"""
        if self._hw_init_thread is not None:
            return
        self._sync_config_from_widgets()
        try:
            self.controller.apply_daq_config(self.config.daq)
        except Exception as exc:
            self._set_error(str(exc))
            return

        def _fn(stop_event: threading.Event) -> str:
            self.controller.initialize_hardware()
            return "硬件初始化完成。"

        stop_event = threading.Event()
        worker = _PulseTestWorker(_fn, stop_event)
        thread = QThread(self)
        worker.moveToThread(thread)
        worker.signal_success.connect(self._on_hw_init_success)
        worker.signal_error.connect(self._on_hw_init_error)
        worker.signal_finished.connect(self._on_hw_init_finished)
        thread.started.connect(worker.run)
        self._hw_init_worker = worker
        self._hw_init_thread = thread
        thread.start()

    @pyqtSlot(str)
    def _on_hw_init_success(self, message: str) -> None:
        self._log(f"[OK] {message}")

    @pyqtSlot(str)
    def _on_hw_init_error(self, error: str) -> None:
        self._set_error(error)

    @pyqtSlot()
    def _on_hw_init_finished(self) -> None:
        if self._hw_init_thread is not None:
            self._hw_init_thread.quit()
            self._hw_init_thread.wait()
            self._hw_init_thread = None
        self._hw_init_worker = None

    def _initialize_camera(self) -> None:
        """Initialize Camera 按钮：worker 线程只初始化相机，便于排查 DCAM 安装问题。"""
        if self._cam_init_thread is not None:
            return

        def _fn(stop_event: threading.Event) -> str:
            self.controller.initialize_camera()
            return "相机初始化完成。"

        stop_event = threading.Event()
        worker = _PulseTestWorker(_fn, stop_event)
        thread = QThread(self)
        worker.moveToThread(thread)
        worker.signal_success.connect(self._on_cam_init_success)
        worker.signal_error.connect(self._on_cam_init_error)
        worker.signal_finished.connect(self._on_cam_init_finished)
        thread.started.connect(worker.run)
        self._cam_init_worker = worker
        self._cam_init_thread = thread
        thread.start()

    @pyqtSlot(str)
    def _on_cam_init_success(self, message: str) -> None:
        self._log(f"[OK] {message}")

    @pyqtSlot(str)
    def _on_cam_init_error(self, error: str) -> None:
        self._set_error(error)

    @pyqtSlot()
    def _on_cam_init_finished(self) -> None:
        if self._cam_init_thread is not None:
            self._cam_init_thread.quit()
            self._cam_init_thread.wait()
            self._cam_init_thread = None
        self._cam_init_worker = None

    def _apply_camera_settings(self) -> None:
        """Apply Camera Settings 按钮：同步 UI → worker 线程把相机参数下发到 DCAM。"""
        if self._cam_settings_thread is not None:
            return
        self._sync_config_from_widgets()
        camera_config_snapshot = copy.copy(self.config.camera)

        def _fn(stop_event: threading.Event) -> str:
            self.controller.apply_camera_config(camera_config_snapshot)
            return "相机参数已下发。"

        stop_event = threading.Event()
        worker = _PulseTestWorker(_fn, stop_event)
        thread = QThread(self)
        worker.moveToThread(thread)
        worker.signal_success.connect(self._on_cam_settings_success)
        worker.signal_error.connect(self._on_cam_settings_error)
        worker.signal_finished.connect(self._on_cam_settings_finished)
        thread.started.connect(worker.run)
        self._cam_settings_worker = worker
        self._cam_settings_thread = thread
        thread.start()

    @pyqtSlot(str)
    def _on_cam_settings_success(self, message: str) -> None:
        self._log(f"[OK] {message}")

    @pyqtSlot(str)
    def _on_cam_settings_error(self, error: str) -> None:
        self._set_error(error)

    @pyqtSlot()
    def _on_cam_settings_finished(self) -> None:
        if self._cam_settings_thread is not None:
            self._cam_settings_thread.quit()
            self._cam_settings_thread.wait()
            self._cam_settings_thread = None
        self._cam_settings_worker = None

    @_catch_to_error
    def _arm_camera(self) -> None:
        """Arm Camera 按钮：让相机进入 snapshot 等待 9 帧外触发状态。"""
        self.controller.arm_camera(frame_count=9)

    @_catch_to_error
    def _disarm_camera(self) -> None:
        """Disarm Camera 按钮：退出 arm 状态，常用于调试中断。"""
        self.controller.disarm_camera()

    def _program_patterns(self) -> None:
        """Program Patterns 按钮：GUI 线程快照路径，worker 线程把 9 个文件烧录到 SLM。"""
        if self._program_patterns_thread is not None:
            return
        try:
            self._sync_config_from_widgets()
        except Exception as exc:
            self._set_error(str(exc))
            return
        pattern_files_snapshot = list(self.config.pattern_files)

        def _fn(stop_event: threading.Event) -> str:
            self.controller.prepare_patterns(pattern_files_snapshot)
            return "图案已烧录到 SLM。"

        stop_event = threading.Event()
        worker = _PulseTestWorker(_fn, stop_event)
        thread = QThread(self)
        worker.moveToThread(thread)
        worker.signal_success.connect(self._on_program_patterns_success)
        worker.signal_error.connect(self._on_program_patterns_error)
        worker.signal_finished.connect(self._on_program_patterns_finished)
        thread.started.connect(worker.run)
        self._program_patterns_worker = worker
        self._program_patterns_thread = thread
        thread.start()

    @pyqtSlot(str)
    def _on_program_patterns_success(self, message: str) -> None:
        self._log(f"[OK] {message}")

    @pyqtSlot(str)
    def _on_program_patterns_error(self, error: str) -> None:
        self._set_error(error)

    @pyqtSlot()
    def _on_program_patterns_finished(self) -> None:
        if self._program_patterns_thread is not None:
            self._program_patterns_thread.quit()
            self._program_patterns_thread.wait()
            self._program_patterns_thread = None
        self._program_patterns_worker = None

    def _ensure_slm_connected_for_running_order(self) -> None:
        """正式采集前检查 SLM 是否连接；未连接抛 ``HardwareError``。"""
        if not self.controller.slm_adapter.is_connected():
            raise HardwareError("Please connect the SLM before starting SIM acquisition.")

    def _task_from_current_config(self) -> SimTaskConfig:
        """把当前 ``self.config`` 转成 ``SimTaskConfig``（一次采集的输入参数集合）。"""
        return SimTaskConfig(
            laser_wavelength_nm=self.config.selected_laser_nm,
            pattern_files=list(self.config.pattern_files),
            running_order_name=self.config.selected_running_order,
            camera=self.config.camera,
            timing=self.config.timing,
        )

    @_catch_to_error
    def _prepare_experiment(self) -> None:
        """Prepare Experiment 按钮：跑准备阶段（初始化硬件 + 选 RO + 配相机），不真正采集。"""
        # 1) UI → config → SLM 连接检查 → 通过 controller 排队 worker task。
        self._sync_config_from_widgets()
        self._ensure_slm_connected_for_running_order()
        task_id = self.controller.start_prepare_experiment(
            self._task_from_current_config(),
            prepare_running_order=True,
            initialize_hardware=True,
            apply_daq_config=True,
            apply_camera_config=True,
        )
        # 2) 记录最近 task_id，便于日志和 UI 关联。
        self.current_task_id = task_id
        self.lbl_current_task.setText(task_id)
        self._log(f"Experiment preparation queued: {task_id}")

    @_catch_to_error
    def _run_single_acquisition(self) -> None:
        """Run Single 9-Frame Acquisition 按钮：执行一次正式 SIM9 采集。"""
        # 0) 重入防护：已有活跃 task 时拒绝重复启动，防止 worker 互相竞争硬件资源。
        if self.controller.is_busy:
            self._set_error("采集正在进行中，请等待完成或先 Stop 后重试。")
            return
        # 1) UI → config，确保 worker 读到一致状态。
        self._sync_config_from_widgets()
        self._ensure_slm_connected_for_running_order()
        task = self._task_from_current_config()
        # 2) 通过 controller 排队正式采集；返回 task_id。
        task_id = self.controller.start_single_acquisition(
            task,
            prepare_running_order=True,
            initialize_hardware=True,
            apply_daq_config=True,
            apply_camera_config=True,
            z_scan_config=self.config.z_scan,
        )
        # 3) 把 task_id / 波长写到状态区，并把 pipeline 标签置成"排队中"。
        self.current_task_id = task_id
        self.lbl_current_task.setText(task_id)
        self.lbl_current_laser.setText(f"{task.laser_wavelength_nm} nm")
        self.pipeline_labels["task_id"].setText(task_id)
        self.pipeline_labels["reconstruction"].setText("Queued")
        self.pipeline_labels["features"].setText("Waiting")
        self.pipeline_labels["decision"].setText("Waiting")
        self._log(f"Acquisition started: {task_id}")

    @_catch_to_error
    def _stop_acquisition(self) -> None:
        """Stop 按钮：让 controller 置位 stop_event；worker 会尽快取消。"""
        self.controller.stop()
        self._log("Stop requested.")

    def _clear_result(self) -> None:
        """Clear Result 按钮：把状态/pipeline 标签恢复到初始空值。"""
        self.current_task_id = "-"
        self.sim_last_reconstruction_result = None
        self.lbl_current_frame.setText("-")
        self.lbl_current_pattern.setText("-")
        self.lbl_current_task.setText("-")
        for label in self.pipeline_labels.values():
            label.setText("-")
        # ``log_message=False`` 避免在日志里刷出"ERROR: -"。
        self._set_error("-", log_message=False)
        self._log("Cleared last result.")

    def _handle_status_changed(self, state: str, payload: dict) -> None:
        """Controller status 信号槽：根据状态名更新状态标签、LED、进度条与 RO 选择。"""
        # 1) 状态名 → 主标签 + 同步 LED。
        self.lbl_current_status.setText(state)
        self._update_hardware_leds(state)
        # 2) 采集开始 → 进度条归 0。
        if state == "acquisition_starting":
            self.progress_acquisition.setValue(0)
        # 3) 单帧捕获事件 → 当前帧号 + 当前 pattern 标签 + 进度条推进。
        if state == "frame_captured":
            frame_index = payload.get("frame_index", "-")
            self.lbl_current_frame.setText(str(frame_index))
            self.lbl_current_pattern.setText(str(frame_index))
            try:
                self.progress_acquisition.setValue(int(frame_index))
            except (TypeError, ValueError):
                pass
        # 4) Running Order 被选中 → 把 RO 名写回 config，便于摘要显示。
        if state == "running_order_selected":
            running_order_name = str(payload.get("running_order_name", ""))
            if running_order_name:
                self.config.selected_running_order = running_order_name
        # 5) 采集完成 → 进度条置满 + 显示 stack 形状。
        if state == "acquisition_complete":
            self.progress_acquisition.setValue(9)
            self.pipeline_labels["stack_shape"].setText(str(payload.get("stack_shape", "-")))
        # 6) 所有状态都打到日志，方便事后回放。
        self._log(f"[{state}] {payload}")

    def _update_hardware_leds(self, state: str) -> None:
        """根据 controller 状态名切换 3 个 LED 的颜色。"""
        # 各状态名 → LED 颜色映射；未列出的状态不改 LED，保留上一帧颜色。
        if state == "hardware_initializing":
            for led in self.hardware_leds.values():
                led.set_state("yellow")
        elif state in {"hardware_initialized", "camera_initialized", "camera_connected", "camera_config_applied", "camera_armed"}:
            self.hardware_leds["camera"].set_state("green")
        elif state == "camera_disconnected":
            self.hardware_leds["camera"].set_state("red")
        elif state in {"slm_connected", "patterns_prepared", "running_order_selected"}:
            self.hardware_leds["slm"].set_state("green")
        elif state == "slm_disconnected":
            self.hardware_leds["slm"].set_state("red")
        elif state == "daq_config_applied":
            self.hardware_leds["daq"].set_state("green")
        elif state.endswith("failed") or state.endswith("error"):
            for led in self.hardware_leds.values():
                led.set_state("red")

    def _handle_acquisition_ready(self, payload: dict) -> None:
        """采集完成槽：只用轻量 summary 更新 GUI，不接收 raw stack。"""
        self.pipeline_labels["task_id"].setText(str(payload.get("task_id", "")))
        self.pipeline_labels["stack_shape"].setText(str(payload.get("stack_shape", "-")))
        self.pipeline_labels["stack_dtype"].setText(str(payload.get("stack_dtype", "-")))
        self.pipeline_labels["reconstruction"].setText("Running")

    def _handle_acquisition_failed(self, task_id: str, message: str) -> None:
        """采集失败槽：状态置错误、进度条归 0、所有 LED 红，并把消息塞到错误标签。"""
        self.pipeline_labels["reconstruction"].setText("Acquisition failed")
        self.progress_acquisition.setValue(0)
        for led in self.hardware_leds.values():
            led.set_state("red")
        self._set_error(f"Acquisition failed for {task_id}: {message}")

    def _handle_acquisition_cancelled(self, task_id: str, message: str) -> None:
        """采集取消槽：标签提示取消，但不点红 LED（用户主动停止不是硬件错误）。"""
        self.pipeline_labels["reconstruction"].setText("Acquisition cancelled")
        self.progress_acquisition.setValue(0)
        self._log(f"Acquisition cancelled for {task_id}: {message}")

    def _handle_reconstruction_ready(self, recon_result) -> None:
        """重建完成槽：把状态切到"重建 Ready / 特征 Running"。"""
        self.sim_last_reconstruction_result = recon_result
        self.pipeline_labels["reconstruction"].setText("Ready")
        self.pipeline_labels["features"].setText("Running")
        self._log(f"Reconstruction ready for {recon_result.task_id}")

    def _handle_reconstruction_failed(self, task_id: str, message: str) -> None:
        """重建失败槽：标签置 Failed，并把消息塞到错误标签。"""
        self.sim_last_reconstruction_result = None
        self.pipeline_labels["reconstruction"].setText("Failed")
        self._set_error(f"Reconstruction failed for {task_id}: {message}")

    def _handle_feature_ready(self, feature_result) -> None:
        """特征完成槽：调用占位决策器，并把决策结果写到 pipeline 标签。"""
        self.pipeline_labels["features"].setText("Ready")
        decision = self.decision_engine.decide(feature_result)
        self.pipeline_labels["decision"].setText(f"{decision.decision} | {decision.reason}")
        self._log(f"Decision for {feature_result.task_id}: {decision.decision}")

    def _handle_feature_failed(self, task_id: str, message: str) -> None:
        """特征失败槽：标签置 Failed，并把消息塞到错误标签。"""
        self.pipeline_labels["features"].setText("Failed")
        self._set_error(f"Feature extraction failed for {task_id}: {message}")

    def _browse_pattern(self, index: int) -> None:
        """Browse 按钮槽：弹文件对话框，把选中路径写到第 ``index`` 个 QLineEdit。"""
        path = browse_pattern_file(self, index)
        if path:
            self.pattern_edits[index].setText(path)

    def _log(self, message: str) -> None:
        """追加一行到滚动日志区。"""
        self.log_output.appendPlainText(message)

    def _set_error(self, message: str, log_message: bool = True) -> None:
        """更新错误标签，并默认同步打到日志区。"""
        self.lbl_current_error.setText(message)
        if log_message:
            self._log(f"ERROR: {message}")

    def closeEvent(self, event) -> None:  # noqa: N802
        """窗口关闭：自动保存配置 → controller shutdown → drain 落盘队列握手 → 关停线程。"""
        # 0) 防重入：收尾期间嵌套事件循环会继续处理 GUI 事件，可能二次触发 closeEvent。
        #    用 ignore（而非 accept）让二次事件不打断首次清理、也不提前关窗。
        if self._closing:
            event.ignore()
            return
        self._closing = True
        try:
            # 1) 把控件状态写回 config 并保存到磁盘。失败时打日志但不阻塞关闭。
            self._sync_config_from_widgets()
            save_app_config(self.config, self.config.config_path or DEFAULT_CONFIG_PATH)
        except Exception:
            self._log(traceback.format_exc())
        # 2) controller.shutdown 会停 worker + 断硬件；最多等 2 秒。
        self.controller.shutdown()
        # 3) 关停重建线程：等 worker drain 落盘队列（signal_shutdown_finished）后再 quit。
        self._shutdown_recon_thread_with_drain()
        # 4) 关停特征线程。
        self.feature_thread.quit()
        self.feature_thread.wait(2000)
        super().closeEvent(event)

    def _shutdown_recon_thread_with_drain(self) -> None:
        """请求重建 worker 关停并等 drain 完成（本地 QEventLoop + 超时 fallback），再 quit/wait。"""
        if not self.recon_thread.isRunning():
            return
        self._recon_shutdown_done = False
        loop = QEventLoop()
        self._recon_shutdown_loop = loop
        timed_out = {"value": False}

        def _on_timeout() -> None:
            # 守卫：finished 先到、loop 已退出时，迟到的 singleShot 不应误置 timed_out。
            if loop.isRunning():
                timed_out["value"] = True
                loop.quit()

        # 先连临时 handler（窗口自身 @pyqtSlot，GUI 线程 QObject，避免裸 closure 跨线程语义），再 emit。
        self.recon_worker.signal_shutdown_finished.connect(self._on_recon_shutdown_during_close)
        self.signal_reconstruction_shutdown_requested.emit()
        try:
            # 只有信号尚未到达（done 仍 False）才进入嵌套循环，避免"信号已到却空等到超时"。
            if not self._recon_shutdown_done:
                QTimer.singleShot(8000, _on_timeout)
                loop.exec_()
        finally:
            try:
                self.recon_worker.signal_shutdown_finished.disconnect(self._on_recon_shutdown_during_close)
            except (TypeError, RuntimeError):
                pass
            self._recon_shutdown_loop = None
        if timed_out["value"]:
            self._log("WARNING: reconstruction worker shutdown timed out; save queue may not be fully drained.")
        self.recon_thread.quit()
        if not self.recon_thread.wait(2000):
            self._log("WARNING: reconstruction thread did not stop within 2s during close.")

    @pyqtSlot()
    def _on_recon_shutdown_during_close(self) -> None:
        """关闭握手专用 handler：标记 drain 完成并退出本地等待循环（GUI 线程）。"""
        self._recon_shutdown_done = True
        loop = self._recon_shutdown_loop
        if loop is not None:
            loop.quit()

    def _on_recon_shutdown_finished(self) -> None:
        """重建 worker drain 落盘队列完成的常驻日志回调（关闭流程由 closeEvent 推进）。"""
        self._log("Reconstruction worker shutdown finished (save queue drained).")

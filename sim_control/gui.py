"""独立 SIM 采集 GUI 与设置弹窗的手写逻辑。

作用：
    本文件实现两个 PyQt5 顶层组件：
        - ``SimSettingsDialog``：SIM 设置弹窗。集成主界面打开它时只编辑配置、
          刷新 DAQ 设备/线位、执行短路径硬件测试（相机触发、单激光脉冲或一次完整
          SIM9 测试采集）；**它不拥有 SLM/相机生命周期**，必须与主界面共享同一个
          ``slm_adapter`` / ``camera_adapter``（决策日志 2026-04-26）。
        - ``SimControlWindow``：独立 SIM 主窗口。承载 DAQ 设置、相机/曝光/ROI、
          9 帧 pattern 槽位、激光选择、采集控制、Pipeline 状态显示与滚动日志。
          供 ``python -m sim_control.sim_acquisition_app`` 入口调试 SIM 采集链路。

协作关系：
    上游：``sim_control/sim_acquisition_app.py``、``control_wangbo/main.py``
          （后者把 ``SimSettingsDialog`` 当作设置面板调出）。
    下游：``adapters.*``、``sim_adapters.*``、``controller.SimAcquisitionController``、
          ``pipeline.*``、``config_store``、``models.*``。
    UI：所有窗口控件来自 ``ui_sim_settings_dialog.py``（pyuic5 生成，不要手改）
        与本文件手写代码。

关键概念：
    - ``_catch_to_error`` 装饰器：把 Qt 槽函数里的异常都打到 GUI 状态区，
      避免 PyQt 把异常静默吞掉。
    - DAQ "测试目标"下拉：``build_daq_test_target_items`` 生成。包含 4 路激光
      短脉冲、1 路相机触发 + 拍单帧、1 路完整 SIM9 测试采集。
    - 配置同步：``_sync_config_from_widgets`` 在保存或启动采集前把控件状态搬到
      ``self.config``，避免 UI 与配置对象漂移。

维护要点：
    - 不要在本文件直接调用厂商 SDK；所有硬件动作都通过 adapter / controller。
    - ``ui_sim_settings_dialog.py`` 由 pyuic5 生成，本文件只**读**它生成的对象，
      不要修改生成代码（编辑 ``.ui`` 再 ``pyuic5`` 重生）。
    - GUI 信号槽里抛错都用 ``_catch_to_error`` 装饰，让 ``_set_error`` 显示原因。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import copy
import functools
import logging
import statistics
import threading
import time
from datetime import datetime
from pathlib import Path
import traceback
from typing import Any

import numpy as np

from PyQt5.QtCore import QEvent, QObject, Qt, QThread, QTimer, pyqtSignal, pyqtSlot
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
    find_z_scan_running_order,
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
    TimingConfig,
    ZScanConfig,
    default_daq_line_name,
)
from .pipeline import DecisionEngine, FeatureWorker, ReconstructionWorker
from .protocols import CameraAdapter, SlmAdapter
from .led_indicator import LedIndicator
from .ui_sim_settings_dialog import Ui_SimSettingsDialog
from .waveform import NIDaqWaveformBuilder, parse_line_name, validate_daq_line_config
from .z_scan_core import run_z_scan, scan_positions
from .z_scan_timing_history import (
    DEFAULT_Z_SCAN_TIMING_HISTORY_PATH,
    append_z_scan_timing_records,
    build_z_scan_run_timing_record,
    build_z_scan_timing_records,
    estimate_z_scan_capture_test_total_time_ms,
    estimate_z_scan_move_only_total_time_ms,
    load_z_scan_timing_records,
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


def _zscan_direction_to_index(direction: str) -> int:
    return 1 if direction == "negative_z" else 0


def _zscan_index_to_direction(index: int) -> str:
    return "negative_z" if int(index) == 1 else "positive_z"


def _zscan_preset_to_index(preset_ms: int) -> int:
    presets = (5, 8, 14, 20)
    try:
        return presets.index(int(preset_ms))
    except ValueError:
        return presets.index(8)


def _zscan_index_to_preset(index: int) -> int:
    presets = (5, 8, 14, 20)
    if 0 <= int(index) < len(presets):
        return presets[int(index)]
    return 8


def write_z_scan_config_to_widgets(config: ZScanConfig, dialog: object) -> None:
    dialog.check_zscan_enabled.setChecked(bool(config.enabled))
    dialog.spin_zscan_start_um.blockSignals(True)
    try:
        if config.start_um is not None:
            dialog.spin_zscan_start_um.setValue(float(config.start_um))
    finally:
        dialog.spin_zscan_start_um.blockSignals(False)
    dialog.spin_zscan_start_um.setProperty("zscan_auto_start", config.start_um is None)
    dialog.combo_zscan_direction.setCurrentIndex(_zscan_direction_to_index(config.direction))
    dialog.spin_zscan_step_um.setValue(float(config.step_um) * 1000.0)
    dialog.spin_zscan_num_steps.setValue(int(config.num_steps))
    dialog.combo_zscan_exposure_preset.setCurrentIndex(_zscan_preset_to_index(config.exposure_preset_ms))


def read_z_scan_config_from_widgets(dialog: object) -> ZScanConfig:
    auto_start = bool(dialog.spin_zscan_start_um.property("zscan_auto_start"))
    return ZScanConfig(
        enabled=bool(dialog.check_zscan_enabled.isChecked()),
        start_um=None if auto_start else float(dialog.spin_zscan_start_um.value()),
        direction=_zscan_index_to_direction(dialog.combo_zscan_direction.currentIndex()),
        step_um=float(dialog.spin_zscan_step_um.value()) / 1000.0,
        num_steps=int(dialog.spin_zscan_num_steps.value()),
        exposure_preset_ms=_zscan_index_to_preset(dialog.combo_zscan_exposure_preset.currentIndex()),
        focus_metric="sml",
    )


RECON_WAVELENGTHS = (405, 488, 561, 647)


def _recon_wavelength_to_index(wavelength_nm: int) -> int:
    try:
        return RECON_WAVELENGTHS.index(int(wavelength_nm))
    except ValueError:
        return RECON_WAVELENGTHS.index(488)


def _recon_otf_attr(wavelength_nm: int) -> str:
    return f"otf_{int(wavelength_nm)}_path"


def _selected_recon_wavelength_nm(combo: QComboBox) -> int:
    data = combo.currentData()
    if data is not None:
        return int(data)
    text = combo.currentText().replace("nm", "").strip()
    return int(text) if text else 488


def write_reconstruction_config_to_widgets(
    config: ReconstructionConfig,
    selected_laser_nm: int,
    dialog: object,
) -> None:
    """把 ``ReconstructionConfig`` 写入 Recon 页控件。"""
    dialog.spin_recon_wiener.setValue(float(config.wiener))
    dialog.spin_recon_na.setValue(float(config.excitation_na))
    dialog.spin_recon_pixel_size_nm.setValue(float(config.pixel_size_nm))
    dialog.edit_recon_background_path.setText(str(config.background_path))
    dialog.edit_recon_output_path.setText(str(config.output_path))
    dialog._sync_recon_wavelength_combo(int(selected_laser_nm))
    dialog._current_recon_wavelength_nm = int(selected_laser_nm)
    dialog.edit_recon_otf_path.setText(config.otf_path_for_wavelength(int(selected_laser_nm)))


def read_reconstruction_config_from_widgets(dialog: object) -> ReconstructionConfig:
    """从 Recon 页控件构造 ``ReconstructionConfig``。"""
    dialog._store_current_recon_otf_path()
    current = dialog.config.reconstruction
    return ReconstructionConfig(
        enabled=True,
        backend="sim_wiener_gpu",
        device="cuda",
        dtype="single",
        otf_405_path=str(current.otf_405_path).strip(),
        otf_488_path=str(current.otf_488_path).strip(),
        otf_561_path=str(current.otf_561_path).strip(),
        otf_647_path=str(current.otf_647_path).strip(),
        background_path=dialog.edit_recon_background_path.text().strip(),
        output_path=dialog.edit_recon_output_path.text().strip() or DEFAULT_RECONSTRUCTION_OUTPUT_DIR,
        wiener=float(dialog.spin_recon_wiener.value()),
        pixel_size_nm=float(dialog.spin_recon_pixel_size_nm.value()),
        excitation_na=float(dialog.spin_recon_na.value()),
        theta_ratio=tuple(current.theta_ratio),
        recon_group_batch=int(current.recon_group_batch),
    )


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


# 8 个 DAQ 角色的可读标签；GUI 错误提示、测试下拉等地方使用。
ROLE_LABELS = {
    "slm_enable_line": "SLM Enable",
    "slm_trigger_line": "SLM Trigger",
    "slm_finish_line": "SLM Finish",
    "camera_trigger_line": "Camera Trigger",
    "laser_405_line": "Laser 405",
    "laser_488_line": "Laser 488",
    "laser_561_line": "Laser 561",
    "laser_647_line": "Laser 647",
}

# 设置弹窗里"SIM 采集测试"的下拉项目 ID 常量。
SIM_ACQUISITION_TEST_ID = "sim_acquisition"
# SIM 采集测试默认参数：500ms 曝光 + 50ms 帧间隔，便于真机调试时观察是否触发。
SIM_ACQUISITION_TEST_EXPOSURE_US = 500_000
SIM_ACQUISITION_TEST_INTER_FRAME_GAP_US = 50_000
# "SLM 激活时序测试"下拉项目 ID：DAQ 拉高 slm_enable 后轮询 R11 激活状态，
# 实测 EXT_RUN -> ACT 的延迟上界（数据手册 tHWAT 规格为 5~500 µs）。
SLM_ACTIVATION_TIMING_TEST_ID = "slm_activation_timing"
# 激活轮询超时：tHWAT 上限 500 µs + USB 轮询粒度（毫秒级），2 秒足够分辨异常。
SLM_ACTIVATION_TIMING_TIMEOUT_S = 2.0
# 测试下拉中可单独发短脉冲的 5 个角色（不含 SLM enable/trigger/finish，避免误触发 SLM）。
DAQ_PULSE_TEST_ROLES = (
    "camera_trigger_line",
    "laser_405_line",
    "laser_488_line",
    "laser_561_line",
    "laser_647_line",
)
# 测试采集的 TIFF 保存目录；按子目录分类相机/SIM9 采集，便于事后审阅。
TEST_CAPTURE_ROOT = Path(__file__).resolve().parent.parent / "data" / "test_captures"
Z_SCAN_TEST_STAGE_ONLY = "zscan_stage_only"
Z_SCAN_TEST_STAGE_PLUS_CAPTURE = "zscan_stage_plus_capture"
# Z-Scan worker 信号编码：success 消息以此开头表示"带警告成功"，接着是警告文本和换行。
_ZSCAN_WARN_SEP = "\x00ZSCAN_WARN\x00"
Z_SCAN_TEST_TARGET_ITEMS = (
    (Z_SCAN_TEST_STAGE_ONLY, "Z 位移台移动 (仅位移台)"),
    (Z_SCAN_TEST_STAGE_PLUS_CAPTURE, "Z 位移台 + 每层采图 (含 SML 评分)"),
)
Z_SCAN_TEST_CAPTURE_MAX_STEPS = 100


@dataclass(frozen=True)
class SimAcquisitionTestResult:
    """``_run_sim_acquisition_test`` 的返回结构。

    职责：
        - ``output_path``：保存到磁盘的 9 帧 uint16 TIFF 路径。
        - ``actual_acquisition_duration_s``：从发起测试到 9 帧 stack 入内存的实际耗时（秒），
          **不含**写盘开销，便于真机评估端到端时延。
        - ``daq_waveform_duration_s``：DAQ 完整波形播放时长（秒）。
    """
    output_path: Path
    actual_acquisition_duration_s: float
    daq_waveform_duration_s: float


@dataclass(frozen=True)
class SlmActivationTimingTestResult:
    """``_run_slm_activation_timing_test`` 的返回结构。

    职责：
        - ``running_order_name``：本次测试选中的 RO 名。
        - ``initial_state``：软件 activate 后、slm_enable 拉高前的状态 ``{"code","name"}``。
        - ``transitions``：``(elapsed_ms, state)`` 列表，记录 enable 拉高后观察到的状态变迁。
        - ``reached_active`` / ``enable_to_active_ms``：是否达到 0x56 ACT 及对应耗时；
          受 USB 轮询粒度限制，该耗时是真实 tHWAT 的**上界**。
        - ``poll_count``：总轮询次数，用于评估轮询粒度。
    """
    running_order_name: str
    initial_state: dict[str, Any]
    transitions: list[tuple[float, dict[str, Any]]]
    reached_active: bool
    enable_to_active_ms: float | None
    poll_count: int


@dataclass(frozen=True)
class ZScanTestResult:
    mode: str
    positions_visited: list[float]
    total_duration_s: float
    move_latencies_ms: list[float]
    output_path: Path | None
    frame_count: int
    best_layer_index: int = -1
    best_z_um: float = float("nan")
    focus_scores: list[float] = field(default_factory=list)
    position_move_latencies_ms: list[float] = field(default_factory=list)
    capture_nonmove_latencies_ms: list[float] = field(default_factory=list)
    initial_position_ms: float = 0.0
    restore_ms: float = 0.0
    fixed_overhead_ms: float = 0.0
    best_focus_move_ms: float = 0.0
    tiff_write_ms: float = 0.0


def build_zscan_test_target_items() -> list[tuple[str, str]]:
    return list(Z_SCAN_TEST_TARGET_ITEMS)


def clone_app_config(config: AppConfig) -> AppConfig:
    """通过 dict 中转的方式深拷贝 ``AppConfig``。

    用途：
        ``SimSettingsDialog`` 编辑配置时拿到的是外部对象的"独立副本"，避免用户
        点击 ``Cancel`` 时已修改的字段污染调用方持有的 AppConfig。
    """
    # 借助现成的 dict 转换路径完成深拷贝；同时跑一次迁移链以确保 schema 一致。
    return app_config_from_dict(app_config_to_dict(config))


def build_daq_test_target_items(daq_config: DaqLineConfig) -> list[tuple[str, str]]:
    """根据当前 DAQ 配置生成"测试目标"下拉项目列表。

    返回：
        ``[(target_id, display_text), ...]``：5 路单线脉冲 + 1 路 SIM9 完整测试。
    """
    # 1) 5 路角色：相机触发显示成"Camera Trigger + Capture"，便于和单线脉冲区分。
    items = [
        (
            role,
            f"{'Camera Trigger + Capture' if role == 'camera_trigger_line' else ROLE_LABELS[role]} -> "
            f"{getattr(daq_config, role)}",
        )
        for role in DAQ_PULSE_TEST_ROLES
    ]
    # 2) 末尾追加完整 SIM9 测试项；ID 用 ``SIM_ACQUISITION_TEST_ID`` 常量。
    items.append((SIM_ACQUISITION_TEST_ID, "SIM采集"))
    # 3) SLM 激活时序诊断：实测 slm_enable(EXT_RUN) 拉高 -> RO ACT 的延迟上界。
    items.append((SLM_ACTIVATION_TIMING_TEST_ID, "SLM激活时序"))
    return items


class _PulseTestWorker(QObject):
    """一次性诊断测试 worker；在独立 QThread 中执行，通过信号把结果回传 GUI。

    线程模型：
        - ``__init__`` 在 GUI 线程构造；``moveToThread`` 后 ``run`` 在工作线程执行。
        - ``cancel()`` 线程安全：置位 ``_stop_event``，fn 在下一个检查点退出。
        - fn 接受 ``threading.Event`` 参数，fn 内各测试方法的 finally 块负责 DAQ 全低收尾。
    """
    signal_success = pyqtSignal(str)   # 测试通过；payload 为显示消息
    signal_error = pyqtSignal(str)     # 失败；payload 为错误描述（取消时不发出）
    signal_finished = pyqtSignal()     # 成功 / 失败 / 取消三条路径均发出

    def __init__(self, fn, stop_event: threading.Event, parent=None):
        super().__init__(parent)
        self._fn = fn                  # Callable[[threading.Event], str]
        self._stop_event = stop_event

    def cancel(self) -> None:
        self._stop_event.set()

    @pyqtSlot()
    def run(self) -> None:
        try:
            if self._stop_event.is_set():
                return
            message = self._fn(self._stop_event)
            if not self._stop_event.is_set():
                self.signal_success.emit(message)
        except Exception as exc:
            if not self._stop_event.is_set():
                self.signal_error.emit(str(exc))
            else:
                logger.debug("Test exception during cancellation (suppressed): %s", exc)
        finally:
            self.signal_finished.emit()


# 设置弹窗只编辑 SIM 配置和执行短测试，不拥有集成主界面共享的 SLM/相机生命周期。
class SimSettingsDialog(QDialog):
    """SIM 设置弹窗。

    职责：
        - 编辑 SIM 配置（DAQ 设备 + 8 路线位 + 选中激光）。
        - 调 NI MAX / SLM SDK 刷新设备/线位下拉。
        - 提供"测试目标"下拉：4 路激光短脉冲、1 路相机触发 + 拍单帧、1 路 SIM9 完整测试。

    协作：
        - 集成主界面（``control_wangbo/main.py``）传入共享的 ``slm_adapter`` /
          ``camera_adapter``，本对话框**不接管**它们的生命周期。
        - 用户点保存时通过 ``signal_settings_saved`` 把更新后的 ``AppConfig`` 回传。
    """
    signal_settings_saved = pyqtSignal(object)

    def __init__(
        self,
        config: AppConfig | None = None,
        config_path: str | None = None,
        parent: QWidget | None = None,
        slm_adapter: SlmAdapter | None = None,
        camera_adapter: CameraAdapter | None = None,
        daq_adapter: object | None = None,
        stage_adapter: object | None = None,
    ):
        # 1) 调父类构造让 Qt 接管对话框生命周期。
        super().__init__(parent)
        # 2) 解析 source_config：显式 config 优先，否则按 config_path 加载；
        #    最终都通过 clone_app_config 复制一份，避免修改影响调用方。
        source_config = config or load_app_config(config_path or DEFAULT_CONFIG_PATH)
        self.config = clone_app_config(source_config)
        if config_path:
            self.config.config_path = config_path

        # 3) 加载 pyuic5 生成的 Ui 类；后续 ``setupUi`` 把控件挂到 self 上。
        self.ui = Ui_SimSettingsDialog()
        # 4) 创建/接受 3 个 adapter。DAQ 默认新建；SLM/相机若外部传入则共享。
        self.daq_adapter = daq_adapter or create_daq_adapter_for_backend(self.config.backend)
        self.slm_adapter = slm_adapter or create_slm_adapter_for_backend(self.config.backend)
        self.camera_adapter = camera_adapter or create_camera_adapter_for_backend(self.config.backend)
        self.stage_adapter = stage_adapter
        # 5) 记录共享 flag：关闭对话框时不要 disconnect 外部传入的 adapter。
        self._slm_externally_owned = slm_adapter is not None
        self._camera_externally_owned = camera_adapter is not None
        self._preferred_daq_device = self.config.daq.device_name
        self._loaded_pattern_result = None
        self._pending_zscan_restore_warning = None
        self._pending_zscan_slm_warning = None
        self._zscan_timing_history_path = DEFAULT_Z_SCAN_TIMING_HISTORY_PATH
        self._zscan_timing_records = load_z_scan_timing_records(self._zscan_timing_history_path)
        self._current_recon_wavelength_nm = int(self.config.selected_laser_nm)
        self._populating_widgets = False
        # 6) 延迟硬件刷新：``showEvent`` 后再做，避免对话框未显示就阻塞 UI。
        self._initial_hardware_refresh_pending = True
        # 诊断测试 worker 状态；None 表示当前没有测试在运行。
        self._pulse_test_thread: QThread | None = None
        self._pulse_test_worker: _PulseTestWorker | None = None
        self._pulse_test_stop_event: threading.Event | None = None
        self._pulse_test_btn_original_text: str = "Run Test"
        # Z-Scan 测试 worker 状态；与 pulse test 独立，两者可并行（实际不会同时触发）。
        self._zscan_test_thread: QThread | None = None
        self._zscan_test_worker: _PulseTestWorker | None = None
        self._zscan_test_stop_event: threading.Event | None = None
        # 7) UI 构造 + 信号连接 + 控件初值填充。
        self._build_ui()
        self._wire_signals()
        self._populate_widgets_from_config(self.config)

    def _build_ui(self) -> None:
        """初始化所有控件引用：从 ``Ui_SimSettingsDialog`` 设置的属性逐个取到 self。"""
        # 1) ``setupUi`` 把 UI 树挂到本对话框，并把控件挂到 self.ui 命名空间。
        self.ui.setupUi(self)

        # 2) 把常用控件 alias 到 self，缩短调用链。
        self.tabs = self.ui.tabs
        self.lbl_error = self.ui.lbl_error
        self.combo_daq_device = self.ui.combo_daq_device
        self.btn_refresh_lines = self.ui.btn_refresh_lines
        self.combo_test_target = self.ui.combo_test_target
        self.btn_pulse_test = self.ui.btn_pulse_test
        self.btn_save_close = self.ui.btn_save_close
        self.btn_cancel = self.ui.btn_cancel
        self.check_zscan_enabled = self.ui.check_zscan_enabled
        self.spin_zscan_start_um = self.ui.spin_zscan_start_um
        self.btn_zscan_read_current = self.ui.btn_zscan_read_current
        self.combo_zscan_direction = self.ui.combo_zscan_direction
        self.spin_zscan_step_um = self.ui.spin_zscan_step_um
        self.spin_zscan_num_steps = self.ui.spin_zscan_num_steps
        self.combo_zscan_exposure_preset = self.ui.combo_zscan_exposure_preset
        self.combo_zscan_test_target = self.ui.combo_zscan_test_target
        self.btn_zscan_test = self.ui.btn_zscan_test
        self.label_zscan_test_status = self.ui.label_zscan_test_status
        self.label_zscan_preview_start_value = self.ui.label_zscan_preview_start_value
        self.label_zscan_preview_end_value = self.ui.label_zscan_preview_end_value
        self.label_zscan_preview_distance_value = self.ui.label_zscan_preview_distance_value
        self.label_zscan_preview_eta_value = self.ui.label_zscan_preview_eta_value
        self.label_zscan_preview_capture_eta_value = self.ui.label_zscan_preview_capture_eta_value
        self.spin_recon_wiener = self.ui.spin_recon_wiener
        self.spin_recon_na = self.ui.spin_recon_na
        self.spin_recon_pixel_size_nm = self.ui.spin_recon_pixel_size_nm
        self.combo_recon_wavelength = self.ui.combo_recon_wavelength
        self.edit_recon_otf_path = self.ui.edit_recon_otf_path
        self.btn_recon_browse_otf = self.ui.btn_recon_browse_otf
        self.edit_recon_background_path = self.ui.edit_recon_background_path
        self.btn_recon_browse_background = self.ui.btn_recon_browse_background
        self.edit_recon_output_path = self.ui.edit_recon_output_path
        self.btn_recon_browse_output = self.ui.btn_recon_browse_output

        self.combo_recon_wavelength.clear()
        for wavelength in RECON_WAVELENGTHS:
            self.combo_recon_wavelength.addItem(f"{wavelength} nm", wavelength)

        # 3) 8 个 DAQ 角色 → 下拉控件映射；构造时一次性建立，后续刷新只改 items。
        self.line_combos = {
            "slm_enable_line": self.ui.combo_slm_enable_line,
            "slm_trigger_line": self.ui.combo_slm_trigger_line,
            "slm_finish_line": self.ui.combo_slm_finish_line,
            "camera_trigger_line": self.ui.combo_camera_trigger_line,
            "laser_405_line": self.ui.combo_laser_405_line,
            "laser_488_line": self.ui.combo_laser_488_line,
            "laser_561_line": self.ui.combo_laser_561_line,
            "laser_647_line": self.ui.combo_laser_647_line,
        }

        # 4) 把 4 个 RadioButton 用 QButtonGroup 关联起来，便于 ``checkedId()`` 读取。
        self.laser_group = QButtonGroup(self)
        self.laser_buttons = {
            405: self.ui.radio_laser_405,
            488: self.ui.radio_laser_488,
            561: self.ui.radio_laser_561,
            647: self.ui.radio_laser_647,
        }
        for wavelength, button in self.laser_buttons.items():
            self.laser_group.addButton(button, wavelength)

        for combo in self.findChildren(QComboBox):
            configure_centered_combobox(combo)

    def _wire_signals(self) -> None:
        """绑定所有按钮 / 下拉的信号到本对话框的槽。"""
        # 1) "刷新线位"按钮 → 触发 ``_refresh_daq_devices``。
        self.btn_refresh_lines.clicked.connect(self._refresh_daq_devices)
        # 2) DAQ 设备变更 → 重新拉对应 port0 的 line 列表。``lambda`` 把未用参数忽略。
        self.combo_daq_device.currentTextChanged.connect(lambda _text: self._refresh_device_lines())
        # 3) 各类按钮 → 对应槽函数。
        self.btn_pulse_test.clicked.connect(self._run_pulse_test)
        self.btn_save_close.clicked.connect(self._save_and_accept)
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_zscan_read_current.clicked.connect(lambda _checked=False: self._read_current_z_into_start())
        self.check_zscan_enabled.toggled.connect(self._sync_zscan_enabled_state)
        self.check_zscan_enabled.toggled.connect(lambda _enabled: self._refresh_zscan_preview())
        self.spin_zscan_start_um.valueChanged.connect(
            lambda _value: self.spin_zscan_start_um.setProperty("zscan_auto_start", False)
        )
        self.spin_zscan_start_um.valueChanged.connect(lambda _value: self._refresh_zscan_preview())
        self.combo_zscan_direction.currentIndexChanged.connect(lambda _index: self._refresh_zscan_preview())
        self.spin_zscan_step_um.valueChanged.connect(lambda _value: self._refresh_zscan_preview())
        self.spin_zscan_num_steps.valueChanged.connect(lambda _value: self._refresh_zscan_preview())
        self.combo_zscan_exposure_preset.currentIndexChanged.connect(lambda _index: self._refresh_zscan_preview())
        self.btn_zscan_test.clicked.connect(lambda _checked=False: self._run_zscan_test())
        self.combo_recon_wavelength.currentIndexChanged.connect(lambda _index: self._on_recon_wavelength_changed())
        self.btn_recon_browse_otf.clicked.connect(lambda _checked=False: self._browse_recon_otf())
        self.btn_recon_browse_background.clicked.connect(lambda _checked=False: self._browse_recon_background())
        self.btn_recon_browse_output.clicked.connect(lambda _checked=False: self._browse_recon_output())
        self.combo_zscan_test_target.clear()
        for target_id, label in build_zscan_test_target_items():
            self.combo_zscan_test_target.addItem(label, target_id)
        if self.combo_zscan_test_target.count():
            self.combo_zscan_test_target.setCurrentIndex(0)
        _center_combobox_items(self.combo_zscan_test_target)
        # 4) 任意线位下拉变更 → 重建测试目标下拉（避免显示旧线位）。
        for combo in self.line_combos.values():
            combo.currentTextChanged.connect(lambda _text: self._refresh_test_targets())
        for wavelength, button in self.laser_buttons.items():
            button.toggled.connect(
                lambda checked, wavelength=wavelength: self._on_laser_wavelength_toggled(wavelength, checked)
            )

    def _perform_initial_hardware_refresh(self) -> None:
        """对话框显示后做一次硬件刷新；放在 ``showEvent`` 中以避免阻塞构造期。"""
        self._refresh_daq_devices()

    def _populate_widgets_from_config(self, config: AppConfig) -> None:
        """把 AppConfig 中影响 UI 的字段写到控件初值。"""
        self._populating_widgets = True
        try:
            self._preferred_daq_device = config.daq.device_name
            write_selected_laser_to_widgets(config.selected_laser_nm, self.laser_buttons)
            write_z_scan_config_to_widgets(config.z_scan, self)
            write_reconstruction_config_to_widgets(config.reconstruction, config.selected_laser_nm, self)
            if config.z_scan.start_um is None and self.stage_adapter is not None and getattr(self.stage_adapter, "is_connected", False):
                self.spin_zscan_start_um.blockSignals(True)
                try:
                    self.spin_zscan_start_um.setValue(float(self.stage_adapter.get_position_um()))
                finally:
                    self.spin_zscan_start_um.blockSignals(False)
                self.spin_zscan_start_um.setProperty("zscan_auto_start", True)
            self._sync_zscan_enabled_state(bool(config.z_scan.enabled))
        finally:
            self._populating_widgets = False
        self._refresh_zscan_preview()

    def _current_daq_config(self) -> DaqLineConfig:
        """从控件读取当前 DAQ 配置；通过共享 helper 完成校验。"""
        return read_daq_config_from_line_combos(
            self.line_combos,
            device_name=self.combo_daq_device.currentText(),
        )

    def _selected_laser_nm(self) -> int:
        """从单选按钮组读取当前选中的波长（nm）。"""
        return read_selected_laser_nm(self.laser_group)

    def _sync_recon_wavelength_combo(self, wavelength_nm: int) -> None:
        """把 Recon 页波长下拉切到指定波长，过程中不触发联动槽。"""
        self.combo_recon_wavelength.blockSignals(True)
        try:
            self.combo_recon_wavelength.setCurrentIndex(_recon_wavelength_to_index(wavelength_nm))
        finally:
            self.combo_recon_wavelength.blockSignals(False)

    def _store_current_recon_otf_path(self) -> None:
        """把当前 OTF 输入框保存到当前波长对应的 config 字段。"""
        wavelength_nm = int(getattr(self, "_current_recon_wavelength_nm", self._selected_laser_nm()))
        setattr(
            self.config.reconstruction,
            _recon_otf_attr(wavelength_nm),
            self.edit_recon_otf_path.text().strip(),
        )

    def _load_recon_otf_path_for_wavelength(self, wavelength_nm: int) -> None:
        """切换 Recon 波长后，把该波长的 OTF 路径显示到输入框。"""
        self._current_recon_wavelength_nm = int(wavelength_nm)
        self.edit_recon_otf_path.setText(
            self.config.reconstruction.otf_path_for_wavelength(int(wavelength_nm))
        )

    def _set_laser_buttons_from_recon(self, wavelength_nm: int) -> None:
        """Recon 页波长改变时同步 Laser 页单选按钮，避免两个页面漂移。"""
        for button in self.laser_buttons.values():
            button.blockSignals(True)
        try:
            write_selected_laser_to_widgets(int(wavelength_nm), self.laser_buttons)
        finally:
            for button in self.laser_buttons.values():
                button.blockSignals(False)

    def _on_recon_wavelength_changed(self) -> None:
        """Recon 页波长下拉变化：保存旧 OTF、同步 Laser 页并显示新 OTF。"""
        if self._populating_widgets:
            return
        self._store_current_recon_otf_path()
        wavelength_nm = _selected_recon_wavelength_nm(self.combo_recon_wavelength)
        self._set_laser_buttons_from_recon(wavelength_nm)
        self.config.selected_laser_nm = int(wavelength_nm)
        self._load_recon_otf_path_for_wavelength(wavelength_nm)

    def _on_laser_wavelength_toggled(self, wavelength_nm: int, checked: bool) -> None:
        """Laser 页单选按钮变化：同步 Recon 页波长与对应 OTF 路径。"""
        if not checked:
            return
        if self._populating_widgets:
            return
        self._store_current_recon_otf_path()
        self.config.selected_laser_nm = int(wavelength_nm)
        self._sync_recon_wavelength_combo(int(wavelength_nm))
        self._load_recon_otf_path_for_wavelength(int(wavelength_nm))

    def _browse_recon_file(self, edit: QLineEdit, title: str, save: bool = False) -> None:
        """Recon 页通用路径选择器。"""
        current = edit.text().strip()
        start_dir = str(Path(current).parent if current else Path.cwd())
        file_filter = "TIFF Files (*.tif *.tiff);;All Files (*.*)"
        if save:
            path, _ = QFileDialog.getSaveFileName(self, title, start_dir, file_filter)
        else:
            path, _ = QFileDialog.getOpenFileName(self, title, start_dir, file_filter)
        if path:
            edit.setText(path)

    def _browse_recon_otf(self) -> None:
        self._browse_recon_file(self.edit_recon_otf_path, "Select OTF File")

    def _browse_recon_background(self) -> None:
        self._browse_recon_file(self.edit_recon_background_path, "Select Background File")

    def _browse_recon_output(self) -> None:
        current = self.edit_recon_output_path.text().strip()
        start_dir = current if current else str(Path.cwd())
        path = QFileDialog.getExistingDirectory(self, "Select Reconstruction Output Folder", start_dir)
        if path:
            self.edit_recon_output_path.setText(path)

    def _sync_config_from_widgets(self) -> AppConfig:
        """把控件最新状态同步回 ``self.config``，避免两边读到不同值。"""
        self.config.daq = self._current_daq_config()
        self.config.selected_laser_nm = self._selected_laser_nm()
        self.config.z_scan = read_z_scan_config_from_widgets(self)
        self.config.reconstruction = read_reconstruction_config_from_widgets(self)
        return self.config

    @_catch_to_error
    def _read_current_z_into_start(self) -> None:
        if self.stage_adapter is None:
            raise HardwareError("No Z stage adapter is available.")
        if not getattr(self.stage_adapter, "is_connected", False):
            self.stage_adapter.connect()
        self.spin_zscan_start_um.setValue(float(self.stage_adapter.get_position_um()))
        self.spin_zscan_start_um.setProperty("zscan_auto_start", False)
        self._refresh_zscan_preview()

    def _sync_zscan_enabled_state(self, enabled: bool) -> None:
        for widget in (
            self.spin_zscan_start_um,
            self.btn_zscan_read_current,
            self.combo_zscan_direction,
            self.spin_zscan_step_um,
            self.spin_zscan_num_steps,
            self.combo_zscan_exposure_preset,
        ):
            widget.setEnabled(bool(enabled))
        for widget in (
            getattr(self.ui, "group_zscan_preview", None),
            getattr(self.ui, "group_zscan_test", None),
            getattr(self, "btn_zscan_test", None),
        ):
            if widget is not None:
                widget.setEnabled(bool(enabled))

    def _refresh_zscan_preview(self) -> None:
        """Update z-scan calculated preview labels without connecting or moving hardware."""
        preview_value_labels = (
            self.label_zscan_preview_start_value,
            self.label_zscan_preview_end_value,
            self.label_zscan_preview_distance_value,
            self.label_zscan_preview_eta_value,
            self.label_zscan_preview_capture_eta_value,
        )
        try:
            cfg = read_z_scan_config_from_widgets(self)
            enabled = bool(cfg.enabled)
            self.ui.group_zscan_preview.setEnabled(enabled)
            self.ui.group_zscan_test.setEnabled(enabled)
            self.btn_zscan_test.setEnabled(enabled and self.combo_zscan_test_target.count() > 0)

            has_runtime_start = cfg.start_um is None
            stage_connected = (
                self.stage_adapter is not None
                and bool(getattr(self.stage_adapter, "is_connected", False))
            )
            start_um: float | None
            if has_runtime_start and stage_connected:
                start_um = float(self.stage_adapter.get_position_um())
                start_text = f"{start_um:.3f} um"
            elif has_runtime_start:
                start_um = None
                start_text = "运行时读取当前 Z"
            else:
                start_um = float(self.spin_zscan_start_um.value())
                start_text = f"{start_um:.3f} um"

            direction_sign = 1.0 if cfg.direction == "positive_z" else -1.0
            move_count = int(cfg.num_steps)
            total_um = abs(float(cfg.step_um) * move_count)
            if start_um is None:
                end_text = "--"
            else:
                end_um = start_um + direction_sign * float(cfg.step_um) * move_count
                end_text = f"{end_um:.3f} um"

            try:
                daq_config = self._current_daq_config()
            except Exception:
                daq_config = self.config.daq
            move_only_eta_ms = estimate_z_scan_move_only_total_time_ms(
                cfg,
                records=self._zscan_timing_records,
            )
            capture_eta_ms = estimate_z_scan_capture_test_total_time_ms(
                cfg,
                daq_config=daq_config,
                timing=self.config.timing,
                records=self._zscan_timing_records,
            )

            _set_language_specific_label_text(self.label_zscan_preview_start_value, start_text)
            _set_language_specific_label_text(self.label_zscan_preview_end_value, end_text)
            _set_language_specific_label_text(self.label_zscan_preview_distance_value, f"{total_um:.3f} um")
            _set_language_specific_label_text(self.label_zscan_preview_eta_value, f"{move_only_eta_ms:.1f} ms")
            _set_language_specific_label_text(
                self.label_zscan_preview_capture_eta_value,
                f"{capture_eta_ms:.1f} ms",
            )
        except Exception:
            for label in preview_value_labels:
                _set_language_specific_label_text(label, "--")

    def _append_zscan_timing_history(self, records: list[object], *, refresh_preview: bool = True) -> None:
        if not records:
            return
        append_z_scan_timing_records(records, self._zscan_timing_history_path)
        self._zscan_timing_records.extend(records)
        if refresh_preview or QThread.currentThread() == self.thread():
            self._refresh_zscan_preview()

    def _handle_zscan_test_error(self, err_msg: str) -> None:
        warning = getattr(self, "_pending_zscan_restore_warning", None)
        if warning:
            err_msg = f"{err_msg}\n附加: {warning}"
            self._pending_zscan_restore_warning = None
        slm_warning = getattr(self, "_pending_zscan_slm_warning", None)
        if slm_warning:
            err_msg = f"{err_msg}\n附加: {slm_warning}"
            self._pending_zscan_slm_warning = None
        self.label_zscan_test_status.setText(f"测试失败: {err_msg}")
        self._set_error(err_msg)
        QMessageBox.critical(self, "Z-Scan Test", err_msg)

    def _run_zscan_test(self) -> None:
        """Z-Scan 测试按钮：首次点击启动 worker；测试进行中再次点击取消。"""
        # 如果 worker 正在运行，本次点击是取消请求。
        if self._zscan_test_thread is not None:
            if self._zscan_test_worker is not None:
                self._zscan_test_worker.cancel()
            self.btn_zscan_test.setEnabled(False)
            return

        # 1) 在 GUI 线程完成所有控件读取和前置校验（快速失败，不启动 worker）。
        started_at_s = time.perf_counter()
        target_id = self.combo_zscan_test_target.currentData()
        if not target_id:
            self._handle_zscan_test_error("Please select a Z-scan test target.")
            return
        try:
            self.config.z_scan = read_z_scan_config_from_widgets(self)
        except Exception as exc:
            self._handle_zscan_test_error(str(exc))
            return
        if not self.config.z_scan.enabled:
            self._handle_zscan_test_error("Z-Scan is disabled. Enable z-scan before running tests.")
            return

        # 2) stage_plus_capture 分支需要 daq_config；在 GUI 线程快照，避免 worker 读 Qt 控件。
        daq_config_snapshot: DaqLineConfig | None = None
        if target_id == Z_SCAN_TEST_STAGE_PLUS_CAPTURE:
            try:
                daq_config_snapshot = copy.copy(self._current_daq_config())
                validate_daq_line_config(daq_config_snapshot)
            except Exception as exc:
                self._handle_zscan_test_error(str(exc))
                return

        # 3) 快照 z_scan config（不可变数据，供 worker 直接使用）。
        z_scan_cfg_snapshot = copy.copy(self.config.z_scan)

        # 4) 构造 worker fn 闭包；只捕获快照值和 self（adapter 均为长生命周期对象）。
        def _zscan_fn(stop_event: threading.Event) -> str:
            # 清上次遗留的警告状态（全在 worker 线程内读写，无跨线程竞争）。
            self._pending_zscan_restore_warning = None
            self._pending_zscan_slm_warning = None
            try:
                if target_id == Z_SCAN_TEST_STAGE_ONLY:
                    result = self._run_zscan_stage_only_test(started_at_s, stop_event=stop_event)
                    message_lines = [
                        "位移台测试完成。",
                        f"步数: {int(z_scan_cfg_snapshot.num_steps)}",
                        f"总耗时: {result.total_duration_s * 1000.0:.1f} ms",
                    ]
                    if result.move_latencies_ms:
                        message_lines.append(
                            "每步移动耗时(min/mean/max): "
                            f"{min(result.move_latencies_ms):.2f} / "
                            f"{statistics.mean(result.move_latencies_ms):.2f} / "
                            f"{max(result.move_latencies_ms):.2f} ms"
                        )
                    else:
                        message_lines.append("每步移动耗时: 无 (没有任何一次层间移动成功)")
                elif target_id == Z_SCAN_TEST_STAGE_PLUS_CAPTURE:
                    result = self._run_zscan_stage_plus_capture_test(
                        started_at_s,
                        daq_config_override=daq_config_snapshot,
                        stop_event=stop_event,
                    )
                    score_min = min(result.focus_scores) if result.focus_scores else float("nan")
                    score_max = max(result.focus_scores) if result.focus_scores else float("nan")
                    message_lines = [
                        "位移台 + 每层采图测试完成 (含 SML 评分)。",
                        f"采集帧数: {result.frame_count}",
                        f"SML 选择的最佳层: 第 {result.best_layer_index + 1} 层 / 共 {result.frame_count} 层",
                        f"最佳层 Z: {result.best_z_um:.3f} um",
                        f"SML 分数范围 (min/max): {score_min:.3g} / {score_max:.3g}",
                        f"TIFF 输出: {result.output_path}",
                        f"总耗时: {result.total_duration_s * 1000.0:.1f} ms",
                        "提示: 测试结束后 SLM 当前 RO 仍为 z-scan RO；下一次正式 SIM9 会重新选择正式 RO。",
                    ]
                else:
                    raise HardwareError(f"Unknown Z-scan test target: {target_id!r}")
            except Exception as exc:
                # 把挂起的警告拼入异常消息，让 signal_error 回传完整错误上下文。
                restore_warn = self._pending_zscan_restore_warning
                slm_warn = self._pending_zscan_slm_warning
                self._pending_zscan_restore_warning = None
                self._pending_zscan_slm_warning = None
                extra = "\n".join(filter(None, [restore_warn, slm_warn]))
                if extra:
                    raise HardwareError(f"{exc}\n附加: {extra}") from exc
                raise

            # 成功：把 restore_warning 编码进返回字符串（sentinel + 警告 + 换行 + 消息）。
            restore_warn = self._pending_zscan_restore_warning
            self._pending_zscan_restore_warning = None
            self._pending_zscan_slm_warning = None
            message = "\n".join(message_lines)
            if restore_warn:
                return f"{_ZSCAN_WARN_SEP}{restore_warn}\n{message}"
            return message

        # 5) 构造 stop_event + worker + thread，保存引用，切换按钮为 "Cancel"。
        stop_event = threading.Event()
        worker = _PulseTestWorker(_zscan_fn, stop_event)
        thread = QThread(self)
        worker.moveToThread(thread)
        worker.signal_success.connect(self._on_zscan_test_success)
        worker.signal_error.connect(self._on_zscan_test_error)
        worker.signal_finished.connect(self._on_zscan_test_finished)
        thread.started.connect(worker.run)

        self._zscan_test_stop_event = stop_event
        self._zscan_test_worker = worker
        self._zscan_test_thread = thread
        self.btn_zscan_test.setText("Cancel")
        self.label_zscan_test_status.setText("测试进行中，请稍候…")

        # 6) 启动 worker 线程。
        thread.start()

    @pyqtSlot(str)
    def _on_zscan_test_success(self, msg: str) -> None:
        if msg.startswith(_ZSCAN_WARN_SEP):
            rest = msg[len(_ZSCAN_WARN_SEP):]
            warning, _, message = rest.partition("\n")
            degraded = f"测试已完成，但回起点失败。\n{message}\n警告: {warning}"
            self.label_zscan_test_status.setText(degraded)
            self._set_error(warning)
            QMessageBox.warning(self, "Z-Scan Test", degraded)
        else:
            self._refresh_zscan_preview()
            self.label_zscan_test_status.setText(msg)
            self._set_error("-")
            QMessageBox.information(self, "Z-Scan Test", msg)

    @pyqtSlot(str)
    def _on_zscan_test_error(self, err_msg: str) -> None:
        self.label_zscan_test_status.setText(f"测试失败: {err_msg}")
        self._set_error(err_msg)
        QMessageBox.critical(self, "Z-Scan Test", err_msg)

    @pyqtSlot()
    def _on_zscan_test_finished(self) -> None:
        if self._zscan_test_thread is not None:
            self._zscan_test_thread.quit()
            self._zscan_test_thread.wait()
            self._zscan_test_thread = None
        self._zscan_test_worker = None
        self._zscan_test_stop_event = None
        self.btn_zscan_test.setText("Run Z-Scan Test")
        self.btn_zscan_test.setEnabled(
            bool(self.config.z_scan.enabled) and self.combo_zscan_test_target.count() > 0
        )

    def _zscan_positions_checked(self, cfg: ZScanConfig) -> list[float]:
        if self.stage_adapter is None:
            raise HardwareError("No Z stage adapter is available.")
        if not getattr(self.stage_adapter, "is_connected", False):
            self.stage_adapter.connect()
        positions = scan_positions(cfg, stage_position_um=float(self.stage_adapter.get_position_um()))
        min_um, max_um = self.stage_adapter.get_z_ranges_um()
        out_of_range = [z for z in positions if not (float(min_um) <= float(z) <= float(max_um))]
        if out_of_range:
            preview = ", ".join(f"{z:.3f}" for z in out_of_range[:5])
            suffix = f" ... (共 {len(out_of_range)} 个越界)" if len(out_of_range) > 5 else ""
            raise HardwareError(
                f"Z-Scan 目标位置超出位移台量程 [{float(min_um):.3f}, {float(max_um):.3f}] um："
                f"{preview}{suffix}"
            )
        return [float(z) for z in positions]

    def _run_zscan_stage_only_test(
        self,
        started_at_s: float,
        stop_event: threading.Event | None = None,
    ) -> ZScanTestResult:
        cfg = self.config.z_scan
        positions = self._zscan_positions_checked(cfg)
        started_from_um = float(self.stage_adapter.get_position_um())
        started_movement = False
        position_latencies_ms: list[float] = []
        scan_move_latencies_ms: list[float] = []
        initial_position_ms = 0.0
        restore_ms = 0.0
        try:
            for index, z_um in enumerate(positions):
                # 取消检查点：每次移动前检查 stop_event；已发出的单次移动无法中断，
                # 但取消后不再发起下一次移动，finally 仍尽力回起始层（与正常完成一致）。
                if stop_event is not None and stop_event.is_set():
                    raise HardwareError("Z-Scan 位移台测试已取消。")
                t0 = time.perf_counter()
                self.stage_adapter.move_z_um(z_um)
                started_movement = True
                move_ms = (time.perf_counter() - t0) * 1000.0
                position_latencies_ms.append(move_ms)
                if index == 0:
                    initial_position_ms = move_ms
                else:
                    scan_move_latencies_ms.append(move_ms)
        finally:
            if started_movement:
                restore_started_s = time.perf_counter()
                try:
                    self.stage_adapter.move_z_um(positions[0])
                except Exception as restore_exc:
                    self._pending_zscan_restore_warning = (
                        f"回到起始层失败: {restore_exc}（位移台可能停在中间位置，请人工确认 Z）"
                    )
                finally:
                    restore_ms = (time.perf_counter() - restore_started_s) * 1000.0
        total_duration_s = max(0.0, time.perf_counter() - float(started_at_s))
        self._append_zscan_timing_history(
            [
                *build_z_scan_timing_records(
                    cfg,
                    mode=Z_SCAN_TEST_STAGE_ONLY,
                    positions=positions,
                    move_latencies_ms=position_latencies_ms,
                    started_from_um=started_from_um,
                    success=True,
                ),
                build_z_scan_run_timing_record(
                    cfg,
                    mode=Z_SCAN_TEST_STAGE_ONLY,
                    total_duration_ms=total_duration_s * 1000.0,
                    initial_position_ms=initial_position_ms,
                    scan_move_latencies_ms=scan_move_latencies_ms,
                    restore_ms=restore_ms,
                    success=True,
                ),
            ],
            refresh_preview=False,
        )
        return ZScanTestResult(
            mode=Z_SCAN_TEST_STAGE_ONLY,
            positions_visited=positions,
            total_duration_s=total_duration_s,
            move_latencies_ms=scan_move_latencies_ms,
            output_path=None,
            frame_count=0,
            position_move_latencies_ms=position_latencies_ms,
            initial_position_ms=initial_position_ms,
            restore_ms=restore_ms,
        )

    def _run_zscan_stage_plus_capture_test(
        self,
        started_at_s: float,
        daq_config_override: DaqLineConfig | None = None,
        stop_event: threading.Event | None = None,
    ) -> ZScanTestResult:
        cfg = self.config.z_scan
        daq_config: DaqLineConfig | None = None
        was_camera_connected = False
        camera_cleanup_needed = False
        stage_may_have_moved = False
        positions: list[float] = []
        try:
            if int(cfg.num_steps) > Z_SCAN_TEST_CAPTURE_MAX_STEPS:
                raise HardwareError(
                    f"Z-Scan 联动采图测试最多允许 {Z_SCAN_TEST_CAPTURE_MAX_STEPS} 次移动；"
                    f"当前为 {int(cfg.num_steps)} 次移动，请降低移动次数后重试。"
                )
            if self.stage_adapter is None:
                raise HardwareError("No Z stage adapter is available.")
            if not getattr(self.stage_adapter, "is_connected", False):
                self.stage_adapter.connect()
            if self.slm_adapter is None or not self.slm_adapter.is_connected():
                raise HardwareError("Z-Scan + 采图测试前需要先连接 SLM。")
            if self.camera_adapter is None:
                raise HardwareError("No camera adapter is available.")

            # 优先使用调用方在 GUI 线程快照的 daq_config，避免 worker 线程读 Qt 控件。
            daq_config = daq_config_override if daq_config_override is not None else self._current_daq_config()
            validate_daq_line_config(daq_config)
            positions = self._zscan_positions_checked(cfg)
            running_orders = self.slm_adapter.list_running_orders()
            z_ro_index, _z_ro_name, z_warnings = find_z_scan_running_order(
                running_orders,
                exposure_preset_ms=cfg.exposure_preset_ms,
            )
            if z_ro_index is None:
                detail = "; ".join(z_warnings)
                message = "No z-scan Running Order found."
                if detail:
                    message = f"{message} {detail}"
                raise HardwareError(message)
            self.slm_adapter.select_running_order(z_ro_index)
            self._pending_zscan_slm_warning = "SLM 当前 RO 可能仍为 z-scan RO。"
            was_camera_connected = self._camera_connected_for_test_cleanup()
            camera_cleanup_needed = True
            timing_payloads: dict[int, dict[str, float]] = {}
            best_focus_move_ms = 0.0

            def _on_zscan_status(event: str, payload: dict[str, object]) -> None:
                nonlocal stage_may_have_moved, best_focus_move_ms
                step_index = int(payload.get("step_index", 0) or 0)
                if event == "z_scan_stage_positioned":
                    stage_may_have_moved = True
                    if step_index > 0 and "move_ms" in payload:
                        timing_payloads.setdefault(step_index, {})["move_ms"] = float(payload["move_ms"])
                        timing_payloads[step_index]["from_z_um"] = float(payload.get("from_z_um", payload.get("z_um", 0.0)))
                elif event == "z_scan_progress" and step_index > 0:
                    if "cycle_ms" in payload:
                        timing_payloads.setdefault(step_index, {})["cycle_ms"] = float(payload["cycle_ms"])
                elif event == "z_scan_best_focus_positioned":
                    best_focus_move_ms = float(payload.get("move_ms", 0.0) or 0.0)

            z_result = run_z_scan(
                stage_adapter=self.stage_adapter,
                camera_adapter=self.camera_adapter,
                slm_adapter=self.slm_adapter,
                daq_adapter=self.daq_adapter,
                daq_config=daq_config,
                camera_config=clone_app_config(self.config).camera,
                timing=self.config.timing,
                z_scan_config=cfg,
                waveform_builder=NIDaqWaveformBuilder(),
                stop_event=stop_event,
                on_status=_on_zscan_status,
                keep_captured_stack=True,
            )
            stack = z_result.captured_stack
            if stack is None:
                raise HardwareError("Z-Scan test did not return a captured stack.")
            stack = np.asarray(stack)
            expected_layers = int(cfg.num_steps) + 1
            if stack.ndim != 3 or stack.shape[0] != expected_layers:
                raise HardwareError(f"Z-Scan test returned invalid stack shape: {stack.shape!r}")
            if stack.dtype != np.uint16:
                raise HardwareError(f"Z-Scan test returned {stack.dtype} stack; expected uint16.")
            if len(z_result.focus_curve) != expected_layers:
                raise HardwareError(
                    "Z-Scan test focus curve length "
                    f"{len(z_result.focus_curve)} does not match expected {expected_layers}."
                )

            focus_scores = [float(point.focus_score) for point in z_result.focus_curve]
            position_move_latencies_ms = [
                float(timing_payloads[index + 1]["move_ms"])
                for index in range(len(z_result.focus_curve))
                if "move_ms" in timing_payloads.get(index + 1, {})
            ]
            move_latencies_ms = position_move_latencies_ms[1:]
            cycle_latencies_ms = [
                float(timing_payloads[index + 1]["cycle_ms"])
                for index in range(len(z_result.focus_curve))
                if "cycle_ms" in timing_payloads.get(index + 1, {})
            ]
            capture_nonmove_latencies_ms = [
                max(
                    0.0,
                    float(timing_payloads[index + 1]["cycle_ms"])
                    - float(timing_payloads[index + 1]["move_ms"]),
                )
                for index in range(len(z_result.focus_curve))
                if "move_ms" in timing_payloads.get(index + 1, {})
                and "cycle_ms" in timing_payloads.get(index + 1, {})
            ]
            started_from_um = (
                float(timing_payloads[1]["from_z_um"])
                if 1 in timing_payloads and "from_z_um" in timing_payloads[1]
                else None
            )
            best_layer_index = max(
                range(len(z_result.focus_curve)),
                key=lambda index: z_result.focus_curve[index].focus_score,
            )
            exposure_ms = max(1, int(round(float(cfg.actual_exposure_us) / 1000.0)))
            output_path = self._test_capture_path(
                "zscan_capture",
                f"zscan_{exposure_ms}ms_{int(cfg.num_steps)}moves",
            )
            tiff_started_s = time.perf_counter()
            self._write_uint16_tiff(output_path, stack)
            tiff_write_ms = (time.perf_counter() - tiff_started_s) * 1000.0
            restore_ms = 0.0
            try:
                restore_started_s = time.perf_counter()
                self.stage_adapter.move_z_um(positions[0])
            except Exception as restore_exc:
                self._pending_zscan_restore_warning = (
                    f"回到起始层失败: {restore_exc}（位移台可能停在中间位置，请人工确认 Z）"
                )
            finally:
                restore_ms = (time.perf_counter() - restore_started_s) * 1000.0
            total_duration_s = max(0.0, time.perf_counter() - float(started_at_s))
            self._append_zscan_timing_history(
                [
                    *build_z_scan_timing_records(
                        cfg,
                        mode=Z_SCAN_TEST_STAGE_PLUS_CAPTURE,
                        positions=[float(point.z_um) for point in z_result.focus_curve],
                        move_latencies_ms=position_move_latencies_ms,
                        cycle_latencies_ms=cycle_latencies_ms,
                        started_from_um=started_from_um,
                        success=True,
                    ),
                    build_z_scan_run_timing_record(
                        cfg,
                        mode=Z_SCAN_TEST_STAGE_PLUS_CAPTURE,
                        total_duration_ms=total_duration_s * 1000.0,
                        initial_position_ms=position_move_latencies_ms[0] if position_move_latencies_ms else 0.0,
                        scan_move_latencies_ms=move_latencies_ms,
                        restore_ms=restore_ms,
                        capture_nonmove_latencies_ms=capture_nonmove_latencies_ms,
                        best_focus_move_ms=best_focus_move_ms,
                        tiff_write_ms=tiff_write_ms,
                        success=True,
                    ),
                ],
                refresh_preview=False,
            )
            self._pending_zscan_slm_warning = None
            return ZScanTestResult(
                mode=Z_SCAN_TEST_STAGE_PLUS_CAPTURE,
                positions_visited=[float(point.z_um) for point in z_result.focus_curve],
                total_duration_s=total_duration_s,
                move_latencies_ms=move_latencies_ms,
                output_path=output_path,
                frame_count=int(stack.shape[0]),
                best_layer_index=int(best_layer_index),
                best_z_um=float(z_result.best_z_um),
                focus_scores=focus_scores,
                position_move_latencies_ms=position_move_latencies_ms,
                capture_nonmove_latencies_ms=capture_nonmove_latencies_ms,
                initial_position_ms=position_move_latencies_ms[0] if position_move_latencies_ms else 0.0,
                restore_ms=restore_ms,
                best_focus_move_ms=best_focus_move_ms,
                tiff_write_ms=tiff_write_ms,
            )
        except Exception:
            if stage_may_have_moved and positions and self.stage_adapter is not None:
                try:
                    self.stage_adapter.move_z_um(positions[0])
                except Exception as restore_exc:
                    self._pending_zscan_restore_warning = (
                        f"回到起始层失败: {restore_exc}（位移台可能停在中间位置，请人工确认 Z）"
                    )
            raise
        finally:
            if daq_config is not None:
                try:
                    self.daq_adapter.set_all_low(daq_config.device_name)
                except Exception:
                    logger.warning("Z-scan test cleanup step failed; continuing teardown.", exc_info=True)
            if camera_cleanup_needed:
                try:
                    self._cleanup_test_camera(was_camera_connected)
                except Exception:
                    logger.warning("Z-scan test cleanup step failed; continuing teardown.", exc_info=True)

    def _clear_line_combos(self) -> None:
        """清空所有线位下拉（设备不可用或换设备失败时使用）。"""
        for combo in self.line_combos.values():
            # 屏蔽信号避免 ``clear()`` 触发刷新链路递归。
            combo.blockSignals(True)
            combo.clear()
            combo.blockSignals(False)
        self._refresh_test_targets()

    def _refresh_daq_devices(self) -> None:
        """刷新 DAQ 设备下拉与对应线位下拉；用户点击"刷新"或对话框首次显示时调用。"""
        # 1) 优先复用当前文本，其次回落到 ``self._preferred_daq_device``。
        current_device = self.combo_daq_device.currentText().strip() or self._preferred_daq_device
        devices = self.daq_adapter.list_devices(default_device=current_device or "Dev1")
        # 2) 重建 DAQ 设备下拉：屏蔽信号，clear → add → 选中目标。
        self.combo_daq_device.blockSignals(True)
        self.combo_daq_device.clear()
        self.combo_daq_device.addItems(devices)
        _center_combobox_items(self.combo_daq_device)
        # 3) 选中策略：优先 _preferred_daq_device，其次 current_device，最后 devices[0]。
        selected_device = self._preferred_daq_device if self._preferred_daq_device in devices else ""
        if not selected_device and current_device in devices:
            selected_device = current_device
        if not selected_device and devices:
            selected_device = devices[0]
        if selected_device:
            set_combobox_current_text(self.combo_daq_device, selected_device)
        self.combo_daq_device.blockSignals(False)
        # 4) 找不到任何设备 → 清线位下拉 + 显示错误。
        if not devices:
            self._clear_line_combos()
            self._set_error("No NI DAQ devices detected.")
            return
        # 5) 设备就绪 → 拉对应 port0 line 列表。
        self._refresh_device_lines()

    def _refresh_device_lines(self) -> None:
        """根据当前 DAQ 设备刷新 8 个角色下拉的 line 候选。"""
        # 1) 保留用户当前文本，让 populate 阶段优先复用。
        current_values = {role: combo.currentText() for role, combo in self.line_combos.items()}
        selected_device = self.combo_daq_device.currentText().strip() or self._preferred_daq_device
        lines = self.daq_adapter.list_port0_lines(device_name=selected_device, default_device=selected_device)
        # 2) 设备或 line 不可用 → 清空并报错。
        if not selected_device or not lines:
            self._clear_line_combos()
            if selected_device:
                self._set_error(f"No DAQ lines available for {selected_device}.")
            return
        # 3) 调共享 helper 把 lines 灌进每个角色下拉。
        populate_daq_line_combos(
            self.line_combos,
            lines,
            current_values,
            self.config.daq,
            selected_device,
        )
        # 4) line 变更后测试目标下拉显示文本要重建。
        self._refresh_test_targets()

    def _current_daq_config_for_test_targets(self) -> DaqLineConfig | None:
        """构造"仅用于测试下拉刷新"的临时 DaqLineConfig；任意字段缺失返回 None。"""
        device_name = self.combo_daq_device.currentText().strip()
        if not device_name:
            return None
        selections = {}
        for role, combo in self.line_combos.items():
            value = combo.currentText().strip()
            if not value:
                return None
            selections[role] = value
        return DaqLineConfig(device_name=device_name, **selections)

    def _refresh_test_targets(self) -> None:
        """重建测试目标下拉项；保留当前选中项（按 data 字段对比）。"""
        # 1) 记录当前选中 data，便于重建后回到原选项。
        selected_target = self.combo_test_target.currentData()
        daq_config = self._current_daq_config_for_test_targets()
        self.combo_test_target.blockSignals(True)
        self.combo_test_target.clear()
        # 2) DAQ 配置就绪才填测试项；否则下拉留空。
        if daq_config is not None:
            for target_id, label in build_daq_test_target_items(daq_config):
                self.combo_test_target.addItem(label, target_id)
            _center_combobox_items(self.combo_test_target)
            selected_index = self.combo_test_target.findData(selected_target)
            # 3) 旧选项不在新列表 → 退回 index 0；下拉为空时 selected_index 仍 < 0。
            if selected_index < 0 and self.combo_test_target.count():
                selected_index = 0
            if selected_index >= 0:
                self.combo_test_target.setCurrentIndex(selected_index)
        self.combo_test_target.blockSignals(False)
        # 4) 没有候选项就禁用测试按钮，避免用户点击空选项。
        self.btn_pulse_test.setEnabled(self.combo_test_target.count() > 0)

    def _test_capture_path(self, subdir: str, prefix: str) -> Path:
        """生成测试采集的 TIFF 保存路径：``TEST_CAPTURE_ROOT/subdir/prefix_<ts>.tiff``。"""
        target_dir = TEST_CAPTURE_ROOT / subdir
        target_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return target_dir / f"{prefix}_{timestamp}.tiff"

    def _write_uint16_tiff(self, path: Path, image_data: np.ndarray) -> None:
        """把 uint16 numpy 数组写成 16 位 TIFF。"""
        # 强制 dtype=uint16 防止外部传入 float 或 uint8 影响保存精度。
        tifffile.imwrite(path, np.asarray(image_data, dtype=np.uint16))

    def _camera_connected_for_test_cleanup(self) -> bool:
        """判断测试前相机是否已连接；用于清理路径决定是否保留连接。"""
        try:
            return bool(self.camera_adapter.is_connected())
        except Exception:
            return False

    def _cleanup_test_camera(self, was_connected_before_test: bool) -> None:
        """测试结束后收尾相机：始终 disarm，仅在适当条件下 disconnect。

        策略：
            - 始终 disarm，避免 snapshot 状态遗留。
            - 共享相机 + 测试前已连接 → 不要 disconnect，让主界面继续使用。
            - 否则（独立持有 / 本次测试中才连接） → 测试结束顺手 disconnect。
        """
        try:
            self.camera_adapter.disarm()
        except Exception:
            logger.warning("Test camera teardown step failed; continuing.", exc_info=True)
        if self._camera_externally_owned and was_connected_before_test:
            return
        try:
            self.camera_adapter.disconnect()
        except Exception:
            logger.warning("Test camera teardown step failed; continuing.", exc_info=True)

    def _execute_sim_capture_sequence(
        self,
        daq_config: DaqLineConfig,
        camera_config: CameraConfig,
        plan,
        frame_count: int = 9,
        stop_event: threading.Event | None = None,
    ) -> tuple[np.ndarray, list[float]]:
        """arm → activate SLM RO → play DAQ waveform → read frames.

        Shared by the SIM acquisition test and future production paths.
        Caller is responsible for cleanup (disarm, DAQ all-low).
        """
        self.camera_adapter.apply_config(camera_config)
        self.camera_adapter.arm(frame_count=frame_count)
        self.slm_adapter.activate_prepared_patterns()
        self.daq_adapter.play_waveform(daq_config.device_name, plan, stop_event=stop_event)
        return self.camera_adapter.read_frame_sequence(
            frame_count=frame_count,
            pattern_files=list(self._loaded_pattern_result.pattern_files),
            laser_wavelength_nm=self.config.selected_laser_nm,
            stop_event=stop_event,
        )

    def _run_camera_trigger_test(
        self,
        daq_config: DaqLineConfig,
        stop_event: threading.Event | None = None,
    ) -> Path:
        """对相机触发线发一次 100 ms 脉冲，读单帧并保存 16 位 TIFF。"""
        # 1) 准备输出路径与目标 line index。
        output_path = self._test_capture_path("camera_pulse", "camera_trigger")
        _, _, line_index = parse_line_name(daq_config.camera_trigger_line)
        was_camera_connected = self._camera_connected_for_test_cleanup()
        try:
            # 2) apply_config → arm → 发脉冲 → 读 1 帧；启动前与等帧期间均可取消
            #    （等帧是主要阻塞段，stop_event 直接透传给 read_frame_sequence）。
            if stop_event is not None and stop_event.is_set():
                raise HardwareError("相机触发测试已取消。")
            self.camera_adapter.apply_config(self.config.camera)
            self.camera_adapter.arm(frame_count=1)
            self.daq_adapter.pulse_line(daq_config.device_name, line_index, duration_s=0.1)
            stack, _timestamps = self.camera_adapter.read_frame_sequence(
                frame_count=1,
                pattern_files=[""],
                laser_wavelength_nm=self.config.selected_laser_nm,
                stop_event=stop_event,
            )
            # 3) 保存 stack[0] 为 16 位 TIFF。
            self._write_uint16_tiff(output_path, stack[0])
            return output_path
        finally:
            # 4) 不论成功失败：都 cleanup 相机 + 把 DAQ 全部置低，避免遗留高电平。
            self._cleanup_test_camera(was_camera_connected)
            try:
                self.daq_adapter.set_all_low(daq_config.device_name)
            except Exception:
                logger.warning("SIM teardown step failed; continuing cleanup.", exc_info=True)

    def _run_laser_pulse_test(
        self,
        daq_config: DaqLineConfig,
        target_role: str,
        stop_event: threading.Event | None = None,
    ) -> None:
        """对选中的激光 TTL 线输出 1 秒短脉冲，用于接线确认。"""
        line_name = getattr(daq_config, target_role)
        device_name, _, line_index = parse_line_name(line_name)
        # 已请求取消则直接跳过脉冲；脉冲期间的取消由 pulse_line 分片 sleep 响应（提前拉低）。
        if stop_event is not None and stop_event.is_set():
            return
        # 默认 1 秒脉冲让肉眼足以看到激光器响应；DAQ adapter 内部把上限夹到 100 ms（仿真路径）或长脉冲（真实路径）。
        self.daq_adapter.pulse_line(device_name, line_index, duration_s=1.0, stop_event=stop_event)

    def _run_sim_acquisition_test(
        self,
        daq_config: DaqLineConfig,
        acquisition_started_at_s: float | None = None,
        stop_event: threading.Event | None = None,
        selected_laser_nm: int | None = None,
    ) -> SimAcquisitionTestResult:
        """按当前波长选 RO，执行一次 SIM9 测试采集并保存 stack 到 TIFF。"""
        # 1) 计时起点：用 ``time.perf_counter`` 精确测量端到端耗时。
        if acquisition_started_at_s is None:
            acquisition_started_at_s = time.perf_counter()
        # 2) 必须先连接 SLM；否则没有 RO 列表可挑。
        if not self.slm_adapter.is_connected():
            raise HardwareError("SIM采集测试前需要先连接 SLM。")
        # 3) 使用调用方快照的波长（worker 场景），或从控件读取（GUI 线程直调场景）。
        #    不写 self.config，避免 worker 线程与 GUI 线程同时访问共享对象。
        laser_nm = selected_laser_nm if selected_laser_nm is not None else self._selected_laser_nm()
        camera_config = clone_app_config(self.config).camera
        # 4) 测试用固定曝光 500 ms / 帧间 50 ms，与 SLM RO 桶（≥50 ms）对齐。
        camera_config.exposure_us = SIM_ACQUISITION_TEST_EXPOSURE_US
        timing_config = clone_app_config(self.config).timing
        timing_config.inter_frame_gap_us = SIM_ACQUISITION_TEST_INTER_FRAME_GAP_US
        # 5) 列举 + 选择 RO。
        running_orders = self.slm_adapter.list_running_orders()
        ro_index, ro_name, warnings = find_best_running_order(
            running_orders,
            wavelength_nm=laser_nm,
            exposure_us=camera_config.exposure_us,
        )
        if ro_index is None:
            raise HardwareError("; ".join(warnings) or "未找到匹配的 SLM Running Order。")
        result = self.slm_adapter.select_running_order(ro_index)
        self._loaded_pattern_result = result["pattern_result"]
        self.config.selected_running_order = ro_name

        # 6) 准备输出路径。
        exposure_ms = max(1, int(round(float(camera_config.exposure_us) / 1000.0)))
        output_path = self._test_capture_path(
            "sim_acquisition",
            f"sim_acquisition_{laser_nm}nm_{exposure_ms}ms",
        )
        # 7) 构建波形 plan：与正式采集走同一段代码路径，确保测试与正式一致。
        waveform_builder = NIDaqWaveformBuilder()
        plan = waveform_builder.build(
            daq_config=daq_config,
            timing=timing_config,
            laser_wavelength_nm=laser_nm,
            exposure_us=camera_config.exposure_us,
            frame_count=9,
            include_role_matrix=False,
        )
        was_camera_connected = self._camera_connected_for_test_cleanup()
        try:
            # 8) arm → 激活 SLM RO → 播放 DAQ → 读 9 帧 stack（共享 helper，与正式采集路径一致）。
            stack, _timestamps = self._execute_sim_capture_sequence(
                daq_config=daq_config,
                camera_config=camera_config,
                plan=plan,
                frame_count=9,
                stop_event=stop_event,
            )
            # 9) 计算实际采集耗时 + 写 TIFF；写盘**不**计入 actual_duration。
            actual_duration_s = max(0.0, time.perf_counter() - float(acquisition_started_at_s))
            self._write_uint16_tiff(output_path, stack)
            return SimAcquisitionTestResult(
                output_path=output_path,
                actual_acquisition_duration_s=actual_duration_s,
                daq_waveform_duration_s=float(plan.duration_s),
            )
        finally:
            # 10) 同 ``_run_camera_trigger_test``：相机 cleanup + DAQ 全 0。
            self._cleanup_test_camera(was_camera_connected)
            try:
                self.daq_adapter.set_all_low(daq_config.device_name)
            except Exception:
                logger.warning("SIM teardown step failed; continuing cleanup.", exc_info=True)

    def _run_slm_activation_timing_test(
        self,
        daq_config: DaqLineConfig,
        stop_event: threading.Event | None = None,
        selected_laser_nm: int | None = None,
    ) -> SlmActivationTimingTestResult:
        """实测 slm_enable(EXT_RUN) 拉高 -> RO 进入 ACT 的延迟上界。

        流程：
            1. 按当前波长选 RO 并软件 activate（与 SIM 采集测试一致）。
            2. 读初始激活状态：[HWA h] RO 预期为 0x54 MHW（等待 EXT_RUN）。
            3. ``set_line`` 拉高 slm_enable，``perf_counter`` 轮询激活状态直至
               0x56 ACT 或超时，记录每次状态变迁。
            4. finally：slm_enable 拉低 + DAQ 全 0，硬件安全归位。

        注意：
            USB 轮询单次往返为毫秒级，因此测得的 enable->ACT 耗时是真实 tHWAT
            （规格 5~500 µs）的上界；用于确认门控机理与排查异常，不用于精确计时。
        """
        # 1) 必须先连接 SLM；选 RO 沿用 SIM 采集测试的波长 + 500ms 曝光桶。
        if not self.slm_adapter.is_connected():
            raise HardwareError("SLM激活时序测试前需要先连接 SLM。")
        # 使用调用方快照的波长（worker 场景），不在 worker 线程读 Qt 控件。
        laser_nm = selected_laser_nm if selected_laser_nm is not None else self._selected_laser_nm()
        running_orders = self.slm_adapter.list_running_orders()
        ro_index, ro_name, ro_warnings = find_best_running_order(
            running_orders,
            wavelength_nm=laser_nm,
            exposure_us=SIM_ACQUISITION_TEST_EXPOSURE_US,
        )
        if ro_index is None:
            raise HardwareError("; ".join(ro_warnings) or "未找到匹配的 SLM Running Order。")
        self.slm_adapter.select_running_order(int(ro_index))
        self.slm_adapter.activate_prepared_patterns()
        _, _, enable_line_index = parse_line_name(daq_config.slm_enable_line)
        # 2) 软件 activate 后、enable 拉高前的基线状态。
        initial_state = self.slm_adapter.get_running_order_activation_state()
        # 2a) 旧版 R11CommLib 缺 GetActivationState 时直接拒绝：后续轮询永远拿不到
        #     ACT，只会变成 2 秒 GUI 线程纯自旋；提示用户升级 SDK。
        if initial_state.get("code") is None:
            raise HardwareError(
                "当前 R11CommLib 不支持 R11_RpcRoGetActivationState，无法执行激活时序测试；"
                "请升级 R11CommLib（>= 1.8）后重试。"
            )
        transitions: list[tuple[float, dict[str, Any]]] = []
        reached_active = False
        enable_to_active_ms: float | None = None
        poll_count = 0
        try:
            # 3) 拉高 enable 并尽快开始轮询；不 sleep，轮询间隔即 USB 往返时间。
            started_at = time.perf_counter()
            self.daq_adapter.set_line(daq_config.device_name, enable_line_index, high=True)
            last_code = initial_state.get("code")
            while True:
                state = self.slm_adapter.get_running_order_activation_state()
                poll_count += 1
                elapsed_ms = (time.perf_counter() - started_at) * 1000.0
                if state.get("code") != last_code:
                    transitions.append((elapsed_ms, state))
                    last_code = state.get("code")
                if state.get("code") == R11_ACTIVATION_STATE_ACTIVE:
                    reached_active = True
                    enable_to_active_ms = elapsed_ms
                    break
                if stop_event is not None and stop_event.is_set():
                    break
                if elapsed_ms >= SLM_ACTIVATION_TIMING_TIMEOUT_S * 1000.0:
                    break
        finally:
            # 4) 不论成败：enable 拉低 + DAQ 全 0，避免 SLM 停在 Active Mode。
            try:
                self.daq_adapter.set_line(daq_config.device_name, enable_line_index, high=False)
            except Exception:
                logger.warning("SIM teardown step failed; continuing cleanup.", exc_info=True)
            try:
                self.daq_adapter.set_all_low(daq_config.device_name)
            except Exception:
                logger.warning("SIM teardown step failed; continuing cleanup.", exc_info=True)
        return SlmActivationTimingTestResult(
            running_order_name=str(ro_name),
            initial_state=initial_state,
            transitions=transitions,
            reached_active=reached_active,
            enable_to_active_ms=enable_to_active_ms,
            poll_count=poll_count,
        )

    def _run_pulse_test(self) -> None:
        """测试按钮总入口：首次点击启动 worker；测试运行中再次点击则取消。"""
        # 如果 worker 正在运行，本次点击是取消请求。
        if self._pulse_test_thread is not None:
            if self._pulse_test_worker is not None:
                self._pulse_test_worker.cancel()
            self.btn_pulse_test.setEnabled(False)
            return

        # 1) 校验 DAQ 配置 → 取目标 ID；失败立即报错，不启动 worker。
        acquisition_started_at_s = time.perf_counter()
        try:
            daq_config = self._current_daq_config()
            validate_daq_line_config(daq_config)
        except Exception as exc:
            self._set_error(str(exc))
            QMessageBox.critical(self, "Pulse Test", str(exc))
            return
        target_id = self.combo_test_target.currentData()
        if not target_id:
            self._set_error("Please select a test target.")
            return

        # 闭包捕获快照，防止 worker 运行期间主线程改写同一 DaqLineConfig 对象。
        daq_config = copy.copy(daq_config)
        # 在 GUI 线程快照波长，worker 线程不得读 QButtonGroup 等 Qt 控件。
        selected_laser_nm_snapshot = self._selected_laser_nm()

        # 2) 构造 worker fn 闭包；闭包捕获已校验的不可变值，不捕获 self。
        def _test_fn(stop_event: threading.Event) -> str:
            if target_id == SIM_ACQUISITION_TEST_ID:
                result = self._run_sim_acquisition_test(
                    daq_config,
                    acquisition_started_at_s=acquisition_started_at_s,
                    stop_event=stop_event,
                    selected_laser_nm=selected_laser_nm_snapshot,
                )
                return (
                    "SIM采集测试完成，16位 TIFF 已保存到:\n"
                    f"{result.output_path}\n"
                    f"SIM采集实际用时: {result.actual_acquisition_duration_s * 1000.0:.3f} ms\n"
                    f"DAQ完整播放时长: {result.daq_waveform_duration_s * 1000.0:.3f} ms"
                )
            elif target_id == SLM_ACTIVATION_TIMING_TEST_ID:
                timing_result = self._run_slm_activation_timing_test(
                    daq_config,
                    stop_event=stop_event,
                    selected_laser_nm=selected_laser_nm_snapshot,
                )
                lines = [
                    f"Running Order: {timing_result.running_order_name}",
                    f"软件激活后初始状态: {timing_result.initial_state.get('name')}",
                ]
                for elapsed_ms, state in timing_result.transitions:
                    lines.append(f"+{elapsed_ms:.3f} ms -> {state.get('name')}")
                if timing_result.reached_active and timing_result.enable_to_active_ms is not None:
                    lines.append(
                        f"slm_enable 拉高 -> ACT(active) 用时: {timing_result.enable_to_active_ms:.3f} ms"
                        f"（USB 轮询粒度上界，共 {timing_result.poll_count} 次轮询；"
                        "数据手册 tHWAT 规格 5~500 µs）"
                    )
                else:
                    lines.append(
                        f"超时 {SLM_ACTIVATION_TIMING_TIMEOUT_S:.1f}s 未达到 ACT(active)；"
                        "请检查 slm_enable(EXT_RUN) 接线、RO 激活方式与 R11CommLib 版本。"
                    )
                return "SLM激活时序测试完成:\n" + "\n".join(lines)
            elif target_id == "camera_trigger_line":
                output_path = self._run_camera_trigger_test(daq_config, stop_event=stop_event)
                return f"相机测试完成，16位 TIFF 已保存到:\n{output_path}"
            else:
                self._run_laser_pulse_test(daq_config, str(target_id), stop_event=stop_event)
                return f"{ROLE_LABELS[str(target_id)]} 脉冲测试完成。"

        # 3) 构造 stop_event + worker + thread。
        stop_event = threading.Event()
        worker = _PulseTestWorker(_test_fn, stop_event)
        thread = QThread(self)
        worker.moveToThread(thread)
        worker.signal_success.connect(self._on_pulse_test_success)
        worker.signal_error.connect(self._on_pulse_test_error)
        worker.signal_finished.connect(self._on_pulse_test_finished)
        thread.started.connect(worker.run)

        # 4) 保存引用，切换按钮为"Cancel"模式，刷新状态标签。
        self._pulse_test_stop_event = stop_event
        self._pulse_test_worker = worker
        self._pulse_test_thread = thread
        self._pulse_test_btn_original_text = self.btn_pulse_test.text()
        self.btn_pulse_test.setText("Cancel")
        self._set_error("测试进行中，请稍候…")

        # 5) 启动 worker 线程。
        thread.start()

    @pyqtSlot(str)
    def _on_pulse_test_success(self, message: str) -> None:
        self._set_error("-")
        QMessageBox.information(self, "Pulse Test", message)

    @pyqtSlot(str)
    def _on_pulse_test_error(self, error_message: str) -> None:
        self._set_error(error_message)
        QMessageBox.critical(self, "Pulse Test", error_message)

    @pyqtSlot()
    def _on_pulse_test_finished(self) -> None:
        """Worker 完成（成功/失败/取消）后：停止 thread，恢复按钮和状态。"""
        if self._pulse_test_thread is not None:
            self._pulse_test_thread.quit()
            self._pulse_test_thread.wait()
            self._pulse_test_thread = None
        self._pulse_test_worker = None
        self._pulse_test_stop_event = None
        self.btn_pulse_test.setText(self._pulse_test_btn_original_text)
        self.btn_pulse_test.setEnabled(self.combo_test_target.count() > 0)

    def _save_and_accept(self) -> None:
        """保存按钮：把 UI 同步到配置 → 校验 DAQ → 保存到 JSON → 发信号 → accept。"""
        try:
            # 1) 同步 UI 状态到 config 副本（``clone_app_config`` 复制以免影响外部）。
            config = clone_app_config(self._sync_config_from_widgets())
            # 2) 校验 DAQ 线位与整份配置；失败立刻报错，不写盘。
            validate_daq_line_config(config.daq)
            validation_errors = validate_app_config(config)
            if validation_errors:
                raise ValueError("\n".join(validation_errors))
            # 3) 保存到 JSON：路径优先用 config_path，否则默认。
            save_app_config(config, config.config_path or DEFAULT_CONFIG_PATH)
            # 4) 信号回传新 config → 关闭对话框。
            self.signal_settings_saved.emit(config)
            self.accept()
        except Exception as exc:
            self._set_error(str(exc))
            QMessageBox.critical(self, "Save Settings", str(exc))

    def _set_error(self, message: str) -> None:
        """把错误文本显示在状态标签上；空串或 ``"-"`` 视为"无错误"并隐藏。"""
        # 1) 空白或 "-" → 复位标签到默认值并隐藏，避免占用 UI 空间。
        clean_message = message.strip()
        if not clean_message or clean_message == "-":
            self.lbl_error.setText("-")
            self.lbl_error.setToolTip("")
            self.lbl_error.hide()
            return
        # 2) 有错误：显示文字，并把整段错误塞到 tooltip 便于看完整内容。
        self.lbl_error.setText(clean_message)
        self.lbl_error.setToolTip(clean_message)
        self.lbl_error.show()

    def get_config(self) -> AppConfig:
        """对外暴露当前 UI 同步出来的 ``AppConfig`` 副本。"""
        return clone_app_config(self._sync_config_from_widgets())

    def closeEvent(self, event) -> None:  # noqa: N802
        """关闭对话框时清理"自己创建的"相机/SLM；外部共享的不动。"""
        # 仅当 adapter 由本对话框创建时才 disconnect；共享 adapter 留给外部继续用。
        if not self._camera_externally_owned:
            try:
                self.camera_adapter.disconnect()
            except Exception:
                logger.warning("SIM teardown step failed; continuing cleanup.", exc_info=True)
        if not self._slm_externally_owned:
            try:
                self.slm_adapter.disconnect()
            except Exception:
                logger.warning("SIM teardown step failed; continuing cleanup.", exc_info=True)
        super().closeEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802
        """对话框首次显示时做硬件刷新；``QTimer.singleShot(0)`` 把它放到事件循环下一拍。"""
        super().showEvent(event)
        if self._initial_hardware_refresh_pending:
            self._initial_hardware_refresh_pending = False
            # 0 ms QTimer：在当前 event 处理完之后再执行，避免 showEvent 内同步阻塞。
            QTimer.singleShot(0, self._perform_initial_hardware_refresh)


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
        """创建 405 / 488 / 561 / 647 nm 激光单选按钮组。"""
        group = QGroupBox("Laser Selection")
        layout = QVBoxLayout(group)
        self.laser_group = QButtonGroup(self)
        # 4 个 RadioButton + QButtonGroup 关联，便于 checkedId() 读取选中波长。
        for wavelength in (405, 488, 561, 647):
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
        self.recon_worker.set_reconstruction_config(self.config.reconstruction)

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
        self.recon_worker.set_reconstruction_config(self.config.reconstruction)
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
        """窗口关闭：自动保存配置 → controller shutdown → 关闭两个 pipeline 线程。"""
        try:
            # 1) 把控件状态写回 config 并保存到磁盘。失败时打日志但不阻塞关闭。
            self._sync_config_from_widgets()
            save_app_config(self.config, self.config.config_path or DEFAULT_CONFIG_PATH)
        except Exception:
            self._log(traceback.format_exc())
        # 2) controller.shutdown 会停 worker + 断硬件；最多等 2 秒。
        self.controller.shutdown()
        # 3) 关闭重建 / 特征线程；2 秒超时避免卡住主线程。
        self.recon_thread.quit()
        self.recon_thread.wait(2000)
        self.feature_thread.quit()
        self.feature_thread.wait(2000)
        super().closeEvent(event)

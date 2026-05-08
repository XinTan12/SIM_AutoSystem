from __future__ import annotations

import functools
from datetime import datetime
from pathlib import Path
import traceback

import numpy as np

from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
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

from .adapters import FusionBtCameraAdapter, HardwareError, KopinSlmAdapter, NIDaqAdapter, find_best_running_order
from .config_store import (
    DEFAULT_CONFIG_PATH,
    app_config_from_dict,
    app_config_to_dict,
    load_app_config,
    save_app_config,
)
from .controller import SimAcquisitionController
from .models import (
    AppConfig,
    CameraConfig,
    DAQ_ROLE_ORDER,
    DaqLineConfig,
    SimTaskConfig,
    TimingConfig,
    default_daq_line_name,
)
from .pipeline import DecisionEngine, FeatureWorker, ReconstructionWorker
from .protocols import CameraAdapter, SlmAdapter
from .sim_adapters import SimulatedCameraAdapter, SimulatedDaqAdapter, SimulatedSlmAdapter
from .led_indicator import LedIndicator
from .ui_sim_settings_dialog import Ui_SimSettingsDialog
from .waveform import NIDaqWaveformBuilder, parse_line_name, validate_daq_line_config


def _catch_to_error(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception as exc:
            self._set_error(str(exc))
    return wrapper


def read_timing_config_from_widgets(
    spin_sample_rate: QSpinBox,
    spin_edge_pulse_us: QSpinBox,
    spin_inter_frame_gap_us: QSpinBox,
    spin_slm_enable_guard_us: QSpinBox,
) -> TimingConfig:
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
    spin_sample_rate.setValue(config.sample_rate_hz)
    spin_edge_pulse_us.setValue(config.edge_pulse_us)
    spin_inter_frame_gap_us.setValue(config.inter_frame_gap_us)
    spin_slm_enable_guard_us.setValue(config.slm_enable_guard_us)


def read_selected_laser_nm(laser_group: QButtonGroup) -> int:
    checked = laser_group.checkedId()
    if checked <= 0:
        raise ValueError("Please select one laser wavelength.")
    return checked


def write_selected_laser_to_widgets(
    laser_nm: int,
    laser_buttons: dict[int, QRadioButton],
) -> None:
    laser_button = laser_buttons.get(laser_nm, laser_buttons[488])
    laser_button.setChecked(True)


def read_daq_config_from_line_combos(
    line_combos: dict[str, QComboBox],
    device_name: str | None = None,
) -> DaqLineConfig:
    selected_device_name = None
    if device_name is not None:
        selected_device_name = device_name.strip()
        if not selected_device_name:
            raise ValueError("Please select a DAQ device.")

    selections = {}
    for role, combo in line_combos.items():
        value = combo.currentText().strip()
        if not value:
            raise ValueError(f"DAQ line not selected for {ROLE_LABELS[role]}")
        selections[role] = value

    if selected_device_name is None:
        selected_device_name, _, _ = parse_line_name(next(iter(selections.values())))
    return DaqLineConfig(device_name=selected_device_name, **selections)


def populate_daq_line_combos(
    line_combos: dict[str, QComboBox],
    lines: list[str],
    current_values: dict[str, str],
    config: DaqLineConfig,
    device_name: str,
) -> None:
    for role, combo in line_combos.items():
        target_value = current_values.get(role) or getattr(config, role)
        if target_value and not target_value.startswith(f"{device_name}/"):
            target_value = default_daq_line_name(device_name, role)
        fallback_value = default_daq_line_name(device_name, role)

        combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItems(lines)
            if target_value in lines:
                combo.setCurrentText(target_value)
            elif fallback_value in lines:
                combo.setCurrentText(fallback_value)
        finally:
            combo.blockSignals(False)


def browse_pattern_file(parent: QWidget, index: int) -> str | None:
    path, _ = QFileDialog.getOpenFileName(
        parent,
        f"Select Pattern {index + 1}",
        str(Path.cwd()),
        "Pattern Files (*.bmp *.png *.tif *.tiff *.jpg *.jpeg *.bin);;All Files (*.*)",
    )
    return path or None


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

SIM_ACQUISITION_TEST_ID = "sim_acquisition"
SIM_ACQUISITION_TEST_EXPOSURE_US = 500_000
SIM_ACQUISITION_TEST_INTER_FRAME_GAP_US = 50_000
DAQ_PULSE_TEST_ROLES = (
    "camera_trigger_line",
    "laser_405_line",
    "laser_488_line",
    "laser_561_line",
    "laser_647_line",
)
TEST_CAPTURE_ROOT = Path(__file__).resolve().parent.parent / "test_captures"


def create_camera_adapter_for_backend(backend):
    if backend.simulation_mode:
        return SimulatedCameraAdapter()
    return FusionBtCameraAdapter(sdk_path=backend.fusion_bt_sdk_path)


def create_slm_adapter_for_backend(backend):
    if backend.simulation_mode:
        return SimulatedSlmAdapter()
    return KopinSlmAdapter(sdk_path=backend.slm_sdk_path)


def create_daq_adapter_for_backend(backend):
    if backend.simulation_mode:
        return SimulatedDaqAdapter()
    return NIDaqAdapter()


def clone_app_config(config: AppConfig) -> AppConfig:
    return app_config_from_dict(app_config_to_dict(config))


def build_daq_test_target_items(daq_config: DaqLineConfig) -> list[tuple[str, str]]:
    items = [
        (
            role,
            f"{'Camera Trigger + Capture' if role == 'camera_trigger_line' else ROLE_LABELS[role]} -> "
            f"{getattr(daq_config, role)}",
        )
        for role in DAQ_PULSE_TEST_ROLES
    ]
    items.append((SIM_ACQUISITION_TEST_ID, "SIM采集"))
    return items


class SimSettingsDialog(QDialog):
    signal_settings_saved = pyqtSignal(object)

    def __init__(
        self,
        config: AppConfig | None = None,
        config_path: str | None = None,
        parent: QWidget | None = None,
        slm_adapter: SlmAdapter | None = None,
        camera_adapter: CameraAdapter | None = None,
    ):
        super().__init__(parent)
        source_config = config or load_app_config(config_path or DEFAULT_CONFIG_PATH)
        self.config = clone_app_config(source_config)
        if config_path:
            self.config.config_path = config_path

        self.ui = Ui_SimSettingsDialog()
        self.daq_adapter = create_daq_adapter_for_backend(self.config.backend)
        self.slm_adapter = slm_adapter or create_slm_adapter_for_backend(self.config.backend)
        self.camera_adapter = camera_adapter or create_camera_adapter_for_backend(self.config.backend)
        self._slm_externally_owned = slm_adapter is not None
        self._camera_externally_owned = camera_adapter is not None
        self._preferred_daq_device = self.config.daq.device_name
        self._loaded_pattern_result = None
        self._initial_hardware_refresh_pending = True
        self._build_ui()
        self._wire_signals()
        self._populate_widgets_from_config(self.config)

    def _build_ui(self) -> None:
        self.ui.setupUi(self)

        self.tabs = self.ui.tabs
        self.lbl_error = self.ui.lbl_error
        self.combo_daq_device = self.ui.combo_daq_device
        self.btn_refresh_lines = self.ui.btn_refresh_lines
        self.combo_test_target = self.ui.combo_test_target
        self.btn_pulse_test = self.ui.btn_pulse_test
        self.btn_save_close = self.ui.btn_save_close
        self.btn_cancel = self.ui.btn_cancel

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

        self.spin_sample_rate = self.ui.spin_sample_rate
        self.spin_edge_pulse_us = self.ui.spin_edge_pulse_us
        self.spin_inter_frame_gap_us = self.ui.spin_inter_frame_gap_us
        self.spin_slm_enable_guard_us = self.ui.spin_slm_enable_guard_us

        self.laser_group = QButtonGroup(self)
        self.laser_buttons = {
            405: self.ui.radio_laser_405,
            488: self.ui.radio_laser_488,
            561: self.ui.radio_laser_561,
            647: self.ui.radio_laser_647,
        }
        for wavelength, button in self.laser_buttons.items():
            self.laser_group.addButton(button, wavelength)

    def _wire_signals(self) -> None:
        self.btn_refresh_lines.clicked.connect(self._refresh_daq_devices)
        self.combo_daq_device.currentTextChanged.connect(lambda _text: self._refresh_device_lines())
        self.btn_pulse_test.clicked.connect(self._run_pulse_test)
        self.btn_save_close.clicked.connect(self._save_and_accept)
        self.btn_cancel.clicked.connect(self.reject)
        for combo in self.line_combos.values():
            combo.currentTextChanged.connect(lambda _text: self._refresh_test_targets())

    def _perform_initial_hardware_refresh(self) -> None:
        self._refresh_daq_devices()

    def _populate_widgets_from_config(self, config: AppConfig) -> None:
        self._preferred_daq_device = config.daq.device_name
        write_timing_config_to_widgets(
            config.timing,
            self.spin_sample_rate, self.spin_edge_pulse_us,
            self.spin_inter_frame_gap_us, self.spin_slm_enable_guard_us,
        )
        write_selected_laser_to_widgets(config.selected_laser_nm, self.laser_buttons)

    def _current_daq_config(self) -> DaqLineConfig:
        return read_daq_config_from_line_combos(
            self.line_combos,
            device_name=self.combo_daq_device.currentText(),
        )

    def _current_timing_config(self) -> TimingConfig:
        return read_timing_config_from_widgets(
            self.spin_sample_rate, self.spin_edge_pulse_us,
            self.spin_inter_frame_gap_us, self.spin_slm_enable_guard_us,
        )

    def _selected_laser_nm(self) -> int:
        return read_selected_laser_nm(self.laser_group)

    def _sync_config_from_widgets(self) -> AppConfig:
        self.config.daq = self._current_daq_config()
        self.config.timing = self._current_timing_config()
        self.config.selected_laser_nm = self._selected_laser_nm()
        return self.config

    def _clear_line_combos(self) -> None:
        for combo in self.line_combos.values():
            combo.blockSignals(True)
            combo.clear()
            combo.blockSignals(False)
        self._refresh_test_targets()

    def _refresh_daq_devices(self) -> None:
        current_device = self.combo_daq_device.currentText().strip() or self._preferred_daq_device
        devices = self.daq_adapter.list_devices(default_device=current_device or "Dev1")
        self.combo_daq_device.blockSignals(True)
        self.combo_daq_device.clear()
        self.combo_daq_device.addItems(devices)
        selected_device = self._preferred_daq_device if self._preferred_daq_device in devices else ""
        if not selected_device and current_device in devices:
            selected_device = current_device
        if not selected_device and devices:
            selected_device = devices[0]
        if selected_device:
            self.combo_daq_device.setCurrentText(selected_device)
        self.combo_daq_device.blockSignals(False)
        if not devices:
            self._clear_line_combos()
            self._set_error("No NI DAQ devices detected.")
            return
        self._refresh_device_lines()

    def _refresh_device_lines(self) -> None:
        current_values = {role: combo.currentText() for role, combo in self.line_combos.items()}
        selected_device = self.combo_daq_device.currentText().strip() or self._preferred_daq_device
        lines = self.daq_adapter.list_port0_lines(device_name=selected_device, default_device=selected_device)
        if not selected_device or not lines:
            self._clear_line_combos()
            if selected_device:
                self._set_error(f"No DAQ lines available for {selected_device}.")
            return
        populate_daq_line_combos(
            self.line_combos,
            lines,
            current_values,
            self.config.daq,
            selected_device,
        )
        self._refresh_test_targets()

    def _current_daq_config_for_test_targets(self) -> DaqLineConfig | None:
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
        selected_target = self.combo_test_target.currentData()
        daq_config = self._current_daq_config_for_test_targets()
        self.combo_test_target.blockSignals(True)
        self.combo_test_target.clear()
        if daq_config is not None:
            for target_id, label in build_daq_test_target_items(daq_config):
                self.combo_test_target.addItem(label, target_id)
            selected_index = self.combo_test_target.findData(selected_target)
            if selected_index < 0 and self.combo_test_target.count():
                selected_index = 0
            if selected_index >= 0:
                self.combo_test_target.setCurrentIndex(selected_index)
        self.combo_test_target.blockSignals(False)
        self.btn_pulse_test.setEnabled(self.combo_test_target.count() > 0)

    def _test_capture_path(self, subdir: str, prefix: str) -> Path:
        target_dir = TEST_CAPTURE_ROOT / subdir
        target_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return target_dir / f"{prefix}_{timestamp}.tiff"

    def _write_uint16_tiff(self, path: Path, image_data: np.ndarray) -> None:
        tifffile.imwrite(path, np.asarray(image_data, dtype=np.uint16))

    def _camera_connected_for_test_cleanup(self) -> bool:
        try:
            return bool(self.camera_adapter.is_connected())
        except Exception:
            return False

    def _cleanup_test_camera(self, was_connected_before_test: bool) -> None:
        try:
            self.camera_adapter.disarm()
        except Exception:
            pass
        if self._camera_externally_owned and was_connected_before_test:
            return
        try:
            self.camera_adapter.disconnect()
        except Exception:
            pass

    def _run_camera_trigger_test(self, daq_config: DaqLineConfig) -> Path:
        output_path = self._test_capture_path("camera_pulse", "camera_trigger")
        _, _, line_index = parse_line_name(daq_config.camera_trigger_line)
        was_camera_connected = self._camera_connected_for_test_cleanup()
        try:
            self.camera_adapter.apply_config(self.config.camera)
            self.camera_adapter.arm(frame_count=1)
            self.daq_adapter.pulse_line(daq_config.device_name, line_index, duration_s=0.1)
            stack, _timestamps = self.camera_adapter.read_frame_sequence(
                frame_count=1,
                pattern_files=[""],
                laser_wavelength_nm=self.config.selected_laser_nm,
            )
            self._write_uint16_tiff(output_path, stack[0])
            return output_path
        finally:
            self._cleanup_test_camera(was_camera_connected)
            try:
                self.daq_adapter.set_all_low(daq_config.device_name)
            except Exception:
                pass

    def _run_laser_pulse_test(self, daq_config: DaqLineConfig, target_role: str) -> None:
        line_name = getattr(daq_config, target_role)
        device_name, _, line_index = parse_line_name(line_name)
        self.daq_adapter.pulse_line(device_name, line_index, duration_s=1.0)

    def _run_sim_acquisition_test(self, daq_config: DaqLineConfig) -> Path:
        if not self.slm_adapter.is_connected():
            raise HardwareError("SIM采集测试前需要先连接 SLM。")
        self.config.timing = self._current_timing_config()
        self.config.selected_laser_nm = self._selected_laser_nm()
        camera_config = clone_app_config(self.config).camera
        camera_config.exposure_us = SIM_ACQUISITION_TEST_EXPOSURE_US
        timing_config = clone_app_config(self.config).timing
        timing_config.inter_frame_gap_us = SIM_ACQUISITION_TEST_INTER_FRAME_GAP_US
        running_orders = self.slm_adapter.list_running_orders()
        ro_index, ro_name, warnings = find_best_running_order(
            running_orders,
            wavelength_nm=self.config.selected_laser_nm,
            exposure_us=camera_config.exposure_us,
        )
        if ro_index is None:
            raise HardwareError("; ".join(warnings) or "未找到匹配的 SLM Running Order。")
        result = self.slm_adapter.select_running_order(ro_index)
        self._loaded_pattern_result = result["pattern_result"]
        self.config.selected_running_order = ro_name

        exposure_ms = max(1, int(round(float(camera_config.exposure_us) / 1000.0)))
        output_path = self._test_capture_path(
            "sim_acquisition",
            f"sim_acquisition_{self.config.selected_laser_nm}nm_{exposure_ms}ms",
        )
        waveform_builder = NIDaqWaveformBuilder()
        plan = waveform_builder.build(
            daq_config=daq_config,
            timing=timing_config,
            laser_wavelength_nm=self.config.selected_laser_nm,
            exposure_us=camera_config.exposure_us,
            frame_count=9,
        )
        was_camera_connected = self._camera_connected_for_test_cleanup()
        try:
            self.camera_adapter.apply_config(camera_config)
            self.camera_adapter.arm(frame_count=9)
            self.slm_adapter.activate_prepared_patterns()
            self.daq_adapter.play_waveform(daq_config.device_name, plan)
            stack, _timestamps = self.camera_adapter.read_frame_sequence(
                frame_count=9,
                pattern_files=list(self._loaded_pattern_result.pattern_files),
                laser_wavelength_nm=self.config.selected_laser_nm,
            )
            self._write_uint16_tiff(output_path, stack)
            return output_path
        finally:
            self._cleanup_test_camera(was_camera_connected)
            try:
                self.daq_adapter.set_all_low(daq_config.device_name)
            except Exception:
                pass

    def _run_pulse_test(self) -> None:
        try:
            daq_config = self._current_daq_config()
            validate_daq_line_config(daq_config)
            target_id = self.combo_test_target.currentData()
            if not target_id:
                raise ValueError("Please select a test target.")
            if target_id == SIM_ACQUISITION_TEST_ID:
                output_path = self._run_sim_acquisition_test(daq_config)
                message = f"SIM采集测试完成，16位 TIFF 已保存到:\n{output_path}"
            elif target_id == "camera_trigger_line":
                output_path = self._run_camera_trigger_test(daq_config)
                message = f"相机测试完成，16位 TIFF 已保存到:\n{output_path}"
            else:
                self._run_laser_pulse_test(daq_config, str(target_id))
                message = f"{ROLE_LABELS[str(target_id)]} 脉冲测试完成。"
            self._set_error("-")
            QMessageBox.information(self, "Pulse Test", message)
        except Exception as exc:
            self._set_error(str(exc))
            QMessageBox.critical(self, "Pulse Test", str(exc))

    def _save_and_accept(self) -> None:
        try:
            config = clone_app_config(self._sync_config_from_widgets())
            validate_daq_line_config(config.daq)
            save_app_config(config, config.config_path or DEFAULT_CONFIG_PATH)
            self.signal_settings_saved.emit(config)
            self.accept()
        except Exception as exc:
            self._set_error(str(exc))
            QMessageBox.critical(self, "Save Settings", str(exc))

    def _set_error(self, message: str) -> None:
        clean_message = message.strip()
        if not clean_message or clean_message == "-":
            self.lbl_error.setText("-")
            self.lbl_error.setToolTip("")
            self.lbl_error.hide()
            return
        self.lbl_error.setText(clean_message)
        self.lbl_error.setToolTip(clean_message)
        self.lbl_error.show()

    def get_config(self) -> AppConfig:
        return clone_app_config(self._sync_config_from_widgets())

    def closeEvent(self, event) -> None:  # noqa: N802
        if not self._camera_externally_owned:
            try:
                self.camera_adapter.disconnect()
            except Exception:
                pass
        if not self._slm_externally_owned:
            try:
                self.slm_adapter.disconnect()
            except Exception:
                pass
        super().closeEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self._initial_hardware_refresh_pending:
            self._initial_hardware_refresh_pending = False
            QTimer.singleShot(0, self._perform_initial_hardware_refresh)


class SimControlWindow(QMainWindow):
    def __init__(self, config_path: str | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.config = load_app_config(config_path or DEFAULT_CONFIG_PATH)
        self.controller = SimAcquisitionController(self.config.backend, self)
        self.decision_engine = DecisionEngine()
        self.recon_thread = QThread(self)
        self.recon_worker = ReconstructionWorker()
        self.recon_worker.moveToThread(self.recon_thread)
        self.recon_thread.start()
        self.feature_thread = QThread(self)
        self.feature_worker = FeatureWorker()
        self.feature_worker.moveToThread(self.feature_thread)
        self.feature_thread.start()

        self.line_combos: dict[str, QComboBox] = {}
        self.pattern_edits: list[QLineEdit] = []
        self.laser_buttons: dict[int, QRadioButton] = {}
        self.pipeline_labels: dict[str, QLabel] = {}
        self.hardware_leds: dict[str, LedIndicator] = {}
        self.current_task_id = "-"
        self.current_laser_nm = self.config.selected_laser_nm

        self._build_ui()
        self._wire_signals()
        self._populate_widgets_from_config(self.config)
        self._refresh_device_lines()
        self._log(f"Loaded config: {self.config.config_path}")

    def _build_ui(self) -> None:
        self.setWindowTitle("SIM Control Window")
        self.resize(1500, 980)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        container = QWidget()
        root_layout = QVBoxLayout(container)
        root_layout.setSpacing(12)

        top_layout = QGridLayout()
        top_layout.addWidget(self._create_daq_group(), 0, 0)
        top_layout.addWidget(self._create_camera_group(), 0, 1)
        top_layout.addWidget(self._create_pattern_group(), 1, 0)
        top_layout.addWidget(self._create_laser_group(), 1, 1)
        root_layout.addLayout(top_layout)
        root_layout.addWidget(self._create_control_group())
        root_layout.addWidget(self._create_pipeline_group())
        root_layout.addWidget(self._create_log_group())
        root_layout.addStretch(1)

        scroll.setWidget(container)
        self.setCentralWidget(scroll)

    def _create_daq_group(self) -> QGroupBox:
        group = QGroupBox("DAQ Wiring")
        layout = QVBoxLayout(group)
        form = QFormLayout()
        for role in DAQ_ROLE_ORDER:
            combo = QComboBox()
            combo.setEditable(False)
            self.line_combos[role] = combo
            form.addRow(ROLE_LABELS[role], combo)
        layout.addLayout(form)

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
        group = QGroupBox("Fusion BT Camera")
        layout = QVBoxLayout(group)

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
        group = QGroupBox("SLM Pattern Preparation")
        layout = QVBoxLayout(group)
        form = QFormLayout()
        for index in range(9):
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            edit = QLineEdit()
            browse = QPushButton("Browse")
            browse.clicked.connect(lambda _checked=False, idx=index: self._browse_pattern(idx))
            row_layout.addWidget(edit)
            row_layout.addWidget(browse)
            self.pattern_edits.append(edit)
            form.addRow(f"Pattern {index + 1}", row)
        layout.addLayout(form)
        self.btn_program_patterns = QPushButton("Program Patterns")
        layout.addWidget(self.btn_program_patterns)
        return group

    def _create_laser_group(self) -> QGroupBox:
        group = QGroupBox("Laser Selection")
        layout = QVBoxLayout(group)
        self.laser_group = QButtonGroup(self)
        for wavelength in (405, 488, 561, 647):
            button = QRadioButton(f"{wavelength} nm")
            self.laser_group.addButton(button, wavelength)
            self.laser_buttons[wavelength] = button
            layout.addWidget(button)
        layout.addStretch(1)
        return group

    def _create_control_group(self) -> QGroupBox:
        group = QGroupBox("Acquisition Control")
        layout = QGridLayout(group)
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

        hardware_row = QHBoxLayout()
        for key, label_text in (("camera", "Camera"), ("slm", "SLM"), ("daq", "DAQ")):
            led = LedIndicator("gray")
            self.hardware_leds[key] = led
            hardware_row.addWidget(led)
            hardware_row.addWidget(QLabel(label_text))
        hardware_row.addStretch(1)
        layout.addLayout(hardware_row, 1, 0, 1, 5)

        self.progress_acquisition = QProgressBar()
        self.progress_acquisition.setRange(0, 9)
        self.progress_acquisition.setValue(0)
        self.progress_acquisition.setFormat("%v/9")
        layout.addWidget(QLabel("Acquisition Progress"), 2, 0)
        layout.addWidget(self.progress_acquisition, 2, 1, 1, 4)

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
        group = QGroupBox("Pipeline Status")
        form = QFormLayout(group)
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
        group = QGroupBox("Log")
        layout = QVBoxLayout(group)
        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        layout.addWidget(self.log_output)
        return group

    def _wire_signals(self) -> None:
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

        self.controller.signal_status_changed.connect(self._handle_status_changed)
        self.controller.signal_acquisition_failed.connect(self._handle_acquisition_failed)
        self.controller.signal_acquisition_ready.connect(self._handle_acquisition_ready)
        self.controller.signal_acquisition_ready.connect(self.recon_worker.slot_reconstruct)
        self.recon_worker.signal_reconstruction_ready.connect(self._handle_reconstruction_ready)
        self.recon_worker.signal_reconstruction_ready.connect(self.feature_worker.slot_extract)
        self.recon_worker.signal_reconstruction_failed.connect(self._handle_reconstruction_failed)
        self.feature_worker.signal_features_ready.connect(self._handle_feature_ready)
        self.feature_worker.signal_features_failed.connect(self._handle_feature_failed)

    def _populate_widgets_from_config(self, config: AppConfig) -> None:
        self.spin_roi_x.setValue(config.camera.roi_x)
        self.spin_roi_y.setValue(config.camera.roi_y)
        self.spin_roi_width.setValue(config.camera.roi_width)
        self.spin_roi_height.setValue(config.camera.roi_height)
        self.spin_exposure_us.setValue(config.camera.exposure_us)
        self.spin_timeout_ms.setValue(config.camera.timeout_ms)
        write_timing_config_to_widgets(
            config.timing,
            self.spin_sample_rate, self.spin_edge_pulse_us,
            self.spin_inter_frame_gap_us, self.spin_slm_enable_guard_us,
        )
        for index, edit in enumerate(self.pattern_edits):
            edit.setText(config.pattern_files[index])
        write_selected_laser_to_widgets(config.selected_laser_nm, self.laser_buttons)

    def _current_daq_config(self) -> DaqLineConfig:
        return read_daq_config_from_line_combos(self.line_combos)

    def _current_camera_config(self) -> CameraConfig:
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
        return read_timing_config_from_widgets(
            self.spin_sample_rate, self.spin_edge_pulse_us,
            self.spin_inter_frame_gap_us, self.spin_slm_enable_guard_us,
        )

    def _current_pattern_files(self) -> list[str]:
        return [edit.text().strip() for edit in self.pattern_edits]

    def _selected_laser_nm(self) -> int:
        return read_selected_laser_nm(self.laser_group)

    def _sync_config_from_widgets(self) -> None:
        self.config.daq = self._current_daq_config()
        self.config.camera = self._current_camera_config()
        self.config.timing = self._current_timing_config()
        self.config.pattern_files = self._current_pattern_files()
        self.config.selected_laser_nm = self._selected_laser_nm()

    def _refresh_device_lines(self) -> None:
        current_values = {role: combo.currentText() for role, combo in self.line_combos.items()}
        default_device = self.config.daq.device_name
        try:
            lines = self.controller.refresh_available_lines()
        except Exception:
            lines = [f"{default_device}/port0/line{i}" for i in range(16)]
        if not lines:
            lines = [f"{default_device}/port0/line{i}" for i in range(16)]

        populate_daq_line_combos(
            self.line_combos,
            lines,
            current_values,
            self.config.daq,
            default_device,
        )
        self._log(f"Loaded {len(lines)} DAQ line options.")

    def _validate_wiring(self) -> None:
        try:
            validate_daq_line_config(self._current_daq_config())
            self._log("DAQ wiring validation passed.")
            QMessageBox.information(self, "DAQ Wiring", "DAQ wiring validation passed.")
        except Exception as exc:
            self._set_error(str(exc))
            QMessageBox.critical(self, "DAQ Wiring", str(exc))

    @_catch_to_error
    def _load_config_from_disk(self) -> None:
        self.config = load_app_config(self.config.config_path or DEFAULT_CONFIG_PATH)
        self._populate_widgets_from_config(self.config)
        self._refresh_device_lines()
        self._log(f"Config loaded from {self.config.config_path}")

    @_catch_to_error
    def _save_config_to_disk(self) -> None:
        self._sync_config_from_widgets()
        path = save_app_config(self.config, self.config.config_path or DEFAULT_CONFIG_PATH)
        self._log(f"Config saved to {path}")

    @_catch_to_error
    def _initialize_hardware(self) -> None:
        self._sync_config_from_widgets()
        self.controller.apply_daq_config(self.config.daq)
        self.controller.initialize_hardware()

    @_catch_to_error
    def _initialize_camera(self) -> None:
        self.controller.initialize_camera()

    @_catch_to_error
    def _apply_camera_settings(self) -> None:
        self._sync_config_from_widgets()
        self.controller.apply_camera_config(self.config.camera)

    @_catch_to_error
    def _arm_camera(self) -> None:
        self.controller.arm_camera(frame_count=9)

    @_catch_to_error
    def _disarm_camera(self) -> None:
        self.controller.disarm_camera()

    @_catch_to_error
    def _program_patterns(self) -> None:
        self._sync_config_from_widgets()
        self.controller.prepare_patterns(self.config.pattern_files)

    @_catch_to_error
    def _prepare_experiment(self) -> None:
        self._sync_config_from_widgets()
        self.controller.apply_daq_config(self.config.daq)
        self.controller.initialize_hardware()
        self.controller.apply_camera_config(self.config.camera)
        plan = self.controller.waveform_builder.build(
            daq_config=self.config.daq,
            timing=self.config.timing,
            laser_wavelength_nm=self.config.selected_laser_nm,
            exposure_us=self.config.camera.exposure_us,
            frame_count=9,
        )
        for warning in plan.warnings:
            self._log(f"WARNING: {warning}")
        self.controller.prepare_patterns(self.config.pattern_files)
        self._log("Experiment prepared.")

    @_catch_to_error
    def _run_single_acquisition(self) -> None:
        self._sync_config_from_widgets()
        task = SimTaskConfig(
            laser_wavelength_nm=self.config.selected_laser_nm,
            pattern_files=list(self.config.pattern_files),
            camera=self.config.camera,
            timing=self.config.timing,
        )
        task_id = self.controller.start_single_acquisition(task)
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
        self.controller.stop()
        self._log("Stop requested.")

    def _clear_result(self) -> None:
        self.current_task_id = "-"
        self.lbl_current_frame.setText("-")
        self.lbl_current_pattern.setText("-")
        self.lbl_current_task.setText("-")
        for label in self.pipeline_labels.values():
            label.setText("-")
        self._set_error("-", log_message=False)
        self._log("Cleared last result.")

    def _handle_status_changed(self, state: str, payload: dict) -> None:
        self.lbl_current_status.setText(state)
        self._update_hardware_leds(state)
        if state == "acquisition_starting":
            self.progress_acquisition.setValue(0)
        if state == "frame_captured":
            frame_index = payload.get("frame_index", "-")
            self.lbl_current_frame.setText(str(frame_index))
            self.lbl_current_pattern.setText(str(frame_index))
            try:
                self.progress_acquisition.setValue(int(frame_index))
            except (TypeError, ValueError):
                pass
        if state == "acquisition_complete":
            self.progress_acquisition.setValue(9)
            self.pipeline_labels["stack_shape"].setText(str(payload.get("stack_shape", "-")))
        self._log(f"[{state}] {payload}")

    def _update_hardware_leds(self, state: str) -> None:
        if state == "hardware_initializing":
            for led in self.hardware_leds.values():
                led.set_state("yellow")
        elif state in {"hardware_initialized", "camera_initialized", "camera_connected", "camera_config_applied", "camera_armed"}:
            self.hardware_leds["camera"].set_state("green")
        elif state == "camera_disconnected":
            self.hardware_leds["camera"].set_state("red")
        elif state in {"slm_connected", "patterns_prepared"}:
            self.hardware_leds["slm"].set_state("green")
        elif state == "slm_disconnected":
            self.hardware_leds["slm"].set_state("red")
        elif state == "daq_config_applied":
            self.hardware_leds["daq"].set_state("green")
        elif state.endswith("failed") or state.endswith("error"):
            for led in self.hardware_leds.values():
                led.set_state("red")

    def _handle_acquisition_ready(self, batch) -> None:
        self.pipeline_labels["task_id"].setText(batch.task_id)
        self.pipeline_labels["stack_shape"].setText(str(list(batch.stack.shape)))
        self.pipeline_labels["stack_dtype"].setText(str(batch.stack.dtype))
        self.pipeline_labels["reconstruction"].setText("Running")

    def _handle_acquisition_failed(self, task_id: str, message: str) -> None:
        self.pipeline_labels["reconstruction"].setText("Acquisition failed")
        self.progress_acquisition.setValue(0)
        for led in self.hardware_leds.values():
            led.set_state("red")
        self._set_error(f"Acquisition failed for {task_id}: {message}")

    def _handle_reconstruction_ready(self, recon_result) -> None:
        self.pipeline_labels["reconstruction"].setText("Ready")
        self.pipeline_labels["features"].setText("Running")
        self._log(f"Reconstruction ready for {recon_result.task_id}")

    def _handle_reconstruction_failed(self, task_id: str, message: str) -> None:
        self.pipeline_labels["reconstruction"].setText("Failed")
        self._set_error(f"Reconstruction failed for {task_id}: {message}")

    def _handle_feature_ready(self, feature_result) -> None:
        self.pipeline_labels["features"].setText("Ready")
        decision = self.decision_engine.decide(feature_result)
        self.pipeline_labels["decision"].setText(f"{decision.decision} | {decision.reason}")
        self._log(f"Decision for {feature_result.task_id}: {decision.decision}")

    def _handle_feature_failed(self, task_id: str, message: str) -> None:
        self.pipeline_labels["features"].setText("Failed")
        self._set_error(f"Feature extraction failed for {task_id}: {message}")

    def _browse_pattern(self, index: int) -> None:
        path = browse_pattern_file(self, index)
        if path:
            self.pattern_edits[index].setText(path)

    def _log(self, message: str) -> None:
        self.log_output.appendPlainText(message)

    def _set_error(self, message: str, log_message: bool = True) -> None:
        self.lbl_current_error.setText(message)
        if log_message:
            self._log(f"ERROR: {message}")

    def closeEvent(self, event) -> None:  # noqa: N802
        try:
            self._sync_config_from_widgets()
            save_app_config(self.config, self.config.config_path or DEFAULT_CONFIG_PATH)
        except Exception:
            self._log(traceback.format_exc())
        self.controller.shutdown()
        self.recon_thread.quit()
        self.recon_thread.wait(2000)
        self.feature_thread.quit()
        self.feature_thread.wait(2000)
        super().closeEvent(event)


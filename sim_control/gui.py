from __future__ import annotations

from datetime import datetime
from pathlib import Path
import traceback

import numpy as np

from PyQt5.QtCore import Qt, QThread, pyqtSignal
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
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
    QComboBox,
)
import tifffile

from .adapters import FusionBtCameraAdapter, HardwareError, KopinSlmAdapter, NIDaqAdapter
from .config_store import (
    DEFAULT_CONFIG_PATH,
    app_config_from_dict,
    app_config_to_dict,
    load_app_config,
    save_app_config,
)
from .controller import SimAcquisitionController
from .models import AppConfig, DAQ_ROLE_ORDER, DaqLineConfig, SimTaskConfig, TimingConfig
from .pipeline import DecisionEngine, FeatureWorker, ReconstructionWorker
from .ui_sim_settings_dialog import Ui_SimSettingsDialog
from .waveform import NIDaqWaveformBuilder, parse_line_name, validate_daq_line_config


ROLE_LABELS = {
    "slm_enable_line": "SLM Enable",
    "slm_trigger_line": "SLM Trigger",
    "slm_finish_line": "SLM Finish",
    "camera_trigger_line": "Camera Trigger",
    "laser_405_line": "Laser 405",
    "laser_488_line": "Laser 488",
    "laser_561_line": "Laser 561",
    "laser_640_line": "Laser 640",
}

SIM_ACQUISITION_TEST_ID = "sim_acquisition"
DAQ_PULSE_TEST_ROLES = (
    "camera_trigger_line",
    "laser_405_line",
    "laser_488_line",
    "laser_561_line",
    "laser_640_line",
)
TEST_CAPTURE_ROOT = Path(__file__).resolve().parent.parent / "test_captures"


def clone_app_config(config: AppConfig) -> AppConfig:
    return app_config_from_dict(app_config_to_dict(config))


def build_daq_test_target_items(daq_config: DaqLineConfig) -> list[tuple[str, str]]:
    items = [
        (role, f"{ROLE_LABELS[role]} -> {getattr(daq_config, role)}")
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
    ):
        super().__init__(parent)
        source_config = config or load_app_config(config_path or DEFAULT_CONFIG_PATH)
        self.config = clone_app_config(source_config)
        if config_path:
            self.config.config_path = config_path

        self.ui = Ui_SimSettingsDialog()
        self.daq_adapter = NIDaqAdapter()
        self.slm_adapter = KopinSlmAdapter(sdk_path=self.config.backend.slm_sdk_path)
        self._preferred_daq_device = self.config.daq.device_name
        self._loaded_pattern_result = None
        self._build_ui()
        self._wire_signals()
        self._populate_widgets_from_config(self.config)
        self._refresh_daq_devices()
        self._refresh_slm_devices()
        self._update_slm_controls()

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
            "laser_640_line": self.ui.combo_laser_640_line,
        }

        self.spin_sample_rate = self.ui.spin_sample_rate
        self.spin_edge_pulse_us = self.ui.spin_edge_pulse_us
        self.spin_inter_frame_gap_us = self.ui.spin_inter_frame_gap_us
        self.spin_slm_enable_guard_us = self.ui.spin_slm_enable_guard_us

        self.pattern_edits = [
            self.ui.edit_pattern_1,
            self.ui.edit_pattern_2,
            self.ui.edit_pattern_3,
            self.ui.edit_pattern_4,
            self.ui.edit_pattern_5,
            self.ui.edit_pattern_6,
            self.ui.edit_pattern_7,
            self.ui.edit_pattern_8,
            self.ui.edit_pattern_9,
        ]
        self.pattern_browse_buttons = [
            self.ui.btn_browse_pattern_1,
            self.ui.btn_browse_pattern_2,
            self.ui.btn_browse_pattern_3,
            self.ui.btn_browse_pattern_4,
            self.ui.btn_browse_pattern_5,
            self.ui.btn_browse_pattern_6,
            self.ui.btn_browse_pattern_7,
            self.ui.btn_browse_pattern_8,
            self.ui.btn_browse_pattern_9,
        ]
        self.combo_slm_device = self.ui.combo_slm_device
        self.btn_connect_slm = self.ui.btn_connect_slm
        self.btn_disconnect_slm = self.ui.btn_disconnect_slm
        self.btn_load_patterns = self.ui.btn_load_patterns
        self.lbl_slm_status_value = self.ui.lbl_slm_status_value

        self.laser_group = QButtonGroup(self)
        self.laser_buttons = {
            405: self.ui.radio_laser_405,
            488: self.ui.radio_laser_488,
            561: self.ui.radio_laser_561,
            640: self.ui.radio_laser_640,
        }
        for wavelength, button in self.laser_buttons.items():
            self.laser_group.addButton(button, wavelength)

    def _wire_signals(self) -> None:
        self.btn_refresh_lines.clicked.connect(self._refresh_daq_devices)
        self.combo_daq_device.currentTextChanged.connect(lambda _text: self._refresh_device_lines())
        self.btn_pulse_test.clicked.connect(self._run_pulse_test)
        self.btn_save_close.clicked.connect(self._save_and_accept)
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_connect_slm.clicked.connect(self._connect_slm)
        self.btn_disconnect_slm.clicked.connect(self._disconnect_slm)
        self.btn_load_patterns.clicked.connect(self._load_patterns)
        for combo in self.line_combos.values():
            combo.currentTextChanged.connect(lambda _text: self._refresh_test_targets())
        for index, button in enumerate(self.pattern_browse_buttons):
            button.clicked.connect(lambda _checked=False, idx=index: self._browse_pattern(idx))

    def _populate_widgets_from_config(self, config: AppConfig) -> None:
        self._preferred_daq_device = config.daq.device_name
        self.spin_sample_rate.setValue(config.timing.sample_rate_hz)
        self.spin_edge_pulse_us.setValue(config.timing.edge_pulse_us)
        self.spin_inter_frame_gap_us.setValue(config.timing.inter_frame_gap_us)
        self.spin_slm_enable_guard_us.setValue(config.timing.slm_enable_guard_us)
        for index, pattern_path in enumerate(config.pattern_files):
            self._set_pattern_path(index, pattern_path)
        laser_button = self.laser_buttons.get(config.selected_laser_nm, self.laser_buttons[488])
        laser_button.setChecked(True)

    def _current_daq_config(self) -> DaqLineConfig:
        device_name = self.combo_daq_device.currentText().strip()
        if not device_name:
            raise ValueError("Please select a DAQ device.")
        selections = {}
        for role, combo in self.line_combos.items():
            value = combo.currentText().strip()
            if not value:
                raise ValueError(f"DAQ line not selected for {ROLE_LABELS[role]}")
            selections[role] = value
        return DaqLineConfig(device_name=device_name, **selections)

    def _current_timing_config(self) -> TimingConfig:
        return TimingConfig(
            sample_rate_hz=self.spin_sample_rate.value(),
            edge_pulse_us=self.spin_edge_pulse_us.value(),
            inter_frame_gap_us=self.spin_inter_frame_gap_us.value(),
            slm_enable_guard_us=self.spin_slm_enable_guard_us.value(),
        )

    def _current_pattern_files(self) -> list[str]:
        files = []
        for edit in self.pattern_edits:
            files.append((edit.property("full_path") or "").strip())
        return files

    def _selected_laser_nm(self) -> int:
        checked = self.laser_group.checkedId()
        if checked <= 0:
            raise ValueError("Please select one laser wavelength.")
        return checked

    def _sync_config_from_widgets(self) -> AppConfig:
        self.config.daq = self._current_daq_config()
        self.config.timing = self._current_timing_config()
        self.config.pattern_files = self._current_pattern_files()
        self.config.selected_laser_nm = self._selected_laser_nm()
        return self.config

    def _clear_line_combos(self) -> None:
        for combo in self.line_combos.values():
            combo.blockSignals(True)
            combo.clear()
            combo.blockSignals(False)
        self._refresh_test_targets()

    def _default_line_for_role(self, device_name: str, role: str) -> str:
        return f"{device_name}/port0/line{DAQ_ROLE_ORDER.index(role)}"

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
        for role, combo in self.line_combos.items():
            target_value = current_values.get(role) or getattr(self.config.daq, role)
            if target_value and not target_value.startswith(f"{selected_device}/"):
                target_value = self._default_line_for_role(selected_device, role)
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(lines)
            if target_value in lines:
                combo.setCurrentText(target_value)
            else:
                fallback_value = self._default_line_for_role(selected_device, role)
                if fallback_value in lines:
                    combo.setCurrentText(fallback_value)
            combo.blockSignals(False)
        self._refresh_test_targets()

    def _refresh_slm_devices(self) -> None:
        selected_path = self.combo_slm_device.currentData()
        try:
            devices = self.slm_adapter.list_devices()
        except Exception as exc:
            self.combo_slm_device.clear()
            self.lbl_slm_status_value.setText("Unavailable")
            self._update_slm_controls()
            self._set_error(str(exc))
            return
        self.combo_slm_device.blockSignals(True)
        self.combo_slm_device.clear()
        for device in devices:
            self.combo_slm_device.addItem(device["display"], device["path"])
        if selected_path:
            index = self.combo_slm_device.findData(selected_path)
            if index >= 0:
                self.combo_slm_device.setCurrentIndex(index)
        self.combo_slm_device.blockSignals(False)
        if not devices:
            self.lbl_slm_status_value.setText("No SLM detected")
        self._update_slm_controls()

    def _selected_slm_device_path(self) -> str:
        device_path = (self.combo_slm_device.currentData() or "").strip()
        if not device_path:
            raise ValueError("Please select an SLM device.")
        return device_path

    def _update_slm_controls(self, status_message: str | None = None) -> None:
        connected = self.slm_adapter.is_connected()
        has_devices = self.combo_slm_device.count() > 0
        self.combo_slm_device.setEnabled(has_devices and not connected)
        self.btn_connect_slm.setEnabled(has_devices and not connected)
        self.btn_disconnect_slm.setEnabled(connected)
        self.btn_load_patterns.setEnabled(connected)
        if status_message is not None:
            self.lbl_slm_status_value.setText(status_message)
            return
        if connected:
            info = self.slm_adapter.connection_info()
            serial = info.get("device_serial_hint") or info.get("serial_number") or "Connected"
            self.lbl_slm_status_value.setText(f"Connected: {serial}")
        elif has_devices:
            self.lbl_slm_status_value.setText("Not connected")
        else:
            self.lbl_slm_status_value.setText("No SLM detected")

    def _connect_slm(self) -> None:
        try:
            device_path = self._selected_slm_device_path()
            info = self.slm_adapter.connect(device_path=device_path)
            serial = info.get("device_serial_hint") or info.get("serial_number") or "Connected"
            self._update_slm_controls(status_message=f"Connected: {serial}")
            self._set_error("-")
        except Exception as exc:
            self._set_error(str(exc))
            self._update_slm_controls()

    def _disconnect_slm(self) -> None:
        try:
            self.slm_adapter.disconnect()
            self._loaded_pattern_result = None
            self._update_slm_controls(status_message="Not connected")
            self._set_error("-")
        except Exception as exc:
            self._set_error(str(exc))

    def _load_patterns(self) -> None:
        try:
            if not self.slm_adapter.is_connected():
                raise HardwareError("Connect to an SLM device before clicking load.")
            pattern_files = self._current_pattern_files()
            device_path = self._selected_slm_device_path()
            self._loaded_pattern_result = self.slm_adapter.program_patterns(pattern_files, device_path=device_path)
            self._update_slm_controls(status_message="Connected: load complete")
            self._set_error("-")
        except Exception as exc:
            self._set_error(str(exc))

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

    def _run_camera_trigger_test(self, daq_config: DaqLineConfig) -> Path:
        camera_adapter = FusionBtCameraAdapter(sdk_path=self.config.backend.fusion_bt_sdk_path)
        output_path = self._test_capture_path("camera_pulse", "camera_trigger")
        _, _, line_index = parse_line_name(daq_config.camera_trigger_line)
        try:
            camera_adapter.apply_config(self.config.camera)
            camera_adapter.arm(frame_count=1)
            self.daq_adapter.pulse_line(daq_config.device_name, line_index, duration_s=0.1)
            stack, _timestamps = camera_adapter.read_frame_sequence(
                frame_count=1,
                pattern_files=[""],
                laser_wavelength_nm=self.config.selected_laser_nm,
            )
            self._write_uint16_tiff(output_path, stack[0])
            return output_path
        finally:
            try:
                camera_adapter.disarm()
            except Exception:
                pass
            try:
                camera_adapter.disconnect()
            except Exception:
                pass
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
        if self._loaded_pattern_result is None or not self._loaded_pattern_result.handles:
            raise HardwareError("SIM采集测试前需要先加载 9 个 pattern。")

        camera_adapter = FusionBtCameraAdapter(sdk_path=self.config.backend.fusion_bt_sdk_path)
        output_path = self._test_capture_path("sim_acquisition", "sim_acquisition_488nm_10ms")
        camera_config = clone_app_config(self.config).camera
        camera_config.exposure_us = 10_000
        waveform_builder = NIDaqWaveformBuilder()
        plan = waveform_builder.build(
            daq_config=daq_config,
            timing=self._current_timing_config(),
            laser_wavelength_nm=488,
            exposure_us=10_000,
            frame_count=9,
        )
        try:
            camera_adapter.apply_config(camera_config)
            camera_adapter.arm(frame_count=9)
            self.slm_adapter.activate_prepared_patterns()
            self.daq_adapter.play_waveform(daq_config.device_name, plan)
            stack, _timestamps = camera_adapter.read_frame_sequence(
                frame_count=9,
                pattern_files=list(self._loaded_pattern_result.pattern_files),
                laser_wavelength_nm=488,
            )
            self._write_uint16_tiff(output_path, stack)
            return output_path
        finally:
            try:
                camera_adapter.disarm()
            except Exception:
                pass
            try:
                camera_adapter.disconnect()
            except Exception:
                pass
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

    def _browse_pattern(self, index: int) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            f"Select Pattern {index + 1}",
            str(Path.cwd()),
            "Pattern Files (*.bmp *.png *.tif *.tiff *.jpg *.jpeg *.bin);;All Files (*.*)",
        )
        if path:
            self._set_pattern_path(index, path)
            self._loaded_pattern_result = None
            if self.slm_adapter.is_connected():
                self._update_slm_controls(status_message="Connected: reload required")

    def _set_error(self, message: str) -> None:
        clean_message = message.strip()
        if not clean_message or clean_message == "-":
            self.lbl_error.setText("-")
            self.lbl_error.hide()
            return
        self.lbl_error.setText(clean_message)
        self.lbl_error.show()

    def get_config(self) -> AppConfig:
        return clone_app_config(self._sync_config_from_widgets())

    def _set_pattern_path(self, index: int, path: str) -> None:
        edit = self.pattern_edits[index]
        clean_path = path.strip()
        edit.setProperty("full_path", clean_path)
        edit.setToolTip(clean_path)
        edit.setText(Path(clean_path).name if clean_path else "")

    def closeEvent(self, event) -> None:  # noqa: N802
        try:
            self.slm_adapter.disconnect()
        except Exception:
            pass
        super().closeEvent(event)


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
        for wavelength in (405, 488, 561, 640):
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

        self.lbl_current_status = QLabel("Idle")
        self.lbl_current_frame = QLabel("-")
        self.lbl_current_laser = QLabel("-")
        self.lbl_current_pattern = QLabel("-")
        self.lbl_current_error = QLabel("-")
        self.lbl_current_task = QLabel("-")
        self.lbl_current_error.setWordWrap(True)

        layout.addWidget(QLabel("Status"), 1, 0)
        layout.addWidget(self.lbl_current_status, 1, 1)
        layout.addWidget(QLabel("Current Frame"), 1, 2)
        layout.addWidget(self.lbl_current_frame, 1, 3)
        layout.addWidget(QLabel("Current Laser"), 2, 0)
        layout.addWidget(self.lbl_current_laser, 2, 1)
        layout.addWidget(QLabel("Current Pattern"), 2, 2)
        layout.addWidget(self.lbl_current_pattern, 2, 3)
        layout.addWidget(QLabel("Task ID"), 3, 0)
        layout.addWidget(self.lbl_current_task, 3, 1, 1, 3)
        layout.addWidget(QLabel("Last Error"), 4, 0)
        layout.addWidget(self.lbl_current_error, 4, 1, 1, 4)
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
        self.spin_sample_rate.setValue(config.timing.sample_rate_hz)
        self.spin_edge_pulse_us.setValue(config.timing.edge_pulse_us)
        self.spin_inter_frame_gap_us.setValue(config.timing.inter_frame_gap_us)
        self.spin_slm_enable_guard_us.setValue(config.timing.slm_enable_guard_us)
        for index, edit in enumerate(self.pattern_edits):
            edit.setText(config.pattern_files[index])
        laser_button = self.laser_buttons.get(config.selected_laser_nm, self.laser_buttons[488])
        laser_button.setChecked(True)

    def _current_daq_config(self) -> DaqLineConfig:
        selections = {}
        for role, combo in self.line_combos.items():
            value = combo.currentText().strip()
            if not value:
                raise ValueError(f"DAQ line not selected for {ROLE_LABELS[role]}")
            selections[role] = value
        device_name, _, _ = parse_line_name(next(iter(selections.values())))
        return DaqLineConfig(device_name=device_name, **selections)

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
        return TimingConfig(
            sample_rate_hz=self.spin_sample_rate.value(),
            edge_pulse_us=self.spin_edge_pulse_us.value(),
            inter_frame_gap_us=self.spin_inter_frame_gap_us.value(),
            slm_enable_guard_us=self.spin_slm_enable_guard_us.value(),
        )

    def _current_pattern_files(self) -> list[str]:
        return [edit.text().strip() for edit in self.pattern_edits]

    def _selected_laser_nm(self) -> int:
        checked = self.laser_group.checkedId()
        if checked <= 0:
            raise ValueError("Please select one laser wavelength.")
        return checked

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

        for role, combo in self.line_combos.items():
            target_value = current_values.get(role) or getattr(self.config.daq, role)
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(lines)
            if target_value in lines:
                combo.setCurrentText(target_value)
            combo.blockSignals(False)
        self._log(f"Loaded {len(lines)} DAQ line options.")

    def _validate_wiring(self) -> None:
        try:
            validate_daq_line_config(self._current_daq_config())
            self._log("DAQ wiring validation passed.")
            QMessageBox.information(self, "DAQ Wiring", "DAQ wiring validation passed.")
        except Exception as exc:
            self._set_error(str(exc))
            QMessageBox.critical(self, "DAQ Wiring", str(exc))

    def _load_config_from_disk(self) -> None:
        try:
            self.config = load_app_config(self.config.config_path or DEFAULT_CONFIG_PATH)
            self._populate_widgets_from_config(self.config)
            self._refresh_device_lines()
            self._log(f"Config loaded from {self.config.config_path}")
        except Exception as exc:
            self._set_error(str(exc))

    def _save_config_to_disk(self) -> None:
        try:
            self._sync_config_from_widgets()
            path = save_app_config(self.config, self.config.config_path or DEFAULT_CONFIG_PATH)
            self._log(f"Config saved to {path}")
        except Exception as exc:
            self._set_error(str(exc))

    def _initialize_hardware(self) -> None:
        try:
            self._sync_config_from_widgets()
            self.controller.apply_daq_config(self.config.daq)
            self.controller.initialize_hardware()
        except Exception as exc:
            self._set_error(str(exc))

    def _initialize_camera(self) -> None:
        try:
            self.controller.initialize_camera()
        except Exception as exc:
            self._set_error(str(exc))

    def _apply_camera_settings(self) -> None:
        try:
            self._sync_config_from_widgets()
            self.controller.apply_camera_config(self.config.camera)
        except Exception as exc:
            self._set_error(str(exc))

    def _arm_camera(self) -> None:
        try:
            self.controller.arm_camera(frame_count=9)
        except Exception as exc:
            self._set_error(str(exc))

    def _disarm_camera(self) -> None:
        try:
            self.controller.disarm_camera()
        except Exception as exc:
            self._set_error(str(exc))

    def _program_patterns(self) -> None:
        try:
            self._sync_config_from_widgets()
            self.controller.prepare_patterns(self.config.pattern_files)
        except Exception as exc:
            self._set_error(str(exc))

    def _prepare_experiment(self) -> None:
        try:
            self._sync_config_from_widgets()
            self.controller.apply_daq_config(self.config.daq)
            self.controller.initialize_hardware()
            self.controller.apply_camera_config(self.config.camera)
            self.controller.prepare_patterns(self.config.pattern_files)
            self._log("Experiment prepared.")
        except Exception as exc:
            self._set_error(str(exc))

    def _run_single_acquisition(self) -> None:
        try:
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
        except Exception as exc:
            self._set_error(str(exc))

    def _stop_acquisition(self) -> None:
        try:
            self.controller.stop()
            self._log("Stop requested.")
        except Exception as exc:
            self._set_error(str(exc))

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
        if state == "frame_captured":
            frame_index = payload.get("frame_index", "-")
            self.lbl_current_frame.setText(str(frame_index))
            self.lbl_current_pattern.setText(str(frame_index))
        if state == "acquisition_complete":
            self.pipeline_labels["stack_shape"].setText(str(payload.get("stack_shape", "-")))
        self._log(f"[{state}] {payload}")

    def _handle_acquisition_ready(self, batch) -> None:
        self.pipeline_labels["task_id"].setText(batch.task_id)
        self.pipeline_labels["stack_shape"].setText(str(list(batch.stack.shape)))
        self.pipeline_labels["stack_dtype"].setText(str(batch.stack.dtype))
        self.pipeline_labels["reconstruction"].setText("Running")

    def _handle_acquisition_failed(self, task_id: str, message: str) -> None:
        self.pipeline_labels["reconstruction"].setText("Acquisition failed")
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
        path, _ = QFileDialog.getOpenFileName(
            self,
            f"Select Pattern {index + 1}",
            str(Path.cwd()),
            "Pattern Files (*.bmp *.png *.tif *.tiff *.jpg *.jpeg *.bin);;All Files (*.*)",
        )
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


"""历史微流控主界面与当前 SIM 控制链路的集成入口。

这个文件承载旧版 CellSorting GUI 的大部分业务槽函数，同时把 sim_control 的配置、预览、SLM Running Order 和正式 SIM9 采集接入到同一个主窗口。它通过 PyQt5 信号槽连接相机线程、MCU 触发线程、ROI 显示和 SIM controller；本次只补结构注释，不改历史控制逻辑。
"""

import copy
import html
import sys
from PyQt5.QtCore import QEvent, QMetaObject, QObject, QThread, Qt, pyqtSlot,pyqtSignal,QTimer
from PyQt5.QtGui import QFont, QFontMetrics, QImage, QPixmap
import PyQt5.QtWidgets as qw
import CellSorting_ui
from PyQt5.QtSerialPort import QSerialPortInfo
import MCUTriggerThread
from PyQt5.QtWidgets import QApplication
import mvsdk
import FastCameraThread
import cv2
import math
import json
import logging
from datetime import datetime
import os
import time
from collections import deque
import pandas as pd
from pathlib import Path
import tkinter as tk
from tkinter import messagebox
from threading import Event, Lock
import numpy as np
from PyQt5.QtWidgets import QScrollArea
from roi_geometry import crop_rotated_roi, draw_rotated_roi
#import torch 

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

from sim_control.config_store import (
    DEFAULT_RECONSTRUCTION_OUTPUT_DIR,
    app_config_from_dict,
    app_config_to_dict,
    load_app_config,
    save_app_config,
)
from sim_control.controller import SimAcquisitionController, immediate_live_wavelength_matches
from sim_control.daq_testing import DaqTestRunner, _PulseTestWorker, build_daq_test_target_items
from sim_control.gui import (
    create_camera_adapter_for_backend,
    create_daq_adapter_for_backend,
    populate_daq_line_combos,
    read_daq_config_from_line_combos,
)
from sim_control.models import SUPPORTED_LASERS, SimTaskConfig, Z_SCAN_EXPOSURE_PRESETS_MS
from sim_control.pipeline import RawStackSaveWorker, ReconstructionWorker
from sim_control.preview import SimPreviewController
from sim_control.preview_contrast import AutoContrastState, fast_preview_uint16_to_uint8
from sim_control.summary import build_sim_settings_summary
from sim_control.z_scan_core import ZScanCancelled, run_z_scan_autofocus, run_z_scan_stage_only
from sim_control.z_scan_timing_history import DEFAULT_Z_SCAN_TIMING_HISTORY_PATH, load_z_scan_timing_records
from sim_control.sim_camera_presets import (
    DEFAULT_SIM_CAMERA_SIZE,
    SIM_CAMERA_SIZE_PRESETS,
    SIM_CAMERA_ROI_STEP_PX,
    build_sim_camera_size_presets,
    centered_sim_camera_roi_origin,
    fit_image_size_to_bounds,
    is_full_frame_sim_camera_size,
    labels_for_sim_camera_size_presets,
    normalize_sim_camera_roi,
    sim_camera_roi_origin_bounds,
    size_from_sim_camera_label,
)

logger = logging.getLogger(__name__)

# SIM 集成区使用毫秒级 UI 控件，进入 sim_control 前统一转换为微秒级配置。
SIM_EXPOSURE_MIN_MS = 1
SIM_EXPOSURE_MAX_MS = 10_000
SIM_EXPOSURE_DEFAULT_MS = 10
SIM_PREVIEW_EXPOSURE_US = 30_000
SIM_BIT_DEPTH_DEFAULT = 16
USER_FACING_BIT_DEPTHS = (8, 12, 16)
SIM_RUNTIME_LED_SIZE_PX = 16
SIM_RUNTIME_LED_RADIUS_PX = SIM_RUNTIME_LED_SIZE_PX // 2
SIM_RUNTIME_LED_GRAY_STYLE = (
    f"background-color: #8b949e; border-radius: {SIM_RUNTIME_LED_RADIUS_PX}px;"
)


# 旧 CellSorting UI 是固定尺寸生成界面，这里外包一层滚动区以适配较小显示器。
def setup_scrollable_cellsorting_ui(window, ui):
    """把固定尺寸的历史 UI 包进滚动区域，保留原布局同时支持小屏幕查看。"""
    content_widget = qw.QWidget()
    ui.setupUi(content_widget)
    content_size = content_widget.size()
    content_widget.setMinimumSize(content_size)
    content_widget.resize(content_size)

    scroll_area = QScrollArea(window)
    scroll_area.setObjectName("cellsortingScrollArea")
    scroll_area.setFrameShape(qw.QFrame.NoFrame)
    scroll_area.setWidgetResizable(False)
    scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    scroll_area.setWidget(content_widget)

    layout = qw.QVBoxLayout(window)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    layout.addWidget(scroll_area)

    window.setObjectName(content_widget.objectName())
    window.setWindowTitle(content_widget.windowTitle())
    window.setCursor(content_widget.cursor())
    window.setMouseTracking(content_widget.hasMouseTracking())
    window.resize(content_size)
    QMetaObject.connectSlotsByName(window)

    window.cellsorting_content_widget = content_widget
    window.cellsorting_scroll_area = scroll_area
    return scroll_area, content_widget


def clone_sim_app_config(config):
    """通过配置序列化往返生成深拷贝，避免直接修改调用方持有的 AppConfig。"""
    return app_config_from_dict(app_config_to_dict(config))


def merge_legacy_sim_control_payload(base_config, legacy_payload):
    """把旧配置文件中的 SIM 片段合并到当前默认配置，同时保留当前配置路径。"""
    merged = clone_sim_app_config(base_config)
    if not legacy_payload:
        return merged

    legacy_config = app_config_from_dict(legacy_payload)
    merged.daq = legacy_config.daq
    merged.camera = legacy_config.camera
    merged.timing = legacy_config.timing
    merged.z_scan = legacy_config.z_scan
    merged.reconstruction = legacy_config.reconstruction
    merged.pattern_files = list(legacy_config.pattern_files)
    merged.selected_running_order = legacy_config.selected_running_order
    merged.selected_laser_nm = legacy_config.selected_laser_nm
    merged.config_path = base_config.config_path
    return merged


def create_sim_runtime_led(parent, object_name=""):
    led = qw.QLabel(parent)
    if object_name:
        led.setObjectName(object_name)
    led.setFixedSize(SIM_RUNTIME_LED_SIZE_PX, SIM_RUNTIME_LED_SIZE_PX)
    led.setText("")
    led.setStyleSheet(SIM_RUNTIME_LED_GRAY_STYLE)
    return led


def apply_real_hardware_preference(config, camera_devices, slm_devices, daq_devices):
    """根据探测到的真实设备更新配置，让主界面优先选择已连接的相机、SLM 和 DAQ。"""
    updated = clone_sim_app_config(config)
    if camera_devices:
        current_label = (updated.camera.device_label or "").strip().lower()
        matching_camera = next(
            (device for device in camera_devices if int(device.get("index", -1)) == int(updated.camera.device_index)),
            None,
        )
        if matching_camera is None:
            matching_camera = camera_devices[0]
        elif current_label != str(matching_camera.get("display", "")).strip().lower():
            matching_camera = camera_devices[0]
        updated.camera.device_index = int(matching_camera.get("index", 0))
        updated.camera.device_label = str(matching_camera.get("display", ""))
    return updated


def sim_exposure_us_to_ms(exposure_us):
    """把 SIM 配置中的微秒曝光换算成旧主界面 spinbox 使用的毫秒整数。"""
    value_ms = int(round(float(exposure_us) / 1000.0))
    return max(SIM_EXPOSURE_MIN_MS, min(SIM_EXPOSURE_MAX_MS, value_ms))


def sim_exposure_ms_to_us(exposure_ms):
    """把主界面毫秒输入转换回 SIM 配置持久化使用的微秒值。"""
    value_ms = int(round(float(exposure_ms)))
    value_ms = max(SIM_EXPOSURE_MIN_MS, min(SIM_EXPOSURE_MAX_MS, value_ms))
    return value_ms * 1000


def normalize_legacy_sim_exposure_setting(exposure_value):
    """兼容旧配置里可能混用毫秒和微秒的曝光字段，并统一输出毫秒值。"""
    value = int(round(float(exposure_value)))
    if value > SIM_EXPOSURE_MAX_MS:
        return sim_exposure_us_to_ms(value)
    return max(SIM_EXPOSURE_MIN_MS, min(SIM_EXPOSURE_MAX_MS, value))


def sim_bit_depth_to_label(bit_depth):
    """把相机位深整数格式化成主界面下拉框的显示文本。"""
    value = int(round(float(bit_depth)))
    return f"{value}-bit"


def sim_bit_depth_from_label(label, default=SIM_BIT_DEPTH_DEFAULT):
    """从下拉框文本中解析相机位深；无数字时回退到默认位深。"""
    digits = "".join(ch for ch in str(label) if ch.isdigit())
    if not digits:
        return int(default)
    return int(digits)


# 曝光时间 spinbox 使用非线性步进，方便在 1/10/50ms 等常用桶之间快速切换。
class SnappingExposureSpinBox(qw.QSpinBox):
    """SIM exposure control with bucketed stepping in milliseconds."""

    def stepBy(self, steps):
        if steps == 0:
            return
        current = int(self.value())
        if steps > 0:
            target = self._step_up(current, steps)
        else:
            target = self._step_down(current, -steps)
        target = max(self.minimum(), min(self.maximum(), target))
        self.setValue(target)

    def _next_up(self, value):
        if value < 10:
            return value + 1
        if value < 50:
            return value + 10 if value % 10 == 0 else ((value // 10) + 1) * 10
        return value + 50 if value % 50 == 0 else ((value // 50) + 1) * 50

    def _next_down(self, value):
        if value <= 10:
            return value - 1
        if value <= 50:
            return value - 10 if value % 10 == 0 else (value // 10) * 10
        return value - 50 if value % 50 == 0 else (value // 50) * 50

    def _step_up(self, current, count):
        value = current
        for _ in range(count):
            value = self._next_up(value)
        return value

    def _step_down(self, current, count):
        value = current
        for _ in range(count):
            value = self._next_down(value)
        return value


class _ConnectWorker(QObject):
    """One-shot hardware-connect worker; runs ``fn()`` in a QThread and reports back.

    Signals are emitted in order: ``signal_success`` or ``signal_error``, then ``signal_finished``.
    The caller is responsible for calling ``thread.quit()`` and ``thread.wait()`` in the
    ``signal_finished`` slot.
    """

    signal_success = pyqtSignal(object)
    signal_error = pyqtSignal(str)
    signal_finished = pyqtSignal()

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    @pyqtSlot()
    def run(self) -> None:
        try:
            result = self._fn()
            self.signal_success.emit(result)
        except Exception as exc:
            self.signal_error.emit(str(exc))
        finally:
            self.signal_finished.emit()


# MainWindow 同时承载历史微流控界面和新增 SIM 控制状态，是两个子系统的集成边界。
class MainWindow(qw.QWidget):
    
    # 创建发送mindvision相机设置参数的信号
    """历史微流控主窗口，同时集成 SIM 配置、预览、硬件状态和正式采集入口。"""
    signal_sendImageProcessingPara = pyqtSignal(dict)  #以字典的形式发送图像处理相关参数
    # 创建发送sCMOS相机参数的信号
    signal_sendImageProcessingPara_sCMOS = pyqtSignal(dict)  #以字典的形式发送sCMOS图像处理相关参数
    #发送单片机线程的参数
    signal_updataMCURecevieParameter      = pyqtSignal(dict)  #以字典的形式发送单片机信号相关参数
    # 创建发送图像保存微流控参数的信号
    signal_sendExperimentImfo      = pyqtSignal(dict)
    # 手动控制rinse微流控管道信号
    signal_btn_rinseChannelCapture = pyqtSignal()      
    signal_btn_rinseChannelSort    = pyqtSignal() 
    signal_btn_rinseChannelRelease = pyqtSignal() 
    signal_btn_rinseChannel_OFF    = pyqtSignal()
    # 发送背景图片 背景图片变量的创建应该在打开相机那里
    signal_sendBackgroundFrame = pyqtSignal(object)
    # 手动控制Trigger的信号
    signal_btn_triggerCapture        = pyqtSignal()
    signal_btn_triggerReleaseSort    = pyqtSignal()
    signal_btn_triggerRelease        = pyqtSignal() 
    signal_updataFastCamera_maxGray  = pyqtSignal(int) 
    signal_setImageProcessingWay_UIThread = pyqtSignal(int) #改变ROI的识别模式
    #手动捕获细胞是否为目标细胞
    signal_isTarget = pyqtSignal(int,int)  #第一位只能是7；第二位： -1:非目标细胞; 0:非目标细胞,miss了; 1:目标细胞
    # Z-Scan 后台 worker 的进度事件经此信号转回 GUI 主线程（禁止 worker 线程直接动控件）。
    signal_zscan_status = pyqtSignal(str, dict)
    # B6：GUI 线程跨线程向常驻 recon worker 下发配置快照（queued slot），不裸 setter。
    signal_reconstruction_config_changed = pyqtSignal(object)


    def __init__(self):
        super().__init__()
        # 初始化UI
        self.ui = CellSorting_ui.Ui_Single_Cell_Sorting()
        setup_scrollable_cellsorting_ui(self, self.ui)
        self.ui.lb_sCMOS_cameraView.installEventFilter(self)
        self.sim_app_config = load_app_config()
        self.sim_camera_adapter = None
        self.sim_acquisition_controller = None
        self.sim_preview_controller = None
        self.sim_runtime_backend_signature = None
        self.sim_preview_backend_signature = None
        self.sim_runtime_timing_snapshot = {}
        self.sim_camera_connected = False
        self.sim_available_cameras = []
        self.sim_slm_connected = False
        self.sim_available_slms = []
        self.sim_preview_active = False
        self.sim_preview_requested = False
        self.sim_preview_restart_requested = False
        self.sim_preview_stop_in_progress = False
        self.sim_acquisition_in_progress = False
        self.sim_resume_preview_after_acquisition = False
        # --- immediate-live（找样品）GUI 运行态（不落盘）---
        # confirmed：是否已收到 preview_started（真正出帧），是激光点亮的唯一前置凭据；
        # sim_preview_active 早于它置位，不能当凭据。
        self.sim_preview_started_confirmed = False
        # 连接时后台 worker 扫描缓存的 immediate RO 列表（已带 activation/parsed wavelength）。
        self.sim_immediate_ro_items = []
        self.sim_immediate_live_active = False
        self.sim_immediate_active_ro_index = None
        self.sim_immediate_active_wavelength = None
        # 两阶段 pending：preview 未确认时先等 preview_started 再点灯。
        self._immediate_pending = None  # dict | None: {token, mode, ro_index, wavelength}
        self._immediate_pending_token = 0
        # planned restart：immediate-live 期间因 ROI/曝光内部重启 preview，需保住激光不关。
        self.sim_preview_planned_restart = False
        # GUI 自维护的 start/restart 序号（对外 preview_started 无 generation）。
        self.sim_preview_start_seq = 0
        self.sim_last_reconstruction_result = None
        self.sim_recon_thread = None
        self.sim_recon_worker = None
        self.sim_raw_stack_save_thread = None
        self.sim_raw_stack_save_worker = None
        self.sim_current_task_id = ""
        self.sim_current_acquisition_raw_only = False
        self.sim_raw_stack_save_finished_task_ids = set()
        self.sim_last_preview_frame = None
        self.sim_last_preview_sequence = -1
        self.sim_auto_contrast_state = AutoContrastState()
        self.sim_zscan_timing_records_cache = None
        self.sim_zscan_timing_history_signature = None
        self.sim_camera_sensor_size = DEFAULT_SIM_CAMERA_SIZE
        self.sim_camera_size_presets = SIM_CAMERA_SIZE_PRESETS
        self.sim_camera_roi_step_px = SIM_CAMERA_ROI_STEP_PX
        self.ui.chb_sCMOS_autoContrast.toggled.connect(lambda _checked: self.sim_auto_contrast_state.reset())
        self.sim_preview_restart_timer = QTimer(self)
        self.sim_preview_restart_timer.setSingleShot(True)
        self.sim_preview_restart_timer.timeout.connect(self.restart_sim_preview_with_current_settings)
        self.sim_preview_poll_timer = QTimer(self)
        self.sim_preview_poll_timer.timeout.connect(self.poll_latest_sim_preview_frame)
        # 两阶段 / planned-restart 等 preview_started 的超时看门狗用 QTimer.singleShot + 捕获
        # token 实现（见 _begin_immediate_pending），陈旧超时按 token 失配被忽略、无竞态。
        self.sim_stage_position_timer = QTimer(self)
        self.sim_stage_position_timer.setInterval(500)
        self.sim_stage_position_timer.timeout.connect(self.poll_sim_stage_position)
        self.UI_Init()
        self.setup_sim_runtime_status_widgets()
        self.setup_sim_z_position_widgets()
        self.setup_sim_zscan_module()
        self.setup_sim_daq_module()
        self.setup_sim_recon_module()
        # 初始化统计细胞ID和个数
        self.cell_ID = 0                   # 用来记录是哪次细胞的
        self.totalNumb_capture = 0
        self.totalNumb_trapped = 0
        self.trappedCell_miss  = 0
        self.totalNumb_relese = 0
        self.totalNumb_functionMeasurement_start = 0
        self.totalNumb_functionMeasurement_end = 0
        self.functionMeasurement_start_miss = 0
        self.functionMeasurement_end_miss = 0
        self.totalNumb_collected = 0       
        self.collectedCell_miss  = 0
        self.flowRate_ID          = 0      # 用来记录是哪次细胞的
        # 初始化串口和相机的变量
        self.portName = []
        self.cameraList = []
        #初始化相机和单片机线程
        self.FastCameraThread = None
        self.MCUTriggerThread = None
        self.sCMOSCameraThread = None
        #sCMOS 连接按钮初始化
        "sCMOS图像处理的相关参数预设"
        self.sCMOS_gaussianKernel = 7
        self.sCMOS_gaussBlurSigma = 0.8
        self.sCMOS_morphologyKernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15,15)) #椭圆形核
        self.sCMOS_openTimes = 1
        self.sCMOS_closeTimes = 1
        self.sCMOS_minArea = 3000
        self.sCMOS_Bi_threshold = 150
        self.function_min_delta_lenth = 1
        self.function_max_delta_lenth = 1000
        self.functionROI_X = 0
        self.functionROI_Y = 0
        self.functionROI_width = 70
        self.functionROI_height = 70
        #设置基于图像分析的Trigger模块的状态
        self.btn_enterImageProcessingModel_state   = False
        self.btn_runScreenCell_continue_state      = False
        self.btn_runScreenCell_single_state        = False
        self.btn_saveROIImage_state                = False
        self.clouseVideoSivingModel     = False
        # 提取背景图片按钮状态
        self.btn_backgroundExtract_state = False
        self.index_number = 0  #初始化显示图像的索引，根据索引每次显示对应的ROI的背景差值图

        self.index_sCMOS_number = 0 #用于间隔保存显示sCMOS的二值化图像
        self.sCMOS_ROI_Bg = None #用于储存sCMOS的ROI的背景图像，由capture trigger信号触发，当信号发生时，将当前帧的前第5帧的ROI图像（深拷贝）作为背景图，每次促发capture信号都更新

        #flowRate的图像分析
        self.btn_flowRateImageProcessing_state       = False
        # 立即执行一次扫描
        self.btn_refresh_camera_function()
        
        #统计ROI图像处理的参数
        #初始化统一数据的队列
        self.roi_data_queue = deque(maxlen = 50000) #限制最大的队列数量3W个
        #初始化文件保存目录
        "这个应该放在保存数据的数组中"
        #保存的文件夹名字
        self.output_dir = Path(os.path.dirname(__file__)) / (str(time.strftime("%Y%m%d")) + "ROI_recordData")
        self.output_dir.mkdir(exist_ok=True)
        self.btn_exportROIData_state = False
        self.lock = Lock()  # 创建一个线程锁
        "自动加载参数"
        # 程序启动时自动加载 default.json
        # 恢复标志位
        self.is_auto_loading = False
        self._loading_configure_settings = False
        default_path = Path(__file__).parent / "lastConfiguration.json"
        if default_path.exists():
            self.load_configure_settings(default_path, apply_legacy_sim_camera_settings=False)
        else:
            print("未找到默认配置文件 lastConfiguration.json")
        self.sync_sim_camera_controls_from_config()
        self.refresh_sim_settings_summary()
        self.refresh_sim_camera_devices()
        self.refresh_sim_slm_devices()
        
    # 关闭界面
    def closeEvent(self, event):
        """
        重写关闭事件，清理资源
        """
        # 关找样品激光要 best-effort 且尽量靠前：绝不能依赖后续保存配置成功，否则保存异常
        # 会让激光线停在高电平。stop_immediate_live_mode 在未激活时安全 no-op。
        try:
            # 关窗口时不复位下拉（控件可能正被 Qt 销毁），只关激光 + 清运行态。
            self.stop_immediate_live_mode(reset_dropdown=False)
        except Exception as e:
            print(f"stop_immediate_live_mode on close failed: {str(e)}")
        # 自动保存当前配置
        self.persist_sim_app_config_from_ui()
        self.save_current_settings_to_default()  # 新增
        print("关闭窗口，清理资源...")
        time.sleep(0.1)
        # 关闭 COM Port 串口线程
        if self.MCUTriggerThread:  # 检查线程是否存在
            self.MCUTriggerThread.stop()
            self.MCUTriggerThread.wait()  # 确保线程完全停止
    
        # 关闭相机线程
        if self.FastCameraThread:  # 检查线程是否存在
            self.FastCameraThread.stop()
            self.FastCameraThread.wait()  # 确保线程完全停止
    
        # 接受关闭事件
        if hasattr(self, "sim_stage_position_timer"):
            self.sim_stage_position_timer.stop()
        if self.sim_preview_controller:
            self.sim_preview_controller.shutdown()
        if self.sim_acquisition_controller:
            self.sim_acquisition_controller.shutdown()
        self.shutdown_sim_raw_stack_save_worker()
        self.shutdown_sim_reconstruction_worker()
        event.accept()

    def ensure_sim_reconstruction_worker(self):
        if self.sim_recon_worker is not None:
            # B6：worker 已在常驻 recon 线程，跨线程更新配置必须走 queued slot（不裸 setter）。
            self.signal_reconstruction_config_changed.emit(self.sim_app_config.reconstruction.snapshot())
            return
        self.sim_recon_thread = QThread(self)
        self.sim_recon_worker = ReconstructionWorker(self.sim_app_config.reconstruction)
        self.sim_recon_worker.moveToThread(self.sim_recon_thread)
        self.sim_recon_worker.signal_reconstruction_ready.connect(self.slot_handle_sim_reconstruction_ready)
        self.sim_recon_worker.signal_reconstruction_failed.connect(self.slot_handle_sim_reconstruction_failed)
        # B6：后续配置更新经 queued 信号→worker slot（worker 在独立线程，跨线程安全）。
        self.signal_reconstruction_config_changed.connect(self.sim_recon_worker.slot_update_reconstruction_config)
        self.sim_recon_thread.start()

    def connect_sim_reconstruction_worker_to_controller(self):
        if self.sim_acquisition_controller is None:
            return
        self.ensure_sim_reconstruction_worker()
        try:
            self.sim_acquisition_controller.signal_acquisition_ready.disconnect(
                self.sim_recon_worker.slot_reconstruct
            )
        except (TypeError, RuntimeError):
            pass
        self.sim_acquisition_controller.signal_acquisition_ready.connect(
            self.sim_recon_worker.slot_reconstruct
        )

    def disconnect_sim_reconstruction_worker_from_controller(self):
        if self.sim_acquisition_controller is None or self.sim_recon_worker is None:
            return
        try:
            self.sim_acquisition_controller.signal_acquisition_ready.disconnect(
                self.sim_recon_worker.slot_reconstruct
            )
        except (TypeError, RuntimeError):
            pass

    def ensure_sim_raw_stack_save_worker(self):
        if self.sim_raw_stack_save_worker is not None:
            return
        self.sim_raw_stack_save_thread = QThread(self)
        self.sim_raw_stack_save_worker = RawStackSaveWorker(PROJECT_ROOT / "data" / "sim_9frames")
        self.sim_raw_stack_save_worker.moveToThread(self.sim_raw_stack_save_thread)
        self.sim_raw_stack_save_worker.signal_stack_saved.connect(self.slot_handle_sim_raw_stack_saved)
        self.sim_raw_stack_save_worker.signal_stack_save_failed.connect(self.slot_handle_sim_raw_stack_save_failed)
        self.sim_raw_stack_save_thread.start()

    def connect_sim_raw_stack_save_worker_to_controller(self):
        if self.sim_acquisition_controller is None:
            return
        self.ensure_sim_raw_stack_save_worker()
        try:
            self.sim_acquisition_controller.signal_acquisition_ready.disconnect(
                self.sim_raw_stack_save_worker.slot_save
            )
        except (TypeError, RuntimeError):
            pass
        self.sim_acquisition_controller.signal_acquisition_ready.connect(
            self.sim_raw_stack_save_worker.slot_save
        )

    def disconnect_sim_raw_stack_save_worker_from_controller(self):
        if self.sim_acquisition_controller is None or self.sim_raw_stack_save_worker is None:
            return
        try:
            self.sim_acquisition_controller.signal_acquisition_ready.disconnect(
                self.sim_raw_stack_save_worker.slot_save
            )
        except (TypeError, RuntimeError):
            pass

    def shutdown_sim_raw_stack_save_worker(self):
        self.disconnect_sim_raw_stack_save_worker_from_controller()
        worker = getattr(self, "sim_raw_stack_save_worker", None)
        thread = getattr(self, "sim_raw_stack_save_thread", None)
        if worker is not None:
            try:
                worker.signal_stack_saved.disconnect(self.slot_handle_sim_raw_stack_saved)
                worker.signal_stack_save_failed.disconnect(self.slot_handle_sim_raw_stack_save_failed)
            except (TypeError, RuntimeError):
                pass
            self.sim_raw_stack_save_worker = None
        if thread is not None:
            thread.quit()
            thread.wait(2000)
            self.sim_raw_stack_save_thread = None

    def _restore_sim_post_acquisition_routing_after_raw_only(self):
        try:
            raw_only = bool(getattr(self, "sim_current_acquisition_raw_only", False))
        except RuntimeError:
            return
        if not raw_only:
            return
        self.disconnect_sim_raw_stack_save_worker_from_controller()
        self.connect_sim_reconstruction_worker_to_controller()
        self.sim_current_acquisition_raw_only = False

    def shutdown_sim_reconstruction_worker(self):
        worker = getattr(self, "sim_recon_worker", None)
        thread = getattr(self, "sim_recon_thread", None)
        if worker is not None:
            try:
                worker.signal_reconstruction_ready.disconnect(self.slot_handle_sim_reconstruction_ready)
                worker.signal_reconstruction_failed.disconnect(self.slot_handle_sim_reconstruction_failed)
            except (TypeError, RuntimeError):
                pass
            self.sim_recon_worker = None
        if thread is not None:
            thread.quit()
            thread.wait(2000)
            self.sim_recon_thread = None

    def _promote_sim_exposure_spinbox(self):
        old = self.ui.spb_sCMOS_exposureTime
        if isinstance(old, SnappingExposureSpinBox):
            return
        parent = old.parentWidget()
        layout = parent.layout() if parent is not None else None
        new = SnappingExposureSpinBox(parent)
        new.setObjectName(old.objectName())
        new.setFont(old.font())
        new.setMinimum(SIM_EXPOSURE_MIN_MS)
        new.setMaximum(SIM_EXPOSURE_MAX_MS)
        new.setSingleStep(1)
        new.setKeyboardTracking(False)
        new.setMinimumSize(old.minimumSize())
        new.setMaximumSize(old.maximumSize())
        new.setSizePolicy(old.sizePolicy())
        new.setEnabled(old.isEnabled())
        new.setToolTip(old.toolTip())
        new.setStatusTip(old.statusTip())
        new.setWhatsThis(old.whatsThis())
        new.setValue(old.value())
        if layout is not None:
            layout.replaceWidget(old, new)
        old.deleteLater()
        self.ui.spb_sCMOS_exposureTime = new
         
    def UI_Init(self):
        self._promote_sim_exposure_spinbox()
        """Config. 配置保存膜块"""
        #配置保存按钮链接
        self.ui.btn_saveConfigureSettings.clicked.connect(self.btn_saveConfigureSettings_function)
        self.ui.btn_loadConfigureSettings.clicked.connect(self.btn_loadConfigureSettings_function)
        self.ui.btn_saveConfigureSettingsMain.clicked.connect(self.btn_saveConfigureSettings_function)
        self.ui.btn_loadConfigureSettingsMain.clicked.connect(self.btn_loadConfigureSettings_function)
        self.ui.grp_configuration.setVisible(False)
        self.ui.pte_simSummary.setReadOnly(True)

        """Hardvare Connection 模块"""
        self.ui.btn_portConnect.clicked.connect(self.slot_toggle_MCU_Camera_connection) # Connect按钮连接单片机和相机的连接函数
        self.ui.btn_refresh.clicked.connect(self.btn_refresh_camera_function)           # 刷新单片机和相机接口的按钮连接    
        self.ui.spb_pixelWidth.valueChanged.connect(self.spb_ROI_value_changed_function)#相机ROI设置宽必须为8的倍数，高必须为2的倍数
        self.ui.spb_pixelHeight.valueChanged.connect(self.spb_ROI_value_changed_function)

        """sCMOS 相机模块"""
        
        self.ui.btn_sCMOS_connection.clicked.connect(self.btn_sCMOS_connection_function) # Connect按钮连接单片机和相机的连接函数
        self.ui.btn_sCMOS_refresh.clicked.connect(self.btn_sCMOS_refresh_function)
        self.ui.btn_sCMOS_live.clicked.connect(self.btn_sCMOS_live_function)
        if hasattr(self.ui, "btn_SLM_refresh"):
            self.ui.btn_SLM_refresh.clicked.connect(self.refresh_sim_slm_devices)
        if hasattr(self.ui, "btn_SLM_connection"):
            self.ui.btn_SLM_connection.clicked.connect(self.toggle_sim_slm_connection)
        if hasattr(self.ui, "lbl_SLM_status"):
            self.ui.lbl_SLM_status.setVisible(False)

        self.configure_sim_camera_spinboxes()
        self.ui.spb_sCMOS_ROI_X.valueChanged.connect(self.on_sim_camera_setting_changed)
        self.ui.spb_sCMOS_ROI_Y.valueChanged.connect(self.on_sim_camera_setting_changed)
        self.ui.spb_sCMOS_exposureTime.valueChanged.connect(self.on_sim_camera_setting_changed)
        self.ui.cmb_sCMOS_bitDepth.currentTextChanged.connect(self.on_sim_camera_setting_changed)
        self.ui.cmb_sCMOS_imageSize.activated.connect(self.on_sim_camera_size_activated)
        self.ui.grp_videoSave_2.setVisible(False)
        self.ui.btn_sCMOS_connection.setText("Connect SIM Camera")
        self.ui.btn_sCMOS_live.setText("Live")
        # 新增：SIM 波长下拉（SIM Camera Settings 组内）、immediate-RO 找样品下拉、SIM9 采集按钮。
        self._build_sim_immediate_and_acquire_controls()
        self.set_sim_camera_controls_enabled(False)
        self.update_sim_camera_action_buttons()
        self.update_sim_slm_controls()

        """fastCamare保存视频模块"""
        self.btn_missEventVideoSaveModel_state = False
        self.ui.btn_snap.setEnabled(False)
        self.ui.btn_triggerSaveVideo.setEnabled(False)
        self.ui.btn_enterSaveModel.setEnabled(False)
        self.ui.btn_missEventVideoSaveModel.setEnabled(False)
        self.btn_enterVideoSaveModel_state = False  # 图像保存模式状态
        self.ui.btn_enterSaveModel.clicked.connect(self.btn_enterSaveModel_function)
        self.ui.btn_missEventVideoSaveModel.clicked.connect(self.btn_missEventVideoSaveModel_function)
        self.ui.btn_triggerSaveVideo.clicked.connect(self.btn_triggerSaveVideo_function)# 开始视频保存
        self.ui.btn_snap.clicked.connect(self.btn_snap_function)# 捕获单张图像
        self.ui.btn_saveExperimentInfo.clicked.connect(self.btn_saveExperimentInfo_function) # 实验参数保存 
        "sCMOS 视频 图片保存模块"
        self.ui.btn_sCMOS_snap.setEnabled(False)
        self.ui.btn_sCMOS_videoSave.setEnabled(False)
        self.ui.btn_sCMOS_enterSaveModel.setEnabled(False)
        self.btn_sCMOS_videoSave_state = False
        self.btn_sCMOS_enterSaveModel_state = False  # 图像保存模式状态
        self.ui.btn_sCMOS_enterSaveModel.clicked.connect(self.btn_sCMOS_enterSaveModel_function)
        self.ui.btn_sCMOS_videoSave.clicked.connect(self.btn_sCMOS_videoSave_function)# 开始视频保存
        self.ui.btn_sCMOS_snap.clicked.connect(self.btn_sCMOS_snap_function)# 捕获单张图像

        # 下面为保存视频文件时命名的实验信息
        # 定义压力信息确认状态，只有确认状态下才会用其数值作为图像保存的名字
        self.ui.spb_triggerCapture_time.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_triggerRelease_time.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_triggerReleaseSort_time.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_triggerFunction_time.valueChanged.connect(self.update_image_processing_para)

        self.ui.btn_missEventVideoSaveModel.clicked.connect(self.update_image_processing_para)
        self.ui.spb_missEventSavePreFrames.valueChanged.connect(self.update_image_processing_para)

        """背景图片相关"""
        self.ui.btn_backgroundExtract.clicked.connect(self.btn_backgroundExtract_function)
        """Capture  ROI 模块"""
        self.btn_captureROI_view_state = False
        self.ui.btn_captureROI_view.clicked.connect(self.btn_captureROI_view_function)
        self.ui.spb_captureROI_X.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_captureROI_Y.setEnabled(False)
        self.ui.spb_captureROI_width.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_captureROI_height.setEnabled(False)
        
        """Cell Flow Through ROI 模块"""
        self.btn_cellFlowThroughROI_view_state = False
        self.ui.btn_cellFlowThroughROI_view.clicked.connect(self.btn_cellFlowThroughROI_view_function)
        self.ui.spb_cellFlowThroughROI_X.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_cellFlowThroughROI_Y.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_cellFlowThroughROI_width.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_cellFlowThroughROI_height.valueChanged.connect(self.update_image_processing_para)
        """Trapped ROI 模块"""
        self.btn_trappedROI_view_state = False
        self.ui.btn_trappedROI_view.clicked.connect(self.btn_trappedROI_view_function)
        self.ui.spb_trappedROI_X.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_trappedROI_Y.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_trappedROI_width.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_trappedROI_height.valueChanged.connect(self.update_image_processing_para)
        """sCMOS Function Measurement ROI 模块"""
        """Collected ROI 模块"""
        self.btn_collectedROI_view_state = False
        self.ui.btn_collectedROI_view.clicked.connect(self.btn_collectedROI_view_function)
        self.ui.spb_collectedROI_X.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_collectedROI_Y.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_collectedROI_width.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_collectedROI_height.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_collectedROI_angle.valueChanged.connect(self.update_image_processing_para)

        """Binary ROI 模块"""
        self.ui.spb_threshold_Bi.valueChanged.connect(self.update_image_processing_para)
        """Flow Rate Detection ROI 模块"""
        self.btn_flowRateROI_view_state = False
        self.ui.btn_flowRateROI_view.clicked.connect(self.btn_flowRateROI_view_function)
        self.ui.spb_flowRateROI_X.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_flowRateROI_Y.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_flowRateROI_width.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_flowRateROI_height.valueChanged.connect(self.update_image_processing_para)
        
        self.ui.spb_flowRateValue.setStyleSheet("QDoubleSpinBox::up-button { width: 0px; } ""QDoubleSpinBox::down-button { width: 0px; }") # 取消流速的增减按钮
        self.ui.spb_cellSpeedValue.setStyleSheet("QDoubleSpinBox::up-button { width: 0px; } ""QDoubleSpinBox::down-button { width: 0px; }")
        self.ui.spb_flowRateValue.setEnabled(False)
        self.ui.spb_cellSpeedValue.setEnabled(False)
        self.ui.cmb_objective.setEnabled(False)
        self.ui.btn_flowRateImageProcessing.setEnabled(False)
        self.ui.spb_chipChannel_width.setEnabled(False)
        self.ui.spb_chipChannel_height.setEnabled(False)
        self.ui.spb_flowRateDetectFramesNumber.setEnabled(False)
        self.ui.spb_flowRateDetectFramesNumber.valueChanged.connect(self.update_image_processing_para)
        self.btn_enterSettingPara_flowRateDetection_state = False
        self.ui.btn_enterSettingPara_flowRateDetection.clicked.connect(self.btn_enterSettingPara_flowRateDetection_function)
        self.ui.btn_flowRateImageProcessing.clicked.connect(self.slot_btn_flowRateImageProcessing_function)

        # 点击设置按钮后才能改：
        self.ui.btn_enterSettingPara_flowRateDetection.clicked.connect(self.update_image_processing_para)

        """Trigger Control 模块"""

        self.ui.btn_triggerCapture.setEnabled(False)
        self.ui.btn_triggerReleaseSort.setEnabled(False)
        self.ui.btn_triggerRelease.setEnabled(False)
        self.ui.btn_triggerFunction.setEnabled(False)
        self.ui.btn_triggerCapture.clicked.connect(self.btn_triggerCapture_function)    # Trigger单片机发送信号的按钮连接
        self.ui.btn_triggerReleaseSort.clicked.connect(self.btn_triggerReleaseSort_function)
        self.ui.btn_triggerRelease.clicked.connect(self.btn_triggerRelease_function)
        self.ui.btn_triggerFunction.clicked.connect(self.slot_btn_triggerFunction_function)
                
        #单片机信号发生变化时更新子线程的参数
        self.ui.spb_triggerCapture_time.valueChanged.connect(self.updata_MCU_tirgger_para)
        self.ui.spb_triggerReleaseSort_time.valueChanged.connect(self.updata_MCU_tirgger_para)
        self.ui.spb_triggerRelease_time.valueChanged.connect(self.updata_MCU_tirgger_para)
        self.ui.spb_triggerFunction_time.valueChanged.connect(self.updata_MCU_tirgger_para)
           
        """Image Processing Settings 模块"""
        self.ui.spb_trapFrames.setEnabled(False)
        self.ui.spb_trapBalance_Time.setEnabled(False)
        self.ui.spb_collectFrames.setEnabled(False)
        self.ui.spb_maxArea.setEnabled(False)
        self.ui.spb_minArea.setEnabled(False)       
        self.ui.btn_runScreenCell_continue.setEnabled(False)
        self.ui.btn_runScreenCell_single.setEnabled(False)
        self.ui.chb_isTarget.setChecked(False)

        self.ui.spb_fastCamera_displayGray_max.valueChanged.connect(self.update_image_processing_para)
        #self.ui.spb_fastCamera_displayGray_max.valueChanged.connect(self.spb_fastCamera_displayGray_max_function)
        self.ui.spb_fastCamera_displayGray_max.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_sCMOS_displayGray_max.valueChanged.connect(self.update_image_processing_para)
        self.ui.btn_enterImageProcessingModel.setEnabled(False)
        self.ui.btn_enterImageProcessingModel.clicked.connect(self.btn_enterImageProcessingModel_function)
        self.ui.spb_minArea.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_maxArea.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_collectFrames.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_trapFrames.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_trapFrames.valueChanged.connect(self.updata_MCU_tirgger_para)
        self.ui.spb_trapBalance_Time.valueChanged.connect(self.updata_MCU_tirgger_para)
        self.ui.chb_isTarget.toggled.connect(self.updata_MCU_tirgger_para)
        self.ui.btn_runScreenCell_continue.clicked.connect(self.update_image_processing_para)
        self.ui.btn_runScreenCell_single.clicked.connect(self.update_image_processing_para)
        
        # 发送参数
        # 物镜大小选择
        self.ui.cmb_objective.currentIndexChanged.connect(self.update_image_processing_para)
        # 将Image trigger的按钮和函数连接
        self.ui.btn_recountCell_number.clicked.connect(self.btn_recountCell_number_function)
        self.ui.btn_saveROIImage.clicked.connect(self.btn_saveROIImage_function)
        self.ui.btn_runScreenCell_continue.clicked.connect(self.btn_runScreenCell_continue_function)
        self.ui.btn_runScreenCell_continue.clicked.connect(self.updata_MCU_tirgger_para)
        self.ui.btn_runScreenCell_single.clicked.connect(self.slot_btn_runScreenCell_single_function)
        self.ui.btn_runScreenCell_single.clicked.connect(self.updata_MCU_tirgger_para)

        # 导出roi的处理参数
        self.ui.btn_exportROIData.clicked.connect(self.btn_exportROIData_function)
        self.ui.spb_roiDataNumber.setStyleSheet("QSpinBox::up-button { width: 0px; } ""QSpinBox::down-button { width: 0px; }") # 取消流速的增减按钮
        self.ui.spb_roiDataNumber.setEnabled(False)
        """Rinse Channel 模块"""
        self.btn_rinseChannelCapture_state    = False
        self.btn_rinseChannelSort_state       = False
        self.btn_rinseChannelRelease_state    = False
        self.btn_enterRinseChannelModel_state = False
        self.ui.btn_enterRinseChannelModel.setEnabled(False)
        self.ui.btn_rinseChannelSort.setEnabled(False)
        self.ui.btn_rinseChannelCapture.setEnabled(False)
        self.ui.btn_rinseChannelRelease.setEnabled(False)
        self.ui.btn_rinseChannelSort.clicked.connect(self.btn_rinseChannelSort_function)
        self.ui.btn_rinseChannelCapture.clicked.connect(self.btn_rinseChannelCapture_function)
        self.ui.btn_rinseChannelRelease.clicked.connect(self.btn_rinseChannelRelease_function)
        self.ui.btn_enterRinseChannelModel.clicked.connect(self.btn_enterRinseChannelModel_function)

        self.ui.lb_none.setVisible(False)
        self.refresh_sim_settings_summary()
    """def spb_fastCamera_displayGray_max_function(self):
        #更新灰度值，用于保存fastCamera的图像
        self.signal_updataFastCamera_maxGray.emit(self.ui.spb_fastCamera_displayGray_max.value())
        print("11111111")"""

    # Configure 参数保存模块
    def btn_saveConfigureSettings_function(self):
        """保存所有控件的参数到JSON文件"""
        # 弹出保存文件对话框
        file_path, _ = qw.QFileDialog.getSaveFileName(self, "保存参数", "", "JSON Files (*.json)")
        
        if file_path:
            try:
                self.persist_sim_app_config_from_ui()
                configure_settings = self.collect_current_settings()  # 复用参数收集
                with open(file_path, 'w') as f:
                    json.dump(configure_settings, f, indent=4)
                qw.QMessageBox.information(self, "成功", "参数保存成功！")
            except Exception as e:
                qw.QMessageBox.critical(self, "错误", f"保存失败：{str(e)}")

    def btn_loadConfigureSettings_function(self):
        """从JSON文件加载参数"""
        file_path, _ = qw.QFileDialog.getOpenFileName(
            self, "加载参数", "", "JSON Files (*.json)"
        )
        
        if not file_path:
            return
        
        try:
            self.load_configure_settings(file_path)
            
            qw.QMessageBox.information(self, "Succeed", "Loaded Successfully!")
            
        except Exception as e:
            qw.QMessageBox.critical(self, "Wrong", f"Wrong Loading：{str(e)}")
            #发送一次参数
        self.update_image_processing_para()

        self.update_image_processing_para()

    # ---- 新增 SIM 控件（波长下拉 / immediate-RO 找样品下拉 / SIM9 采集按钮）----
    def _build_sim_immediate_and_acquire_controls(self):
        """为静态定义的波长下拉、immediate-RO 下拉、SIM9 采集按钮做运行时接线。

        三者均已由 CellSorting_ui 静态定义（``cmb_sCMOS_laser`` 在 SIM Camera Settings 组
        gridLayout_32；``cmb_SLM_immediateRO`` 在 SLM 组 2×2 的 (1,1)；``btn_sim9_acquire``
        在滚动内容容器内）；本方法只补 .ui 无法表达的运行时部分——带 itemData 的下拉项填充、
        列伸缩 / popup 浮层宽、tooltip、字体绑定、信号连接、blockSignals 防回环。
        """

        # 1) 波长下拉：已由 CellSorting_ui 在 SIM Camera Settings 组 gridLayout_32 (2,1)=标签、
        #    (3,1)=下拉 静态定义；此处仅填充波长项（带 itemData，无法静态表达）、选默认、接信号。
        combo = getattr(self.ui, "cmb_sCMOS_laser", None)
        if combo is not None:
            combo.blockSignals(True)
            combo.clear()
            for wavelength in (405, 488, 561, 638):
                combo.addItem(str(wavelength), wavelength)
            combo.setCurrentText("488")
            combo.blockSignals(False)
            combo.currentIndexChanged.connect(self.on_sim_camera_setting_changed)

        # 2) SLM 连接区 2×2 已由 CellSorting_ui 静态定义（device(0,0)110 / connect(0,1)154 /
        #    refresh(1,0)90 / immediateRO(1,1)154；lbl_SLM_status 移出布局作 layout 外 hidden 子）。
        #    此处仅补 .ui 无法表达的：列伸缩/列最小宽、immediateRO 的 popup 浮层宽、tooltip、初始项、信号。
        slm_grid = getattr(self.ui, "gridLayout_SLMConnection", None)
        combo_ro = getattr(self.ui, "cmb_SLM_immediateRO", None)
        if slm_grid is not None and combo_ro is not None:
            # 列伸缩 / 列最小宽（.ui 的 columnStretch/columnMinimumWidth 不被 pyuic5 生成，运行时补）。
            slm_grid.setColumnMinimumWidth(0, 110)   # 左列：设备名 / Refresh
            slm_grid.setColumnMinimumWidth(1, 154)   # 右列：Connect / RO 下拉
            slm_grid.setColumnMinimumWidth(2, 0)
            slm_grid.setColumnStretch(0, 0)
            slm_grid.setColumnStretch(1, 0)
            slm_grid.setColumnStretch(2, 1)   # 余量由最右空列吸收，窄控件不被拉伸错位
            # 闭合下拉宽 154（.ui 静态）；弹出列表(浮层)固定加宽到 190，以便出现竖向滚动条(~17px)时
            # 仍能完整显示最长 RO 名（如 488_3.5_2d_imm_f10/_3dir≈141px）；长项 ElideRight + 全文走 ToolTipRole。
            # QComboBox.view() 是运行时对象、.ui 无法表达，故 popup 属性留运行时设置。
            combo_ro.view().setMinimumWidth(190)
            combo_ro.view().setMaximumWidth(190)
            combo_ro.view().setTextElideMode(Qt.ElideRight)
            combo_ro.setToolTip(
                "找样品：选 immediate RO 让 SLM 持续出图并自动开对应波长激光"
            )
            self._reset_immediate_ro_dropdown()
            combo_ro.activated.connect(self.on_immediate_ro_changed)
            combo_ro.currentTextChanged.connect(self._update_immediate_ro_dropdown_tooltip)

        # 3) SIM9 采集按钮：已由 CellSorting_ui 静态定义（父=滚动内容容器，缩小窗口随内容
        #    滚动、不再浮在主窗口上被裁剪）。此处仅运行时接线：字体沿用 SIM 设置按钮、连接采集触发。
        button = getattr(self.ui, "btn_sim9_acquire", None)
        if button is not None:
            # 字体已由 CellSorting_ui 静态给定（Times New Roman 12pt）；不再依赖已删除的
            # btn_openSimSettings 复制字体。
            button.clicked.connect(lambda: self.trigger_sim_raw_9frame_acquisition("sim9_button"))

    def _reset_immediate_ro_dropdown(self):
        """把 immediate-RO 下拉重置为只含「(none)」并选中它（blockSignals 避免触发激活）。"""
        combo = getattr(self.ui, "cmb_SLM_immediateRO", None)
        if combo is None:
            return
        combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItem("(none)", None)
            if hasattr(combo, "setItemData"):
                combo.setItemData(0, "(none)", Qt.ToolTipRole)
            combo.setCurrentIndex(0)
        finally:
            combo.blockSignals(False)
        update_tooltip = getattr(self, "_update_immediate_ro_dropdown_tooltip", None)
        if callable(update_tooltip):
            update_tooltip(combo.currentText())

    def _select_immediate_ro_none_without_clearing(self):
        """只把 immediate-RO 下拉选回「(none)」，保留已扫描出的 RO 条目。"""
        combo = getattr(self.ui, "cmb_SLM_immediateRO", None)
        if combo is None:
            return
        count = combo.count() if hasattr(combo, "count") else 0
        if count <= 0:
            return
        combo.blockSignals(True)
        try:
            combo.setCurrentIndex(0)
        finally:
            combo.blockSignals(False)
        update_tooltip = getattr(self, "_update_immediate_ro_dropdown_tooltip", None)
        if callable(update_tooltip):
            update_tooltip(combo.currentText())

    def _update_immediate_ro_dropdown_tooltip(self, text=None):
        combo = getattr(self.ui, "cmb_SLM_immediateRO", None)
        if combo is None:
            return
        current_text = str(text if text is not None else combo.currentText())
        if not hasattr(combo, "setToolTip"):
            return
        if current_text and current_text != "(none)":
            combo.setToolTip(current_text)
        else:
            combo.setToolTip("找样品：选 immediate RO 让 SLM 持续出图并自动开对应波长激光")

    def get_sim_zscan_timing_records(self):
        # 摘要刷新调用点多且都在 GUI 线程；历史 JSONL 只在 mtime/size 变化（设置弹窗
        # 测试追加、外部进程写入）时重读，其余时候复用内存缓存，避免每次全量读盘。
        history_path = DEFAULT_Z_SCAN_TIMING_HISTORY_PATH
        try:
            stat_result = history_path.stat()
            signature = (stat_result.st_mtime_ns, stat_result.st_size)
        except OSError:
            signature = ("missing",)
        cached_records = getattr(self, "sim_zscan_timing_records_cache", None)
        if cached_records is None or signature != getattr(self, "sim_zscan_timing_history_signature", None):
            try:
                cached_records = load_z_scan_timing_records(history_path)
            except OSError:
                # 文件被锁定/权限异常时退回旧缓存，保证摘要刷新不因历史文件不可读而失败。
                logger.debug("Failed to read z-scan timing history.", exc_info=True)
                cached_records = [] if cached_records is None else cached_records
            self.sim_zscan_timing_records_cache = cached_records
            self.sim_zscan_timing_history_signature = signature
        return cached_records

    def refresh_sim_settings_summary(self):
        sim_config = app_config_from_dict(app_config_to_dict(self.sim_app_config))
        runtime_timing = dict(getattr(self, "sim_runtime_timing_snapshot", {}) or {})
        self.ui.pte_simSummary.setPlainText(
            build_sim_settings_summary(
                sim_config,
                runtime_timing=runtime_timing,
                z_scan_timing_records=MainWindow.get_sim_zscan_timing_records(self),
            )
        )

    def current_sim_camera_size_presets(self):
        return tuple(getattr(self, "sim_camera_size_presets", SIM_CAMERA_SIZE_PRESETS) or SIM_CAMERA_SIZE_PRESETS)

    def current_sim_camera_sensor_size(self):
        return tuple(getattr(self, "sim_camera_sensor_size", DEFAULT_SIM_CAMERA_SIZE) or DEFAULT_SIM_CAMERA_SIZE)

    def current_sim_camera_roi_step_px(self):
        return max(1, int(getattr(self, "sim_camera_roi_step_px", SIM_CAMERA_ROI_STEP_PX) or SIM_CAMERA_ROI_STEP_PX))

    def refresh_sim_camera_size_choices(self, selected_size=None):
        combo = self.ui.cmb_sCMOS_imageSize
        presets = MainWindow.current_sim_camera_size_presets(self)
        if selected_size is None:
            selected_size = (
                int(self.sim_app_config.camera.roi_width),
                int(self.sim_app_config.camera.roi_height),
            )
        selected_label = f"{int(selected_size[0])} x {int(selected_size[1])}"
        labels = labels_for_sim_camera_size_presets(presets)
        combo.blockSignals(True)
        try:
            combo.clear()
            for label in labels:
                combo.addItem(label)
            target_index = combo.findText(selected_label)
            if target_index < 0:
                target_index = 0
            combo.setCurrentIndex(target_index)
            combo.setCurrentText(combo.itemText(target_index))
        finally:
            combo.blockSignals(False)

    def configure_sim_camera_spinboxes(self):
        for spinbox in (
            self.ui.spb_sCMOS_ROI_X,
            self.ui.spb_sCMOS_ROI_Y,
            self.ui.spb_sCMOS_exposureTime,
        ):
            spinbox.setKeyboardTracking(False)
        step_px = MainWindow.current_sim_camera_roi_step_px(self)
        self.ui.spb_sCMOS_ROI_X.setSingleStep(step_px)
        self.ui.spb_sCMOS_ROI_Y.setSingleStep(step_px)

    def sync_sim_camera_roi_position_controls(self, controls_enabled=None):
        camera = self.sim_app_config.camera
        presets = MainWindow.current_sim_camera_size_presets(self)
        sensor_size = MainWindow.current_sim_camera_sensor_size(self)
        max_x, max_y = sim_camera_roi_origin_bounds(
            camera.roi_width,
            camera.roi_height,
            sensor_size=sensor_size,
            presets=presets,
        )
        blocked_widgets = (
            self.ui.spb_sCMOS_ROI_X,
            self.ui.spb_sCMOS_ROI_Y,
        )
        for widget in blocked_widgets:
            widget.blockSignals(True)
        try:
            self.ui.spb_sCMOS_ROI_X.setMaximum(max_x)
            self.ui.spb_sCMOS_ROI_Y.setMaximum(max_y)
            self.ui.spb_sCMOS_ROI_X.setValue(camera.roi_x)
            self.ui.spb_sCMOS_ROI_Y.setValue(camera.roi_y)
        finally:
            for widget in blocked_widgets:
                widget.blockSignals(False)

        if controls_enabled is None:
            controls_enabled = self.ui.cmb_sCMOS_imageSize.isEnabled()
        roi_position_enabled = bool(controls_enabled) and not is_full_frame_sim_camera_size(
            camera.roi_width,
            camera.roi_height,
            sensor_size=sensor_size,
            presets=presets,
        )
        self.ui.spb_sCMOS_ROI_X.setEnabled(roi_position_enabled)
        self.ui.spb_sCMOS_ROI_Y.setEnabled(roi_position_enabled)

    def sync_sim_camera_controls_from_config(self):
        camera = self.sim_app_config.camera
        bit_depth_combo = getattr(self.ui, "cmb_sCMOS_bitDepth", None)
        presets = MainWindow.current_sim_camera_size_presets(self)
        sensor_size = MainWindow.current_sim_camera_sensor_size(self)
        step_px = MainWindow.current_sim_camera_roi_step_px(self)
        roi_width, roi_height, roi_x, roi_y = normalize_sim_camera_roi(
            camera.roi_width,
            camera.roi_height,
            camera.roi_x,
            camera.roi_y,
            sensor_size=sensor_size,
            step_px=step_px,
            presets=presets,
        )
        camera.roi_width = roi_width
        camera.roi_height = roi_height
        camera.roi_x = roi_x
        camera.roi_y = roi_y
        laser_combo = getattr(self.ui, "cmb_sCMOS_laser", None)
        blocked_widgets = [
            self.ui.cmb_sCMOS_imageSize,
            self.ui.spb_sCMOS_exposureTime,
        ]
        if bit_depth_combo is not None:
            blocked_widgets.append(bit_depth_combo)
        if laser_combo is not None:
            blocked_widgets.append(laser_combo)
        for widget in blocked_widgets:
            widget.blockSignals(True)
        try:
            MainWindow.refresh_sim_camera_size_choices(
                self,
                selected_size=(camera.roi_width, camera.roi_height),
            )
            self.ui.spb_sCMOS_exposureTime.setValue(sim_exposure_us_to_ms(camera.exposure_us))
            if laser_combo is not None:
                # 波长是采集波长唯一主入口；按配置回显，未知波长回落 488。
                laser_index = laser_combo.findData(int(self.sim_app_config.selected_laser_nm))
                if laser_index < 0:
                    laser_index = laser_combo.findData(488)
                if laser_index >= 0:
                    laser_combo.setCurrentIndex(laser_index)
            if bit_depth_combo is not None:
                target_bit_depth_label = sim_bit_depth_to_label(getattr(camera, "bit_depth", SIM_BIT_DEPTH_DEFAULT))
                target_index = bit_depth_combo.findText(target_bit_depth_label)
                if target_index < 0:
                    bit_depth_combo.clear()
                    bit_depth_combo.addItem(target_bit_depth_label)
                    target_index = 0
                bit_depth_combo.setCurrentIndex(target_index)
                bit_depth_combo.setCurrentText(target_bit_depth_label)
        finally:
            for widget in blocked_widgets:
                widget.blockSignals(False)
        self.sync_sim_camera_roi_position_controls()

    def refresh_sim_camera_bit_depth_choices(self, supported_bit_depths=None):
        combo = getattr(self.ui, "cmb_sCMOS_bitDepth", None)
        if combo is None:
            return
        camera = self.sim_app_config.camera
        supported = sorted(
            set(USER_FACING_BIT_DEPTHS)
            | {int(value) for value in (supported_bit_depths or [SIM_BIT_DEPTH_DEFAULT])}
        )
        if not supported:
            supported = [SIM_BIT_DEPTH_DEFAULT]
        current_bit_depth = int(getattr(camera, "bit_depth", SIM_BIT_DEPTH_DEFAULT))
        if current_bit_depth in supported:
            selected_bit_depth = current_bit_depth
        elif SIM_BIT_DEPTH_DEFAULT in supported:
            selected_bit_depth = SIM_BIT_DEPTH_DEFAULT
        else:
            selected_bit_depth = max(supported)
        camera.bit_depth = int(selected_bit_depth)
        combo.blockSignals(True)
        try:
            combo.clear()
            for bit_depth in supported:
                combo.addItem(sim_bit_depth_to_label(bit_depth))
            combo.setCurrentText(sim_bit_depth_to_label(selected_bit_depth))
        finally:
            combo.blockSignals(False)
        self.refresh_sim_settings_summary()

    def commit_sim_camera_pending_widget_edits(self):
        for widget_name in (
            "spb_sCMOS_exposureTime",
            "spb_sCMOS_ROI_X",
            "spb_sCMOS_ROI_Y",
        ):
            widget = getattr(self.ui, widget_name, None)
            interpret_text = getattr(widget, "interpretText", None)
            if callable(interpret_text):
                interpret_text()

    def sync_sim_camera_config_from_ui(self, save_to_disk=True):
        MainWindow.commit_sim_camera_pending_widget_edits(self)
        camera = self.sim_app_config.camera
        bit_depth_combo = getattr(self.ui, "cmb_sCMOS_bitDepth", None)
        presets = MainWindow.current_sim_camera_size_presets(self)
        sensor_size = MainWindow.current_sim_camera_sensor_size(self)
        step_px = MainWindow.current_sim_camera_roi_step_px(self)
        roi_width, roi_height = size_from_sim_camera_label(
            self.ui.cmb_sCMOS_imageSize.currentText(),
            presets=presets,
        )
        roi_width, roi_height, roi_x, roi_y = normalize_sim_camera_roi(
            roi_width,
            roi_height,
            self.ui.spb_sCMOS_ROI_X.value(),
            self.ui.spb_sCMOS_ROI_Y.value(),
            sensor_size=sensor_size,
            step_px=step_px,
            presets=presets,
        )
        camera.roi_x = roi_x
        camera.roi_y = roi_y
        camera.roi_width = roi_width
        camera.roi_height = roi_height
        camera.exposure_us = sim_exposure_ms_to_us(self.ui.spb_sCMOS_exposureTime.value())
        camera.bit_depth = sim_bit_depth_from_label(
            bit_depth_combo.currentText() if bit_depth_combo is not None else getattr(camera, "bit_depth", SIM_BIT_DEPTH_DEFAULT)
        )
        camera.timeout_ms = max(camera.timeout_ms, 2000)
        # 波长：主 GUI 下拉是采集波长唯一主入口，写回 selected_laser_nm。
        laser_combo = getattr(self.ui, "cmb_sCMOS_laser", None)
        if laser_combo is not None:
            laser_data = laser_combo.currentData()
            if laser_data is not None:
                self.sim_app_config.selected_laser_nm = int(laser_data)
        self.sync_sim_camera_roi_position_controls()
        if hasattr(self.ui, "cmb_sCMOS_camera") and self.ui.cmb_sCMOS_camera.currentIndex() >= 0:
            selected_index = self.ui.cmb_sCMOS_camera.currentIndex()
            if selected_index < len(self.sim_available_cameras):
                selected_camera = self.sim_available_cameras[selected_index]
                camera.device_index = int(selected_camera.get("index", camera.device_index))
                camera.device_label = str(selected_camera.get("display", camera.device_label))
        if save_to_disk:
            save_app_config(self.sim_app_config, self.sim_app_config.config_path)
        self.refresh_sim_settings_summary()

    def persist_sim_app_config_from_ui(self):
        self.sync_sim_camera_config_from_ui(save_to_disk=False)
        # B3：Save Config 时把主界面 DAQ/Recon 模块控件最新状态写回 sim_app_config，
        # 与相机控件一致（即便改值槽已即时写回，这里再同步一次作保存前防御）。
        self._persist_main_daq_config_from_ui()
        self._persist_main_recon_config_from_ui()
        save_app_config(self.sim_app_config, self.sim_app_config.config_path)

    def update_sim_camera_action_buttons(self):
        has_devices = bool(self.sim_available_cameras)
        connect_enabled = (self.sim_camera_connected or has_devices) and not self.sim_acquisition_in_progress
        live_button_active = bool(
            getattr(self, "sim_preview_requested", False)
            or self.sim_preview_active
            or self.sim_preview_stop_in_progress
        )
        self.ui.btn_sCMOS_connection.setEnabled(connect_enabled)
        self.ui.btn_sCMOS_connection.setText(
            "Disconnect SIM Camera" if self.sim_camera_connected else "Connect SIM Camera"
        )
        self.ui.btn_sCMOS_connection.setStyleSheet(
            "background-color: #4EEE94" if self.sim_camera_connected else "background-color: #E1E1E1"
        )
        self.ui.btn_sCMOS_live.setEnabled(self.sim_camera_connected and not self.sim_acquisition_in_progress)
        self.ui.btn_sCMOS_live.setText("Abort" if live_button_active else "Live")
        self.ui.btn_sCMOS_live.setStyleSheet(
            "background-color: #4EEE94" if live_button_active else "background-color: #E1E1E1"
        )
        self.ui.btn_sCMOS_refresh.setEnabled((not self.sim_camera_connected) and not self.sim_acquisition_in_progress)
        self.ui.cmb_sCMOS_camera.setEnabled(
            (not self.sim_camera_connected) and has_devices and not self.sim_acquisition_in_progress
        )
        update_slm = getattr(self, "update_sim_slm_controls", None)
        if callable(update_slm):
            update_slm()

    def refresh_sim_camera_devices(self, show_dialog_on_error=False):
        self.prefer_real_sim_hardware(
            save_to_disk=True,
            probe_camera=not self.sim_camera_connected,
        )
        devices = []
        if self.sim_camera_connected:
            try:
                devices = [dict(self.sim_acquisition_controller.camera_connection_info())]
            except Exception as e:
                print(f"SIM connected camera info refresh failed: {str(e)}")
                if show_dialog_on_error:
                    qw.QMessageBox.warning(self, "SIM Camera", str(e))
            if not devices or not devices[0]:
                fallback_index = int(self.sim_app_config.camera.device_index)
                fallback_label = str(
                    self.sim_app_config.camera.device_label or f"Camera {fallback_index}"
                )
                devices = [{"index": fallback_index, "display": fallback_label}]
            else:
                devices[0].setdefault("index", int(self.sim_app_config.camera.device_index))
                devices[0].setdefault(
                    "display",
                    str(
                        self.sim_app_config.camera.device_label
                        or f"Camera {int(devices[0]['index'])}"
                    ),
                )
        else:
            self.ensure_sim_runtime()
            try:
                devices = self.sim_acquisition_controller.refresh_available_camera_devices()
            except Exception as e:
                devices = []
                print(f"SIM camera enumeration failed: {str(e)}")
                if show_dialog_on_error:
                    qw.QMessageBox.warning(self, "SIM Camera", str(e))
        self.sim_available_cameras = list(devices)
        self.ui.cmb_sCMOS_camera.blockSignals(True)
        self.ui.cmb_sCMOS_camera.clear()
        selected_combo_index = -1
        for combo_index, device in enumerate(self.sim_available_cameras):
            display = str(device.get("display", f"Camera {combo_index}"))
            self.ui.cmb_sCMOS_camera.addItem(display)
            if int(device.get("index", -1)) == int(self.sim_app_config.camera.device_index):
                selected_combo_index = combo_index
        if self.sim_available_cameras:
            if selected_combo_index < 0:
                selected_combo_index = 0
            self.ui.cmb_sCMOS_camera.setCurrentIndex(selected_combo_index)
            selected_camera = self.sim_available_cameras[selected_combo_index]
            self.sim_app_config.camera.device_index = int(selected_camera.get("index", 0))
            self.sim_app_config.camera.device_label = str(selected_camera.get("display", ""))
        else:
            self.sim_app_config.camera.device_label = ""
        self.ui.cmb_sCMOS_camera.blockSignals(False)
        supported_bit_depths = [SIM_BIT_DEPTH_DEFAULT]
        if self.sim_available_cameras:
            selected_device = self.sim_available_cameras[max(self.ui.cmb_sCMOS_camera.currentIndex(), 0)]
            supported_bit_depths = selected_device.get("supported_bit_depths") or supported_bit_depths
        MainWindow.refresh_sim_camera_bit_depth_choices(self, supported_bit_depths)
        self.update_sim_camera_action_buttons()

    def refresh_sim_slm_devices(self, show_dialog_on_error=False):
        if not hasattr(self.ui, "cmb_SLM_device"):
            return
        self.ensure_sim_runtime()
        devices = []
        if self.sim_slm_connected:
            try:
                devices = [dict(self.sim_acquisition_controller.slm_connection_info())]
            except Exception as e:
                print(f"SIM connected SLM info refresh failed: {str(e)}")
                if show_dialog_on_error:
                    qw.QMessageBox.warning(self, "SIM SLM", str(e))
            if not devices or not devices[0]:
                devices = [{"display": "Connected SLM", "path": ""}]
            else:
                devices[0].setdefault(
                    "display",
                    str(
                        devices[0].get("device_serial_hint")
                        or devices[0].get("serial_number")
                        or devices[0].get("device_id")
                        or "Connected SLM"
                    ),
                )
                devices[0].setdefault("path", str(devices[0].get("device_path", "")))
        else:
            try:
                devices = self.sim_acquisition_controller.refresh_available_slm_devices()
            except Exception as e:
                devices = []
                print(f"SIM SLM enumeration failed: {str(e)}")
                if show_dialog_on_error:
                    qw.QMessageBox.warning(self, "SIM SLM", str(e))
        self.sim_available_slms = list(devices)
        self.ui.cmb_SLM_device.blockSignals(True)
        self.ui.cmb_SLM_device.clear()
        selected_index = 0
        for index, device in enumerate(self.sim_available_slms):
            display = str(device.get("display") or device.get("serial") or device.get("id") or f"SLM {index}")
            self.ui.cmb_SLM_device.addItem(display)
        if self.sim_available_slms:
            self.ui.cmb_SLM_device.setCurrentIndex(selected_index)
        self.ui.cmb_SLM_device.blockSignals(False)
        self.update_sim_slm_controls()

    def update_sim_slm_controls(self):
        if not hasattr(self.ui, "btn_SLM_connection"):
            return
        has_devices = bool(getattr(self, "sim_available_slms", []))
        busy = bool(getattr(self, "sim_acquisition_in_progress", False))
        self.ui.cmb_SLM_device.setEnabled((not self.sim_slm_connected) and has_devices and not busy)
        self.ui.btn_SLM_refresh.setEnabled((not self.sim_slm_connected) and not busy)
        self.ui.btn_SLM_connection.setEnabled((self.sim_slm_connected or has_devices) and not busy)
        self.ui.btn_SLM_connection.setText("Disconnect SLM" if self.sim_slm_connected else "Connect SLM")
        # immediate-RO 找样品下拉：仅在 SLM 已连接、未采集、未停预览时可用。
        immediate_combo = getattr(self.ui, "cmb_SLM_immediateRO", None)
        if immediate_combo is not None:
            stopping = bool(getattr(self, "sim_preview_stop_in_progress", False))
            immediate_combo.setEnabled(self.sim_slm_connected and not busy and not stopping)

    def toggle_sim_slm_connection(self):
        self.ensure_sim_runtime()
        if self.sim_slm_connected:
            # 断开前先关找样品激光（best-effort，尽早），再清缓存的 immediate 列表。
            self.stop_immediate_live_mode()
            try:
                self.sim_acquisition_controller.disconnect_slm()
            except Exception as e:
                print(f"SIM SLM disconnect failed: {str(e)}")
                qw.QMessageBox.warning(self, "SIM SLM", str(e))
                return
            self.sim_slm_connected = False
            self.sim_immediate_ro_items = []
            self._reset_immediate_ro_dropdown()
            self.sim_app_config.selected_running_order = ""
            save_app_config(self.sim_app_config, self.sim_app_config.config_path)
            self.refresh_sim_settings_summary()
            self.refresh_sim_slm_devices()
            return

        if not self.sim_available_slms:
            self.refresh_sim_slm_devices(show_dialog_on_error=True)
        if not self.sim_available_slms:
            qw.QMessageBox.information(self, "SIM SLM", "No SLM was found. Please refresh the device list.")
            return

        selected_combo_index = self.ui.cmb_SLM_device.currentIndex()
        if selected_combo_index < 0 or selected_combo_index >= len(self.sim_available_slms):
            qw.QMessageBox.information(self, "SIM SLM", "Please select an SLM before connecting.")
            return

        selected_slm = self.sim_available_slms[selected_combo_index]
        device_path = str(selected_slm.get("path") or selected_slm.get("id") or "")
        wavelength = self.sim_app_config.selected_laser_nm
        exposure_us = self.sim_app_config.camera.exposure_us
        controller = self.sim_acquisition_controller

        def _slm_connect_fn():
            # 冷启动安全复位：上次进程若崩溃时激光线写高，NI 静态 DO 仍保持高，新进程
            # 无从得知。连接前做一次 best-effort set_all_low（仅在 controller 不忙时）。
            if not controller.is_busy:
                try:
                    controller.reset_all_daq_low()
                except Exception as reset_error:
                    print(f"SIM DAQ cold-start reset failed (verify DAQ state): {reset_error}")
            controller.connect_slm(device_path=device_path or None)
            # 在后台 worker 里扫描全部 RO+激活类型并缓存（逐个 select 有 USB 往返）。
            controller.refresh_immediate_running_orders()
            ro_payload = controller.select_running_order_for_task(wavelength, exposure_us)
            result = dict(ro_payload or {})
            # 带回找样品下拉展示列表（含命名正确但未烧成 immediate 的诊断项）供 GUI 线程纯读填充。
            result["immediate_items"] = list(controller.list_immediate_dropdown_running_orders())
            result["immediate_warnings"] = controller.immediate_running_order_warnings(wavelength)
            return result

        if getattr(self, "_slm_connect_thread", None) is not None:
            return
        self.ui.btn_SLM_connection.setEnabled(False)
        _slm_thread = QThread()
        _slm_worker = _ConnectWorker(_slm_connect_fn)
        _slm_worker.moveToThread(_slm_thread)
        # Keep strong references so the GC cannot collect live QThread/QObject.
        self._slm_connect_thread = _slm_thread
        self._slm_connect_worker = _slm_worker

        def _on_slm_success(result):
            self.sim_slm_connected = True
            self.sim_app_config.selected_running_order = str(result.get("running_order_name", ""))
            # 纯读缓存填充 immediate 下拉（扫描已在后台 worker 完成）。
            self.sim_immediate_ro_items = list(result.get("immediate_items", []))
            self.refresh_immediate_ro_dropdown()
            save_app_config(self.sim_app_config, self.sim_app_config.config_path)
            self.refresh_sim_settings_summary()
            self.update_sim_slm_controls()
            immediate_warnings = [str(item) for item in result.get("immediate_warnings", []) if item]
            if immediate_warnings:
                preview = "\n".join(immediate_warnings[:8])
                if len(immediate_warnings) > 8:
                    preview += f"\n... and {len(immediate_warnings) - 8} more"
                qw.QMessageBox.warning(
                    self,
                    "SIM SLM",
                    "Some sample-finding Running Orders were found but are not ACT_IMMEDIATE, "
                    "so they are shown disabled and cannot drive the laser.\n\n"
                    f"{preview}",
                )

        def _on_slm_error(msg):
            self.sim_slm_connected = False
            self.sim_immediate_ro_items = []
            self._reset_immediate_ro_dropdown()
            self.sim_app_config.selected_running_order = ""
            try:
                self.sim_acquisition_controller.disconnect_slm()
            except Exception as cleanup_error:
                print(f"SIM SLM cleanup after failed connect failed: {str(cleanup_error)}")
            try:
                save_app_config(self.sim_app_config, self.sim_app_config.config_path)
            except Exception as save_error:
                print(f"SIM SLM failed to save cleared running order after failed connect: {str(save_error)}")
            self.refresh_sim_settings_summary()
            self.update_sim_slm_controls()
            print(f"SIM SLM connect failed: {msg}")
            qw.QMessageBox.warning(self, "SIM SLM", msg)

        def _on_slm_finished():
            _slm_thread.quit()
            _slm_thread.wait()
            self._slm_connect_thread = None
            self._slm_connect_worker = None
            self.ui.btn_SLM_connection.setEnabled(True)
            self.refresh_sim_slm_devices()

        _slm_worker.signal_success.connect(_on_slm_success)
        _slm_worker.signal_error.connect(_on_slm_error)
        _slm_worker.signal_finished.connect(_on_slm_finished)
        _slm_thread.started.connect(_slm_worker.run)
        _slm_thread.start()

    def select_current_sim_running_order(self, save_to_disk=False):
        if not getattr(self, "sim_slm_connected", False):
            return None
        self.ensure_sim_runtime()
        result = self.sim_acquisition_controller.select_running_order_for_task(
            self.sim_app_config.selected_laser_nm,
            self.sim_app_config.camera.exposure_us,
        )
        self.sim_app_config.selected_running_order = str(result.get("running_order_name", ""))
        if save_to_disk:
            save_app_config(self.sim_app_config, self.sim_app_config.config_path)
        self.refresh_sim_settings_summary()
        self.update_sim_slm_controls()
        return result

    # ---- immediate-live（找样品）下拉 + 激光联动 ----------------------------
    def refresh_immediate_ro_dropdown(self):
        """按当前波长过滤缓存的 immediate 列表填充下拉；不匹配波长的项 disable。

        总是把当前项重置为「(none)」（blockSignals，避免触发激活）。波长变化时调用。
        """
        combo = getattr(self.ui, "cmb_SLM_immediateRO", None)
        if combo is None:
            return
        current_wl = int(self.sim_app_config.selected_laser_nm)
        combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItem("(none)", None)
            if hasattr(combo, "setItemData"):
                combo.setItemData(0, "(none)", Qt.ToolTipRole)
            for item in self.sim_immediate_ro_items:
                name = str(item.get("name", ""))
                ro_index = int(item.get("index", -1))
                parsed = item.get("parsed_wavelength_nm")
                is_immediate = bool(item.get("is_immediate"))
                wavelength_matches = immediate_live_wavelength_matches(parsed, current_wl)
                selectable = is_immediate and wavelength_matches
                if not is_immediate:
                    label = f"{name}  (not immediate: {item.get('activation_type', '?')})"
                elif parsed is not None and not wavelength_matches:
                    label = f"{name}  (λ={parsed})"
                elif parsed is None:
                    label = f"{name}  (λ=?)"
                else:
                    label = name
                combo.addItem(label, ro_index if selectable else None)
                item_index = combo.count() - 1
                if hasattr(combo, "setItemData"):
                    combo.setItemData(item_index, label, Qt.ToolTipRole)
                if not selectable:
                    # 非 ACT_IMMEDIATE、波长不匹配或无法解析 → disable，不可被选中激活。
                    model_item = combo.model().item(item_index)
                    if model_item is not None:
                        model_item.setEnabled(False)
            combo.setCurrentIndex(0)
        finally:
            combo.blockSignals(False)
        update_tooltip = getattr(self, "_update_immediate_ro_dropdown_tooltip", None)
        if callable(update_tooltip):
            update_tooltip(combo.currentText())

    def on_immediate_ro_changed(self, _index=None):
        """immediate-RO 下拉变更：选 RO → 确保有效预览后开激光；选「(none)」→ 关激光。"""
        combo = getattr(self.ui, "cmb_SLM_immediateRO", None)
        if combo is None:
            return
        ro_index = combo.currentData()
        if ro_index is None:
            # 选回「(none)」：关找样品激光，但不重置下拉（用户已在 (none)）。
            self.stop_immediate_live_mode(reset_dropdown=False)
            return
        # 前置条件：SIM 相机必须已连接，否则拒绝激活、保持激光关闭。
        if not self.sim_camera_connected:
            qw.QMessageBox.information(
                self, "SIM Camera", "请先连接 SIM 相机，再选择 immediate RO 找样品。"
            )
            self.stop_immediate_live_mode()
            return
        ro_index = int(ro_index)
        wavelength = int(self.sim_app_config.selected_laser_nm)
        # 切换到新的 immediate 选择前，先取消任何在途 pending（不动已开的激光由下方决定）。
        self._cancel_immediate_pending()
        if self.sim_preview_started_confirmed and self.sim_preview_active:
            # 已确认出帧 → 直接激活 RO + 开激光。
            self._activate_immediate_now(ro_index, wavelength)
            return
        # 否则进入两阶段 pending：等 token 匹配的 preview_started 再点灯。
        self._begin_immediate_pending("activate", ro_index, wavelength)
        if not self.sim_preview_active and not self.sim_preview_stop_in_progress:
            started = self.start_sim_preview()
            if not started:
                # start 静默失败（如相机配置应用失败）→ 中止、绝不开激光、复位下拉。
                self._cancel_immediate_pending()
                self._reset_immediate_ro_dropdown()
                return
        elif self.sim_preview_stop_in_progress:
            # 预览正在停止 → 请求重启，等其 started 后由 pending 点灯。
            self.start_sim_preview()
        # preview 已发起但未确认：preview_started 到来时由 pending 点灯。

    def _activate_immediate_now(self, ro_index, wavelength):
        """已确认预览后真正激活 immediate RO + 拉高激光；失败则关激光并复位下拉。"""
        try:
            self.sim_acquisition_controller.activate_immediate_running_order(int(ro_index), int(wavelength))
        except Exception as e:
            print(f"Immediate RO activation failed: {str(e)}")
            # 兜底关光（方案：失败必拉低激光 + 回退下拉）。controller 激活失败已 best-effort
            # 拉低；这里再统一走 stop_immediate_live_mode（含重试拉低 + 复位下拉 + 清状态），
            # 确保 GUI 层也有兜底关光路径；若拉低仍失败，stop 会保留状态供后续入口重试（不误清）。
            try:
                self.stop_immediate_live_mode()
            except Exception as stop_err:  # noqa: BLE001
                print(f"Immediate RO best-effort laser-off failed: {str(stop_err)}")
            qw.QMessageBox.warning(self, "SIM SLM", str(e))
            return False
        self.sim_immediate_live_active = True
        self.sim_immediate_active_ro_index = int(ro_index)
        self.sim_immediate_active_wavelength = int(wavelength)
        return True

    def _begin_immediate_pending(self, mode, ro_index, wavelength):
        """开启两阶段 pending（等 preview_started）；带 token + 超时看门狗。

        看门狗用 ``QTimer.singleShot`` 捕获本轮 token：取消/兑现/替换都会 bump token，
        因此任何在途的旧 singleShot 触发时会因 token 失配而被忽略（无竞态）。
        """
        self._immediate_pending_token += 1
        token = self._immediate_pending_token
        self._immediate_pending = {
            "token": token,
            "mode": str(mode),
            "ro_index": int(ro_index),
            "wavelength": int(wavelength),
        }
        QTimer.singleShot(8000, lambda t=token: self._on_immediate_pending_timeout(t))

    def _cancel_immediate_pending(self):
        # bump token 使任何在途 singleShot 失配而被忽略。
        self._immediate_pending_token += 1
        self._immediate_pending = None

    def _on_immediate_pending_timeout(self, token):
        pending = self._immediate_pending
        if pending is None or pending.get("token") != token:
            return  # 已被取消/兑现/替换：忽略陈旧超时。
        mode = pending.get("mode")
        self._immediate_pending = None
        print("Immediate-live preview confirmation timed out.")
        if mode == "reconfirm":
            # planned restart 始终未拿到新 preview_started → 激光仍开，必须关。
            self.stop_immediate_live_mode()
        else:
            # activate pending：激光从未开，仅复位下拉与 planned 标志。
            self.sim_preview_planned_restart = False
            self.sim_immediate_live_active = False
            self._reset_immediate_ro_dropdown()

    def stop_immediate_live_mode(self, reset_dropdown=True):
        """关找样品激光并清全部 immediate-live 运行态；best-effort，未激活时也安全。

        所有"强制关"入口（停 Live / SIM9 / 断 SLM / 换波长 / 开设置 / 关窗口 / 异常）统一调它。
        controller.stop_immediate_live() 在未 active 时严格 no-op，不会误写 DAQ 整 port。

        关键安全：若 controller 关光失败（DAQ 拉低异常、激光可能仍高），**保留** GUI
        immediate-live 状态与下拉、不清空——否则后续入口（preview_stopped/error、下次 stop、
        采集前互锁）会误以为已关、不再重试关光。两阶段 pending（激光未开）总是先取消。
        """
        # 两阶段 pending 不涉及"已开的激光"，先无条件取消；planned restart 标志同理。
        self._cancel_immediate_pending()
        self.sim_preview_planned_restart = False
        controller = getattr(self, "sim_acquisition_controller", None)
        if controller is not None:
            try:
                controller.stop_immediate_live()
            except Exception as e:
                # 关光失败：保留 active/RO/wavelength 与下拉，留待后续入口重试，绝不误清。
                print(f"stop_immediate_live failed (laser may still be ON, keeping state to retry): {str(e)}")
                return
        self.sim_immediate_live_active = False
        self.sim_immediate_active_ro_index = None
        self.sim_immediate_active_wavelength = None
        if reset_dropdown:
            self._reset_immediate_ro_dropdown()

    def _immediate_live_active_or_pending(self):
        if self.sim_immediate_live_active or self._immediate_pending is not None:
            return True
        # fail-safe：还须参考 controller 权威态。激活失败 + 清理拉低也失败时，controller 会保留
        # arming line（激光可能仍高）而 GUI active 仍为 False；此时正式采集互锁必须据此拦住，
        # 否则可能在激光线未拉低的情况下放行 SIM9 波形（set_line 整 port 与波形并存危险）。
        controller = getattr(self, "sim_acquisition_controller", None)
        return bool(controller is not None and getattr(controller, "is_immediate_live_engaged", False))

    def btn_sCMOS_refresh_function(self):
        self.refresh_sim_camera_devices(show_dialog_on_error=True)

    def on_sim_camera_setting_changed(self):
        if getattr(self, "_loading_configure_settings", False):
            return
        # 必须在 sync 之前先记旧波长：sync 后只能读到新值，无法区分"仅改 ROI/曝光"与"换波长"。
        cfg = getattr(self, "sim_app_config", None)
        old_wavelength = int(getattr(cfg, "selected_laser_nm", 488)) if cfg is not None else 488
        self.sync_sim_camera_config_from_ui(save_to_disk=False)
        new_wavelength = int(getattr(cfg, "selected_laser_nm", old_wavelength)) if cfg is not None else old_wavelength
        wavelength_changed = new_wavelength != old_wavelength

        if wavelength_changed:
            # 换波长 = 换激光线，必须先关找样品激光；再按新波长重过滤 immediate 下拉。
            stop_immediate = getattr(self, "stop_immediate_live_mode", None)
            if callable(stop_immediate):
                stop_immediate()
            refresh_dropdown = getattr(self, "refresh_immediate_ro_dropdown", None)
            if callable(refresh_dropdown):
                refresh_dropdown()

        # immediate-live 期间仅改 ROI/曝光：保住 immediate RO，绝不重选正式 RO（否则画面又黑）。
        immediate_live = bool(getattr(self, "sim_immediate_live_active", False)) and not wavelength_changed

        if getattr(self, "sim_slm_connected", False) and not immediate_live:
            try:
                self.select_current_sim_running_order(save_to_disk=False)
            except Exception as e:
                print(f"SIM SLM running order refresh failed: {str(e)}")
                self.sim_app_config.selected_running_order = ""
                self.refresh_sim_settings_summary()
        if not self.sim_camera_connected:
            return
        live_requested = bool(getattr(self, "sim_preview_requested", False) or self.sim_preview_active)
        if live_requested:
            if immediate_live:
                # planned restart：保住激光，restart 不当作异常停止；等新 preview_started 重新
                # confirm（reconfirm pending + 看门狗，迟迟不回则关激光）。
                self.sim_preview_planned_restart = True
                begin_pending = getattr(self, "_begin_immediate_pending", None)
                if callable(begin_pending):
                    active_index = getattr(self, "sim_immediate_active_ro_index", None)
                    begin_pending("reconfirm", active_index if active_index is not None else -1, new_wavelength)
            self.sim_preview_restart_timer.start(150)
            return
        try:
            MainWindow.apply_connected_sim_camera_config(self)
        except Exception as e:
            print(f"SIM camera config refresh failed: {str(e)}")
            qw.QMessageBox.warning(self, "SIM Camera", str(e))

    def on_sim_camera_size_activated(self, *_):
        presets = MainWindow.current_sim_camera_size_presets(self)
        sensor_size = MainWindow.current_sim_camera_sensor_size(self)
        step_px = MainWindow.current_sim_camera_roi_step_px(self)
        roi_width, roi_height = size_from_sim_camera_label(
            self.ui.cmb_sCMOS_imageSize.currentText(),
            presets=presets,
        )
        roi_x, roi_y = centered_sim_camera_roi_origin(
            roi_width,
            roi_height,
            sensor_size=sensor_size,
            step_px=step_px,
            presets=presets,
        )
        roi_width, roi_height, roi_x, roi_y = normalize_sim_camera_roi(
            roi_width,
            roi_height,
            roi_x,
            roi_y,
            sensor_size=sensor_size,
            step_px=step_px,
            presets=presets,
        )
        camera = self.sim_app_config.camera
        if (
            roi_width == int(camera.roi_width)
            and roi_height == int(camera.roi_height)
            and roi_x == int(camera.roi_x)
            and roi_y == int(camera.roi_y)
        ):
            return
        camera.roi_width = roi_width
        camera.roi_height = roi_height
        camera.roi_x = roi_x
        camera.roi_y = roi_y
        self.sync_sim_camera_roi_position_controls()
        self.on_sim_camera_setting_changed()

    def ensure_sim_runtime(self):
        backend = self.sim_app_config.backend
        signature = (
            backend.fusion_bt_sdk_path,
            backend.slm_sdk_path,
            backend.simulation_mode,
        )
        if self.sim_acquisition_controller is not None and signature == self.sim_runtime_backend_signature:
            self.sim_acquisition_controller.z_scan_config = self.sim_app_config.z_scan
            self.sim_acquisition_controller.reconstruction_config = self.sim_app_config.reconstruction
            self.connect_sim_reconstruction_worker_to_controller()
            return
        # 重建 controller 前先关旧 controller 的找样品激光，并清陈旧 immediate 缓存/确认态
        # （新 controller 的 daq/slm/scan 缓存都是空的）。
        stop_immediate = getattr(self, "stop_immediate_live_mode", None)
        if callable(stop_immediate):
            stop_immediate()
        self.sim_preview_started_confirmed = False
        self.sim_immediate_ro_items = []
        reset_dropdown = getattr(self, "_reset_immediate_ro_dropdown", None)
        if callable(reset_dropdown):
            reset_dropdown()
        preview_poll_timer = getattr(self, "sim_preview_poll_timer", None)
        if preview_poll_timer is not None:
            preview_poll_timer.stop()
        self.sim_last_preview_sequence = -1
        if self.sim_preview_controller is not None:
            self.sim_preview_controller.shutdown()
            self.sim_preview_controller = None
        if self.sim_acquisition_controller is not None:
            self.sim_acquisition_controller.shutdown()
            self.sim_acquisition_controller = None
        self.sim_slm_connected = False
        self.sim_available_slms = []
        self.sim_app_config.selected_running_order = ""
        self.sim_acquisition_controller = SimAcquisitionController(backend=backend, parent=self)
        self.sim_acquisition_controller.z_scan_config = self.sim_app_config.z_scan
        self.sim_acquisition_controller.reconstruction_config = self.sim_app_config.reconstruction
        self.sim_acquisition_controller.signal_status_changed.connect(self.slot_handle_sim_acquisition_status)
        self.sim_acquisition_controller.signal_z_scan_progress.connect(self.slot_handle_sim_z_scan_progress)
        self.sim_acquisition_controller.signal_z_scan_complete.connect(self.slot_handle_sim_z_scan_complete)
        self.sim_acquisition_controller.signal_acquisition_summary_ready.connect(self.slot_handle_sim_acquisition_ready)
        self.connect_sim_reconstruction_worker_to_controller()
        self.sim_acquisition_controller.signal_acquisition_failed.connect(self.slot_handle_sim_acquisition_failed)
        self.sim_acquisition_controller.signal_acquisition_cancelled.connect(self.slot_handle_sim_acquisition_cancelled)
        self.sim_camera_adapter = self.sim_acquisition_controller.camera_adapter
        self.sim_preview_controller = SimPreviewController(self.sim_camera_adapter, self)
        self.sim_preview_controller.signal_error.connect(self.slot_handle_sim_preview_error)
        self.sim_preview_controller.signal_status_changed.connect(self.slot_handle_sim_preview_status)
        self.sim_runtime_backend_signature = signature
        self.sim_preview_backend_signature = signature
        if hasattr(self, "sim_stage_position_timer") and not self.sim_stage_position_timer.isActive():
            self.sim_stage_position_timer.start()

    def detect_real_sim_hardware(self, probe_camera=True):
        backend = self.sim_app_config.backend
        camera_devices = []
        slm_devices = []
        daq_devices = []

        if probe_camera:
            try:
                camera_devices = create_camera_adapter_for_backend(backend).list_devices()
            except Exception as e:
                print(f"SIM real camera probe failed: {str(e)}")

        try:
            self.ensure_sim_runtime()
            if getattr(self, "sim_slm_connected", False):
                slm_devices = [dict(self.sim_acquisition_controller.slm_connection_info())]
            else:
                slm_devices = self.sim_acquisition_controller.refresh_available_slm_devices()
        except Exception as e:
            print(f"SIM real SLM probe failed: {str(e)}")

        try:
            daq_devices = create_daq_adapter_for_backend(backend).list_devices(default_device=self.sim_app_config.daq.device_name)
        except Exception as e:
            print(f"SIM real DAQ probe failed: {str(e)}")

        return camera_devices, slm_devices, daq_devices

    def prefer_real_sim_hardware(self, save_to_disk=True, probe_camera=True):
        camera_devices, slm_devices, daq_devices = self.detect_real_sim_hardware(
            probe_camera=probe_camera,
        )
        updated_config = apply_real_hardware_preference(
            self.sim_app_config,
            camera_devices=camera_devices,
            slm_devices=slm_devices,
            daq_devices=daq_devices,
        )
        if app_config_to_dict(updated_config) != app_config_to_dict(self.sim_app_config):
            self.sim_app_config = updated_config
            if save_to_disk:
                save_app_config(self.sim_app_config, self.sim_app_config.config_path)
        else:
            self.sim_app_config = updated_config
        return camera_devices, slm_devices, daq_devices

    def ensure_sim_preview_controller(self):
        self.ensure_sim_runtime()

    def set_sim_camera_controls_enabled(self, enabled):
        self.ui.spb_sCMOS_exposureTime.setEnabled(enabled)
        if hasattr(self.ui, "cmb_sCMOS_bitDepth"):
            self.ui.cmb_sCMOS_bitDepth.setEnabled(enabled)
        self.ui.cmb_sCMOS_imageSize.setEnabled(enabled)
        self.sync_sim_camera_roi_position_controls(controls_enabled=enabled)

    def setup_sim_runtime_status_widgets(self):
        runtime_devices = ("camera", "slm", "daq", "reconstruction")
        # LED 与状态标签已由 CellSorting_ui 在 grp_simRuntime 内静态定义；此处仅复用并初始化为未连接。
        existing_leds = {
            "camera": getattr(self.ui, "led_simRuntimeCamera", None),
            "slm": getattr(self.ui, "led_simRuntimeSlm", None),
            "daq": getattr(self.ui, "led_simRuntimeDaq", None),
            "reconstruction": getattr(self.ui, "led_simRuntimeReconstruction", None),
        }
        existing_labels = {
            "camera": getattr(self.ui, "lbl_simRuntimeCameraStatus", None),
            "slm": getattr(self.ui, "lbl_simRuntimeSlmStatus", None),
            "daq": getattr(self.ui, "lbl_simRuntimeDaqStatus", None),
            "reconstruction": getattr(self.ui, "lbl_simRuntimeReconstructionStatus", None),
        }
        self.sim_runtime_leds = {k: v for k, v in existing_leds.items() if v is not None}
        self.sim_runtime_status_labels = {k: v for k, v in existing_labels.items() if v is not None}
        for device in runtime_devices:
            if existing_leds.get(device) is not None:
                self.set_sim_runtime_led_state(existing_leds[device], "gray")
            if existing_labels.get(device) is not None:
                existing_labels[device].setText("Not initialized")

    def setup_sim_z_position_widgets(self):
        """lbl_z_position_label / lbl_z_position_value 已由 CellSorting_ui 在
        grp_realTimeLiveView_2 内静态定义（绝对坐标 122/142,486、与 FPS 显示同行）；无需运行期
        创建，文本由 poll_sim_stage_position 更新。保留空方法以兼容既有调用与测试委托。"""
        return

    def poll_sim_stage_position(self, force=False):
        label = getattr(self.ui, "lbl_z_position_value", None)
        if label is None:
            return
        # 采集期间 / 手动 Z-Scan 运行期间跳过定时器轮询，避免 GUI 线程与后台 worker 并发访问
        # Ti2 SDK；运行结束等明确时机用 force=True 主动刷新（adapter 内已有锁串行化）。
        if (
            getattr(self, "sim_acquisition_in_progress", False)
            or getattr(self, "_zscan_move_in_progress", False)
        ) and not force:
            return
        controller = getattr(self, "sim_acquisition_controller", None)
        if controller is None:
            label.setText("-- um")
            return
        try:
            z_um = float(controller.get_stage_position_um())
            label.setText(f"{z_um:.2f} um")
        except Exception:
            logger.debug("SIM stage position poll failed.", exc_info=True)
            label.setText("-- um")

    # ---- Z-Scan 模块（主 GUI 一级界面，位于 SLM 与 SIM Runtime 之间） ----
    def setup_sim_zscan_module(self):
        """为静态定义的主 GUI Z-Scan 模块填充下拉项、补列伸缩并接线。

        控件（``*_main_zscan_*`` 前缀）已由 CellSorting_ui 在 SLM 组与 SIM Runtime 组之间
        静态定义：方向下拉、步进(nm)、步数、曝光预设下拉、是否开启 Z-Scan 开关、选择焦面开关、
        测试按钮、状态标签。本方法仅运行时填充带 itemData 的下拉项、补 .ui 无法表达的列伸缩、
        从配置初始化并接信号。测试按钮：选择焦面 OFF → 仅移动位移台；ON → 完整 Z-Scan 自动对焦。
        是否开启开关写入 ``z_scan.enabled``，决定主 GUI「SIM9帧采集」是否先做对焦再采 9 帧。
        """
        group = getattr(self.ui, "grp_zscan", None)
        if group is None:
            return
        if getattr(self, "_zscan_module_wired", False):
            return

        # Z-Scan 后台运行状态（一次性 worker；保留强引用防 GC）。
        self._zscan_move_thread = None
        self._zscan_move_worker = None
        self._zscan_move_stop_event = None
        self._zscan_move_in_progress = False

        # 列伸缩：col0-3 不拉伸、col4 吸收余量，使窄控件左对齐、不被拉伸。
        # （.ui 的 columnStretch 属性不被 pyuic5 生成，故运行时补。）
        grid = getattr(self.ui, "gridLayout_zscan", None)
        if grid is not None:
            for _col in range(4):
                grid.setColumnStretch(_col, 0)
            grid.setColumnStretch(4, 1)

        # 填充下拉项（带 itemData，无法在 .ui 静态表达）；blockSignals+clear 防重复填充。
        self.ui.cmb_main_zscan_direction.blockSignals(True)
        self.ui.cmb_main_zscan_direction.clear()
        self.ui.cmb_main_zscan_direction.addItem("+Z", "positive_z")
        self.ui.cmb_main_zscan_direction.addItem("-Z", "negative_z")
        self.ui.cmb_main_zscan_direction.blockSignals(False)
        self.ui.cmb_main_zscan_exposure.blockSignals(True)
        self.ui.cmb_main_zscan_exposure.clear()
        for preset_ms in Z_SCAN_EXPOSURE_PRESETS_MS:
            self.ui.cmb_main_zscan_exposure.addItem(str(int(preset_ms)), int(preset_ms))
        self.ui.cmb_main_zscan_exposure.blockSignals(False)

        self._init_zscan_module_from_config()
        self.ui.cmb_main_zscan_direction.currentIndexChanged.connect(self.on_main_zscan_setting_changed)
        self.ui.spb_main_zscan_step_nm.valueChanged.connect(self.on_main_zscan_setting_changed)
        self.ui.spb_main_zscan_num_steps.valueChanged.connect(self.on_main_zscan_setting_changed)
        self.ui.cmb_main_zscan_exposure.currentIndexChanged.connect(self.on_main_zscan_setting_changed)
        self.ui.chk_main_zscan_enabled.toggled.connect(self.on_main_zscan_setting_changed)
        self.ui.btn_main_zscan_run.clicked.connect(lambda _checked=False: self.on_main_zscan_run_clicked())
        self.signal_zscan_status.connect(self._on_zscan_status)
        # 接线全部完成后才置位，防首次中途失败留半接线态。
        self._zscan_module_wired = True

    def _init_zscan_module_from_config(self):
        """从 ``self.sim_app_config.z_scan`` 填充 Z-Scan 模块控件（blockSignals 防回环）。

        start_um / focus_metric 主 GUI 不暴露，保留 config 原值（start_um=None 即以运行时
        当前 Z 为起点）；选择焦面开关为 UI-only、不入 config、不在此重置。
        """
        if not hasattr(self.ui, "chk_main_zscan_enabled"):
            return
        cfg = self.sim_app_config.z_scan
        widgets = (
            self.ui.cmb_main_zscan_direction,
            self.ui.spb_main_zscan_step_nm,
            self.ui.spb_main_zscan_num_steps,
            self.ui.cmb_main_zscan_exposure,
            self.ui.chk_main_zscan_enabled,
        )
        for widget in widgets:
            widget.blockSignals(True)
        try:
            dir_index = self.ui.cmb_main_zscan_direction.findData(str(cfg.direction))
            self.ui.cmb_main_zscan_direction.setCurrentIndex(dir_index if dir_index >= 0 else 0)
            self.ui.spb_main_zscan_step_nm.setValue(float(cfg.step_um) * 1000.0)
            self.ui.spb_main_zscan_num_steps.setValue(int(cfg.num_steps))
            exp_index = self.ui.cmb_main_zscan_exposure.findData(int(cfg.exposure_preset_ms))
            self.ui.cmb_main_zscan_exposure.setCurrentIndex(exp_index if exp_index >= 0 else 0)
            self.ui.chk_main_zscan_enabled.setChecked(bool(cfg.enabled))
        finally:
            for widget in widgets:
                widget.blockSignals(False)

    def on_main_zscan_setting_changed(self, *_):
        """Z-Scan 模块控件改值 → 写回 z_scan 配置 + 落盘 + 同步 controller + 刷新摘要。"""
        if getattr(self, "_loading_configure_settings", False):
            return
        if not hasattr(self.ui, "chk_main_zscan_enabled"):
            return
        cfg = self.sim_app_config.z_scan
        direction = self.ui.cmb_main_zscan_direction.currentData()
        cfg.direction = str(direction) if direction in ("positive_z", "negative_z") else "positive_z"
        cfg.step_um = float(self.ui.spb_main_zscan_step_nm.value()) / 1000.0
        cfg.num_steps = int(self.ui.spb_main_zscan_num_steps.value())
        exposure = self.ui.cmb_main_zscan_exposure.currentData()
        if exposure is not None:
            cfg.exposure_preset_ms = int(exposure)
        cfg.enabled = bool(self.ui.chk_main_zscan_enabled.isChecked())
        try:
            save_app_config(self.sim_app_config, self.sim_app_config.config_path)
        except Exception as exc:
            print(f"SIM z-scan config save failed: {exc}")
        controller = getattr(self, "sim_acquisition_controller", None)
        if controller is not None:
            controller.z_scan_config = self.sim_app_config.z_scan
        refresh_summary = getattr(self, "refresh_sim_settings_summary", None)
        if callable(refresh_summary):
            refresh_summary()

    def _set_zscan_inputs_enabled(self, enabled):
        """启用/禁用 Z-Scan 输入控件（运行按钮不在内，运行期作取消按钮保持可点）。"""
        for name in (
            "cmb_main_zscan_direction",
            "spb_main_zscan_step_nm",
            "spb_main_zscan_num_steps",
            "cmb_main_zscan_exposure",
            "chk_main_zscan_enabled",
            "chk_main_zscan_capture",
        ):
            widget = getattr(self.ui, name, None)
            if widget is not None:
                widget.setEnabled(bool(enabled))

    def _run_zscan_blocking(
        self, capture, stage_adapter, z_scan_config, stop_event, on_status,
        daq_config=None, camera_config=None, timing=None,
    ):
        """后台线程内执行 Z-Scan：选择焦面 ON → 完整自动对焦；OFF → 仅移动位移台。

        无 Qt 依赖（不碰控件），便于单元测试直接调用、验证分派与入参；越界预检 / 相机/SLM
        前置检查 / 移到最佳焦面均在 z_scan_core 的两个函数内完成。daq/camera/timing 由调用方
        在 GUI 线程预快照传入（避免运行中被改的竞态）；未传时回退实时配置（供单测直接调用）。
        """
        if capture:
            controller = self.sim_acquisition_controller
            return run_z_scan_autofocus(
                stage_adapter=stage_adapter,
                camera_adapter=controller.camera_adapter,
                slm_adapter=controller.slm_adapter,
                daq_adapter=controller.daq_adapter,
                daq_config=daq_config if daq_config is not None else self.sim_app_config.daq,
                camera_config=camera_config if camera_config is not None else self.sim_app_config.camera,
                timing=timing if timing is not None else self.sim_app_config.timing,
                z_scan_config=z_scan_config,
                stop_event=stop_event,
                on_status=on_status,
                keep_captured_stack=False,
            )
        return run_z_scan_stage_only(
            stage_adapter=stage_adapter,
            z_scan_config=z_scan_config,
            stop_event=stop_event,
            on_status=on_status,
        )

    def on_main_zscan_run_clicked(self):
        """测试Z-Scan：选择焦面 OFF 仅移动位移台、ON 执行完整自动对焦；运行中再点为取消。"""
        # 1) 取消分支：已有 worker 在跑 → 置位 stop_event 请求取消（已发出的单次移动不可中断）。
        if getattr(self, "_zscan_move_thread", None) is not None:
            stop_event = getattr(self, "_zscan_move_stop_event", None)
            if stop_event is not None:
                stop_event.set()
            self.ui.btn_main_zscan_run.setEnabled(False)
            self.ui.lbl_main_zscan_status.setText("Cancelling Z-Scan...")
            return
        # 2) 与 SIM9 采集互斥。
        if getattr(self, "sim_acquisition_in_progress", False):
            qw.QMessageBox.information(self, "Z-Scan", "SIM9 acquisition is running. Please test Z-Scan later.")
            return
        # 3) 关找样品激光（仅移动也不需要激光；与采集前清光一致）。
        stop_immediate = getattr(self, "stop_immediate_live_mode", None)
        if callable(stop_immediate):
            stop_immediate(reset_dropdown=False)
        # 4) 就绪 controller / stage。
        self.ensure_sim_runtime()
        controller = getattr(self, "sim_acquisition_controller", None)
        stage_adapter = getattr(controller, "stage_adapter", None) if controller is not None else None
        if controller is None or stage_adapter is None:
            qw.QMessageBox.warning(self, "Z-Scan", "Z stage is unavailable. Cannot test Z-Scan.")
            return
        capture = bool(
            getattr(self.ui, "chk_main_zscan_capture", None) is not None
            and self.ui.chk_main_zscan_capture.isChecked()
        )
        # 5) 选择焦面模式需要相机/SLM：先停预览让出相机、要求 SLM/相机已连接、下发相机配置。
        if capture:
            if self.sim_preview_active or self.sim_preview_stop_in_progress:
                if not self.stop_sim_preview(wait=True):
                    qw.QMessageBox.warning(self, "Z-Scan", "SIM preview is still stopping. Please try again later.")
                    return
            if not getattr(self, "sim_slm_connected", False):
                qw.QMessageBox.information(self, "Z-Scan", "Select Focus Plane mode requires SLM connection first.")
                return
            if not getattr(self, "sim_camera_connected", False):
                qw.QMessageBox.information(
                    self,
                    "Z-Scan",
                    "Select Focus Plane mode requires the camera to be connected (start preview once first).",
                )
                return
            apply_cam = getattr(self, "apply_connected_sim_camera_config", None)
            if callable(apply_cam):
                try:
                    apply_cam()
                except Exception as exc:
                    print(f"Z-Scan camera config apply failed: {exc}")
        # 6) 连接 stage（仿真即时；真实 Ti2 同步阻塞，adapter 内有锁）。
        try:
            if not getattr(stage_adapter, "is_connected", False):
                controller.connect_stage()
        except Exception as exc:
            qw.QMessageBox.warning(self, "Z-Scan", f"Failed to connect Z stage: {exc}")
            return
        # 7) 在 GUI 线程预快照配置（避免运行中被改的竞态）+ 取消事件 + 进度回调（强制经信号回主线程）。
        cfg = copy.copy(self.sim_app_config.z_scan)
        daq_snap = copy.copy(self.sim_app_config.daq) if capture else None
        camera_snap = copy.copy(self.sim_app_config.camera) if capture else None
        timing_snap = copy.copy(self.sim_app_config.timing) if capture else None
        stop_event = Event()

        def _emit_status(event, payload):
            self.signal_zscan_status.emit(str(event), dict(payload or {}))

        def _fn():
            # 核心阻塞调用抽到 _run_zscan_blocking（无 Qt 依赖，便于单测直接验证分派与入参）。
            return self._run_zscan_blocking(
                capture, stage_adapter, cfg, stop_event, _emit_status,
                daq_config=daq_snap, camera_config=camera_snap, timing=timing_snap,
            )

        # 8) 进入运行态：按钮变取消、禁用输入、状态提示。
        self._zscan_move_in_progress = True
        self.ui.btn_main_zscan_run.setText("Cancel")
        self._set_zscan_inputs_enabled(False)
        self.ui.lbl_main_zscan_status.setText("Z-Scan running...")

        # 9) 起后台线程（仿 SLM 连接范式；强引用防 GC）。
        thread = QThread()
        worker = _ConnectWorker(_fn)
        worker.moveToThread(thread)
        self._zscan_move_thread = thread
        self._zscan_move_worker = worker
        self._zscan_move_stop_event = stop_event
        worker.signal_success.connect(self._on_zscan_move_success)
        worker.signal_error.connect(self._on_zscan_move_error)
        worker.signal_finished.connect(self._on_zscan_move_finished)
        thread.started.connect(worker.run)
        thread.start()

    def _on_zscan_status(self, event, payload):
        """worker 进度事件（已在主线程）：更新 Z-Scan 状态标签。"""
        label = getattr(self.ui, "lbl_main_zscan_status", None)
        if label is None:
            return
        payload = payload or {}
        if event == "z_scan_stage_positioned":
            try:
                label.setText(
                    f"Moving {int(payload['step_index'])}/{int(payload['total_steps'])}"
                    f"  z={float(payload['z_um']):.2f} um"
                )
            except (KeyError, TypeError, ValueError):
                label.setText("Z-Scan moving...")
        elif event == "z_scan_progress":
            try:
                label.setText(f"Focus capture {int(payload['step_index'])}/{int(payload['total_steps'])}...")
            except (KeyError, TypeError, ValueError):
                label.setText("Z-Scan focusing...")

    def _on_zscan_move_success(self, result):
        label = getattr(self.ui, "lbl_main_zscan_status", None)
        if label is None:
            return
        best_z = getattr(result, "best_z_um", None)
        if best_z is not None:
            label.setText(f"Z-Scan complete: best focus z={float(best_z):.2f} um")
            return
        positions = getattr(result, "positions_visited", None)
        count = len(positions) if positions is not None else 0
        label.setText(f"Z-Scan complete: moved {count} positions.")

    def _on_zscan_move_error(self, message):
        label = getattr(self.ui, "lbl_main_zscan_status", None)
        if label is not None:
            label.setText(f"Z-Scan ended: {message}")

    def _on_zscan_move_finished(self):
        thread = getattr(self, "_zscan_move_thread", None)
        if thread is not None:
            thread.quit()
            thread.wait()
        self._zscan_move_thread = None
        self._zscan_move_worker = None
        self._zscan_move_stop_event = None
        self._zscan_move_in_progress = False
        if hasattr(self.ui, "btn_main_zscan_run"):
            self.ui.btn_main_zscan_run.setText("Test Z-Scan")
            self.ui.btn_main_zscan_run.setEnabled(True)
        self._set_zscan_inputs_enabled(True)
        self.poll_sim_stage_position(force=True)

    # ===================== DAQ 模块（主 GUI 一级 DAQ 接线 + 诊断测试） =====================
    def setup_sim_daq_module(self):
        """为静态定义的主 GUI DAQ 模块建角色映射、补列伸缩、从配置初始化并接线。

        控件（``*_main_daq_*`` 前缀）已由 CellSorting_ui 在 Z-Scan 组与 Recon 组之间静态定义：
        设备下拉 + Refresh、8 路 TTL 线位下拉（2 列）、测试目标下拉 + Pulse Test 按钮、状态标签。
        设备/线位的真实枚举走 Refresh（按需 ensure_sim_runtime 用 controller 共享 daq_adapter）；
        诊断测试在后台 ``_PulseTestWorker`` 执行、可取消，启动前做 immediate-live/preview 安全互锁。
        """
        group = getattr(self.ui, "grp_daq", None)
        if group is None:
            return
        if getattr(self, "_daq_module_wired", False):
            return
        # DAQ 测试后台运行状态（一次性 worker；保留强引用防 GC）。
        self._daq_test_thread = None
        self._daq_test_worker = None
        self._daq_test_stop_event = None
        self._daq_test_resume_preview = False
        self._daq_test_btn_text = "Pulse Test"
        # 角色 -> 线位下拉 映射（供 read_daq_config_from_line_combos / populate_daq_line_combos 复用）。
        self.main_daq_line_combos = {
            "slm_enable_line": self.ui.cmb_main_daq_slm_enable,
            "slm_trigger_line": self.ui.cmb_main_daq_slm_trigger,
            "slm_finish_line": self.ui.cmb_main_daq_slm_finish,
            "camera_trigger_line": self.ui.cmb_main_daq_camera_trigger,
            "laser_405_line": self.ui.cmb_main_daq_laser_405,
            "laser_488_line": self.ui.cmb_main_daq_laser_488,
            "laser_561_line": self.ui.cmb_main_daq_laser_561,
            "laser_638_line": self.ui.cmb_main_daq_laser_638,
        }
        # 列伸缩：label-on-top 2 列布局——两列（左 SLM/Cam-Trig 通道、右 Laser 通道）等分余量、
        # 宽度一致；Device/Test 行用跨 2 列的嵌套 HBox（其内 combo 设 Expanding 吃余量）。
        # .ui columnStretch 不被 pyuic5 生成，运行时补。
        grid = getattr(self.ui, "gridLayout_daq", None)
        if grid is not None:
            grid.setColumnStretch(0, 1)
            grid.setColumnStretch(1, 1)
        self._init_daq_module_from_config()
        self.ui.btn_main_daq_refresh.clicked.connect(lambda _checked=False: self._refresh_main_daq_devices())
        self.ui.cmb_main_daq_device.currentTextChanged.connect(lambda _text=None: self._refresh_main_daq_lines())
        for combo in self.main_daq_line_combos.values():
            combo.currentTextChanged.connect(self.on_main_daq_setting_changed)
        self.ui.btn_main_daq_test.clicked.connect(lambda _checked=False: self.on_main_daq_test_clicked())
        self._daq_module_wired = True

    def _init_daq_module_from_config(self):
        """从 ``self.sim_app_config.daq`` 填充 DAQ 控件（blockSignals 防回环）。

        启动不查硬件：每个线位下拉只放配置当前线名（单项占位）、设备下拉放配置设备名，
        测试目标下拉按当前配置构建；用户点 Refresh 才枚举真实设备/线位。
        """
        if not hasattr(self.ui, "cmb_main_daq_device"):
            return
        daq = self.sim_app_config.daq
        device = self.ui.cmb_main_daq_device
        device.blockSignals(True)
        try:
            device.clear()
            if str(daq.device_name):
                device.addItem(str(daq.device_name))
                device.setCurrentText(str(daq.device_name))
        finally:
            device.blockSignals(False)
        for role, combo in self.main_daq_line_combos.items():
            line_name = str(getattr(daq, role, "") or "")
            combo.blockSignals(True)
            try:
                combo.clear()
                if line_name:
                    combo.addItem(line_name)
                    combo.setCurrentText(line_name)
            finally:
                combo.blockSignals(False)
        self._refresh_main_daq_test_targets()

    def _refresh_main_daq_test_targets(self):
        """按当前 DAQ 配置重建测试目标下拉（保留当前选中项，按 itemData 比较）。"""
        combo = getattr(self.ui, "cmb_main_daq_test_target", None)
        if combo is None:
            return
        selected = combo.currentData()
        combo.blockSignals(True)
        try:
            combo.clear()
            for target_id, label in build_daq_test_target_items(self.sim_app_config.daq):
                combo.addItem(label, target_id)
            index = combo.findData(selected)
            if index < 0 and combo.count():
                index = 0
            if index >= 0:
                combo.setCurrentIndex(index)
        finally:
            combo.blockSignals(False)
        if hasattr(self.ui, "btn_main_daq_test"):
            self.ui.btn_main_daq_test.setEnabled(
                combo.count() > 0 and getattr(self, "_daq_test_thread", None) is None
            )

    def _shared_main_daq_adapter(self):
        """返回与 controller 共享的 daq_adapter（先 ensure_sim_runtime）；不可用返回 None。"""
        self.ensure_sim_runtime()
        controller = getattr(self, "sim_acquisition_controller", None)
        return getattr(controller, "daq_adapter", None) if controller is not None else None

    def _refresh_main_daq_devices(self):
        """枚举 DAQ 设备并刷新设备下拉，随后刷新线位（点击 Refresh 时调用）。"""
        adapter = self._shared_main_daq_adapter()
        if adapter is None:
            return
        current = self.ui.cmb_main_daq_device.currentText().strip() or str(self.sim_app_config.daq.device_name)
        try:
            devices = adapter.list_devices(default_device=current or "Dev1")
        except Exception as exc:
            self.ui.lbl_main_daq_status.setText(f"DAQ device query failed: {exc}")
            return
        self.ui.cmb_main_daq_device.blockSignals(True)
        self.ui.cmb_main_daq_device.clear()
        self.ui.cmb_main_daq_device.addItems(devices)
        selected = current if current in devices else (devices[0] if devices else "")
        if selected:
            self.ui.cmb_main_daq_device.setCurrentText(selected)
        self.ui.cmb_main_daq_device.blockSignals(False)
        if not devices:
            self.ui.lbl_main_daq_status.setText("No NI DAQ devices detected.")
            return
        self._refresh_main_daq_lines()

    def _refresh_main_daq_lines(self):
        """按当前设备枚举 port0 线位灌入 8 个角色下拉（复用 populate_daq_line_combos）。"""
        adapter = self._shared_main_daq_adapter()
        if adapter is None:
            return
        selected_device = self.ui.cmb_main_daq_device.currentText().strip() or str(self.sim_app_config.daq.device_name)
        try:
            lines = adapter.list_port0_lines(device_name=selected_device, default_device=selected_device)
        except Exception as exc:
            self.ui.lbl_main_daq_status.setText(f"DAQ line query failed: {exc}")
            return
        if not selected_device or not lines:
            if selected_device:
                self.ui.lbl_main_daq_status.setText(f"No DAQ lines available for {selected_device}.")
            return
        current_values = {role: combo.currentText() for role, combo in self.main_daq_line_combos.items()}
        populate_daq_line_combos(
            self.main_daq_line_combos, lines, current_values, self.sim_app_config.daq, selected_device
        )
        self.on_main_daq_setting_changed()

    def _persist_main_daq_config_from_ui(self):
        """把 DAQ 模块 8 个线位下拉读回 ``sim_app_config.daq``；任一项缺失则保持原配置。"""
        if not getattr(self, "_daq_module_wired", False):
            return
        try:
            self.sim_app_config.daq = read_daq_config_from_line_combos(
                self.main_daq_line_combos,
                device_name=self.ui.cmb_main_daq_device.currentText(),
            )
        except ValueError:
            # 控件尚未枚举（仅占位单项）或为空时不覆盖既有配置。
            return

    def on_main_daq_setting_changed(self, *_):
        """DAQ 线位/设备改值 → 写回 daq 配置 + 落盘 + 同步 controller + 重建测试项 + 刷新摘要。"""
        if getattr(self, "_loading_configure_settings", False):
            return
        if not getattr(self, "_daq_module_wired", False):
            return
        try:
            daq_config = read_daq_config_from_line_combos(
                self.main_daq_line_combos,
                device_name=self.ui.cmb_main_daq_device.currentText(),
            )
        except ValueError:
            self._refresh_main_daq_test_targets()
            return
        self.sim_app_config.daq = daq_config
        try:
            save_app_config(self.sim_app_config, self.sim_app_config.config_path)
        except Exception as exc:
            print(f"SIM daq config save failed: {exc}")
        controller = getattr(self, "sim_acquisition_controller", None)
        if controller is not None:
            try:
                controller.apply_daq_config(self.sim_app_config.daq)
            except Exception as exc:
                print(f"SIM apply_daq_config failed: {exc}")
        self._refresh_main_daq_test_targets()
        refresh_summary = getattr(self, "refresh_sim_settings_summary", None)
        if callable(refresh_summary):
            refresh_summary()

    def _set_main_daq_inputs_enabled(self, enabled):
        """启用/禁用 DAQ 模块输入控件（测试按钮除外，运行期作取消按钮保持可点）。"""
        for combo in self.main_daq_line_combos.values():
            combo.setEnabled(bool(enabled))
        for name in ("cmb_main_daq_device", "btn_main_daq_refresh", "cmb_main_daq_test_target"):
            widget = getattr(self.ui, name, None)
            if widget is not None:
                widget.setEnabled(bool(enabled))

    def on_main_daq_test_clicked(self):
        """运行 DAQ 测试：运行中再点为取消；否则按 B1 安全互锁后台执行选中测试。"""
        # 1) 取消分支：worker 在跑 → cancel（置位 stop_event）。
        if getattr(self, "_daq_test_thread", None) is not None:
            worker = getattr(self, "_daq_test_worker", None)
            if worker is not None:
                worker.cancel()
            self.ui.btn_main_daq_test.setEnabled(False)
            self.ui.lbl_main_daq_status.setText("Cancelling DAQ test...")
            return
        # 2) 与 SIM9 采集互斥。
        if getattr(self, "sim_acquisition_in_progress", False):
            qw.QMessageBox.information(self, "DAQ Test", "SIM9 acquisition is running. Please test DAQ later.")
            return
        target_id = self.ui.cmb_main_daq_test_target.currentData()
        if not target_id:
            self.ui.lbl_main_daq_status.setText("Please select a test target.")
            return
        # 3) 校验 DAQ 配置（任一线位缺失即拒绝，不启动 worker）。
        try:
            daq_config = read_daq_config_from_line_combos(
                self.main_daq_line_combos,
                device_name=self.ui.cmb_main_daq_device.currentText(),
            )
        except ValueError as exc:
            qw.QMessageBox.critical(self, "DAQ Test", str(exc))
            return
        # 4) B1 安全互锁：先关找样品激光（set_line 整 port 写与诊断波形互斥，遵 2026-06-24）。
        stop_immediate = getattr(self, "stop_immediate_live_mode", None)
        if callable(stop_immediate):
            stop_immediate(reset_dropdown=False)
        # 5) 就绪 controller + 共享 adapter。
        self.ensure_sim_runtime()
        controller = getattr(self, "sim_acquisition_controller", None)
        if controller is None:
            qw.QMessageBox.warning(self, "DAQ Test", "SIM runtime unavailable. Cannot run DAQ test.")
            return
        # 6) B1：暂停 live preview 让出相机/DAQ；记 resume 标志，测试结束后安全恢复。
        resume_preview = bool(
            self.sim_camera_connected
            and not self.sim_acquisition_in_progress
            and (getattr(self, "sim_preview_requested", False) or self.sim_preview_active)
        )
        if resume_preview or self.sim_preview_active or self.sim_preview_stop_in_progress:
            self.sim_preview_requested = False
            if not self.stop_sim_preview(wait=True):
                qw.QMessageBox.warning(self, "DAQ Test", "SIM preview is still stopping. Please try again later.")
                return
        self._daq_test_resume_preview = resume_preview
        # 7) GUI 线程快照波长 + immediate RO 索引 + 配置（worker 内不读 Qt；排除 immediate RO）。
        selected_laser_nm = int(self.sim_app_config.selected_laser_nm)
        immediate_indices = set(getattr(controller, "_immediate_ro_indices", None) or ())
        config_snapshot = app_config_from_dict(app_config_to_dict(self.sim_app_config))
        runner = DaqTestRunner(
            camera_adapter=controller.camera_adapter,
            slm_adapter=controller.slm_adapter,
            daq_adapter=controller.daq_adapter,
            config=config_snapshot,
            immediate_ro_indices=immediate_indices,
            camera_externally_owned=True,
            selected_laser_nm=selected_laser_nm,
        )
        acquisition_started_at_s = time.perf_counter()
        daq_config_snap = copy.copy(daq_config)

        def _fn(stop_event):
            return runner.run_test(
                str(target_id),
                daq_config_snap,
                selected_laser_nm=selected_laser_nm,
                stop_event=stop_event,
                acquisition_started_at_s=acquisition_started_at_s,
            )

        # 8) 进入运行态：按钮变 Cancel、禁用输入、状态提示。
        stop_event = Event()
        self._daq_test_btn_text = self.ui.btn_main_daq_test.text()
        self.ui.btn_main_daq_test.setText("Cancel")
        self._set_main_daq_inputs_enabled(False)
        self.ui.lbl_main_daq_status.setText("DAQ test running...")
        # 9) 后台线程（_PulseTestWorker；强引用防 GC）。
        thread = QThread()
        worker = _PulseTestWorker(_fn, stop_event)
        worker.moveToThread(thread)
        self._daq_test_thread = thread
        self._daq_test_worker = worker
        self._daq_test_stop_event = stop_event
        worker.signal_success.connect(self._on_main_daq_test_success)
        worker.signal_error.connect(self._on_main_daq_test_error)
        worker.signal_finished.connect(self._on_main_daq_test_finished)
        thread.started.connect(worker.run)
        thread.start()

    @pyqtSlot(str)
    def _on_main_daq_test_success(self, message):
        self.ui.lbl_main_daq_status.setText("DAQ test done.")
        qw.QMessageBox.information(self, "DAQ Test", message)

    @pyqtSlot(str)
    def _on_main_daq_test_error(self, message):
        self.ui.lbl_main_daq_status.setText(f"DAQ test failed: {message}")
        qw.QMessageBox.critical(self, "DAQ Test", message)

    @pyqtSlot()
    def _on_main_daq_test_finished(self):
        """测试结束（成功/失败/取消）：停线程、恢复按钮/输入、按需恢复 preview。"""
        thread = getattr(self, "_daq_test_thread", None)
        if thread is not None:
            thread.quit()
            thread.wait()
        self._daq_test_thread = None
        self._daq_test_worker = None
        self._daq_test_stop_event = None
        if hasattr(self.ui, "btn_main_daq_test"):
            self.ui.btn_main_daq_test.setText(getattr(self, "_daq_test_btn_text", "Pulse Test"))
            self.ui.btn_main_daq_test.setEnabled(True)
        self._set_main_daq_inputs_enabled(True)
        # B1：测试前若暂停了 live preview，结束后安全恢复。
        if getattr(self, "_daq_test_resume_preview", False):
            self._daq_test_resume_preview = False
            if self.sim_camera_connected and not self.sim_acquisition_in_progress:
                self.start_sim_preview()

    # ===================== Recon 模块（主 GUI 一级重建参数接线） =====================
    def setup_sim_recon_module(self):
        """为静态定义的主 GUI Recon 模块填充波长下拉、补列伸缩、从配置初始化并接线。

        控件（``*_main_recon_*`` 前缀）已由 CellSorting_ui 在 DAQ 组与 SIM Runtime 组之间静态定义：
        Wiener / Illumination NA / Pixel(nm) / Ex 波长下拉、OTF / Background / Output 路径 + Browse。
        波长下拉只切换"查看/编辑哪个波长的 OTF"，绝不改采集波长（唯一入口为 cmb_sCMOS_laser）。
        """
        group = getattr(self.ui, "grp_recon", None)
        if group is None:
            return
        if getattr(self, "_recon_module_wired", False):
            return
        # 列伸缩：col1=1 让 OTF/Background/Output 的 edit_* 跨 col1-2 拉伸吸收余量；
        # col3=0——na/wavelength 已限宽 90，不再被列拉伸（避免参数 spinbox 过宽）。
        # .ui columnStretch 不被 pyuic5 生成，运行时补。
        grid = getattr(self.ui, "gridLayout_recon", None)
        if grid is not None:
            grid.setColumnStretch(0, 0)
            grid.setColumnStretch(1, 1)
            grid.setColumnStretch(2, 0)
            grid.setColumnStretch(3, 0)
        # 波长下拉项（带 itemData，.ui 无法静态表达）。
        self.ui.cmb_main_recon_wavelength.blockSignals(True)
        self.ui.cmb_main_recon_wavelength.clear()
        for wl in SUPPORTED_LASERS:
            self.ui.cmb_main_recon_wavelength.addItem(f"{int(wl)} nm", int(wl))
        self.ui.cmb_main_recon_wavelength.blockSignals(False)
        self._main_recon_current_wavelength_nm = int(self.sim_app_config.selected_laser_nm)
        self._init_recon_module_from_config()
        self.ui.spb_main_recon_wiener.valueChanged.connect(self.on_main_recon_setting_changed)
        self.ui.spb_main_recon_na.valueChanged.connect(self.on_main_recon_setting_changed)
        self.ui.spb_main_recon_pixel_nm.valueChanged.connect(self.on_main_recon_setting_changed)
        self.ui.cmb_main_recon_wavelength.currentIndexChanged.connect(self._on_main_recon_wavelength_changed)
        self.ui.edit_main_recon_otf.editingFinished.connect(self.on_main_recon_setting_changed)
        self.ui.edit_main_recon_background.editingFinished.connect(self.on_main_recon_setting_changed)
        self.ui.edit_main_recon_output.editingFinished.connect(self.on_main_recon_setting_changed)
        self.ui.btn_main_recon_browse_otf.clicked.connect(lambda _c=False: self._browse_main_recon_otf())
        self.ui.btn_main_recon_browse_background.clicked.connect(lambda _c=False: self._browse_main_recon_background())
        self.ui.btn_main_recon_browse_output.clicked.connect(lambda _c=False: self._browse_main_recon_output())
        self._recon_module_wired = True

    def _init_recon_module_from_config(self):
        """从 ``self.sim_app_config.reconstruction`` 填充 Recon 控件（blockSignals 防回环）。"""
        if not hasattr(self.ui, "spb_main_recon_wiener"):
            return
        recon = self.sim_app_config.reconstruction
        wavelength_nm = int(self.sim_app_config.selected_laser_nm)
        self._main_recon_current_wavelength_nm = wavelength_nm
        widgets = (
            self.ui.spb_main_recon_wiener,
            self.ui.spb_main_recon_na,
            self.ui.spb_main_recon_pixel_nm,
            self.ui.cmb_main_recon_wavelength,
            self.ui.edit_main_recon_otf,
            self.ui.edit_main_recon_background,
            self.ui.edit_main_recon_output,
        )
        for w in widgets:
            w.blockSignals(True)
        try:
            self.ui.spb_main_recon_wiener.setValue(float(recon.wiener))
            self.ui.spb_main_recon_na.setValue(float(recon.excitation_na))
            self.ui.spb_main_recon_pixel_nm.setValue(float(recon.pixel_size_nm))
            wl_index = self.ui.cmb_main_recon_wavelength.findData(wavelength_nm)
            self.ui.cmb_main_recon_wavelength.setCurrentIndex(wl_index if wl_index >= 0 else 0)
            self.ui.edit_main_recon_otf.setText(recon.otf_path_for_wavelength(wavelength_nm))
            self.ui.edit_main_recon_background.setText(str(recon.background_path))
            self.ui.edit_main_recon_output.setText(str(recon.output_path))
        finally:
            for w in widgets:
                w.blockSignals(False)

    def _store_main_recon_otf_path(self):
        """把 OTF 输入框存到当前波长对应的 config 字段（``otf_<wl>_path``）。"""
        wavelength_nm = int(getattr(self, "_main_recon_current_wavelength_nm", self.sim_app_config.selected_laser_nm))
        setattr(
            self.sim_app_config.reconstruction,
            f"otf_{wavelength_nm}_path",
            self.ui.edit_main_recon_otf.text().strip(),
        )

    def _on_main_recon_wavelength_changed(self, *_):
        """Recon 波长下拉变化：先存当前波长 OTF，再加载新波长 OTF（不改采集波长、不落盘）。"""
        if getattr(self, "_loading_configure_settings", False):
            return
        self._store_main_recon_otf_path()
        data = self.ui.cmb_main_recon_wavelength.currentData()
        wavelength_nm = int(data) if data is not None else int(self.sim_app_config.selected_laser_nm)
        self._main_recon_current_wavelength_nm = wavelength_nm
        self.ui.edit_main_recon_otf.blockSignals(True)
        self.ui.edit_main_recon_otf.setText(
            self.sim_app_config.reconstruction.otf_path_for_wavelength(wavelength_nm)
        )
        self.ui.edit_main_recon_otf.blockSignals(False)

    def _persist_main_recon_config_from_ui(self):
        """把 Recon 控件读回 ``sim_app_config.reconstruction``（含按波长保存当前 OTF）。"""
        if not getattr(self, "_recon_module_wired", False):
            return
        self._store_main_recon_otf_path()
        recon = self.sim_app_config.reconstruction
        recon.wiener = float(self.ui.spb_main_recon_wiener.value())
        recon.excitation_na = float(self.ui.spb_main_recon_na.value())
        recon.pixel_size_nm = float(self.ui.spb_main_recon_pixel_nm.value())
        recon.background_path = self.ui.edit_main_recon_background.text().strip()
        recon.output_path = self.ui.edit_main_recon_output.text().strip() or DEFAULT_RECONSTRUCTION_OUTPUT_DIR

    def on_main_recon_setting_changed(self, *_):
        """Recon 控件改值 → 写回 reconstruction 配置 + 落盘 + B6 下发 worker + 刷新摘要。"""
        if getattr(self, "_loading_configure_settings", False):
            return
        if not getattr(self, "_recon_module_wired", False):
            return
        self._persist_main_recon_config_from_ui()
        try:
            save_app_config(self.sim_app_config, self.sim_app_config.config_path)
        except Exception as exc:
            print(f"SIM recon config save failed: {exc}")
        # B6：经常驻 recon worker 的 queued slot 下发配置快照（ensure_* 内部 emit；不裸 setter）。
        ensure_worker = getattr(self, "ensure_sim_reconstruction_worker", None)
        if callable(ensure_worker):
            ensure_worker()
        refresh_summary = getattr(self, "refresh_sim_settings_summary", None)
        if callable(refresh_summary):
            refresh_summary()

    def _browse_main_recon_file(self, edit, title):
        """Recon 路径文件选择器：选中后写回输入框并触发一次保存。"""
        current = edit.text().strip()
        start_dir = str(Path(current).parent if current else Path.cwd())
        path, _ = qw.QFileDialog.getOpenFileName(
            self, title, start_dir, "TIFF Files (*.tif *.tiff);;All Files (*.*)"
        )
        if path:
            edit.setText(path)
            self.on_main_recon_setting_changed()

    def _browse_main_recon_otf(self):
        self._browse_main_recon_file(self.ui.edit_main_recon_otf, "Select OTF File")

    def _browse_main_recon_background(self):
        self._browse_main_recon_file(self.ui.edit_main_recon_background, "Select Background File")

    def _browse_main_recon_output(self):
        current = self.ui.edit_main_recon_output.text().strip()
        start_dir = current if current else str(Path.cwd())
        path = qw.QFileDialog.getExistingDirectory(self, "Select Reconstruction Output Folder", start_dir)
        if path:
            self.ui.edit_main_recon_output.setText(path)
            self.on_main_recon_setting_changed()

    def set_sim_runtime_led_state(self, led, state):
        if hasattr(led, "set_state"):
            led.set_state(state)
            return
        colors = {
            "gray": "#8b949e",
            "yellow": "#f2b705",
            "green": "#2da44e",
            "red": "#cf222e",
        }
        led.setStyleSheet(f"background-color: {colors.get(state, colors['gray'])}; border-radius: 8px;")

    def update_sim_runtime_status_widgets(self, status, payload=None):
        payload = payload or {}
        leds = getattr(self, "sim_runtime_leds", {})
        labels = getattr(self, "sim_runtime_status_labels", {})

        def set_device(device, state, text):
            led = leds.get(device)
            label = labels.get(device)
            if led is not None:
                self.set_sim_runtime_led_state(led, state)
            if label is not None:
                label.setText(text)

        if status == "hardware_initializing":
            for device in ("camera", "slm", "daq"):
                set_device(device, "yellow", "Initializing")
        elif status in {"hardware_initialized", "camera_initialized", "camera_config_applied", "camera_connected"}:
            set_device("camera", "green", "Ready")
        elif status == "camera_disconnected":
            set_device("camera", "red", "Disconnected")
        elif status in {"slm_connected", "patterns_prepared", "running_order_selected"}:
            set_device("slm", "green", "Ready")
        elif status == "slm_disconnected":
            set_device("slm", "red", "Disconnected")
        elif status == "daq_config_applied":
            set_device("daq", "green", "Ready")
        elif status == "reconstruction_starting":
            set_device("reconstruction", "yellow", "Running")
        elif status == "reconstruction_complete":
            shape = payload.get("preview_shape", "")
            suffix = f" {shape}" if shape else ""
            set_device("reconstruction", "green", f"Ready{suffix}")
        elif status == "reconstruction_failed":
            set_device("reconstruction", "red", "Failed")
        elif status == "raw_stack_saving":
            set_device("reconstruction", "yellow", "Saving raw stack")
        elif status == "raw_stack_saved":
            path = str(payload.get("path", ""))
            suffix = f" {Path(path).name}" if path else ""
            set_device("reconstruction", "green", f"Saved{suffix}")
        elif status == "raw_stack_save_failed":
            set_device("reconstruction", "red", "Save failed")
        elif status in {"acquisition_failed", "hardware_error"} or str(status).endswith("failed"):
            for device in ("camera", "slm", "daq"):
                set_device(device, "red", "Error")

    def start_sim_preview(self):
        """启动/调度 live 预览；返回 True 表示已发起或已调度，False 表示未发起（如静默失败）。

        immediate-live 两阶段据此判断"是否真正发起"（不要用 sim_preview_active——它早于
        真正 preview_started 即被置位）；激光只在收到 preview_started 后才点亮。
        """
        if self.sim_acquisition_in_progress:
            print("SIM preview start skipped because acquisition is in progress.")
            return False
        if not self.sim_camera_connected:
            qw.QMessageBox.information(self, "SIM Camera", "Please connect the SIM camera before starting Live.")
            return False
        self.sim_preview_requested = True
        if self.sim_preview_stop_in_progress:
            self.sim_preview_restart_requested = True
            self.update_sim_camera_action_buttons()
            return True
        # 每次真正发起前清 confirmed：它只由后续 preview_started 重新置。
        self.sim_preview_started_confirmed = False
        self.sync_sim_camera_config_from_ui(save_to_disk=False)
        self.ensure_sim_runtime()
        preview_camera_config = copy.copy(self.sim_app_config.camera)
        preview_camera_config.exposure_us = SIM_PREVIEW_EXPOSURE_US
        try:
            MainWindow.apply_connected_sim_camera_config(self, camera_config=preview_camera_config)
        except Exception as e:
            self.sim_preview_requested = False
            self.sim_preview_restart_requested = False
            print(f"SIM camera config apply before preview failed: {str(e)}")
            qw.QMessageBox.warning(self, "SIM Camera", str(e))
            self.update_sim_camera_action_buttons()
            return False
        auto_contrast_state = getattr(self, "sim_auto_contrast_state", None)
        if auto_contrast_state is not None:
            auto_contrast_state.reset()
        self.sim_preview_controller.start(preview_camera_config, timeout_ms=200)
        # controller.start() 实际被调用后才递增序号（作为"真正发起"凭据）。
        self.sim_preview_start_seq = int(getattr(self, "sim_preview_start_seq", 0)) + 1
        self.sim_preview_restart_requested = False
        self.sim_preview_stop_in_progress = False
        self.sim_preview_active = True
        self.update_sim_camera_action_buttons()
        return True

    def stop_sim_preview(self, wait=True, clear_restart=True, clear_display=False, stop_immediate_live=True):
        # 真正停止预览 = 关找样品激光（默认 True）。注意：内部 ROI/曝光 restart 走
        # restart_sim_preview_with_current_settings（直接 controller.stop），不经本函数，
        # 故 immediate-live 在那条路径不会被关掉（可用性 carve-out 自动满足）。
        if stop_immediate_live:
            active_or_pending = getattr(self, "_immediate_live_active_or_pending", None)
            stop_immediate = getattr(self, "stop_immediate_live_mode", None)
            if callable(active_or_pending) and callable(stop_immediate) and active_or_pending():
                stop_immediate()
        self.sim_preview_started_confirmed = False
        self.sim_preview_restart_timer.stop()
        preview_poll_timer = getattr(self, "sim_preview_poll_timer", None)
        if preview_poll_timer is not None:
            preview_poll_timer.stop()
        if clear_restart:
            self.sim_preview_restart_requested = False
        controller_busy = False
        if self.sim_preview_controller is not None:
            controller_busy = (
                getattr(self.sim_preview_controller, "active", False)
                or getattr(self.sim_preview_controller, "stopping", False)
                or self.sim_preview_stop_in_progress
            )
        self.sim_preview_active = False
        self.sim_preview_stop_in_progress = bool(controller_busy)
        self.sim_last_preview_sequence = -1
        auto_contrast_state = getattr(self, "sim_auto_contrast_state", None)
        if auto_contrast_state is not None:
            auto_contrast_state.reset()
        self.ui.lb_sCMOS_FPSshow.setText("0")
        if clear_display:
            self._clear_sim_preview_display()
        self.update_sim_camera_action_buttons()
        if self.sim_preview_controller is None or not controller_busy:
            return True
        self.sim_preview_controller.stop(wait=wait)
        if wait:
            still_busy = bool(
                getattr(self.sim_preview_controller, "active", False)
                or getattr(self.sim_preview_controller, "stopping", False)
            )
            self.sim_preview_stop_in_progress = still_busy
            self.update_sim_camera_action_buttons()
            return not still_busy
        return False

    def btn_sCMOS_live_function(self):
        if not self.sim_camera_connected:
            qw.QMessageBox.information(self, "SIM Camera", "Please connect the SIM camera before starting Live.")
            return
        live_requested = bool(getattr(self, "sim_preview_requested", False))
        if live_requested or self.sim_preview_active or self.sim_preview_stop_in_progress:
            self.sim_preview_requested = False
            self.sim_preview_restart_requested = False
            self.stop_sim_preview(wait=False, clear_restart=True, clear_display=True)
        else:
            self.start_sim_preview()

    def restart_sim_preview_with_current_settings(self):
        if not self.sim_camera_connected or self.sim_acquisition_in_progress:
            return
        live_requested = bool(getattr(self, "sim_preview_requested", False) or self.sim_preview_active)
        if not live_requested:
            return
        if not self.sim_preview_active and not self.sim_preview_stop_in_progress:
            return
        self.sim_preview_restart_requested = True
        if self.sim_preview_stop_in_progress:
            return
        self.sim_preview_stop_in_progress = True
        self.sim_preview_active = False
        preview_poll_timer = getattr(self, "sim_preview_poll_timer", None)
        if preview_poll_timer is not None:
            preview_poll_timer.stop()
        self.sim_last_preview_sequence = -1
        self.ui.lb_sCMOS_FPSshow.setText("0")
        self.update_sim_camera_action_buttons()
        if self.sim_preview_controller is not None:
            self.sim_preview_controller.stop(wait=False)

    def slot_handle_sim_preview_status(self, status, payload):
        if status == "preview_started":
            self.sim_preview_active = True
            # 真正出帧后才置 confirmed：它是点亮找样品激光的唯一前置凭据。
            self.sim_preview_started_confirmed = True
            self.sim_preview_restart_requested = False
            self.sim_preview_stop_in_progress = False
            self.sim_last_preview_sequence = -1
            poll_interval_ms = int(
                getattr(
                    self.sim_preview_controller,
                    "frame_poll_interval_ms",
                    33,
                )
            )
            preview_poll_timer = getattr(self, "sim_preview_poll_timer", None)
            if preview_poll_timer is not None:
                preview_poll_timer.start(max(1, poll_interval_ms))
            self.update_sim_camera_action_buttons()
            # 两阶段 pending：预览已确认 → 兑现。activate=点灯；reconfirm=planned restart 重新确认。
            pending = getattr(self, "_immediate_pending", None)
            if pending is not None:
                self._immediate_pending = None
                # bump token，使配对的在途 singleShot 看门狗失配而被忽略（已兑现）。
                self._immediate_pending_token = int(getattr(self, "_immediate_pending_token", 0)) + 1
                if pending.get("mode") == "activate":
                    ro_index = pending.get("ro_index")
                    wavelength = pending.get("wavelength")
                    # 点灯前再确认：相机仍连、下拉仍选同一 RO（等待期间用户可能改了），否则不点灯。
                    combo = getattr(self.ui, "cmb_SLM_immediateRO", None)
                    still_selected = combo is not None and combo.currentData() == ro_index
                    activate_now = getattr(self, "_activate_immediate_now", None)
                    if getattr(self, "sim_camera_connected", False) and still_selected and callable(activate_now):
                        activate_now(ro_index, wavelength)
                    else:
                        reset_dropdown = getattr(self, "_reset_immediate_ro_dropdown", None)
                        if callable(reset_dropdown):
                            reset_dropdown()
                else:  # reconfirm：planned restart 重新拿到 preview_started，恢复 confirmed。
                    self.sim_preview_planned_restart = False
            return
        if status == "preview_stopped":
            # 信号源头清 confirmed：覆盖异常停止 / worker 自退出 / error 后 finally 发 stopped。
            self.sim_preview_started_confirmed = False
            live_requested = bool(getattr(self, "sim_preview_requested", self.sim_preview_restart_requested))
            pending_restart = self.sim_preview_restart_requested and live_requested
            self.sim_preview_restart_requested = False
            self.sim_preview_active = False
            self.sim_preview_stop_in_progress = False
            preview_poll_timer = getattr(self, "sim_preview_poll_timer", None)
            if preview_poll_timer is not None:
                preview_poll_timer.stop()
            self.sim_last_preview_sequence = -1
            self.ui.lb_sCMOS_FPSshow.setText("0")
            self.update_sim_camera_action_buttons()
            planned = bool(getattr(self, "sim_preview_planned_restart", False))
            stop_immediate = getattr(self, "stop_immediate_live_mode", None)
            active_or_pending = getattr(self, "_immediate_live_active_or_pending", None)
            if pending_restart and self.sim_camera_connected and not self.sim_acquisition_in_progress:
                restarted = self.start_sim_preview()
                # planned restart 但 restart 没真正发起（静默失败）→ 立即关激光（[P1] 看门狗即时分支）。
                if planned and not restarted and callable(stop_immediate):
                    stop_immediate()
                return
            # 非预期停止（无重启）：若处于 immediate-live 且非 planned restart，关激光复位下拉。
            if (
                not planned
                and callable(active_or_pending)
                and callable(stop_immediate)
                and active_or_pending()
            ):
                stop_immediate()

    def slot_handle_sim_preview_error(self, message):
        self.sim_preview_requested = False
        self.sim_preview_restart_requested = False
        self.stop_sim_preview(wait=False)
        print(f"SIM preview error: {message}")
        qw.QMessageBox.warning(self, "SIM Preview Error", message.splitlines()[0])

    def trigger_sim_raw_9frame_acquisition(self, trigger_source="manual"):
        return MainWindow.trigger_sim_formal_acquisition(
            self,
            trigger_source=trigger_source,
            raw_only=True,
        )

    # 正式 SIM 采集会先停止 live preview，再把当前 UI/config 快照交给后台 controller。
    def trigger_sim_formal_acquisition(self, trigger_source="manual", raw_only=False):
        if self.sim_acquisition_in_progress:
            print("SIM acquisition skipped because another acquisition is still running.")
            return
        # 手动 Z-Scan 移动/对焦正在后台运行时，禁止启动 SIM9 采集（共用 stage/相机/SLM/DAQ）。
        if getattr(self, "_zscan_move_in_progress", False):
            qw.QMessageBox.information(
                self,
                "Z-Scan",
                "Z-Scan is running. Please wait for it to finish before starting SIM9 acquisition.",
            )
            return
        # 正式采集前必须先关找样品激光：SIM9 波形自己驱动激光线，且 set_line 整 port 写
        # 绝不能与波形并存。此处只关光不清空 immediate RO 列表，采集后仍可继续找样品。
        stop_immediate = getattr(self, "stop_immediate_live_mode", None)
        if callable(stop_immediate):
            stop_immediate(reset_dropdown=False)
        # 硬互锁：若找样品激光未能关闭（DAQ 拉低失败、状态仍 active/pending），绝不能启动
        # SIM9 波形（set_line 整 port 与波形并存会破坏 TTL/时序）。中止采集并提示检查 DAQ。
        active_or_pending = getattr(self, "_immediate_live_active_or_pending", None)
        if callable(active_or_pending) and active_or_pending():
            qw.QMessageBox.warning(
                self,
                "SIM SLM",
                "找样品激光未能关闭（DAQ 拉低失败），已中止 SIM9 采集以避免与采集波形冲突。请检查 DAQ 连接后重试。",
            )
            return
        select_none = getattr(self, "_select_immediate_ro_none_without_clearing", None)
        if callable(select_none):
            select_none()
        self.sync_sim_camera_config_from_ui(save_to_disk=False)
        self.ensure_sim_runtime()
        if not getattr(self, "sim_slm_connected", False):
            qw.QMessageBox.information(
                self,
                "SIM SLM",
                "Please connect the SLM before starting SIM acquisition.",
            )
            return
        self.sim_preview_restart_timer.stop()
        original_preview_requested = bool(getattr(self, "sim_preview_requested", False))
        original_restart_requested = bool(getattr(self, "sim_preview_restart_requested", False))
        preview_should_resume = bool(
            getattr(self, "sim_preview_requested", self.sim_preview_active or self.sim_preview_restart_requested)
        )
        self.sim_resume_preview_after_acquisition = (
            preview_should_resume
        )
        self.sim_preview_requested = False
        self.sim_preview_restart_requested = False
        if self.sim_preview_active or self.sim_preview_stop_in_progress:
            if not self.stop_sim_preview(wait=True):
                self.sim_resume_preview_after_acquisition = False
                self.sim_preview_requested = original_preview_requested
                self.sim_preview_restart_requested = original_restart_requested
                qw.QMessageBox.warning(
                    self,
                    "SIM Preview",
                    "SIM preview is still stopping. Please retry after it stops.",
                )
                return
        self.sim_acquisition_in_progress = True
        self.update_sim_camera_action_buttons()
        self.set_sim_camera_controls_enabled(False)
        self.sim_current_task_id = ""
        try:
            if raw_only:
                ensure_raw_save = getattr(self, "ensure_sim_raw_stack_save_worker", None)
                disconnect_reconstruction = getattr(self, "disconnect_sim_reconstruction_worker_from_controller", None)
                connect_raw_save = getattr(self, "connect_sim_raw_stack_save_worker_to_controller", None)
                if callable(ensure_raw_save):
                    ensure_raw_save()
                if callable(disconnect_reconstruction):
                    disconnect_reconstruction()
                if callable(connect_raw_save):
                    connect_raw_save()
            else:
                disconnect_raw_save = getattr(self, "disconnect_sim_raw_stack_save_worker_from_controller", None)
                connect_reconstruction = getattr(self, "connect_sim_reconstruction_worker_to_controller", None)
                if callable(disconnect_raw_save):
                    disconnect_raw_save()
                if callable(connect_reconstruction):
                    connect_reconstruction()
            self.sim_current_acquisition_raw_only = bool(raw_only)
            app_config_snapshot = app_config_from_dict(app_config_to_dict(self.sim_app_config))
            task = SimTaskConfig(
                laser_wavelength_nm=app_config_snapshot.selected_laser_nm,
                pattern_files=list(app_config_snapshot.pattern_files),
                running_order_name=self.sim_app_config.selected_running_order,
                camera=app_config_snapshot.camera,
                timing=app_config_snapshot.timing,
            )
            start_kwargs = {
                "prepare_running_order": True,
                "initialize_hardware": True,
                "apply_daq_config": True,
                "apply_camera_config": True,
                "z_scan_config": app_config_snapshot.z_scan,
            }
            if raw_only:
                raw_reconstruction_config = copy.copy(app_config_snapshot.reconstruction)
                raw_reconstruction_config.enabled = False
                # 「是否开启 Z-Scan」开关 ON 时，SIM9 采集前先做完整 Z-Scan 自动对焦；OFF 时直接
                # 采 9 帧。沿用 ZScanConfig.enabled 语义（worker 会自动选 z-scan RO、对焦、恢复正式 RO）。
                start_kwargs["z_scan_enabled"] = bool(app_config_snapshot.z_scan.enabled)
                start_kwargs["reconstruction_config"] = raw_reconstruction_config
                self._sim_raw_acquisition_pending = {
                    "start_perf": time.perf_counter(),
                    "laser_wavelength_nm": int(app_config_snapshot.selected_laser_nm),
                    "exposure_us": int(app_config_snapshot.camera.exposure_us),
                }
            self.sim_current_task_id = self.sim_acquisition_controller.start_single_acquisition(task, **start_kwargs)
            print(f"SIM acquisition started from {trigger_source}: {self.sim_current_task_id}")
        except Exception as e:
            self.sim_acquisition_in_progress = False
            restore_routing = getattr(self, "_restore_sim_post_acquisition_routing_after_raw_only", None)
            if callable(restore_routing):
                restore_routing()
            self.update_sim_camera_action_buttons()
            self.set_sim_camera_controls_enabled(self.sim_camera_connected)
            print(f"SIM acquisition start failed ({trigger_source}): {str(e)}")
            if self.sim_resume_preview_after_acquisition and self.sim_camera_connected:
                self.sim_resume_preview_after_acquisition = False
                self.start_sim_preview()
            qw.QMessageBox.warning(self, "SIM Acquisition Error", str(e))

    def apply_sim_camera_runtime_capabilities(self, payload, save_to_disk=False):
        payload = dict(payload or {})
        if not hasattr(self, "sim_app_config"):
            return
        camera = self.sim_app_config.camera

        sensor_width = payload.get("sensor_width")
        sensor_height = payload.get("sensor_height")
        if sensor_width is not None and sensor_height is not None:
            try:
                self.sim_camera_sensor_size = (
                    max(1, int(sensor_width)),
                    max(1, int(sensor_height)),
                )
            except (TypeError, ValueError):
                pass

        roi_step_px = payload.get("roi_step_px")
        if roi_step_px is not None:
            try:
                self.sim_camera_roi_step_px = max(1, int(roi_step_px))
            except (TypeError, ValueError):
                pass

        roi_size_presets = payload.get("roi_size_presets")
        normalized_presets = []
        for preset in roi_size_presets or []:
            try:
                width, height = preset
                normalized_presets.append((max(1, int(width)), max(1, int(height))))
            except (TypeError, ValueError):
                continue
        if normalized_presets:
            self.sim_camera_size_presets = tuple(normalized_presets)
        elif sensor_width is not None and sensor_height is not None:
            sensor_size = MainWindow.current_sim_camera_sensor_size(self)
            self.sim_camera_size_presets = build_sim_camera_size_presets(
                sensor_size[0],
                sensor_size[1],
            )

        applied_roi = payload.get("applied_roi")
        if isinstance(applied_roi, dict):
            camera.roi_x = int(applied_roi.get("x", camera.roi_x))
            camera.roi_y = int(applied_roi.get("y", camera.roi_y))
            camera.roi_width = int(applied_roi.get("width", camera.roi_width))
            camera.roi_height = int(applied_roi.get("height", camera.roi_height))
        else:
            camera.roi_width, camera.roi_height, camera.roi_x, camera.roi_y = normalize_sim_camera_roi(
                camera.roi_width,
                camera.roi_height,
                camera.roi_x,
                camera.roi_y,
                sensor_size=MainWindow.current_sim_camera_sensor_size(self),
                step_px=MainWindow.current_sim_camera_roi_step_px(self),
                presets=MainWindow.current_sim_camera_size_presets(self),
            )

        ui = getattr(self, "ui", None)
        if ui is not None and hasattr(ui, "cmb_sCMOS_imageSize"):
            MainWindow.refresh_sim_camera_size_choices(
                self,
                selected_size=(camera.roi_width, camera.roi_height),
            )
        sync_roi_controls = getattr(self, "sync_sim_camera_roi_position_controls", None)
        if callable(sync_roi_controls):
            sync_roi_controls()
        elif ui is not None and hasattr(ui, "spb_sCMOS_ROI_X") and hasattr(ui, "spb_sCMOS_ROI_Y"):
            MainWindow.sync_sim_camera_roi_position_controls(self)

        if save_to_disk:
            save_app_config(self.sim_app_config, self.sim_app_config.config_path)
        refresh_summary = getattr(self, "refresh_sim_settings_summary", None)
        if callable(refresh_summary):
            refresh_summary()

    def update_sim_runtime_timing_from_payload(self, payload):
        payload = dict(payload or {})
        self.sim_runtime_timing_snapshot = payload
        if payload.get("applied_bit_depth") is not None and hasattr(self, "sim_app_config"):
            self.sim_app_config.camera.bit_depth = int(payload["applied_bit_depth"])
        roi_capability_keys = {
            "applied_roi",
            "sensor_width",
            "sensor_height",
            "roi_step_px",
            "roi_size_presets",
        }
        if roi_capability_keys.intersection(payload):
            MainWindow.apply_sim_camera_runtime_capabilities(self, payload, save_to_disk=False)
        supported_bit_depths = payload.get("supported_bit_depths")
        if supported_bit_depths:
            MainWindow.refresh_sim_camera_bit_depth_choices(self, supported_bit_depths)
        else:
            self.refresh_sim_settings_summary()

    def apply_connected_sim_camera_config(self, camera_config=None):
        if not getattr(self, "sim_camera_connected", False):
            return {}
        controller = getattr(self, "sim_acquisition_controller", None)
        if controller is None:
            return {}
        camera_config = camera_config if camera_config is not None else self.sim_app_config.camera
        result = controller.apply_camera_config(camera_config) or {}
        payload = {
            "camera_config": dict(getattr(camera_config, "__dict__", {})),
            **dict(result),
        }
        MainWindow.update_sim_runtime_timing_from_payload(self, payload)
        return dict(result)

    def slot_handle_sim_acquisition_status(self, status, payload):
        update_runtime = getattr(self, "update_sim_runtime_status_widgets", None)
        if callable(update_runtime):
            update_runtime(status, payload)
        if status == "acquisition_starting":
            task_id = payload.get("task_id", "")
            if task_id:
                self.sim_current_task_id = task_id
            print(f"SIM acquisition status: {status} {payload}")
        elif status == "camera_config_applied":
            MainWindow.update_sim_runtime_timing_from_payload(self, payload)
            print(f"SIM acquisition status: {status} {payload}")
        elif status == "running_order_selected":
            running_order_name = str(payload.get("running_order_name", ""))
            if running_order_name:
                self.sim_app_config.selected_running_order = running_order_name
                refresh_summary = getattr(self, "refresh_sim_settings_summary", None)
                if callable(refresh_summary):
                    refresh_summary()
            print(f"SIM acquisition status: {status} {payload}")
        elif status in {"acquisition_complete", "acquisition_cancelled", "patterns_prepared", "camera_connected", "frame_captured", "slm_connected", "slm_disconnected", "daq_config_applied"}:
            print(f"SIM acquisition status: {status} {payload}")
        elif status == "running_order_restore_warning":
            print(f"SIM acquisition status: {status} {payload}")

    def slot_handle_sim_z_scan_progress(self, step_index, total_steps, z_um, focus_score):
        print(
            "SIM z-scan progress: "
            f"{int(step_index)}/{int(total_steps)} z={float(z_um):.3f} um score={float(focus_score):.3f}"
        )

    def slot_handle_sim_z_scan_complete(self, best_z_um, focus_curve):
        print(f"SIM z-scan complete: best_z={float(best_z_um):.3f} um, points={len(focus_curve or [])}")
        self.poll_sim_stage_position(force=True)

    def slot_handle_sim_acquisition_ready(self, payload):
        self.sim_acquisition_in_progress = False
        self.update_sim_camera_action_buttons()
        self.set_sim_camera_controls_enabled(self.sim_camera_connected)
        payload = dict(payload or {})
        task_id = payload.get("task_id", self.sim_current_task_id)
        self.sim_current_task_id = task_id
        update_runtime = getattr(self, "update_sim_runtime_status_widgets", None)
        raw_only = bool(getattr(self, "sim_current_acquisition_raw_only", False))
        if raw_only:
            pending = getattr(self, "_sim_raw_acquisition_pending", {}) or {}
            self.sim_last_raw_acquisition_summary = {
                "task_id": task_id,
                "stack_shape": payload.get("stack_shape", []),
                "stack_dtype": payload.get("stack_dtype", ""),
                "laser_wavelength_nm": pending.get("laser_wavelength_nm"),
                "exposure_us": pending.get("exposure_us"),
                "start_perf": pending.get("start_perf"),
            }
        if callable(update_runtime):
            update_runtime(
                "acquisition_complete",
                {
                    "task_id": task_id,
                    "stack_shape": payload.get("stack_shape", []),
                    "stack_dtype": payload.get("stack_dtype", ""),
                    "metadata": dict(payload.get("metadata", {}) or {}),
                },
            )
            if raw_only:
                finished_task_ids = getattr(self, "sim_raw_stack_save_finished_task_ids", set())
                if task_id not in finished_task_ids:
                    update_runtime("raw_stack_saving", {"task_id": task_id})
            else:
                update_runtime("reconstruction_starting", {"task_id": task_id})
        print(f"SIM acquisition ready: {task_id}")
        restore_routing = getattr(self, "_restore_sim_post_acquisition_routing_after_raw_only", None)
        if callable(restore_routing):
            restore_routing()
        if self.sim_resume_preview_after_acquisition and self.sim_camera_connected:
            self.sim_resume_preview_after_acquisition = False
            self.start_sim_preview()

    def slot_handle_sim_reconstruction_ready(self, result):
        self.sim_last_reconstruction_result = result
        task_id = getattr(result, "task_id", self.sim_current_task_id)
        self.sim_current_task_id = task_id
        preview = getattr(result, "preview_image", None)
        shape = list(getattr(preview, "shape", [])) if preview is not None else []
        update_runtime = getattr(self, "update_sim_runtime_status_widgets", None)
        if callable(update_runtime):
            update_runtime("reconstruction_complete", {"task_id": task_id, "preview_shape": shape})
        print(f"SIM reconstruction ready: {task_id} shape={shape}")

    def slot_handle_sim_reconstruction_failed(self, task_id, message):
        self.sim_last_reconstruction_result = None
        update_runtime = getattr(self, "update_sim_runtime_status_widgets", None)
        if callable(update_runtime):
            update_runtime("reconstruction_failed", {"task_id": task_id})
        print(f"SIM reconstruction failed: {task_id} {message}")
        message_lines = str(message).splitlines()
        display_message = message_lines[0] if message_lines else "SIM reconstruction failed."
        qw.QMessageBox.warning(self, "SIM Reconstruction Error", display_message)

    def slot_handle_sim_raw_stack_saved(self, task_id, path):
        task_id = str(task_id)
        path = str(path)
        finished_task_ids = getattr(self, "sim_raw_stack_save_finished_task_ids", None)
        if finished_task_ids is None:
            finished_task_ids = set()
            self.sim_raw_stack_save_finished_task_ids = finished_task_ids
        finished_task_ids.add(task_id)
        update_runtime = getattr(self, "update_sim_runtime_status_widgets", None)
        if callable(update_runtime):
            update_runtime("raw_stack_saved", {"task_id": task_id, "path": path})
        labels = getattr(self, "sim_runtime_status_labels", {})
        status_label = labels.get("reconstruction") if hasattr(labels, "get") else None
        if status_label is not None:
            status_label.setToolTip(path)
        print(f"SIM raw stack saved: {path}")
        summary = getattr(self, "sim_last_raw_acquisition_summary", None) or {}
        matched = summary.get("task_id") == task_id
        shape = summary.get("stack_shape") if matched else None
        # 第一行：完成提示 + 总耗时
        first_line = "SIM9 帧采集已完成。"
        start_perf = summary.get("start_perf") if matched else None
        if start_perf is not None:
            elapsed_s = max(0.0, time.perf_counter() - float(start_perf))
            first_line += f"  总耗时: {elapsed_s:.2f} s"
        message_lines = [first_line]
        # 第二行：图像格式 + 曝光 + 波长
        detail_parts = []
        if shape and len(shape) >= 3:
            size_text = f"{int(shape[2])}X{int(shape[1])}"
            dtype_str = str(summary.get("stack_dtype") or "")
            bits = None
            if dtype_str:
                try:
                    bits = int(np.dtype(dtype_str).itemsize) * 8
                except (TypeError, ValueError):
                    bits = None
            if bits:
                size_text += f"（{bits}位）"
            detail_parts.append(f"图像格式: {size_text}")
        if matched and summary.get("exposure_us"):
            detail_parts.append(f"曝光时间: {float(summary['exposure_us']) / 1000.0:g} ms")
        if matched and summary.get("laser_wavelength_nm"):
            detail_parts.append(f"激光波长: {int(summary['laser_wavelength_nm'])} nm")
        if detail_parts:
            message_lines.append(", ".join(detail_parts))
        # 末行：保存路径
        message_lines.append(f"保存路径: {path}")

        self._show_sim_raw_stack_saved_dialog(message_lines)

    def _show_sim_raw_stack_saved_dialog(self, message_lines):
        try:
            dialog = qw.QDialog(self)
        except RuntimeError:
            dialog = qw.QDialog()
        dialog.setWindowTitle("SIM9 帧采集")
        font_size_pt = 13
        chinese_font = QFont("Microsoft YaHei", font_size_pt)
        ascii_font = QFont("Arial", font_size_pt)
        dialog.setFont(chinese_font)

        main_layout = qw.QVBoxLayout(dialog)
        content_layout = qw.QHBoxLayout()
        icon_label = qw.QLabel(dialog)
        icon_label.setFont(chinese_font)
        icon_size = dialog.style().pixelMetric(qw.QStyle.PM_MessageBoxIconSize, None, dialog)
        icon_pixmap = dialog.style().standardIcon(qw.QStyle.SP_MessageBoxInformation).pixmap(
            icon_size, icon_size
        )
        icon_label.setPixmap(icon_pixmap)
        icon_label.setAlignment(Qt.AlignTop)

        plain_text = "\n".join(message_lines)
        text_label = qw.QLabel(dialog)
        text_label.setObjectName("sim_raw_stack_saved_message_label")
        text_label.setFont(chinese_font)
        text_label.setTextFormat(Qt.RichText)
        text_label.setText(self._sim_dialog_message_to_html(message_lines, font_size_pt))
        text_label.setProperty("sim_plain_text", plain_text)
        text_label.setWordWrap(False)
        text_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        longest_px = max(
            (
                self._measure_sim_dialog_line_width(line, chinese_font, ascii_font)
                for line in message_lines
            ),
            default=0,
        )
        right_padding_px = 96
        if longest_px > 0:
            text_label.setMinimumWidth(longest_px + right_padding_px)
            dialog.setMinimumWidth(longest_px + icon_size + right_padding_px + 80)

        content_layout.addWidget(icon_label)
        content_layout.addWidget(text_label)
        main_layout.addLayout(content_layout)

        button_box = qw.QDialogButtonBox(qw.QDialogButtonBox.Ok, dialog)
        button_box.setFont(ascii_font)
        for button in button_box.buttons():
            button.setFont(ascii_font)
        button_box.accepted.connect(dialog.accept)
        main_layout.addWidget(button_box)
        dialog.exec_()

    @staticmethod
    def _sim_dialog_message_to_html(message_lines, font_size_pt):
        html_lines = []
        for line in message_lines:
            html_lines.append(
                '<div style="white-space: nowrap;">'
                + MainWindow._sim_dialog_line_to_html(line, font_size_pt)
                + "</div>"
            )
        return '<html><body style="margin:0;">' + "".join(html_lines) + "</body></html>"

    @staticmethod
    def _sim_dialog_line_to_html(line, font_size_pt):
        if not line:
            return ""
        segments = []
        current_is_ascii = ord(line[0]) < 128
        current_chars = []
        for char in line:
            char_is_ascii = ord(char) < 128
            if char_is_ascii != current_is_ascii:
                segments.append((current_is_ascii, "".join(current_chars)))
                current_chars = []
                current_is_ascii = char_is_ascii
            current_chars.append(char)
        segments.append((current_is_ascii, "".join(current_chars)))

        html_segments = []
        for is_ascii, segment in segments:
            family = "Arial" if is_ascii else "Microsoft YaHei"
            escaped = html.escape(segment, quote=False)
            html_segments.append(
                f'<span style="font-family:\'{family}\'; font-size:{font_size_pt}pt;">'
                f"{escaped}</span>"
            )
        return "".join(html_segments)

    @staticmethod
    def _measure_sim_dialog_line_width(line, chinese_font, ascii_font):
        chinese_metrics = QFontMetrics(chinese_font)
        ascii_metrics = QFontMetrics(ascii_font)
        width = 0
        for char in line:
            metrics = ascii_metrics if ord(char) < 128 else chinese_metrics
            width += metrics.horizontalAdvance(char)
        return width

    def slot_handle_sim_raw_stack_save_failed(self, task_id, message):
        task_id = str(task_id)
        message = str(message)
        finished_task_ids = getattr(self, "sim_raw_stack_save_finished_task_ids", None)
        if finished_task_ids is None:
            finished_task_ids = set()
            self.sim_raw_stack_save_finished_task_ids = finished_task_ids
        finished_task_ids.add(task_id)
        update_runtime = getattr(self, "update_sim_runtime_status_widgets", None)
        if callable(update_runtime):
            update_runtime("raw_stack_save_failed", {"task_id": task_id, "message": message})
        print(f"SIM raw stack save failed: {task_id} {message}")
        message_lines = message.splitlines()
        display_message = message_lines[0] if message_lines else "SIM raw stack save failed."
        qw.QMessageBox.warning(self, "SIM9 Frames Save Error", display_message)

    def slot_handle_sim_acquisition_failed(self, task_id, message):
        self.sim_acquisition_in_progress = False
        restore_routing = getattr(self, "_restore_sim_post_acquisition_routing_after_raw_only", None)
        if callable(restore_routing):
            restore_routing()
        self.update_sim_camera_action_buttons()
        self.set_sim_camera_controls_enabled(self.sim_camera_connected)
        self.sim_current_task_id = task_id
        update_runtime = getattr(self, "update_sim_runtime_status_widgets", None)
        if callable(update_runtime):
            update_runtime("acquisition_failed", {"task_id": task_id})
        print(f"SIM acquisition failed: {task_id} {message}")
        if self.sim_resume_preview_after_acquisition and self.sim_camera_connected:
            self.sim_resume_preview_after_acquisition = False
            self.start_sim_preview()
        message_lines = str(message).splitlines()
        display_message = message_lines[0] if message_lines else "SIM acquisition failed."
        appended_warning_lines = set()
        for line in message_lines[1:]:
            if "SLM may still be on z-scan RO" in line or "formal SIM running order could not be restored" in line:
                if line in appended_warning_lines:
                    continue
                appended_warning_lines.add(line)
                display_message = f"{display_message}\n{line}"
        qw.QMessageBox.warning(self, "SIM Acquisition Error", display_message)

    def slot_handle_sim_acquisition_cancelled(self, task_id, message):
        self.sim_acquisition_in_progress = False
        restore_routing = getattr(self, "_restore_sim_post_acquisition_routing_after_raw_only", None)
        if callable(restore_routing):
            restore_routing()
        self.update_sim_camera_action_buttons()
        self.set_sim_camera_controls_enabled(self.sim_camera_connected)
        self.sim_current_task_id = task_id
        update_runtime = getattr(self, "update_sim_runtime_status_widgets", None)
        if callable(update_runtime):
            update_runtime("acquisition_cancelled", {"task_id": task_id})
        print(f"SIM acquisition cancelled: {task_id} {message}")
        if self.sim_resume_preview_after_acquisition and self.sim_camera_connected:
            self.sim_resume_preview_after_acquisition = False
            self.start_sim_preview()

    def collect_current_settings(self):
        """收集当前所有控件的参数值，返回字典"""
        MainWindow.commit_sim_camera_pending_widget_edits(self)
        configure_settings = {}
         # 收集所有需要保存的控件数据
        #Hardvare Connection模块

        configure_settings['spb_ringBufferCapacity'] = self.ui.spb_ringBufferCapacity.value()
        configure_settings['cmb_fastCamera'] = self.ui.cmb_fastCamera.currentIndex()
        configure_settings['cmb_singleChip'] = self.ui.cmb_singleChip.currentIndex()
        #Camera Settings模块 Fast Camera
        configure_settings['spb_pixelWidth'] = self.ui.spb_pixelWidth.value()
        configure_settings['spb_pixelHeight'] = self.ui.spb_pixelHeight.value()
        configure_settings['spb_exposureTime'] = self.ui.spb_exposureTime.value()
        configure_settings['spb_cameraGain'] = self.ui.spb_cameraGain.value()
        configure_settings['spb_frameRate'] = self.ui.spb_frameRate.value()
        configure_settings['spb_preTriggerBuffer'] = self.ui.spb_preTriggerBuffer.value()
        configure_settings['spb_postTriggerBuffer'] = self.ui.spb_postTriggerBuffer.value()
        configure_settings['spb_missEventSavePreFrames'] = self.ui.spb_missEventSavePreFrames.value()
        configure_settings['spb_fastCamera_displayGray_max']         = self.ui.spb_fastCamera_displayGray_max.value()
        configure_settings['spb_fastCamera_displayGray_max']         = self.ui.spb_fastCamera_displayGray_max.value()
        #Camera Settings模块 sCMOS
        sim_roi_width, sim_roi_height = size_from_sim_camera_label(
            self.ui.cmb_sCMOS_imageSize.currentText(),
            presets=MainWindow.current_sim_camera_size_presets(self),
        )
        configure_settings['spb_sCMOS_pixelWidth']              = sim_roi_width
        configure_settings['spb_sCMOS_pixelHeight']             = sim_roi_height
        configure_settings['spb_sCMOS_exposureTime']            = normalize_legacy_sim_exposure_setting(
            self.ui.spb_sCMOS_exposureTime.value()
        )
        configure_settings['spb_sCMOS_displayGray_max']         = self.ui.spb_sCMOS_displayGray_max.value()
        #configure_settings['spb_sCMOS_frameRate']               = self.ui.spb_sCMOS_frameRate.value()
        configure_settings['spb_sCMOS_ROI_X']                   = self.ui.spb_sCMOS_ROI_X.value()
        configure_settings['spb_sCMOS_ROI_Y']                   = self.ui.spb_sCMOS_ROI_Y.value()
        configure_settings['spb_sCMOS_preTriggerBuffer']        = self.ui.spb_sCMOS_preTriggerBuffer.value()
        configure_settings['spb_sCMOS_postTriggerBuffer']       = self.ui.spb_sCMOS_postTriggerBuffer.value()

        #Capture ROI 模块
        configure_settings['spb_captureROI_X']      = self.ui.spb_captureROI_X.value()
        configure_settings['spb_captureROI_Y']      = self.ui.spb_captureROI_Y.value()
        configure_settings['spb_captureROI_width']  = self.ui.spb_captureROI_width.value()
        configure_settings['spb_captureROI_height'] = self.ui.spb_captureROI_height.value()
        #Trapped ROI 模块
        configure_settings['spb_trappedROI_X']      = self.ui.spb_trappedROI_X.value()
        configure_settings['spb_trappedROI_Y']      = self.ui.spb_trappedROI_Y.value()
        configure_settings['spb_trappedROI_width']  = self.ui.spb_trappedROI_width.value()
        configure_settings['spb_trappedROI_height'] = self.ui.spb_trappedROI_height.value()
        #Function Measurement ROI 模块
        configure_settings['spb_sCMOS_functionROI_X']      = self.functionROI_X
        configure_settings['spb_sCMOS_functionROI_Y']      = self.functionROI_Y
        configure_settings['spb_sCMOS_functionROI_width']  = self.functionROI_width
        configure_settings['spb_sCMOS_functionROI_height'] = self.functionROI_height
        #Release ROI 模块
        configure_settings['spb_releaseROI_X']      = self.ui.spb_cellFlowThroughROI_X.value()
        configure_settings['spb_releaseROI_Y']      = self.ui.spb_cellFlowThroughROI_Y.value()
        configure_settings['spb_releaseROI_width']  = self.ui.spb_cellFlowThroughROI_width.value()
        configure_settings['spb_releaseROI_height'] = self.ui.spb_cellFlowThroughROI_height.value()
        # Collected Cells ROI 模块
        configure_settings['spb_collectedCellsROI_X']         = self.ui.spb_collectedROI_X.value()
        configure_settings['spb_collectedCellsROI_Y']         = self.ui.spb_collectedROI_Y.value()
        configure_settings['spb_collectedCellsROI_width']     = self.ui.spb_collectedROI_width.value()
        configure_settings['spb_collectedCellsROI_height']    = self.ui.spb_collectedROI_height.value()
        configure_settings['spb_collectedCellsROI_angle']     = self.ui.spb_collectedROI_angle.value()

        # Flow Rate Detection ROI 模块
        configure_settings['spb_flowRateROI_X']         = self.ui.spb_flowRateROI_X.value()
        configure_settings['spb_flowRateROI_Y']         = self.ui.spb_flowRateROI_Y.value()
        configure_settings['spb_flowRateROI_width']     = self.ui.spb_flowRateROI_width.value()
        configure_settings['spb_flowRateROI_height']    = self.ui.spb_flowRateROI_height.value()
        configure_settings['cmb_objective']             = self.ui.cmb_objective.currentIndex()
        configure_settings['spb_chipChannel_width']     = self.ui.spb_chipChannel_width.value()
        configure_settings['spb_chipChannel_height']    = self.ui.spb_chipChannel_height.value()
        configure_settings['spb_flowRateDetectFramesNumber']    = self.ui.spb_flowRateDetectFramesNumber.value()
        #Trigger Control 模块
        configure_settings['spb_triggerCapture_time']       = self.ui.spb_triggerCapture_time.value()
        configure_settings['spb_triggerReleaseSort_time']   = self.ui.spb_triggerReleaseSort_time.value()
        configure_settings['spb_triggerRelease_time']       = self.ui.spb_triggerRelease_time.value()
        configure_settings['spb_triggerFunction_time']      = self.ui.spb_triggerFunction_time.value()
        #Image Processing Settings 模块
        configure_settings['spb_trapFrames']            = self.ui.spb_trapFrames.value()
        configure_settings['spb_trapBalance_Time']      = self.ui.spb_trapBalance_Time.value()
        configure_settings['spb_collectFrames']         = self.ui.spb_collectFrames.value()
        configure_settings['spb_minArea']               = self.ui.spb_minArea.value()
        configure_settings['spb_maxArea']               = self.ui.spb_maxArea.value()
        configure_settings['spb_minLenth']              = self.function_min_delta_lenth
        configure_settings['spb_maxLenth']              = self.function_max_delta_lenth
        configure_settings['spb_threshold_Bi_sCMOS']     = self.sCMOS_Bi_threshold
        configure_settings['spb_sCMOS_morphologyKernel_size'] = self.sCMOS_morphologyKernel.shape[0]
        configure_settings['spb_sCMOS_openTimes']        = self.sCMOS_openTimes
        configure_settings['spb_sCMOS_closeTimes']       = self.sCMOS_closeTimes
        configure_settings['spb_sCMOS_minArea']          = self.sCMOS_minArea
        #Binary ROI 模块
        configure_settings['spb_threshold_Bi'] = self.ui.spb_threshold_Bi.value()
        configure_settings['sim_control'] = app_config_to_dict(self.sim_app_config)

        # 添加其他需要保存的控件...
        return configure_settings

    def save_current_settings_to_default(self):
        """自动保存当前配置到 lastConfiguration.json"""
        configure_settings = self.collect_current_settings()  # 复用参数收集
        
        default_path = Path(__file__).parent / "lastConfiguration.json"
        print("保存了pppppppppp")
        try:
            with open(default_path, 'w') as f:
                json.dump(configure_settings, f, indent=4)
            print("配置已自动保存到 lastConfiguration.json")
        except Exception as e:
            print(f"自动保存失败: {str(e)}")

    def apply_loaded_sim_settings_payload(self, configure_settings, *, apply_legacy_sim_camera_settings=True):
        sim_control_payload = configure_settings.get('sim_control')
        if sim_control_payload:
            self.sim_app_config = merge_legacy_sim_control_payload(self.sim_app_config, sim_control_payload)
            save_app_config(self.sim_app_config, self.sim_app_config.config_path)
            # B3：Load 替换了 sim_app_config（含 DAQ/Recon/Z-Scan）；把主界面三个 SIM 模块控件
            # 回填到最新配置（各 _init_* 内 blockSignals 防回环），并同步 controller DAQ / z-scan /
            # 常驻 recon worker，避免配置与控件/后端状态分裂（下次 Save 才不会用旧控件值覆盖）。
            for init_name in (
                "_init_daq_module_from_config",
                "_init_recon_module_from_config",
                "_init_zscan_module_from_config",
            ):
                init_fn = getattr(self, init_name, None)
                if callable(init_fn):
                    init_fn()
            controller = getattr(self, "sim_acquisition_controller", None)
            if controller is not None:
                try:
                    controller.apply_daq_config(self.sim_app_config.daq)
                    controller.z_scan_config = self.sim_app_config.z_scan
                except Exception as exc:
                    print(f"SIM apply DAQ/z-scan config on load failed: {exc}")
            ensure_worker = getattr(self, "ensure_sim_reconstruction_worker", None)
            if callable(ensure_worker):
                ensure_worker()  # B6：经 queued signal 下发 reconstruction 快照到常驻 worker
        elif apply_legacy_sim_camera_settings:
            self.sync_sim_camera_config_from_ui(save_to_disk=False)
        self.sync_sim_camera_controls_from_config()
        self.refresh_sim_settings_summary()

    def load_configure_settings(self, file_path, *, apply_legacy_sim_camera_settings=True):
        """通用配置加载逻辑"""
        previous_loading_state = bool(getattr(self, "_loading_configure_settings", False))
        self._loading_configure_settings = True
        try:
            with open(file_path, 'r') as f:
                configure_settings = json.load(f)
            # 设置Hardvare Connection模块
            self.ui.cmb_fastCamera.setCurrentIndex(configure_settings.get('cmb_fastCamera', 0))
            self.ui.cmb_singleChip.setCurrentIndex(configure_settings.get('cmb_singleChip', 0))

            self.ui.spb_ringBufferCapacity.setValue(configure_settings.get('spb_ringBufferCapacity', 5000))
            # 设置Camera Settings模块 Fast Camera
            self.ui.spb_pixelWidth.setValue(configure_settings.get('spb_pixelWidth', 816))
            self.ui.spb_pixelHeight.setValue(configure_settings.get('spb_pixelHeight', 624))
            self.ui.spb_exposureTime.setValue(configure_settings.get('spb_exposureTime', 0.4))
            self.ui.spb_cameraGain.setValue(configure_settings.get('spb_cameraGain', 12))
            self.ui.spb_frameRate.setValue(configure_settings.get('spb_frameRate', 1500))
            self.ui.spb_preTriggerBuffer.setValue(configure_settings.get('spb_preTriggerBuffer', 10))
            self.ui.spb_postTriggerBuffer.setValue(configure_settings.get('spb_postTriggerBuffer', 300))
            self.ui.spb_missEventSavePreFrames.setValue(configure_settings.get('spb_missEventSavePreFrames', 300))
            self.ui.spb_fastCamera_displayGray_max.setValue(configure_settings.get('spb_fastCamera_displayGray_max', 5000))

            # 设置Camera Settings模块 sCMOS
            sim_width = configure_settings.get('spb_sCMOS_pixelWidth', DEFAULT_SIM_CAMERA_SIZE[0])
            sim_height = configure_settings.get('spb_sCMOS_pixelHeight', DEFAULT_SIM_CAMERA_SIZE[1])
            MainWindow.refresh_sim_camera_size_choices(
                self,
                selected_size=(sim_width, sim_height),
            )
            self.ui.spb_sCMOS_exposureTime.setValue(
                normalize_legacy_sim_exposure_setting(
                    configure_settings.get('spb_sCMOS_exposureTime', SIM_EXPOSURE_DEFAULT_MS)
                )
            )
            self.ui.spb_sCMOS_displayGray_max.setValue(configure_settings.get('spb_sCMOS_displayGray_max', 5000))
            #self.ui.spb_sCMOS_frameRate.setValue(configure_settings.get('spb_sCMOS_frameRate', 1500))
            self.ui.spb_sCMOS_ROI_X.setValue(
                configure_settings.get('spb_sCMOS_ROI_X', configure_settings.get('spb_sCMOS_ringBufferCapacity', 0))
            )
            self.ui.spb_sCMOS_ROI_Y.setValue(
                configure_settings.get('spb_sCMOS_ROI_Y', configure_settings.get('spb_sCMOS_delayTime', 0))
            )
            self.ui.spb_sCMOS_preTriggerBuffer.setValue(configure_settings.get('spb_sCMOS_preTriggerBuffer', 10))
            self.ui.spb_sCMOS_postTriggerBuffer.setValue(configure_settings.get('spb_sCMOS_postTriggerBuffer', 200))

            # 设置Capture ROI 模块
            self.ui.spb_captureROI_X.setValue(configure_settings.get('spb_captureROI_X', 0))
            self.ui.spb_captureROI_Y.setValue(configure_settings.get('spb_captureROI_Y', 0))
            self.ui.spb_captureROI_width.setValue(configure_settings.get('spb_captureROI_width', 0))
            self.ui.spb_captureROI_height.setValue(configure_settings.get('spb_captureROI_height', 0))
            # 设置Trapped ROI 模块
            self.ui.spb_trappedROI_X.setValue(configure_settings.get('spb_trappedROI_X', 0))
            self.ui.spb_trappedROI_Y.setValue(configure_settings.get('spb_trappedROI_Y', 0))
            self.ui.spb_trappedROI_width.setValue(configure_settings.get('spb_trappedROI_width', 0))
            self.ui.spb_trappedROI_height.setValue(configure_settings.get('spb_trappedROI_height', 0))
            # 设置 Function Measurement ROI 模块。旧 elasticity 配置键继续兼容读取。
            self.functionROI_X = configure_settings.get(
                'spb_sCMOS_functionROI_X',
                configure_settings.get('spb_sCMOS_elasticityMeasurementROI_X', 0),
            )
            self.functionROI_Y = configure_settings.get(
                'spb_sCMOS_functionROI_Y',
                configure_settings.get('spb_sCMOS_elasticityMeasurementROI_Y', 0),
            )
            self.functionROI_width = configure_settings.get(
                'spb_sCMOS_functionROI_width',
                configure_settings.get('spb_sCMOS_elasticityMeasurementROI_width', 70),
            )
            self.functionROI_height = configure_settings.get(
                'spb_sCMOS_functionROI_height',
                configure_settings.get('spb_sCMOS_elasticityMeasurementROI_height', 70),
            )
            # 设置 cell flow rate ROI 模块
            self.ui.spb_cellFlowThroughROI_X.setValue(configure_settings.get('spb_releaseROI_X', 0))
            self.ui.spb_cellFlowThroughROI_Y.setValue(configure_settings.get('spb_releaseROI_Y', 0))
            self.ui.spb_cellFlowThroughROI_width.setValue(configure_settings.get('spb_releaseROI_width', 320))
            self.ui.spb_cellFlowThroughROI_height.setValue(configure_settings.get('spb_releaseROI_height', 40))
            # 设置Collected Cells ROI
            self.ui.spb_collectedROI_X.setValue(configure_settings.get('spb_collectedCellsROI_X', 0))
            self.ui.spb_collectedROI_Y.setValue(configure_settings.get('spb_collectedCellsROI_Y', 0))
            self.ui.spb_collectedROI_width.setValue(configure_settings.get('spb_collectedCellsROI_width', 0))
            self.ui.spb_collectedROI_height.setValue(configure_settings.get('spb_collectedCellsROI_height', 0))
            self.ui.spb_collectedROI_angle.setValue(configure_settings.get('spb_collectedCellsROI_angle', 0))
            #设置Flow Rate Detection ROI
            self.ui.spb_flowRateROI_X.setValue(configure_settings.get('spb_flowRateROI_X', 0))
            self.ui.spb_flowRateROI_Y.setValue(configure_settings.get('spb_flowRateROI_Y', 0))
            self.ui.spb_flowRateROI_width.setValue(configure_settings.get('spb_flowRateROI_width', 0))
            self.ui.spb_flowRateROI_height.setValue(configure_settings.get('spb_flowRateROI_height', 0))
            self.ui.cmb_objective.setCurrentIndex(configure_settings.get('cmb_objective', 0))
            self.ui.spb_chipChannel_width.setValue(configure_settings.get('spb_chipChannel_width', 0))
            self.ui.spb_chipChannel_height.setValue(configure_settings.get('spb_chipChannel_height', 0))   
            self.ui.spb_flowRateDetectFramesNumber.setValue(configure_settings.get('spb_flowRateDetectFramesNumber', 0))        
            # 设置 Trigger Control 模块
            self.ui.spb_triggerCapture_time.setValue(configure_settings.get('spb_triggerCapture_time', 1))
            self.ui.spb_triggerReleaseSort_time.setValue(
                configure_settings.get('spb_triggerReleaseSort_time', configure_settings.get('spb_triggerSort_time', 1))
            )
            self.ui.spb_triggerRelease_time.setValue(configure_settings.get('spb_triggerRelease_time', 1))
            self.ui.spb_triggerFunction_time.setValue(
                configure_settings.get(
                    'spb_triggerFunction_time',
                    configure_settings.get('spb_triggerElasticityMeasurement_time', 1),
                )
            )
            
            # 设置 Image Processing Settings 模块
            self.ui.spb_minArea.setValue(configure_settings.get('spb_minArea', 120))
            self.ui.spb_maxArea.setValue(configure_settings.get('spb_maxArea', 300))
            self.function_min_delta_lenth = configure_settings.get('spb_minLenth', self.function_min_delta_lenth)
            self.function_max_delta_lenth = configure_settings.get('spb_maxLenth', self.function_max_delta_lenth)
            morphology_size = configure_settings.get('spb_sCMOS_morphologyKernel_size', 13)
            self.sCMOS_Bi_threshold = configure_settings.get('spb_threshold_Bi_sCMOS', 150)
            self.sCMOS_openTimes = configure_settings.get('spb_sCMOS_openTimes', 1)
            self.sCMOS_closeTimes = configure_settings.get('spb_sCMOS_closeTimes', 1)
            self.sCMOS_morphologyKernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (morphology_size, morphology_size))
            self.sCMOS_minArea = configure_settings.get('spb_sCMOS_minArea', self.sCMOS_minArea)

            self.ui.spb_trapFrames.setValue(
                configure_settings.get('spb_trapFrames', configure_settings.get('spb_trappedIntervalTime', 30))
            )
            self.ui.spb_trapBalance_Time.setValue(configure_settings.get('spb_trapBalance_Time', 300))
            self.ui.spb_collectFrames.setValue(
                configure_settings.get('spb_collectFrames', configure_settings.get('spb_sortFrames', 50))
            )

            # 设置 Binary ROI 模块
            self.ui.spb_threshold_Bi.setValue(configure_settings.get('spb_threshold_Bi', 120))
            MainWindow.apply_loaded_sim_settings_payload(
                self,
                configure_settings,
                apply_legacy_sim_camera_settings=apply_legacy_sim_camera_settings,
            )

            
            # 加载其他参数...
            # 仅在手动加载时弹出提示
            if self.is_auto_loading:  # 需要定义一个标志位区分自动/手动加载
                self.is_auto_loading = True
                qw.QMessageBox.information(self, "Succeed", "Loaded Successfully!")
                
            self.update_image_processing_para()
            
        except Exception as e:
            error_msg = f"加载配置失败：{str(e)}"
            qw.QMessageBox.critical(self, "错误", error_msg)
        finally:
            self._loading_configure_settings = previous_loading_state
    #保存实验参数TXT
    def btn_saveExperimentInfo_function(self):
        # 获取文本内容
        text = self.ui.pte_experiment_infomation.toPlainText()

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-4]  # 去掉最后三位微秒，保留到毫秒
        folder_name = str(time.strftime("%Y%m%d"))+"_video"
        # 创建日期文件夹
        try:
            # 创建日期文件夹（如果不存在）
            os.makedirs(folder_name, exist_ok=True)
            
            # 构建完整文件路径
            filename = f"experiment_info_{timestamp}.txt"
            file_path = os.path.join(folder_name, filename)
            
            # 写入文件（使用UTF-8编码）
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(text)
                
            print(f"实验信息已成功保存到: {file_path}")
            
            # 如果需要，可以在这里添加成功提示框
            # QMessageBox.information(self, "保存成功", f"实验信息已保存到:\n{file_path}")
            
        except Exception as e:
            print(f"保存实验信息时出错: {str(e)}")
            # 如果需要，可以在这里添加错误提示框
            # QMessageBox.critical(self, "保存失败", f"保存文件时出错:\n{str(e)}")
    # 图像处理区域的模块 
    # ROI细胞测速
    def btn_enterSettingPara_flowRateDetection_function(self):
        if self.btn_enterSettingPara_flowRateDetection_state:
            self.ui.btn_enterSettingPara_flowRateDetection.setStyleSheet("background-color: #E1E1E1")
            self.btn_enterSettingPara_flowRateDetection_state = False
            self.ui.cmb_objective.setEnabled(False)
            self.ui.spb_chipChannel_width.setEnabled(False)
            self.ui.spb_chipChannel_height.setEnabled(False)
            self.ui.spb_flowRateDetectFramesNumber.setEnabled(False)
        else:
            self.ui.btn_enterSettingPara_flowRateDetection.setStyleSheet("background-color: #4EEE94")
            self.btn_enterSettingPara_flowRateDetection_state = True
            self.ui.cmb_objective.setEnabled(True)
            self.ui.spb_chipChannel_width.setEnabled(True)
            self.ui.spb_chipChannel_height.setEnabled(True)
            self.ui.spb_flowRateDetectFramesNumber.setEnabled(True)  
    @pyqtSlot()         
    def slot_btn_flowRateImageProcessing_function(self):
        if self.btn_flowRateImageProcessing_state:
            self.signal_setImageProcessingWay_UIThread.emit(-1)  #关闭ROI图像处理  
            self.btn_flowRateImageProcessing_state = False
            self.ui.btn_flowRateImageProcessing.setStyleSheet("background-color: #E1E1E1")            
        else:
            self.signal_setImageProcessingWay_UIThread.emit(5)
            self.ui.btn_flowRateImageProcessing.setStyleSheet("background-color: #4EEE94")
            self.btn_flowRateImageProcessing_state = True
    
    # ROI细胞筛选
    def btn_enterImageProcessingModel_function(self):
        """管理图像处理模块"""
        if self.btn_enterImageProcessingModel_state:
           self.FastCameraThread.camera_worker.enterImageProcessor_state = False  #退出图像识别的筛选模式
           self.btn_enterImageProcessingModel_state = False
           self.ui.btn_enterImageProcessingModel.setStyleSheet("background-color: #E1E1E1")
           self.ui.spb_maxArea.setEnabled(False)
           self.ui.spb_minArea.setEnabled(False)

           self.ui.spb_trapFrames.setEnabled(False)
           self.ui.spb_trapBalance_Time.setEnabled(False)
           self.ui.spb_collectFrames.setEnabled(False)
           self.ui.btn_runScreenCell_single.setEnabled(False)
           self.ui.btn_runScreenCell_continue.setEnabled(False)  
                  
           if self.btn_runScreenCell_continue_state:
               self.btn_runScreenCell_continue_function()
           if self.btn_runScreenCell_single_state:
               self.slot_btn_runScreenCell_single_function()
        else:
            self.FastCameraThread.camera_worker.enterImageProcessor_state = True  #进入图像识别的筛选模式
            self.btn_enterImageProcessingModel_state = True
            self.ui.btn_enterImageProcessingModel.setStyleSheet("background-color: #4EEE94")
            self.ui.btn_runScreenCell_continue.setEnabled(True)
            self.ui.btn_runScreenCell_single.setEnabled(True)
            self.ui.spb_maxArea.setEnabled(True)
            self.ui.spb_minArea.setEnabled(True)
            self.ui.spb_trapFrames.setEnabled(True)
            self.ui.spb_trapBalance_Time.setEnabled(True)
            self.ui.spb_collectFrames.setEnabled(True)

    def btn_runScreenCell_continue_function(self):
        "自动化连续运行整个筛选细胞的流程"
        # single和continue筛选只能存在一个
        if self.btn_runScreenCell_single_state:
            self.slot_btn_runScreenCell_single_function()

        if self.btn_runScreenCell_continue_state:
            self.btn_runScreenCell_continue_state = False
            self.ui.btn_runScreenCell_continue.setStyleSheet("background-color: #E1E1E1")
            self.signal_setImageProcessingWay_UIThread.emit(-1)  #关闭ROI图像处理          
        else:
            self.btn_runScreenCell_continue_state = True
            self.ui.btn_runScreenCell_continue.setStyleSheet("background-color: #4EEE94")
            self.signal_setImageProcessingWay_UIThread.emit(0) #开始进入capture ROI的筛选

    @pyqtSlot()
    def slot_btn_runScreenCell_single_function(self):
        """单次筛选只进行一次细胞捕获"""
        """非目标细胞则releaseTrigger后结束"""
        """目标细胞则往后执行到collectedROI分析,然后结束"""
        # single和continue筛选只能存在一个
        if self.btn_runScreenCell_continue_state:
            self.btn_runScreenCell_continue_function()
        if self.btn_runScreenCell_single_state:
            self.btn_runScreenCell_single_state = False
            self.ui.btn_runScreenCell_single.setStyleSheet("background-color: #E1E1E1")
            self.FastCameraThread.image_processor.singleRun = False
            self.signal_setImageProcessingWay_UIThread.emit(-1)  #关闭ROI图像处理  
        else:
            self.btn_runScreenCell_single_state = True
            self.ui.btn_runScreenCell_single.setStyleSheet("background-color: #4EEE94")
            self.FastCameraThread.image_processor.singleRun = True
            self.signal_setImageProcessingWay_UIThread.emit(0) #开始进入capture ROI的筛选

    def btn_recountCell_number_function(self):
        "将统计的细胞数值归0"
        self.cell_ID = 0                   # 用来记录是哪次细胞的
        self.totalNumb_capture = 0
        self.totalNumb_trapped = 0
        self.totalNumb_relese = 0
        self.totalNumb_collected = 0
        self.totalNumb_functionMeasurement_start = 0
        self.totalNumb_functionMeasurement_end = 0
        self.trappedCell_miss  = 0
        self.collectedCell_miss  = 0
        self.functionMeasurement_start_miss = 0
        self.functionMeasurement_end_miss = 0
        self.flowRate_ID          = 0 
        self.roi_data_queue.clear()
        self.ui.spb_roiDataNumber.setValue(0)
        for roi_key in self.roi_display_lb:
            for label in self.roi_display_lb[roi_key]:
                # 跳过占位符控件 lb_none
                if label is self.ui.lb_none:
                    continue
                # 将 QLabel 的文本设置为空
                label.setText("")  # 或者设置为 "NULL" 字符串 label.setText("NULL")

    def btn_exportROIData_function(self):
        """导出数据主函数"""
        with self.lock:
            if not self.roi_data_queue:
                # 显示空队列提示
                root = tk.Tk()
                root.withdraw()
                messagebox.showinfo("提示", "队列为空，无数据可处理")
                root.destroy()
                return

            try:
                # 生成带时间戳的文件名
                timestamp = time.strftime("%H%M%S")
                filename = f"ROI_recordData_{timestamp}.csv"
                filepath = self.output_dir / filename
                
                # 确保输出目录存在
                self.output_dir.mkdir(parents=True, exist_ok=True)
                
                # 转换为DataFrame并保存
                pd.DataFrame(self.roi_data_queue).to_csv(
                    filepath,
                    index=False,
                    encoding='utf-8-sig'  # 修正后的参数
                )
                
                # 清空队列
                self.roi_data_queue.clear()
                
                # 显示保存成功提示
                root = tk.Tk()
                root.withdraw()
                messagebox.showinfo("导出成功", f"数据已保存至:\n{filepath}")
                root.destroy()

            except Exception as e:
                # 错误处理
                root = tk.Tk()
                root.withdraw()
                messagebox.showerror("导出失败", f"保存失败:\n{str(e)}")
                root.destroy()

    def btn_saveROIImage_function(self):
        "自动化运行整个筛选细胞的流程"
        "开始的话自动点击capture按钮,关闭的话，结束ROI筛选"
        if self.btn_saveROIImage_state:
            self.btn_saveROIImage_state = False
            self.ui.btn_saveROIImage.setStyleSheet("background-color: #E1E1E1")            
        else:
            self.btn_saveROIImage_state = True
            self.ui.btn_saveROIImage.setStyleSheet("background-color: #4EEE94")
    "更新UI所有控制参数"
    def update_image_processing_para(self):
        "将capture ROI的Y和高与cell flow through绑定"
        self.ui.spb_captureROI_Y.setValue(self.ui.spb_cellFlowThroughROI_Y.value())
        self.ui.spb_captureROI_height.setValue(self.ui.spb_cellFlowThroughROI_height.value())
        # Function Measurement ROI 由 SIM 主链路/当前内部参数提供，当前 UI 不新增独立排版控件。
        """将图像处理的参数发给fastCamera图像处理线程"""
        para = {}
        # image processing settings
        para["threshold_Bi"]            = self.ui.spb_threshold_Bi.value()
        para["minArea"]                 = self.ui.spb_minArea.value()
        para["maxArea"]                 = self.ui.spb_maxArea.value()
        para["collectFrames"]           = self.ui.spb_collectFrames.value()
        para["trapFrames"]              = self.ui.spb_trapFrames.value()
        para["displayGray_max"]         = self.ui.spb_fastCamera_displayGray_max.value()
        
        # capture ROI
        para["captureROI_X"]            = self.ui.spb_captureROI_X.value()
        para["captureROI_width"]        = self.ui.spb_captureROI_width.value()

        # trapped ROI
        para["trappedROI_X"]            = self.ui.spb_trappedROI_X.value()
        para["trappedROI_Y"]            = self.ui.spb_trappedROI_Y.value()
        para["trappedROI_width"]        = self.ui.spb_trappedROI_width.value()
        para["trappedROI_height"]       = self.ui.spb_trappedROI_height.value()
        para["displayGray_max"]         = self.ui.spb_fastCamera_displayGray_max.value()
        
        # cell Flow Through ROI
        para["cellFlowThroughROI_X"]            = self.ui.spb_cellFlowThroughROI_X.value()
        para["cellFlowThroughROI_Y"]            = self.ui.spb_cellFlowThroughROI_Y.value()
        para["cellFlowThroughROI_width"]        = self.ui.spb_cellFlowThroughROI_width.value()
        para["cellFlowThroughROI_height"]       = self.ui.spb_cellFlowThroughROI_height.value()
        # Hidden compatibility values for legacy FastCameraThread way 3; no UI controls feed these fields.
        para["sortROI_X"]               = 0
        para["sortROI_Y"]               = 0
        para["sortROI_width"]           = 1
        para["sortROI_height"]          = 1

        # collected Cells ROI
        para["collectedROI_X"]               = self.ui.spb_collectedROI_X.value()
        para["collectedROI_Y"]               = self.ui.spb_collectedROI_Y.value()
        para["collectedROI_width"]           = self.ui.spb_collectedROI_width.value()
        para["collectedROI_height"]          = self.ui.spb_collectedROI_height.value()
        para["collectedROI_angle"]           = self.ui.spb_collectedROI_angle.value()

        # flowRate ROI
        para["flowRateROI_X"]               = self.ui.spb_flowRateROI_X.value()
        para["flowRateROI_Y"]               = self.ui.spb_flowRateROI_Y.value()
        para["flowRateROI_width"]           = self.ui.spb_flowRateROI_width.value()
        para["flowRateROI_height"]          = self.ui.spb_flowRateROI_height.value()
        para["flowRateScanFrames"]          = self.ui.spb_flowRateDetectFramesNumber.value()

        para["chipChannel_width"]           = self.ui.spb_chipChannel_width.value()   
        para["chipChannel_height"]          = self.ui.spb_chipChannel_height.value()
        para["cameraPixelSize"]             = 9 #9μm for MV-XG51GM-T fastCamera
        #图像保存模块（主要是missEventVideoSave）
        para["missEventSavePreFrames"]      = self.ui.spb_missEventSavePreFrames.value() # miss event 保存的帧数，当前帧往前数 
        para["missEventVideoSaveModel"]     = self.btn_missEventVideoSaveModel_state

        if self.ui.cmb_objective.currentIndex() == 0:
            para["objective_magnification"] = 10 # 10X物镜
        elif self.ui.cmb_objective.currentIndex() == 1:
            para["objective_magnification"] = 20 # 20X物镜

        #每当有参数接收就重新发送信号给图像处理线程，改变其参数
        self.signal_sendImageProcessingPara.emit(para)

        "将图像处理的参数发给sCMOS图像处理线程"

        sCMOS_para = {}
        sCMOS_para["minDeltaLenth"]           = self.function_min_delta_lenth
        sCMOS_para["maxDeltaLenth"]           = self.function_max_delta_lenth
        sCMOS_para["sCMOS_gaussianKernel"]    = self.sCMOS_gaussianKernel 
        sCMOS_para["sCMOS_gaussBlurSigma"]    = self.sCMOS_gaussBlurSigma 

        sCMOS_para["threshold_Bi_sCMOS"]      = self.sCMOS_Bi_threshold
        sCMOS_para["morphologyKernel"]        = self.sCMOS_morphologyKernel
        sCMOS_para["openTimes"]               = self.sCMOS_openTimes
        sCMOS_para["closeTimes"]              = self.sCMOS_closeTimes
        sCMOS_para["sCMOS_minArea"]           = self.sCMOS_minArea
        sCMOS_para["displayGray_max"]         = self.ui.spb_sCMOS_displayGray_max.value()
        
        # Function Measurement ROI
        sCMOS_para["functionROI_X"]         = self.functionROI_X
        sCMOS_para["functionROI_Y"]         = self.functionROI_Y
        sCMOS_para["functionROI_width"]     = self.functionROI_width
        sCMOS_para["functionROI_height"]    = self.functionROI_height

        self.signal_sendImageProcessingPara_sCMOS.emit(sCMOS_para)
        #图像保存的名称信息
        experiment_imfo = {}
        # 只有btn_ensurePresureInfo为真时，才能保存压力参数作为文件名
        experiment_imfo["spb_triggerCapture_time"] = str(self.ui.spb_triggerCapture_time.value())
        experiment_imfo["spb_triggerFunction_time"] = str(self.ui.spb_triggerFunction_time.value())
        experiment_imfo["spb_triggerRelease_time"] = str(self.ui.spb_triggerRelease_time.value())
        experiment_imfo["spb_triggerReleaseSort_time"] = str(self.ui.spb_triggerReleaseSort_time.value())
        experiment_imfo["spb_cellSpeedValue"] = str(self.ui.spb_cellSpeedValue.value())
        experiment_imfo["spb_preTriggerBuffer"] = str(self.ui.spb_preTriggerBuffer.value())
        experiment_imfo["spb_sCMOS_preTriggerBuffer"] = str(self.ui.spb_sCMOS_preTriggerBuffer.value())
        experiment_imfo["fastCameraMaxGrayscale"] =  self.ui.spb_fastCamera_displayGray_max.value()        
        #发送实验参数给视频保存模块
        #图像命名规则："time"_"fps"_"triggerTime_(c_r_s)"_"presure_(b1_c_b2_t_v)"
        self.signal_sendExperimentImfo.emit(experiment_imfo)

        #...................................
        #参数为：1.保存图像的名称；2.显示原始ROI图像的lb控件；3.显示形态学处理过的二值化ROI图像的lb控件；4显示勾勒目标外轮廓的lb控件
        self.roi_display_widgets = {
            0: ("Capture_ROI",self.ui.lb_cellFlowThroughROIView_original,self.ui.lb_cellFlowThroughROIView_processed,self.ui.lb_cellFlowThroughROIView_target),
            1: ("Trapped_ROI",self.ui.lb_trappedROIView_original,self.ui.lb_trappedROIView_processed,self.ui.lb_trappedROIView_target),
            2: ("Release_ROI",self.ui.lb_cellFlowThroughROIView_original,self.ui.lb_cellFlowThroughROIView_processed,self.ui.lb_cellFlowThroughROIView_target),
            4: ("Collected_ROI",self.ui.lb_collectedROIView_original,self.ui.lb_collectedROIView_processed,self.ui.lb_collectedROIView_target),
            5: ("flowRate_ROI_start",self.ui.lb_flowRateDetectROIView_original_start,self.ui.lb_flowRateDetectROIView_processed_start,self.ui.lb_flowRateDetectROIView_target_start),
            6: ("flowRate_ROI_end"  ,self.ui.lb_flowRateDetectROIView_original_end  ,self.ui.lb_flowRateDetectROIView_processed_end  ,self.ui.lb_flowRateDetectROIView_target_end),
        }
        #参数为0时-capture:1.ID; 2.area; 3.X; 4.Y; 5.total; 6.算法时间; 7. 间隔时间;
        #参数为1时-capture:1.ID; 2.area; 3.total; 4.miss;  5.ROI_result-state;
        #参数为2时-capture:1.ID;  2.total; 3.算法时间; 4. 间隔时间; 
        #参数为4时-capture:1.ID; 2.area; 3.X; 4.Y; 5.total; 6.miss;  7.算法时间; 8. 间隔时间; 9.ROI_result-state;
        #参数为5时-capture:1.ID; 2.area; 3.X; 
        #参数为6时-capture:1.ID; 2.area; 3.X; 4. 间隔时间; 5.ROI_result-state;
        #7代表Function Measurement ROI分析的参数。1.ID; 2.total; 3.miss; 4.state; 5.Lenth(pixel)就是轮廓最低点的y位置;
        self.roi_display_lb = {
            0: (self.ui.lb_captureCell_id,self.ui.lb_captureCellArea, self.ui.lb_captureCell_X,self.ui.lb_captureCell_Y,self.ui.lb_captureCell_total,self.ui.lb_captureCell_algorithmTime,self.ui.lb_captureCell_intervalTime),
            1: (self.ui.lb_trappedCell_id,self.ui.lb_trappedCellArea,self.ui.lb_trappedCell_total,self.ui.lb_trappedCell_miss,self.ui.lb_trappedROI_state),
            2: (self.ui.lb_releaseCell_id,self.ui.lb_releaseCell_total,self.ui.lb_releaseCell_algorithmTime,self.ui.lb_releaseCell_intervalTime),
            4: (self.ui.lb_collectedCell_id,self.ui.lb_collectedCellArea, self.ui.lb_collectedCell_X,self.ui.lb_collectedCell_Y,self.ui.lb_collectedCell_total,self.ui.lb_collectedCell_miss,self.ui.lb_collectiveCell_algorithmTime,self.ui.lb_collectedCell_intervalTime,self.ui.lb_collectedROI_state),
            5: (self.ui.lb_flowRateCell_id_start,self.ui.lb_flowRateCell_id_start, self.ui.lb_flowRateDetectionCell_X_start),
            6: (self.ui.lb_flowRateCell_id_end,self.ui.lb_flowRateDetectionCellArea_end, self.ui.lb_flowRateDetectionCell_X_end,self.ui.lb_flowRateDetectionCell_end_intervalTime,self.ui.lb_flowRateROI_state),
        }
        # 二值化阈值
        self.threshold_Bi = self.ui.spb_threshold_Bi.value()
        self.kernel = np.ones((3,3), np.uint8) #形态学处理的卷积核
        "Capture 和 release 公用cellFlowThrough ROI作为判断依据"
        "Capture时需要只有一个细胞在Capture Area中才行"
        "release时需要cellFlowThrough中无细胞中"
         # cell Flow Through ROI
        self.cellFlowThroughROI_X       = self.ui.spb_cellFlowThroughROI_X.value()
        self.cellFlowThroughROI_Y       = self.ui.spb_cellFlowThroughROI_Y.value()
        self.cellFlowThroughROI_width   = self.ui.spb_cellFlowThroughROI_width.value()
        self.cellFlowThroughROI_height  = self.ui.spb_cellFlowThroughROI_height.value()      
        # capture ROI
        self.captureROI_X       = self.ui.spb_captureROI_X.value()
        self.captureROI_width   = self.ui.spb_captureROI_width.value()
        # trapped ROI
        self.trappedROI_X       = self.ui.spb_trappedROI_X.value()
        self.trappedROI_Y       = self.ui.spb_trappedROI_Y.value()
        self.trappedROI_width   = self.ui.spb_trappedROI_width.value()
        self.trappedROI_height  = self.ui.spb_trappedROI_height.value()
        # collected ROI
        self.collectedROI_X = self.ui.spb_collectedROI_X.value()
        self.collectedROI_Y = self.ui.spb_collectedROI_Y.value()
        self.collectedROI_width = self.ui.spb_collectedROI_width.value()
        self.collectedROI_height = self.ui.spb_collectedROI_height.value()
        self.collectedROI_angle = self.ui.spb_collectedROI_angle.value()
        # flow rate ROI
        self.flowRateROI_X = self.ui.spb_flowRateROI_X.value()
        self.flowRateROI_Y = self.ui.spb_flowRateROI_Y.value()
        self.flowRateROI_width  = self.ui.spb_flowRateROI_width.value()
        self.flowRateROI_height = self.ui.spb_flowRateROI_height.value()

        self.roi_way = {
            0: (self.captureROI_X+self.cellFlowThroughROI_X, self.cellFlowThroughROI_Y, self.captureROI_width, self.cellFlowThroughROI_height),
            1: (self.trappedROI_X, self.trappedROI_Y, self.trappedROI_width, self.trappedROI_height),
            2: (self.cellFlowThroughROI_X, self.cellFlowThroughROI_Y, self.cellFlowThroughROI_width, self.cellFlowThroughROI_height),
            4: (self.collectedROI_X, self.collectedROI_Y, self.collectedROI_width, self.collectedROI_height),
            5: (self.flowRateROI_X, self.flowRateROI_Y, self.flowRateROI_width, self.flowRateROI_height), #start flowRateROI 
            6: (self.flowRateROI_X, self.flowRateROI_Y, self.flowRateROI_width, self.flowRateROI_height), #start flowRateROI 
        }
        self.roi_angles = {
            4: self.collectedROI_angle,
        }
        self.roi_diff_disply_lb = {
            1:(self.ui.lb_trappedROIView_BgDiff_Bi),
            2:(self.ui.lb_cellFlowThroughROIView_BgDiff_Bi),
            4:(self.ui.lb_collectedROIView_BgDiff_Bi),
        } 
   
  
    # 提取背景图片
    def btn_backgroundExtract_function(self):
        """背景提取功能按钮:
           把显示的图像抓出来,复制给给创建的背景图像变量,具体操作在slot_updata_displayFastCameraFrameIndex_FPS()函数中;
           并将背景图片发送给图像处理线程"""
        self.btn_backgroundExtract_state = True
        self.update_image_processing_para()
    # 可视化ROI按钮
    def btn_cellFlowThroughROI_view_function(self):
        """cellFlowThrough ROI启动按钮功能"""
        if self.btn_cellFlowThroughROI_view_state:
            self.btn_cellFlowThroughROI_view_state = False
            self.ui.btn_cellFlowThroughROI_view.setStyleSheet("background-color: #E1E1E1")
        else:
            self.btn_cellFlowThroughROI_view_state = True
            self.ui.btn_cellFlowThroughROI_view.setStyleSheet("background-color: #FFDC96")
    def btn_captureROI_view_function(self):
        """capture ROI启动按钮功能"""
        if self.btn_captureROI_view_state:
            self.btn_captureROI_view_state = False
            self.ui.btn_captureROI_view.setStyleSheet("background-color: #E1E1E1")
        else:
            self.btn_captureROI_view_state = True
            self.ui.btn_captureROI_view.setStyleSheet("background-color: #90EE90")  
    def btn_trappedROI_view_function(self):
        """trapped ROI启动按钮功能"""
        if self.btn_trappedROI_view_state:
            self.btn_trappedROI_view_state = False
            self.ui.btn_trappedROI_view.setStyleSheet("background-color: #E1E1E1")
        else:
            self.btn_trappedROI_view_state = True
            self.ui.btn_trappedROI_view.setStyleSheet("background-color: #FFCCCC")

    def btn_collectedROI_view_function(self):
        """collected cells ROI启动按钮功能"""
        if self.btn_collectedROI_view_state:
            self.btn_collectedROI_view_state = False
            self.ui.btn_collectedROI_view.setStyleSheet("background-color: #E1E1E1")
        else:
            self.btn_collectedROI_view_state = True
            self.ui.btn_collectedROI_view.setStyleSheet("background-color: #E7DDFF")   
    def btn_flowRateROI_view_function(self):
        """flow rate ROI 启动按钮功能"""
        if self.btn_flowRateROI_view_state:
            self.btn_flowRateROI_view_state = False
            self.ui.btn_flowRateROI_view.setStyleSheet("background-color: #E1E1E1")
        else:
            self.btn_flowRateROI_view_state = True
            self.ui.btn_flowRateROI_view.setStyleSheet("background-color: #BBFFFF")  # 255 255 187 BGR

    # Hardvare Connection 模块
    def btn_refresh_camera_function(self):
        """扫描并更新串口列表"""
        # 获取当前所有串口
        available_ports = QSerialPortInfo.availablePorts()
        new_ports = [port.portName() for port in available_ports]
        # 当检测到串口数量变化时更新
        if len(self.portName) != len(new_ports) or set(self.portName) != set(new_ports):
            self.portName = new_ports
            self.ui.cmb_singleChip.clear()
            self.ui.cmb_singleChip.addItems(self.portName)
        """"扫描超快相机"""                                                   
        #先清理再扫描
        self.ui.cmb_fastCamera.clear()
        #扫描所有MindVision相机
        try:
            self.cameraList = mvsdk.CameraEnumerateDevice()
            number_camera = len(self.cameraList)
            if number_camera < 1:
                print("No camera was found!")
                return
        
            for i , cameraInfo in enumerate(self.cameraList):
                name =  str(cameraInfo.GetProductName())
                self.ui.cmb_fastCamera.addItem(name)
                print(f"找到 {len(self.cameraList)} 个相机")
        except Exception as e:
            print(f"设备枚举失败: {str(e)}")
    def spb_ROI_value_changed_function(self):
        """强行将ROI设置在合法范围"""
        self.ui.spb_pixelWidth.setValue(8*(math.floor(self.ui.spb_pixelWidth.value()/8)))         
        self.ui.spb_pixelHeight.setValue(2*(math.floor(self.ui.spb_pixelHeight.value()/2)))

    def btn_sCMOS_connection_function(self):
        self.ensure_sim_runtime()
        if self.sim_camera_connected:
            self.sim_preview_restart_timer.stop()
            self.sim_preview_requested = False
            self.sim_preview_restart_requested = False
            if self.sim_preview_active or self.sim_preview_stop_in_progress:
                self.stop_sim_preview(wait=True)
            try:
                self.sim_acquisition_controller.disconnect_camera()
            except Exception as e:
                print(f"SIM camera disconnect failed: {str(e)}")
                qw.QMessageBox.warning(self, "SIM Camera", str(e))
                return
            self.sim_runtime_timing_snapshot = {}
            self.sim_camera_connected = False
            MainWindow.refresh_sim_camera_bit_depth_choices(self, [SIM_BIT_DEPTH_DEFAULT])
            self.set_sim_camera_controls_enabled(False)
            self.update_sim_camera_action_buttons()
            return

        if not self.sim_available_cameras:
            self.refresh_sim_camera_devices(show_dialog_on_error=True)
        if not self.sim_available_cameras:
            qw.QMessageBox.information(self, "SIM Camera", "No SIM camera was found. Please refresh the device list.")
            return

        selected_combo_index = self.ui.cmb_sCMOS_camera.currentIndex()
        if selected_combo_index < 0 or selected_combo_index >= len(self.sim_available_cameras):
            qw.QMessageBox.information(self, "SIM Camera", "Please select a SIM camera before connecting.")
            return

        selected_camera = self.sim_available_cameras[selected_combo_index]
        self.sim_app_config.camera.device_index = int(selected_camera.get("index", 0))
        self.sim_app_config.camera.device_label = str(selected_camera.get("display", ""))
        self.sync_sim_camera_config_from_ui(save_to_disk=False)
        device_index = self.sim_app_config.camera.device_index
        device_label = self.sim_app_config.camera.device_label
        camera_config_snap = copy.copy(self.sim_app_config.camera)
        controller = self.sim_acquisition_controller
        config_path = self.sim_app_config.config_path

        def _cam_connect_fn():
            connection_info = controller.connect_camera(
                device_index=device_index,
                device_label=device_label,
            )
            camera_result = controller.apply_camera_config(camera_config_snap) or {}
            if not isinstance(camera_result, dict):
                camera_result = {}
            supported_bit_depths = camera_result.get("supported_bit_depths")
            if not supported_bit_depths and isinstance(connection_info, dict):
                supported_bit_depths = connection_info.get("supported_bit_depths")
            runtime_payload = {
                "camera_config": dict(getattr(camera_config_snap, "__dict__", {})),
                **dict(camera_result),
            }
            if supported_bit_depths and "supported_bit_depths" not in runtime_payload:
                runtime_payload["supported_bit_depths"] = supported_bit_depths
            return runtime_payload

        if getattr(self, "_cam_connect_thread", None) is not None:
            return
        self.ui.btn_sCMOS_connection.setEnabled(False)
        _cam_thread = QThread()
        _cam_worker = _ConnectWorker(_cam_connect_fn)
        _cam_worker.moveToThread(_cam_thread)
        # Keep strong references so the GC cannot collect live QThread/QObject.
        self._cam_connect_thread = _cam_thread
        self._cam_connect_worker = _cam_worker

        def _on_cam_success(runtime_payload):
            MainWindow.update_sim_runtime_timing_from_payload(self, runtime_payload)
            if {
                "applied_roi",
                "sensor_width",
                "sensor_height",
                "roi_step_px",
                "roi_size_presets",
            }.intersection(runtime_payload):
                save_app_config(self.sim_app_config, config_path)
            self.sim_camera_connected = True
            self.set_sim_camera_controls_enabled(True)

        def _on_cam_error(msg):
            try:
                controller.disconnect_camera()
            except Exception as cleanup_error:
                print(f"SIM camera cleanup after connection failure failed: {str(cleanup_error)}")
            print(f"SIM camera connection failed: {msg}")
            qw.QMessageBox.warning(self, "SIM Camera", msg)
            self.sim_camera_connected = False
            self.sim_preview_requested = False
            self.set_sim_camera_controls_enabled(False)

        def _on_cam_finished():
            _cam_thread.quit()
            _cam_thread.wait()
            self._cam_connect_thread = None
            self._cam_connect_worker = None
            self.ui.btn_sCMOS_connection.setEnabled(True)
            self.update_sim_camera_action_buttons()

        _cam_worker.signal_success.connect(_on_cam_success)
        _cam_worker.signal_error.connect(_on_cam_error)
        _cam_worker.signal_finished.connect(_on_cam_finished)
        _cam_thread.started.connect(_cam_worker.run)
        _cam_thread.start()

    def connection_camera_function(self):
        """连接选中的相机"""
        #选择相机
        index = self.ui.cmb_fastCamera.currentIndex()
        camera_info = self.cameraList[index]
        #读取相机参数
        camera_para = {} 
        camera_para["camera_info"] = camera_info                                        # 读取相机信息
        camera_para["exposure_time"] = self.ui.spb_exposureTime.value()                 # 曝光时间ms
        camera_para["ring_buffer_capacity"] = self.ui.spb_ringBufferCapacity.value()    # 设置的环形缓冲的帧数
        camera_para["pre_trigger"] = self.ui.spb_preTriggerBuffer.value()               # trigger前帧数
        camera_para["post_trigger"] = self.ui.spb_postTriggerBuffer.value()             # trigger后帧数       
        camera_para["pixel_height"] = self.ui.spb_pixelHeight.value()                   # ROI的高度
        camera_para["pixel_width"] = self.ui.spb_pixelWidth.value()                     # ROI的宽度
        camera_para["frameRate"] = self.ui.spb_frameRate.value()                        # 帧率
        camera_para["cameraGain"] = self.ui.spb_cameraGain.value()                      # 增益
        # 创建一个默认255亮度的灰度图像,后续被替换用于存放细胞轮廓提取的背景图片
        self.background_frame = None
        try:
            # 创建并启动工作线程
            #将相机相关信息发送给子线程
            self.FastCameraThread = FastCameraThread.FastCameraThread(camera_para)
            #连接显示相机图像的信号
            self.FastCameraThread.camera_worker.signal_send_FastCameraFrameIndex_FPS.connect(self.slot_updata_display_fastCameraFrameIndex_FPS)
            #保连接保存完成的信号  开始保存发送 0 ，保存结束发送 1 FastCamera
            self.FastCameraThread.video_saver.signal_finished_triggerSaveVideo.connect(self.slot_handle_save_video_finished)
            #保连接保存完成的信号  开始保存发送 0 ，保存结束发送 1 sCMOS
            #self.sCMOSCameraThread.sCMOS_video_saver.signal_finished_triggerSaveVideo.connect(self.slot_handle_sCMOS_save_video_finis
            #将图像处理的参数发送和图像处理线程的接收函数连接
            self.signal_sendImageProcessingPara.connect(self.FastCameraThread.image_processor.slot_image_processing_parameters)
            #先发送一次参数
            #连接图像处理完和显示的信号
            self.FastCameraThread.image_processor.signal_processedROIImage_for_view.connect(self.slot_display_processed_ROI_image)
            #这是手动选择是否为目标细胞
            self.signal_isTarget.connect(self.MCUTriggerThread.worker.slot_finish_ROI_processing)

            #连接连续筛选的信号
            self.FastCameraThread.image_processor.signal_finishROIProcessing.connect(self.MCUTriggerThread.worker.slot_finish_ROI_processing)
            #将单片机的trigger信号和视频保存联系在一起
            self.MCUTriggerThread.worker.signal_sendTriggerforVideoSaving.connect(self.btn_triggerSaveVideo_function)
            self.MCUTriggerThread.worker.signal_sendTriggerforVideoSaving_sCMOS.connect(self.btn_sCMOS_videoSave_function)
            #连接图像保存的实验参数信号
            self.signal_sendExperimentImfo.connect(self.FastCameraThread.video_saver.slot_receive_expeimentData_forVideoNamed)
            # 把singleRun和其关闭信号连接
            self.MCUTriggerThread.worker.signal_close_btn_runScreenCell_single_function.connect(self.slot_btn_runScreenCell_single_function)
            # 把singleRun和其关闭信号连接
            self.MCUTriggerThread.worker.signal_close_btn_flowRateImageProcessing_function.connect(self.slot_btn_flowRateImageProcessing_function)
            #连接MCU信号和图像处理状态
            self.MCUTriggerThread.worker.signal_setImageProcessingWay_MCUThread.connect(self.FastCameraThread.image_processor.slot_set_image_processing_way)

            self.signal_setImageProcessingWay_UIThread.connect(self.FastCameraThread.image_processor.slot_set_image_processing_way)
            #专门为了Delay Sort Trigger Model 使用，防止连续发送release和sort 信号导致单片机报错，所以通过信号转折一下

            self.MCUTriggerThread.worker.signal_forDelaySort.connect(self.FastCameraThread.image_processor.slot_forDelaySort)
            # 背景图片发送给图像处理线程
            self.signal_sendBackgroundFrame.connect(self.FastCameraThread.image_processor.slot_updateBackgroundFrame)
            # 关闭missEvent视频保存按钮
            self.FastCameraThread.image_processor.signal_closeMissEventVideoSave.connect(self.btn_missEventVideoSaveModel_function)

            self.signal_updataFastCamera_maxGray.connect(self.FastCameraThread.camera_worker.slot_updataFastCamera_maxGray)
            # 先发送一遍参数
            self.update_image_processing_para()
            self.updata_MCU_tirgger_para()
            # 最后在启动图像采集程序，这样就不会报错了
            self.FastCameraThread.start()

        except Exception as e:
            print(f"fast相机连接问题:{str(e)}")
            if self.FastCameraThread:
                self.FastCameraThread.stop()
    
    "更新sCMOS Function Measurement的图"
    @pyqtSlot()
    def slot_sCMOS_Updata_functionTrigger(self):
        try:
            self.trigger_sim_formal_acquisition("mcu_function_trigger")
        except Exception as e:
            print(f"sCMOS的Function索引发送转真: {str(e)}")

    "更新sCMOS Bg 图的"
    @pyqtSlot()
    def slot_sCMOS_BgUpdata_captureTrigger(self):
        try:
            print("SIM background update trigger received; no separate SIM background frame path is configured.")
        except Exception as e:
            print(f"sCMOS的Bg索引发送转真: {str(e)}")   
    
    @pyqtSlot()
    def slot_toggle_camera_connection(self):
        """切换相机的连接状态"""
        if self.FastCameraThread and self.FastCameraThread.isRunning():
            self.disconnection_camera_function()
        else:
            self.connection_camera_function()  
    def disconnection_camera_function(self):
        """断开连接的相机"""
        if self.FastCameraThread:
            self.FastCameraThread.stop()
            self.FastCameraThread = None
    @pyqtSlot()
    def slot_toggle_MCU_Camera_connection(self):
        """切换单片机的连接状态"""
        if self.MCUTriggerThread and self.MCUTriggerThread.isRunning():
            print("进入相机断开连接函数")
            self.disconnection_MCU_camera_function()
        else:
            print("进入相机连接函数")
            self.connection_MCU_camera_function()
    def connection_MCU_camera_function(self):
        """"连接选中的单片机"""
        #单片机串口的参数设置
        self.MCU_parameter = {}
        self.MCU_parameter['comPort'] = self.ui.cmb_singleChip.currentText()
        self.MCU_parameter['baud']  = "115200"
        self.MCU_parameter['data']  = "8"  
        self.MCU_parameter['stop']  = "1"
        self.MCU_parameter['check'] = "0"
        #创建单片机控制子线程的类
        self.MCUTriggerThread = MCUTriggerThread.MCUTriggerThread()
        #连接打开串口的信号和槽函数
        self.MCUTriggerThread.worker.signal_btn_portConnect.connect(self.MCUTriggerThread.worker.slot_btn_portConnect)
        #连接单片机串口打开状态的信号和槽函数
        self.MCUTriggerThread.worker.signal_btn_portConnect_MCU_state.connect(self.slot_btn_portConnect_MCU_state)
        #信号和槽函数的连接----trigger信号的连接
        #Capture信号的连接
        self.signal_btn_triggerCapture.connect(self.MCUTriggerThread.worker.slot_btn_triggerCapture)
        self.MCUTriggerThread.worker.signal_btn_triggerCapture_finish.connect(self.slot_btn_triggerCapture_finish)
        #Release信号连接
        self.signal_btn_triggerRelease.connect(self.MCUTriggerThread.worker.slot_btn_triggerRelease)
        self.MCUTriggerThread.worker.signal_btn_triggerRelease_finish.connect(self.slot_btn_triggerRelease_finish)
        #Function只作为等待时间: 自动筛选路径由MCU状态机等待, 手动按钮在UI线程本地等待。
        self.MCUTriggerThread.worker.signal_btn_triggerFunction_finish.connect(self.slot_btn_triggerFunction_finish)
        #Release+Sort信号连接
        self.signal_btn_triggerReleaseSort.connect(self.MCUTriggerThread.worker.slot_btn_triggerReleaseSort)
        self.MCUTriggerThread.worker.signal_btn_triggerReleaseSort_finish.connect(self.slot_btn_triggerReleaseSort_finish)
        #启动单片机子线程
        self.MCUTriggerThread.start()
        self.MCUTriggerThread.worker.signal_btn_portConnect.emit(self.MCU_parameter)
        self.update_image_processing_para()#调用一次参数
        #连接单片机trigger参数发送的信号
        self.signal_updataMCURecevieParameter.connect(self.MCUTriggerThread.worker.slot_MCU_updata_receive_parameter)
        #连接关闭视频保存模式的函数
        self.MCUTriggerThread.worker.signal_closeVideoSavingModel.connect(self.slot_closeVideoSavingModel)
        #连接手动rinse微流控管道的按钮
        self.signal_btn_rinseChannelCapture.connect(self.MCUTriggerThread.worker.slot_btn_rinseChannelCapture)
        self.signal_btn_rinseChannelSort.connect(self.MCUTriggerThread.worker.slot_btn_rinseChannelSort)
        self.signal_btn_rinseChannelRelease.connect(self.MCUTriggerThread.worker.slot_btn_rinseChannelRelease)
        self.signal_btn_rinseChannel_OFF.connect(self.MCUTriggerThread.worker.slot_btn_rinseChannel_OFF) 
        self.MCUTriggerThread.worker.signal_sCMOS_BgUpdata_captureTrigger.connect(self.slot_sCMOS_BgUpdata_captureTrigger)
        self.MCUTriggerThread.worker.signal_sCMOS_enterImageProcessor.connect(self.slot_sCMOS_Updata_functionTrigger)
        print("打开连接了MCU线程")
    def updata_MCU_tirgger_para(self):
        para = {}
        # 筛选模式
        if self.btn_runScreenCell_continue_state:
            para["runModel"] = 2 #连续筛选模式
        elif self.btn_runScreenCell_single_state:
            para["runModel"] = 1 #单次筛选模式
        else:
            para["runModel"] = 0 #非筛选模式

        # Trigger的参数。王波最新版 MCU 线程按 1 ms 单位接收。
        para["spb_triggerCapture_time"]                 = self.ui.spb_triggerCapture_time.value()
        para["spb_triggerFunction_time"]                = self.ui.spb_triggerFunction_time.value()
        para["spb_triggerRelease_time"]                 = self.ui.spb_triggerRelease_time.value()
        para["spb_triggerReleaseSort_time"]             = self.ui.spb_triggerReleaseSort_time.value()
        para["trapFrames"]                              = self.ui.spb_trapFrames.value()
        para["trapBalance_Time"]                        = self.ui.spb_trapBalance_Time.value()
        para["isTarget"]                                = self.ui.chb_isTarget.isChecked()
        self.signal_updataMCURecevieParameter.emit(para)
    def disconnection_MCU_camera_function(self):
        """断开单片机连接"""
        #推出视频保存和相机设置修改模式
        if self.btn_enterVideoSaveModel_state:
            self.btn_enterSaveModel_function()
            self.ui.btn_enterSaveModel.setEnabled(False)
        else:
            self.ui.btn_enterSaveModel.setEnabled(False)    
        if self.btn_missEventVideoSaveModel_state:
            self.btn_missEventVideoSaveModel_function()
            self.ui.btn_missEventVideoSaveModel.setEnabled(False)
        else:
            self.ui.btn_missEventVideoSaveModel.setEnabled(False) 

        if self.MCUTriggerThread:
            self.MCUTriggerThread.stop()
            self.MCUTriggerThread.wait()
            self.MCUTriggerThread = None
        # ROI模块状态关闭
        if self.btn_captureROI_view_state:
            self.btn_captureROI_view_function()
        if self.btn_cellFlowThroughROI_view_state:
            self.btn_cellFlowThroughROI_view_function()
        if self.btn_trappedROI_view_state:
            self.btn_trappedROI_view_function()
        if self.btn_flowRateROI_view_state:
            self.btn_flowRateROI_view_function()
        if self.btn_collectedROI_view_state:
            self.btn_collectedROI_view_function()            
        if self.btn_flowRateImageProcessing_state:
            self.slot_btn_flowRateImageProcessing_function()
        self.ui.btn_flowRateImageProcessing.setEnabled(False)
        # 图像处理模块
        if self.btn_enterImageProcessingModel_state:
            self.btn_enterImageProcessingModel_function()
        self.ui.btn_enterImageProcessingModel.setEnabled(False)

        # Maunal Rinse Channel 模块
        if self.btn_enterRinseChannelModel_state:
            self.btn_enterRinseChannelModel_function()
        self.ui.btn_enterRinseChannelModel.setEnabled(False)

        #禁用单片机通信触发模块
        self.ui.btn_triggerCapture.setEnabled(False)
        self.ui.btn_triggerFunction.setEnabled(False)
        self.ui.btn_triggerReleaseSort.setEnabled(False)
        self.ui.btn_triggerRelease.setEnabled(False)



        #断开相机的连接
        self.disconnection_camera_function()
        self.ui.btn_portConnect.setStyleSheet("background-color: #E1E1E1")
        #设置控件状态
        self.ui.cmb_fastCamera.setEnabled(True)
        self.ui.cmb_singleChip.setEnabled(True)
        self.ui.btn_refresh.setEnabled(True)

        # 相机参数设置
        self.ui.spb_ringBufferCapacity.setEnabled(True)
        self.ui.spb_pixelHeight.setEnabled(True)
        self.ui.spb_pixelWidth.setEnabled(True)
        self.ui.spb_exposureTime.setEnabled(True)     
        self.ui.spb_cameraGain.setEnabled(True)
        self.ui.spb_preTriggerBuffer.setEnabled(True)
        self.ui.spb_postTriggerBuffer.setEnabled(True)
        self.ui.spb_missEventSavePreFrames.setEnabled(True)
        
        self.ui.spb_frameRate.setEnabled(True)

    #按钮状态
    @pyqtSlot(object)
    def slot_btn_portConnect_MCU_state(self, state):
        """单片机串口状态槽函数"""
        """单片机连接正确自动启动相机连接功能"""
        print("单片机串口打开状态",state)
        
        if state == 0: #提示错误
            qw.QMessageBox.warning(self,'错误信息','串口已被占用，打开失败')
        elif state == 1:
            #COM口连接没问题启动相机连接程序
            self.connection_camera_function()            
            self.ui.btn_portConnect.setStyleSheet("background-color: #4EEE94")
            self.ui.cmb_fastCamera.setEnabled(False)
            self.ui.cmb_singleChip.setEnabled(False)
            self.ui.btn_refresh.setEnabled(False)
            self.ui.spb_ringBufferCapacity.setEnabled(False)
            self.ui.spb_pixelHeight.setEnabled(False)
            self.ui.spb_pixelWidth.setEnabled(False)
            self.ui.spb_exposureTime.setEnabled(False)     
            self.ui.spb_cameraGain.setEnabled(False)
            self.ui.spb_preTriggerBuffer.setEnabled(False)
            self.ui.spb_postTriggerBuffer.setEnabled(False)
            self.ui.spb_missEventSavePreFrames.setEnabled(False)
            
            self.ui.spb_frameRate.setEnabled(False)

            #将保存视频和相机参数修改的控件释放
            self.ui.btn_enterSaveModel.setEnabled(True)
            self.ui.btn_missEventVideoSaveModel.setEnabled(True)
            #启用单片机trigger通信模块
            self.ui.btn_triggerCapture.setEnabled(True)
            self.ui.btn_triggerFunction.setEnabled(True)
            self.ui.btn_triggerReleaseSort.setEnabled(True)
            self.ui.btn_triggerRelease.setEnabled(True)

            #图像处理模块
            self.ui.btn_enterImageProcessingModel.setEnabled(True)
            # flowRate ROI模块
            self.ui.btn_flowRateImageProcessing.setEnabled(True)
            # 手动润洗管道模块
            self.ui.btn_enterRinseChannelModel.setEnabled(True)
    
    def convert_16bit_to_8bit_fastCamera(self,frame_16bit):
        # FastCamera的图像大小为12bit 4095，用16bit储存
        # 从0-4095，线性拉伸到0-255
        scale = 255.0 / 65535
        frame_8bit = np.clip(frame_16bit* scale, 0, 255).astype(np.uint8)
        return frame_8bit

    # 视频显示模块
    @pyqtSlot(int,int)
    def slot_updata_display_fastCameraFrameIndex_FPS(self,index,fps):   
        "根据发送来的ringbuffer index显示图像"
        """耗时800-1500us"""
        self.ui.lb_FPSshow.setText("FPS:"+ str(fps))
        try:
            frame_16bit = self.FastCameraThread.ring_buffer.get_frame(index)
            if frame_16bit is None:
                return
            frame_8bit = self.sCMOS_dynamic_16bit_to_8bit(frame_16bit, self.ui.spb_fastCamera_displayGray_max.value())
            # 获取原始图像的宽度和高度
            original_height, original_width = frame_8bit.shape[:2]
            
            #判断ROI的宽和高谁大决定设为固定值
            if int(self.ui.spb_pixelHeight.value()*2) < int(self.ui.spb_pixelWidth.value()):
                # 固定显示宽度为，计算等比例的高度
                display_width = 600
                aspect_ratio = original_height / original_width
                display_height = int(display_width * aspect_ratio)
            else:
                # 固定显示高度，计算等比例的宽度
                display_height = 300
                aspect_ratio = original_width / original_height 
                display_width = int(display_height * aspect_ratio)
            # 调整图像大小为 
            display_frame = cv2.resize(cv2.cvtColor(frame_8bit, cv2.COLOR_GRAY2BGR), (display_width, display_height))
            # 计算缩放后的ROI
            self.Ratio_x = display_width/original_width
            self.Ratio_y = display_width/original_width
            # ROI 显示      
            if self.btn_cellFlowThroughROI_view_state:
                # release ROI 显示淡黄色显示
                self.ROI_live_view(display_frame,self.cellFlowThroughROI_X,self.cellFlowThroughROI_Y,self.cellFlowThroughROI_width,self.cellFlowThroughROI_height,150,220,255) # BGR #FFDC96
            if self.btn_captureROI_view_state:
                # 获取UI上的ROI坐标 cell capture
                self.ROI_live_view(display_frame,self.cellFlowThroughROI_X+self.captureROI_X,self.cellFlowThroughROI_Y,self.captureROI_width,self.cellFlowThroughROI_height,144,238,144) # BGR #90EE90         
            if self.btn_trappedROI_view_state:
                # trapped ROI 显示 粉红色显示
                self.ROI_live_view(display_frame,self.trappedROI_X,self.trappedROI_Y,self.trappedROI_width,self.trappedROI_height,204,204,255)  # BGR #FFCCCC
             
            if self.btn_flowRateROI_view_state:
                # 测试流速ROI淡蓝色
                self.ROI_live_view(display_frame,self.flowRateROI_X,self.flowRateROI_Y,self.flowRateROI_width,self.flowRateROI_height,255,255,187)  # BGR #BBFFFF
            if self.btn_collectedROI_view_state:
                # 搜集的细胞ROI鲜红色
                self.ROI_live_view(display_frame,self.collectedROI_X,self.collectedROI_Y,self.collectedROI_width,self.collectedROI_height,255,221,231,self.collectedROI_angle) # BGR #E7DDFF
            
            # 将 numpy 格式的图像转换为 QImage
            h, w, ch = display_frame.shape
            bytes_per_line = ch * w
            q_img = QImage(display_frame.data, w, h, bytes_per_line, QImage.Format_BGR888)
            # 将 QImage 转换为 QPixmap
            pixmap = QPixmap.fromImage(q_img)
            # 设置 QLabel 显示图像
            self.ui.lb_cameraView.setPixmap(pixmap)
            # 确保图像在 QLabel 中居中显示
            self.ui.lb_cameraView.setAlignment(Qt.AlignCenter)
            cv2.rectangle
            self.index_number += 1
            "每显示2帧,刷新一次二值化图像,相当于20FPS刷新次数"
            if self.index_number % 2 == 0:
                #高斯模糊
                blur_frame = cv2.GaussianBlur(frame_16bit, (3,3),sigmaX=0.8)
                # 如果开启图像提取的话，将当前帧复制给背景图片变量
                if self.btn_backgroundExtract_state:
                    self.background_frame = blur_frame.copy()
                    # 发送背景图片给图像处理线程，用于做背景
                    self.signal_sendBackgroundFrame.emit(self.background_frame)
                    self.btn_backgroundExtract_state = False
                "用来显示差值ROI图像"
                if self.background_frame is None:
                    #print("bg=none")
                    return      
                # 严格校验尺寸和通道数
                if blur_frame.shape != self.background_frame.shape:
                    print("警告:背景与当前ROI尺寸/类型不匹配，已自动清除背景")
                    self.background_frame = None
                    return  # 提前返回避免后续错误
                """# 计算差值，只保留变暗区域（细胞）
                diff_frame = cv2.subtract(self.background_frame, blur_frame)  # 背景 - 当前帧"""

                # 关键修改1：计算绝对差值（同时捕获亮区和暗区变化）
                diff_frame = cv2.absdiff(self.background_frame, blur_frame)  # 替换原来的subtract 
                # 阈值处理需调整范围（0-65535）[1,5](@ref)
                _, binary = cv2.threshold(diff_frame, 
                                        self.ui.spb_threshold_Bi.value(),  # 需确保阈值参数是16位范围
                                        65535,                 # 16位最大值
                                        cv2.THRESH_BINARY)
                # 将16位二值图像转换为8位
                binary_8bit = cv2.convertScaleAbs(binary, alpha=255.0/65535.0)

                # 形态学处理
                closed = cv2.morphologyEx(binary_8bit, cv2.MORPH_CLOSE, self.kernel, iterations=1)
                opened = cv2.morphologyEx(closed, cv2.MORPH_OPEN, self.kernel, iterations=1)
                for i in (1,2,4):
                    roi_x, roi_y, roi_w, roi_h = self.roi_way[i]
                    if i == 4:
                        diff_roi_frame = self._collected_background_diff_roi(frame_16bit, roi_x, roi_y, roi_w, roi_h)
                    else:
                        diff_roi_frame = opened[roi_y:roi_y+roi_h, roi_x:roi_x+roi_w]
                    diff_display_view = self.roi_diff_disply_lb[i]
                    self.display_ROI_image(diff_roi_frame,diff_display_view)
                self.display_ROI_image(opened,self.ui.lb_cameraView_BgDiff_Bi)
        except Exception as e:
            print(f"显示错误: {str(e)}")
    @pyqtSlot(object) #处理后的ROI显示

    #动态调整灰度范围显示图像
    def sCMOS_dynamic_16bit_to_8bit(self,frame_16bit, gray_max):
        """将16位图像动态映射到指定范围(input_min-input_max)的8位图像"""
        # 方法1：手动线性缩放（最快）
        scale = 255.0 / gray_max
        return np.clip(frame_16bit * scale, 0, 255).astype(np.uint8)

     # 视频显示模块 sCMOS
    @pyqtSlot(int,int)
    def slot_updata_display_sCMOSCameraFrameIndex_FPS(self,index,fps):   
        "根据发送来的ringbuffer index显示图像"
        self.ui.lb_sCMOS_FPSshow.setText("FPS:"+ str(fps))
        try:
            # 获取16位灰度图像
            frame = self.sCMOSCameraThread.sCMOS_ring_buffer.get_frame(index)
            if frame is None:
                return
                    # 步骤1：将16位数据转换为8位（关键修改）
            # 使用OpenCV的归一化函数将16位数据线性缩放到0-255范围
            #frame_8bit = cv2.normalize(frame, None, 0, 255, cv2.NORM_MINMAX, dtype=cv2.CV_8U)

            frame_8bit = self.sCMOS_dynamic_16bit_to_8bit(frame, self.ui.spb_sCMOS_displayGray_max.value())

            # 获取原始图像的宽度和高度
            original_height, original_width = frame_8bit.shape[:2]
             # 步骤1：将16位数据转换为8位（关键修改）
             # 判断ROI的宽和高谁大决定设为固定值
            if int(self.ui.spb_sCMOS_pixelHeight.value()*2) < int(self.ui.spb_sCMOS_pixelWidth.value()):
                # 固定显示宽度为，计算等比例的高度
                display_width = 600
                aspect_ratio = original_height / original_width
                display_height = int(display_width * aspect_ratio)
            else:
                # 固定显示高度，计算等比例的宽度
                display_height = 300
                aspect_ratio = original_width / original_height 
                display_width = int(display_height * aspect_ratio)
            # 调整图像大小为 
            display_frame = cv2.resize(cv2.cvtColor(frame_8bit, cv2.COLOR_GRAY2BGR), (display_width, display_height))
            # 将 numpy 格式的图像转换为 QImage
            h, w, ch = display_frame.shape
            bytes_per_line = ch * w
            q_img = QImage(display_frame.data, w, h, bytes_per_line, QImage.Format_BGR888)
            # 将 QImage 转换为 QPixmap
            pixmap = QPixmap.fromImage(q_img)
            # 设置 QLabel 显示图像
            self.ui.lb_sCMOS_cameraView.setPixmap(pixmap)
            # 确保图像在 QLabel 中居中显示
            self.ui.lb_sCMOS_cameraView.setAlignment(Qt.AlignCenter)
            cv2.rectangle
            self.index_sCMOS_number += 1
        except Exception as e:
            print(f"sCMOS显示错误: {str(e)}")
    @pyqtSlot(object) #处理后的ROI显示

    def slot_display_processed_ROI_image(self,ROI_para):
        """把数据记录的所有都初始化为None"""
        ID   = "None" 
        roi_x= "None"
        roi_y= "None"
        roi_w= "None"
        roi_h= "None"
        total_number= "None"
        miss_number= "None"
        algorithm_time= "None"
        interval_time= "None"
        cell_area= "None"
        cell_cX= "None"
        cell_cY= "None"
        bottom= "None"
        """显示ROI图像"""
        """耗时400-800us"""
        imageProcessing_way = ROI_para["imageProcessing_way"] #非控件
        #ID_add              = ROI_para["ID_add"] 这是固定的，只有0和5会+1
        algorithm_time      = math.ceil(ROI_para["algorithm_time"])
        interval_time       = ROI_para["interval_time"]
        total_add           = ROI_para["total_add"]
        miss_add            = ROI_para["miss_add"]
        cell_area           = ROI_para["cell_area"]          
        cell_cX             = ROI_para["cell_cX"]            
        cell_cY             = ROI_para["cell_cY"]           
        roi_frame           = ROI_para["roi_frame"]          
        processed_frame     = ROI_para["processed_frame"]    
        target_frame        = ROI_para["target_frame"]  
        processing_state    = ROI_para["processing_state"] #非控件 1:√; 0:X
        
        if imageProcessing_way not in self.roi_display_widgets:
            return "wrong image processor state"
        roi_x, roi_y, roi_w, roi_h = self.roi_way[imageProcessing_way]
        imgROIName,original_view, processed_view, target_view= self.roi_display_widgets[imageProcessing_way]
        #参数: 1.ID; 2.area; 3.X; 4.Y; 5.total; 6.miss;  7.算法时间; 8. 间隔时间; 9.筛选状态


        if imageProcessing_way == 0: # cell flow through _roi
            ID_lb ,area_lb, x_lb, y_lb,total_lb,algorithm_time_lb,interval_time_lb = self.roi_display_lb[imageProcessing_way]
            self.cell_ID += 1
            self.totalNumb_capture = self.totalNumb_capture + total_add
            ID = self.cell_ID
            total_number = self.totalNumb_capture
            miss_number  = "NULL"
            "单独把Capture ROI提出来"
            capture_roi_x = self.captureROI_X
            capture_roi_width = self.captureROI_width
            capture_roi_frame = roi_frame[:, capture_roi_x:capture_roi_x+capture_roi_width]
            capture_processed_frame = processed_frame[:, capture_roi_x:capture_roi_x+capture_roi_width]
            capture_target_frame = target_frame[:, capture_roi_x:capture_roi_x+capture_roi_width]
            #显示原始图像
            self.display_ROI_image(capture_roi_frame,self.ui.lb_captureROIView_original)
            #显示形态学运算后的二值化图像
            self.display_ROI_image(capture_processed_frame,self.ui.lb_captureROIView_processed)
            #显示把目标颗粒轮廓勾勒出来的原始图像
            self.display_ROI_image(capture_target_frame,self.ui.lb_captureROIView_target)

            ID_lb.setText(str(self.cell_ID))
            area_lb.setText(str(cell_area))
            x_lb.setText(str(cell_cX))
            y_lb.setText(str(cell_cY))
            total_lb.setText(str(self.totalNumb_capture))
            algorithm_time_lb.setText(str(algorithm_time))
            interval_time_lb.setText(str(interval_time))

        elif imageProcessing_way == 1: # trapped_roi
            ID_lb ,area_lb,total_lb,miss_lb,roi_state_lb = self.roi_display_lb[imageProcessing_way]
            self.totalNumb_trapped = self.totalNumb_trapped + total_add
            self.trappedCell_miss  = self.trappedCell_miss + miss_add
            total_number = self.totalNumb_trapped
            miss_number  = self.trappedCell_miss
            ID =self.cell_ID
            ID_lb.setText(str(self.cell_ID))
            area_lb.setText(str(cell_area))
            total_lb.setText(str(self.totalNumb_trapped))
            miss_lb.setText(str(self.trappedCell_miss))
            roi_state_lb.setText(str(processing_state))

        elif imageProcessing_way == 2: # relese_roi
            ID_lb,total_lb,algorithm_time_lb,interval_time_lb = self.roi_display_lb[imageProcessing_way]
            self.totalNumb_relese = self.totalNumb_relese + total_add
            ID =self.cell_ID
            ID_lb.setText(str(self.cell_ID))
            total_lb.setText(str(self.totalNumb_relese))
            algorithm_time_lb.setText(str(algorithm_time))
            interval_time_lb.setText(str(interval_time))

        elif imageProcessing_way == 4: # collected_roi
            ID_lb ,area_lb, x_lb, y_lb,total_lb,miss_lb,algorithm_time_lb,interval_time_lb,roi_state_lb = self.roi_display_lb[imageProcessing_way]
            self.totalNumb_collected = self.totalNumb_collected + total_add
            self.collectedCell_miss  = self.collectedCell_miss + miss_add
            ID = self.cell_ID
            ID_lb.setText(str(self.cell_ID))
            area_lb.setText(str(cell_area))
            x_lb.setText(str(cell_cX))
            y_lb.setText(str(cell_cY))
            total_lb.setText(str(self.totalNumb_collected))
            miss_lb.setText(str(self.collectedCell_miss))
            algorithm_time_lb.setText(str(algorithm_time))
            interval_time_lb.setText(str(interval_time))
            roi_state_lb.setText(str(processing_state))

        elif imageProcessing_way == 5: # flowRate_roi
            ID_lb ,area_lb, x_lb = self.roi_display_lb[imageProcessing_way]
            self.flowRate_ID += 1
            self.ui.spb_cellSpeedValue.setValue(0)
            self.ui.spb_flowRateValue.setValue(0) 
            ID = self.flowRate_ID
            ID_lb.setText(str(self.flowRate_ID))
            area_lb.setText(str(cell_area))
            x_lb.setText(str(cell_cX))
        elif imageProcessing_way == 6: # flowRate_roi
            ID_lb ,area_lb, x_lb,interval_time_lb,roi_state_lb = self.roi_display_lb[imageProcessing_way]
            self.ui.spb_cellSpeedValue.setValue(ROI_para["cellSpeedValue"])
            self.ui.spb_flowRateValue.setValue(ROI_para["flowRateValue"]) 

            ID = self.flowRate_ID
            ID_lb.setText(str(self.flowRate_ID))
            area_lb.setText(str(cell_area))
            x_lb.setText(str(cell_cX))
            interval_time_lb.setText(str(interval_time))
            roi_state_lb.setText(str(processing_state))
        elif imageProcessing_way == 7: # function_measurement_roi
            ID_lb ,total_lb,miss_lb,roi_state_lb,bottom_lb = self.roi_display_lb[imageProcessing_way]
            ID = self.cell_ID
            self.totalNumb_functionMeasurement_end = self.totalNumb_functionMeasurement_end + total_add
            self.functionMeasurement_end_miss  = self.functionMeasurement_end_miss + miss_add
            ID_lb.setText(str(self.cell_ID))
            total_lb.setText(str(self.totalNumb_functionMeasurement_end))
            miss_lb.setText(str(self.functionMeasurement_end_miss))
            roi_state_lb.setText(str(processing_state))
            bottom = ROI_para["max_bottom_y"]
            bottom_lb.setText(str(bottom))
 
        #创建带有时间戳的数据记录
        # 流速测试的不记录
        if imageProcessing_way not in (5,6):
            record = {
                'ID':ID,
                'roi_x': roi_x,
                'roi_y': roi_y,
                'roi_w': roi_w,
                'roi_h': roi_h,
                'roi_angle': ROI_para.get("roi_angle", self.roi_angles.get(imageProcessing_way, 0)),
                'total_number':total_number,
                'miss_number':miss_number,
                'algorithm_time_μs' : algorithm_time,
                'interval_time_μs' : interval_time,
                'cell_area' : cell_area,
                'cell_cX' : cell_cX,
                'cell_cY' : cell_cY,
                "cell_speed(μm/ms)" : self.ui.spb_cellSpeedValue.value(),
                'timestamp': datetime.now().strftime("%H:%M:%S:%f")[:-3],#保留到毫秒
                'processing_way': ROI_para["imageProcessing_way"],  # 保留处理类型标识
                'Lenth(bottom)':bottom,
            }
            # 加入统一队列
            self.roi_data_queue.append(record)
            #显示队列的元素个数
            self.ui.spb_roiDataNumber.setValue(len(self.roi_data_queue))
        
        #显示原始图像
        self.display_ROI_image(roi_frame,original_view)
        #显示形态学运算后的二值化图像
        self.display_ROI_image(processed_frame,processed_view)
        #显示把目标颗粒轮廓勾勒出来的原始图像
        self.display_ROI_image(target_frame,target_view)

        if self.btn_saveROIImage_state:
            self.save_ROI_image(roi_frame,imgROIName,"Original")
            self.save_ROI_image(processed_frame,imgROIName,"Binary")
            self.save_ROI_image(target_frame,imgROIName,"processed")
    def display_ROI_image(self, frame, ui_lb_View):
        try:
            # 获取原始图像的尺寸
            original_height, original_width = frame.shape[:2]
            # 获取 QLabel 的尺寸
            label_width = ui_lb_View.width()
            label_height = ui_lb_View.height()
            
            # 计算缩放比例（完全覆盖 QLabel）
            scale_width = label_width / original_width
            scale_height = label_height / original_height
            scale = min(scale_width, scale_height)  # 选择较大的比例
            
            # 计算缩放后的尺寸
            display_width = int(original_width * scale)
            display_height = int(original_height * scale)
            
            # 调整图像大小
            display_frame = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR), (display_width, display_height))
            
            # 转换为 QImage
            h, w, ch = display_frame.shape
            bytes_per_line = ch * w
            q_img = QImage(display_frame.data, w, h, bytes_per_line, QImage.Format_BGR888)
            
            # 转换为 QPixmap
            pixmap = QPixmap.fromImage(q_img)
            
            # 计算裁剪区域（居中裁剪）
            x_offset = (display_width - label_width) // 2
            y_offset = (display_height - label_height) // 2
            cropped_pixmap = pixmap.copy(x_offset, y_offset, label_width, label_height)
            
            # 设置 QLabel 显示图像
            ui_lb_View.setPixmap(cropped_pixmap)
            ui_lb_View.setAlignment(Qt.AlignCenter)
        except Exception as e:
            print(f"显示错误: {str(e)}")

    def _collected_background_diff_roi(self, frame_16bit, roi_x, roi_y, roi_w, roi_h):
        angle = self.roi_angles.get(4, 0)
        roi_frame = crop_rotated_roi(frame_16bit, roi_x, roi_y, roi_w, roi_h, angle)
        blur_roi_frame = cv2.GaussianBlur(roi_frame.astype(np.float32), (3,3), sigmaX=0.8).astype(np.uint16)
        roi_background_frame = crop_rotated_roi(self.background_frame, roi_x, roi_y, roi_w, roi_h, angle)
        diff_roi_frame = cv2.absdiff(roi_background_frame, blur_roi_frame)
        _, binary = cv2.threshold(
            diff_roi_frame,
            self.ui.spb_threshold_Bi.value(),
            65535,
            cv2.THRESH_BINARY,
        )
        binary_8bit = cv2.convertScaleAbs(binary, alpha=255.0/65535.0)
        collected_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3,3))
        closed = cv2.morphologyEx(binary_8bit, cv2.MORPH_CLOSE, collected_kernel, iterations=1)
        return cv2.morphologyEx(closed, cv2.MORPH_OPEN, collected_kernel, iterations=1)

    def ROI_live_view(self,display_frame,roi_x,roi_y,width,height,B,G,R,angle=0):
        """显示框选ROI的区域"""
        x_scaled = int(roi_x * self.Ratio_x)
        y_scaled = int(roi_y * self.Ratio_y)
        width_scaled = int(width * self.Ratio_x)
        height_scaled = int(height * self.Ratio_y)
        if abs(float(angle or 0)) > 1e-6:
            draw_rotated_roi(display_frame, roi_x, roi_y, width, height, angle, (B,G,R), self.Ratio_x, self.Ratio_y)
            return
        cv2.rectangle(display_frame,(x_scaled , y_scaled),(x_scaled + width_scaled, y_scaled + height_scaled),(B,G,R),1)

    def save_ROI_image(self,frame,imgROIName,imgClassName):
        #def camera_snap(self,index,average_fps):
        """利用OpenCV保存单张照片"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-4]  # 去掉最后三位微秒，保留到毫秒
        folder_name = str(time.strftime("%Y%m%d"))+"_image"
            # 创建日期文件夹
        if not os.path.exists(folder_name):
            os.makedirs(folder_name)
        filename = f"{folder_name}/{timestamp}_{imgROIName}_{imgClassName}.jpg"  
        cv2.imwrite(filename, frame)
        print("图像已成功保存到")
    
    # Fast Camera Video Save 模块 
    def btn_enterSaveModel_function(self):
        if self.btn_enterVideoSaveModel_state:
            self.ui.btn_enterSaveModel.setStyleSheet("background-color: #E1E1E1")
            self.btn_enterVideoSaveModel_state = False
            self.FastCameraThread.camera_worker.trigger_buffer.btn_videoSavingModel_state = False #是否进入图像保存模式，即开始记录pre_trigger的数据
            self.ui.btn_snap.setEnabled(False)
            self.ui.btn_triggerSaveVideo.setEnabled(False)
        else:
            # 初始化关闭标识
            self.clouseVideoSivingModel = False
            self.ui.btn_enterSaveModel.setStyleSheet("background-color: #4EEE94")
            self.btn_enterVideoSaveModel_state = True
            self.FastCameraThread.camera_worker.trigger_buffer.btn_videoSavingModel_state = True
            self.ui.btn_snap.setEnabled(True)
            self.ui.btn_triggerSaveVideo.setEnabled(True)            
            if self.btn_missEventVideoSaveModel_state:
                self.btn_missEventVideoSaveModel_function()

    # sCMOS Video Save 模块 
    def btn_sCMOS_enterSaveModel_function(self):
        if self.btn_sCMOS_enterSaveModel_state:
            self.ui.btn_sCMOS_enterSaveModel.setStyleSheet("background-color: #E1E1E1")
            self.btn_sCMOS_enterSaveModel_state = False
            self.sCMOSCameraThread.sCMOS_camera_worker.sCMOS_trigger_buffer.btn_videoSavingModel_state = False #是否进入图像保存模式，即开始记录pre_trigger的数据
            self.ui.btn_sCMOS_snap.setEnabled(False)
            self.ui.btn_sCMOS_videoSave.setEnabled(False)
        else:
            # 初始化关闭标识
            self.clouse_sCMOSVideoSivingModel = False 
            self.btn_sCMOS_enterSaveModel_state = False
            self.ui.btn_sCMOS_enterSaveModel.setStyleSheet("background-color: #4EEE94")
            self.btn_sCMOS_enterSaveModel_state = True
            self.sCMOSCameraThread.sCMOS_camera_worker.sCMOS_trigger_buffer.btn_videoSavingModel_state = True
            self.ui.btn_sCMOS_snap.setEnabled(True)
            self.ui.btn_sCMOS_videoSave.setEnabled(True)  

    def btn_missEventVideoSaveModel_function(self):
        if self.btn_missEventVideoSaveModel_state:
            self.btn_missEventVideoSaveModel_state = False
            self.ui.btn_missEventVideoSaveModel.setStyleSheet("background-color: #E1E1E1")
        else:
            self.btn_missEventVideoSaveModel_state = True
            self.ui.btn_missEventVideoSaveModel.setStyleSheet("background-color: #4EEE94")
            if self.btn_enterVideoSaveModel_state:
                self.btn_enterSaveModel_function()   
        self.update_image_processing_para()     

    def btn_sCMOS_snap_function(self):
        """sCMOS保存单张图片"""
        self.ui.btn_sCMOS_snap.setEnabled(False)
        self.sCMOSCameraThread.sCMOS_camera_worker.cameraSnap_state = True
        self.ui.btn_sCMOS_snap.setEnabled(True)
    def btn_sCMOS_videoSave_function(self):
        """触发保存视频"""
        if self.btn_sCMOS_enterSaveModel_state:
            try:
                # 触发保存事件
                self.sCMOSCameraThread.sCMOS_camera_worker.sCMOS_trigger_buffer.trigger_event()
                # 更新按钮状态为保存中
                self.ui.btn_sCMOS_videoSave.setStyleSheet("background-color: #4EEE94")
                self.ui.btn_sCMOS_videoSave.setEnabled(False)  # 防止重复点击
            except Exception as e:
                print(f"触发保存失败: {e}")

    @pyqtSlot(int)
    def slot_handle_sCMOS_save_video_finished(self, state):
         """处理保存完成信号,隔离信号,不然会一直触发trigger"""
         if state == 1:
            # 恢复按钮状态
            self.ui.btn_sCMOS_videoSave.setEnabled(True)
            self.ui.btn_sCMOS_videoSave.setStyleSheet("background-color: #E1E1E1")
            self.btn_sCMOS_enterSaveModel_function()

    def btn_snap_function(self):
        """fastCamera保存单张图片"""
        self.ui.btn_snap.setEnabled(False)
        self.FastCameraThread.camera_worker.cameraSnap_state = True
        self.ui.btn_snap.setEnabled(True)
    def btn_triggerSaveVideo_function(self):
        """触发保存视频"""
        if self.btn_enterVideoSaveModel_state:
            try:
                # 触发保存事件
                self.FastCameraThread.camera_worker.trigger_buffer.trigger_event()
                # 更新按钮状态为保存中
                self.ui.btn_triggerSaveVideo.setStyleSheet("background-color: #4EEE94")
                self.ui.btn_triggerSaveVideo.setEnabled(False)  # 防止重复点击
            except Exception as e:
                print(f"触发保存失败: {e}")
    @pyqtSlot(int)
    def slot_handle_save_video_finished(self, state):
         """处理保存完成信号,隔离信号,不然会一直触发trigger"""
         if state == 1:
            # 恢复按钮状态
            self.ui.btn_triggerSaveVideo.setEnabled(True)
            self.ui.btn_triggerSaveVideo.setStyleSheet("background-color: #E1E1E1")
            if self.clouseVideoSivingModel:
                self.btn_enterSaveModel_function()

    @pyqtSlot()
    def slot_closeVideoSavingModel(self):
        self.clouseVideoSivingModel = True

    # Trigger Control 模块
    def btn_triggerCapture_function(self):
        """"Trigger信号相关函数"""
        #self.ui.btn_triggerCapture.setEnabled(False)
        self.signal_btn_triggerCapture.emit()
    @pyqtSlot()
    def slot_btn_triggerCapture_finish(self):   
        self.ui.btn_triggerCapture.setEnabled(True)
    def btn_triggerReleaseSort_function(self):
        """"Trigger信号相关函数"""
        # 禁用按钮，防止重复点击
        self.ui.btn_triggerReleaseSort.setEnabled(False)
        self.signal_btn_triggerReleaseSort.emit()
    @pyqtSlot()
    def slot_btn_triggerReleaseSort_finish(self):
        self.ui.btn_triggerReleaseSort.setEnabled(True)
    def btn_triggerRelease_function(self):# 默认值为0，表示仅仅进行release Trigger
        """"Release信号相关函数"""
        # 禁用按钮，防止重复点击
        self.ui.btn_triggerRelease.setEnabled(False)
        self.signal_btn_triggerRelease.emit()
    @pyqtSlot()
    def slot_btn_triggerRelease_finish(self):
        self.ui.btn_triggerRelease.setEnabled(True)
    @pyqtSlot()
    def slot_btn_triggerFunction_function(self):
        """"Function只等待设定时间,不向单片机发送指令"""
        self.ui.btn_triggerFunction.setEnabled(False)
        QTimer.singleShot(
            int(self.ui.spb_triggerFunction_time.value()),
            Qt.PreciseTimer,
            self._finish_manual_function_wait,
        )
    @pyqtSlot()
    def _finish_manual_function_wait(self):
        """手动Function按钮只负责等待,不进入自动分选判定。"""
        self.ui.btn_triggerFunction.setEnabled(True)

    #自动筛选路径的Function等待结束后,根据checkbox判断是否为目标细胞
    @pyqtSlot()
    def slot_btn_triggerFunction_finish(self):
        self.ui.btn_triggerFunction.setEnabled(True)
        #第一位只能是7；第二位： -1:非目标细胞; 0:非目标细胞,miss了; 1:目标细胞
        if self.ui.chb_isTarget.isChecked():
            self.signal_isTarget.emit(7,1)
            print("假装是目标细胞")
        else:
            self.signal_isTarget.emit(7,-1)
            print("假装非目标细胞")


    # 手动rinse 管道 模块
    def btn_rinseChannelCapture_function(self):
        """手动控制润洗筛选通道"""
        if self.btn_rinseChannelCapture_state:
            self.ui.btn_rinseChannelCapture.setStyleSheet("background-color: #E1E1E1")
            self.btn_rinseChannelCapture_state = False
            self.ui.btn_rinseChannelSort.setEnabled(True)
            self.ui.btn_rinseChannelRelease.setEnabled(True)
            self.ui.btn_enterRinseChannelModel.setEnabled(True)
            self.signal_btn_rinseChannel_OFF.emit()
        else:
            self.ui.btn_rinseChannelCapture.setStyleSheet("background-color: #4EEE94")
            self.btn_rinseChannelCapture_state = True
            self.ui.btn_rinseChannelSort.setEnabled(False)
            self.ui.btn_rinseChannelRelease.setEnabled(False)
            self.ui.btn_enterRinseChannelModel.setEnabled(False)
            self.signal_btn_rinseChannelCapture.emit()
    def btn_rinseChannelSort_function(self):
        """手动控制润洗筛选通道"""
        if self.btn_rinseChannelSort_state:
            self.ui.btn_rinseChannelSort.setStyleSheet("background-color: #E1E1E1")
            self.btn_rinseChannelSort_state = False
            self.ui.btn_rinseChannelCapture.setEnabled(True)
            self.ui.btn_rinseChannelRelease.setEnabled(True)
            self.ui.btn_enterRinseChannelModel.setEnabled(True)
            self.signal_btn_rinseChannel_OFF.emit()
        else:
            self.ui.btn_rinseChannelSort.setStyleSheet("background-color: #4EEE94")
            self.btn_rinseChannelSort_state = True
            self.ui.btn_rinseChannelCapture.setEnabled(False)
            self.ui.btn_rinseChannelRelease.setEnabled(False)
            self.ui.btn_enterRinseChannelModel.setEnabled(False)
            self.signal_btn_rinseChannelSort.emit()
    def btn_rinseChannelRelease_function(self):
        """手动控制润洗筛选通道"""
        if self.btn_rinseChannelRelease_state:
            self.ui.btn_rinseChannelRelease.setStyleSheet("background-color: #E1E1E1")
            self.btn_rinseChannelRelease_state = False
            self.ui.btn_rinseChannelSort.setEnabled(True)
            self.ui.btn_rinseChannelCapture.setEnabled(True)
            self.ui.btn_enterRinseChannelModel.setEnabled(True)
            self.signal_btn_rinseChannel_OFF.emit()
        else:
            self.ui.btn_rinseChannelRelease.setStyleSheet("background-color: #4EEE94")
            self.btn_rinseChannelRelease_state = True
            self.ui.btn_rinseChannelSort.setEnabled(False)
            self.ui.btn_rinseChannelCapture.setEnabled(False)
            self.ui.btn_enterRinseChannelModel.setEnabled(False)
            self.signal_btn_rinseChannelRelease.emit()
            #发送信号开启。。。

    def btn_enterRinseChannelModel_function(self):
        if self.btn_enterRinseChannelModel_state:
            self.ui.btn_enterRinseChannelModel.setStyleSheet("background-color: #E1E1E1")
            self.btn_enterRinseChannelModel_state = False
            # 判断是否有工作的模块
            if self.btn_rinseChannelCapture_state:
                self.btn_rinseChannelCapture_function()
            elif self.btn_rinseChannelRelease_state:
                self.btn_rinseChannelRelease_function()
            elif self.btn_rinseChannelSort_state:
                self.btn_rinseChannelSort_function()
            self.ui.btn_rinseChannelSort.setEnabled(False)
            self.ui.btn_rinseChannelCapture.setEnabled(False)
            self.ui.btn_rinseChannelRelease.setEnabled(False)
            #发送信号关闭。。。
        else:
            self.ui.btn_enterRinseChannelModel.setStyleSheet("background-color: #4EEE94")
            self.btn_enterRinseChannelModel_state = True
            self.ui.btn_rinseChannelSort.setEnabled(True)
            self.ui.btn_rinseChannelCapture.setEnabled(True)
            self.ui.btn_rinseChannelRelease.setEnabled(True)

    def _render_sim_preview_frame(self, frame, cache_frame=True, copy_cached_frame=True):
        if frame is None:
            return
        original_height, original_width = frame.shape[:2]
        bounds = self.ui.lb_sCMOS_cameraView.contentsRect()
        display_width, display_height = fit_image_size_to_bounds(
            original_width,
            original_height,
            bounds.width(),
            bounds.height(),
        )
        display_frame = fast_preview_uint16_to_uint8(
            frame,
            (display_width, display_height),
            gray_max=self.ui.spb_sCMOS_displayGray_max.value(),
            auto_contrast=self.ui.chb_sCMOS_autoContrast.isChecked(),
            auto_state=self.sim_auto_contrast_state,
        )
        h, w = display_frame.shape[:2]
        bytes_per_line = int(display_frame.strides[0])
        q_img = QImage(display_frame.data, w, h, bytes_per_line, QImage.Format_Grayscale8)
        self.ui.lb_sCMOS_cameraView.setPixmap(QPixmap.fromImage(q_img.copy()))
        self.ui.lb_sCMOS_cameraView.setAlignment(Qt.AlignCenter)
        if cache_frame:
            # resize 重绘缓存：SIM 轮询路径的快照帧每帧独立分配（latest-frame-wins
            # 取走即清空），存引用即可；王波相机线程路径缓冲生命周期未确认，维持 copy 防御。
            self.sim_last_preview_frame = frame.copy() if copy_cached_frame else frame

    def _clear_sim_preview_display(self):
        label = self.ui.lb_sCMOS_cameraView
        bounds = label.contentsRect()
        width = max(1, bounds.width())
        height = max(1, bounds.height())
        pixmap = QPixmap(width, height)
        pixmap.fill(Qt.black)
        label.setPixmap(pixmap)
        label.setAlignment(Qt.AlignCenter)
        self.sim_last_preview_frame = None
        self.sim_last_preview_sequence = -1
        auto_contrast_state = getattr(self, "sim_auto_contrast_state", None)
        if auto_contrast_state is not None:
            auto_contrast_state.reset()

    # GUI 定时器只取最新预览帧，旧帧被覆盖丢弃以保持低延迟显示。
    def poll_latest_sim_preview_frame(self):
        if self.sim_preview_controller is None:
            return
        snapshot = self.sim_preview_controller.take_latest_frame()
        if snapshot is None:
            return
        if int(snapshot.sequence) <= int(getattr(self, "sim_last_preview_sequence", -1)):
            return
        try:
            self._render_sim_preview_frame(snapshot.frame, copy_cached_frame=False)
        except Exception as e:
            print(f"SIM preview display error: {str(e)}")
            return
        self.sim_last_preview_sequence = int(snapshot.sequence)
        self.ui.lb_sCMOS_FPSshow.setText(str(snapshot.fps))

    @pyqtSlot(object, int)
    def slot_updata_display_sCMOSCameraFrameIndex_FPS(self, frame, fps):
        self.ui.lb_sCMOS_FPSshow.setText(str(fps))
        try:
            self._render_sim_preview_frame(frame)
        except Exception as e:
            print(f"SIM preview display error: {str(e)}")

    def eventFilter(self, watched, event):
        if watched is self.ui.lb_sCMOS_cameraView and event.type() == QEvent.Resize and self.sim_last_preview_frame is not None:
            try:
                self._render_sim_preview_frame(self.sim_last_preview_frame, cache_frame=False)
            except Exception as e:
                print(f"SIM preview resize error: {str(e)}")
        return super().eventFilter(watched, event)

    def btn_sCMOS_enterSaveModel_function(self):
        qw.QMessageBox.information(self, "SIM Preview", "SIM preview mode does not use the old sCMOS save module.")

    def btn_sCMOS_snap_function(self):
        qw.QMessageBox.information(self, "SIM Preview", "Snapshot saving for the legacy sCMOS module has been disabled.")

    def btn_sCMOS_videoSave_function(self):
        qw.QMessageBox.information(self, "SIM Preview", "Video saving for the legacy sCMOS module has been disabled.")

    @pyqtSlot(int)
    def slot_handle_sCMOS_save_video_finished(self, state):
        self.ui.btn_sCMOS_videoSave.setEnabled(False)
        self.ui.btn_sCMOS_videoSave.setStyleSheet("background-color: #E1E1E1")

if __name__ == "__main__":
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)  # 启用高 DPI 缩放
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)  # 启用高 DPI 图标
    app = qw.QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec_())  

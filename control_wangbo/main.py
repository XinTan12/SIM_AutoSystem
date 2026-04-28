import sys
from PyQt5.QtCore import QEvent, QMetaObject, Qt, pyqtSlot,pyqtSignal,QTimer
from PyQt5.QtGui import QImage, QPixmap
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
from datetime import datetime
import os
import time
from collections import deque
import pandas as pd
from pathlib import Path
import tkinter as tk
from tkinter import messagebox
from threading import Lock
import numpy as np
from PyQt5.QtWidgets import QScrollArea
#import torch 

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

from sim_control.config_store import app_config_from_dict, app_config_to_dict, load_app_config, save_app_config
from sim_control.controller import SimAcquisitionController
from sim_control.gui import (
    SimSettingsDialog,
    create_camera_adapter_for_backend,
    create_daq_adapter_for_backend,
)
from sim_control.led_indicator import LedIndicator
from sim_control.models import SimTaskConfig
from sim_control.preview import SimPreviewController
from sim_control.preview_contrast import AutoContrastState, auto_uint16_to_uint8, manual_uint16_to_uint8
from sim_control.summary import build_sim_settings_summary
from sim_control.sim_camera_presets import (
    DEFAULT_SIM_CAMERA_SIZE,
    SIM_CAMERA_ROI_STEP_PX,
    fit_image_size_to_bounds,
    is_full_frame_sim_camera_size,
    normalize_sim_camera_roi,
    preset_index_for_sim_camera_size,
    sim_camera_roi_origin_bounds,
    size_from_sim_camera_label,
)

SIM_EXPOSURE_MIN_MS = 1
SIM_EXPOSURE_MAX_MS = 10_000
SIM_EXPOSURE_DEFAULT_MS = 10
SIM_BIT_DEPTH_DEFAULT = 16
USER_FACING_BIT_DEPTHS = (8, 12, 16)


def setup_scrollable_cellsorting_ui(window, ui):
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
    return app_config_from_dict(app_config_to_dict(config))


def merge_legacy_sim_control_payload(base_config, legacy_payload):
    merged = clone_sim_app_config(base_config)
    if not legacy_payload:
        return merged

    legacy_config = app_config_from_dict(legacy_payload)
    merged.daq = legacy_config.daq
    merged.camera = legacy_config.camera
    merged.timing = legacy_config.timing
    merged.pattern_files = list(legacy_config.pattern_files)
    merged.selected_running_order = legacy_config.selected_running_order
    merged.selected_laser_nm = legacy_config.selected_laser_nm
    merged.config_path = base_config.config_path
    return merged


def apply_real_hardware_preference(config, camera_devices, slm_devices, daq_devices):
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
    value_ms = int(round(float(exposure_us) / 1000.0))
    return max(SIM_EXPOSURE_MIN_MS, min(SIM_EXPOSURE_MAX_MS, value_ms))


def sim_exposure_ms_to_us(exposure_ms):
    value_ms = int(round(float(exposure_ms)))
    value_ms = max(SIM_EXPOSURE_MIN_MS, min(SIM_EXPOSURE_MAX_MS, value_ms))
    return value_ms * 1000


def normalize_legacy_sim_exposure_setting(exposure_value):
    value = int(round(float(exposure_value)))
    if value > SIM_EXPOSURE_MAX_MS:
        return sim_exposure_us_to_ms(value)
    return max(SIM_EXPOSURE_MIN_MS, min(SIM_EXPOSURE_MAX_MS, value))


def sim_bit_depth_to_label(bit_depth):
    value = int(round(float(bit_depth)))
    return f"{value}-bit"


def sim_bit_depth_from_label(label, default=SIM_BIT_DEPTH_DEFAULT):
    digits = "".join(ch for ch in str(label) if ch.isdigit())
    if not digits:
        return int(default)
    return int(digits)


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


class MainWindow(qw.QWidget):
    
    # 创建发送mindvision相机设置参数的信号
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
    signal_btn_rinseChannelElasticityMeasurement    = pyqtSignal()
    signal_btn_rinseChannel_OFF    = pyqtSignal()
    # 发送背景图片 背景图片变量的创建应该在打开相机那里
    signal_sendBackgroundFrame = pyqtSignal(object)
    # 手动控制Trigger的信号
    signal_btn_triggerCapture        = pyqtSignal()
    signal_btn_triggerSort           = pyqtSignal()
    signal_btn_triggerRelease        = pyqtSignal() 
    signal_btn_triggerElasticityMeasurement           = pyqtSignal() 
    signal_updataFastCamera_maxGray  = pyqtSignal(int) 
    signal_setImageProcessingWay_UIThread = pyqtSignal(int) #改变ROI的识别模式
    #手动捕获细胞是否为目标细胞
    signal_isTarget = pyqtSignal(int,int)  #第一位只能是7；第二位： -1:非目标细胞; 0:非目标细胞,miss了; 1:目标细胞


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
        self.sim_last_acquisition_batch = None
        self.sim_current_task_id = ""
        self.sim_last_preview_frame = None
        self.sim_last_preview_sequence = -1
        self.sim_auto_contrast_state = AutoContrastState()
        self.ui.chb_sCMOS_autoContrast.toggled.connect(lambda _checked: self.sim_auto_contrast_state.reset())
        self.sim_preview_restart_timer = QTimer(self)
        self.sim_preview_restart_timer.setSingleShot(True)
        self.sim_preview_restart_timer.timeout.connect(self.restart_sim_preview_with_current_settings)
        self.sim_preview_poll_timer = QTimer(self)
        self.sim_preview_poll_timer.timeout.connect(self.poll_latest_sim_preview_frame)
        self.UI_Init()
        self.setup_sim_runtime_status_widgets()
        # 初始化统计细胞ID和个数
        self.cell_ID = 0                   # 用来记录是哪次细胞的
        self.totalNumb_capture = 0
        self.totalNumb_trapped = 0
        self.trappedCell_miss  = 0
        self.totalNumb_relese = 0
        self.totalNumb_sort = 0
        self.totalNumb_elasticityMeasurement_start = 0
        self.totalNumb_elasticityMeasurement_end = 0
        self.sortCell_miss  = 0
        self.elasticityMeasurement_start_miss = 0
        self.elasticityMeasurement_end_miss = 0
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
        self.elasticityROI_X = 0
        self.elasticityROI_Y = 0
        self.elasticityROI_width = 70
        self.elasticityROI_heigh = 70
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
        default_path = Path(__file__).parent / "lastConfiguration.json"
        if default_path.exists():
            self.load_configure_settings(default_path)
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
        if self.sim_preview_controller:
            self.sim_preview_controller.shutdown()
        if self.sim_acquisition_controller:
            self.sim_acquisition_controller.shutdown()
        event.accept()

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
        self.ui.btn_openSimSettings.clicked.connect(self.btn_openSimSettings_function)
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
        self.ui.spb_triggerSort_time.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_triggerElasticityMeasurement_time.valueChanged.connect(self.update_image_processing_para)

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
        """Sort ROI 模块"""
        self.btn_sortROI_view_state    = False
        self.ui.btn_sortROI_view.clicked.connect(self.btn_sortROI_view_function)
        self.ui.spb_sortROI_X.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_sortROI_Y.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_sortROI_width.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_sortROI_height.valueChanged.connect(self.update_image_processing_para)
        """Trapped ROI 模块"""
        self.btn_trappedROI_view_state = False
        self.ui.btn_trappedROI_view.clicked.connect(self.btn_trappedROI_view_function)
        self.ui.spb_trappedROI_X.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_trappedROI_Y.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_trappedROI_width.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_trappedROI_height.valueChanged.connect(self.update_image_processing_para)
        """sCMOS Elasticity Measurement ROI 模块"""
        """Collected ROI 模块"""
        self.btn_collectedROI_view_state = False
        self.ui.btn_collectedROI_view.clicked.connect(self.btn_collectedROI_view_function)
        self.ui.spb_collectedROI_X.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_collectedROI_Y.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_collectedROI_width.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_collectedROI_height.valueChanged.connect(self.update_image_processing_para)

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
        self.ui.btn_triggerSort.setEnabled(False)
        self.ui.btn_triggerRelease.setEnabled(False)
        self.ui.btn_triggerElasticityMeasurement.setEnabled(False)
        self.ui.btn_triggerCapture.clicked.connect(self.btn_triggerCapture_function)    # Trigger单片机发送信号的按钮连接
        self.ui.btn_triggerSort.clicked.connect(self.btn_triggerSort_function)
        self.ui.btn_triggerRelease.clicked.connect(self.btn_triggerRelease_function)
        self.ui.btn_triggerElasticityMeasurement.clicked.connect(self.slot_btn_triggerElasticityMeasurement_function)
                
        #单片机信号发生变化时更新子线程的参数
        self.ui.spb_triggerCapture_time.valueChanged.connect(self.updata_MCU_tirgger_para)
        self.ui.spb_triggerSort_time.valueChanged.connect(self.updata_MCU_tirgger_para)
        self.ui.spb_triggerRelease_time.valueChanged.connect(self.updata_MCU_tirgger_para)
        self.ui.spb_triggerElasticityMeasurement_time.valueChanged.connect(self.updata_MCU_tirgger_para)
           
        """Image Processing Settings 模块"""
        self.ui.spb_trappedIntervalTime.setEnabled(False)
        self.ui.spb_sortFrames.setEnabled(False)
        self.ui.spb_maxArea.setEnabled(False)
        self.ui.spb_minArea.setEnabled(False)       
        self.ui.spb_minLenth.setEnabled(False)
        self.ui.spb_maxLenth.setEnabled(False) 
        self.ui.spb_sCMOS_minArea.setEnabled(False)
        self.ui.btn_runScreenCell_continue.setEnabled(False)
        self.ui.btn_runScreenCell_single.setEnabled(False)
        self.ui.chb_isTarget.setChecked(False)

        self.ui.spb_minLenth.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_maxLenth.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_fastCamera_displayGray_max.valueChanged.connect(self.update_image_processing_para)
        #self.ui.spb_fastCamera_displayGray_max.valueChanged.connect(self.spb_fastCamera_displayGray_max_function)
        self.ui.spb_fastCamera_displayGray_max.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_sCMOS_displayGray_max.valueChanged.connect(self.update_image_processing_para)
        self.ui.btn_enterImageProcessingModel.setEnabled(False)
        self.ui.btn_enterImageProcessingModel.clicked.connect(self.btn_enterImageProcessingModel_function)
        self.ui.spb_minArea.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_maxArea.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_minLenth.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_sCMOS_minArea.valueChanged.connect(self.update_image_processing_para)
        self.ui.spb_sortFrames.valueChanged.connect(self.update_image_processing_para) 
        self.ui.spb_sortFrames.valueChanged.connect(self.update_image_processing_para)
        self.ui.btn_runScreenCell_continue.clicked.connect(self.update_image_processing_para)
        self.ui.btn_runScreenCell_single.clicked.connect(self.update_image_processing_para)
        self.ui.spb_trappedIntervalTime.valueChanged.connect(self.updata_MCU_tirgger_para)
        
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
        self.btn_rinseChannelElasticityMeasurement_state       = False
        self.btn_enterRinseChannelModel_state = False
        self.ui.btn_enterRinseChannelModel.setEnabled(False)
        self.ui.btn_rinseChannelSort.setEnabled(False)
        self.ui.btn_rinseChannelElasticityMeasurement.setEnabled(False)
        self.ui.btn_rinseChannelCapture.setEnabled(False)
        self.ui.btn_rinseChannelRelease.setEnabled(False)
        self.ui.btn_rinseChannelSort.clicked.connect(self.btn_rinseChannelSort_function)
        self.ui.btn_rinseChannelCapture.clicked.connect(self.btn_rinseChannelCapture_function)
        self.ui.btn_rinseChannelRelease.clicked.connect(self.btn_rinseChannelRelease_function)
        self.ui.btn_rinseChannelElasticityMeasurement.clicked.connect(self.btn_rinseChannelElasticityMeasurement_function)
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
        configure_settings = self.collect_current_settings()  # 复用参数收集
        # 弹出保存文件对话框
        file_path, _ = qw.QFileDialog.getSaveFileName(self, "保存参数", "", "JSON Files (*.json)")
        
        if file_path:
            try:
                self.persist_sim_app_config_from_ui()
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

    def btn_openSimSettings_function(self):
        resume_live_after_dialog = bool(
            self.sim_camera_connected
            and not self.sim_acquisition_in_progress
            and (self.sim_preview_requested or self.sim_preview_active)
        )
        if resume_live_after_dialog:
            self.sim_preview_requested = False
            self.stop_sim_preview(wait=True)
        self.ensure_sim_runtime()
        dialog = SimSettingsDialog(
            config=self.sim_app_config,
            parent=self,
            slm_adapter=self.sim_acquisition_controller.slm_adapter,
        )
        dialog.signal_settings_saved.connect(self.apply_sim_settings)
        dialog.exec_()
        if resume_live_after_dialog and self.sim_camera_connected and not self.sim_acquisition_in_progress:
            self.start_sim_preview()

    def apply_sim_settings(self, config):
        self.sim_app_config = app_config_from_dict(app_config_to_dict(config))
        save_app_config(self.sim_app_config, self.sim_app_config.config_path)
        self.prefer_real_sim_hardware(
            save_to_disk=True,
            probe_camera=not self.sim_camera_connected,
        )
        try:
            self.sim_acquisition_controller.apply_daq_config(self.sim_app_config.daq)
        except Exception as e:
            print(f"SIM DAQ apply failed: {str(e)}")
            qw.QMessageBox.warning(self, "SIM DAQ", str(e))
        if getattr(self, "sim_slm_connected", False):
            try:
                result = self.sim_acquisition_controller.select_running_order_for_task(
                    self.sim_app_config.selected_laser_nm,
                    self.sim_app_config.camera.exposure_us,
                )
                self.sim_app_config.selected_running_order = str(result.get("running_order_name", ""))
                save_app_config(self.sim_app_config, self.sim_app_config.config_path)
            except Exception as e:
                self.sim_app_config.selected_running_order = ""
                qw.QMessageBox.warning(self, "SIM SLM", str(e))
        self.sync_sim_camera_controls_from_config()
        self.refresh_sim_settings_summary()
        self.refresh_sim_camera_devices()
        refresh_slm = getattr(self, "refresh_sim_slm_devices", None)
        if callable(refresh_slm):
            refresh_slm()

    def refresh_sim_settings_summary(self):
        sim_config = app_config_from_dict(app_config_to_dict(self.sim_app_config))
        runtime_timing = dict(getattr(self, "sim_runtime_timing_snapshot", {}) or {})
        self.ui.pte_simSummary.setPlainText(build_sim_settings_summary(sim_config, runtime_timing=runtime_timing))

    def configure_sim_camera_spinboxes(self):
        for spinbox in (
            self.ui.spb_sCMOS_ROI_X,
            self.ui.spb_sCMOS_ROI_Y,
            self.ui.spb_sCMOS_exposureTime,
        ):
            spinbox.setKeyboardTracking(False)
        self.ui.spb_sCMOS_ROI_X.setSingleStep(SIM_CAMERA_ROI_STEP_PX)
        self.ui.spb_sCMOS_ROI_Y.setSingleStep(SIM_CAMERA_ROI_STEP_PX)

    def sync_sim_camera_roi_position_controls(self, controls_enabled=None):
        camera = self.sim_app_config.camera
        max_x, max_y = sim_camera_roi_origin_bounds(camera.roi_width, camera.roi_height)
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
        )
        self.ui.spb_sCMOS_ROI_X.setEnabled(roi_position_enabled)
        self.ui.spb_sCMOS_ROI_Y.setEnabled(roi_position_enabled)

    def sync_sim_camera_controls_from_config(self):
        camera = self.sim_app_config.camera
        bit_depth_combo = getattr(self.ui, "cmb_sCMOS_bitDepth", None)
        roi_width, roi_height, roi_x, roi_y = normalize_sim_camera_roi(
            camera.roi_width,
            camera.roi_height,
            camera.roi_x,
            camera.roi_y,
        )
        camera.roi_width = roi_width
        camera.roi_height = roi_height
        camera.roi_x = roi_x
        camera.roi_y = roi_y
        blocked_widgets = [
            self.ui.cmb_sCMOS_imageSize,
            self.ui.spb_sCMOS_exposureTime,
        ]
        if bit_depth_combo is not None:
            blocked_widgets.append(bit_depth_combo)
        for widget in blocked_widgets:
            widget.blockSignals(True)
        try:
            self.ui.cmb_sCMOS_imageSize.setCurrentIndex(
                preset_index_for_sim_camera_size(camera.roi_width, camera.roi_height)
            )
            self.ui.spb_sCMOS_exposureTime.setValue(sim_exposure_us_to_ms(camera.exposure_us))
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

    def sync_sim_camera_config_from_ui(self, save_to_disk=True):
        camera = self.sim_app_config.camera
        bit_depth_combo = getattr(self.ui, "cmb_sCMOS_bitDepth", None)
        roi_width, roi_height = size_from_sim_camera_label(self.ui.cmb_sCMOS_imageSize.currentText())
        roi_width, roi_height, roi_x, roi_y = normalize_sim_camera_roi(
            roi_width,
            roi_height,
            self.ui.spb_sCMOS_ROI_X.value(),
            self.ui.spb_sCMOS_ROI_Y.value(),
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

    def toggle_sim_slm_connection(self):
        self.ensure_sim_runtime()
        if self.sim_slm_connected:
            try:
                self.sim_acquisition_controller.disconnect_slm()
            except Exception as e:
                print(f"SIM SLM disconnect failed: {str(e)}")
                qw.QMessageBox.warning(self, "SIM SLM", str(e))
                return
            self.sim_slm_connected = False
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
        try:
            self.sim_acquisition_controller.connect_slm(device_path=device_path or None)
            self.sim_slm_connected = True
            self.select_current_sim_running_order(save_to_disk=True)
        except Exception as e:
            self.sim_slm_connected = False
            print(f"SIM SLM connect failed: {str(e)}")
            qw.QMessageBox.warning(self, "SIM SLM", str(e))
        finally:
            self.refresh_sim_slm_devices()

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

    def btn_sCMOS_refresh_function(self):
        self.refresh_sim_camera_devices(show_dialog_on_error=True)

    def on_sim_camera_setting_changed(self):
        self.sync_sim_camera_config_from_ui(save_to_disk=False)
        if getattr(self, "sim_slm_connected", False):
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
            self.sim_preview_restart_timer.start(150)
            return
        try:
            MainWindow.apply_connected_sim_camera_config(self)
        except Exception as e:
            print(f"SIM camera config refresh failed: {str(e)}")
            qw.QMessageBox.warning(self, "SIM Camera", str(e))

    def on_sim_camera_size_activated(self, *_):
        roi_width, roi_height = size_from_sim_camera_label(self.ui.cmb_sCMOS_imageSize.currentText())
        if (
            roi_width == int(self.sim_app_config.camera.roi_width)
            and roi_height == int(self.sim_app_config.camera.roi_height)
        ):
            return
        self.on_sim_camera_setting_changed()

    def ensure_sim_runtime(self):
        backend = self.sim_app_config.backend
        signature = (
            backend.fusion_bt_sdk_path,
            backend.slm_sdk_path,
            backend.simulation_mode,
        )
        if self.sim_acquisition_controller is not None and signature == self.sim_runtime_backend_signature:
            return
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
        self.sim_acquisition_controller.signal_status_changed.connect(self.slot_handle_sim_acquisition_status)
        self.sim_acquisition_controller.signal_acquisition_ready.connect(self.slot_handle_sim_acquisition_ready)
        self.sim_acquisition_controller.signal_acquisition_failed.connect(self.slot_handle_sim_acquisition_failed)
        self.sim_camera_adapter = self.sim_acquisition_controller.camera_adapter
        self.sim_preview_controller = SimPreviewController(self.sim_camera_adapter, self)
        self.sim_preview_controller.signal_error.connect(self.slot_handle_sim_preview_error)
        self.sim_preview_controller.signal_status_changed.connect(self.slot_handle_sim_preview_status)
        self.sim_runtime_backend_signature = signature
        self.sim_preview_backend_signature = signature

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
        existing_group = getattr(self.ui, "grp_simRuntime", None)
        existing_leds = {
            "camera": getattr(self.ui, "led_simRuntimeCamera", None),
            "slm": getattr(self.ui, "led_simRuntimeSlm", None),
            "daq": getattr(self.ui, "led_simRuntimeDaq", None),
        }
        existing_labels = {
            "camera": getattr(self.ui, "lbl_simRuntimeCameraStatus", None),
            "slm": getattr(self.ui, "lbl_simRuntimeSlmStatus", None),
            "daq": getattr(self.ui, "lbl_simRuntimeDaqStatus", None),
        }
        if existing_group is not None and all(existing_leds.values()) and all(existing_labels.values()):
            self.sim_runtime_leds = existing_leds
            self.sim_runtime_status_labels = existing_labels
            for device in ("camera", "slm", "daq"):
                self.set_sim_runtime_led_state(existing_leds[device], "gray")
                existing_labels[device].setText("Not initialized")
            return

        layout = getattr(self.ui, "verticalLayout_simConfiguration", None)
        if layout is None:
            return
        group = qw.QGroupBox("SIM Runtime", self.ui.grp_simConfiguration)
        status_layout = qw.QVBoxLayout(group)
        self.sim_runtime_leds = {}
        self.sim_runtime_status_labels = {}
        for key, label_text in (("camera", "Camera"), ("slm", "SLM"), ("daq", "DAQ")):
            row = qw.QHBoxLayout()
            led = LedIndicator("gray", group)
            label = qw.QLabel("Not initialized", group)
            self.sim_runtime_leds[key] = led
            self.sim_runtime_status_labels[key] = label
            row.addWidget(led)
            row.addWidget(qw.QLabel(label_text, group))
            row.addStretch(1)
            row.addWidget(label)
            status_layout.addLayout(row)
        insert_index = 2 if hasattr(self.ui, "grp_hardvareConnection_SLM") else 1
        layout.insertWidget(insert_index, group)

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
        elif status in {"acquisition_failed", "hardware_error"} or str(status).endswith("failed"):
            for device in ("camera", "slm", "daq"):
                set_device(device, "red", "Error")

    def start_sim_preview(self):
        if self.sim_acquisition_in_progress:
            print("SIM preview start skipped because acquisition is in progress.")
            return
        if not self.sim_camera_connected:
            qw.QMessageBox.information(self, "SIM Camera", "Please connect the SIM camera before starting Live.")
            return
        self.sim_preview_requested = True
        if self.sim_preview_stop_in_progress:
            self.sim_preview_restart_requested = True
            self.update_sim_camera_action_buttons()
            return
        self.sync_sim_camera_config_from_ui(save_to_disk=False)
        self.ensure_sim_runtime()
        try:
            MainWindow.apply_connected_sim_camera_config(self)
        except Exception as e:
            self.sim_preview_requested = False
            self.sim_preview_restart_requested = False
            print(f"SIM camera config apply before preview failed: {str(e)}")
            qw.QMessageBox.warning(self, "SIM Camera", str(e))
            self.update_sim_camera_action_buttons()
            return
        auto_contrast_state = getattr(self, "sim_auto_contrast_state", None)
        if auto_contrast_state is not None:
            auto_contrast_state.reset()
        self.sim_preview_controller.start(self.sim_app_config.camera, timeout_ms=200)
        self.sim_preview_restart_requested = False
        self.sim_preview_stop_in_progress = False
        self.sim_preview_active = True
        self.update_sim_camera_action_buttons()

    def stop_sim_preview(self, wait=True, clear_restart=True, clear_display=False):
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
            return
        self.sim_preview_controller.stop(wait=wait)
        if wait:
            self.sim_preview_stop_in_progress = False
            self.update_sim_camera_action_buttons()

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
            return
        if status == "preview_stopped":
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
            if pending_restart and self.sim_camera_connected and not self.sim_acquisition_in_progress:
                self.start_sim_preview()

    def slot_handle_sim_preview_error(self, message):
        self.sim_preview_requested = False
        self.sim_preview_restart_requested = False
        self.stop_sim_preview(wait=False)
        print(f"SIM preview error: {message}")
        qw.QMessageBox.warning(self, "SIM Preview Error", message.splitlines()[0])

    def trigger_sim_formal_acquisition(self, trigger_source="manual"):
        if self.sim_acquisition_in_progress:
            print("SIM acquisition skipped because another acquisition is still running.")
            return
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
        preview_should_resume = bool(
            getattr(self, "sim_preview_requested", self.sim_preview_active or self.sim_preview_restart_requested)
        )
        self.sim_resume_preview_after_acquisition = (
            preview_should_resume
        )
        self.sim_preview_requested = False
        self.sim_preview_restart_requested = False
        if self.sim_preview_active or self.sim_preview_stop_in_progress:
            self.stop_sim_preview(wait=True)
        self.sim_acquisition_in_progress = True
        self.update_sim_camera_action_buttons()
        self.set_sim_camera_controls_enabled(False)
        self.sim_last_acquisition_batch = None
        self.sim_current_task_id = ""
        try:
            self.sim_acquisition_controller.initialize_hardware()
            self.sim_acquisition_controller.apply_daq_config(self.sim_app_config.daq)
            self.sim_acquisition_controller.apply_camera_config(self.sim_app_config.camera)
            ro_result = self.sim_acquisition_controller.select_running_order_for_task(
                self.sim_app_config.selected_laser_nm,
                self.sim_app_config.camera.exposure_us,
            )
            self.sim_app_config.selected_running_order = str(ro_result.get("running_order_name", ""))
            refresh_summary = getattr(self, "refresh_sim_settings_summary", None)
            if callable(refresh_summary):
                refresh_summary()
            pattern_result = getattr(self.sim_acquisition_controller, "pattern_result", None)
            selected_pattern_files = list(
                getattr(pattern_result, "pattern_files", self.sim_app_config.pattern_files)
            )
            task = SimTaskConfig(
                laser_wavelength_nm=self.sim_app_config.selected_laser_nm,
                pattern_files=selected_pattern_files,
                running_order_name=self.sim_app_config.selected_running_order,
                camera=app_config_from_dict(app_config_to_dict(self.sim_app_config)).camera,
                timing=app_config_from_dict(app_config_to_dict(self.sim_app_config)).timing,
            )
            self.sim_current_task_id = self.sim_acquisition_controller.start_single_acquisition(task)
            print(f"SIM acquisition started from {trigger_source}: {self.sim_current_task_id}")
        except Exception as e:
            self.sim_acquisition_in_progress = False
            self.update_sim_camera_action_buttons()
            self.set_sim_camera_controls_enabled(self.sim_camera_connected)
            print(f"SIM acquisition start failed ({trigger_source}): {str(e)}")
            if self.sim_resume_preview_after_acquisition and self.sim_camera_connected:
                self.sim_resume_preview_after_acquisition = False
                self.start_sim_preview()
            qw.QMessageBox.warning(self, "SIM Acquisition Error", str(e))

    def update_sim_runtime_timing_from_payload(self, payload):
        payload = dict(payload or {})
        self.sim_runtime_timing_snapshot = payload
        if payload.get("applied_bit_depth") is not None and hasattr(self, "sim_app_config"):
            self.sim_app_config.camera.bit_depth = int(payload["applied_bit_depth"])
        supported_bit_depths = payload.get("supported_bit_depths")
        if supported_bit_depths:
            MainWindow.refresh_sim_camera_bit_depth_choices(self, supported_bit_depths)
        else:
            self.refresh_sim_settings_summary()

    def apply_connected_sim_camera_config(self):
        if not getattr(self, "sim_camera_connected", False):
            return {}
        controller = getattr(self, "sim_acquisition_controller", None)
        if controller is None:
            return {}
        result = controller.apply_camera_config(self.sim_app_config.camera) or {}
        payload = {
            "camera_config": dict(getattr(self.sim_app_config.camera, "__dict__", {})),
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
        elif status in {"acquisition_complete", "patterns_prepared", "running_order_selected", "camera_connected", "frame_captured", "slm_connected", "slm_disconnected", "daq_config_applied"}:
            print(f"SIM acquisition status: {status} {payload}")

    def slot_handle_sim_acquisition_ready(self, payload):
        self.sim_acquisition_in_progress = False
        self.update_sim_camera_action_buttons()
        self.set_sim_camera_controls_enabled(self.sim_camera_connected)
        self.sim_last_acquisition_batch = payload
        stack = payload.get("stack") if hasattr(payload, "get") else getattr(payload, "stack", None)
        if stack is not None and getattr(stack, "size", 0):
            self.sim_last_preview_frame = stack[0]
        task_id = payload.get("task_id", self.sim_current_task_id) if hasattr(payload, "get") else getattr(payload, "task_id", self.sim_current_task_id)
        self.sim_current_task_id = task_id
        update_runtime = getattr(self, "update_sim_runtime_status_widgets", None)
        if callable(update_runtime):
            update_runtime("acquisition_complete", {"task_id": task_id})
        print(f"SIM acquisition ready: {task_id}")
        if self.sim_resume_preview_after_acquisition and self.sim_camera_connected:
            self.sim_resume_preview_after_acquisition = False
            self.start_sim_preview()

    def slot_handle_sim_acquisition_failed(self, task_id, message):
        self.sim_acquisition_in_progress = False
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
        qw.QMessageBox.warning(self, "SIM Acquisition Error", message.splitlines()[0])

    def collect_current_settings(self):
        """收集当前所有控件的参数值，返回字典"""
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
        sim_roi_width, sim_roi_height = size_from_sim_camera_label(self.ui.cmb_sCMOS_imageSize.currentText())
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
        #Elasticity Measurement ROI 模块
        configure_settings['spb_sCMOS_elasticityMeasurementROI_X']      = self.elasticityROI_X
        configure_settings['spb_sCMOS_elasticityMeasurementROI_Y']      = self.elasticityROI_Y
        configure_settings['spb_sCMOS_elasticityMeasurementROI_width']  = self.elasticityROI_width
        configure_settings['spb_sCMOS_elasticityMeasurementROI_height'] = self.elasticityROI_heigh
        #Release ROI 模块
        configure_settings['spb_releaseROI_X']      = self.ui.spb_cellFlowThroughROI_X.value()
        configure_settings['spb_releaseROI_Y']      = self.ui.spb_cellFlowThroughROI_Y.value()
        configure_settings['spb_releaseROI_width']  = self.ui.spb_cellFlowThroughROI_width.value()
        configure_settings['spb_releaseROI_height'] = self.ui.spb_cellFlowThroughROI_height.value()
        #sort ROI 模块
        configure_settings['spb_sortROI_X']         = self.ui.spb_sortROI_X.value()
        configure_settings['spb_sortROI_Y']         = self.ui.spb_sortROI_Y.value()
        configure_settings['spb_sortROI_width']     = self.ui.spb_sortROI_width.value()
        configure_settings['spb_sortROI_height']    = self.ui.spb_sortROI_height.value()
        # Collected Cells ROI 模块
        configure_settings['spb_collectedCellsROI_X']         = self.ui.spb_collectedROI_X.value()
        configure_settings['spb_collectedCellsROI_Y']         = self.ui.spb_collectedROI_Y.value()
        configure_settings['spb_collectedCellsROI_width']     = self.ui.spb_collectedROI_width.value()
        configure_settings['spb_collectedCellsROI_height']    = self.ui.spb_collectedROI_height.value()

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
        configure_settings['spb_triggerSort_time']          = self.ui.spb_triggerSort_time.value()
        configure_settings['spb_triggerRelease_time']       = self.ui.spb_triggerRelease_time.value()
        configure_settings['spb_triggerElasticityMeasurement_time']       = self.ui.spb_triggerElasticityMeasurement_time.value()
        #Image Processing Settings 模块
        configure_settings['spb_trappedIntervalTime']   = self.ui.spb_trappedIntervalTime.value()
        configure_settings['spb_sortFrames']            = self.ui.spb_sortFrames.value()
        configure_settings['spb_minArea']               = self.ui.spb_minArea.value()
        configure_settings['spb_maxArea']               = self.ui.spb_maxArea.value()
        configure_settings['spb_minLenth']               = self.ui.spb_minLenth.value()
        configure_settings['spb_maxLenth']               = self.ui.spb_maxLenth.value()
        configure_settings['spb_threshold_Bi_sCMOS']     = self.sCMOS_Bi_threshold
        configure_settings['spb_sCMOS_morphologyKernel_size'] = self.sCMOS_morphologyKernel.shape[0]
        configure_settings['spb_sCMOS_openTimes']        = self.sCMOS_openTimes
        configure_settings['spb_sCMOS_closeTimes']       = self.sCMOS_closeTimes
        configure_settings['spb_sCMOS_minArea']          = self.ui.spb_sCMOS_minArea.value()
        #Binary ROI 模块
        configure_settings['spb_threshold_Bi'] = self.ui.spb_threshold_Bi.value()

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

    def load_configure_settings(self, file_path):
        """通用配置加载逻辑"""
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
            self.ui.cmb_sCMOS_imageSize.setCurrentIndex(preset_index_for_sim_camera_size(sim_width, sim_height))
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
            # 设置Elasticity Measurement ROI 模块
            self.elasticityROI_X = configure_settings.get('spb_sCMOS_elasticityMeasurementROI_X', 0)
            self.elasticityROI_Y = configure_settings.get('spb_sCMOS_elasticityMeasurementROI_Y', 0)
            self.elasticityROI_width = configure_settings.get('spb_sCMOS_elasticityMeasurementROI_width', 70)
            self.elasticityROI_heigh = configure_settings.get('spb_sCMOS_elasticityMeasurementROI_height', 70)
            # 设置 cell flow rate ROI 模块
            self.ui.spb_cellFlowThroughROI_X.setValue(configure_settings.get('spb_releaseROI_X', 0))
            self.ui.spb_cellFlowThroughROI_Y.setValue(configure_settings.get('spb_releaseROI_Y', 0))
            self.ui.spb_cellFlowThroughROI_width.setValue(configure_settings.get('spb_releaseROI_width', 320))
            self.ui.spb_cellFlowThroughROI_height.setValue(configure_settings.get('spb_releaseROI_height', 40))
            # 设置 sort ROI 模块
            self.ui.spb_sortROI_X.setValue(configure_settings.get('spb_sortROI_X', 0))
            self.ui.spb_sortROI_Y.setValue(configure_settings.get('spb_sortROI_Y', 0))
            self.ui.spb_sortROI_width.setValue(configure_settings.get('spb_sortROI_width', 0))
            self.ui.spb_sortROI_height.setValue(configure_settings.get('spb_sortROI_height', 0))
            # 设置Collected Cells ROI
            self.ui.spb_collectedROI_X.setValue(configure_settings.get('spb_collectedCellsROI_X', 0))
            self.ui.spb_collectedROI_Y.setValue(configure_settings.get('spb_collectedCellsROI_Y', 0))
            self.ui.spb_collectedROI_width.setValue(configure_settings.get('spb_collectedCellsROI_width', 0))
            self.ui.spb_collectedROI_height.setValue(configure_settings.get('spb_collectedCellsROI_height', 0))
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
            self.ui.spb_triggerSort_time.setValue(configure_settings.get('spb_triggerSort_time', 1))
            self.ui.spb_triggerRelease_time.setValue(configure_settings.get('spb_triggerRelease_time', 1))
            self.ui.spb_triggerElasticityMeasurement_time.setValue(configure_settings.get('spb_triggerElasticityMeasurement_time', 1))
            
            # 设置 Image Processing Settings 模块
            self.ui.spb_minArea.setValue(configure_settings.get('spb_minArea', 120))
            self.ui.spb_maxArea.setValue(configure_settings.get('spb_maxArea', 300))
            self.ui.spb_minLenth.setValue(configure_settings.get('spb_minLenth', 0))
            self.ui.spb_maxLenth.setValue(configure_settings.get('spb_maxLenth', 0))
            morphology_size = configure_settings.get('spb_sCMOS_morphologyKernel_size', 13)
            self.sCMOS_Bi_threshold = configure_settings.get('spb_threshold_Bi_sCMOS', 150)
            self.sCMOS_openTimes = configure_settings.get('spb_sCMOS_openTimes', 1)
            self.sCMOS_closeTimes = configure_settings.get('spb_sCMOS_closeTimes', 1)
            self.sCMOS_morphologyKernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (morphology_size, morphology_size))
            self.ui.spb_sCMOS_minArea.setValue(configure_settings.get('spb_sCMOS_minArea', 3000))

            self.ui.spb_trappedIntervalTime.setValue(configure_settings.get('spb_trappedIntervalTime', 30))
            self.ui.spb_sortFrames.setValue(configure_settings.get('spb_sortFrames', 50))

            # 设置 Binary ROI 模块
            self.ui.spb_threshold_Bi.setValue(configure_settings.get('spb_threshold_Bi', 120))
            sim_control_payload = configure_settings.get('sim_control')
            if sim_control_payload:
                self.sim_app_config = merge_legacy_sim_control_payload(self.sim_app_config, sim_control_payload)
                save_app_config(self.sim_app_config, self.sim_app_config.config_path)
            else:
                self.sync_sim_camera_config_from_ui(save_to_disk=False)
            self.sync_sim_camera_controls_from_config()
            self.refresh_sim_settings_summary()

            
            # 加载其他参数...
            # 仅在手动加载时弹出提示
            if self.is_auto_loading:  # 需要定义一个标志位区分自动/手动加载
                self.is_auto_loading = True
                qw.QMessageBox.information(self, "Succeed", "Loaded Successfully!")
                
            self.update_image_processing_para()
            
        except Exception as e:
            error_msg = f"加载配置失败：{str(e)}"
            qw.QMessageBox.critical(self, "错误", error_msg)
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

           self.ui.spb_trappedIntervalTime.setEnabled(False)
           self.ui.spb_sortFrames.setEnabled(False)
           self.ui.btn_runScreenCell_single.setEnabled(False)
           self.ui.btn_runScreenCell_continue.setEnabled(False)  
           self.ui.spb_minLenth.setEnabled(False)
           self.ui.spb_maxLenth.setEnabled(False)
           self.ui.spb_sCMOS_minArea.setEnabled(False)
                  
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
            self.ui.spb_trappedIntervalTime.setEnabled(True)
            self.ui.spb_sortFrames.setEnabled(True)
            self.ui.spb_minLenth.setEnabled(True)
            self.ui.spb_maxLenth.setEnabled(True)
            self.ui.spb_sCMOS_minArea.setEnabled(True)

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
        self.totalNumb_sort = 0
        self.totalNumb_collected = 0
        self.totalNumb_elasticityMeasurement_start = 0
        self.totalNumb_elasticityMeasurement_end = 0
        self.trappedCell_miss  = 0
        self.sortCell_miss  = 0
        self.collectedCell_miss  = 0
        self.elasticityMeasurement_start_miss = 0
        self.elasticityMeasurement_end_miss = 0
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
        #将Elasticity Measurement ROI
        """将图像处理的参数发给fastCamera图像处理线程"""
        para = {}
        # image processing settings
        para["threshold_Bi"]            = self.ui.spb_threshold_Bi.value()
        para["minArea"]                 = self.ui.spb_minArea.value()
        para["maxArea"]                 = self.ui.spb_maxArea.value()
        para["sortFrames"]              = self.ui.spb_sortFrames.value()
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
        # sort ROI
        para["sortROI_X"]               = self.ui.spb_sortROI_X.value()
        para["sortROI_Y"]               = self.ui.spb_sortROI_Y.value()
        para["sortROI_width"]           = self.ui.spb_sortROI_width.value()
        para["sortROI_height"]          = self.ui.spb_sortROI_height.value()

        # collected Cells ROI
        para["collectedROI_X"]               = self.ui.spb_collectedROI_X.value()
        para["collectedROI_Y"]               = self.ui.spb_collectedROI_Y.value()
        para["collectedROI_width"]           = self.ui.spb_collectedROI_width.value()
        para["collectedROI_height"]          = self.ui.spb_collectedROI_height.value()

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

        self.sCMOS_minArea      = self.ui.spb_sCMOS_minArea.value()
        sCMOS_para = {}
        sCMOS_para["minDeltaLenth"]           = self.ui.spb_minLenth.value()
        sCMOS_para["maxDeltaLenth"]           = self.ui.spb_maxLenth.value()
        sCMOS_para["sCMOS_gaussianKernel"]    = self.sCMOS_gaussianKernel 
        sCMOS_para["sCMOS_gaussBlurSigma"]    = self.sCMOS_gaussBlurSigma 

        sCMOS_para["threshold_Bi_sCMOS"]      = self.sCMOS_Bi_threshold
        sCMOS_para["morphologyKernel"]        = self.sCMOS_morphologyKernel
        sCMOS_para["openTimes"]               = self.sCMOS_openTimes
        sCMOS_para["closeTimes"]              = self.sCMOS_closeTimes
        sCMOS_para["sCMOS_minArea"]           = self.ui.spb_sCMOS_minArea.value()
        sCMOS_para["displayGray_max"]         = self.ui.spb_sCMOS_displayGray_max.value()
        
        # Elasticity Measurement ROI
        sCMOS_para["elasticityROI_X"]         = self.elasticityROI_X
        sCMOS_para["elasticityROI_Y"]         = self.elasticityROI_Y
        sCMOS_para["elasticityROI_width"]     = self.elasticityROI_width
        sCMOS_para["elasticityROI_height"]    = self.elasticityROI_heigh

        self.signal_sendImageProcessingPara_sCMOS.emit(sCMOS_para)
        #图像保存的名称信息
        experiment_imfo = {}
        # 只有btn_ensurePresureInfo为真时，才能保存压力参数作为文件名
        experiment_imfo["spb_triggerCapture_time"] = str(self.ui.spb_triggerCapture_time.value())
        experiment_imfo["spb_triggerElasticityMeasurement_time"] = str(self.ui.spb_triggerElasticityMeasurement_time.value())
        experiment_imfo["spb_triggerRelease_time"] = str(self.ui.spb_triggerRelease_time.value())
        experiment_imfo["spb_triggerSort_time"] = str(self.ui.spb_triggerSort_time.value())
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
            3: ("Sort_ROI",self.ui.lb_sortROIView_original,self.ui.lb_sortROIView_processed,self.ui.lb_sortROIView_target),
            4: ("Collected_ROI",self.ui.lb_collectedROIView_original,self.ui.lb_collectedROIView_processed,self.ui.lb_collectedROIView_target),
            5: ("flowRate_ROI_start",self.ui.lb_flowRateDetectROIView_original_start,self.ui.lb_flowRateDetectROIView_processed_start,self.ui.lb_flowRateDetectROIView_target_start),
            6: ("flowRate_ROI_end"  ,self.ui.lb_flowRateDetectROIView_original_end  ,self.ui.lb_flowRateDetectROIView_processed_end  ,self.ui.lb_flowRateDetectROIView_target_end),
        }
        #参数为0时-capture:1.ID; 2.area; 3.X; 4.Y; 5.total; 6.算法时间; 7. 间隔时间;
        #参数为1时-capture:1.ID; 2.area; 3.total; 4.miss;  5.ROI_result-state;
        #参数为2时-capture:1.ID;  2.total; 3.算法时间; 4. 间隔时间; 
        #参数为3\4时-capture:1.ID; 2.area; 3.X; 4.Y; 5.total; 6.miss;  7.算法时间; 8. 间隔时间; 9.ROI_result-state;
        #参数为5时-capture:1.ID; 2.area; 3.X; 
        #参数为6时-capture:1.ID; 2.area; 3.X; 4. 间隔时间; 5.ROI_result-state;
        #7代表弹性形变ROI分析的参数。1.ID; 2.total; 3.miss; 4.state; 5.Lenth(pixel)就是轮廓最低点的y位置;
        self.roi_display_lb = {
            0: (self.ui.lb_captureCell_id,self.ui.lb_captureCellArea, self.ui.lb_captureCell_X,self.ui.lb_captureCell_Y,self.ui.lb_captureCell_total,self.ui.lb_captureCell_algorithmTime,self.ui.lb_captureCell_intervalTime),
            1: (self.ui.lb_trappedCell_id,self.ui.lb_trappedCellArea,self.ui.lb_trappedCell_total,self.ui.lb_trappedCell_miss,self.ui.lb_trappedROI_state),
            2: (self.ui.lb_releaseCell_id,self.ui.lb_releaseCell_total,self.ui.lb_releaseCell_algorithmTime,self.ui.lb_releaseCell_intervalTime),
            3: (self.ui.lb_sortCell_id,self.ui.lb_sortCellArea, self.ui.lb_sortCell_X,self.ui.lb_sortCell_Y,self.ui.lb_sortCell_total,self.ui.lb_sortCell_miss,self.ui.lb_sortCell_algorithmTime,self.ui.lb_sortCell_intervalTime,self.ui.lb_sortROI_state),
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
        # sort ROI
        self.sortROI_X          = self.ui.spb_sortROI_X.value()
        self.sortROI_Y          = self.ui.spb_sortROI_Y.value()
        self.sortROI_width      = self.ui.spb_sortROI_width.value()
        self.sortROI_height     = self.ui.spb_sortROI_height.value()
        # collected ROI
        self.collectedROI_X = self.ui.spb_collectedROI_X.value()
        self.collectedROI_Y = self.ui.spb_collectedROI_Y.value()
        self.collectedROI_width = self.ui.spb_collectedROI_width.value()
        self.collectedROI_height = self.ui.spb_collectedROI_height.value()
        # flow rate ROI
        self.flowRateROI_X = self.ui.spb_flowRateROI_X.value()
        self.flowRateROI_Y = self.ui.spb_flowRateROI_Y.value()
        self.flowRateROI_width  = self.ui.spb_flowRateROI_width.value()
        self.flowRateROI_height = self.ui.spb_flowRateROI_height.value()

        self.roi_way = {
            0: (self.captureROI_X+self.cellFlowThroughROI_X, self.cellFlowThroughROI_Y, self.captureROI_width, self.cellFlowThroughROI_height),
            1: (self.trappedROI_X, self.trappedROI_Y, self.trappedROI_width, self.trappedROI_height),
            2: (self.cellFlowThroughROI_X, self.cellFlowThroughROI_Y, self.cellFlowThroughROI_width, self.cellFlowThroughROI_height),
            3: (self.sortROI_X, self.sortROI_Y, self.sortROI_width, self.sortROI_height),
            4: (self.collectedROI_X, self.collectedROI_Y, self.collectedROI_width, self.collectedROI_height),
            5: (self.flowRateROI_X, self.flowRateROI_Y, self.flowRateROI_width, self.flowRateROI_height), #start flowRateROI 
            6: (self.flowRateROI_X, self.flowRateROI_Y, self.flowRateROI_width, self.flowRateROI_height), #start flowRateROI 
        }
        self.roi_diff_disply_lb = {
            1:(self.ui.lb_trappedROIView_BgDiff_Bi),
            2:(self.ui.lb_cellFlowThroughROIView_BgDiff_Bi),
            3:(self.ui.lb_sortROIView_BgDiff_Bi),
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
    def btn_sortROI_view_function(self):
        """sort ROI启动按钮功能"""
        if self.btn_sortROI_view_state:
            self.btn_sortROI_view_state = False
            self.ui.btn_sortROI_view.setStyleSheet("background-color: #E1E1E1")
        else:
            self.btn_sortROI_view_state = True
            self.ui.btn_sortROI_view.setStyleSheet("background-color: #87CEEB")
 
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

        try:
            connection_info = self.sim_acquisition_controller.connect_camera(
                device_index=self.sim_app_config.camera.device_index,
                device_label=self.sim_app_config.camera.device_label,
            )
            camera_result = self.sim_acquisition_controller.apply_camera_config(self.sim_app_config.camera) or {}
            supported_bit_depths = camera_result.get("supported_bit_depths")
            if not supported_bit_depths and isinstance(connection_info, dict):
                supported_bit_depths = connection_info.get("supported_bit_depths")
            if supported_bit_depths:
                MainWindow.refresh_sim_camera_bit_depth_choices(self, supported_bit_depths)
        except Exception as e:
            try:
                self.sim_acquisition_controller.disconnect_camera()
            except Exception as cleanup_error:
                print(f"SIM camera cleanup after connection failure failed: {str(cleanup_error)}")
            print(f"SIM camera connection failed: {str(e)}")
            qw.QMessageBox.warning(self, "SIM Camera", str(e))
            self.sim_camera_connected = False
            self.sim_preview_requested = False
            self.set_sim_camera_controls_enabled(False)
            self.update_sim_camera_action_buttons()
            return

        self.sim_camera_connected = True
        self.set_sim_camera_controls_enabled(True)
        self.update_sim_camera_action_buttons()

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
    
    "更新sCMOS elasticity measurement的图"
    @pyqtSlot()
    def slot_sCMOS_Updata_elasticityTrigger(self):
        try:
            self.trigger_sim_formal_acquisition("mcu_elasticity_trigger")
        except Exception as e:
            print(f"sCMOS的elasticity索引发送转真: {str(e)}")   

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
        #sort信号连接
        self.signal_btn_triggerSort.connect(self.MCUTriggerThread.worker.slot_btn_triggerSort)
        self.MCUTriggerThread.worker.signal_btn_triggerSort_finish.connect(self.slot_btn_triggerSort_finish)
        #elasticity Measurement信号连接
        self.signal_btn_triggerElasticityMeasurement.connect(self.MCUTriggerThread.worker.slot_btn_triggerElasticityMeasurement)
        self.MCUTriggerThread.worker.signal_btn_triggerElasticityMeasurement_finish.connect(self.slot_btn_triggerElasticityMeasurement_finish)
        self.MCUTriggerThread.worker.signal_btn_triggerElasticityMeasurement_start.connect(self.slot_btn_triggerElasticityMeasurement_function)
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
        self.signal_btn_rinseChannelElasticityMeasurement.connect(self.MCUTriggerThread.worker.slot_btn_rinseChannelElasticityMeasurement)
        self.signal_btn_rinseChannel_OFF.connect(self.MCUTriggerThread.worker.slot_btn_rinseChannel_OFF) 
        self.MCUTriggerThread.worker.signal_sCMOS_BgUpdata_captureTrigger.connect(self.slot_sCMOS_BgUpdata_captureTrigger)
        self.MCUTriggerThread.worker.signal_sCMOS_enterImageProcessor.connect(self.slot_sCMOS_Updata_elasticityTrigger) 
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

        # 完全捕获住细胞的间隔时间
        para["trappedIntervalTime"]                 = self.ui.spb_trappedIntervalTime.value()
        # Trigger的参数
        para["spb_triggerCapture_time"]                 = self.ui.spb_triggerCapture_time.value() * 10  #整数单位100us
        para["spb_triggerElasticityMeasurement_time"]   = self.ui.spb_triggerElasticityMeasurement_time.value() * 10  #整数单位100us
        para["spb_triggerRelease_time"]                 = self.ui.spb_triggerRelease_time.value() * 10  #整数单位100us
        para["spb_triggerSort_time"]                    = self.ui.spb_triggerSort_time.value() * 10     #整数单位100us
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
        if self.btn_sortROI_view_state:
            self.btn_sortROI_view_function()
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
        self.ui.btn_triggerElasticityMeasurement.setEnabled(False)
        self.ui.btn_triggerSort.setEnabled(False)
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
            self.ui.btn_triggerElasticityMeasurement.setEnabled(True)
            self.ui.btn_triggerSort.setEnabled(True)
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
            if self.btn_sortROI_view_state:
                # Sort ROI 显示 天蓝色显示
                self.ROI_live_view(display_frame,self.sortROI_X,self.sortROI_Y,self.sortROI_width,self.sortROI_height,235,206,135)  # BGR #87CEEB
            if self.btn_trappedROI_view_state:
                # trapped ROI 显示 粉红色显示
                self.ROI_live_view(display_frame,self.trappedROI_X,self.trappedROI_Y,self.trappedROI_width,self.trappedROI_height,204,204,255)  # BGR #FFCCCC
             
            if self.btn_flowRateROI_view_state:
                # 测试流速ROI淡蓝色
                self.ROI_live_view(display_frame,self.flowRateROI_X,self.flowRateROI_Y,self.flowRateROI_width,self.flowRateROI_height,255,255,187)  # BGR #BBFFFF
            if self.btn_collectedROI_view_state:
                # 搜集的细胞ROI鲜红色
                self.ROI_live_view(display_frame,self.collectedROI_X,self.collectedROI_Y,self.collectedROI_width,self.collectedROI_height,255,221,231) # BGR #E7DDFF
            
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
                for i in (1,2,3,4):
                    roi_x, roi_y, roi_w, roi_h = self.roi_way[i]
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

        elif imageProcessing_way == 3: # sort_roi
            ID_lb ,area_lb, x_lb, y_lb,total_lb,miss_lb,algorithm_time_lb,interval_time_lb,roi_state_lb = self.roi_display_lb[imageProcessing_way]
            self.totalNumb_sort = self.totalNumb_sort + total_add
            self.sortCell_miss  = self.sortCell_miss + miss_add
            ID =self.cell_ID
            ID_lb.setText(str(self.cell_ID))
            area_lb.setText(str(cell_area))
            x_lb.setText(str(cell_cX))
            y_lb.setText(str(cell_cY))
            total_lb.setText(str(self.totalNumb_sort))
            miss_lb.setText(str(self.sortCell_miss))
            algorithm_time_lb.setText(str(algorithm_time))
            interval_time_lb.setText(str(interval_time))
            roi_state_lb.setText(str(processing_state))

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
        elif imageProcessing_way == 7: # elasticity_mesurement_roi_start
            ID_lb ,total_lb,miss_lb,roi_state_lb,bottom_lb = self.roi_display_lb[imageProcessing_way]
            ID = self.cell_ID
            self.totalNumb_elasticityMeasurement_end = self.totalNumb_elasticityMeasurement_end + total_add
            self.elasticityMeasurement_end_miss  = self.elasticityMeasurement_end_miss + miss_add
            ID_lb.setText(str(self.cell_ID))
            total_lb.setText(str(self.totalNumb_elasticityMeasurement_end))
            miss_lb.setText(str(self.elasticityMeasurement_end_miss))
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

    def ROI_live_view(self,display_frame,roi_x,roi_y,width,height,B,G,R):
        """显示框选ROI的区域"""
        x_scaled = int(roi_x * self.Ratio_x)
        y_scaled = int(roi_y * self.Ratio_y)
        width_scaled = int(width * self.Ratio_x)
        height_scaled = int(height * self.Ratio_y)
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
    def btn_triggerSort_function(self):
        """"Trigger信号相关函数"""
        # 禁用按钮，防止重复点击
        self.ui.btn_triggerSort.setEnabled(False)
        self.signal_btn_triggerSort.emit()
    @pyqtSlot()
    def slot_btn_triggerSort_finish(self):
        self.ui.btn_triggerSort.setEnabled(True)
    def btn_triggerRelease_function(self):# 默认值为0，表示仅仅进行release Trigger
        """"Release信号相关函数"""
        # 禁用按钮，防止重复点击
        self.ui.btn_triggerRelease.setEnabled(False)
        self.signal_btn_triggerRelease.emit()
    @pyqtSlot()
    def slot_btn_triggerRelease_finish(self):
        self.ui.btn_triggerRelease.setEnabled(True)
    @pyqtSlot()
    def slot_btn_triggerElasticityMeasurement_function(self):# 默认值为0，表示仅仅进行release Trigger
        """"Release信号相关函数"""
        # 禁用按钮，防止重复点击
        self.ui.btn_triggerElasticityMeasurement.setEnabled(False)
        self.signal_btn_triggerElasticityMeasurement.emit()
    #手动判断是否为目标细胞
    @pyqtSlot()
    def slot_btn_triggerElasticityMeasurement_finish(self):
        self.ui.btn_triggerElasticityMeasurement.setEnabled(True)
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
            self.ui.btn_rinseChannelElasticityMeasurement.setEnabled(True)
            self.ui.btn_enterRinseChannelModel.setEnabled(True)
            self.signal_btn_rinseChannel_OFF.emit()
        else:
            self.ui.btn_rinseChannelCapture.setStyleSheet("background-color: #4EEE94")
            self.btn_rinseChannelCapture_state = True
            self.ui.btn_rinseChannelSort.setEnabled(False)
            self.ui.btn_rinseChannelRelease.setEnabled(False)
            self.ui.btn_rinseChannelElasticityMeasurement.setEnabled(False)
            self.ui.btn_enterRinseChannelModel.setEnabled(False)
            self.signal_btn_rinseChannelCapture.emit()
    def btn_rinseChannelSort_function(self):
        """手动控制润洗筛选通道"""
        if self.btn_rinseChannelSort_state:
            self.ui.btn_rinseChannelSort.setStyleSheet("background-color: #E1E1E1")
            self.btn_rinseChannelSort_state = False
            self.ui.btn_rinseChannelCapture.setEnabled(True)
            self.ui.btn_rinseChannelRelease.setEnabled(True)
            self.ui.btn_rinseChannelElasticityMeasurement.setEnabled(True)
            self.ui.btn_rinseChannelElasticityMeasurement.setEnabled(False)
            self.ui.btn_enterRinseChannelModel.setEnabled(True)
            self.signal_btn_rinseChannel_OFF.emit()
        else:
            self.ui.btn_rinseChannelSort.setStyleSheet("background-color: #4EEE94")
            self.btn_rinseChannelSort_state = True
            self.ui.btn_rinseChannelCapture.setEnabled(False)
            self.ui.btn_rinseChannelRelease.setEnabled(False)
            self.ui.btn_rinseChannelElasticityMeasurement.setEnabled(False)
            self.ui.btn_enterRinseChannelModel.setEnabled(False)
            self.signal_btn_rinseChannelSort.emit()
    def btn_rinseChannelRelease_function(self):
        """手动控制润洗筛选通道"""
        if self.btn_rinseChannelRelease_state:
            self.ui.btn_rinseChannelRelease.setStyleSheet("background-color: #E1E1E1")
            self.btn_rinseChannelRelease_state = False
            self.ui.btn_rinseChannelSort.setEnabled(True)
            self.ui.btn_rinseChannelCapture.setEnabled(True)
            self.ui.btn_rinseChannelElasticityMeasurement.setEnabled(True)
            self.ui.btn_enterRinseChannelModel.setEnabled(True)
            self.signal_btn_rinseChannel_OFF.emit()
        else:
            self.ui.btn_rinseChannelRelease.setStyleSheet("background-color: #4EEE94")
            self.btn_rinseChannelRelease_state = True
            self.ui.btn_rinseChannelSort.setEnabled(False)
            self.ui.btn_rinseChannelCapture.setEnabled(False)
            self.ui.btn_rinseChannelElasticityMeasurement.setEnabled(False)
            self.ui.btn_enterRinseChannelModel.setEnabled(False)
            self.signal_btn_rinseChannelRelease.emit()
            #发送信号开启。。。
    def btn_rinseChannelElasticityMeasurement_function(self):
        """手动控制润洗筛选通道"""
        if self.btn_rinseChannelElasticityMeasurement_state:
            self.ui.btn_rinseChannelElasticityMeasurement.setStyleSheet("background-color: #E1E1E1")
            self.btn_rinseChannelElasticityMeasurement_state = False
            self.ui.btn_rinseChannelSort.setEnabled(True)
            self.ui.btn_rinseChannelCapture.setEnabled(True)
            self.ui.btn_rinseChannelRelease.setEnabled(True)
            self.ui.btn_enterRinseChannelModel.setEnabled(True)
            self.signal_btn_rinseChannel_OFF.emit()
        else:
            self.ui.btn_rinseChannelElasticityMeasurement.setStyleSheet("background-color: #4EEE94")
            self.btn_rinseChannelElasticityMeasurement_state = True
            self.ui.btn_rinseChannelSort.setEnabled(False)
            self.ui.btn_rinseChannelCapture.setEnabled(False)
            self.ui.btn_rinseChannelRelease.setEnabled(False)
            self.ui.btn_enterRinseChannelModel.setEnabled(False)
            self.signal_btn_rinseChannelElasticityMeasurement.emit()
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
            elif self.btn_rinseChannelElasticityMeasurement_state:
                self.btn_rinseChannelElasticityMeasurement_function()
            self.ui.btn_rinseChannelSort.setEnabled(False)
            self.ui.btn_rinseChannelCapture.setEnabled(False)
            self.ui.btn_rinseChannelRelease.setEnabled(False)
            self.ui.btn_rinseChannelElasticityMeasurement.setEnabled(False)
            #发送信号关闭。。。
        else:
            self.ui.btn_enterRinseChannelModel.setStyleSheet("background-color: #4EEE94")
            self.btn_enterRinseChannelModel_state = True
            self.ui.btn_rinseChannelSort.setEnabled(True)
            self.ui.btn_rinseChannelCapture.setEnabled(True)
            self.ui.btn_rinseChannelRelease.setEnabled(True)
            self.ui.btn_rinseChannelElasticityMeasurement.setEnabled(True)

    def _render_sim_preview_frame(self, frame, cache_frame=True):
        if frame is None:
            return
        if self.ui.chb_sCMOS_autoContrast.isChecked():
            frame_8bit = auto_uint16_to_uint8(frame, self.sim_auto_contrast_state)
        else:
            frame_8bit = manual_uint16_to_uint8(frame, self.ui.spb_sCMOS_displayGray_max.value())
        display_frame = frame_8bit
        original_height, original_width = display_frame.shape[:2]
        bounds = self.ui.lb_sCMOS_cameraView.contentsRect()
        display_width, display_height = fit_image_size_to_bounds(
            original_width,
            original_height,
            bounds.width(),
            bounds.height(),
        )
        interpolation = cv2.INTER_AREA if display_width < original_width or display_height < original_height else cv2.INTER_LINEAR
        if (display_width, display_height) != (original_width, original_height):
            display_frame = cv2.resize(display_frame, (display_width, display_height), interpolation=interpolation)
        display_frame = np.ascontiguousarray(display_frame)
        h, w = display_frame.shape[:2]
        bytes_per_line = int(display_frame.strides[0])
        q_img = QImage(display_frame.data, w, h, bytes_per_line, QImage.Format_Grayscale8)
        self.ui.lb_sCMOS_cameraView.setPixmap(QPixmap.fromImage(q_img.copy()))
        self.ui.lb_sCMOS_cameraView.setAlignment(Qt.AlignCenter)
        if cache_frame:
            self.sim_last_preview_frame = frame.copy()

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

    def poll_latest_sim_preview_frame(self):
        if self.sim_preview_controller is None:
            return
        snapshot = self.sim_preview_controller.take_latest_frame()
        if snapshot is None:
            return
        if int(snapshot.sequence) <= int(getattr(self, "sim_last_preview_sequence", -1)):
            return
        try:
            self._render_sim_preview_frame(snapshot.frame)
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

    @pyqtSlot()
    def slot_sCMOS_Updata_elasticityTrigger(self):
        try:
            self.trigger_sim_formal_acquisition("mcu_elasticity_trigger")
        except Exception as e:
            print(f"SIM acquisition trigger failed: {str(e)}")

    @pyqtSlot()
    def slot_sCMOS_BgUpdata_captureTrigger(self):
        try:
            print("SIM background update trigger received; no separate SIM background frame path is configured.")
        except Exception as e:
            print(f"SIM background trigger handling failed: {str(e)}")
 
if __name__ == "__main__":
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)  # 启用高 DPI 缩放
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)  # 启用高 DPI 图标
    app = qw.QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec_())  

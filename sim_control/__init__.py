"""SIM 控制包的公共导出面。

外部代码通过这个模块获取配置读写函数、核心数据类、控制器、主窗口和协议类型。它不执行采集流程，只把 sim_control 内部稳定入口集中暴露出来，减少调用方直接依赖深层模块路径。
"""

from .config_store import DEFAULT_CONFIG_PATH, load_app_config, save_app_config
from .controller import SimAcquisitionController
from .gui import SimControlWindow
from .models import (
    AcquisitionBatch,
    AppConfig,
    CameraConfig,
    DaqLineConfig,
    DecisionResult,
    FeatureResult,
    ReconstructionResult,
    SimTaskConfig,
    TimingConfig,
)
from .protocols import CameraAdapter, DaqAdapter, SlmAdapter

__all__ = [
    "AcquisitionBatch",
    "AppConfig",
    "CameraAdapter",
    "CameraConfig",
    "DaqAdapter",
    "DaqLineConfig",
    "DEFAULT_CONFIG_PATH",
    "DecisionResult",
    "FeatureResult",
    "ReconstructionResult",
    "SimAcquisitionController",
    "SimControlWindow",
    "SimTaskConfig",
    "SlmAdapter",
    "TimingConfig",
    "load_app_config",
    "save_app_config",
]

"""SIM 控制包的公共导出面。

作用：
    本文件是 ``sim_control`` 包的「门面」（facade）。外部代码（集成主界面、
    独立 SIM 入口、测试套件、对接 SIM9 重建算法的同学）只需要从 ``sim_control``
    顶层 import 即可拿到稳定接口，避免把调用方写死在内部子模块路径上。
    它本身不执行采集流程，仅做名字重导出。

协作关系：
    上游：``app.py``、``sim_control/sim_acquisition_app.py``、
          ``control_wangbo/main.py``、``tests/`` 等所有需要 SIM 接口的入口。
    下游：``config_store``、``controller``、``gui``、``models``、``protocols``
          这些 sim_control 内部模块。
    相关：``sim_control/adapters.py``、``sim_control/sim_adapters.py``
          这些硬件适配器并不在此处重导出，因为外部不应该直接构造它们。

关键概念：
    - ``__all__`` 列表是 ``from sim_control import *`` 唯一可见名字集合，
      它把"对外稳定接口"和"内部实现"区分开来。
    - 重导出的命名固定来自 ``models``/``config_store``/``controller``/``gui``/
      ``protocols`` 五个模块；其它（adapters、waveform、acquisition_core、
      pipeline 等）属于实现细节，外部不应直接 import。

维护要点：
    - 新增对外接口时，在对应内部模块定义后，**同时**更新这里的 import 和
      ``__all__``；删除接口时反向操作。
    - 不要让任何 import 触发硬件 SDK 加载或 GUI 弹窗，本文件只能做名字搬运。
"""

# 配置读写函数：外部加载/保存 ``AppConfig`` JSON 时直接调用这两个函数。
from .config_store import DEFAULT_CONFIG_PATH, load_app_config, save_app_config
# SIM 采集控制器：集成主界面和独立 GUI 共享同一个控制器实例。
from .controller import SimAcquisitionController
# SIM 顶层窗口：独立 SIM 采集 GUI 与集成主界面的弹窗都基于它。
from .gui import SimControlWindow
# 全套配置/结果数据类：覆盖配置（AppConfig）、子配置（Camera/DaqLine/Timing）
# 与采集后产物（Acquisition/Reconstruction/Feature/Decision Result）。
from .models import (
    AcquisitionBatch,
    AppConfig,
    CameraConfig,
    DaqLineConfig,
    DecisionResult,
    FeatureResult,
    ReconstructionConfig,
    ReconstructionResult,
    SimTaskConfig,
    TimingConfig,
)
# 硬件 adapter 协议：测试与外部实现可以基于这三个 Protocol 自定义 mock/真实绑定。
from .protocols import CameraAdapter, DaqAdapter, SlmAdapter

# ``__all__`` 决定 ``from sim_control import *`` 与外部静态分析工具看到的导出面。
# 排序保持字母序，便于 review 时一眼比对是否漏导出。
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
    "ReconstructionConfig",
    "ReconstructionResult",
    "SimAcquisitionController",
    "SimControlWindow",
    "SimTaskConfig",
    "SlmAdapter",
    "TimingConfig",
    "load_app_config",
    "save_app_config",
]

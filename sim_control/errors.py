"""SIM 适配层共享的领域异常类型。

抽到独立模块的原因：
    真实适配器（``adapters.py``）与仿真适配器（``sim_adapters.py``）都需要抛出
    同一种硬件错误类型，GUI / preview / controller 据此与取消、参数错误区分。
    若让仿真层直接 ``from .adapters import HardwareError``，会让仿真路径反向依赖
    1700+ 行的真实适配器模块（连带其 ctypes / SDK import 副作用）。把类型抽到这里，
    两侧共同引用，仿真层无需触碰真实适配器模块。

兼容性：
    ``adapters.py`` 仍 re-export ``HardwareError``，因此既有
    ``from sim_control.adapters import HardwareError`` 调用点不受影响。
"""

from __future__ import annotations


class HardwareError(RuntimeError):
    """SIM 硬件 / SDK / 设备状态相关错误的统一类型。

    继承自 ``RuntimeError``，保证既有 ``except RuntimeError`` /
    ``assertRaises(RuntimeError)`` 仍然命中；真实与仿真适配器共用此类型，使
    "未连接被拒"等回归在仿真模式下也能被测出，并让 preview/controller 的
    ``except HardwareError`` 分支在两种模式下走同一路径。
    """
    pass


__all__ = ["HardwareError"]

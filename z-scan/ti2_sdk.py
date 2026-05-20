"""Nikon Ti2 SDK 的 ctypes 薄包装（``z-scan/`` 独立工具用）。

作用：
    本文件手动实现 Nikon Ti2 SDK 中与 Z stage 控制相关的 C 结构体（``MIC_Data``
    及其内嵌联合体 ``MIC_DataTail``）的 ctypes 映射，并提供高层包装
    ``Ti2Stage``：``open`` / ``close`` / ``get_device_list`` / ``get_z_ranges_um``
    / ``move_z_um``。``z_scan_timing.py`` 通过它发起每一次 Z 步进。

协作关系：
    上游：``z-scan/z_scan_timing.py``、``z-scan/test_z_scan_timing.py``。
    下游：``Ti2_Mic_Driver.dll``（默认在 ``C:\\Program Files\\Nikon\\Ti2-SDK\\bin``）。

关键概念：
    - ``MIC_Data``：一个超大 ctypes 结构体，描述显微镜各零部件状态；本工具只关心
      其中 ``iZPOSITION`` / ``iZPOSITIONSpeed`` / ``iZPOSITIONTolerance`` 三个字段。
    - ``MIC_DATA_MASK_ZPOSITION``：位掩码，告诉 SDK"本次只关心 Z 位置"。
    - ``LX_OK = 0``：SDK 成功返回码；非 0 都被包装成 ``Ti2SdkError``。
    - ``ZRange``：从 SDK 读取的物理 / 逻辑 Z 范围（μm）。

维护要点：
    - 结构体字段顺序与字节宽度必须与 SDK 头文件**严格一致**；改动需对照官方文档。
    - 与 SIM 主流程完全解耦；不要让本文件 import ``sim_control``。
"""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from pathlib import Path


# Nikon Ti2 SDK 常用常量：返回码、位掩码、SDK 默认安装路径。
LX_OK = 0
MIC_DATA_MASK_ZPOSITION = 0x0000000000000001
MIC_ACCESSORY_MASK_ZSTAGE = 0x0000000000000001
MIC_METADATA_MASK_FULL = 0xFFFFFFFFFFFFFFFF
MIC_EQUIPMENT_MASK_FULL = 0xFFFFFFFFFFFFFFFF

# 默认 SDK 安装目录与主 DLL 路径；用户可通过命令行 ``--dll`` 覆盖。
DEFAULT_SDK_DIR = Path(r"C:\Program Files\Nikon\Ti2-SDK\bin")
DEFAULT_DLL_PATH = DEFAULT_SDK_DIR / "Ti2_Mic_Driver.dll"


class Ti2SdkError(RuntimeError):
    """Nikon Ti2 SDK 调用失败的领域异常；附带原始 retcode。"""
    def __init__(self, message: str, retcode: int | None = None) -> None:
        super().__init__(message)
        self.retcode = retcode


class MIC_TirfData(ctypes.Structure):
    _fields_ = [
        ("uiDataUsageSubMask", ctypes.c_uint64),
        ("iTirf1XSpeed", ctypes.c_int32),
        ("iTirf1XPOSITION", ctypes.c_int32),
        ("iTirf1YSpeed", ctypes.c_int32),
        ("iTirf1YPOSITION", ctypes.c_int32),
        ("iTirf2XSpeed", ctypes.c_int32),
        ("iTirf2XPOSITION", ctypes.c_int32),
        ("iTirf2YSpeed", ctypes.c_int32),
        ("iTirf2YPOSITION", ctypes.c_int32),
        ("iTirf3XSpeed", ctypes.c_int32),
        ("iTirf3XPOSITION", ctypes.c_int32),
        ("iTirf3YSpeed", ctypes.c_int32),
        ("iTirf3YPOSITION", ctypes.c_int32),
        ("Reserve", ctypes.c_char * 128),
    ]


class MIC_DLedData(ctypes.Structure):
    _fields_ = [("Reserve", ctypes.c_char * ctypes.sizeof(MIC_TirfData))]


class MIC_DataTail(ctypes.Union):
    _fields_ = [
        ("sTIRF", MIC_TirfData),
        ("sDLED", MIC_DLedData),
        ("Reserve", ctypes.c_char * 1024),
    ]


class MIC_Data(ctypes.Structure):
    _fields_ = [
        ("uiDataUsageMask", ctypes.c_uint64),
        ("uiMicOperationCounter", ctypes.c_uint64),
        ("uiIOforTrigger", ctypes.c_uint64),
        ("iZPOSITION", ctypes.c_int32),
        ("iZPOSITIONSpeed", ctypes.c_int32),
        ("iZPOSITIONTolerance", ctypes.c_int32),
        ("iXPOSITION", ctypes.c_int32),
        ("iXPOSITIONSpeed", ctypes.c_int32),
        ("iXPOSITIONTolerance", ctypes.c_int32),
        ("iYPOSITION", ctypes.c_int32),
        ("iYPOSITIONSpeed", ctypes.c_int32),
        ("iYPOSITIONTolerance", ctypes.c_int32),
        ("iNOSEPIECE", ctypes.c_int32),
        ("iTURRET1POS", ctypes.c_int32),
        ("iTURRET1SHUTTER", ctypes.c_int32),
        ("iTURRET2POS", ctypes.c_int32),
        ("iTURRET2SHUTTER", ctypes.c_int32),
        ("iCONDENSER", ctypes.c_int32),
        ("iFILTERWHEEL_BARRIER1", ctypes.c_int32),
        ("iFILTERWHEEL_BARRIER2", ctypes.c_int32),
        ("iLIGHTPATH", ctypes.c_int32),
        ("iSHUTTER_EPI", ctypes.c_int32),
        ("iSHUTTER_DIA", ctypes.c_int32),
        ("iSHUTTER_AUX", ctypes.c_int32),
        ("iDIA_LAMP_Switch", ctypes.c_int32),
        ("iDIA_LAMP_Pos", ctypes.c_int32),
        ("iINTENSILIGHT_POS", ctypes.c_int32),
        ("iINTENSILIGHT_SHUTTER", ctypes.c_int32),
        ("iINTENSILIGHT_SWITCH", ctypes.c_int32),
        ("iEPI_LEDPos", ctypes.c_int32 * 4),
        ("iEPI_LEDUnit", ctypes.c_int32),
        ("iEPI_LEDSwitch", ctypes.c_int32 * 4),
        ("iPFS_SWITCH", ctypes.c_int32),
        ("iPFS_OFFSET", ctypes.c_int32),
        ("iPFS_DM", ctypes.c_int32),
        ("iPFS_STATUS", ctypes.c_int32),
        ("iEYEPIECE_TUBEBASECamPort", ctypes.c_int32),
        ("iEYEPIECE_TUBEBASETurret", ctypes.c_int32),
        ("iLAPP_MAINBranch1", ctypes.c_int32),
        ("iLAPP_MAINBranch2", ctypes.c_int32),
        ("iLAPP_SUBBranch", ctypes.c_int32),
        ("iCORRECTION_COLLAR", ctypes.c_int32),
        ("iDIC_PRISM", ctypes.c_int32),
        ("iDIC_POLARIZER", ctypes.c_int32),
        ("iOPTZOOM", ctypes.c_int32),
        ("iZEscape", ctypes.c_int32),
        ("iANALYZER_SLOT", ctypes.c_int32),
        ("iANALYZER_POS", ctypes.c_int32),
        ("iBertrandLens", ctypes.c_int32),
        ("iCamera", ctypes.c_int32),
        ("iCORRECTION_COLLAR_Limit", ctypes.c_int32),
        ("iZLimit", ctypes.c_int32),
        ("iXLimit", ctypes.c_int32),
        ("iYLimit", ctypes.c_int32),
        ("iMirrorSwitch", ctypes.c_int32),
        ("iZReset", ctypes.c_int32),
        ("iXReset", ctypes.c_int32),
        ("iYReset", ctypes.c_int32),
        ("iPWSS_Switch", ctypes.c_int32),
        ("iPWSS_Speed", ctypes.c_int32),
        ("iPWSA_Switch", ctypes.c_int32),
        ("iPWSA_Speed", ctypes.c_int32),
        ("tail", MIC_DataTail),
    ]


class MIC_Command(ctypes.Structure):
    _fields_ = [
        ("wszCommandString", ctypes.c_wchar * 256),
        ("pCommandData", ctypes.c_void_p),
    ]


class MIC_Objective(ctypes.Structure):
    _fields_ = [
        ("wszOrderNumber", ctypes.c_wchar * 8),
        ("dMagnification", ctypes.c_double),
        ("dNumericalAperture", ctypes.c_double),
        ("iIsPFSEnabled", ctypes.c_int32),
        ("wszObjectiveModel", ctypes.c_wchar * 14),
        ("wszWorkingDistance", ctypes.c_wchar * 4),
        ("wszUsage", ctypes.c_wchar * 4),
        ("wszObjectiveType", ctypes.c_wchar * 4),
        ("wszWDType", ctypes.c_wchar * 5),
        ("iPWS", ctypes.c_int32),
        ("iCorrectionCollar", ctypes.c_int32),
        ("iObservationMask", ctypes.c_int32),
        ("wszPH_Module", ctypes.c_wchar * 7),
        ("wszExPH_Module", ctypes.c_wchar * 4),
        ("wszDIC_Module", ctypes.c_wchar * 2),
        ("wszDIC_Slider", ctypes.c_wchar * 8),
        ("wszDIC_ModuleHR", ctypes.c_wchar * 2),
        ("wszDIC_SliderHR", ctypes.c_wchar * 10),
        ("iDF_Module", ctypes.c_int32),
        ("wszNAMC_Module", ctypes.c_wchar * 3),
    ]


class MIC_OpticsParts(ctypes.Structure):
    _fields_ = [
        ("wsLongName", ctypes.c_wchar * 30),
        ("wsShortName", ctypes.c_wchar * 10),
    ]


class MIC_Equipment(ctypes.Structure):
    _fields_ = [
        ("wszOrderNumber", ctypes.c_wchar * 8),
        ("wszReserve", ctypes.c_wchar * 1),
    ]


class MIC_EquipmentControl(ctypes.Structure):
    _fields_ = [
        ("uiEquipmentUsageMask", ctypes.c_uint64),
        ("iNosepieceControl", ctypes.c_int32),
        ("iCondenserControl", ctypes.c_int32),
        ("iTurret1Control", ctypes.c_int32),
        ("iTurret1ShutterControl", ctypes.c_int32),
        ("iTurret2Control", ctypes.c_int32),
        ("iTurret2ShutterControl", ctypes.c_int32),
        ("iAnalyzerSlotControl", ctypes.c_int32),
        ("iLappMainBranch1Control", ctypes.c_int32),
        ("iLappMainBranch2Control", ctypes.c_int32),
        ("iLappSubBranchControl", ctypes.c_int32),
        ("iPolarizerControl", ctypes.c_int32),
        ("iStageControl", ctypes.c_int32),
        ("iEyepieceCameraPortControl", ctypes.c_int32),
        ("iUnit1Control", ctypes.c_int32),
        ("iUnit2Control", ctypes.c_int32),
        ("iUnit3Control", ctypes.c_int32),
    ]


class MIC_Equipments(ctypes.Structure):
    _fields_ = [
        ("uiEquipmentUsageMask", ctypes.c_uint64),
        ("Reserve", ctypes.c_char * 1024),
    ]


class MIC_MetaDataPrefix(ctypes.Structure):
    _fields_ = [
        ("uiMetaDataUsageMask", ctypes.c_uint64),
        ("sNosepieceObjective", MIC_Objective * 8),
        ("sCondenser", MIC_OpticsParts * 8),
        ("sTurret1Filter", MIC_OpticsParts * 8),
        ("sTurret2Filter", MIC_OpticsParts * 8),
        ("sBar1FilterWheel", MIC_OpticsParts * 8),
        ("sBar2FilterWheel", MIC_OpticsParts * 8),
        ("sLightPath_Prism", MIC_OpticsParts * 8),
        ("sExternalPH_Ring", MIC_OpticsParts * 8),
        ("sLapp_MainBranch1", MIC_OpticsParts * 8),
        ("sLapp_MainBranch2", MIC_OpticsParts * 8),
        ("sDIC_Prism", MIC_OpticsParts * 8),
        ("sOPT_Zoom", MIC_OpticsParts * 8),
        ("sDSC", MIC_OpticsParts * 8),
        ("nEPI_LEDWavelength", ctypes.c_int32 * 4),
        ("iXYStage_XRangePhys", ctypes.c_int32 * 2),
        ("iXYStage_YRangePhys", ctypes.c_int32 * 2),
        ("iZDrive_RangePhys", ctypes.c_int32 * 2),
        ("iNosepiece_RangePhys", ctypes.c_int32 * 2),
        ("iCondenser_RangePhys", ctypes.c_int32 * 2),
        ("iDICPrism_RangePhys", ctypes.c_int32 * 2),
        ("iDICPolarizer_RangePhys", ctypes.c_int32 * 2),
        ("iTurret1_RangePhys", ctypes.c_int32 * 2),
        ("iTurret1Shutter_RangePhys", ctypes.c_int32 * 2),
        ("iTurret2_RangePhys", ctypes.c_int32 * 2),
        ("iTurret2Shutter_RangePhys", ctypes.c_int32 * 2),
        ("iBar1_RangePhys", ctypes.c_int32 * 2),
        ("iBar2_RangePhys", ctypes.c_int32 * 2),
        ("iLightpath_RangePhys", ctypes.c_int32 * 2),
        ("iPfsSwitch_RangePhys", ctypes.c_int32 * 2),
        ("iPfsOffset_RangePhys", ctypes.c_int32 * 2),
        ("iPfsDM_RangePhys", ctypes.c_int32 * 2),
        ("iShutterEpi_RangePhys", ctypes.c_int32 * 2),
        ("iShutterDia_RangePhys", ctypes.c_int32 * 2),
        ("iShutterAux_RangePhys", ctypes.c_int32 * 2),
        ("iDiaLampSwitch_RangePhys", ctypes.c_int32 * 2),
        ("iDiaLampPos_RangePhys", ctypes.c_int32 * 2),
        ("iIntensilightShutter_RangePhys", ctypes.c_int32 * 2),
        ("iIntensilightPosition_RangePhys", ctypes.c_int32 * 2),
        ("iIntensilightSwitch_RangePhys", ctypes.c_int32 * 2),
        ("iEpiLEDUnit_RangePhys", ctypes.c_int32 * 2),
        ("iEpiLEDSwitch_RangePhys", ctypes.c_int32 * 2),
        ("iEpiLEDPos_RangePhys", ctypes.c_int32 * 2),
        ("iEyepiece_TubeBasePort_RangePhys", ctypes.c_int32 * 2),
        ("iEyepiece_TubeBaseTurret_RangePhys", ctypes.c_int32 * 2),
        ("iLappMainBranch1_RangePhys", ctypes.c_int32 * 2),
        ("iLappMainBranch2_RangePhys", ctypes.c_int32 * 2),
        ("iLappSubBranch_RangePhys", ctypes.c_int32 * 2),
        ("iCorrectionCollar_RangePhys", ctypes.c_int32 * 2),
        ("iOptZoom_RangePhys", ctypes.c_int32 * 2),
        ("iAnalyzerSlot_RangePhys", ctypes.c_int32 * 2),
        ("iAnalyzerPos_RangePhys", ctypes.c_int32 * 2),
        ("iPWSS_Switch_RangePhys", ctypes.c_int32 * 2),
        ("iPWSA_Switch_RangePhys", ctypes.c_int32 * 2),
        ("iXYStage_XRangeLog", ctypes.c_int32 * 2),
        ("iXYStage_YRangeLog", ctypes.c_int32 * 2),
        ("iZDrive_RangeLog", ctypes.c_int32 * 2),
        ("sEquipmentControl", MIC_EquipmentControl),
        ("sEquipmentOrder", MIC_Equipments),
    ]


@dataclass(frozen=True)
class ZRange:
    """Z 范围记录：物理 / 逻辑两套同时给出，便于不同坐标系下校验。"""
    lower_um: float
    upper_um: float
    lower_dev: int
    upper_dev: int
    source: str


@dataclass(frozen=True)
class MoveResult:
    """单次 ``MIC_DataSet`` 结果。``final_um`` 在 SDK 失败时为 NaN。"""
    retcode: int
    final_dev: int
    final_um: float


def _initialized_data() -> MIC_Data:
    """构造一个"所有 tolerance/speed 置 -1"的 ``MIC_Data`` 结构体。

    用途：
        Nikon SDK 约定 -1 = "不修改该字段"。本工具只关心 Z 字段，所以其它字段都用
        -1 让 SDK 忽略，避免误改 X/Y 速度或 tolerance。
    """
    data = MIC_Data()
    data.iXPOSITIONTolerance = -1
    data.iYPOSITIONTolerance = -1
    data.iZPOSITIONTolerance = -1
    data.iZPOSITIONSpeed = -1
    data.iXPOSITIONSpeed = -1
    data.iYPOSITIONSpeed = -1
    return data


class Ti2Stage:
    """Nikon Ti2 SDK 的高层包装，提供 open/close/move/range 等便捷接口。

    职责：
        - 加载 ``Ti2_Mic_Driver.dll`` 并把所有用到的 C 函数 ``argtypes/restype`` 绑定。
        - ``open`` / ``close`` 设备；支持 ``simulator=True`` 走 ``MIC_SimulatorOpen``。
        - ``move_z_um`` / ``read_z_position_um`` / ``get_z_ranges_um`` 操控 Z stage。
        - dev↔phys 单位转换（``MIC_Convert_Dev2Phys`` / ``MIC_Convert_Phys2Dev``）。

    上下文管理：
        支持 ``with Ti2Stage(...) as stage:`` 自动 open/close。

    抛出：
        ``Ti2SdkError``：DLL 缺失、accessory 不含 Z stage、SDK 调用返回非 ``LX_OK``。
    """
    def __init__(self, dll_path: str | Path | None = None) -> None:
        self.dll_path = Path(dll_path) if dll_path else DEFAULT_DLL_PATH
        if not self.dll_path.exists():
            raise Ti2SdkError(f"Nikon Ti2 SDK DLL not found: {self.dll_path}")
        os.add_dll_directory(str(self.dll_path.parent))
        self._dll = ctypes.WinDLL(str(self.dll_path))
        self._is_open = False
        self.connected_accessory_mask = 0
        self._bind_functions()

    def __enter__(self) -> Ti2Stage:
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _bind_functions(self) -> None:
        self._dll.MIC_GetDeviceList.argtypes = [
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_int32)),
        ]
        self._dll.MIC_GetDeviceList.restype = ctypes.c_int32
        self._dll.MIC_Open.argtypes = [
            ctypes.c_int32,
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_wchar),
        ]
        self._dll.MIC_Open.restype = ctypes.c_int32
        self._dll.MIC_SimulatorOpen.argtypes = [
            ctypes.c_int32,
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_wchar),
        ]
        self._dll.MIC_SimulatorOpen.restype = ctypes.c_int32
        self._dll.MIC_Close.argtypes = []
        self._dll.MIC_Close.restype = ctypes.c_int32
        self._dll.MIC_DataSet.argtypes = [
            ctypes.POINTER(MIC_Data),
            ctypes.POINTER(MIC_Data),
            ctypes.c_bool,
        ]
        self._dll.MIC_DataSet.restype = ctypes.c_int32
        self._dll.MIC_DataGet.argtypes = [ctypes.POINTER(MIC_Data)]
        self._dll.MIC_DataGet.restype = ctypes.c_int32
        self._dll.MIC_MetadataGet.argtypes = [ctypes.c_void_p]
        self._dll.MIC_MetadataGet.restype = ctypes.c_int32
        self._dll.MIC_Convert_Dev2Phys.argtypes = [
            ctypes.c_uint64,
            ctypes.c_int32,
            ctypes.POINTER(ctypes.c_double),
        ]
        self._dll.MIC_Convert_Dev2Phys.restype = ctypes.c_int32
        self._dll.MIC_Convert_Phys2Dev.argtypes = [
            ctypes.c_uint64,
            ctypes.c_double,
            ctypes.POINTER(ctypes.c_int32),
        ]
        self._dll.MIC_Convert_Phys2Dev.restype = ctypes.c_int32

    def get_device_list(self) -> list[int]:
        count = ctypes.c_uint32(0)
        devices = ctypes.POINTER(ctypes.c_int32)()
        ret = self._dll.MIC_GetDeviceList(ctypes.byref(count), ctypes.byref(devices))
        self._raise_if_error(ret, "MIC_GetDeviceList failed")
        return [devices[index] for index in range(count.value)] if devices else []

    def open(self, device_index: int = 0, simulator: bool = False) -> None:
        accessories = ctypes.c_uint64(0)
        err_msg = ctypes.create_unicode_buffer(1024)
        if simulator:
            ret = self._dll.MIC_SimulatorOpen(
                device_index,
                ctypes.byref(accessories),
                len(err_msg),
                err_msg,
            )
        else:
            ret = self._dll.MIC_Open(
                device_index,
                ctypes.byref(accessories),
                len(err_msg),
                err_msg,
            )
        self._raise_if_error(ret, f"MIC_Open failed: {err_msg.value}")
        self._is_open = True
        self.connected_accessory_mask = accessories.value
        if not self.has_zstage:
            self.close()
            raise Ti2SdkError("Connected Ti2 does not report MIC_ACCESSORY_MASK_ZSTAGE")

    @property
    def has_zstage(self) -> bool:
        return bool(self.connected_accessory_mask & MIC_ACCESSORY_MASK_ZSTAGE)

    def close(self) -> None:
        if self._is_open:
            ret = self._dll.MIC_Close()
            self._is_open = False
            self._raise_if_error(ret, "MIC_Close failed")

    def get_z_ranges_um(self) -> dict[str, ZRange]:
        raw = ctypes.create_string_buffer(65536)
        request = MIC_MetaDataPrefix.from_buffer(raw)
        request.uiMetaDataUsageMask = MIC_METADATA_MASK_FULL
        request.sEquipmentControl.uiEquipmentUsageMask = MIC_EQUIPMENT_MASK_FULL
        request.sEquipmentOrder.uiEquipmentUsageMask = MIC_EQUIPMENT_MASK_FULL
        ret = self._dll.MIC_MetadataGet(ctypes.cast(raw, ctypes.c_void_p))
        self._raise_if_error(ret, "MIC_MetadataGet failed")
        prefix = MIC_MetaDataPrefix.from_buffer_copy(raw.raw[: ctypes.sizeof(MIC_MetaDataPrefix)])
        return {
            "physical": self._make_range(prefix.iZDrive_RangePhys, "physical"),
            "logical": self._make_range(prefix.iZDrive_RangeLog, "logical"),
        }

    def read_z_position_um(self) -> float:
        data = _initialized_data()
        data.uiDataUsageMask = MIC_DATA_MASK_ZPOSITION
        ret = self._dll.MIC_DataGet(ctypes.byref(data))
        self._raise_if_error(ret, "MIC_DataGet failed")
        return self.dev_to_um(data.iZPOSITION)

    def move_z_um(self, target_um: float, speed: int = 1, tolerance: int = 0) -> MoveResult:
        target_dev = self.um_to_dev(target_um)
        data_in = _initialized_data()
        data_out = _initialized_data()
        data_in.uiDataUsageMask = MIC_DATA_MASK_ZPOSITION
        data_in.iZPOSITION = target_dev
        data_in.iZPOSITIONSpeed = speed
        data_in.iZPOSITIONTolerance = tolerance
        ret = self._dll.MIC_DataSet(ctypes.byref(data_in), ctypes.byref(data_out), True)
        if ret != LX_OK:
            return MoveResult(retcode=ret, final_dev=data_out.iZPOSITION, final_um=float("nan"))
        return MoveResult(retcode=ret, final_dev=data_out.iZPOSITION, final_um=self.dev_to_um(data_out.iZPOSITION))

    def dev_to_um(self, value: int) -> float:
        out = ctypes.c_double(0.0)
        ret = self._dll.MIC_Convert_Dev2Phys(MIC_DATA_MASK_ZPOSITION, int(value), ctypes.byref(out))
        self._raise_if_error(ret, "MIC_Convert_Dev2Phys failed")
        return float(out.value)

    def um_to_dev(self, value: float) -> int:
        out = ctypes.c_int32(0)
        ret = self._dll.MIC_Convert_Phys2Dev(MIC_DATA_MASK_ZPOSITION, float(value), ctypes.byref(out))
        self._raise_if_error(ret, "MIC_Convert_Phys2Dev failed")
        return int(out.value)

    def _make_range(self, values: ctypes.Array, source: str) -> ZRange:
        lower_dev = int(values[0])
        upper_dev = int(values[1])
        lower_um = self.dev_to_um(lower_dev)
        upper_um = self.dev_to_um(upper_dev)
        if lower_um <= upper_um:
            return ZRange(lower_um=lower_um, upper_um=upper_um, lower_dev=lower_dev, upper_dev=upper_dev, source=source)
        return ZRange(lower_um=upper_um, upper_um=lower_um, lower_dev=upper_dev, upper_dev=lower_dev, source=source)

    def _raise_if_error(self, retcode: int, message: str) -> None:
        if retcode != LX_OK:
            raise Ti2SdkError(f"{message} (retcode={retcode})", retcode=retcode)

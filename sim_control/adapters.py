from __future__ import annotations

import ctypes
import importlib
import math
import os
import re
import struct
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np

from .models import CameraConfig, PatternPreparationResult
from .sim_camera_presets import DEFAULT_SIM_CAMERA_SIZE, SIM_CAMERA_ROI_STEP_PX, build_sim_camera_size_presets
from .waveform import WaveformPlan

try:
    import nidaqmx
    from nidaqmx.constants import AcquisitionType, LineGrouping
    from nidaqmx.stream_writers import DigitalSingleChannelWriter
except Exception:  # pragma: no cover
    nidaqmx = None
    AcquisitionType = None
    LineGrouping = None
    DigitalSingleChannelWriter = None


class HardwareError(RuntimeError):
    pass


APP_ROOT = Path(__file__).resolve().parent.parent
R11_WINUSB_GUID = b"54ED7AC9-CC23-4165-BE32-79016BAFB950"
R11_QXGA_WIDTH = 2048
R11_QXGA_HEIGHT = 1536
R11_PAGE_SIZE = 2048
R11_PAGES_PER_BLOCK = 64
R11_IMAGE_BASE = 0x01000000
R11_BITPLANE_BYTES = (R11_QXGA_WIDTH // 8) * R11_QXGA_HEIGHT
R11_BITPLANE_PAGES = R11_BITPLANE_BYTES // R11_PAGE_SIZE
_REVERSE_BITS_LUT = bytes(int(f"{index:08b}"[::-1], 2) for index in range(256))
_SUPPORTED_CAMERA_BIT_DEPTHS = (8, 10, 12, 14, 16)
_RUNNING_ORDER_NAME_RE = re.compile(
    r"^(?P<wavelength>\d+)_(?P<pitch>\d+(?:\.\d+)?)_(?P<mode>[A-Za-z0-9]+)_"
    r"(?P<exposure>\d+)ms(?P<single_angle>_ang0)?$"
)


def parse_running_order_name(name: str) -> dict[str, Any] | None:
    match = _RUNNING_ORDER_NAME_RE.match(str(name).strip())
    if match is None:
        return None
    try:
        return {
            "wavelength_nm": int(match.group("wavelength")),
            "pitch": match.group("pitch"),
            "mode": match.group("mode").lower(),
            "exposure_ms": int(match.group("exposure")),
            "single_angle": bool(match.group("single_angle")),
        }
    except (TypeError, ValueError):
        return None


def _target_running_order_exposure_ms(exposure_us: int) -> int:
    exposure_us = int(exposure_us)
    if exposure_us < 10_000:
        return 1
    if exposure_us < 50_000:
        return 10
    return 50


def find_best_running_order(
    running_orders: list[tuple[int, str]],
    wavelength_nm: int,
    exposure_us: int,
) -> tuple[int | None, str, list[str]]:
    target_exposure_ms = _target_running_order_exposure_ms(exposure_us)
    warnings: list[str] = []
    candidates: list[tuple[int, str]] = []
    for index, name in running_orders:
        parsed = parse_running_order_name(name)
        if parsed is None:
            continue
        if parsed["single_angle"]:
            continue
        if parsed["wavelength_nm"] != int(wavelength_nm):
            continue
        if parsed["pitch"] != "3.5":
            continue
        if parsed["mode"] != "2d":
            continue
        if parsed["exposure_ms"] != target_exposure_ms:
            continue
        candidates.append((int(index), str(name)))

    if candidates:
        return candidates[0][0], candidates[0][1], warnings

    warnings.append(
        "No matching SLM running order found for "
        f"{int(wavelength_nm)} nm, {target_exposure_ms} ms bucket, pitch 3.5, mode 2d."
    )
    return None, "", warnings


def _resolve_r11_dll_path(user_path: str = "") -> Path:
    if user_path:
        candidate = Path(user_path)
        if candidate.is_dir():
            dll_name = "R11CommLib-1.8-x64.dll" if sys.maxsize > 2**32 else "R11CommLib-1.8-x86.dll"
            dll_path = candidate / dll_name
            if dll_path.exists():
                return dll_path
        if candidate.exists():
            return candidate

    default_dir = (
        APP_ROOT
        / "SDK"
        / "R11 CD Bundle Mar 2020"
        / "2020-03"
        / "Software"
        / "R11CommLib"
        / "R11CommLib-1.8.189.118"
        / "examples"
        / "msvc"
        / "lib"
    )
    dll_name = "R11CommLib-1.8-x64.dll" if sys.maxsize > 2**32 else "R11CommLib-1.8-x86.dll"
    dll_path = default_dir / dll_name
    if dll_path.exists():
        return dll_path
    raise HardwareError(f"R11CommLib DLL not found. Checked: {dll_path}")


def _resolve_dcam_python_dir(user_path: str = "") -> Path:
    if user_path:
        candidate = Path(user_path)
        if candidate.is_file() and candidate.name.lower() == "dcam.py":
            return candidate.parent
        if candidate.is_dir():
            if (candidate / "dcam.py").exists():
                return candidate
            nested = candidate / "samples" / "python"
            if (nested / "dcam.py").exists():
                return nested

    default_dir = APP_ROOT / "SDK" / "Hamamatsu_DCAMSDK4_v25056964" / "dcamsdk4" / "samples" / "python"
    if (default_dir / "dcam.py").exists():
        return default_dir
    raise HardwareError(f"DCAM Python sample directory not found. Checked: {default_dir}")


def _load_r11_bitplane_file(path: Path) -> bytes:
    if path.suffix.lower() == ".bmp":
        with path.open("rb") as handle:
            file_header = handle.read(14)
            info_header = handle.read(40)
            if len(file_header) != 14 or len(info_header) != 40:
                raise HardwareError(f"Invalid BMP header: {path}")
            bf_type, _, _, _, bf_off_bits = struct.unpack("<HIHHI", file_header)
            if bf_type != 0x4D42:
                raise HardwareError(f"Unsupported BMP signature: {path}")
            (
                _bi_size,
                bi_width,
                bi_height,
                bi_planes,
                bi_bit_count,
                _bi_compression,
                _bi_size_image,
                _bi_xppm,
                _bi_yppm,
                _bi_clr_used,
                _bi_clr_important,
            ) = struct.unpack("<IIIHHIIIIII", info_header)
            if bi_width != R11_QXGA_WIDTH or bi_height != R11_QXGA_HEIGHT:
                raise HardwareError(
                    f"R11 BMP must be {R11_QXGA_WIDTH}x{R11_QXGA_HEIGHT}, got {bi_width}x{bi_height}: {path}"
                )
            if bi_planes != 1 or bi_bit_count != 1:
                raise HardwareError(f"R11 BMP must be 1-bit monochrome: {path}")
            handle.seek(bf_off_bits)
            data = handle.read(R11_BITPLANE_BYTES)
            if len(data) != R11_BITPLANE_BYTES:
                raise HardwareError(f"Unexpected BMP payload size: {path}")
        rows = [
            data[row_index * (R11_QXGA_WIDTH // 8):(row_index + 1) * (R11_QXGA_WIDTH // 8)]
            for row_index in range(R11_QXGA_HEIGHT)
        ]
        rows.reverse()
        return b"".join(rows)

    payload = path.read_bytes()
    if len(payload) != R11_BITPLANE_BYTES:
        raise HardwareError(
            f"R11 pattern must contain exactly {R11_BITPLANE_BYTES} bytes or be a 1-bit BMP: {path}"
        )
    return payload


def _iter_r11_flash_pages(bitplane_data: bytes):
    byte_width = R11_QXGA_WIDTH // 8
    page = bytearray()
    for row in range(R11_QXGA_HEIGHT):
        row_data = bitplane_data[row * byte_width:(row + 1) * byte_width]
        for col in range(byte_width // 4):
            page.append(_REVERSE_BITS_LUT[row_data[col + 0x00]])
            page.append(_REVERSE_BITS_LUT[row_data[col + 0x40]])
            page.append(_REVERSE_BITS_LUT[row_data[col + 0x80]])
            page.append(_REVERSE_BITS_LUT[row_data[col + 0xC0]])
            if len(page) == R11_PAGE_SIZE:
                yield bytes(page)
                page.clear()
    if page:
        raise HardwareError("Unexpected partial R11 flash page produced from bitplane payload.")


class _R11CommLib:
    FDD_SUCCESS = 0
    FDD_SLAVE_EXCEPTION = 0x12

    def __init__(self, dll_path: Path):
        self.dll_path = dll_path
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(str(dll_path.parent))
        self.dll = ctypes.WinDLL(str(dll_path))
        self._bind()

    def _bind(self) -> None:
        self.dll.FDD_LibGetVersion.argtypes = [ctypes.c_char_p, ctypes.c_uint8]
        self.dll.FDD_LibGetVersion.restype = ctypes.c_int
        self.dll.FDD_ExcGetMsg.argtypes = [ctypes.POINTER(ctypes.c_char_p)]
        self.dll.FDD_ExcGetMsg.restype = ctypes.c_int
        self.dll.FDD_DevEnumerateWinUSB.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p]
        self.dll.FDD_DevEnumerateWinUSB.restype = ctypes.c_int
        self.dll.FDD_DevGetFirst.argtypes = [ctypes.POINTER(ctypes.c_char_p)]
        self.dll.FDD_DevGetFirst.restype = ctypes.c_int
        self.dll.FDD_DevGetNext.argtypes = [ctypes.POINTER(ctypes.c_char_p)]
        self.dll.FDD_DevGetNext.restype = ctypes.c_int
        self.dll.FDD_DevOpenWinUSB.argtypes = [ctypes.c_char_p, ctypes.c_uint16]
        self.dll.FDD_DevOpenWinUSB.restype = ctypes.c_int
        self.dll.FDD_DevClose.argtypes = []
        self.dll.FDD_DevClose.restype = ctypes.c_int
        self.dll.R11_LibGetVersion.argtypes = [ctypes.c_char_p, ctypes.c_uint8]
        self.dll.R11_LibGetVersion.restype = ctypes.c_int
        self.dll.R11_RpcSysGetSerialNum.argtypes = [ctypes.POINTER(ctypes.c_uint32)]
        self.dll.R11_RpcSysGetSerialNum.restype = ctypes.c_int
        self.dll.R11_RpcSysGetRepertoireName.argtypes = [ctypes.c_char_p, ctypes.c_uint8]
        self.dll.R11_RpcSysGetRepertoireName.restype = ctypes.c_int
        self.dll.R11_RpcSysGetBitplaneCount.argtypes = [ctypes.POINTER(ctypes.c_uint32)]
        self.dll.R11_RpcSysGetBitplaneCount.restype = ctypes.c_int
        self.dll.R11_RpcSysReloadRepertoire.argtypes = []
        self.dll.R11_RpcSysReloadRepertoire.restype = ctypes.c_int
        self.dll.R11_RpcRoGetCount.argtypes = [ctypes.POINTER(ctypes.c_uint16)]
        self.dll.R11_RpcRoGetCount.restype = ctypes.c_int
        self.dll.R11_RpcRoGetName.argtypes = [ctypes.c_uint16, ctypes.c_char_p, ctypes.c_uint8]
        self.dll.R11_RpcRoGetName.restype = ctypes.c_int
        self.dll.R11_RpcRoGetSelected.argtypes = [ctypes.POINTER(ctypes.c_uint16)]
        self.dll.R11_RpcRoGetSelected.restype = ctypes.c_int
        self.dll.R11_RpcRoSetSelected.argtypes = [ctypes.c_uint16]
        self.dll.R11_RpcRoSetSelected.restype = ctypes.c_int
        if hasattr(self.dll, "R11_RpcRoGetActivationType"):
            self.dll.R11_RpcRoGetActivationType.argtypes = [ctypes.POINTER(ctypes.c_uint8)]
            self.dll.R11_RpcRoGetActivationType.restype = ctypes.c_int
        self.dll.R11_RpcRoActivate.argtypes = []
        self.dll.R11_RpcRoActivate.restype = ctypes.c_int
        self.dll.R11_RpcRoDeactivate.argtypes = []
        self.dll.R11_RpcRoDeactivate.restype = ctypes.c_int
        self.dll.R11_DevGetProgress.argtypes = [ctypes.POINTER(ctypes.c_uint8)]
        self.dll.R11_DevGetProgress.restype = ctypes.c_int
        self.dll.R11_RpcFlashEraseBlock.argtypes = [ctypes.c_uint32]
        self.dll.R11_RpcFlashEraseBlock.restype = ctypes.c_int
        self.dll.R11_FlashWrite.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint16]
        self.dll.R11_FlashWrite.restype = ctypes.c_int
        self.dll.R11_FlashBurn.argtypes = [ctypes.c_uint32]
        self.dll.R11_FlashBurn.restype = ctypes.c_int

    def _exception_message(self) -> str:
        message = ctypes.c_char_p()
        try:
            self.dll.FDD_ExcGetMsg(ctypes.byref(message))
        except Exception:
            return ""
        return message.value.decode("utf-8", errors="ignore") if message.value else ""

    def _check(self, result: int, function_name: str) -> None:
        if result == self.FDD_SUCCESS:
            return
        extra = ""
        if result == self.FDD_SLAVE_EXCEPTION:
            message = self._exception_message()
            if message:
                extra = f": {message}"
        raise HardwareError(f"{function_name} failed with code 0x{result:02X}{extra}")

    def get_versions(self) -> dict[str, str]:
        comm = ctypes.create_string_buffer(128)
        r11 = ctypes.create_string_buffer(128)
        self._check(self.dll.FDD_LibGetVersion(comm, len(comm)), "FDD_LibGetVersion")
        self._check(self.dll.R11_LibGetVersion(r11, len(r11)), "R11_LibGetVersion")
        return {
            "comm_lib": comm.value.decode("utf-8", errors="ignore"),
            "r11_comm_lib": r11.value.decode("utf-8", errors="ignore"),
        }

    def enumerate_winusb_devices(self) -> list[dict[str, str]]:
        self._check(self.dll.FDD_DevEnumerateWinUSB(R11_WINUSB_GUID, None, None), "FDD_DevEnumerateWinUSB")
        devices: list[dict[str, str]] = []
        current = ctypes.c_char_p()
        self._check(self.dll.FDD_DevGetFirst(ctypes.byref(current)), "FDD_DevGetFirst")
        while current.value:
            raw = current.value.decode("utf-8", errors="ignore")
            device_path, serial = (raw.split(":", 1) + [""])[:2] if ":" in raw else (raw, "")
            devices.append({"id": raw, "path": device_path, "serial": serial})
            current = ctypes.c_char_p()
            self._check(self.dll.FDD_DevGetNext(ctypes.byref(current)), "FDD_DevGetNext")
        return devices

    def open_winusb(self, device_path: str, timeout_ms: int = 1000) -> None:
        self._check(self.dll.FDD_DevOpenWinUSB(device_path.encode("utf-8"), timeout_ms), "FDD_DevOpenWinUSB")

    def close(self) -> None:
        self._check(self.dll.FDD_DevClose(), "FDD_DevClose")

    def get_serial_number(self) -> int:
        value = ctypes.c_uint32()
        self._check(self.dll.R11_RpcSysGetSerialNum(ctypes.byref(value)), "R11_RpcSysGetSerialNum")
        return int(value.value)

    def get_bitplane_count(self) -> int:
        value = ctypes.c_uint32()
        self._check(self.dll.R11_RpcSysGetBitplaneCount(ctypes.byref(value)), "R11_RpcSysGetBitplaneCount")
        return int(value.value)

    def get_repertoire_name(self) -> str:
        buffer = ctypes.create_string_buffer(128)
        self._check(self.dll.R11_RpcSysGetRepertoireName(buffer, len(buffer)), "R11_RpcSysGetRepertoireName")
        return buffer.value.decode("utf-8", errors="ignore")

    def get_running_order_count(self) -> int:
        value = ctypes.c_uint16()
        self._check(self.dll.R11_RpcRoGetCount(ctypes.byref(value)), "R11_RpcRoGetCount")
        return int(value.value)

    def get_selected_running_order(self) -> int:
        value = ctypes.c_uint16()
        self._check(self.dll.R11_RpcRoGetSelected(ctypes.byref(value)), "R11_RpcRoGetSelected")
        return int(value.value)

    def set_selected_running_order(self, index: int) -> None:
        self._check(self.dll.R11_RpcRoSetSelected(ctypes.c_uint16(int(index))), "R11_RpcRoSetSelected")

    def get_running_order_name(self, index: int) -> str:
        buffer = ctypes.create_string_buffer(128)
        self._check(self.dll.R11_RpcRoGetName(index, buffer, len(buffer)), "R11_RpcRoGetName")
        return buffer.value.decode("utf-8", errors="ignore")

    def get_running_order_activation_type(self) -> int | None:
        if not hasattr(self.dll, "R11_RpcRoGetActivationType"):
            return None
        value = ctypes.c_uint8()
        self._check(self.dll.R11_RpcRoGetActivationType(ctypes.byref(value)), "R11_RpcRoGetActivationType")
        return int(value.value)

    def deactivate_running_order(self) -> None:
        self._check(self.dll.R11_RpcRoDeactivate(), "R11_RpcRoDeactivate")

    def activate_running_order(self) -> None:
        self._check(self.dll.R11_RpcRoActivate(), "R11_RpcRoActivate")

    def reload_repertoire(self) -> None:
        self._check(self.dll.R11_RpcSysReloadRepertoire(), "R11_RpcSysReloadRepertoire")

    def get_progress(self) -> int:
        value = ctypes.c_uint8()
        self._check(self.dll.R11_DevGetProgress(ctypes.byref(value)), "R11_DevGetProgress")
        return int(value.value)

    def erase_block(self, page_address: int) -> None:
        self._check(self.dll.R11_RpcFlashEraseBlock(page_address), "R11_RpcFlashEraseBlock")

    def write_flash_page(self, payload: bytes) -> None:
        if len(payload) != R11_PAGE_SIZE:
            raise HardwareError(f"R11 flash writes require {R11_PAGE_SIZE}-byte pages.")
        buffer = (ctypes.c_uint8 * len(payload)).from_buffer_copy(payload)
        self._check(self.dll.R11_FlashWrite(buffer, 0, len(payload)), "R11_FlashWrite")

    def burn_flash_page(self, page_address: int) -> None:
        self._check(self.dll.R11_FlashBurn(page_address), "R11_FlashBurn")


class NIDaqAdapter:
    def __init__(self):
        self._available = nidaqmx is not None

    def list_devices(self, default_device: str = "Dev1") -> list[str]:
        if not self._available:
            return []
        try:
            system = nidaqmx.system.System.local()
            return [device.name for device in system.devices]
        except Exception:
            return []

    def list_port0_lines(self, device_name: str | None = None, default_device: str | None = None) -> list[str]:
        selected_device = (device_name or default_device or "Dev1").strip()
        if not selected_device:
            return []
        try:
            devices = self.list_devices(default_device=selected_device)
            if selected_device not in devices:
                return []
            return [f"{selected_device}/port0/line{i}" for i in range(16)]
        except Exception:
            return []

    def play_waveform(self, device_name: str, plan: WaveformPlan) -> None:
        if not self._available:
            raise HardwareError("nidaqmx is not available; cannot drive NI hardware.")
        try:
            with nidaqmx.Task() as task:
                task.do_channels.add_do_chan(
                    f"{device_name}/port0",
                    line_grouping=LineGrouping.CHAN_FOR_ALL_LINES,
                )
                task.timing.cfg_samp_clk_timing(
                    rate=plan.sample_rate_hz,
                    sample_mode=AcquisitionType.FINITE,
                    samps_per_chan=plan.sample_count,
                )
                writer = DigitalSingleChannelWriter(task.out_stream, auto_start=False)
                writer.write_many_sample_port_uint32(plan.packed_port_values.astype(np.uint32))
                task.start()
                task.wait_until_done(timeout=max(5.0, plan.duration_s + 2.0))
        except Exception as exc:
            raise HardwareError(f"Failed to play NI waveform: {exc}") from exc

    def set_all_low(self, device_name: str) -> None:
        if not self._available:
            raise HardwareError("nidaqmx is not available; cannot reset NI outputs.")
        try:
            with nidaqmx.Task() as task:
                task.do_channels.add_do_chan(
                    f"{device_name}/port0",
                    line_grouping=LineGrouping.CHAN_FOR_ALL_LINES,
                )
                task.write(0, auto_start=True)
        except Exception as exc:
            raise HardwareError(f"Failed to reset NI outputs: {exc}") from exc

    def pulse_line(self, device_name: str, line_index: int, duration_s: float) -> None:
        if not self._available:
            raise HardwareError("nidaqmx is not available; cannot pulse NI outputs.")
        if not 0 <= int(line_index) <= 31:
            raise HardwareError(f"Invalid NI line index: {line_index}")
        if duration_s <= 0:
            raise HardwareError(f"Pulse duration must be positive: {duration_s}")

        line_mask = int(1 << int(line_index))
        try:
            with nidaqmx.Task() as task:
                task.do_channels.add_do_chan(
                    f"{device_name}/port0",
                    line_grouping=LineGrouping.CHAN_FOR_ALL_LINES,
                )
                task.write(0, auto_start=True)
                try:
                    task.write(line_mask, auto_start=True)
                    time.sleep(duration_s)
                finally:
                    task.write(0, auto_start=True)
        except Exception as exc:
            raise HardwareError(f"Failed to pulse NI line {line_index} on {device_name}: {exc}") from exc


class FusionBtCameraAdapter:
    def __init__(self, sdk_path: str = ""):
        self.sdk_path = sdk_path
        self._sdk = None
        self._initialized = False
        self._armed = False
        self._frame_count = 0
        self._camera_config = CameraConfig()
        self._dcam = None
        self._dcamapi4 = None
        self._dcam_camera = None
        self._module_dir: Path | None = None
        self._preview_active = False
        self._preview_frame_counter = 0
        self._device_open = False
        self._connected_device_index = 0
        self._connected_device_label = ""
        self._connection_info: dict[str, Any] = {}

    def _initialize_dcam_api(self) -> None:
        self._load_dcam_modules()
        if not self._dcam.Dcamapi.init():
            last_error = self._dcam.Dcamapi.lasterr()
            error_name = getattr(last_error, "name", str(last_error))
            if "ALREADYINITIALIZED" not in str(error_name):
                raise HardwareError(f"DCAM initialization failed: {error_name}")

    def _camera_string(self, camera: Any, key: Any) -> str:
        try:
            value = camera.dev_getstring(key)
        except Exception:
            return ""
        return "" if value is False or value is None else str(value)

    def _close_camera(self) -> None:
        if self._dcam_camera is None:
            return
        try:
            self.stop_preview()
        except Exception:
            pass
        try:
            self.disarm()
        except Exception:
            pass
        try:
            self._dcam_camera.dev_close()
        except Exception:
            pass
        finally:
            self._dcam_camera = None
            self._device_open = False
            self._connection_info = {}
            self._connected_device_label = ""

    def _build_device_descriptor(self, camera_index: int, camera: Any) -> dict[str, Any]:
        idstr = self._dcamapi4.DCAM_IDSTR
        model = self._camera_string(camera, idstr.MODEL) or "Unknown Camera"
        camera_id = self._camera_string(camera, idstr.CAMERAID) or f"CAM-{camera_index:03d}"
        driver_version = self._camera_string(camera, idstr.DRIVERVERSION)
        display = f"{camera_index}: {model} [{camera_id}]"
        descriptor = {
            "index": camera_index,
            "model": model,
            "camera_id": camera_id,
            "driver_version": driver_version,
            "display": display,
        }
        if self._module_dir is not None:
            descriptor["module_dir"] = str(self._module_dir)
        return descriptor

    def _ensure_camera_open(self, device_index: int | None = None, device_label: str = "") -> dict[str, Any]:
        self.initialize()
        selected_index = int(self._camera_config.device_index if device_index is None else device_index)
        if self._device_open and self._dcam_camera is not None and self._connected_device_index == selected_index:
            if device_label:
                self._connected_device_label = device_label
                self._connection_info["display"] = device_label
            return dict(self._connection_info)

        self._close_camera()
        camera = self._dcam.Dcam(selected_index)
        if not camera.dev_open():
            last_error = camera.lasterr()
            error_name = getattr(last_error, "name", str(last_error))
            raise HardwareError(f"Failed to open DCAM camera {selected_index}: {error_name}")
        descriptor = self._build_device_descriptor(selected_index, camera)
        if device_label:
            descriptor["display"] = device_label
        self._dcam_camera = camera
        self._device_open = True
        self._connected_device_index = selected_index
        self._connected_device_label = descriptor["display"]
        self._connection_info = dict(descriptor)
        self._sdk = dict(descriptor)
        return dict(self._connection_info)

    def _load_dcam_modules(self) -> None:
        module_dir = _resolve_dcam_python_dir(self.sdk_path)
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(str(module_dir))
            if self.sdk_path:
                candidate = Path(self.sdk_path)
                if candidate.is_dir():
                    os.add_dll_directory(str(candidate))
        if str(module_dir) not in sys.path:
            sys.path.insert(0, str(module_dir))
        try:
            self._dcamapi4 = importlib.import_module("dcamapi4")
            self._dcam = importlib.import_module("dcam")
        except Exception as exc:
            raise HardwareError(
                "Failed to import Hamamatsu DCAM Python modules. "
                "Confirm the DCAM driver is installed and dcamapi.dll is available."
            ) from exc
        self._module_dir = module_dir

    def _set_property(self, prop_id: int, value: float) -> float:
        actual = self._dcam_camera.prop_setgetvalue(prop_id, value)
        if actual is False:
            raise HardwareError(
                f"Failed to set DCAM property {prop_id}: {self._dcam_camera.lasterr().name}"
            )
        return float(actual)

    def _get_property(self, prop_id: int) -> float:
        value = self._dcam_camera.prop_getvalue(prop_id)
        if value is False:
            raise HardwareError(
                f"Failed to query DCAM property {prop_id}: {self._dcam_camera.lasterr().name}"
            )
        return float(value)

    def _try_get_property(self, prop_id: int) -> float | None:
        try:
            return self._get_property(prop_id)
        except Exception:
            return None

    def _property_text(self, prop_id: int, value: float) -> str:
        try:
            text = self._dcam_camera.prop_getvaluetext(prop_id, value)
        except Exception:
            return ""
        return "" if text is False or text is None else str(text)

    def _get_property_attr(self, prop_id: int) -> Any | None:
        if not hasattr(self._dcam_camera, "prop_getattr"):
            return None
        try:
            attr = self._dcam_camera.prop_getattr(prop_id)
        except Exception:
            return None
        return None if attr is False or attr is None else attr

    @staticmethod
    def _attr_int(attr: Any | None, name: str, default: int) -> int:
        if attr is None:
            return int(default)
        try:
            return int(round(float(getattr(attr, name))))
        except Exception:
            return int(default)

    @staticmethod
    def _align_long_value(value: int, minimum: int, maximum: int, step: int) -> int:
        minimum = int(minimum)
        maximum = max(minimum, int(maximum))
        step = max(1, int(step))
        clamped = min(max(int(value), minimum), maximum)
        return minimum + ((clamped - minimum) // step) * step

    def _roi_axis_limits(self, offset_prop: int, size_prop: int, fallback_sensor_size: int) -> dict[str, int]:
        offset_attr = self._get_property_attr(offset_prop)
        size_attr = self._get_property_attr(size_prop)
        sensor_size = max(1, self._attr_int(size_attr, "valuemax", fallback_sensor_size))
        min_size = max(1, self._attr_int(size_attr, "valuemin", 1))
        size_step = max(1, self._attr_int(size_attr, "valuestep", SIM_CAMERA_ROI_STEP_PX))
        offset_step = max(1, self._attr_int(offset_attr, "valuestep", size_step))
        max_offset = max(0, self._attr_int(offset_attr, "valuemax", sensor_size - min_size))
        return {
            "sensor_size": sensor_size,
            "min_size": min(min_size, sensor_size),
            "size_step": size_step,
            "offset_step": offset_step,
            "max_offset": max_offset,
        }

    def _resolve_roi_axis(self, requested_offset: int, requested_size: int, limits: dict[str, int]) -> tuple[int, int]:
        sensor_size = int(limits["sensor_size"])
        size = self._align_long_value(
            requested_size,
            limits["min_size"],
            sensor_size,
            limits["size_step"],
        )
        max_offset = max(0, min(int(limits["max_offset"]), sensor_size - size))
        offset = self._align_long_value(
            requested_offset,
            0,
            max_offset,
            limits["offset_step"],
        )
        return offset, size

    def _resolve_roi_config(self, config: CameraConfig) -> dict[str, Any]:
        dcamapi4 = self._dcamapi4
        fallback_width, fallback_height = DEFAULT_SIM_CAMERA_SIZE
        h_limits = self._roi_axis_limits(
            dcamapi4.DCAM_IDPROP.SUBARRAYHPOS,
            dcamapi4.DCAM_IDPROP.SUBARRAYHSIZE,
            fallback_sensor_size=fallback_width,
        )
        v_limits = self._roi_axis_limits(
            dcamapi4.DCAM_IDPROP.SUBARRAYVPOS,
            dcamapi4.DCAM_IDPROP.SUBARRAYVSIZE,
            fallback_sensor_size=fallback_height,
        )
        roi_x, roi_width = self._resolve_roi_axis(config.roi_x, config.roi_width, h_limits)
        roi_y, roi_height = self._resolve_roi_axis(config.roi_y, config.roi_height, v_limits)
        sensor_width = int(h_limits["sensor_size"])
        sensor_height = int(v_limits["sensor_size"])
        roi_step_px = max(1, int(max(h_limits["offset_step"], v_limits["offset_step"])))
        return {
            "applied_roi": {
                "x": int(roi_x),
                "y": int(roi_y),
                "width": int(roi_width),
                "height": int(roi_height),
            },
            "sensor_width": sensor_width,
            "sensor_height": sensor_height,
            "roi_step_px": roi_step_px,
            "roi_size_presets": list(build_sim_camera_size_presets(sensor_width, sensor_height)),
        }

    def _apply_roi_axis(self, offset_prop: int, size_prop: int, offset: int, size: int) -> None:
        current_size = self._try_get_property(size_prop)
        if current_size is not None and int(size) < int(round(current_size)):
            self._set_property(size_prop, size)
            self._set_property(offset_prop, offset)
            return
        self._set_property(offset_prop, offset)
        self._set_property(size_prop, size)

    def _apply_roi_config(self, roi_summary: dict[str, Any]) -> None:
        dcamapi4 = self._dcamapi4
        applied_roi = roi_summary["applied_roi"]
        self._set_property(dcamapi4.DCAM_IDPROP.SUBARRAYMODE, dcamapi4.DCAMPROP.MODE.OFF)
        self._apply_roi_axis(
            dcamapi4.DCAM_IDPROP.SUBARRAYHPOS,
            dcamapi4.DCAM_IDPROP.SUBARRAYHSIZE,
            applied_roi["x"],
            applied_roi["width"],
        )
        self._apply_roi_axis(
            dcamapi4.DCAM_IDPROP.SUBARRAYVPOS,
            dcamapi4.DCAM_IDPROP.SUBARRAYVSIZE,
            applied_roi["y"],
            applied_roi["height"],
        )
        self._set_property(dcamapi4.DCAM_IDPROP.SUBARRAYMODE, dcamapi4.DCAMPROP.MODE.ON)

    def _camera_signature(self) -> str:
        parts = [
            str(self._connection_info.get("model", "")),
            str(self._connection_info.get("camera_id", "")),
            str(self._connected_device_label or ""),
        ]
        return " ".join(part for part in parts if part).upper()

    def _fixed_readout_speed_value(self) -> float:
        dcamapi4 = self._dcamapi4
        signature = self._camera_signature()
        requested = None
        if "C13440" in signature or "FLASH4.0" in signature or "FLASH 4.0" in signature:
            requested = 2
        elif "C15440" in signature or "FUSION BT" in signature:
            requested = 3
        if requested is None:
            requested = dcamapi4.DCAMPROP.READOUTSPEED.FASTEST
        try:
            queried = self._dcam_camera.prop_queryvalue(dcamapi4.DCAM_IDPROP.READOUTSPEED, requested)
        except Exception:
            queried = False
        if queried is False:
            return float(requested)
        return float(queried)

    def _supported_bit_depths(self) -> list[int]:
        prop_id = self._dcamapi4.DCAM_IDPROP.BITSPERCHANNEL
        supported: list[int] = []
        for candidate in _SUPPORTED_CAMERA_BIT_DEPTHS:
            try:
                queried = self._dcam_camera.prop_queryvalue(prop_id, candidate)
            except Exception:
                queried = False
            if queried is False:
                continue
            try:
                queried_value = int(round(float(queried)))
            except Exception:
                continue
            if queried_value == candidate and candidate not in supported:
                supported.append(candidate)
        if not supported:
            current_value = self._try_get_property(prop_id)
            if current_value is not None:
                supported.append(int(round(current_value)))
        return sorted({int(value) for value in supported})

    def get_supported_bit_depths(self) -> list[int]:
        if not self._device_open or self._dcam_camera is None:
            return [16]
        supported = self._supported_bit_depths()
        return supported or [16]

    def _resolve_bit_depth(self, requested_bit_depth: int) -> tuple[list[int], int]:
        supported_bit_depths = self._supported_bit_depths()
        if not supported_bit_depths:
            supported_bit_depths = [16]
        if int(requested_bit_depth) in supported_bit_depths:
            return supported_bit_depths, int(requested_bit_depth)
        if 16 in supported_bit_depths:
            return supported_bit_depths, 16
        return supported_bit_depths, max(supported_bit_depths)

    def _apply_bit_depth(self, bit_depth: int) -> None:
        dcamapi4 = self._dcamapi4
        bits_enum_name = f"_{int(bit_depth)}"
        bits_value = getattr(dcamapi4.DCAMPROP.BITSPERCHANNEL, bits_enum_name, int(bit_depth))
        pixel_type = dcamapi4.DCAM_PIXELTYPE.MONO8 if int(bit_depth) <= 8 else dcamapi4.DCAM_PIXELTYPE.MONO16
        self._set_property(dcamapi4.DCAM_IDPROP.BITSPERCHANNEL, bits_value)
        self._set_property(dcamapi4.DCAM_IDPROP.IMAGE_PIXELTYPE, pixel_type)

    def _read_timing_summary(
        self,
        config: CameraConfig,
        supported_bit_depths: list[int],
        applied_bit_depth: int,
        applied_readout_speed_value: float,
    ) -> dict[str, Any]:
        dcamapi4 = self._dcamapi4
        timing_readout_time_s = self._try_get_property(dcamapi4.DCAM_IDPROP.TIMING_READOUTTIME)
        timing_cyclic_trigger_period_s = self._try_get_property(dcamapi4.DCAM_IDPROP.TIMING_CYCLICTRIGGERPERIOD)
        timing_min_trigger_blanking_s = self._try_get_property(dcamapi4.DCAM_IDPROP.TIMING_MINTRIGGERBLANKING)
        readout_s = None if timing_readout_time_s is None else max(0.0, float(timing_readout_time_s))
        min_tb_s = max(0.0, float(timing_min_trigger_blanking_s or 0.0))
        summary = {
            "camera_model": str(self._connection_info.get("model", "")),
            "camera_id": str(self._connection_info.get("camera_id", "")),
            "applied_readout_speed_value": int(round(float(applied_readout_speed_value))),
            "applied_readout_speed_text": self._property_text(
                dcamapi4.DCAM_IDPROP.READOUTSPEED,
                applied_readout_speed_value,
            ),
            "supported_bit_depths": list(supported_bit_depths),
            "applied_bit_depth": int(applied_bit_depth),
            "timing_readout_time_s": timing_readout_time_s,
            "timing_cyclic_trigger_period_s": timing_cyclic_trigger_period_s,
            "timing_min_trigger_blanking_s": timing_min_trigger_blanking_s,
        }
        if readout_s is not None:
            summary["recommended_inter_frame_gap_us"] = (
                int(math.ceil(readout_s * 1_000_000.0))
                + int(math.ceil(min_tb_s * 1_000_000.0))
                + 1000
            )
        return summary

    def initialize(self) -> None:
        if self._initialized:
            return
        self._initialize_dcam_api()
        device_count = self._dcam.Dcamapi.get_devicecount()
        if not device_count:
            raise HardwareError("No Hamamatsu DCAM-compatible camera detected.")
        self._sdk = {"module_dir": str(self._module_dir), "device_count": int(device_count)}
        self._initialized = True

    def list_devices(self) -> list[dict[str, Any]]:
        self._initialize_dcam_api()
        device_count = self._dcam.Dcamapi.get_devicecount()
        if device_count is False:
            raise HardwareError("DCAM API is not initialized; failed to enumerate Hamamatsu cameras.")
        devices: list[dict[str, Any]] = []
        for index in range(int(device_count)):
            camera = self._dcam.Dcam(index)
            if not camera.dev_open():
                last_error = camera.lasterr()
                error_name = getattr(last_error, "name", str(last_error))
                raise HardwareError(f"Failed to enumerate DCAM camera {index}: {error_name}")
            try:
                devices.append(self._build_device_descriptor(index, camera))
            finally:
                try:
                    camera.dev_close()
                except Exception:
                    pass
        return devices

    def is_connected(self) -> bool:
        return self._device_open

    def connection_info(self) -> dict[str, Any]:
        info = dict(self._connection_info)
        if self._device_open and self._dcam_camera is not None:
            info["supported_bit_depths"] = self.get_supported_bit_depths()
        return info

    def connect(self, device_index: int | None = None, device_label: str = "") -> dict[str, Any]:
        selected_index = self._camera_config.device_index if device_index is None else int(device_index)
        self._camera_config.device_index = selected_index
        if device_label:
            self._camera_config.device_label = device_label
        info = self._ensure_camera_open(device_index=selected_index, device_label=device_label or self._camera_config.device_label)
        info["supported_bit_depths"] = self.get_supported_bit_depths()
        self._connection_info["supported_bit_depths"] = list(info["supported_bit_depths"])
        return info

    def disconnect(self) -> None:
        self._close_camera()

    def apply_config(self, config: CameraConfig) -> dict[str, Any]:
        if not self._initialized:
            self.initialize()
        if config.trigger_mode != "external_level":
            raise HardwareError("Fusion BT adapter only supports external_level trigger mode.")
        self._camera_config = config
        self._ensure_camera_open(device_index=config.device_index, device_label=config.device_label)
        try:
            result = self._configure_camera(
                config,
                trigger_source=self._dcamapi4.DCAMPROP.TRIGGERSOURCE.EXTERNAL,
                trigger_active=self._dcamapi4.DCAMPROP.TRIGGERACTIVE.LEVEL,
            )
        except Exception:
            self._close_camera()
            raise
        return result

    def _configure_camera(self, config: CameraConfig, trigger_source: int, trigger_active: int) -> dict[str, Any]:
        self._camera_config = config
        dcamapi4 = self._dcamapi4
        self._set_property(dcamapi4.DCAM_IDPROP.TRIGGERSOURCE, trigger_source)
        self._set_property(dcamapi4.DCAM_IDPROP.TRIGGERACTIVE, trigger_active)
        self._set_property(dcamapi4.DCAM_IDPROP.TRIGGER_MODE, dcamapi4.DCAMPROP.TRIGGER_MODE.NORMAL)
        self._set_property(dcamapi4.DCAM_IDPROP.TRIGGERPOLARITY, dcamapi4.DCAMPROP.TRIGGERPOLARITY.POSITIVE)
        applied_readout_speed_value = self._set_property(
            dcamapi4.DCAM_IDPROP.READOUTSPEED,
            self._fixed_readout_speed_value(),
        )
        supported_bit_depths, applied_bit_depth = self._resolve_bit_depth(config.bit_depth)
        self._apply_bit_depth(applied_bit_depth)
        config.bit_depth = int(applied_bit_depth)
        roi_summary = self._resolve_roi_config(config)
        self._apply_roi_config(roi_summary)
        applied_roi = roi_summary["applied_roi"]
        config.roi_x = int(applied_roi["x"])
        config.roi_y = int(applied_roi["y"])
        config.roi_width = int(applied_roi["width"])
        config.roi_height = int(applied_roi["height"])
        exposure_s = config.exposure_us / 1_000_000.0
        self._set_property(dcamapi4.DCAM_IDPROP.EXPOSURETIME, exposure_s)
        summary = self._read_timing_summary(
            config,
            supported_bit_depths=supported_bit_depths,
            applied_bit_depth=applied_bit_depth,
            applied_readout_speed_value=applied_readout_speed_value,
        )
        summary.update(roi_summary)
        self._connection_info["supported_bit_depths"] = list(supported_bit_depths)
        return summary

    @property
    def preview_active(self) -> bool:
        return self._preview_active

    def start_preview(self, config: CameraConfig, frame_buffer_count: int = 3) -> None:
        if not self._initialized:
            self.initialize()
        if frame_buffer_count <= 0:
            raise HardwareError("frame_buffer_count must be positive.")
        self.stop_preview()
        self.disarm()
        self._camera_config = config
        self._preview_frame_counter = 0
        self._ensure_camera_open(device_index=config.device_index, device_label=config.device_label)
        self._configure_camera(
            config,
            trigger_source=self._dcamapi4.DCAMPROP.TRIGGERSOURCE.INTERNAL,
            trigger_active=self._dcamapi4.DCAMPROP.TRIGGERACTIVE.EDGE,
        )
        if not self._dcam_camera.buf_alloc(frame_buffer_count):
            raise HardwareError(f"Failed to allocate DCAM preview buffer: {self._dcam_camera.lasterr().name}")
        if not self._dcam_camera.cap_start(True):
            try:
                self._dcam_camera.buf_release()
            except Exception:
                pass
            raise HardwareError(f"Failed to start DCAM preview capture: {self._dcam_camera.lasterr().name}")
        self._preview_active = True

    def read_preview_frame(self, timeout_ms: int = 100) -> np.ndarray:
        if not self._preview_active:
            raise HardwareError("Preview must be started before reading frames.")
        if not self._dcam_camera.wait_capevent_frameready(timeout_ms):
            raise HardwareError(f"DCAM preview wait failed: {self._dcam_camera.lasterr().name}")
        frame = self._dcam_camera.buf_getlastframedata()
        if frame is False:
            raise HardwareError(f"Failed to fetch DCAM preview frame: {self._dcam_camera.lasterr().name}")
        self._preview_frame_counter += 1
        return np.array(frame, copy=True, dtype=np.uint16)

    def stop_preview(self) -> None:
        if not self._preview_active:
            return
        self._preview_active = False
        if not self._initialized:
            return
        try:
            self._dcam_camera.cap_stop()
        except Exception:
            pass
        try:
            self._dcam_camera.buf_release()
        except Exception:
            pass

    def arm(self, frame_count: int) -> None:
        if not self._initialized:
            self.initialize()
        if self._preview_active:
            self.stop_preview()
        self._frame_count = frame_count
        self._armed = True
        if frame_count <= 0:
            raise HardwareError("frame_count must be positive.")
        self._ensure_camera_open(
            device_index=self._camera_config.device_index,
            device_label=self._camera_config.device_label,
        )
        if not self._dcam_camera.buf_alloc(frame_count):
            raise HardwareError(f"Failed to allocate DCAM frame buffer: {self._dcam_camera.lasterr().name}")
        if not self._dcam_camera.cap_snapshot():
            raise HardwareError(f"Failed to arm DCAM snapshot capture: {self._dcam_camera.lasterr().name}")

    def disarm(self) -> None:
        self._armed = False
        self._frame_count = 0
        if not self._initialized:
            return
        try:
            self._dcam_camera.cap_stop()
        except Exception:
            pass
        try:
            self._dcam_camera.buf_release()
        except Exception:
            pass

    def read_frame_sequence(
        self,
        frame_count: int,
        pattern_files: list[str],
        laser_wavelength_nm: int,
        frame_callback: Any | None = None,
    ) -> tuple[np.ndarray, list[float]]:
        if not self._armed:
            raise HardwareError("Camera must be armed before reading frame sequence.")
        timestamps: list[float] = []
        captured = 0
        overall_timeout_ms = max(
            self._camera_config.timeout_ms,
            int(frame_count * max(self._camera_config.exposure_us / 1000.0 + 50.0, 10.0) + 1000.0),
        )
        started_at = time.time()
        while captured < frame_count:
            transfer = self._dcam_camera.cap_transferinfo()
            if transfer is False:
                raise HardwareError(f"Failed to query DCAM transfer info: {self._dcam_camera.lasterr().name}")
            transferred = min(max(int(transfer.nFrameCount), 0), frame_count)
            if transferred > captured:
                while captured < transferred:
                    captured += 1
                    timestamp = time.time()
                    timestamps.append(timestamp)
                    if frame_callback is not None:
                        frame_callback(captured, timestamp)
                continue

            elapsed_ms = int((time.time() - started_at) * 1000.0)
            remaining_ms = overall_timeout_ms - elapsed_ms
            if remaining_ms <= 0:
                raise HardwareError(
                    "Timed out waiting for "
                    f"{frame_count} externally triggered frames from Fusion BT; captured {captured}/{frame_count}."
                )
            if not self._dcam_camera.wait_capevent_frameready(remaining_ms):
                transfer = self._dcam_camera.cap_transferinfo()
                if transfer is False:
                    raise HardwareError(f"Failed to query DCAM transfer info: {self._dcam_camera.lasterr().name}")
                transferred = min(max(int(transfer.nFrameCount), 0), frame_count)
                if transferred > captured:
                    continue
                raise HardwareError(
                    f"DCAM frame wait failed: {self._dcam_camera.lasterr().name}; "
                    f"captured {captured}/{frame_count} frames. Confirm camera trigger TTL, trigger mode, "
                    "and DAQ camera_trigger_line wiring."
                )

        frames = np.empty((frame_count, self._camera_config.roi_height, self._camera_config.roi_width), dtype=np.uint16)
        for index in range(frame_count):
            frame = self._dcam_camera.buf_getframedata(index)
            if frame is False:
                raise HardwareError(f"Failed to read DCAM frame {index}: {self._dcam_camera.lasterr().name}")
            frames[index] = np.array(frame, copy=True, dtype=np.uint16)
        return frames, timestamps


class KopinSlmAdapter:
    def __init__(self, sdk_path: str = ""):
        self.sdk_path = sdk_path
        self._sdk: _R11CommLib | None = None
        self._initialized = False
        self._prepared = PatternPreparationResult()
        self._device_info: dict[str, Any] = {}
        self._base_info: dict[str, Any] = {}
        self._device_open = False
        self._connected_device_path = ""

    def list_devices(self) -> list[dict[str, str]]:
        if not self._initialized:
            self.initialize()
        devices = self._sdk.enumerate_winusb_devices()
        formatted: list[dict[str, str]] = []
        for index, device in enumerate(devices, start=1):
            label = device["serial"] or f"R11 Device {index}"
            if not device["serial"] and device["path"]:
                label = device["path"]
            formatted.append({**device, "display": label})
        return formatted

    def is_connected(self) -> bool:
        return self._device_open

    def connection_info(self) -> dict[str, Any]:
        return dict(self._device_info)

    def connect(self, device_path: str | None = None) -> dict[str, Any]:
        if not self._initialized:
            self.initialize()
        if self._device_open and (not device_path or device_path == self._connected_device_path):
            return self.connection_info()
        if self._device_open and device_path and device_path != self._connected_device_path:
            self.disconnect()
        devices = self.list_devices()
        if not devices:
            raise HardwareError("No R11 WinUSB devices found. Confirm the R11 driver is installed and the device is connected.")
        device = devices[0]
        if device_path:
            matching = next((item for item in devices if item["path"] == device_path or item["id"] == device_path), None)
            if matching is None:
                raise HardwareError(f"Selected R11 device not found: {device_path}")
            device = matching
        self._sdk.open_winusb(device["path"])
        self._connected_device_path = device["path"]
        self._device_info = {
            **self._base_info,
            "device_id": device["id"],
            "device_path": device["path"],
            "device_serial_hint": device["serial"],
            "serial_number": self._sdk.get_serial_number(),
            "repertoire_name": self._sdk.get_repertoire_name(),
        }
        self._device_open = True
        return self.connection_info()

    def disconnect(self) -> None:
        if not self._device_open:
            return
        try:
            self._sdk.close()
        finally:
            self._device_open = False
            self._connected_device_path = ""
            self._device_info = dict(self._base_info)

    def _connect(self, device_path: str | None = None) -> None:
        self.connect(device_path=device_path)

    def list_running_orders(self) -> list[tuple[int, str]]:
        if not self._initialized:
            self.initialize()
        if not self._device_open:
            raise HardwareError("Connect to an SLM before listing running orders.")
        running_order_count = self._sdk.get_running_order_count()
        return [
            (index, self._sdk.get_running_order_name(index))
            for index in range(running_order_count)
        ]

    def select_running_order(self, ro_index: int) -> dict[str, Any]:
        if not self._initialized:
            self.initialize()
        if not self._device_open:
            raise HardwareError("Connect to an SLM before selecting a running order.")
        ro_index = int(ro_index)
        self._sdk.set_selected_running_order(ro_index)
        ro_name = self._sdk.get_running_order_name(ro_index)
        activation_type = self._sdk.get_running_order_activation_type()
        self._prepared = PatternPreparationResult(
            pattern_files=[ro_name] * 9,
            handles=[-1],
            prepared_at=time.time(),
            metadata={
                "mode": "running_order",
                "running_order_index": ro_index,
                "running_order_name": ro_name,
                "activation_type": activation_type,
                **self._device_info,
            },
        )
        return {
            "running_order_index": ro_index,
            "running_order_name": ro_name,
            "activation_type": activation_type,
            "pattern_result": self._prepared,
        }

    def _upload_pattern(self, bitplane_index: int, pattern_path: Path) -> None:
        payload = _load_r11_bitplane_file(pattern_path)
        page_base = bitplane_index * R11_BITPLANE_PAGES
        last_block_num = None
        for page_offset, page_payload in enumerate(_iter_r11_flash_pages(payload)):
            page_num = page_base + page_offset
            block_num = page_num // R11_PAGES_PER_BLOCK
            page_address = R11_IMAGE_BASE + page_num
            if block_num != last_block_num:
                self._sdk.erase_block(page_address)
                last_block_num = block_num
            self._sdk.write_flash_page(page_payload)
            self._sdk.burn_flash_page(page_address)

    def initialize(self) -> None:
        if self._initialized:
            return
        dll_path = _resolve_r11_dll_path(self.sdk_path)
        self._sdk = _R11CommLib(dll_path)
        versions = self._sdk.get_versions()
        self._base_info = {
            "dll_path": str(dll_path),
            "comm_lib_version": versions["comm_lib"],
            "r11_comm_lib_version": versions["r11_comm_lib"],
        }
        self._device_info = dict(self._base_info)
        self._initialized = True

    def program_patterns(self, pattern_files: list[str], device_path: str | None = None) -> PatternPreparationResult:
        if not self._initialized:
            self.initialize()
        if len(pattern_files) != 9:
            raise HardwareError("Exactly 9 pattern files are required.")
        missing = [path for path in pattern_files if not path or not Path(path).exists()]
        if missing:
            raise HardwareError(f"Pattern files not found: {missing}")

        self._connect(device_path=device_path)
        self._sdk.deactivate_running_order()
        bitplane_count = self._sdk.get_bitplane_count()
        if bitplane_count < len(pattern_files):
            raise HardwareError(
                f"R11 repertoire exposes only {bitplane_count} bitplanes, but {len(pattern_files)} pattern files were provided."
            )
        for index, pattern_file in enumerate(pattern_files):
            self._upload_pattern(index, Path(pattern_file))
        self._sdk.reload_repertoire()
        started_at = time.time()
        progress = 0
        while progress < 100:
            if time.time() - started_at > 30.0:
                raise HardwareError("Timed out waiting for R11 repertoire reload to complete.")
            progress = self._sdk.get_progress()
            time.sleep(0.05)
        running_order_count = self._sdk.get_running_order_count()
        selected_ro = self._sdk.get_selected_running_order()
        running_orders = [
            self._sdk.get_running_order_name(index)
            for index in range(running_order_count)
        ]
        self._prepared = PatternPreparationResult(
            pattern_files=list(pattern_files),
            handles=list(range(len(pattern_files))),
            prepared_at=time.time(),
            metadata={
                "mode": "r11_winusb",
                "bitplane_count": bitplane_count,
                "selected_running_order": selected_ro,
                "running_orders": running_orders,
                **self._device_info,
            },
        )
        return self._prepared

    def activate_prepared_patterns(self) -> None:
        if not self._prepared.handles:
            raise HardwareError("No patterns or running order prepared on SLM.")
        self._connect(device_path=self._connected_device_path or None)
        self._sdk.activate_running_order()

    def prepared_summary(self) -> dict[str, Any]:
        return asdict(self._prepared)


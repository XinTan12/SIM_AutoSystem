from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np


_BACKEND_MODULE = "sim_wiener_gpu_emdapp_batchInGroup_batchBetGroup"
_SUPPORTED_DTYPES = {"single", "float32", "fp32", "double", "float64", "fp64"}


def _validate_stack(stack: np.ndarray) -> np.ndarray:
    arr = np.asarray(stack)
    if arr.ndim != 3 or arr.shape[0] != 9:
        raise ValueError(f"Expected SIM stack shape (9, H, W), got {arr.shape}.")
    if arr.dtype != np.uint16:
        raise TypeError(f"Expected SIM stack dtype uint16, got {arr.dtype}.")
    return np.ascontiguousarray(arr)


def _validate_file_path(path: str, label: str) -> str:
    value = str(path or "").strip()
    if not value:
        raise FileNotFoundError(f"{label} path is required for SIM Wiener reconstruction.")
    resolved = Path(value)
    if not resolved.is_file():
        raise FileNotFoundError(f"{label} file does not exist: {value}")
    return str(resolved)


def _load_backend():
    module_dir = Path(__file__).resolve().parent
    module_dir_str = str(module_dir)
    if module_dir_str not in sys.path:
        sys.path.insert(0, module_dir_str)
    try:
        return importlib.import_module(_BACKEND_MODULE)
    except ModuleNotFoundError as exc:
        missing = exc.name or str(exc)
        raise RuntimeError(
            "SIM Wiener GPU reconstruction requires the backend module and its "
            f"Python dependencies. Missing import: {missing}. Install torch/scipy "
            "and keep the reconstruction files together before enabling reconstruction."
        ) from exc


def _to_float_array(value: Any) -> np.ndarray:
    if value is None:
        return np.asarray([], dtype=np.float32)
    return np.asarray(value, dtype=np.float32)


def _metadata_from_result(
    *,
    result: dict[str, Any],
    stack: np.ndarray,
    reconstruction: np.ndarray,
    wavelength_nm: int,
    otf_path: str,
    background_path: str,
    device: str,
    dtype: str,
) -> dict[str, Any]:
    param = result.get("param_estimation", {}) or {}
    cfg = result.get("cfg")
    timings = dict(result.get("timings_ms", {}) or {})
    return {
        "algorithm": "sim_wiener_gpu",
        "stack_shape": list(stack.shape),
        "reconstruction_shape": list(reconstruction.shape),
        "laser_wavelength_nm": int(wavelength_nm),
        "otf_path": str(otf_path),
        "background_path": str(background_path or ""),
        "c6": _to_float_array(param.get("c6")).tolist(),
        "angle6": _to_float_array(param.get("angle6")).tolist(),
        "R2_angles": _to_float_array(param.get("R2_angles")).tolist(),
        "timings_ms": timings,
        "device": str(result.get("device") or device),
        "dtype": str(result.get("dtype") or dtype),
        "wiener": float(getattr(cfg, "wiener", result.get("wiener", 2.0))),
        "pixel_size_nm": float(getattr(cfg, "pixel_size_nm", 65.0)),
        "excitation_na": float(getattr(cfg, "excitation_na", 1.49)),
        "theta_ratio": list(getattr(cfg, "theta_ratio", (1, 1, 1))),
        "recon_group_batch": int(getattr(cfg, "recon_group_batch", 1)),
    }


def _build_options(
    backend,
    *,
    wavelength_nm: int,
    otf_path: str,
    background_path: str,
    device: str,
    dtype: str,
    wiener: float,
    pixel_size_nm: float,
    excitation_na: float,
    theta_ratio: tuple[int, int, int],
    recon_group_batch: int,
):
    if dtype not in _SUPPORTED_DTYPES:
        raise ValueError(f"Unsupported dtype: {dtype}")
    opts = backend.hessian_sim_wiener_default_options()
    opts.mode = "3beam"
    opts.wavelength_nm = int(wavelength_nm)
    opts.wiener = float(wiener)
    opts.avg_groups = 1
    opts.starframe = 1
    opts.reconstruct_group_stride = 1
    opts.recon_group_batch = int(recon_group_batch)
    opts.pixel_size_nm = float(pixel_size_nm)
    opts.excitation_na = float(excitation_na)
    opts.theta_ratio = tuple(int(value) for value in theta_ratio)
    opts.otf_path = str(otf_path)
    opts.background_path = str(background_path or "")
    opts.output_dir = ""
    opts.dtype = str(dtype)
    opts.device = str(device)
    opts.use_background = bool(background_path)
    opts.save_pseudo_tirf = False
    opts.save_param_mat = False
    opts.save_param_npy = False
    opts.debug_save = False
    opts.profile_timing = False
    opts.save_timing_json = False
    opts.timing_json_path = ""
    opts.fail_on_low_r2 = False
    opts.warn_on_low_r2 = True
    opts.fill_missing_c6 = False
    opts.raw_path = f"in_memory_sim9_{int(wavelength_nm)}nm.tif"
    return opts


def _create_in_memory_reconstructor(backend, *, device: str, dtype: str):
    class InMemorySIMWienerReconstructor(backend.SIMWienerGPUReconstructor):
        def _save_param_files(self, param, prefix, cfg):  # noqa: ANN001
            return None

        def _finalize_timings(self, cfg):  # noqa: ANN001
            return None

        def reconstruct_stack(self, stack: np.ndarray, opts) -> dict[str, Any]:  # noqa: ANN001
            t_total0 = self._now()
            self.timings_ms = {}
            cfg = opts.normalize(opts.raw_path)
            self.cfg = cfg
            if cfg.device:
                self.device = backend.torch.device(cfg.device)
            self.real_dtype = backend._torch_dtype_from_name(cfg.dtype)
            self.complex_dtype = backend._complex_dtype(self.real_dtype)

            try:
                t0 = self._now()
                raw_info = {
                    "num_frames": int(stack.shape[0]),
                    "height": int(stack.shape[1]),
                    "width": int(stack.shape[2]),
                }
                raw_gpu = backend.torch.as_tensor(
                    stack.astype(self._np_dtype(), copy=False),
                    device=self.device,
                    dtype=self.real_dtype,
                )
                otf_template = self._load_2d_tiff(cfg.otf_path)
                background = self._load_2d_tiff(cfg.background_path) if cfg.background_path else None
                t1 = self._now()
                self._record_timing("step1_in_memory_prepare_ms", self._ms(t0, t1))

                param = self._estimate_parameters(
                    cfg,
                    raw_info,
                    raw_gpu,
                    otf_template,
                    f"in_memory_sim9_{int(cfg.wavelength_nm)}nm",
                )
                recon, c6, angle6, r2_angles = self._wiener_reconstruct(
                    cfg,
                    raw_info,
                    raw_gpu,
                    otf_template,
                    background,
                    param,
                )

                t0 = self._now()
                out_arr = backend._to_numpy(recon).astype(np.float32)
                t1 = self._now()
                self._record_timing("step5_materialize_output_ms", self._ms(t0, t1))
                self._record_timing("total_pipeline_ms", self._ms(t_total0, self._now()))
                self._finalize_timings(cfg)
                return {
                    "cfg": cfg,
                    "raw_info": raw_info,
                    "param_estimation": {
                        "c6": backend._to_numpy(c6),
                        "angle6": backend._to_numpy(angle6),
                        "R2_angles": backend._to_numpy(r2_angles),
                    },
                    "reconstruction": out_arr,
                    "paths": {},
                    "timings_ms": dict(self.timings_ms),
                    "device": str(self.device),
                    "dtype": str(cfg.dtype),
                }
            except Exception:
                self._record_timing("total_pipeline_ms", self._ms(t_total0, self._now()))
                self._finalize_timings(cfg)
                raise

    return InMemorySIMWienerReconstructor(device=device, dtype=dtype)


def reconstruct_sim9_stack(
    stack: np.ndarray,
    *,
    wavelength_nm: int,
    otf_path: str,
    background_path: str = "",
    device: str = "cuda",
    dtype: str = "single",
    wiener: float = 2.0,
    pixel_size_nm: float = 65.0,
    excitation_na: float = 1.49,
    theta_ratio: tuple[int, int, int] = (1, 1, 1),
    recon_group_batch: int = 1,
) -> dict[str, Any]:
    """Run the accelerated SIM9 Wiener reconstructor on an in-memory uint16 stack."""
    t0 = time.perf_counter()
    stack_arr = _validate_stack(stack)
    resolved_otf_path = _validate_file_path(otf_path, "OTF")
    resolved_background_path = ""
    if background_path:
        resolved_background_path = _validate_file_path(background_path, "Background")
    if len(theta_ratio) != 3:
        raise ValueError(f"theta_ratio must contain exactly 3 values, got {theta_ratio!r}.")
    if int(recon_group_batch) < 1:
        raise ValueError("recon_group_batch must be >= 1.")

    backend = _load_backend()
    opts = _build_options(
        backend,
        wavelength_nm=wavelength_nm,
        otf_path=resolved_otf_path,
        background_path=resolved_background_path,
        device=device,
        dtype=dtype,
        wiener=wiener,
        pixel_size_nm=pixel_size_nm,
        excitation_na=excitation_na,
        theta_ratio=theta_ratio,
        recon_group_batch=recon_group_batch,
    )
    reconstructor = _create_in_memory_reconstructor(backend, device=device, dtype=dtype)
    result = reconstructor.reconstruct_stack(stack_arr, opts)
    reconstruction = np.asarray(result["reconstruction"], dtype=np.float32)
    metadata = _metadata_from_result(
        result=result,
        stack=stack_arr,
        reconstruction=reconstruction,
        wavelength_nm=wavelength_nm,
        otf_path=resolved_otf_path,
        background_path=resolved_background_path,
        device=device,
        dtype=dtype,
    )
    metadata["timings_ms"].setdefault("wrapper_total_ms", (time.perf_counter() - t0) * 1000.0)
    result["reconstruction"] = reconstruction
    result["metadata"] = metadata
    return result

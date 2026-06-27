"""SIM9 GPU Wiener 重建的唯一集成层（import-safe，进程内常驻热重建器）。

作用：
    把 ``reconstruction/`` 下队友的 GPU Wiener 重建引擎
    （``sim_wiener_gpu_emdapp_batchInGroup_batchBetGroup.py``）安全地接入 SIM 采集
    pipeline：接收内存里的 ``(9, H, W)`` ``numpy.uint16`` 栈，跳过引擎自带的磁盘
    读写脚本路径，返回 ``{"reconstruction", "metadata", ...}``。

    本模块是项目里**唯一**依赖重建引擎私有方法的地方（GUI / pipeline 只看到这里
    暴露的公开类与函数）。``reconstruction/sim_wiener.py`` 是队友的计时 demo，
    ``import`` 即执行整段重建且含硬编码路径，**绝不能被当作库导入**；本模块只
    ``import`` 引擎模块本身。

设计要点：
    - ``_load_backend``：幂等加载引擎模块（只插一次 ``sys.path``、模块级缓存），并
      校验所需公开/私有 API 仍然存在，缺失时给出可区分"缺 torch/scipy"与"后端 API
      变化"的错误。
    - ``InMemorySIMWienerReconstructor.reconstruct_stack``：自复刻必要重建流程，
      **不调用引擎原始 ``reconstruct()``**（后者会读/写 TIFF、写 ``.mat``），按
      ``use_saved_params`` 在 estimate 与 saved-params 两条路径间分派，产线不写盘。
    - ``WarmSIMReconstructor``：持有**唯一一个**重建器实例并跨细胞复用，让 cuFFT
      plan / caching-allocator / 坐标网格缓存在进程内保留，从第二个细胞起进入热速度；
      自动按首帧形状预热。

协作关系：
    上游：``sim_control/pipeline.py`` 的 ``ReconstructionWorker``。
    下游：``reconstruction/sim_wiener_gpu_emdapp_batchInGroup_batchBetGroup.py``（引擎，只读 import）。
    相关：``tests/test_sim_reconstruction.py`` 用 fake backend 覆盖分支、复用与 fallback。
"""

from __future__ import annotations

import copy
import importlib
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# 引擎模块名（按文件名 import，不走 ``reconstruction.`` 包，避免触碰 demo 脚本）。
_BACKEND_MODULE = "sim_wiener_gpu_emdapp_batchInGroup_batchBetGroup"
# 仓库内重建引擎目录；引擎内部含 ``from emd_fast_torch import ...``，故需把该目录加入 sys.path。
_RECONSTRUCTION_DIR = Path(__file__).resolve().parent.parent / "reconstruction"
_SUPPORTED_DTYPES = {"single", "float32", "fp32", "double", "float64", "fp64"}
_FALLBACK_MODES = {"fail", "estimate"}

# 后端必须暴露的公开符号与重建器方法；缺失说明引擎 API 变化，需要更新本集成层。
_REQUIRED_BACKEND_ATTRS = (
    "hessian_sim_wiener_default_options",
    "SIMWienerGPUReconstructor",
    "torch",
    "_torch_dtype_from_name",
    "_complex_dtype",
    "_to_numpy",
)
_REQUIRED_RECONSTRUCTOR_METHODS = (
    "_np_dtype",
    "_now",
    "_ms",
    "_record_timing",
    "_load_2d_tiff",
    "_estimate_parameters",
    "_wiener_reconstruct",
    # saved-params 路径用到（reconstruct_stack 在 cfg.use_saved_params 时调用）；
    # 列入必检，使后端 API 变化在加载期就清晰失败，而非到 saved 重建时才崩。
    "_load_estimated_params_file",
    "_wiener_reconstruct_with_saved_params",
)

# 进程级后端缓存：只在成功 import 后写入；失败不污染缓存，便于无 torch 环境重试/报错。
_BACKEND_CACHE: Any | None = None
_BACKEND_LOCK = threading.Lock()


def _validate_stack(stack: np.ndarray) -> np.ndarray:
    """校验输入为 ``(9, H, W)`` ``uint16``，并返回 C 连续副本。

    校验顺序（shape -> dtype）在后端 import 之前完成，保证无 torch 环境也能给出
    清晰错误，且不会因为非法输入去触发昂贵的后端加载。
    """
    arr = np.asarray(stack)
    if arr.ndim != 3 or arr.shape[0] != 9:
        raise ValueError(f"Expected SIM stack shape (9, H, W), got {arr.shape}.")
    if arr.dtype != np.uint16:
        raise TypeError(f"Expected SIM stack dtype uint16, got {arr.dtype}.")
    return np.ascontiguousarray(arr)


def _validate_file_path(path: str, label: str) -> str:
    """校验文件路径非空且存在；返回解析后的字符串路径。"""
    value = str(path or "").strip()
    if not value:
        raise FileNotFoundError(f"{label} path is required for SIM Wiener reconstruction.")
    resolved = Path(value)
    if not resolved.is_file():
        raise FileNotFoundError(f"{label} file does not exist: {value}")
    return str(resolved)


def _to_float_array(value: Any) -> np.ndarray:
    if value is None:
        return np.asarray([], dtype=np.float32)
    return np.asarray(value, dtype=np.float32)


def _path_fingerprint(path: str, device: str, dtype: str) -> tuple:
    """缓存指纹：解析路径 + mtime_ns + size + device + dtype。

    切换 ROI 不影响（OTF 是原始 2D 模板），但换文件、换 device/dtype 或文件被原地
    替换（mtime/size 变化）都会让指纹变化，从而避免错误复用旧缓存（codex C4）。
    """
    resolved = str(Path(path).resolve())
    try:
        stat = os.stat(resolved)
        return (resolved, stat.st_mtime_ns, stat.st_size, str(device), str(dtype))
    except OSError:
        return (resolved, None, None, str(device), str(dtype))


def _load_backend() -> Any:
    """幂等加载并缓存重建后端模块，并校验所需 API。

    - 只把 ``reconstruction/`` 绝对路径插入 ``sys.path`` 一次；
    - 成功后写进程级缓存，长期 GUI 进程不重复 import / 不重复改 ``sys.path``；
    - 精确区分缺依赖（torch/scipy/emd_fast_torch）与后端 API 变化两类错误。
    """
    global _BACKEND_CACHE
    if _BACKEND_CACHE is not None:
        return _BACKEND_CACHE
    with _BACKEND_LOCK:
        if _BACKEND_CACHE is not None:
            return _BACKEND_CACHE
        module_dir_str = str(_RECONSTRUCTION_DIR)
        if module_dir_str not in sys.path:
            sys.path.insert(0, module_dir_str)
        try:
            backend = importlib.import_module(_BACKEND_MODULE)
        except ModuleNotFoundError as exc:
            missing = exc.name or str(exc)
            raise RuntimeError(
                "SIM Wiener GPU reconstruction requires the backend module and its "
                f"Python dependencies. Missing import: {missing}. Install torch/scipy "
                "and keep the reconstruction files together before enabling reconstruction."
            ) from exc
        except ImportError as exc:  # 后端文件存在但导入期出错（如 emd_fast_torch 内部问题）。
            raise RuntimeError(
                "Failed to import the SIM Wiener reconstruction backend "
                f"({_BACKEND_MODULE}). Underlying error: {exc}."
            ) from exc

        missing_attrs = [name for name in _REQUIRED_BACKEND_ATTRS if not hasattr(backend, name)]
        recon_cls = getattr(backend, "SIMWienerGPUReconstructor", None)
        missing_methods = (
            [name for name in _REQUIRED_RECONSTRUCTOR_METHODS if not hasattr(recon_cls, name)]
            if recon_cls is not None
            else list(_REQUIRED_RECONSTRUCTOR_METHODS)
        )
        if missing_attrs or missing_methods:
            raise RuntimeError(
                "SIM Wiener reconstruction backend API changed; integration layer "
                f"({__name__}) needs updating. Missing module attrs: {missing_attrs}; "
                f"missing reconstructor methods: {missing_methods}."
            )
        _BACKEND_CACHE = backend
        return backend


def _build_options(
    backend: Any,
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
    use_saved_params: bool,
    estimated_params_path: str,
) -> Any:
    """构造 ``SIMWienerOptions``，显式禁用一切磁盘保存与 profiling（产线不落盘）。"""
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
    opts.use_saved_params = bool(use_saved_params)
    opts.estimated_params_path = str(estimated_params_path or "")
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


def _create_in_memory_reconstructor(backend: Any, *, device: str, dtype: str):
    """构造一个内存注入版重建器实例（子类化引擎 ``SIMWienerGPUReconstructor``）。"""

    class InMemorySIMWienerReconstructor(backend.SIMWienerGPUReconstructor):
        """内存重建器：直接吃 numpy 栈、按需 saved/estimate 分派、绝不落盘。"""

        def __init__(self, device=None, dtype=None):  # noqa: ANN001
            super().__init__(device=device, dtype=dtype)
            # 按 (路径, mtime, size, device, dtype) 指纹缓存 OTF/background/saved-params，
            # 让常驻实例跨细胞复用，避免每帧重复读盘/重采样（codex C4/C7）。
            self._tiff_cache: dict[tuple, Any] = {}
            self._params_cache: dict[tuple, Any] = {}

        def _save_param_files(self, param, prefix, cfg):  # noqa: ANN001
            # 关闭引擎内部参数落盘（estimate 路径也会途经此 hook）。
            return None

        def _finalize_timings(self, cfg):  # noqa: ANN001
            return None

        def _load_2d_tiff(self, path):  # noqa: ANN001
            key = _path_fingerprint(str(path), self.device, str(self.real_dtype))
            cached = self._tiff_cache.get(key)
            if cached is not None:
                return cached
            arr = super()._load_2d_tiff(path)
            self._tiff_cache[key] = arr
            return arr

        def _load_estimated_params_cached(self, path):  # noqa: ANN001
            key = _path_fingerprint(str(path), self.device, str(self.real_dtype))
            cached = self._params_cache.get(key)
            if cached is not None:
                return cached
            loaded = self._load_estimated_params_file(path)
            self._params_cache[key] = loaded
            return loaded

        def reconstruct_stack(
            self,
            stack: np.ndarray,
            opts,  # noqa: ANN001
            *,
            saved_params_fallback: str = "fail",
        ) -> dict[str, Any]:
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
                # 显式复刻引擎 ``_load_stack_to_gpu`` 的 dtype 转换语义（codex C6）：
                # 先 astype 到后端期望的 np dtype，再 as_tensor 上 GPU。
                raw_np = stack.astype(self._np_dtype(), copy=False)
                raw_gpu = backend.torch.as_tensor(raw_np, device=self.device, dtype=self.real_dtype)
                otf_template = self._load_2d_tiff(cfg.otf_path)
                background = self._load_2d_tiff(cfg.background_path) if cfg.background_path else None
                t1 = self._now()
                self._record_timing("step1_in_memory_prepare_ms", self._ms(t0, t1))

                param_prefix = f"in_memory_sim9_{int(cfg.wavelength_nm)}nm"
                fallback_used = False
                if getattr(cfg, "use_saved_params", False):
                    try:
                        param, saved_estimates = self._load_estimated_params_cached(cfg.estimated_params_path)
                        recon, c6, angle6, r2_angles = self._wiener_reconstruct_with_saved_params(
                            cfg, raw_info, raw_gpu, otf_template, background, param, saved_estimates
                        )
                    except (FileNotFoundError, KeyError, ValueError) as exc:
                        # 引擎自身已对 .mat 结构/长度做校验（按 n / nangles / ns，不假设 num_modes）。
                        if saved_params_fallback != "estimate":
                            raise
                        logger.warning(
                            "Saved-params reconstruction failed (%s); falling back to per-cell estimate.",
                            exc,
                        )
                        fallback_used = True
                        param = self._estimate_parameters(cfg, raw_info, raw_gpu, otf_template, param_prefix)
                        recon, c6, angle6, r2_angles = self._wiener_reconstruct(
                            cfg, raw_info, raw_gpu, otf_template, background, param
                        )
                else:
                    param = self._estimate_parameters(cfg, raw_info, raw_gpu, otf_template, param_prefix)
                    recon, c6, angle6, r2_angles = self._wiener_reconstruct(
                        cfg, raw_info, raw_gpu, otf_template, background, param
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
                    "use_saved_params": bool(getattr(cfg, "use_saved_params", False)),
                    "estimated_params_path": str(getattr(cfg, "estimated_params_path", "") or ""),
                    "fallback_used": bool(fallback_used),
                }
            except Exception:
                self._record_timing("total_pipeline_ms", self._ms(t_total0, self._now()))
                self._finalize_timings(cfg)
                raise

    return InMemorySIMWienerReconstructor(device=device, dtype=dtype)


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
    warm_cache_hit: bool,
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
        "use_saved_params": bool(result.get("use_saved_params", False)),
        "params_path": str(result.get("estimated_params_path", "") or ""),
        "fallback_used": bool(result.get("fallback_used", False)),
        "warm_cache_hit": bool(warm_cache_hit),
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


def _validate_common_inputs(
    *,
    theta_ratio: tuple[int, int, int],
    recon_group_batch: int,
    saved_params_fallback: str,
) -> None:
    if len(theta_ratio) != 3:
        raise ValueError(f"theta_ratio must contain exactly 3 values, got {theta_ratio!r}.")
    if int(recon_group_batch) < 1:
        raise ValueError("recon_group_batch must be >= 1.")
    if saved_params_fallback not in _FALLBACK_MODES:
        raise ValueError(f"saved_params_fallback must be one of {sorted(_FALLBACK_MODES)}.")


class WarmSIMReconstructor:
    """进程内常驻热重建器：持有唯一引擎实例并跨细胞复用。

    复用同一个 ``InMemorySIMWienerReconstructor`` 实例，使 cuFFT plan、PyTorch
    caching-allocator、坐标网格缓存与 OTF/参数缓存在进程内保留，从第二个同尺寸细胞
    起进入热速度（首帧顺带建好该尺寸 plan，自动按首帧预热）。``device``/``dtype``
    变化时重建内部实例（CUDA 上下文绑定）。本类不是线程安全容器：约定只在持有它的
    重建线程上调用。
    """

    def __init__(self, device: str = "cuda", dtype: str = "single") -> None:
        if dtype not in _SUPPORTED_DTYPES:
            raise ValueError(f"Unsupported dtype: {dtype}")
        self._device = str(device)
        self._dtype = str(dtype)
        self._backend: Any | None = None
        self._reconstructor: Any | None = None
        self._warmed_shapes: set[tuple[int, int]] = set()

    def _ensure_reconstructor(self, device: str, dtype: str) -> Any:
        if dtype not in _SUPPORTED_DTYPES:
            raise ValueError(f"Unsupported dtype: {dtype}")
        # device/dtype 变化 -> 重建实例（连带丢弃其缓存与 warm 状态）。
        if self._reconstructor is None or str(device) != self._device or str(dtype) != self._dtype:
            self._device = str(device)
            self._dtype = str(dtype)
            self._backend = _load_backend()
            self._reconstructor = _create_in_memory_reconstructor(
                self._backend, device=self._device, dtype=self._dtype
            )
            self._warmed_shapes.clear()
        return self._reconstructor

    def reconstruct(
        self,
        stack: np.ndarray,
        *,
        wavelength_nm: int,
        otf_path: str,
        background_path: str = "",
        device: str | None = None,
        dtype: str | None = None,
        wiener: float = 2.0,
        pixel_size_nm: float = 65.0,
        excitation_na: float = 1.49,
        theta_ratio: tuple[int, int, int] = (1, 1, 1),
        recon_group_batch: int = 1,
        use_saved_params: bool = False,
        estimated_params_path: str = "",
        saved_params_fallback: str = "fail",
    ) -> dict[str, Any]:
        """对内存栈做一次重建，复用常驻实例，返回 ``{"reconstruction", "metadata", ...}``。"""
        device = self._device if device is None else str(device)
        dtype = self._dtype if dtype is None else str(dtype)
        stack_arr = _validate_stack(stack)
        resolved_otf = _validate_file_path(otf_path, "OTF")
        resolved_bg = _validate_file_path(background_path, "Background") if background_path else ""
        resolved_params = ""
        if use_saved_params:
            resolved_params = _validate_file_path(estimated_params_path, "Saved parameter")
        _validate_common_inputs(
            theta_ratio=theta_ratio,
            recon_group_batch=recon_group_batch,
            saved_params_fallback=saved_params_fallback,
        )

        reconstructor = self._ensure_reconstructor(device, dtype)
        backend = self._backend
        opts = _build_options(
            backend,
            wavelength_nm=wavelength_nm,
            otf_path=resolved_otf,
            background_path=resolved_bg,
            device=device,
            dtype=dtype,
            wiener=wiener,
            pixel_size_nm=pixel_size_nm,
            excitation_na=excitation_na,
            theta_ratio=theta_ratio,
            recon_group_batch=recon_group_batch,
            use_saved_params=use_saved_params,
            estimated_params_path=resolved_params,
        )

        shape = (int(stack_arr.shape[1]), int(stack_arr.shape[2]))
        warm_cache_hit = shape in self._warmed_shapes
        result = reconstructor.reconstruct_stack(
            stack_arr, opts, saved_params_fallback=saved_params_fallback
        )
        self._warmed_shapes.add(shape)

        reconstruction = np.asarray(result["reconstruction"], dtype=np.float32)
        metadata = _metadata_from_result(
            result=result,
            stack=stack_arr,
            reconstruction=reconstruction,
            wavelength_nm=wavelength_nm,
            otf_path=resolved_otf,
            background_path=resolved_bg,
            device=device,
            dtype=dtype,
            warm_cache_hit=warm_cache_hit,
        )
        result["reconstruction"] = reconstruction
        result["metadata"] = metadata
        return result

    def warmup_environment(self) -> bool:
        """轻量预热：仅 import 后端并尽力初始化 CUDA 上下文，不跑真实重建。

        覆盖与形状无关的一次性成本（import torch/EMD、CUDA context、分配器起步）。
        任何失败都视为非致命，返回 ``False``。
        """
        try:
            reconstructor = self._ensure_reconstructor(self._device, self._dtype)
            backend = _load_backend()  # 幂等、返回已缓存后端模块（Any 类型，避免 None 收窄告警）
            try:
                backend.torch.zeros(1, device=reconstructor.device, dtype=reconstructor.real_dtype)
            except Exception:  # CUDA 不可用等：import 已完成即算部分预热。
                logger.debug("warmup_environment: CUDA touch skipped", exc_info=True)
            return True
        except Exception:
            logger.warning("Reconstruction environment warmup skipped: backend unavailable.", exc_info=True)
            return False

    def warmup(
        self,
        *,
        wavelength_nm: int,
        otf_path: str,
        background_path: str = "",
        shape: tuple[int, int] = (128, 128),
        iters: int = 2,
        use_saved_params: bool = False,
        estimated_params_path: str = "",
        saved_params_fallback: str = "estimate",
        **recon_kwargs: Any,
    ) -> bool:
        """用合成 ``(9, H, W)`` uint16 栈跑 ``iters`` 次重建做预热（非致命）。

        预热默认把 saved-params 失败回退成 estimate，确保即使 ``.mat`` 缺失也能焐热
        与形状无关的成本；返回是否至少成功一次。
        """
        height, width = int(shape[0]), int(shape[1])
        rng = np.random.default_rng(0)
        dummy = rng.integers(0, 4096, size=(9, height, width), dtype=np.uint16)
        ok = False
        for _ in range(max(1, int(iters))):
            try:
                self.reconstruct(
                    dummy,
                    wavelength_nm=wavelength_nm,
                    otf_path=otf_path,
                    background_path=background_path,
                    use_saved_params=use_saved_params,
                    estimated_params_path=estimated_params_path,
                    saved_params_fallback=saved_params_fallback,
                    **recon_kwargs,
                )
                ok = True
            except Exception:
                logger.warning("Reconstruction warmup iteration failed (non-fatal).", exc_info=True)
        return ok


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
    use_saved_params: bool = False,
    estimated_params_path: str = "",
    saved_params_fallback: str = "fail",
    reconstructor: WarmSIMReconstructor | None = None,
) -> dict[str, Any]:
    """对内存 ``(9, H, W)`` uint16 栈跑一次 SIM9 GPU Wiener 重建（import-safe 封装）。

    无 ``reconstructor`` 时每次新建一个 ``WarmSIMReconstructor``（冷调用语义，供测试/
    基准/向后兼容）；传入常驻实例时复用其热状态（生产路径由 ``ReconstructionWorker`` 走）。
    """
    t0 = time.perf_counter()
    warm = reconstructor if reconstructor is not None else WarmSIMReconstructor(device=device, dtype=dtype)
    result = warm.reconstruct(
        stack,
        wavelength_nm=wavelength_nm,
        otf_path=otf_path,
        background_path=background_path,
        device=device,
        dtype=dtype,
        wiener=wiener,
        pixel_size_nm=pixel_size_nm,
        excitation_na=excitation_na,
        theta_ratio=theta_ratio,
        recon_group_batch=recon_group_batch,
        use_saved_params=use_saved_params,
        estimated_params_path=estimated_params_path,
        saved_params_fallback=saved_params_fallback,
    )
    result.setdefault("metadata", {}).setdefault("timings_ms", {})
    result["metadata"]["timings_ms"].setdefault("wrapper_total_ms", (time.perf_counter() - t0) * 1000.0)
    return result

from __future__ import annotations

import math
import json
import time
import warnings
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import numpy as np
import tifffile
import torch
import torch.nn.functional as F
from scipy.io import loadmat, savemat

try:
    from emd import emd as matlab_emd
except Exception:
    matlab_emd = None

from emd_fast_torch import emd_reduce_for_sim_fast

Tensor = torch.Tensor


@dataclass
class SIMWienerOptions:
    mode: str = "3beam"
    wiener: float = 2.0
    wavelength_nm: int = 488
    avg_groups: int = 20
    mu: float = 150.0
    sigma: float = 1.0
    pixel_size_nm: float = 65.0
    excitation_na: float = 1.4
    theta_ratio: Tuple[int, int, int] = (4, 4, 3)
    otf_path: str = ""
    background_path: str = ""
    output_dir: str = ""

    starframe: int = 1
    reconstruct_group_stride: int = 1
    recon_group_batch: int = 4
    notch_switch: bool = False
    notch_para_1: float = 0.05
    notch_para_2: float = 1.2
    save_c: bool = False
    rep_images: int = 10
    min_otf_size: int = 512
    fc_param_base: int = 220
    fc_angle_base: int = 120
    fc_content_base_488_561: int = 105
    fc_content_base_647: int = 80
    fc_recon_base: int = 200
    search_radius_px: int = 50
    regul: float = 2 * math.pi
    use_background: bool = True
    dtype: str = "single"
    debug: bool = False

    use_emd: bool = True
    use_regress: bool = True
    min_r2: float = 0.10
    fail_on_low_r2: bool = True
    warn_on_low_r2: bool = True
    fill_missing_c6: bool = False
    default_c6: float = 0.5
    default_angle: float = 0.0

    device: Optional[str] = None
    save_pseudo_tirf: bool = True
    save_param_mat: bool = True
    save_param_npy: bool = True
    use_saved_params: bool = False
    estimated_params_path: str = ""
    output_dtype: str = "float32"

    debug_save: bool = False
    debug_dir: str = ""
    debug_max_recon_groups: int = 1

    profile_timing: bool = True
    save_timing_json: bool = False
    timing_json_path: str = ""

    raw_path: str = ""
    nphases: int = field(default=3, init=False)
    nangles: int = field(default=3, init=False)

    def normalize(self, raw_path: str) -> "SIMWienerOptions":
        cfg = SIMWienerOptions(
            **{
                k: v
                for k, v in asdict(self).items()
                if k not in {"nphases", "nangles", "raw_path"}
            }
        )
        cfg.raw_path = str(raw_path)
        if cfg.mode == "3beam":
            cfg.nphases = 3
            cfg.nangles = 3
        elif cfg.mode == "2beam":
            cfg.nphases = 3
            cfg.nangles = 2
        else:
            raise ValueError(f"Unsupported mode: {cfg.mode}")
        if not cfg.output_dir:
            cfg.output_dir = str(Path(cfg.raw_path).parent / "submit")
        if not cfg.otf_path:
            cfg.otf_path = str(_resolve_default_otf(cfg.wavelength_nm, Path(cfg.raw_path).parent))
        if cfg.background_path is None:
            cfg.background_path = ""
        if not cfg.debug_dir:
            cfg.debug_dir = str(Path(cfg.raw_path).parent / "debug_compare_py")
        if not cfg.timing_json_path:
            cfg.timing_json_path = str(Path(cfg.output_dir) / "timing_summary.json")
        if not cfg.estimated_params_path:
            cfg.estimated_params_path = str(Path(cfg.raw_path).with_name(Path(cfg.raw_path).stem + "_estimated_params.mat"))
        return cfg


def hessian_sim_wiener_default_options() -> SIMWienerOptions:
    return SIMWienerOptions()


def _resolve_default_otf(wavelength_nm: int, base_dir: Union[str, Path]) -> Path:
    base_dir = Path(base_dir)
    mapping = {488: "488OTF_512.tif", 561: "561OTF_512.tif", 638: "638OTF_512.tif", 647: "647OTF_512.tif"}
    if wavelength_nm not in mapping:
        raise ValueError(f"No default OTF for wavelength {wavelength_nm}. Please set opts.otf_path explicitly.")
    name = mapping[wavelength_nm]
    p1 = base_dir / name
    if p1.exists():
        return p1
    p2 = Path(name)
    if p2.exists():
        return p2.resolve()
    raise FileNotFoundError(f"Could not resolve default OTF file: {name}")


def _torch_dtype_from_name(name: str) -> torch.dtype:
    name = str(name).lower()
    if name in {"single", "float32", "fp32"}:
        return torch.float32
    if name in {"double", "float64", "fp64"}:
        return torch.float64
    raise ValueError(f"Unsupported dtype: {name}")


def _complex_dtype(real_dtype: torch.dtype) -> torch.dtype:
    if real_dtype == torch.float32:
        return torch.complex64
    if real_dtype == torch.float64:
        return torch.complex128
    raise ValueError(f"Unsupported real dtype: {real_dtype}")


def _to_numpy(x):
    if isinstance(x, torch.Tensor):
        return x.detach().cpu().numpy()
    return np.asarray(x)


def _ensure_dir(path: Union[str, Path]) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)


def _prepare_output_dirs(output_dir: Union[str, Path], raw_path: Union[str, Path]) -> Dict[str, str]:
    output_dir = Path(output_dir)
    raw_path = Path(raw_path)
    base_name = raw_path.name
    base_stem = raw_path.stem
    paths = {
        "root": str(output_dir),
        "pseudo_dir": str(output_dir / "Pseudo-TIRF"),
        "wiener_dir": str(output_dir / "SIM-Wiener"),
        "pseudo_tirf_path": str(output_dir / "Pseudo-TIRF" / f"TIRF_{base_name}"),
        "wiener_path": str(output_dir / "SIM-Wiener" / f"re-{base_name}"),
        "param_prefix": str(raw_path.parent / base_stem),
    }
    for key in ("root", "pseudo_dir", "wiener_dir"):
        _ensure_dir(paths[key])
    return paths


def _read_stack_info(path: Union[str, Path]) -> Dict[str, int]:
    with tifffile.TiffFile(path) as tif:
        num_frames = len(tif.pages)
        if num_frames == 0:
            raise ValueError(f"Empty TIFF stack: {path}")
        h, w = tif.pages[0].shape
    return {"num_frames": num_frames, "height": h, "width": w}


def _read_stack(path: Union[str, Path]) -> np.ndarray:
    arr = tifffile.imread(path)
    if arr.ndim == 2:
        arr = arr[None, ...]
    elif arr.ndim != 3:
        raise ValueError(f"Expected 2D or 3D TIFF stack, got shape {arr.shape}")
    return np.asarray(arr)


def _read_partial_stack(path: Union[str, Path], start_frame_1b: int, count: int) -> np.ndarray:
    start = int(start_frame_1b) - 1
    stop = start + int(count)
    with tifffile.TiffFile(path) as tif:
        data = tif.asarray(key=range(start, stop))
    if data.ndim == 2:
        data = data[None, ...]
    return np.asarray(data)


def _write_tiff(path, arr):
    path = Path(path)
    _ensure_dir(path.parent)
    arr = np.asarray(arr, dtype=np.float32)
    if arr.ndim == 2:
        tifffile.imwrite(path, arr, photometric="minisblack")
    elif arr.ndim == 3:
        tifffile.imwrite(path, arr, photometric="minisblack", imagej=False, metadata=None)
    else:
        raise ValueError(f"Expected 2D or 3D array, got shape {arr.shape}")


def _center_embed(img: Tensor, out_hw: Tuple[int, int]) -> Tensor:
    if img.ndim == 2:
        img = img.unsqueeze(0)
        squeeze_back = True
    else:
        squeeze_back = False
    n_img, in_h, in_w = img.shape
    out_h, out_w = out_hw
    start_h = math.ceil((out_h - in_h) / 2)
    start_w = math.ceil((out_w - in_w) / 2)
    out = torch.zeros((n_img, out_h, out_w), dtype=img.dtype, device=img.device)
    out[:, start_h:start_h + in_h, start_w:start_w + in_w] = img
    return out.squeeze(0) if squeeze_back else out


def _crop_center(img: Tensor, out_hw: Tuple[int, int]) -> Tensor:
    if img.ndim == 2:
        img = img.unsqueeze(0)
        squeeze_back = True
    else:
        squeeze_back = False
    _, in_h, in_w = img.shape
    out_h, out_w = out_hw
    start_h = math.ceil((in_h - out_h) / 2)
    start_w = math.ceil((in_w - out_w) / 2)
    out = img[:, start_h:start_h + out_h, start_w:start_w + out_w]
    return out.squeeze(0) if squeeze_back else out


def _resize2d(img: Tensor, out_hw: Tuple[int, int]) -> Tensor:
    x = img.unsqueeze(0).unsqueeze(0)
    y = F.interpolate(x, size=out_hw, mode="bilinear", align_corners=False)
    return y[0, 0]


def _sigmoid(x: Tensor) -> Tensor:
    return torch.sigmoid(x)


def _centered_fft2(x: Tensor) -> Tensor:
    return torch.fft.fftshift(torch.fft.fft2(torch.fft.ifftshift(x, dim=(-2, -1))), dim=(-2, -1))


def _centered_ifft2(x: Tensor) -> Tensor:
    return torch.fft.fftshift(torch.fft.ifft2(torch.fft.ifftshift(x, dim=(-2, -1))), dim=(-2, -1))


def _full_fft_corr(a: Tensor, b: Tensor) -> Tensor:
    out_h = a.shape[-2] + b.shape[-2] - 1
    out_w = a.shape[-1] + b.shape[-1] - 1
    fa = torch.fft.fft2(a, s=(out_h, out_w))
    fb = torch.fft.fft2(b, s=(out_h, out_w))
    return torch.fft.ifft2(fa * fb).real


def _phase_matrix(theta_ratio: Iterable[int], regul: float, device: torch.device, complex_dtype: torch.dtype) -> Tensor:
    real_dtype = torch.float64 if complex_dtype == torch.complex128 else torch.float32
    r = torch.tensor(list(theta_ratio), device=device, dtype=real_dtype)
    phi1 = regul * (r[0] / r.sum())
    phi2 = regul * ((r[0] + r[1]) / r.sum())
    one = torch.ones((), device=device, dtype=real_dtype)
    e_phi1 = torch.polar(one.expand(()), phi1).to(complex_dtype)
    e_mphi1 = torch.polar(one.expand(()), -phi1).to(complex_dtype)
    e_phi2 = torch.polar(one.expand(()), phi2).to(complex_dtype)
    e_mphi2 = torch.polar(one.expand(()), -phi2).to(complex_dtype)
    return torch.tensor(
        [[1 + 0j, 1 + 0j, 1 + 0j],
         [e_phi1, 1 + 0j, e_mphi1],
         [e_phi2, 1 + 0j, e_mphi2]],
        device=device,
        dtype=complex_dtype
    )


def _phase_list(theta_ratio: Iterable[int], regul: float, dphi: float, dtype: torch.dtype = torch.float32) -> Tensor:
    ratios = list(theta_ratio)
    total = int(sum(ratios))
    t = torch.linspace(dphi, regul + dphi, total + 1, dtype=dtype)
    return torch.stack([t[0], t[ratios[0]], t[ratios[0] + ratios[1]]])


def _sigmoid_mask(hw: Tuple[int, int], sig: float, device: torch.device, dtype: torch.dtype) -> Tensor:
    h, w = hw
    x = torch.arange(1, w + 1, device=device, dtype=dtype)
    y = torch.arange(1, h + 1, device=device, dtype=dtype)
    mask_x = _sigmoid(sig * x) - _sigmoid(sig * (x - (w + 1)))
    mask_y = _sigmoid(sig * y) - _sigmoid(sig * (y - (h + 1)))
    return mask_y[:, None] * mask_x[None, :]


def _matlab_max_position_1b(x: Tensor) -> Tuple[float, float]:
    h, w = x.shape
    x_col_major = x.t().contiguous().view(-1)
    idx = int(torch.argmax(x_col_major).item())
    row = (idx % h) + 1
    col = (idx // h) + 1
    return float(row), float(col)


def _histc_matlab(x: Tensor, edges: Tensor) -> Tensor:
    x = x.reshape(-1)
    edges = edges.reshape(-1)
    counts = torch.zeros(edges.numel(), device=x.device, dtype=x.dtype)
    for i in range(edges.numel() - 1):
        left = edges[i]
        right = edges[i + 1]
        if i < edges.numel() - 2:
            mask = (x >= left) & (x < right)
        else:
            mask = (x >= left) & (x <= right)
        counts[i] = mask.sum()
    return counts


def _sideband_center_index_0(spi_1b: int, nphases: int) -> int:
    return int(math.ceil(spi_1b / 2) * nphases - 2)


def _sideband_order_index_0(spi_1b: int) -> int:
    return int(math.ceil(spi_1b / 2) + 2 * math.floor(spi_1b / 2) - 1)


def _coord_index_0(spi_1b: int) -> int:
    return spi_1b + (spi_1b - 1) // 2


def _pair_pos_index_0(spi_1b: int) -> int:
    return spi_1b + (spi_1b - 1) // 2


def _pair_neg_index_0(spi_1b: int) -> int:
    return spi_1b + 1 + (spi_1b - 1) // 2


class SIMWienerGPUReconstructor:
    def __init__(self, device: Optional[Union[str, torch.device]] = None, dtype: Union[str, torch.dtype] = torch.float32):
        self.real_dtype = _torch_dtype_from_name(dtype) if isinstance(dtype, str) else dtype
        self.device = torch.device(device if device is not None else ("cuda" if torch.cuda.is_available() else "cpu"))
        self.complex_dtype = _complex_dtype(self.real_dtype)
        self.eps = 1e-8
        self._grid_cache = {}
        self.cfg = None
        self.timings_ms: Dict[str, float] = {}

    def _np_dtype(self):
        return np.float64 if self.real_dtype == torch.float64 else np.float32

    def _dbg_save(self, name: str, payload: Dict[str, Any]):
        if self.cfg is None or not self.cfg.debug_save:
            return
        out_dir = Path(self.cfg.debug_dir)
        _ensure_dir(out_dir)
        to_save = {}
        for k, v in payload.items():
            if isinstance(v, (str, bytes)):
                continue
            try:
                to_save[k] = _to_numpy(v)
            except Exception:
                pass
        savemat(str(out_dir / f"{name}.mat"), to_save, do_compression=True)

    def _sync_cuda(self):
        if self.device.type == "cuda":
            torch.cuda.synchronize(self.device)

    def _now(self) -> float:
        self._sync_cuda()
        return time.perf_counter()

    def _ms(self, t0: float, t1: float) -> float:
        return (t1 - t0) * 1000.0

    def _record_timing(self, name: str, value_ms: float):
        self.timings_ms[name] = float(value_ms)

    def _finalize_timings(self, cfg):
        if getattr(cfg, "profile_timing", False):
            print("\n===== Timing Summary (ms) =====")
            for k, v in self.timings_ms.items():
                print(f"{k}: {v:.3f}")
        if getattr(cfg, "save_timing_json", False):
            path = Path(cfg.timing_json_path)
            _ensure_dir(path.parent)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.timings_ms, f, ensure_ascii=False, indent=2)

    def reconstruct(self, raw_path: Union[str, Path], opts: Optional[Union[SIMWienerOptions, Dict[str, Any]]] = None) -> \
    Dict[str, Any]:
        t_total0 = self._now()
        self.timings_ms = {}

        if opts is None:
            opts = hessian_sim_wiener_default_options()
        elif isinstance(opts, dict):
            opts = SIMWienerOptions(**opts)

        cfg = opts.normalize(str(raw_path))
        self.cfg = cfg
        if cfg.device:
            self.device = torch.device(cfg.device)
        self.real_dtype = _torch_dtype_from_name(cfg.dtype)
        self.complex_dtype = _complex_dtype(self.real_dtype)

        try:
            t0 = self._now()
            raw_info = _read_stack_info(cfg.raw_path)
            paths = _prepare_output_dirs(cfg.output_dir, cfg.raw_path)
            if cfg.debug_save:
                _ensure_dir(cfg.debug_dir)
            t1 = self._now()
            self._record_timing("step1_io_prepare_ms", self._ms(t0, t1))

            # ===== 一次性载入到 GPU =====
            t0 = self._now()
            raw_gpu = self._load_stack_to_gpu(cfg.raw_path)
            otf_template = self._load_2d_tiff(cfg.otf_path)
            background = self._load_2d_tiff(cfg.background_path) if cfg.background_path else None
            t1 = self._now()
            self._record_timing("step1b_preload_raw_stack_to_gpu_ms", self._ms(t0, t1))

            t0 = self._now()
            if cfg.save_pseudo_tirf:
                self._save_pseudo_tirf_from_gpu(raw_gpu, cfg, raw_info, paths["pseudo_tirf_path"])
            self._dbg_save(
                "step2_inputs",
                {
                    "otfTemplate": otf_template,
                    "background": background if background is not None else np.array([]),
                },
            )
            t1 = self._now()
            self._record_timing("step2_inputs_prepare_ms", self._ms(t0, t1))

            if cfg.use_saved_params:
                t0 = self._now()
                param, saved_estimates = self._load_estimated_params_file(cfg.estimated_params_path)
                paths["estimated_params_mat"] = str(cfg.estimated_params_path)
                t1 = self._now()
                self._record_timing("step3_load_saved_params_ms", self._ms(t0, t1))
            else:
                param = self._estimate_parameters(cfg, raw_info, raw_gpu, otf_template, paths["param_prefix"])
                saved_estimates = None

            if saved_estimates is None:
                recon, c6, angle6, r2_angles = self._wiener_reconstruct(
                    cfg, raw_info, raw_gpu, otf_template, background, param
                )
            else:
                recon, c6, angle6, r2_angles = self._wiener_reconstruct_with_saved_params(
                    cfg, raw_info, raw_gpu, otf_template, background, param, saved_estimates
                )
            if not cfg.use_saved_params:
                paths["estimated_params_mat"] = self._save_estimated_params_file(
                    paths["param_prefix"], param, c6, angle6, r2_angles, cfg
                )

            t0 = self._now()
            out_arr = _to_numpy(recon).astype(np.float32)
            _write_tiff(paths["wiener_path"], out_arr)
            t1 = self._now()
            self._record_timing("step5_save_output_ms", self._ms(t0, t1))

            self._record_timing("total_pipeline_ms", self._ms(t_total0, self._now()))
            self._finalize_timings(cfg)

            return {
                "cfg": cfg,
                "raw_info": raw_info,
                "param_estimation": {
                    "c6": _to_numpy(c6),
                    "angle6": _to_numpy(angle6),
                    "R2_angles": _to_numpy(r2_angles),
                },
                "reconstruction": out_arr,
                "paths": paths,
                "timings_ms": dict(self.timings_ms),
            }

        except Exception:
            self._record_timing("total_pipeline_ms", self._ms(t_total0, self._now()))
            self._finalize_timings(cfg)
            raise

    def _load_2d_tiff(self, path):
        arr = _read_stack(path)
        if arr.ndim == 3:
            arr = arr[0]
        return torch.as_tensor(arr, device=self.device, dtype=self.real_dtype)

    def _load_stack_to_gpu(self, path):
        arr = _read_stack(path).astype(self._np_dtype(), copy=False)
        return torch.as_tensor(arr, device=self.device, dtype=self.real_dtype)

    def _save_pseudo_tirf_from_gpu(self, raw_gpu: Tensor, cfg, raw_info, out_path):
        num_modes = cfg.nphases * cfg.nangles
        start0 = cfg.starframe - 1
        available = raw_info["num_frames"] - start0
        num_groups = available // num_modes
        if num_groups <= 0:
            return

        raw_valid = raw_gpu[start0:start0 + num_groups * num_modes]  # [G*K, H, W]
        raw_valid = raw_valid.view(num_groups, num_modes, raw_valid.shape[-2], raw_valid.shape[-1])
        pseudo = raw_valid.mean(dim=1)  # [G, H, W]

        _write_tiff(out_path, _to_numpy(pseudo).astype(np.float32))

    def _average_groups_from_gpu(self, raw_gpu: Tensor, nphases, nangles, avg_groups, starframe):
        num_modes = nphases * nangles
        start0 = starframe - 1
        count = num_modes * avg_groups

        raw_t = raw_gpu[start0:start0 + count]  # already on GPU
        if raw_t.shape[0] < count:
            raise ValueError(
                f"Not enough frames for averaging: need {count}, got {raw_t.shape[0]}"
            )

        raw_t = raw_t.view(avg_groups, num_modes, raw_t.shape[-2], raw_t.shape[-1])
        return raw_t.mean(dim=0)

    def _save_param_files(self, param, prefix, cfg):
        return

    def _save_estimated_params_file(self, prefix, param, c6, angle6, r2_angles, cfg):
        prefix = Path(prefix)
        zuobiaox = _to_numpy(param["zuobiaox"]).astype(np.float64)
        zuobiaoy = _to_numpy(param["zuobiaoy"]).astype(np.float64)
        c6_np = _to_numpy(c6).astype(np.float64)
        angle6_np = _to_numpy(angle6).astype(np.float64)
        r2_np = _to_numpy(r2_angles).astype(np.float64)
        out_path = str(prefix) + "_estimated_params.mat"
        savemat(
            out_path,
            {
                "zuobiaox": zuobiaox,
                "zuobiaoy": zuobiaoy,
                "c6": c6_np,
                "angle6": angle6_np,
                "R2_angles": r2_np,
                "n": float(_to_numpy(param["n"])),
                "pg": float(_to_numpy(param["pg"])),
                "fc": float(_to_numpy(param["fc"])),
                "fanwei": float(_to_numpy(param["fanwei"])),
            },
            do_compression=True,
        )
        return out_path

    def _load_estimated_params_file(self, path):
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Saved SIM-Wiener parameter file not found: {path}")

        mat = loadmat(str(path))
        required = ("zuobiaox", "zuobiaoy", "c6", "angle6", "R2_angles", "n")
        missing = [k for k in required if k not in mat]
        if missing:
            raise KeyError(f"Saved parameter file is missing required field(s): {missing}")

        def tensor_1d(name):
            return torch.as_tensor(np.asarray(mat[name]).squeeze(), device=self.device, dtype=self.real_dtype).reshape(-1)

        def scalar(name, default=0.0):
            if name not in mat:
                return float(default)
            return float(np.asarray(mat[name]).squeeze())

        param = {
            "n": torch.tensor(scalar("n"), device=self.device, dtype=self.real_dtype),
            "zuobiaox": tensor_1d("zuobiaox"),
            "zuobiaoy": tensor_1d("zuobiaoy"),
            "pg": torch.tensor(scalar("pg"), device=self.device, dtype=self.real_dtype),
            "fc": torch.tensor(scalar("fc"), device=self.device, dtype=self.real_dtype),
            "fanwei": torch.tensor(scalar("fanwei"), device=self.device, dtype=self.real_dtype),
        }
        saved_estimates = {
            "c6": tensor_1d("c6"),
            "angle6": tensor_1d("angle6"),
            "R2_angles": tensor_1d("R2_angles"),
        }
        return param, saved_estimates

    def _get_xy_grid(self, hw, dtype):
        key = (hw[0], hw[1], dtype, str(self.device))
        if key not in self._grid_cache:
            h, w = hw
            y = torch.arange(0, h, device=self.device, dtype=dtype)
            x = torch.arange(0, w, device=self.device, dtype=dtype)
            yy, xx = torch.meshgrid(y, x, indexing="ij")
            self._grid_cache[key] = (xx, yy)
        return self._grid_cache[key]

    def _phase_ramp(self, hw, kx, ky):
        xx, yy = self._get_xy_grid(hw, self.real_dtype)
        phase = kx * xx + ky * yy
        return torch.polar(torch.ones_like(phase), phase).to(self.complex_dtype)

    def _shift_centered_spectrum_from_spatial(self, spatial_centered, kx, ky):
        ramp = self._phase_ramp((spatial_centered.shape[-2], spatial_centered.shape[-1]), kx, ky)
        return _centered_fft2(spatial_centered * ramp)

    def _center_embed_batch(self, img: Tensor, out_hw: Tuple[int, int]) -> Tensor:
        """
        img: [B, H, W]
        out: [B, out_h, out_w]
        """
        if img.ndim != 3:
            raise ValueError(f"_center_embed_batch expects [B,H,W], got shape {tuple(img.shape)}")
        b, in_h, in_w = img.shape
        out_h, out_w = out_hw
        start_h = math.ceil((out_h - in_h) / 2)
        start_w = math.ceil((out_w - in_w) / 2)
        out = torch.zeros((b, out_h, out_w), dtype=img.dtype, device=img.device)
        out[:, start_h:start_h + in_h, start_w:start_w + in_w] = img
        return out

    def _phase_ramp_batch(self, hw: Tuple[int, int], kx_vec: Tensor, ky_vec: Tensor) -> Tensor:
        """
        kx_vec, ky_vec: [B]
        return: [B, H, W]
        """
        if kx_vec.ndim != 1 or ky_vec.ndim != 1:
            raise ValueError("kx_vec and ky_vec must be 1D tensors")
        if kx_vec.numel() != ky_vec.numel():
            raise ValueError("kx_vec and ky_vec must have the same length")

        xx, yy = self._get_xy_grid(hw, self.real_dtype)  # [H, W]
        phase = kx_vec[:, None, None] * xx[None, :, :] + ky_vec[:, None, None] * yy[None, :, :]
        return torch.polar(torch.ones_like(phase), phase).to(self.complex_dtype)

    def _shift_centered_spectrum_from_spatial_batch(self, spatial_centered: Tensor, kx_vec: Tensor,
                                                    ky_vec: Tensor) -> Tensor:
        """
        spatial_centered: [B, H, W]
        return: [B, H, W]
        """
        if spatial_centered.ndim != 3:
            raise ValueError(
                f"_shift_centered_spectrum_from_spatial_batch expects [B,H,W], got shape {tuple(spatial_centered.shape)}"
            )
        ramps = self._phase_ramp_batch(
            (spatial_centered.shape[-2], spatial_centered.shape[-1]),
            kx_vec.to(self.real_dtype),
            ky_vec.to(self.real_dtype),
        )
        return _centered_fft2(spatial_centered * ramps)

    def _overlap_score_from_spatial(self, kx, ky, h1_spatial, h_spatial, h1_big, h_big, sp_side_spatial, sp_center):
        replc_h = self._shift_centered_spectrum_from_spatial(h1_spatial, kx, ky)
        replc_h = (replc_h.abs() > 0.9).to(self.real_dtype).to(self.complex_dtype)
        replch = self._shift_centered_spectrum_from_spatial(h_spatial, kx, ky) * replc_h
        replct = self._shift_centered_spectrum_from_spatial(sp_side_spatial, kx, ky)
        youhua = replct * replc_h * h_big
        he = torch.abs(torch.sum(torch.conj(youhua) * (sp_center * replch)))
        cishu = torch.sum(h1_big * replc_h).real
        return float((he / torch.clamp(cishu, min=self.eps)).real.item())

    def _estimate_parameters(self, cfg, raw_info, raw_gpu, otf_template, param_prefix):
        t_ep0 = self._now()
        num_modes = cfg.nphases * cfg.nangles

        t0 = self._now()
        fd = self._average_groups_from_gpu(raw_gpu, cfg.nphases, cfg.nangles, cfg.avg_groups, cfg.starframe)
        t1 = self._now()
        self._record_timing("step3a_average_groups_ms", self._ms(t0, t1))

        t0 = self._now()
        h0 = _resize2d(otf_template, (cfg.min_otf_size, cfg.min_otf_size))
        n = int(max(raw_info["height"], raw_info["width"], cfg.min_otf_size))
        if n > cfg.min_otf_size:
            h0 = _resize2d(h0, (n, n))
        pg = math.ceil((512 * 2 * cfg.excitation_na * cfg.pixel_size_nm / cfg.wavelength_nm) * (n / 512))
        fanwei = math.ceil(cfg.search_radius_px * (n / 512))
        fc = math.ceil(cfg.fc_param_base * (n / 512))
        phase_matrix = _phase_matrix(cfg.theta_ratio, cfg.regul, self.device, self.complex_dtype)
        t1 = self._now()
        self._record_timing("step3b_prepare_otf_ms", self._ms(t0, t1))

        t0 = self._now()
        inv_phase_matrix = torch.linalg.inv(phase_matrix)
        freq = torch.arange(-n / 2, n / 2, device=self.device, dtype=self.real_dtype)
        ky, kx = torch.meshgrid(freq, freq, indexing="ij")
        kr = torch.sqrt(kx ** 2 + ky ** 2)
        h = h0.abs().clone()
        h[kr > fc] = 0
        h1 = (h != 0).to(self.real_dtype)
        fd_pad = _center_embed(fd, (n, n))
        dibars = _centered_fft2(fd_pad) * h1.unsqueeze(0).to(self.complex_dtype)
        di_reshaped = dibars.view(cfg.nangles, cfg.nphases, n, n)
        sp = torch.einsum("jk,akxy->ajxy", inv_phase_matrix, di_reshaped).reshape(num_modes, n, n)
        sp = sp / (sp.abs() + self.eps)
        sp = sp * h1.unsqueeze(0).to(self.complex_dtype)
        h_big = _center_embed(h, (2 * n, 2 * n)).to(self.complex_dtype)
        h1_big = _center_embed(h1, (2 * n, 2 * n)).to(self.complex_dtype)
        h_big_spatial = _centered_ifft2(h_big)
        h1_big_spatial = _centered_ifft2(h1_big)
        self._dbg_save("param_step_global", {
            "fd": fd,
            "H": h,
            "H1": h1,
            "fdPad": fd_pad,
            "DIbars": dibars,
            "phase_matrix": phase_matrix,
            "inv_phase_matrix": inv_phase_matrix,
            "sp": sp
        })
        t1 = self._now()
        self._record_timing("step3c_phase_separation_ms", self._ms(t0, t1))

        t0 = self._now()
        zuobiaox = torch.full((num_modes,), float(n), device=self.device, dtype=self.real_dtype)
        zuobiaoy = torch.full((num_modes,), float(n), device=self.device, dtype=self.real_dtype)
        h_rev = torch.flip(h1, dims=(0, 1))
        cishu = _full_fft_corr(h1, h_rev)
        ci = (cishu >= 0.9).to(self.real_dtype)
        freq2 = torch.arange(-n + 1, n, device=self.device, dtype=self.real_dtype)
        ky2, kx2 = torch.meshgrid(freq2, freq2, indexing="ij")
        kr2 = torch.sqrt(kx2 ** 2 + ky2 ** 2)
        for spi in range(1, cfg.nangles * (cfg.nphases - 1) + 1, 2):
            center_idx = _sideband_center_index_0(spi, cfg.nphases)
            side_idx = _sideband_order_index_0(spi)
            sp_center = sp[center_idx]
            sp_side = sp[side_idx]
            sp_tmp = torch.flip(torch.conj(sp_side), dims=(0, 1))
            corr_map = _full_fft_corr(sp_center, sp_tmp) * ci
            lihe = torch.abs(corr_map / (cishu + self.eps))
            lihe = torch.where(
                (kr2 >= (pg - fanwei)) & (kr2 <= (pg + fanwei)),
                lihe,
                torch.zeros_like(lihe)
            )
            maxx, maxy = _matlab_max_position_1b(lihe)
            maxx_init, maxy_init = maxx, maxy
            sp_side_big = _center_embed(sp_side, (2 * n, 2 * n))
            sp_center_big = _center_embed(sp_center, (2 * n, 2 * n))
            sp_side_big_spatial = _centered_ifft2(sp_side_big)
            kyc = 2 * math.pi * (maxx - n) / (2 * n)
            kxc = 2 * math.pi * (maxy - n) / (2 * n)
            he = self._overlap_score_from_spatial(kxc, kyc, h1_big_spatial, h_big_spatial, h1_big, h_big, sp_side_big_spatial, sp_center_big)
            test1 = self._overlap_score_from_spatial(2 * math.pi * ((maxy - 1e-5) - n) / (2 * n), kyc, h1_big_spatial, h_big_spatial, h1_big, h_big, sp_side_big_spatial, sp_center_big)
            test2 = self._overlap_score_from_spatial(2 * math.pi * ((maxy + 1e-5) - n) / (2 * n), kyc, h1_big_spatial, h_big_spatial, h1_big, h_big, sp_side_big_spatial, sp_center_big)
            test3 = self._overlap_score_from_spatial(kxc, 2 * math.pi * ((maxx - 1e-5) - n) / (2 * n), h1_big_spatial, h_big_spatial, h1_big, h_big, sp_side_big_spatial, sp_center_big)
            test4 = self._overlap_score_from_spatial(kxc, 2 * math.pi * ((maxx + 1e-5) - n) / (2 * n), h1_big_spatial, h_big_spatial, h1_big, h_big, sp_side_big_spatial, sp_center_big)
            flag_maxy = -1.0 if test1 > test2 else 1.0
            flag_maxx = -1.0 if test3 > test4 else 1.0
            buchangx = 1.0
            buchangy = 1.0
            while (buchangx > 1e-4) or (buchangy > 1e-4):
                while buchangx > 1e-4:
                    candx = maxx + flag_maxx * buchangx
                    score = self._overlap_score_from_spatial(
                        2 * math.pi * (maxy - n) / (2 * n),
                        2 * math.pi * (candx - n) / (2 * n),
                        h1_big_spatial, h_big_spatial, h1_big, h_big, sp_side_big_spatial, sp_center_big
                    )
                    if score <= he:
                        buchangx *= 0.5
                    else:
                        he = score
                        maxx = candx
                        break
                while buchangy > 1e-4:
                    candy = maxy + flag_maxy * buchangy
                    score = self._overlap_score_from_spatial(
                        2 * math.pi * (candy - n) / (2 * n),
                        2 * math.pi * (maxx - n) / (2 * n),
                        h1_big_spatial, h_big_spatial, h1_big, h_big, sp_side_big_spatial, sp_center_big
                    )
                    if score <= he:
                        buchangy *= 0.5
                    else:
                        he = score
                        maxy = candy
                        break
            pos_idx = _pair_pos_index_0(spi)
            neg_idx = _pair_neg_index_0(spi)
            zuobiaox[pos_idx] = maxx
            zuobiaox[neg_idx] = 2 * n - maxx
            zuobiaoy[pos_idx] = maxy
            zuobiaoy[neg_idx] = 2 * n - maxy
            self._dbg_save(f"param_spi_{spi:02d}", {
                "spi": spi,
                "sp_center": sp_center,
                "sp_side": sp_side,
                "cishu": cishu,
                "ci": ci,
                "corrMap": corr_map,
                "lihe": lihe,
                "maxx_init": maxx_init,
                "maxy_init": maxy_init,
                "maxx_final": maxx,
                "maxy_final": maxy,
                "he_final": he
            })
        param = {
            "n": torch.tensor(n, device=self.device),
            "zuobiaox": zuobiaox,
            "zuobiaoy": zuobiaoy,
            "phase_matrix": phase_matrix,
            "pg": torch.tensor(pg, device=self.device),
            "fc": torch.tensor(fc, device=self.device),
            "fanwei": torch.tensor(fanwei, device=self.device)
        }
        self._save_param_files(param, param_prefix, cfg)
        self._dbg_save("param_final", param)
        t1 = self._now()
        self._record_timing("step3d_sideband_search_refine_ms", self._ms(t0, t1))
        self._record_timing("step3_parameter_estimation_total_ms", self._ms(t_ep0, t1))
        return param

    def _hist_reduce(self, hist: Tensor, start_idx: int) -> Tensor:
        if hist.numel() == 0:
            return hist
        if matlab_emd is None:
            raise ImportError("matlab_emd unavailable. Put emd.py or emd_matlab_compatible.py next to this file.")
        x = hist.detach().cpu().numpy().astype(np.float64, copy=False)
        imfs, ort, nbits = matlab_emd(x)
        self._dbg_save(f"emd_reduce_start_{start_idx}", {"hist_in": x, "imfs": imfs, "ort": ort, "nbits": nbits})
        if imfs.size == 0:
            y = x
        elif imfs.shape[0] >= start_idx:
            y = imfs[start_idx - 1:, :].sum(axis=0)
        else:
            y = x
        self._dbg_save(f"emd_reduce_start_{start_idx}_out", {"hist_out": y})
        return torch.as_tensor(y, device=hist.device, dtype=hist.dtype)

    def _regress_no_intercept_details(self, x: Tensor, y: Tensor) -> Dict[str, Tensor]:
        x = x.to(self.complex_dtype)
        y = y.to(self.complex_dtype)

        denom = torch.sum(torch.conj(x) * x)
        if torch.abs(denom) <= self.eps:
            beta = torch.tensor(0.0 + 0.0j, device=self.device, dtype=self.complex_dtype)
            yhat = torch.zeros_like(y)
            resid = y - yhat
        else:
            beta = torch.sum(torch.conj(x) * y) / denom
            yhat = beta * x
            resid = y - yhat

        sse = torch.sum(torch.abs(resid) ** 2).to(self.real_dtype)
        yc = y - torch.mean(y)
        sst = torch.sum(torch.abs(yc) ** 2).to(self.real_dtype)
        if sst <= self.eps:
            r2 = torch.tensor(0.0, device=self.device, dtype=self.real_dtype)
        else:
            r2 = 1.0 - sse / sst

        stats = torch.stack([
            r2.to(self.real_dtype),
            torch.tensor(float("nan"), device=self.device, dtype=self.real_dtype),
            torch.tensor(float("nan"), device=self.device, dtype=self.real_dtype),
            torch.tensor(float("nan"), device=self.device, dtype=self.real_dtype),
        ])
        return {
            "beta": beta,
            "yhat": yhat,
            "resid": resid,
            "sse": sse,
            "sst": sst,
            "r2": r2.to(self.real_dtype),
            "stats": stats,
        }

    def _r2_no_intercept(self, x: Tensor, y: Tensor) -> Tensor:
        return self._regress_no_intercept_details(x, y)["r2"]

    def _estimate_c_angle_single(self, cm_con, cm_ang, replc6_con, replc6_ang, r2_ang, r2_center_ang, cfg, ii):
        jdjd = 0.02
        jdc = 0.02
        angle_mask = replc6_ang.abs() != 0
        con_mask = replc6_con.abs() != 0
        f = torch.arange(-math.pi, math.pi + jdjd / 2, jdjd, device=self.device, dtype=self.real_dtype)
        ff = torch.arange(0.0, 0.6 + jdc / 2, jdc, device=self.device, dtype=self.real_dtype)

        g_raw = gg_raw = g_after = gg_after = None
        imf = imf2 = None
        e = ee = None

        if angle_mask.any():
            e = torch.angle(cm_ang[angle_mask]).to(self.real_dtype)
            g_raw = _histc_matlab(e, f)
            g_after = g_raw.clone()

            if cfg.use_emd:
                start_idx = 5 if torch.max(g_raw) < 50 else 4
                g_after = emd_reduce_for_sim_fast(
                    g_raw,
                    start_idx=start_idx,
                    device=self.device,
                    dtype=self.real_dtype,
                )

            h = torch.where(g_after == g_after.max())[0]
            angle6 = torch.tensor(
                -math.pi + float(h[0].item() + 1) * jdjd,
                device=self.device,
                dtype=self.real_dtype,
            )
        else:
            h = torch.tensor([], device=self.device, dtype=torch.long)
            angle6 = torch.tensor(float("nan"), device=self.device, dtype=self.real_dtype)

        if con_mask.any():
            ee = torch.abs(cm_con[con_mask]).to(self.real_dtype)
            gg_raw = _histc_matlab(ee, ff)
            gg_after = gg_raw.clone()

            if cfg.use_emd:
                gg_after = emd_reduce_for_sim_fast(
                    gg_raw,
                    start_idx=1,
                    device=self.device,
                    dtype=self.real_dtype,
                )

            hh = torch.where(gg_after == gg_after.max())[0]
            c6 = torch.tensor(
                jdc * float((hh.to(self.real_dtype) + 1).mean().item()),
                device=self.device,
                dtype=self.real_dtype,
            )
        else:
            hh = torch.tensor([], device=self.device, dtype=torch.long)
            c6 = torch.tensor(float("nan"), device=self.device, dtype=self.real_dtype)

        if cfg.use_regress and angle_mask.any():
            y = r2_ang[angle_mask].to(self.complex_dtype)
            x = r2_center_ang[angle_mask].to(self.complex_dtype)
            reg = self._regress_no_intercept_details(x, y)
            r2_value = reg["r2"]
            beta = reg["beta"]
            yhat = reg["yhat"]
            resid = reg["resid"]
            sse = reg["sse"]
            sst = reg["sst"]
            stats = reg["stats"]
        else:
            x = y = torch.tensor([], device=self.device, dtype=self.complex_dtype)
            beta = torch.tensor(float("nan") + 0.0j, device=self.device, dtype=self.complex_dtype)
            yhat = resid = torch.tensor([], device=self.device, dtype=self.complex_dtype)
            sse = sst = torch.tensor(float("nan"), device=self.device, dtype=self.real_dtype)
            stats = torch.tensor([1.0, float("nan"), float("nan"), float("nan")], device=self.device,
                                 dtype=self.real_dtype)
            r2_value = torch.tensor(1.0, device=self.device, dtype=self.real_dtype)

        self._dbg_save(f"R2_spi_{ii:02d}", {
            "ii": ii,
            "e": e if e is not None else np.array([]),
            "ee": ee if ee is not None else np.array([]),
            "f": f,
            "ff": ff,
            "g_raw": g_raw if g_raw is not None else np.array([]),
            "gg_raw": gg_raw if gg_raw is not None else np.array([]),
            "imf": imf if imf is not None else np.array([]),
            "imf2": imf2 if imf2 is not None else np.array([]),
            "g_after": g_after if g_after is not None else np.array([]),
            "gg_after": gg_after if gg_after is not None else np.array([]),
            "h": h,
            "hh": hh,
            "angle6": angle6,
            "c6": c6,
            "R2_x": x,
            "R2_y": y,
            "beta": beta,
            "yhat": yhat,
            "resid": resid,
            "sse": sse,
            "sst": sst,
            "stats": stats,
            "R2_angle": r2_value,
        })

        if float(r2_value.item()) < cfg.min_r2:
            msg = f"The R-square statistic of linear regression of parameters is lower than {cfg.min_r2:.3f}"
            if cfg.fail_on_low_r2:
                raise RuntimeError(msg)
            if cfg.warn_on_low_r2:
                warnings.warn(msg)

        return c6, angle6, r2_value

    def _wiener_reconstruct_with_saved_params(self, cfg, raw_info, raw_gpu, otf_template, background, param, saved_estimates):
        t_wr0 = self._now()
        sx = raw_info["height"]
        sy = raw_info["width"]
        num_modes = cfg.nphases * cfg.nangles
        ns = cfg.nangles * (cfg.nphases - 1)
        n = int(max(512, sx, sy))
        n = 256 if n <= 256 else 512 if n <= 512 else n

        zuobiaox = param["zuobiaox"].to(self.device, dtype=self.real_dtype) * (n / float(param["n"].item()))
        zuobiaoy = param["zuobiaoy"].to(self.device, dtype=self.real_dtype) * (n / float(param["n"].item()))
        c6 = saved_estimates["c6"].to(self.device, dtype=self.real_dtype).reshape(-1)
        angle6 = saved_estimates["angle6"].to(self.device, dtype=self.real_dtype).reshape(-1)
        r2_angles = saved_estimates["R2_angles"].to(self.device, dtype=self.real_dtype).reshape(-1)

        if zuobiaox.numel() != num_modes or zuobiaoy.numel() != num_modes:
            raise ValueError(
                f"Saved zuobiaox/zuobiaoy length must be {num_modes}, "
                f"got {zuobiaox.numel()} and {zuobiaoy.numel()}."
            )
        if c6.numel() != ns or angle6.numel() != ns:
            raise ValueError(
                f"Saved c6/angle6 length must be {ns}, got {c6.numel()} and {angle6.numel()}."
            )

        bg = (
            _crop_center(background, (sx, sy)).to(self.real_dtype)
            if background is not None and cfg.use_background
            else torch.zeros((sx, sy), device=self.device, dtype=self.real_dtype)
        )

        self._record_timing("step4a_setup_preR2_ms", 0.0)
        self._record_timing("step4b_c6_angle_r2_ms", 0.0)

        t_pc = self._now()
        angle_pairs = angle6.view(cfg.nangles, cfg.nphases - 1)
        c_pairs = c6.view(cfg.nangles, cfg.nphases - 1)
        deph = torch.sign(angle_pairs[:, 0]) * (torch.abs(angle_pairs[:, 0]) + torch.abs(angle_pairs[:, 1])) * 0.5

        inv_phase_all = []
        for a in range(cfg.nangles):
            phi = _phase_list(cfg.theta_ratio, cfg.regul, float(deph[a].item()), dtype=self.real_dtype).to(self.device)
            one = torch.ones((), device=self.device, dtype=self.real_dtype)
            mat = torch.stack([
                torch.stack([
                    torch.tensor(1 + 0j, device=self.device, dtype=self.complex_dtype),
                    torch.polar(one, phi[0]).to(self.complex_dtype),
                    torch.polar(one, -phi[0]).to(self.complex_dtype),
                ]),
                torch.stack([
                    torch.tensor(1 + 0j, device=self.device, dtype=self.complex_dtype),
                    torch.polar(one, phi[1]).to(self.complex_dtype),
                    torch.polar(one, -phi[1]).to(self.complex_dtype),
                ]),
                torch.stack([
                    torch.tensor(1 + 0j, device=self.device, dtype=self.complex_dtype),
                    torch.polar(one, phi[2]).to(self.complex_dtype),
                    torch.polar(one, -phi[2]).to(self.complex_dtype),
                ]),
            ], dim=0)
            inv_phase_all.append(torch.linalg.inv(mat))
        inv_phase_all = torch.stack(inv_phase_all, dim=0)

        xishu_vals = []
        for a in range(cfg.nangles):
            pw = 0.5 * ((1.0 / c_pairs[a, 0]) + (1.0 / c_pairs[a, 1]))
            xishu_vals.extend([
                torch.tensor(1.0, device=self.device, dtype=self.real_dtype),
                pw.to(self.real_dtype),
                pw.to(self.real_dtype),
            ])
        xishu = torch.stack(xishu_vals)

        plong = torch.floor(torch.sum(torch.sqrt((zuobiaox - zuobiaox[0]) ** 2 + (zuobiaoy - zuobiaoy[0]) ** 2)) / (2 * cfg.nangles))
        fc = math.ceil(cfg.fc_recon_base * (n / 512))
        freq_n = torch.arange(-n / 2, n / 2, device=self.device, dtype=self.real_dtype)
        ky_n, kx_n = torch.meshgrid(freq_n, freq_n, indexing="ij")
        kr_n = torch.sqrt(kx_n ** 2 + ky_n ** 2)
        jiequ = (kr_n <= fc).to(self.real_dtype)
        psf = _resize2d(otf_template, (n, n)).to(self.real_dtype)
        h = (psf * jiequ).to(self.real_dtype)
        h = h / torch.clamp(h.max(), min=self.eps)
        h1 = (h != 0).to(self.real_dtype)
        hk = _center_embed(h, (2 * n, 2 * n)).to(self.complex_dtype)
        h1big = _center_embed(h1, (2 * n, 2 * n)).to(self.complex_dtype)
        hk_spatial = _centered_ifft2(hk)
        h1big_spatial = _centered_ifft2(h1big)

        replc_h_test = []
        replch_stack = []
        kx_modes = []
        ky_modes = []
        for ii in range(num_modes):
            kytest = 2 * math.pi * (float(zuobiaox[ii].item()) - n) / (2 * n)
            kxtest = 2 * math.pi * (float(zuobiaoy[ii].item()) - n) / (2 * n)
            ky_modes.append(kytest)
            kx_modes.append(kxtest)
            replc = self._shift_centered_spectrum_from_spatial(h1big_spatial, kxtest, kytest)
            replc = (replc.abs() > 0.9).to(self.real_dtype)
            replch = self._shift_centered_spectrum_from_spatial(hk_spatial, kxtest, kytest) * replc.to(self.complex_dtype)
            replc_h_test.append(replc)
            replch_stack.append(replch)

        kx_modes = torch.tensor(kx_modes, device=self.device, dtype=self.real_dtype)
        ky_modes = torch.tensor(ky_modes, device=self.device, dtype=self.real_dtype)
        replc_h_test = torch.stack(replc_h_test, dim=0)
        replch_complex = torch.stack(replch_stack, dim=0)
        reh = replch_complex.abs().to(self.real_dtype)
        re = torch.where(reh > self.eps, replch_complex, torch.full_like(replch_complex, 1e19 + 0.0j))
        re = re / re.abs()
        hs = torch.sum(reh ** 2, dim=0)

        replc_h_b = replc_h_test.to(self.complex_dtype).view(1, num_modes, 2 * n, 2 * n)
        reh_b = reh.to(self.complex_dtype).view(1, num_modes, 2 * n, 2 * n)
        re_b = re.view(1, num_modes, 2 * n, 2 * n)
        xishu_b = xishu.to(self.complex_dtype).view(1, num_modes, 1, 1)

        freq_2n = torch.arange(-n, n, device=self.device, dtype=self.real_dtype)
        ky_2n, kx_2n = torch.meshgrid(freq_2n, freq_2n, indexing="ij")
        kr_2n = torch.sqrt(kx_2n ** 2 + ky_2n ** 2)
        k_max = float(plong.item()) + fc
        bhs = torch.cos(math.pi * kr_2n / (2 * k_max))
        bhs[kr_2n > k_max] = 0
        mask = _sigmoid_mask((sx, sy), 0.25, self.device, self.real_dtype)
        self._record_timing("step4c_phase_compensation_setup_ms", self._ms(t_pc, self._now()))

        t_loop = self._now()
        group_frames = num_modes * cfg.reconstruct_group_stride
        total_groups = (raw_info["num_frames"] - cfg.starframe + 1) // group_frames
        if total_groups <= 0:
            raise RuntimeError(
                "No reconstruction groups available. "
                "Please check raw frame count, starframe, and reconstruct_group_stride."
            )

        group_batch_size = max(1, int(getattr(cfg, "recon_group_batch", 1)))
        recon_frames = []
        group_idx = 0
        denom_c = (hs + 0.005 * cfg.nangles * (cfg.wiener ** 2)).to(self.complex_dtype)
        bhs_c = bhs.to(self.complex_dtype)
        mask_b = (mask ** 3).view(1, 1, sx, sy)
        jiequ_b = jiequ.view(1, 1, n, n).to(self.complex_dtype)
        start0_global = cfg.starframe - 1
        total_frames_needed = total_groups * group_frames
        raw_recon_gpu = raw_gpu[start0_global:start0_global + total_frames_needed]

        while group_idx < total_groups:
            curr_batch = min(group_batch_size, total_groups - group_idx)
            start_frame_0 = group_idx * group_frames
            end_frame_0 = start_frame_0 + curr_batch * group_frames
            d = raw_recon_gpu[start_frame_0:end_frame_0]
            d = d.view(curr_batch, cfg.reconstruct_group_stride, num_modes, sx, sy)
            d = d - bg.view(1, 1, 1, sx, sy)
            d = d.mean(dim=1)
            d = d * mask_b

            d_flat = d.reshape(curr_batch * num_modes, sx, sy)
            d_pad_flat = self._center_embed_batch(d_flat, (n, n))
            di_bar_flat = _centered_fft2(d_pad_flat)
            di_bar = di_bar_flat.view(curr_batch, num_modes, n, n)
            di_group = di_bar.view(curr_batch, cfg.nangles, cfg.nphases, n, n)
            sp_now = torch.einsum("ajk,gakxy->gajxy", inv_phase_all, di_group)
            sp_now = sp_now.reshape(curr_batch, num_modes, n, n)
            sp_now = sp_now * jiequ_b

            sp_now_flat = sp_now.reshape(curr_batch * num_modes, n, n)
            sp_now_big_flat = self._center_embed_batch(sp_now_flat, (2 * n, 2 * n))
            sp_now_spatial_flat = _centered_ifft2(sp_now_big_flat)
            kx_batch = kx_modes.view(1, num_modes).expand(curr_batch, num_modes).reshape(-1)
            ky_batch = ky_modes.view(1, num_modes).expand(curr_batch, num_modes).reshape(-1)
            retirff_flat = self._shift_centered_spectrum_from_spatial_batch(sp_now_spatial_flat, kx_batch, ky_batch)
            retirff = retirff_flat.view(curr_batch, num_modes, 2 * n, 2 * n)
            retirff = (retirff * replc_h_b) / re_b
            tmprc1 = xishu_b * retirff * reh_b / denom_c
            dr = tmprc1.sum(dim=1)
            drr = dr * bhs_c
            fimage = torch.abs(_centered_ifft2(drr))
            frame_batch = fimage[:, n - sx:n + sx, n - sy:n + sy]
            for gi in range(curr_batch):
                recon_frames.append(frame_batch[gi].real.to(self.real_dtype))
            group_idx += curr_batch

        loop_end = self._now()
        self._record_timing("step4d_recon_loop_total_ms", self._ms(t_loop, loop_end))
        self._record_timing("step4d_recon_loop_mean_per_group_ms", self._ms(t_loop, loop_end) / max(len(recon_frames), 1))
        self._record_timing("step4_wiener_reconstruction_total_ms", self._ms(t_wr0, loop_end))
        if len(recon_frames) == 0:
            raise RuntimeError(
                "No reconstruction frames were generated. "
                "Please check raw frame count, starframe, and reconstruct_group_stride."
            )
        recon = torch.stack(recon_frames, dim=0)
        recon = torch.clamp(recon, min=0)
        return recon, c6, angle6, r2_angles

    def _wiener_reconstruct(self, cfg, raw_info, raw_gpu, otf_template, background, param):
        t_wr0 = self._now()
        sx = raw_info["height"]
        sy = raw_info["width"]
        num_modes = cfg.nphases * cfg.nangles
        n = int(max(512, sx, sy))
        n = 256 if n <= 256 else 512 if n <= 512 else n
        zuobiaox = param["zuobiaox"] * (n / float(param["n"].item()))
        zuobiaoy = param["zuobiaoy"] * (n / float(param["n"].item()))
        fc_ang = math.ceil(cfg.fc_angle_base * (n / 512))
        fc_con = math.ceil((cfg.fc_content_base_647 if cfg.wavelength_nm in {638, 647} else cfg.fc_content_base_488_561) * (n / 512))
        phase_matrix = _phase_matrix(cfg.theta_ratio, cfg.regul, self.device, self.complex_dtype)
        inv_phase_matrix = torch.linalg.inv(phase_matrix)
        fd = self._average_groups_from_gpu(raw_gpu, cfg.nphases, cfg.nangles, cfg.avg_groups, cfg.starframe)
        bg = _crop_center(background, (sx, sy)).to(self.real_dtype) if background is not None and cfg.use_background else torch.zeros((sx, sy), device=self.device, dtype=self.real_dtype)
        fd = fd - bg.unsqueeze(0)
        fd_n = _center_embed(fd, (n, n))
        dibars = _centered_fft2(fd_n)
        h_ang = _resize2d(otf_template, (n, n)).abs()
        h_con = h_ang.clone()
        freq = torch.arange(-n / 2, n / 2, device=self.device, dtype=self.real_dtype)
        ky, kx = torch.meshgrid(freq, freq, indexing="ij")
        kr = torch.sqrt(kx ** 2 + ky ** 2)
        h_ang[kr > fc_ang] = 0
        h_con[kr > fc_con] = 0
        h1_ang = (h_ang != 0).to(self.real_dtype)
        h1_con = (h_con != 0).to(self.real_dtype)
        dibars_ang = dibars * h1_ang.unsqueeze(0).to(self.complex_dtype)
        di_reshaped = dibars_ang.view(cfg.nangles, cfg.nphases, n, n)
        sp = torch.einsum("jk,akxy->ajxy", inv_phase_matrix, di_reshaped).reshape(num_modes, n, n)
        h_ang_big = _center_embed(h_ang, (2 * n, 2 * n)).to(self.complex_dtype)
        h_con_big = _center_embed(h_con, (2 * n, 2 * n)).to(self.complex_dtype)
        h1_ang_big = _center_embed(h1_ang, (2 * n, 2 * n)).to(self.complex_dtype)
        h1_con_big = _center_embed(h1_con, (2 * n, 2 * n)).to(self.complex_dtype)
        h_ang_big_spatial = _centered_ifft2(h_ang_big)
        h_con_big_spatial = _centered_ifft2(h_con_big)
        h1_ang_big_spatial = _centered_ifft2(h1_ang_big)
        h1_con_big_spatial = _centered_ifft2(h1_con_big)
        self._dbg_save("wiener_global_preR2", {
            "fd": fd,
            "bg": bg,
            "fd512": fd_n,
            "DIbars": dibars,
            "H_ang": h_ang,
            "H_con": h_con,
            "H1_ang": h1_ang,
            "H1_con": h1_con,
            "DIbars_ang": dibars_ang,
            "sp": sp,
            "zuobiaox": zuobiaox,
            "zuobiaoy": zuobiaoy
        })
        self._record_timing("step4a_setup_preR2_ms", self._ms(t_wr0, self._now()))

        t_r2 = self._now()
        ns = cfg.nangles * (cfg.nphases - 1)
        c6_list = []
        angle6_list = []
        r2_list = []
        cm_ang = []
        cm_con = []
        replc6_ang_all = []
        replc6_con_all = []
        R2_ang_all = []
        R2_center_all = []
        for spi in range(1, ns + 1):
            center_idx = _sideband_center_index_0(spi, cfg.nphases)
            side_idx = _sideband_order_index_0(spi)
            coord_idx = _coord_index_0(spi)
            sp_center = _center_embed(sp[center_idx], (2 * n, 2 * n))
            sp_side = _center_embed(sp[side_idx], (2 * n, 2 * n))
            sp_side_spatial = _centered_ifft2(sp_side)
            kytest = 2 * math.pi * (float(zuobiaox[coord_idx].item()) - n) / (2 * n)
            kxtest = 2 * math.pi * (float(zuobiaoy[coord_idx].item()) - n) / (2 * n)
            xx, yy = self._get_xy_grid((2 * n, 2 * n), self.real_dtype)
            Ir = torch.polar(torch.ones_like(xx), kxtest * xx + kytest * yy).to(self.complex_dtype)
            replc_h_ang = self._shift_centered_spectrum_from_spatial(h1_ang_big_spatial, kxtest, kytest)
            replc_h_con = self._shift_centered_spectrum_from_spatial(h1_con_big_spatial, kxtest, kytest)
            replc_h_ang = (replc_h_ang.abs() > 0.9).to(self.real_dtype).to(self.complex_dtype)
            replc_h_con = (replc_h_con.abs() > 0.9).to(self.real_dtype).to(self.complex_dtype)
            replch_ang = self._shift_centered_spectrum_from_spatial(h_ang_big_spatial, kxtest, kytest) * replc_h_ang
            replch_con = self._shift_centered_spectrum_from_spatial(h_con_big_spatial, kxtest, kytest) * replc_h_con
            replctest = self._shift_centered_spectrum_from_spatial(sp_side_spatial, kxtest, kytest)
            youhua_ang = replctest * replc_h_ang * h_ang_big
            youhua_con = replctest * replc_h_con * h_con_big
            overlap_ang = sp_center * replch_ang
            overlap_con = sp_center * replch_con
            cm_ang_i = youhua_ang / (overlap_ang + self.eps)
            cm_con_i = youhua_con / (overlap_con + self.eps)
            replc6_ang = replch_ang * h_ang_big
            replc6_con = replch_con * h_con_big
            cm_ang.append(cm_ang_i)
            cm_con.append(cm_con_i)
            replc6_ang_all.append(replc6_ang)
            replc6_con_all.append(replc6_con)
            R2_ang_all.append(youhua_ang)
            R2_center_all.append(overlap_ang)
            self._dbg_save(f"wiener_spi_{spi:02d}_preR2", {
                "spi": spi,
                "sp_center": sp_center,
                "sp_side": sp_side,
                "kxtest": kxtest,
                "kytest": kytest,
                "Ir": Ir,
                "replcH_ang": replc_h_ang,
                "replcH_con": replc_h_con,
                "replch_ang": replch_ang,
                "replch_con": replch_con,
                "replctest": replctest,
                "youhua_ang": youhua_ang,
                "youhua_con": youhua_con,
                "overlap_ang": overlap_ang,
                "overlap_con": overlap_con,
                "cm_ang": cm_ang_i,
                "cm_con": cm_con_i,
                "replc6_ang": replc6_ang,
                "replc6_con": replc6_con,
                "R2_ang": youhua_ang,
                "R2_zhongxin_ang": overlap_ang
            })
            c6_i, angle_i, r2_i = self._estimate_c_angle_single(
                cm_con_i, cm_ang_i, replc6_con, replc6_ang, youhua_ang, overlap_ang, cfg, spi
            )
            c6_list.append(c6_i)
            angle6_list.append(angle_i)
            r2_list.append(r2_i)
        c6 = torch.stack(c6_list)
        angle6 = torch.stack(angle6_list)
        r2_angles = torch.stack(r2_list)
        self._dbg_save("wiener_R2_summary", {"c6": c6, "angle6": angle6, "R2_angles": r2_angles})
        self._record_timing("step4b_c6_angle_r2_ms", self._ms(t_r2, self._now()))

        t_pc = self._now()
        angle_pairs = angle6.view(cfg.nangles, cfg.nphases - 1)
        c_pairs = c6.view(cfg.nangles, cfg.nphases - 1)
        deph = torch.sign(angle_pairs[:, 0]) * (torch.abs(angle_pairs[:, 0]) + torch.abs(angle_pairs[:, 1])) * 0.5
        inv_phase_all = []
        for a in range(cfg.nangles):
            phi = _phase_list(cfg.theta_ratio, cfg.regul, float(deph[a].item()), dtype=self.real_dtype).to(self.device)
            one = torch.ones((), device=self.device, dtype=self.real_dtype)
            mat = torch.stack([
                torch.stack([
                    torch.tensor(1 + 0j, device=self.device, dtype=self.complex_dtype),
                    torch.polar(one, phi[0]).to(self.complex_dtype),
                    torch.polar(one, -phi[0]).to(self.complex_dtype)
                ]),
                torch.stack([
                    torch.tensor(1 + 0j, device=self.device, dtype=self.complex_dtype),
                    torch.polar(one, phi[1]).to(self.complex_dtype),
                    torch.polar(one, -phi[1]).to(self.complex_dtype)
                ]),
                torch.stack([
                    torch.tensor(1 + 0j, device=self.device, dtype=self.complex_dtype),
                    torch.polar(one, phi[2]).to(self.complex_dtype),
                    torch.polar(one, -phi[2]).to(self.complex_dtype)
                ])
            ], dim=0)
            inv_phase_all.append(torch.linalg.inv(mat))
        inv_phase_all = torch.stack(inv_phase_all, dim=0)
        xishu_vals = []
        for a in range(cfg.nangles):
            pw = 0.5 * ((1.0 / c_pairs[a, 0]) + (1.0 / c_pairs[a, 1]))
            xishu_vals.extend([
                torch.tensor(1.0, device=self.device, dtype=self.real_dtype),
                pw.to(self.real_dtype),
                pw.to(self.real_dtype),
            ])
        xishu = torch.stack(xishu_vals)
        plong = torch.floor(torch.sum(torch.sqrt((zuobiaox - zuobiaox[0]) ** 2 + (zuobiaoy - zuobiaoy[0]) ** 2)) / (2 * cfg.nangles))
        fc = math.ceil(cfg.fc_recon_base * (n / 512))
        freq_n = torch.arange(-n / 2, n / 2, device=self.device, dtype=self.real_dtype)
        ky_n, kx_n = torch.meshgrid(freq_n, freq_n, indexing="ij")
        kr_n = torch.sqrt(kx_n ** 2 + ky_n ** 2)
        jiequ = (kr_n <= fc).to(self.real_dtype)
        jiequ_modes = jiequ.unsqueeze(0).to(self.complex_dtype)
        psf = _resize2d(otf_template, (n, n)).to(self.real_dtype)
        h = (psf * jiequ).to(self.real_dtype)
        h = h / torch.clamp(h.max(), min=self.eps)
        h1 = (h != 0).to(self.real_dtype)
        hk = _center_embed(h, (2 * n, 2 * n)).to(self.complex_dtype)
        h1big = _center_embed(h1, (2 * n, 2 * n)).to(self.complex_dtype)
        hk_spatial = _centered_ifft2(hk)
        h1big_spatial = _centered_ifft2(h1big)
        replc_h_test = []
        replch_stack = []
        kx_modes = []
        ky_modes = []

        for ii in range(num_modes):
            kytest = 2 * math.pi * (float(zuobiaox[ii].item()) - n) / (2 * n)
            kxtest = 2 * math.pi * (float(zuobiaoy[ii].item()) - n) / (2 * n)

            ky_modes.append(kytest)
            kx_modes.append(kxtest)

            replc = self._shift_centered_spectrum_from_spatial(h1big_spatial, kxtest, kytest)
            replc = (replc.abs() > 0.9).to(self.real_dtype)

            replch = self._shift_centered_spectrum_from_spatial(hk_spatial, kxtest, kytest) * replc.to(
                self.complex_dtype)

            replc_h_test.append(replc)
            replch_stack.append(replch)

        kx_modes = torch.tensor(kx_modes, device=self.device, dtype=self.real_dtype)
        ky_modes = torch.tensor(ky_modes, device=self.device, dtype=self.real_dtype)

        replc_h_test = torch.stack(replc_h_test, dim=0)  # [K, 2n, 2n], real
        replch_complex = torch.stack(replch_stack, dim=0)  # [K, 2n, 2n], complex
        reh = replch_complex.abs().to(self.real_dtype)  # [K, 2n, 2n], real

        re = torch.where(
            reh > self.eps,
            replch_complex,
            torch.full_like(replch_complex, 1e19 + 0.0j)
        )
        re = re / re.abs()  # [K, 2n, 2n], complex
        hs = torch.sum(reh ** 2, dim=0)  # [2n, 2n], real

        # 预先准备好 step4d 会用到的广播张量
        replc_h_test_c = replc_h_test.to(self.complex_dtype)  # [K, 2n, 2n]
        reh_c = reh.to(self.complex_dtype)  # [K, 2n, 2n]
        xishu_c = xishu.to(self.complex_dtype).view(num_modes, 1, 1)  # [K, 1, 1]

        freq_2n = torch.arange(-n, n, device=self.device, dtype=self.real_dtype)
        ky_2n, kx_2n = torch.meshgrid(freq_2n, freq_2n, indexing="ij")
        kr_2n = torch.sqrt(kx_2n ** 2 + ky_2n ** 2)
        k_max = float(plong.item()) + fc
        bhs = torch.cos(math.pi * kr_2n / (2 * k_max))
        bhs[kr_2n > k_max] = 0
        mask = _sigmoid_mask((sx, sy), 0.25, self.device, self.real_dtype)
        mask_modes = (mask ** 3).unsqueeze(0)
        self._dbg_save("wiener_global_postR2", {
            "inv_phase_all": inv_phase_all,
            "xishu": xishu,
            "plong": plong,
            "fc": fc,
            "jiequ": jiequ,
            "H": h,
            "H1": h1,
            "Hk": hk,
            "H1big": h1big,
            "replcHtest": replc_h_test,
            "replch": replch_complex,
            "re": re,
            "reh": reh,
            "hs": hs,
            "bhs": bhs,
            "mask": mask,
            "mask9": mask_modes
        })
        self._record_timing("step4c_phase_compensation_setup_ms", self._ms(t_pc, self._now()))

        t_loop = self._now()

        group_frames = num_modes * cfg.reconstruct_group_stride
        total_groups = (raw_info["num_frames"] - cfg.starframe + 1) // group_frames
        if total_groups <= 0:
            raise RuntimeError(
                "No reconstruction groups available. "
                "Please check raw frame count, starframe, and reconstruct_group_stride."
            )

        group_batch_size = max(1, int(getattr(cfg, "recon_group_batch", 1)))

        recon_frames = []
        group_idx = 0

        denom = hs + 0.005 * cfg.nangles * (cfg.wiener ** 2)
        denom_c = denom.to(self.complex_dtype)  # [2n, 2n]
        bhs_c = bhs.to(self.complex_dtype)  # [2n, 2n]

        # 这些张量在 batch 维上共享
        mask_b = (mask ** 3).view(1, 1, sx, sy)  # [1, 1, sx, sy]
        jiequ_b = jiequ.view(1, 1, n, n).to(self.complex_dtype)  # [1, 1, n, n]
        xishu_b = xishu_c.view(1, num_modes, 1, 1)  # [1, K, 1, 1]
        replc_h_b = replc_h_test_c.view(1, num_modes, 2 * n, 2 * n)  # [1, K, 2n, 2n]
        reh_b = reh_c.view(1, num_modes, 2 * n, 2 * n)  # [1, K, 2n, 2n]
        re_b = re.view(1, num_modes, 2 * n, 2 * n)  # [1, K, 2n, 2n]

        # raw_gpu 已经在显存里，直接切片
        start0_global = cfg.starframe - 1
        total_frames_needed = total_groups * group_frames
        raw_recon_gpu = raw_gpu[start0_global:start0_global + total_frames_needed]  # [T, sx, sy]

        while group_idx < total_groups:
            curr_batch = min(group_batch_size, total_groups - group_idx)

            # 从 all_raw 里切 batch，不再读盘
            start_frame_0 = group_idx * group_frames
            end_frame_0 = start_frame_0 + curr_batch * group_frames
            d = raw_recon_gpu[start_frame_0:end_frame_0]  # already on GPU
            d = d.view(curr_batch, cfg.reconstruct_group_stride, num_modes, sx, sy)
            d = d - bg.view(1, 1, 1, sx, sy)
            d = d.mean(dim=1)  # [G, K, sx, sy]
            d = d * mask_b  # [G, K, sx, sy]

            # === batched FFT / phase unmix ===
            d_flat = d.reshape(curr_batch * num_modes, sx, sy)  # [G*K, sx, sy]
            d_pad_flat = self._center_embed_batch(d_flat, (n, n))  # [G*K, n, n]
            di_bar_flat = _centered_fft2(d_pad_flat)  # [G*K, n, n]
            di_bar = di_bar_flat.view(curr_batch, num_modes, n, n)  # [G, K, n, n]

            di_group = di_bar.view(curr_batch, cfg.nangles, cfg.nphases, n, n)  # [G, A, P, n, n]
            sp_now = torch.einsum("ajk,gakxy->gajxy", inv_phase_all, di_group)  # [G, A, P, n, n]
            sp_now = sp_now.reshape(curr_batch, num_modes, n, n)  # [G, K, n, n]
            sp_now = sp_now * jiequ_b  # [G, K, n, n]

            # === batched mode shift ===
            sp_now_flat = sp_now.reshape(curr_batch * num_modes, n, n)  # [G*K, n, n]
            sp_now_big_flat = self._center_embed_batch(sp_now_flat, (2 * n, 2 * n))
            sp_now_spatial_flat = _centered_ifft2(sp_now_big_flat)  # [G*K, 2n, 2n]

            kx_batch = kx_modes.view(1, num_modes).expand(curr_batch, num_modes).reshape(-1)  # [G*K]
            ky_batch = ky_modes.view(1, num_modes).expand(curr_batch, num_modes).reshape(-1)  # [G*K]

            retirff_flat = self._shift_centered_spectrum_from_spatial_batch(
                sp_now_spatial_flat,
                kx_batch,
                ky_batch,
            )  # [G*K, 2n, 2n]

            retirff = retirff_flat.view(curr_batch, num_modes, 2 * n, 2 * n)  # [G, K, 2n, 2n]
            retirff = (retirff * replc_h_b) / re_b  # [G, K, 2n, 2n]

            tmprc1 = xishu_b * retirff * reh_b / denom_c  # [G, K, 2n, 2n]
            dr = tmprc1.sum(dim=1)  # [G, 2n, 2n]

            # === final image ===
            drr = dr * bhs_c  # [G, 2n, 2n]
            fimage = torch.abs(_centered_ifft2(drr))  # [G, 2n, 2n]
            frame_batch = fimage[:, n - sx:n + sx, n - sy:n + sy]  # [G, out_h, out_w]

            for gi in range(curr_batch):
                recon_frames.append(frame_batch[gi].real.to(self.real_dtype))

            if cfg.debug_save and group_idx < cfg.debug_max_recon_groups:
                self._dbg_save(
                    f"recon_group_batch_{group_idx + 1:02d}",
                    {
                        "group_idx": group_idx,
                        "curr_batch": curr_batch,
                        "D_batch": d,
                        "DIbar_batch": di_bar,
                        "spNow_batch": sp_now,
                        "retirff_batch": retirff,
                        "tmprc1_batch": tmprc1,
                        "dr_batch": dr,
                        "drr_batch": drr,
                        "fimage_batch": fimage,
                        "recon_frame_batch": frame_batch,
                    },
                )

            group_idx += curr_batch
        loop_end = self._now()

        self._record_timing("step4d_recon_loop_total_ms", self._ms(t_loop, loop_end))
        self._record_timing("step4d_recon_loop_mean_per_group_ms", self._ms(t_loop, loop_end) / max(len(recon_frames), 1))
        self._record_timing("step4_wiener_reconstruction_total_ms", self._ms(t_wr0, loop_end))

        if len(recon_frames) == 0:
            raise RuntimeError(
                "No reconstruction frames were generated. "
                "Please check raw frame count, starframe, and reconstruct_group_stride."
            )

        recon = torch.stack(recon_frames, dim=0)
        recon = torch.clamp(recon, min=0)
        return recon, c6, angle6, r2_angles


def hessian_sim_wiener_refactor_torch(
    raw_path: Union[str, Path],
    opts: Optional[Union[SIMWienerOptions, Dict[str, Any]]] = None,
    device: Optional[Union[str, torch.device]] = None,
) -> Dict[str, Any]:
    reconstructor = SIMWienerGPUReconstructor(device=device)
    return reconstructor.reconstruct(raw_path, opts)


hessian_sim_wiener_refactor = hessian_sim_wiener_refactor_torch

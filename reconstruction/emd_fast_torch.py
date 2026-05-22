from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Union

import numpy as np
import torch


TensorLike = Union[torch.Tensor, np.ndarray, Sequence[float]]


@dataclass
class FastEMDOptions:
    device: Optional[Union[str, torch.device]] = None
    dtype: torch.dtype = torch.float32
    # 控制“保留低频趋势”的强弱；数值越大，保留越平滑
    base_keep_ratio: float = 0.35
    # start_idx 越大，说明你想去掉越多高频 IMF，这里对应更强低通
    per_level_extra_ratio: float = 0.08
    eps: float = 1e-8


def _resolve_device(device=None):
    if device is not None:
        return torch.device(device)
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def _to_2d_torch(x: TensorLike, device, dtype: torch.dtype) -> torch.Tensor:
    if isinstance(x, torch.Tensor):
        t = x.to(device=device, dtype=dtype)
    else:
        t = torch.as_tensor(x, device=device, dtype=dtype)

    if t.ndim == 1:
        t = t.unsqueeze(0)   # [L] -> [1, L]
    elif t.ndim != 2:
        raise ValueError(f"Expected 1D or 2D histogram tensor, got shape {tuple(t.shape)}")
    return t


def _hann_lowpass_mask_rfft(length: int, cutoff_bin: int, device, dtype):
    """
    构造一个平滑低通 mask，避免硬截止带来的振铃。
    rfft 长度为 length//2 + 1
    """
    nfreq = length // 2 + 1
    cutoff_bin = max(1, min(int(cutoff_bin), nfreq - 1))

    mask = torch.zeros(nfreq, device=device, dtype=dtype)

    # [0, cutoff] 全通
    mask[:cutoff_bin] = 1.0

    # 用一小段 Hann 过渡
    trans = max(2, min(8, nfreq - cutoff_bin))
    if cutoff_bin < nfreq:
        end = min(nfreq, cutoff_bin + trans)
        m = end - cutoff_bin
        if m > 0:
            win = 0.5 * (1 + torch.cos(torch.linspace(0, torch.pi, m, device=device, dtype=dtype)))
            mask[cutoff_bin:end] = win

    return mask


def fft_trend_reduce_torch(
    hist: TensorLike,
    start_idx: int,
    opts: Optional[FastEMDOptions] = None,
) -> torch.Tensor:
    """
    用 GPU-friendly FFT 低通趋势替代:
        sum(imf[start_idx-1:, :], axis=0)

    输入:
        hist: [L] 或 [B, L]
        start_idx: 模拟“从第几层 IMF 开始保留后面的分量”
    输出:
        [L] 或 [B, L] torch.Tensor
    """
    if opts is None:
        opts = FastEMDOptions()

    device = _resolve_device(opts.device)
    x = _to_2d_torch(hist, device=device, dtype=opts.dtype)
    B, L = x.shape

    # 去均值，减少 DC 过强干扰
    mean = x.mean(dim=-1, keepdim=True)
    xc = x - mean

    # 根据 start_idx 控制平滑程度
    # start_idx 越大 -> 去掉更多高频 -> 保留更低频的趋势
    keep_ratio = opts.base_keep_ratio - (max(start_idx, 1) - 1) * opts.per_level_extra_ratio
    keep_ratio = max(0.08, min(0.8, keep_ratio))

    nfreq = L // 2 + 1
    cutoff_bin = max(1, int(round(keep_ratio * nfreq)))

    Xf = torch.fft.rfft(xc, dim=-1)
    lp_mask = _hann_lowpass_mask_rfft(L, cutoff_bin, device=device, dtype=x.real.dtype)
    trend = torch.fft.irfft(Xf * lp_mask.unsqueeze(0), n=L, dim=-1)

    # 加回均值，保持原 histogram 量级
    y = trend + mean

    # histogram 不应出现明显负数
    y = torch.clamp(y, min=0.0)

    if isinstance(hist, torch.Tensor) and hist.ndim == 1:
        return y[0]
    if not isinstance(hist, torch.Tensor):
        arr = np.asarray(hist)
        if arr.ndim == 1:
            return y[0]
    return y


def fft_trend_reduce_numpy(
    hist: TensorLike,
    start_idx: int,
    opts: Optional[FastEMDOptions] = None,
) -> np.ndarray:
    y = fft_trend_reduce_torch(hist, start_idx, opts=opts)
    return y.detach().cpu().numpy()


def emd_reduce_for_sim_fast(
    hist: TensorLike,
    start_idx: int,
    device: Optional[Union[str, torch.device]] = None,
    dtype: torch.dtype = torch.float32,
):
    """
    用法上替代原 emd_reduce_for_sim(hist, start_idx)
    """
    opts = FastEMDOptions(device=device, dtype=dtype)
    return fft_trend_reduce_torch(hist, start_idx, opts=opts)
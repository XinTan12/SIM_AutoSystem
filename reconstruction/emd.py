
"""MATLAB-compatible-ish EMD for the Hessian-SIM histogram path.

This module focuses on reproducing Gabriel Rilling's classic MATLAB emd.m for
real 1D vectors, which is exactly the case used by the SIM-Wiener parameter
estimation path:

    imf = emd(g)
    imf2 = emd(gg)

It implements the same default stop rule, extrema extraction with plateau
handling, mirror-symmetry boundary conditions, and cubic spline envelope
interpolation. It is intentionally CPU/Numpy/Scipy based because the vectors
here are short histograms, so exact behavior matters far more than GPU speed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence, Tuple

import numpy as np
from scipy.interpolate import CubicSpline, interp1d, PchipInterpolator


EPS = np.finfo(np.float64).eps


@dataclass
class EMDOptions:
    t: Optional[np.ndarray] = None
    stop: Tuple[float, float, float] = (0.05, 0.5, 0.05)
    display: int = 0
    maxiterations: int = 2000
    fix: int = 0
    maxmodes: int = 0
    interp: str = "spline"
    fix_h: int = 0
    mask: Optional[np.ndarray] = None
    ndirs: int = 4
    complex_version: int = 2


def _as_row_vector(x: Sequence[float]) -> np.ndarray:
    arr = np.asarray(x, dtype=np.float64)
    if arr.ndim != 1:
        arr = np.ravel(arr)
    return arr.astype(np.float64, copy=False)


def _interp1_matlab(xp: np.ndarray, yp: np.ndarray, xq: np.ndarray, method: str) -> np.ndarray:
    xp = np.asarray(xp, dtype=np.float64)
    yp = np.asarray(yp, dtype=np.float64)
    xq = np.asarray(xq, dtype=np.float64)

    method = method.lower()
    if method == "spline":
        cs = CubicSpline(xp, yp, bc_type="not-a-knot", extrapolate=True)
        return cs(xq)
    if method == "pchip":
        f = PchipInterpolator(xp, yp, extrapolate=True)
        return f(xq)
    if method in ("linear", "cubic"):
        f = interp1d(xp, yp, kind=method, fill_value="extrapolate", assume_sorted=True)
        return f(xq)
    raise ValueError(f"unsupported interp method: {method}")


def extr(x: Sequence[float], t: Optional[Sequence[float]] = None):
    x = _as_row_vector(x)
    if t is None:
        t = np.arange(1, x.size + 1, dtype=np.float64)
    else:
        t = _as_row_vector(t)

    m = x.size
    x1 = x[:-1]
    x2 = x[1:]
    indzer = np.where(x1 * x2 < 0)[0] + 1

    if np.any(x == 0):
        iz = np.where(x == 0)[0] + 1  # MATLAB 1-based in the internal logic
        if np.any(np.diff(iz) == 1):
            zer = (x == 0).astype(np.int64)
            dz = np.diff(np.concatenate([[0], zer, [0]]))
            debz = np.where(dz == 1)[0] + 1
            finz = np.where(dz == -1)[0]
            indz = np.round((debz + finz) / 2.0).astype(int)
        else:
            indz = iz.astype(int)
        indzer = np.sort(np.concatenate([indzer, indz - 1]))

    d = np.diff(x)
    n = d.size
    d1 = d[: n - 1]
    d2 = d[1:]
    indmin = np.where((d1 * d2 < 0) & (d1 < 0))[0] + 1
    indmax = np.where((d1 * d2 < 0) & (d1 > 0))[0] + 1

    if np.any(d == 0):
        imax = []
        imin = []
        bad = (d == 0).astype(np.int64)
        dd = np.diff(np.concatenate([[0], bad, [0]]))
        debs = np.where(dd == 1)[0] + 1
        fins = np.where(dd == -1)[0]

        if debs.size > 0 and debs[0] == 1:
            if debs.size > 1:
                debs = debs[1:]
                fins = fins[1:]
            else:
                debs = np.array([], dtype=int)
                fins = np.array([], dtype=int)

        if debs.size > 0 and fins[-1] == m:
            if debs.size > 1:
                debs = debs[:-1]
                fins = fins[:-1]
            else:
                debs = np.array([], dtype=int)
                fins = np.array([], dtype=int)

        for deb, fin in zip(debs, fins):
            if d[deb - 2] > 0:
                if d[fin - 1] < 0:
                    imax.append(int(round((fin + deb) / 2.0)))
            else:
                if d[fin - 1] > 0:
                    imin.append(int(round((fin + deb) / 2.0)))

        if imax:
            indmax = np.sort(np.concatenate([indmax, np.asarray(imax) - 1]))
        if imin:
            indmin = np.sort(np.concatenate([indmin, np.asarray(imin) - 1]))

    return indmin.astype(int), indmax.astype(int), indzer.astype(int)


def boundary_conditions(indmin, indmax, t, x, z, nbsym=2):
    indmin = np.asarray(indmin, dtype=int) + 1  # internal 1-based logic
    indmax = np.asarray(indmax, dtype=int) + 1
    t = _as_row_vector(t)
    x = _as_row_vector(x)
    z = _as_row_vector(z)
    lx = x.size

    if indmin.size + indmax.size < 3:
        raise ValueError("not enough extrema")

    if indmax[0] < indmin[0]:
        if x[0] > x[indmin[0] - 1]:
            lmax = np.flip(indmax[1 : min(indmax.size, nbsym + 1)])
            lmin = np.flip(indmin[: min(indmin.size, nbsym)])
            lsym = indmax[0]
        else:
            lmax = np.flip(indmax[: min(indmax.size, nbsym)])
            lmin = np.concatenate([np.flip(indmin[: min(indmin.size, nbsym - 1)]), np.array([1])])
            lsym = 1
    else:
        if x[0] < x[indmax[0] - 1]:
            lmax = np.flip(indmax[: min(indmax.size, nbsym)])
            lmin = np.flip(indmin[1 : min(indmin.size, nbsym + 1)])
            lsym = indmin[0]
        else:
            lmax = np.concatenate([np.flip(indmax[: min(indmax.size, nbsym - 1)]), np.array([1])])
            lmin = np.flip(indmin[: min(indmin.size, nbsym)])
            lsym = 1

    if indmax[-1] < indmin[-1]:
        if x[-1] < x[indmax[-1] - 1]:
            rmax = np.flip(indmax[max(indmax.size - nbsym, 0) :])
            rmin = np.flip(indmin[max(indmin.size - nbsym - 1, 0) : indmin.size - 1])
            rsym = indmin[-1]
        else:
            rmax = np.concatenate([np.array([lx]), np.flip(indmax[max(indmax.size - nbsym + 1, 0) :])])
            rmin = np.flip(indmin[max(indmin.size - nbsym, 0) :])
            rsym = lx
    else:
        if x[-1] > x[indmin[-1] - 1]:
            rmax = np.flip(indmax[max(indmax.size - nbsym - 1, 0) : indmax.size - 1])
            rmin = np.flip(indmin[max(indmin.size - nbsym, 0) :])
            rsym = indmax[-1]
        else:
            rmax = np.flip(indmax[max(indmax.size - nbsym, 0) :])
            rmin = np.concatenate([np.array([lx]), np.flip(indmin[max(indmin.size - nbsym + 1, 0) :])])
            rsym = lx

    tlmin = 2 * t[lsym - 1] - t[lmin - 1]
    tlmax = 2 * t[lsym - 1] - t[lmax - 1]
    trmin = 2 * t[rsym - 1] - t[rmin - 1]
    trmax = 2 * t[rsym - 1] - t[rmax - 1]

    if tlmin[0] > t[0] or tlmax[0] > t[0]:
        if lsym == indmax[0]:
            lmax = np.flip(indmax[: min(indmax.size, nbsym)])
        else:
            lmin = np.flip(indmin[: min(indmin.size, nbsym)])
        if lsym == 1:
            raise RuntimeError("bug")
        lsym = 1
        tlmin = 2 * t[lsym - 1] - t[lmin - 1]
        tlmax = 2 * t[lsym - 1] - t[lmax - 1]

    if trmin[-1] < t[lx - 1] or trmax[-1] < t[lx - 1]:
        if rsym == indmax[-1]:
            rmax = np.flip(indmax[max(indmax.size - nbsym, 0) :])
        else:
            rmin = np.flip(indmin[max(indmin.size - nbsym, 0) :])
        if rsym == lx:
            raise RuntimeError("bug")
        rsym = lx
        trmin = 2 * t[rsym - 1] - t[rmin - 1]
        trmax = 2 * t[rsym - 1] - t[rmax - 1]

    zlmax = z[lmax - 1]
    zlmin = z[lmin - 1]
    zrmax = z[rmax - 1]
    zrmin = z[rmin - 1]

    tmin = np.concatenate([tlmin, t[indmin - 1], trmin])
    tmax = np.concatenate([tlmax, t[indmax - 1], trmax])
    zmin = np.concatenate([zlmin, z[indmin - 1], zrmin])
    zmax = np.concatenate([zlmax, z[indmax - 1], zrmax])
    return tmin, tmax, zmin, zmax


def mean_and_amplitude(m, t, interp="spline"):
    m = _as_row_vector(m)
    t = _as_row_vector(t)
    indmin, indmax, indzer = extr(m)
    nem = indmin.size + indmax.size
    nzm = indzer.size
    tmin, tmax, mmin, mmax = boundary_conditions(indmin, indmax, t, m, m, 2)
    envmin = _interp1_matlab(tmin, mmin, t, interp)
    envmax = _interp1_matlab(tmax, mmax, t, interp)
    envmoy = (envmin + envmax) / 2.0
    amp = np.mean(np.abs(envmax - envmin), axis=0) / 2.0
    return envmoy, nem, nzm, amp


def stop_sifting(m, t, sd, sd2, tol, interp="spline"):
    try:
        envmoy, nem, nzm, amp = mean_and_amplitude(m, t, interp)
        amp = np.where(np.abs(amp) < EPS, np.inf, amp)
        sx = np.abs(envmoy) / amp
        s = np.mean(sx)
        stop = not (((np.mean(sx > sd) > tol) or np.any(sx > sd2)) and (nem > 2))
        stop = stop and not (abs(nzm - nem) > 1)
        return stop, envmoy, s
    except Exception:
        return True, np.zeros_like(m, dtype=np.float64), np.nan


def stop_emd(r):
    indmin, indmax, _ = extr(r)
    return (indmin.size + indmax.size) < 3


def emd(x: Sequence[float], opts: Optional[EMDOptions] = None):
    x = _as_row_vector(x)
    if opts is None:
        opts = EMDOptions()
    t = _as_row_vector(opts.t if opts.t is not None else np.arange(1, x.size + 1, dtype=np.float64))
    sd, sd2, tol = opts.stop
    maxiterations = int(opts.maxiterations)
    fixe = int(opts.fix)
    fixe_h = int(opts.fix_h)
    maxmodes = int(opts.maxmodes)
    interp = opts.interp

    if fixe and fixe_h:
        raise ValueError("cannot use both FIX and FIX_H modes")
    if fixe:
        maxiterations = fixe

    r = x.copy()
    imfs = []
    nbits = []
    total_nbit = 0
    k = 1

    while (not stop_emd(r)) and ((k < maxmodes + 1) or maxmodes == 0):
        m = r.copy()
        if fixe:
            stop_sift = False
            nbit = 0
            moyenne = np.zeros_like(m)
        else:
            stop_sift, moyenne, _ = stop_sifting(m, t, sd, sd2, tol, interp)
            nbit = 0

        if np.max(np.abs(m)) < 1e-10 * np.max(np.abs(x)):
            break

        while (not stop_sift) and (nbit < maxiterations):
            m = m - moyenne
            if fixe:
                nbit += 1
                if nbit >= fixe:
                    stop_sift = True
                    moyenne = np.zeros_like(m)
                else:
                    stop_sift = False
                    moyenne = mean_and_amplitude(m, t, interp)[0]
            elif fixe_h:
                # Rarely used on your path; kept simple but compatible in spirit.
                if nbit == 0:
                    stop_count = 0
                moyenne, nem, nzm, _ = mean_and_amplitude(m, t, interp)
                if abs(nzm - nem) > 1:
                    stop_count = 0
                    stop_sift = False
                else:
                    stop_count += 1
                    stop_sift = (stop_count == fixe_h)
            else:
                stop_sift, moyenne, _ = stop_sifting(m, t, sd, sd2, tol, interp)
            nbit += 1
            total_nbit += 1

        imfs.append(m.copy())
        nbits.append(nbit)
        r = r - m
        k += 1

    if np.any(r):
        imfs.append(r.copy())

    imf = np.vstack(imfs) if imfs else np.zeros((0, x.size), dtype=np.float64)
    ort = io(x, imf)
    return imf, ort, np.asarray(nbits, dtype=int)


def io(x: Sequence[float], imf: np.ndarray) -> float:
    x = _as_row_vector(x)
    if imf.size == 0:
        return 0.0
    n = imf.shape[0]
    s = 0.0
    denom = np.sum(x ** 2)
    if denom <= EPS:
        return 0.0
    for i in range(n):
        for j in range(n):
            if i != j:
                s += abs(np.sum(imf[i, :] * np.conj(imf[j, :])) / denom)
    return 0.5 * s


def emd_reduce_for_sim(hist: Sequence[float], start_idx: int) -> np.ndarray:
    """Match the SIM-Wiener use pattern: sum imfs(start_idx:end,:)."""
    imf, _, _ = emd(hist)
    if imf.shape[0] >= start_idx:
        return np.sum(imf[start_idx - 1 :, :], axis=0)
    return _as_row_vector(hist)

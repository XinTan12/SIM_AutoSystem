"""Focus metrics used by the pre-SIM z-scan autofocus step."""

from __future__ import annotations

import numpy as np


def sum_modified_laplacian(image: np.ndarray) -> float:
    """Return the Sum-Modified-Laplacian focus score for a 2-D image."""
    array = np.asarray(image)
    if array.ndim != 2:
        raise ValueError("sum_modified_laplacian expects a 2-D image.")
    data = array.astype(np.float64, copy=False)
    if min(data.shape) < 3:
        return 0.0

    mlx = np.abs((2.0 * data[1:-1, 1:-1]) - data[1:-1, :-2] - data[1:-1, 2:])
    mly = np.abs((2.0 * data[1:-1, 1:-1]) - data[:-2, 1:-1] - data[2:, 1:-1])
    return float(np.sum(mlx + mly))

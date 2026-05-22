import sys
import unittest
from pathlib import Path

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _box_blur(image: np.ndarray) -> np.ndarray:
    padded = np.pad(image.astype(np.float64), 1, mode="edge")
    return (
        padded[:-2, :-2]
        + padded[:-2, 1:-1]
        + padded[:-2, 2:]
        + padded[1:-1, :-2]
        + padded[1:-1, 1:-1]
        + padded[1:-1, 2:]
        + padded[2:, :-2]
        + padded[2:, 1:-1]
        + padded[2:, 2:]
    ) / 9.0


class FocusMetricTests(unittest.TestCase):
    def test_sum_modified_laplacian_scores_sharp_edges_above_blurred_edges(self):
        from sim_control.focus_metrics import sum_modified_laplacian

        sharp = np.zeros((64, 64), dtype=np.uint16)
        sharp[16:48, 16:48] = 4000
        blurred = sharp.astype(np.float64)
        for _ in range(5):
            blurred = _box_blur(blurred)

        self.assertGreater(sum_modified_laplacian(sharp), sum_modified_laplacian(blurred))

    def test_sum_modified_laplacian_rejects_non_2d_images(self):
        from sim_control.focus_metrics import sum_modified_laplacian

        with self.assertRaisesRegex(ValueError, "2-D"):
            sum_modified_laplacian(np.zeros((2, 4, 4), dtype=np.uint16))


if __name__ == "__main__":
    unittest.main()

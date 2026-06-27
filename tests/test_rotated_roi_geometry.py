from __future__ import annotations

import sys
from pathlib import Path

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from control_wangbo.roi_geometry import crop_rotated_roi, rotated_rect_points


def test_zero_degree_rotated_roi_matches_legacy_slice():
    frame = np.arange(100, dtype=np.uint16).reshape(10, 10)

    cropped = crop_rotated_roi(frame, 2, 3, 4, 5, 0)

    np.testing.assert_array_equal(cropped, frame[3:8, 2:6])


def test_rotated_roi_preserves_requested_output_shape_and_changes_content():
    frame = np.zeros((20, 20), dtype=np.uint16)
    frame[8:12, :] = 1000

    unrotated = crop_rotated_roi(frame, 5, 5, 10, 10, 0)
    rotated = crop_rotated_roi(frame, 5, 5, 10, 10, 90)

    assert rotated.shape == (10, 10)
    assert not np.array_equal(rotated, unrotated)


def test_rotated_rect_points_keep_roi_center():
    points = rotated_rect_points(10, 20, 30, 40, 33)

    np.testing.assert_allclose(points.mean(axis=0), np.array([25.0, 40.0]), atol=1e-5)

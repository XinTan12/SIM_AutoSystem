from __future__ import annotations

import math

import cv2
import numpy as np


def rotated_rect_points(x, y, width, height, angle_degrees):
    """Return clockwise corner points for an ROI rotated around its center."""
    width = max(1, int(round(width)))
    height = max(1, int(round(height)))
    cx = float(x) + width / 2.0
    cy = float(y) + height / 2.0
    half_w = width / 2.0
    half_h = height / 2.0
    corners = np.array(
        [
            [-half_w, -half_h],
            [half_w, -half_h],
            [half_w, half_h],
            [-half_w, half_h],
        ],
        dtype=np.float32,
    )
    angle = math.radians(float(angle_degrees or 0.0))
    cos_angle = math.cos(angle)
    sin_angle = math.sin(angle)
    rotation = np.array(
        [[cos_angle, -sin_angle], [sin_angle, cos_angle]],
        dtype=np.float32,
    )
    points = corners @ rotation.T
    points[:, 0] += cx
    points[:, 1] += cy
    return points.astype(np.float32)


def crop_rotated_roi(frame, x, y, width, height, angle_degrees):
    """Crop an ROI so the returned image is the actual rotated ROI content."""
    width = max(1, int(round(width)))
    height = max(1, int(round(height)))
    angle = float(angle_degrees or 0.0)
    if abs(angle % 360.0) < 1e-6:
        return frame[int(y) : int(y) + height, int(x) : int(x) + width].copy()

    source_points = rotated_rect_points(x, y, width, height, angle)
    target_points = np.array(
        [
            [0.0, 0.0],
            [width - 1.0, 0.0],
            [width - 1.0, height - 1.0],
            [0.0, height - 1.0],
        ],
        dtype=np.float32,
    )
    transform = cv2.getPerspectiveTransform(source_points, target_points)
    return cv2.warpPerspective(
        frame,
        transform,
        (width, height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )


def draw_rotated_roi(display_frame, x, y, width, height, angle_degrees, color_bgr, scale_x, scale_y):
    """Draw the same rotated ROI outline on a scaled display frame."""
    points = rotated_rect_points(
        float(x) * scale_x,
        float(y) * scale_y,
        float(width) * scale_x,
        float(height) * scale_y,
        angle_degrees,
    )
    points = np.round(points).astype(np.int32)
    cv2.polylines(display_frame, [points], True, color_bgr, 1)

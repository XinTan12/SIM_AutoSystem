from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_collected_roi_rotation_runtime_paths_removed():
    """Fast Camera Collected ROI should stay axis-aligned in UI/runtime code."""
    checked_files = [
        PROJECT_ROOT / "control_wangbo" / "main.py",
        PROJECT_ROOT / "control_wangbo" / "FastCameraThread.py",
        PROJECT_ROOT / "control_wangbo" / "CellSorting_ui.ui",
    ]
    forbidden = [
        "crop_rotated_roi",
        "draw_rotated_roi",
        "rotated_rect_points",
        "spb_collectedROI_angle",
        "label_collectedROI_angle",
        "collectedROI_angle",
        "roi_angles",
        "roi_angle",
    ]

    for path in checked_files:
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{token} should not remain in {path}"

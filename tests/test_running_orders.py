"""SLM Running Order 名称解析和选择规则测试。

用例验证 405/488/561/647 波长、曝光桶、3.5 pitch、2d 模式和非 _ang0 条件，防止正式采集选错预烧录 Running Order。
"""

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class RunningOrderSelectionTests(unittest.TestCase):
    """验证 SLM Running Order 名称解析和最佳匹配选择。"""
    def test_parse_running_order_name_extracts_fields(self):
        from sim_control.adapters import parse_running_order_name

        parsed = parse_running_order_name("488_3.5_2d_10ms")

        self.assertEqual(
            parsed,
            {
                "wavelength_nm": 488,
                "pitch": "3.5",
                "mode": "2d",
                "exposure_ms": 10,
                "single_angle": False,
            },
        )

    def test_parse_running_order_name_marks_single_angle_variant(self):
        from sim_control.adapters import parse_running_order_name

        parsed = parse_running_order_name("488_3.5_2d_10ms_ang0")

        self.assertTrue(parsed["single_angle"])

    def test_parse_running_order_name_rejects_malformed_names(self):
        from sim_control.adapters import parse_running_order_name

        self.assertIsNone(parse_running_order_name("488_3.5_2d"))
        self.assertIsNone(parse_running_order_name("bad_3.5_2d_10ms"))

    def test_find_best_running_order_uses_exposure_buckets_and_ignores_ang0(self):
        from sim_control.adapters import find_best_running_order

        running_orders = [
            (0, "488_3.5_2d_1ms_ang0"),
            (1, "488_3.5_2d_1ms"),
            (2, "488_3.5_2d_10ms"),
            (3, "488_3.5_2d_50ms"),
            (4, "488_4.0_2d_1ms"),
            (5, "488_3.5_3d_1ms"),
        ]

        cases = [
            (9_999, (1, "488_3.5_2d_1ms")),
            (10_000, (2, "488_3.5_2d_10ms")),
            (49_999, (2, "488_3.5_2d_10ms")),
            (50_000, (3, "488_3.5_2d_50ms")),
        ]
        for exposure_us, expected in cases:
            with self.subTest(exposure_us=exposure_us):
                index, name, warnings = find_best_running_order(running_orders, 488, exposure_us)
                self.assertEqual((index, name), expected)
                self.assertEqual(warnings, [])

    def test_find_best_running_order_reports_no_match(self):
        from sim_control.adapters import find_best_running_order

        index, name, warnings = find_best_running_order([(0, "488_3.5_2d_1ms_ang0")], 488, 5_000)

        self.assertIsNone(index)
        self.assertEqual(name, "")
        self.assertTrue(warnings)


if __name__ == "__main__":
    unittest.main()

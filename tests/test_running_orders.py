"""SLM Running Order 名称解析与选择规则测试。

作用：
    覆盖 ``adapters.parse_running_order_name`` 与 ``find_best_running_order`` 的核心
    用例：
        1. 名称解析正确（波长、pitch、mode、曝光桶、_ang0 标记）。
        2. ``_ang0`` 单角度变体被识别为 ``single_angle=True``。
        3. 非法名字返回 ``None``，不会污染候选列表。
        4. 选择规则严格按 (3.5, 2d, 非 ang0, 曝光桶) 四联条件筛选。
        5. 当没有匹配项时返回 ``(None, "", warnings)``，由 GUI 转为对话框文本。

协作关系：
    上游：``unittest``。
    下游：``sim_control.adapters.parse_running_order_name`` 与 ``find_best_running_order``。

维护要点：
    - 曝光桶规则：``<10 ms`` → 1ms 桶；``10..50 ms`` → 10ms 桶；``≥50 ms`` → 50ms 桶。
    - 修改命名约定时同步 ``adapters._RUNNING_ORDER_NAME_RE`` 正则与本测试。
"""

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class RunningOrderSelectionTests(unittest.TestCase):
    """覆盖 SLM Running Order 名称解析与最佳匹配选择。"""

    def test_parse_running_order_name_extracts_fields(self):
        """``"488_3.5_2d_10ms"`` 应被解析为 5 个结构化字段。"""
        from sim_control.adapters import parse_running_order_name

        parsed = parse_running_order_name("488_3.5_2d_10ms")

        # 严格相等：保护正则命名组数量与字段类型。
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
        """``_ang0`` 后缀必须把 ``single_angle`` 标为 True。"""
        from sim_control.adapters import parse_running_order_name

        parsed = parse_running_order_name("488_3.5_2d_10ms_ang0")

        self.assertTrue(parsed["single_angle"])

    def test_parse_running_order_name_rejects_malformed_names(self):
        """缺字段或非数字波长 → 返回 None；调用方 should skip。"""
        from sim_control.adapters import parse_running_order_name

        # 缺曝光后缀 / 波长非数字 都应返回 None。
        self.assertIsNone(parse_running_order_name("488_3.5_2d"))
        self.assertIsNone(parse_running_order_name("bad_3.5_2d_10ms"))

    def test_find_best_running_order_uses_exposure_buckets_and_ignores_ang0(self):
        """对每个曝光桶都正确选出非 _ang0 / 3.5 / 2d 的候选。"""
        from sim_control.adapters import find_best_running_order

        # 构造一个 6 项 RO 列表：包含 _ang0、非 3.5、非 2d 等"应被忽略"项。
        running_orders = [
            (0, "488_3.5_2d_1ms_ang0"),
            (1, "488_3.5_2d_1ms"),
            (2, "488_3.5_2d_10ms"),
            (3, "488_3.5_2d_50ms"),
            (4, "488_4.0_2d_1ms"),
            (5, "488_3.5_3d_1ms"),
        ]

        # 4 个曝光桶边界：9_999 落 1ms；10_000 → 10ms；49_999 → 10ms；50_000 → 50ms。
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
                # 找到候选时 warnings 应为空。
                self.assertEqual(warnings, [])

    def test_find_best_running_order_reports_no_match(self):
        """没匹配到 RO 时返回 (None, "", [warning, ...])。"""
        from sim_control.adapters import find_best_running_order

        # 列表里只有 _ang0 变体，因此筛选后必然为空。
        index, name, warnings = find_best_running_order([(0, "488_3.5_2d_1ms_ang0")], 488, 5_000)

        self.assertIsNone(index)
        self.assertEqual(name, "")
        # warnings 应至少有一条，便于 GUI 翻译。
        self.assertTrue(warnings)

    def test_find_best_running_order_prefers_638_over_legacy_647(self):
        """638 nm 请求应优先选择 638 RO，即使列表里也存在旧 647 命名。"""
        from sim_control.adapters import find_best_running_order

        running_orders = [
            (0, "647_3.5_2d_10ms"),
            (1, "638_3.5_2d_10ms"),
        ]

        index, name, warnings = find_best_running_order(running_orders, 638, 10_000)

        self.assertEqual((index, name), (1, "638_3.5_2d_10ms"))
        self.assertEqual(warnings, [])

    def test_find_best_running_order_falls_back_to_647_for_638_request(self):
        """现场 repertoire 未重命名时，638 nm 请求允许 fallback 到 647 RO（双向红光兜底）。"""
        from sim_control.adapters import find_best_running_order

        running_orders = [
            (0, "647_3.5_2d_10ms"),
            (1, "647_3.5_2d_10ms_ang0"),
        ]

        index, name, warnings = find_best_running_order(running_orders, 638, 10_000)

        self.assertEqual((index, name), (0, "647_3.5_2d_10ms"))
        # 文案按实际命中方向动态生成（不再有写死的 "legacy 647" 字样）。
        self.assertTrue(
            any("Using 647 nm" in warning and "requested 638 nm" in warning for warning in warnings)
        )

    def test_find_best_running_order_prefers_647_over_638(self):
        """647 nm 请求应优先选择 647 RO，即使列表里也存在新 638 命名（与 638 优先对称）。"""
        from sim_control.adapters import find_best_running_order

        running_orders = [
            (0, "638_3.5_2d_10ms"),
            (1, "647_3.5_2d_10ms"),
        ]

        index, name, warnings = find_best_running_order(running_orders, 647, 10_000)

        self.assertEqual((index, name), (1, "647_3.5_2d_10ms"))
        self.assertEqual(warnings, [])

    def test_find_best_running_order_falls_back_to_638_for_647_request(self):
        """647 nm 请求在只有 638 RO 时 fallback 到 638（与 638→647 fallback 对称）。"""
        from sim_control.adapters import find_best_running_order

        running_orders = [
            (0, "638_3.5_2d_10ms"),
            (1, "638_3.5_2d_10ms_ang0"),
        ]

        index, name, warnings = find_best_running_order(running_orders, 647, 10_000)

        self.assertEqual((index, name), (0, "638_3.5_2d_10ms"))
        # fallback 方向反过来：命中 638、请求 647。
        self.assertTrue(
            any("Using 638 nm" in warning and "requested 647 nm" in warning for warning in warnings)
        )


    def test_find_z_scan_running_order_accepts_only_488_zscan3p_presets(self):
        """Z-scan 只能选择 488 nm 专用三相位 RO，且按固定 preset 精确匹配。"""
        from sim_control.adapters import find_z_scan_running_order, parse_z_scan_running_order_name

        running_orders = [
            (0, "488_3.5_2d_10ms"),
            (1, "561_3.5_2d_zscan3p_8ms"),
            (2, "488_3.5_2d_zscan3p_5ms"),
            (3, "488_3.5_2d_zscan3p_8ms"),
            (4, "488_4.0_2d_zscan3p_8ms"),
        ]

        parsed = parse_z_scan_running_order_name("488_3.5_2d_zscan3p_8ms")
        self.assertEqual(
            parsed,
            {
                "wavelength_nm": 488,
                "pitch": "3.5",
                "mode": "2d",
                "exposure_preset_ms": 8,
            },
        )

        index, name, warnings = find_z_scan_running_order(running_orders, exposure_preset_ms=8)

        self.assertEqual((index, name), (3, "488_3.5_2d_zscan3p_8ms"))
        self.assertEqual(warnings, [])

    def test_find_z_scan_running_order_reports_missing_preset(self):
        """缺少指定 z-scan preset 时应返回 warning，而不是回退到正式 SIM9 RO。"""
        from sim_control.adapters import find_z_scan_running_order

        index, name, warnings = find_z_scan_running_order(
            [(0, "488_3.5_2d_10ms"), (1, "488_3.5_2d_zscan3p_5ms")],
            exposure_preset_ms=14,
        )

        self.assertIsNone(index)
        self.assertEqual(name, "")
        self.assertTrue(any("z-scan" in warning.lower() for warning in warnings))


if __name__ == "__main__":
    unittest.main()

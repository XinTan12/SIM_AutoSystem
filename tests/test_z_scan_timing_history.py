import json
import sys
import unittest
import uuid
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class ZScanTimingHistoryTests(unittest.TestCase):
    def test_jsonl_history_round_trips_timing_records(self):
        from sim_control.z_scan_timing_history import (
            ZScanTimingRecord,
            append_z_scan_timing_records,
            load_z_scan_timing_records,
        )

        tmp_root = PROJECT_ROOT / ".codex_tmp_pyc"
        tmp_root.mkdir(exist_ok=True)
        history_path = tmp_root / f"z_scan_timing_history_{uuid.uuid4().hex}.jsonl"
        append_z_scan_timing_records(
            [
                ZScanTimingRecord(
                    timestamp="2026-05-25T10:00:00",
                    mode="zscan_stage_only",
                    scan_gap_nm=300.0,
                    num_steps=3,
                    exposure_preset_ms=8,
                    direction="positive_z",
                    move_index=1,
                    total_moves=4,
                    from_z_um=1.0,
                    to_z_um=1.3,
                    distance_um=0.3,
                    move_ms=12.5,
                    cycle_ms=None,
                    success=True,
                )
            ],
            history_path,
        )

        records = load_z_scan_timing_records(history_path)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].scan_gap_nm, 300.0)
        self.assertEqual(records[0].move_ms, 12.5)
        self.assertIsNone(records[0].cycle_ms)

    def test_legacy_step_history_without_record_type_loads_as_step_record(self):
        from sim_control.z_scan_timing_history import ZScanTimingRecord, load_z_scan_timing_records

        tmp_root = PROJECT_ROOT / ".codex_tmp_pyc"
        tmp_root.mkdir(exist_ok=True)
        history_path = tmp_root / f"z_scan_timing_history_{uuid.uuid4().hex}.jsonl"
        legacy_payload = {
            "timestamp": "2026-05-25T10:00:00",
            "mode": "zscan_stage_only",
            "scan_gap_nm": 300.0,
            "num_steps": 3,
            "exposure_preset_ms": 8,
            "direction": "positive_z",
            "move_index": 2,
            "total_moves": 4,
            "from_z_um": 1.0,
            "to_z_um": 1.3,
            "distance_um": 0.3,
            "move_ms": 22.5,
            "cycle_ms": None,
            "success": True,
        }
        history_path.write_text(json.dumps(legacy_payload, ensure_ascii=False) + "\n", encoding="utf-8")

        records = load_z_scan_timing_records(history_path)

        self.assertEqual(len(records), 1)
        self.assertIsInstance(records[0], ZScanTimingRecord)
        self.assertEqual(records[0].record_type, "step")
        self.assertEqual(records[0].move_ms, 22.5)

    def test_move_estimate_uses_exact_gap_before_distance_fit_and_default(self):
        from sim_control.z_scan_timing_history import (
            DEFAULT_Z_SCAN_MOVE_MS,
            ZScanTimingRecord,
            estimate_z_scan_move_ms,
        )

        records_with_exact = [
            ZScanTimingRecord.for_test(scan_gap_nm=200.0, move_ms=20.0),
            ZScanTimingRecord.for_test(scan_gap_nm=600.0, move_ms=60.0),
            ZScanTimingRecord.for_test(scan_gap_nm=400.0, move_ms=100.0),
            ZScanTimingRecord.for_test(scan_gap_nm=400.0, move_ms=100.0),
        ]
        distance_records = [
            ZScanTimingRecord.for_test(scan_gap_nm=200.0, move_ms=20.0),
            ZScanTimingRecord.for_test(scan_gap_nm=600.0, move_ms=60.0),
        ]

        self.assertAlmostEqual(estimate_z_scan_move_ms(400.0, records_with_exact), 100.0)
        self.assertAlmostEqual(estimate_z_scan_move_ms(300.0, distance_records), 30.0)
        self.assertAlmostEqual(estimate_z_scan_move_ms(300.0, []), DEFAULT_Z_SCAN_MOVE_MS)

    def test_move_only_run_summary_estimate_reconstructs_total_and_scales_moves(self):
        from sim_control.models import ZScanConfig
        from sim_control.z_scan_timing_history import (
            ZScanTimingRunRecord,
            estimate_z_scan_move_only_total_time_ms,
        )

        records = [
            ZScanTimingRunRecord.for_test(
                mode="zscan_stage_only",
                scan_gap_nm=300.0,
                num_steps=10,
                total_duration_ms=260.1,
                initial_position_ms=0.1,
                scan_move_ms_sum=220.0,
                scan_move_ms_mean=22.0,
                scan_move_count=10,
                restore_ms=35.0,
                fixed_overhead_ms=5.0,
            )
        ]

        exact = estimate_z_scan_move_only_total_time_ms(
            ZScanConfig(step_um=0.3, num_steps=10),
            records=records,
        )
        more_moves = estimate_z_scan_move_only_total_time_ms(
            ZScanConfig(step_um=0.3, num_steps=12),
            records=records,
        )

        self.assertAlmostEqual(exact, 260.1)
        self.assertGreater(more_moves, exact)

    def test_move_only_gap_model_uses_run_summary_before_legacy_steps(self):
        from sim_control.models import ZScanConfig
        from sim_control.z_scan_timing_history import (
            ZScanTimingRecord,
            ZScanTimingRunRecord,
            estimate_z_scan_move_only_total_time_ms,
        )

        records = [
            ZScanTimingRecord.for_test(scan_gap_nm=300.0, move_ms=100.0),
            ZScanTimingRunRecord.for_test(
                mode="zscan_stage_only",
                scan_gap_nm=200.0,
                num_steps=5,
                total_duration_ms=60.0,
                initial_position_ms=0.0,
                scan_move_ms_sum=50.0,
                scan_move_ms_mean=10.0,
                scan_move_count=5,
                restore_ms=10.0,
                fixed_overhead_ms=0.0,
            ),
            ZScanTimingRunRecord.for_test(
                mode="zscan_stage_only",
                scan_gap_nm=600.0,
                num_steps=5,
                total_duration_ms=180.0,
                initial_position_ms=0.0,
                scan_move_ms_sum=150.0,
                scan_move_ms_mean=30.0,
                scan_move_count=5,
                restore_ms=30.0,
                fixed_overhead_ms=0.0,
            ),
        ]

        eta = estimate_z_scan_move_only_total_time_ms(
            ZScanConfig(step_um=0.4, num_steps=5),
            records=records,
        )

        self.assertAlmostEqual(eta, 120.0)

    def test_capture_run_summary_estimate_uses_capture_modules(self):
        from sim_control.models import DaqLineConfig, TimingConfig, ZScanConfig
        from sim_control.z_scan_timing_history import (
            ZScanTimingRunRecord,
            estimate_z_scan_capture_test_total_time_ms,
        )

        records = [
            ZScanTimingRunRecord.for_test(
                mode="zscan_stage_plus_capture",
                scan_gap_nm=300.0,
                num_steps=2,
                exposure_preset_ms=8,
                total_duration_ms=160.0,
                initial_position_ms=1.0,
                scan_move_ms_sum=20.0,
                scan_move_ms_mean=10.0,
                scan_move_count=2,
                capture_nonmove_ms_sum=90.0,
                capture_nonmove_ms_mean=30.0,
                best_focus_move_ms=5.0,
                tiff_write_ms=7.0,
                restore_ms=9.0,
                fixed_overhead_ms=28.0,
            )
        ]

        eta = estimate_z_scan_capture_test_total_time_ms(
            ZScanConfig(step_um=0.3, num_steps=2, exposure_preset_ms=8),
            daq_config=DaqLineConfig(),
            timing=TimingConfig(),
            records=records,
        )

        self.assertAlmostEqual(eta, 160.0)

    def test_total_eta_changes_with_gap_and_moves_from_history(self):
        from sim_control.models import DaqLineConfig, TimingConfig, ZScanConfig
        from sim_control.z_scan_timing_history import ZScanTimingRecord, estimate_z_scan_total_time_ms

        records = [
            ZScanTimingRecord.for_test(scan_gap_nm=200.0, move_ms=20.0),
            ZScanTimingRecord.for_test(scan_gap_nm=600.0, move_ms=60.0),
        ]
        daq = DaqLineConfig()
        timing = TimingConfig(sample_rate_hz=1_000_000, slm_enable_guard_us=100)

        gap_200_moves_3 = estimate_z_scan_total_time_ms(
            ZScanConfig(step_um=0.2, num_steps=3, exposure_preset_ms=8),
            daq_config=daq,
            timing=timing,
            records=records,
        )
        gap_200_moves_5 = estimate_z_scan_total_time_ms(
            ZScanConfig(step_um=0.2, num_steps=5, exposure_preset_ms=8),
            daq_config=daq,
            timing=timing,
            records=records,
        )
        gap_600_moves_3 = estimate_z_scan_total_time_ms(
            ZScanConfig(step_um=0.6, num_steps=3, exposure_preset_ms=8),
            daq_config=daq,
            timing=timing,
            records=records,
        )

        self.assertGreater(gap_200_moves_5, gap_200_moves_3)
        self.assertGreater(gap_600_moves_3, gap_200_moves_3)

    def test_cycle_history_with_matching_exposure_overrides_move_model(self):
        from sim_control.models import DaqLineConfig, TimingConfig, ZScanConfig
        from sim_control.z_scan_timing_history import ZScanTimingRecord, estimate_z_scan_total_time_ms

        records = [
            ZScanTimingRecord.for_test(
                scan_gap_nm=300.0,
                move_ms=15.0,
                cycle_ms=90.0,
                exposure_preset_ms=8,
            ),
            ZScanTimingRecord.for_test(
                scan_gap_nm=300.0,
                move_ms=15.0,
                cycle_ms=110.0,
                exposure_preset_ms=8,
            ),
        ]

        eta_ms = estimate_z_scan_total_time_ms(
            ZScanConfig(step_um=0.3, num_steps=4, exposure_preset_ms=8),
            daq_config=DaqLineConfig(),
            timing=TimingConfig(sample_rate_hz=1_000_000, slm_enable_guard_us=100),
            records=records,
        )

        self.assertAlmostEqual(eta_ms, 500.0)


if __name__ == "__main__":
    unittest.main()

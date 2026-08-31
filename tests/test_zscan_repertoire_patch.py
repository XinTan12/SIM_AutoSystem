"""Regression tests for patching z-scan Running Orders into R11 repertoires."""

from __future__ import annotations

import shutil
import re
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


BASE_REP_TEXT = """ID
"V1.0"
ID_END

SEQUENCES
A+ "48030 500us 1-bit Lit Pair +.seq11"
A- "48030 500us 1-bit Lit Pair -.seq11"
B+ "48090 5ms 1-bit Lit Pair +.seq11"
B- "48090 5ms 1-bit Lit Pair -.seq11"
C+ "48093 25ms 1-bit Lit Pair +.seq11"
C- "48093 25ms 1-bit Lit Pair -.seq11"
F+ "48037 1ms 1-bit Lit Pair +.seq11"
F- "48037 1ms 1-bit Lit Pair -.seq11"
G+ "48038 2ms 1-bit Lit Pair +.seq11"
G- "48038 2ms 1-bit Lit Pair -.seq11"
D+ "48039 3ms 1-bit Lit Pair +.seq11"
D- "48039 3ms 1-bit Lit Pair -.seq11"
SEQUENCES_END

IMAGES
1 "D3.5L488Ang94DT50F200M0_001.png"
IMAGES_END
"""


class ZScanRepertoirePatchTests(unittest.TestCase):
    """Keep generated z-scan sequence aliases inside R11 compiler limits."""

    def test_zscan_blocks_cover_all_wavelengths_presets_and_pattern_indices(self):
        from tools import add_zscan_ro

        add_zscan_ro._validate_sequence_aliases(BASE_REP_TEXT)
        updated = add_zscan_ro._ensure_zscan_blocks(BASE_REP_TEXT)

        expected_aliases = {5: "A", 8: "F", 14: "G", 20: "D"}
        expected_indices = {405: (0, 1, 2), 488: (9, 10, 11), 561: (18, 19, 20), 647: (27, 28, 29)}
        for wavelength, indices in expected_indices.items():
            for preset, alias in expected_aliases.items():
                with self.subTest(wavelength=wavelength, preset=preset):
                    marker = f'"{wavelength}_3.5_2d_zscan3p_{preset}ms"'
                    block = updated.split(marker, maxsplit=1)[1].split("]", maxsplit=1)[0]
                    for index in indices:
                        self.assertIn(f"({alias}+,{index})", block)
                        self.assertIn(f"({alias}-,{index})", block)
                    self.assertNotIn("FINISH", block.upper())
                    self.assertNotIn("48088", block)

        self.assertEqual(updated.count("zscan3p_"), 16)

    def test_sequence_alias_validation_fails_closed_without_adding_alias_or_files(self):
        from tools import add_zscan_ro

        missing = BASE_REP_TEXT.replace('G- "48038 2ms 1-bit Lit Pair -.seq11"\n', "")
        with self.assertRaisesRegex(RuntimeError, "G-"):
            add_zscan_ro._validate_sequence_aliases(missing)

        self.assertNotIn("48088", add_zscan_ro._ensure_zscan_blocks(BASE_REP_TEXT))

    def test_duplicate_running_order_names_are_rejected(self):
        from tools import add_zscan_ro

        once = add_zscan_ro._ensure_zscan_blocks(BASE_REP_TEXT)
        marker = '\n "488_3.5_2d_zscan3p_8ms"\n'
        start = once.index(marker)
        end = once.index("\n]", start) + len("\n]")
        duplicate = once + once[start:end]

        with self.assertRaisesRegex(RuntimeError, "Duplicate.*488_3.5_2d_zscan3p_8ms"):
            add_zscan_ro._running_order_names(duplicate)

    def test_missing_sequence_file_entry_fails_closed_without_changing_archive(self):
        from tools import add_zscan_ro

        source_repz = Path("patterns/2d_3.5.repz11")
        missing_filename = next(iter(add_zscan_ro.SEQUENCE_ALIASES.values()))
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "missing-sequence.repz11"
            with zipfile.ZipFile(source_repz, "r") as src, zipfile.ZipFile(
                target, "w", compression=zipfile.ZIP_DEFLATED
            ) as dst:
                dst.comment = src.comment
                for info in src.infolist():
                    if info.filename != missing_filename:
                        dst.writestr(info, src.read(info.filename))
            before = target.read_bytes()

            with self.assertRaisesRegex(RuntimeError, re.escape(missing_filename)):
                add_zscan_ro.patch_repertoire(target)

            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(list(target.parent.glob(f".{target.name}.*.tmp")), [])

    def test_atomic_replace_failure_keeps_original_and_cleans_temp_file(self):
        from tools import add_zscan_ro

        source_repz = Path("patterns/2d_3.5.repz11")
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "atomic.repz11"
            with zipfile.ZipFile(source_repz, "r") as src:
                entries = {info.filename: src.read(info.filename) for info in src.infolist()}
                infos = list(src.infolist())
            rep_name = next(name for name in entries if name.endswith(".rep"))
            rep_text = entries[rep_name].decode("utf-8")
            for wavelength in (405, 561, 647):
                for preset in (5, 8, 14, 20):
                    marker = f'\n "{wavelength}_3.5_2d_zscan3p_{preset}ms"\n'
                    start = rep_text.index(marker)
                    end = rep_text.index("\n]", start) + len("\n]")
                    rep_text = rep_text[:start] + rep_text[end:]
            entries[rep_name] = rep_text.encode("utf-8")
            with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as dst:
                dst.comment = b"preserve-this-comment"
                for info in infos:
                    dst.writestr(info, entries[info.filename])
            before = target.read_bytes()

            with mock.patch.object(add_zscan_ro.os, "replace", side_effect=OSError("replace failed")), \
                    mock.patch.object(add_zscan_ro.os, "fsync", wraps=add_zscan_ro.os.fsync) as fsync_mock:
                with self.assertRaisesRegex(OSError, "replace failed"):
                    add_zscan_ro.patch_repertoire(target)

            self.assertTrue(fsync_mock.called)
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(list(target.parent.glob(f".{target.name}.*.tmp")), [])

    def test_legacy_alias_migration_only_rewrites_zscan_20ms_block(self):
        from tools import add_zscan_ro

        rep_text = (
            BASE_REP_TEXT
            + '\n "debug_h_alias_should_remain"\n'
            + "[HWA h\n"
            + "<GPO=0 t.wait(20) GPO=2 (H+,0) (H-,0)  >\n"
            + "]\n"
            + '\n "488_3.5_2d_zscan3p_20ms"\n'
            + "[HWA h\n"
            + "GPO3 GPO2 GPO1\n"
            + "<GPO=0 t.wait(20) GPO=2 (H+,9) (H-,9) (H+,10) (H-,10) (H+,11) (H-,11)  >\n"
            + "]\n"
        )

        updated = add_zscan_ro._rewrite_legacy_zscan_aliases(rep_text)

        debug_block = updated.split('"debug_h_alias_should_remain"', maxsplit=1)[1].split("]", maxsplit=1)[0]
        zscan_20ms = updated.split('"488_3.5_2d_zscan3p_20ms"', maxsplit=1)[1].split("]", maxsplit=1)[0]
        self.assertIn("(H+,0)", debug_block)
        self.assertIn("(H-,0)", debug_block)
        self.assertIn("(D+,9)", zscan_20ms)
        self.assertIn("(D-,11)", zscan_20ms)
        self.assertNotIn("(H+", zscan_20ms)
        self.assertNotIn("(H-", zscan_20ms)

    def test_patch_repertoire_rejects_archive_missing_required_alias(self):
        from tools import add_zscan_ro

        source_repz = Path("patterns/2d_3.5.repz11")
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_repz = Path(temp_dir) / "bad.repz11"
            shutil.copy2(source_repz, temp_repz)

            with zipfile.ZipFile(temp_repz, "r") as src:
                entries = {info.filename: src.read(info.filename) for info in src.infolist()}
            rep_name = next(name for name in entries if name.endswith(".rep"))
            bad_text = entries[rep_name].decode("utf-8")
            bad_text = bad_text.replace('D+ "48039 3ms 1-bit Lit Pair +.seq11"\n', "")
            entries[rep_name] = bad_text.encode("utf-8")
            with zipfile.ZipFile(temp_repz, "w", compression=zipfile.ZIP_DEFLATED) as dst:
                for name, data in entries.items():
                    dst.writestr(name, data)

            with self.assertRaisesRegex(RuntimeError, "D\\+"):
                add_zscan_ro.patch_repertoire(temp_repz)

    def test_patch_repertoire_is_idempotent_preserves_entries_and_adds_twelve_ros(self):
        from tools import add_zscan_ro

        source_repz = Path("patterns/2d_3.5.repz11")
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_repz = Path(temp_dir) / "copy.repz11"
            shutil.copy2(source_repz, temp_repz)
            with zipfile.ZipFile(temp_repz, "r") as src:
                raw_entries = {info.filename: src.read(info.filename) for info in src.infolist()}
            before_entries = set(raw_entries)
            rep_name = next(name for name in before_entries if name.endswith(".rep"))
            before_text = raw_entries[rep_name].decode("utf-8")
            # 构造真实的 68-RO 输入：保留旧 488 四档，只移除本次新增的 12 块。
            for wavelength in (405, 561, 647):
                for preset in (5, 8, 14, 20):
                    marker = f'\n "{wavelength}_3.5_2d_zscan3p_{preset}ms"\n'
                    start = before_text.find(marker)
                    self.assertGreaterEqual(start, 0, marker)
                    end = before_text.find("\n]", start)
                    self.assertGreaterEqual(end, 0, marker)
                    before_text = before_text[:start] + before_text[end + len("\n]"):]
            raw_entries[rep_name] = before_text.encode("utf-8")
            with zipfile.ZipFile(temp_repz, "w", compression=zipfile.ZIP_DEFLATED) as dst:
                dst.comment = b"zscan-archive-comment"
                for name, data in raw_entries.items():
                    dst.writestr(name, data)
            before_ro_names = add_zscan_ro._running_order_names(before_text)
            self.assertEqual(len(before_ro_names), 68)

            add_zscan_ro.patch_repertoire(temp_repz)
            first_bytes = temp_repz.read_bytes()
            add_zscan_ro.patch_repertoire(temp_repz)
            second_bytes = temp_repz.read_bytes()

            with zipfile.ZipFile(temp_repz, "r") as patched:
                after_entries = set(patched.namelist())
                after_text = patched.read(rep_name).decode("utf-8")
                after_comment = patched.comment
            self.assertEqual(after_entries, before_entries)
            self.assertEqual(second_bytes, first_bytes)
            self.assertTrue(before_ro_names <= add_zscan_ro._running_order_names(after_text))
            self.assertEqual(len(add_zscan_ro._running_order_names(after_text)), 80)
            self.assertEqual(len(add_zscan_ro._running_order_names(after_text) - before_ro_names), 12)
            self.assertEqual(after_text.count("zscan3p_"), 16)
            self.assertEqual(after_comment, b"zscan-archive-comment")

    def test_current_archive_uses_d_aliases_for_zscan_20ms(self):
        repz_path = Path("patterns/2d_3.5.repz11")
        with zipfile.ZipFile(repz_path, "r") as repz:
            rep_name = next(name for name in repz.namelist() if name.endswith(".rep"))
            rep_text = repz.read(rep_name).decode("utf-8")

        self.assertIn('D+ "48039 3ms 1-bit Lit Pair +.seq11"', rep_text)
        self.assertIn('D- "48039 3ms 1-bit Lit Pair -.seq11"', rep_text)
        self.assertNotIn('H+ "48039 3ms 1-bit Lit Pair +.seq11"', rep_text)
        self.assertNotIn('H- "48039 3ms 1-bit Lit Pair -.seq11"', rep_text)
        zscan_20ms = rep_text.split('"488_3.5_2d_zscan3p_20ms"', maxsplit=1)[1]
        zscan_20ms = zscan_20ms.split("]", maxsplit=1)[0]
        self.assertIn("(D+,9)", zscan_20ms)
        self.assertIn("(D-,11)", zscan_20ms)
        self.assertNotIn("(H+", zscan_20ms)
        self.assertNotIn("(H-", zscan_20ms)

    def test_current_archive_contains_all_80_running_orders_and_16_zscan_blocks(self):
        from tools import add_zscan_ro

        repz_path = Path("patterns/2d_3.5.repz11")
        with zipfile.ZipFile(repz_path, "r") as repz:
            rep_name = next(name for name in repz.namelist() if name.endswith(".rep"))
            rep_text = repz.read(rep_name).decode("utf-8")

        self.assertEqual(len(add_zscan_ro._running_order_names(rep_text)), 80)
        self.assertEqual(rep_text.count("zscan3p_"), 16)
        self.assertNotIn("48088", rep_text)


if __name__ == "__main__":
    unittest.main()

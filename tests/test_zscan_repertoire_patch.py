"""Regression tests for patching z-scan Running Orders into R11 repertoires."""

from __future__ import annotations

import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path


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
D "48088 100us 1-bit Lit Balanced.seq11"
E "48042 300us 1-bit Balanced.seq11"
SEQUENCES_END

IMAGES
1 "D3.5L488Ang94DT50F200M0_001.png"
IMAGES_END
"""


class ZScanRepertoirePatchTests(unittest.TestCase):
    """Keep generated z-scan sequence aliases inside R11 compiler limits."""

    def test_zscan_20ms_does_not_use_out_of_range_h_sequence_aliases(self):
        from tools import add_zscan_ro

        updated = add_zscan_ro._ensure_sequence_aliases(BASE_REP_TEXT)
        updated = add_zscan_ro._ensure_zscan_blocks(updated)

        self.assertIn('D+ "48039 3ms 1-bit Lit Pair +.seq11"', updated)
        self.assertIn('D- "48039 3ms 1-bit Lit Pair -.seq11"', updated)
        self.assertNotIn('H+ "48039 3ms 1-bit Lit Pair +.seq11"', updated)
        self.assertNotIn('H- "48039 3ms 1-bit Lit Pair -.seq11"', updated)

        zscan_20ms = updated.split('"488_3.5_2d_zscan3p_20ms"', maxsplit=1)[1]
        zscan_20ms = zscan_20ms.split("]", maxsplit=1)[0]
        self.assertIn("(D+,9)", zscan_20ms)
        self.assertIn("(D-,11)", zscan_20ms)
        self.assertNotIn("(H+", zscan_20ms)
        self.assertNotIn("(H-", zscan_20ms)

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

    def test_patch_repertoire_migrates_existing_bad_archive(self):
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
            bad_text = bad_text.replace('D- "48039 3ms 1-bit Lit Pair -.seq11"\n', "")
            bad_text = bad_text.replace(
                "SEQUENCES_END",
                'H+ "48039 3ms 1-bit Lit Pair +.seq11"\n'
                'H- "48039 3ms 1-bit Lit Pair -.seq11"\n'
                "SEQUENCES_END",
                1,
            )
            bad_text = bad_text.replace("(D+,", "(H+,").replace("(D-,", "(H-,")
            entries[rep_name] = bad_text.encode("utf-8")
            with zipfile.ZipFile(temp_repz, "w", compression=zipfile.ZIP_DEFLATED) as dst:
                for name, data in entries.items():
                    dst.writestr(name, data)

            add_zscan_ro.patch_repertoire(temp_repz)

            with zipfile.ZipFile(temp_repz, "r") as patched:
                patched_text = patched.read(rep_name).decode("utf-8")
            self.assertIn('D+ "48039 3ms 1-bit Lit Pair +.seq11"', patched_text)
            self.assertIn('D- "48039 3ms 1-bit Lit Pair -.seq11"', patched_text)
            self.assertNotIn('H+ "48039 3ms 1-bit Lit Pair +.seq11"', patched_text)
            self.assertNotIn('H- "48039 3ms 1-bit Lit Pair -.seq11"', patched_text)
            zscan_20ms = patched_text.split('"488_3.5_2d_zscan3p_20ms"', maxsplit=1)[1]
            zscan_20ms = zscan_20ms.split("]", maxsplit=1)[0]
            self.assertIn("(D+,9)", zscan_20ms)
            self.assertIn("(D-,11)", zscan_20ms)
            self.assertNotIn("(H+", zscan_20ms)
            self.assertNotIn("(H-", zscan_20ms)

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


if __name__ == "__main__":
    unittest.main()

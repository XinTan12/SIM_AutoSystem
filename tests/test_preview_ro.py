"""Regression tests for tools/add_preview_ro.py (sample-finding immediate RO skeletons)."""

from __future__ import annotations

import re
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BEFORE_MERGE = REPO_ROOT / "patterns" / "2d_3.5_BEFORE_MERGE.repz11"

# 合成 28-RO target：12 别名（A+/A- 指向 48030 ±，匹配脚本前置/自校验），36 图，28 RO。
ALIASES = [
    ("A+", "48030 500us 1-bit Lit Pair +.seq11"),
    ("A-", "48030 500us 1-bit Lit Pair -.seq11"),
    ("B+", "seq B+.seq11"), ("B-", "seq B-.seq11"),
    ("C+", "seq C+.seq11"), ("C-", "seq C-.seq11"),
    ("F+", "seq F+.seq11"), ("F-", "seq F-.seq11"),
    ("G+", "seq G+.seq11"), ("G-", "seq G-.seq11"),
    ("D+", "seq D+.seq11"), ("D-", "seq D-.seq11"),
]


def _synthetic_target_rep() -> str:
    lines = [
        "ID", '"V1.0"', "ID_END", "",
        "PLATFORM", '"R11"', "PLATFORM_END", "",
        "DISPLAY", '"QXGA"', "DISPLAY_END", "",
        "FORMATVERSION", '"FV4"', "FORMATVERSION_END", "",
        "SEQUENCES",
    ]
    lines += [f'{a} "{v}"' for a, v in ALIASES]
    lines.append("SEQUENCES_END")
    lines.append("")
    lines.append("IMAGES")
    for i in range(36):  # 9-17 标记 488（脚本只按全局索引引用，名字不影响逻辑）
        tag = "D3.5L488" if 9 <= i <= 17 else "D3.5Lxxx"
        lines.append(f'1 "{tag}_{i:03d}.png"')
    lines.append("IMAGES_END")
    text = "\n".join(lines) + "\n"
    text += '\nDEFAULT "405_3.5_2d_50ms"\n[HWA h\n\nGPO3 GPO2 GPO1\n<GPO=0 t.wait(20) GPO=2 {f (C+,0) (C-,0) }  >\n\n]\n'
    for i in range(1, 28):  # 27 个普通正式 RO（共 28 含 DEFAULT）
        text += f'\n "formal_ro_{i}"\n[HWA h\n\n<GPO=0 t.wait(20) GPO=2 {{f (C+,0) }}  >\n]\n'
    return text


def _make_target(path: Path, rep_text: str | None = None) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("2d_3.5.rep", rep_text if rep_text is not None else _synthetic_target_rep())
        for i in range(36):
            z.writestr(f"img{i:03d}.png", b"\x89PNG\r\n\x1a\n" + bytes([i % 256]))


def _read_rep(path: Path) -> str:
    with zipfile.ZipFile(path, "r") as z:
        rep = next(n for n in z.namelist() if n.lower().endswith(".rep"))
        return z.read(rep).decode("utf-8")


def _ro_block(text: str, name: str) -> str:
    s = text.find(f'"{name}"')
    if s < 0:
        raise AssertionError(f"RO {name} not found")
    e = text.find("\n]", s)
    if e < 0:
        raise AssertionError(f"Malformed RO block for {name}")
    return text[s:e + 2]


class AddPreviewRoTests(unittest.TestCase):
    def test_adds_40_pair_ros_no_new_seq_or_img(self):
        from tools import add_preview_ro

        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "2d_3.5.repz11"
            _make_target(target)
            before = add_preview_ro._all_ro_names(_read_rep(target))
            add_preview_ro.add_preview_ro(target)
            text = _read_rep(target)
            names = add_preview_ro._all_ro_names(text)

            self.assertEqual(len(before), 28, "合成 target 应有 28 个 RO")
            self.assertEqual(len(add_preview_ro.PREVIEW_RO_NAMES), 40)
            self.assertEqual(len(names), 68, "追加后应为 68 个 RO")
            self.assertEqual(len(add_preview_ro._alias_lines(text)), 12, "不得新增序列别名")
            self.assertEqual(add_preview_ro._image_count(text), 36, "不得新增图像")
            for n in add_preview_ro.PREVIEW_RO_NAMES:
                self.assertIn(n, names)
            am = add_preview_ro._alias_map(text)
            self.assertEqual(am["A+"], "48030 500us 1-bit Lit Pair +.seq11")
            self.assertEqual(am["A-"], "48030 500us 1-bit Lit Pair -.seq11")
            self.assertEqual(text.count("DEFAULT "), 1)

    def test_pair_frames_with_spaces_and_no_singleton(self):
        from tools import add_preview_ro

        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "2d_3.5.repz11"
            _make_target(target)
            add_preview_ro.add_preview_ro(target)
            text = _read_rep(target)

            for wavelength, base in add_preview_ro.PREVIEW_IMAGE_BASES:
                self.assertIn(
                    f"<(A+,{base}) (A-,{base}) >",
                    _ro_block(text, f"{wavelength}_3.5_2d_imm_f1"),
                )
                self.assertIn(
                    f"<(A+,{base + 8}) (A-,{base + 8}) >",
                    _ro_block(text, f"{wavelength}_3.5_2d_imm_f9"),
                )
                dir3_terms = " ".join(
                    f"(A+,{base + off}) (A-,{base + off})" for off in add_preview_ro.DIR3_OFFSETS
                )
                self.assertIn(
                    f"<{dir3_terms} >",
                    _ro_block(text, f"{wavelength}_3.5_2d_imm_3dir"),
                )
            # 完整 token 序列恰等于期望 pairs（带空格、无 )( 紧靠、无落单正相、无 [HWA h]）。
            for name, frames in add_preview_ro._ro_specs():
                block = _ro_block(text, name)
                self.assertNotIn(")(", block)
                self.assertIn("[HWA \n", block)
                self.assertNotIn("[HWA h", block)
                toks = re.findall(r"\((A[+-]),(\d+)\)", block)
                self.assertEqual(toks, [(a, str(i)) for a, i in frames], name)
                for k in range(0, len(toks), 2):
                    self.assertEqual(toks[k][0], "A+", name)
                    self.assertEqual(toks[k + 1], ("A-", toks[k][1]), name)

    def test_names_not_formal_and_preserve_leading_wavelength(self):
        from sim_control.adapters import (
            parse_leading_wavelength_nm,
            parse_running_order_name,
        )
        from tools import add_preview_ro

        try:
            from sim_control.adapters import parse_z_scan_running_order_name
        except ImportError:
            parse_z_scan_running_order_name = None

        for n in add_preview_ro.PREVIEW_RO_NAMES:
            self.assertIsNone(parse_running_order_name(n), n)
            if parse_z_scan_running_order_name is not None:
                self.assertIsNone(parse_z_scan_running_order_name(n), n)
            self.assertEqual(parse_leading_wavelength_nm(n), int(n.split("_", 1)[0]), n)

    def test_idempotent_and_content_mismatch_fails(self):
        from tools import add_preview_ro

        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "2d_3.5.repz11"
            _make_target(target)
            add_preview_ro.add_preview_ro(target)
            names1 = add_preview_ro._all_ro_names(_read_rep(target))
            backup = target.with_suffix(target.suffix + ".bak")
            self.assertTrue(backup.exists())
            mtime = backup.stat().st_mtime_ns

            add_preview_ro.add_preview_ro(target)  # 重跑：无变化、.bak 不重建
            self.assertEqual(add_preview_ro._all_ro_names(_read_rep(target)), names1)
            self.assertEqual(backup.stat().st_mtime_ns, mtime)

            # 同名块内容被篡改（405 f1 改成 (A+,0) (A-,1)）→ fail before write
            tampered = _read_rep(target).replace("(A+,0) (A-,0)", "(A+,0) (A-,1)", 1)
            t2 = Path(d) / "tampered.repz11"
            _make_target(t2, rep_text=tampered)
            with self.assertRaises(RuntimeError):
                add_preview_ro.add_preview_ro(t2)

            # 同名 RO 即使帧 token 一样，只要骨架被改成 [HWA h] 也必须 fail before write。
            tampered_header = _read_rep(target).replace("[HWA \n", "[HWA h\n", 1)
            t3 = Path(d) / "tampered_header.repz11"
            _make_target(t3, rep_text=tampered_header)
            with self.assertRaises(RuntimeError):
                add_preview_ro.add_preview_ro(t3)

    def test_rejects_old_L_merge_residue(self):
        from tools import add_preview_ro

        with tempfile.TemporaryDirectory() as d:
            residue = _synthetic_target_rep().replace(
                "SEQUENCES_END",
                'L "48088 100us 1-bit Lit Balanced.seq11"\nSEQUENCES_END',
                1,
            ) + '\n "488_3.5_2d_imm_f1"\n[HWA \n\n <(L,36) >\n]\n'
            target = Path(d) / "residue.repz11"
            _make_target(target, rep_text=residue)
            with self.assertRaises(RuntimeError):
                add_preview_ro.add_preview_ro(target)

    @unittest.skipUnless(BEFORE_MERGE.exists(), "BEFORE_MERGE 对照文件不存在")
    def test_real_before_merge_integration(self):
        from tools import add_preview_ro

        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "2d_3.5.repz11"
            shutil.copy2(BEFORE_MERGE, target)
            add_preview_ro.add_preview_ro(target)
            text = _read_rep(target)
            self.assertEqual(len(add_preview_ro._all_ro_names(text)), 68)
            self.assertEqual(len(add_preview_ro._alias_lines(text)), 12)
            self.assertEqual(add_preview_ro._image_count(text), 36)
            for wavelength, base in add_preview_ro.PREVIEW_IMAGE_BASES:
                self.assertIn(
                    f"<(A+,{base}) (A-,{base}) >",
                    _ro_block(text, f"{wavelength}_3.5_2d_imm_f1"),
                )
                dir3_terms = " ".join(
                    f"(A+,{base + off}) (A-,{base + off})" for off in add_preview_ro.DIR3_OFFSETS
                )
                self.assertIn(
                    f"<{dir3_terms} >",
                    _ro_block(text, f"{wavelength}_3.5_2d_imm_3dir"),
                )


if __name__ == "__main__":
    unittest.main()

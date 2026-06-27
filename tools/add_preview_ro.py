"""Add sample-finding (preview) immediate RO skeletons into patterns/2d_3.5.repz11.

为"实时找样品"在主 repertoire 追加 40 个 immediate RO 骨架（405/488/561/647 四个波长，
各自 ``*_3.5_2d_imm_f1``..``f9`` 单图案 + ``*_3.5_2d_imm_3dir`` 三图案）：操作者
select 后让 SLM 立即循环显示指定波长结构光、光透过 MASK 做相机 free-run 实时预览找样品。

关键约束（详见 plan / SDK 文档，均已坐实）：
- **复用现有 Lit Pair 序列 ``A+``/``A-``（48030），绝不加新序列**——R11 序列上限已满，加第
  13 个别名会撞 MetroCon ``sequence number (16) out of range``。
- **DC 平衡硬约束**：Lit Pair 的 ``+``/``-`` 是同一图案的正/反相（``b0`` // ``/b0``），R11 的
  FLC 必须正反相成对驱动、净 DC=0，否则物理损坏液晶（``AN0028BA`` §3.3.1.2 p.14）。所以每个
  图案必须 ``(A+,n) (A-,n)`` **成对**，绝不单独 ``(A+,n)``。
- **复用现有 405/488/561/647 图案**（IMAGES 全局索引分别为 0-8、9-17、18-26、27-35），
  不加图。
- 帧引用 token 之间留空格、结尾 `` >``，对齐现有可用 ``.rep`` 格式（避免 ``)(`` 紧靠导致
  MetroCon parser 不兼容）。
- 激活标记写 ``[HWA ]``（骨架）；**最终 immediate 激活类型由用户在 MetroCon GUI 设、编译进
  ``.repc``**——脚本只生成可加载的 RO 结构，不负责激活类型。

幂等：同名 RO 已在 ``.rep`` 则**逐块校验内容**、不一致则写入前失败（不静默 skip）；开头**拒绝
旧合并残留**（``L "48088…"`` 别名 / ``(L,*)`` 帧引用），提示先回退干净原版。所有校验通过后才建
``.bak`` 再写回；所有 ``raise`` 在 ``write_bytes`` 前。
"""

from __future__ import annotations

import argparse
import io
import re
import shutil
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPZ = REPO_ROOT / "patterns" / "2d_3.5.repz11"

# 现有 Lit Pair 别名（48030 500us），成对做 DC 平衡。
PAIR_POS = "A+"
PAIR_NEG = "A-"
PAIR_POS_SEQ = "48030 500us 1-bit Lit Pair +.seq11"
PAIR_NEG_SEQ = "48030 500us 1-bit Lit Pair -.seq11"

# 各波长图案在 IMAGES 段中的全局起始索引。
PREVIEW_IMAGE_BASES = (
    (405, 0),
    (488, 9),
    (561, 18),
    (647, 27),
)
# 3direction 三方向各一相位：每个波长的第 1/4/7 张（base+0/base+3/base+6）。
# 若要严格复刻旧 F6.8014 顺序，改成 (0, 6, 3)。
DIR3_OFFSETS = (0, 3, 6)

PREVIEW_RO_SUFFIXES = tuple(f"f{i}" for i in range(1, 10)) + ("3dir",)
PREVIEW_RO_NAMES = [
    f"{wavelength}_3.5_2d_imm_{suffix}"
    for wavelength, _base in PREVIEW_IMAGE_BASES
    for suffix in PREVIEW_RO_SUFFIXES
]

EXPECTED_RO_COUNT = 68     # 现有 28（含 DEFAULT）+ 新 40
EXPECTED_ALIAS_COUNT = 12  # 不加序列
EXPECTED_IMAGE_COUNT = 36  # 不加图

QUOTED_LINE_RE = re.compile(r'^\s*"(?P<name>[^"]+)"\s*$')
DEFAULT_RO_RE = re.compile(r'^\s*DEFAULT\s+"(?P<name>[^"]+)"\s*$')


def _read_entries(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path, "r") as src:
        return {info.filename: src.read(info.filename) for info in src.infolist()}


def _rep_name(entries: dict[str, bytes]) -> str:
    names = [n for n in entries if n.lower().endswith(".rep")]
    if len(names) != 1:
        raise RuntimeError(f"Expected exactly one .rep file, found {names!r}")
    return names[0]


def _all_ro_names(rep_text: str) -> set[str]:
    """RO 名（含 DEFAULT 行）；只扫 IMAGES_END 之后，排除头部段的引号值。"""
    marker = "IMAGES_END"
    idx = rep_text.find(marker)
    if idx < 0:
        raise RuntimeError("IMAGES_END not found in .rep — malformed repertoire.")
    body = rep_text[idx + len(marker):]
    names: set[str] = set()
    for line in body.splitlines():
        m = QUOTED_LINE_RE.match(line) or DEFAULT_RO_RE.match(line)
        if m:
            names.add(m.group("name"))
    return names


def _section(rep_text: str, start_tag: str, end_tag: str):
    lines = rep_text.splitlines()
    try:
        s = lines.index(start_tag)
        e = lines.index(end_tag)
    except ValueError as exc:
        raise RuntimeError(f"{start_tag}/{end_tag} not found in .rep") from exc
    return lines, s, e


def _alias_lines(rep_text: str) -> list[str]:
    lines, s, e = _section(rep_text, "SEQUENCES", "SEQUENCES_END")
    return [l for l in lines[s + 1:e] if l.strip()]


def _alias_map(rep_text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in _alias_lines(rep_text):
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2:
            out[parts[0]] = parts[1].strip().strip('"')
    return out


def _image_count(rep_text: str) -> int:
    lines, s, e = _section(rep_text, "IMAGES", "IMAGES_END")
    return sum(1 for l in lines[s + 1:e] if l.strip())


def _preview_block(name: str, frames: list[tuple[str, int]]) -> str:
    """生成 immediate RO 骨架块：``[HWA ]`` + token 间空格 + 结尾 `` >``。"""
    terms = " ".join(f"({alias},{idx})" for alias, idx in frames)
    return f'\n "{name}"\n[HWA \n\n <{terms} >\n]\n'


def _ro_specs() -> list[tuple[str, list[tuple[str, int]]]]:
    """40 个找样品 RO 的 (name, frames)；每图案成对 (A+,n) (A-,n)。"""
    specs: list[tuple[str, list[tuple[str, int]]]] = []
    for wavelength, base in PREVIEW_IMAGE_BASES:
        for i in range(1, 10):
            n = base + (i - 1)
            specs.append((f"{wavelength}_3.5_2d_imm_f{i}", [(PAIR_POS, n), (PAIR_NEG, n)]))
        dir3: list[tuple[str, int]] = []
        for off in DIR3_OFFSETS:
            n = base + off
            dir3 += [(PAIR_POS, n), (PAIR_NEG, n)]
        specs.append((f"{wavelength}_3.5_2d_imm_3dir", dir3))
    return specs


def _reject_old_merge_residue(rep_text: str) -> None:
    if 'L "48088' in rep_text or "(L," in rep_text:
        raise RuntimeError(
            "Old merge residue detected (L alias / (L,*) refs). "
            "Revert patterns/2d_3.5.repz11 to the clean pre-merge version first, then re-run."
        )


def _extract_ro_blocks(rep_text: str, name: str) -> list[str]:
    """Extract full RO blocks for ``name`` after ``IMAGES_END``."""
    marker = "IMAGES_END"
    body_start = rep_text.find(marker)
    if body_start < 0:
        raise RuntimeError("IMAGES_END not found in .rep - malformed repertoire.")
    pos = body_start + len(marker)
    needle = f'"{name}"'
    blocks: list[str] = []
    while True:
        s = rep_text.find(needle, pos)
        if s < 0:
            break
        line_start = rep_text.rfind("\n", 0, s) + 1
        if rep_text[line_start:s].strip():
            pos = s + len(needle)
            continue
        line_end = rep_text.find("\n", s)
        if line_end < 0 or rep_text[s + len(needle):line_end].strip():
            pos = s + len(needle)
            continue
        block_end = rep_text.find("\n]", line_end)
        if block_end < 0:
            blocks.append(rep_text[line_start:].strip())
            break
        blocks.append(rep_text[line_start:block_end + 2].strip())
        pos = block_end + 2
    return blocks


def _ensure_blocks(rep_text: str, specs) -> str:
    existing = _all_ro_names(rep_text)
    additions: list[str] = []
    for name, frames in specs:
        expected_block = _preview_block(name, frames).strip()
        if name in existing:
            # 同名已存在 -> 完整 block 必须唯一且与脚本生成结果一致，不一致则 fail（不静默 skip）。
            blocks = _extract_ro_blocks(rep_text, name)
            if len(blocks) != 1 or blocks[0] != expected_block:
                raise RuntimeError(
                    f"RO {name!r} already exists with different block; refusing to overwrite. "
                    f"Inspect/revert the .rep first."
                )
            continue
        additions.append(_preview_block(name, frames))
    if not additions:
        return rep_text
    return rep_text.rstrip() + "\n" + "".join(additions)


def _repack(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as dst:
        for name, data in entries.items():
            dst.writestr(name, data)
    return buf.getvalue()


def _self_check(archive_bytes: bytes, rep_name: str, original_ro_names: set[str]) -> None:
    with zipfile.ZipFile(io.BytesIO(archive_bytes), "r") as check:
        text = check.read(rep_name).decode("utf-8")
    ro = _all_ro_names(text)
    missing = original_ro_names - ro
    if missing:
        raise RuntimeError(f"Existing RO names removed: {sorted(missing)}")
    for n in PREVIEW_RO_NAMES:
        if n not in ro:
            raise RuntimeError(f"Preview RO missing after add: {n}")
    if len(ro) != EXPECTED_RO_COUNT:
        raise RuntimeError(f"Expected {EXPECTED_RO_COUNT} ROs, got {len(ro)}")
    aliases = _alias_map(text)
    if len(aliases) != EXPECTED_ALIAS_COUNT:
        raise RuntimeError(f"Expected {EXPECTED_ALIAS_COUNT} aliases (no new sequence), got {len(aliases)}")
    if aliases.get(PAIR_POS) != PAIR_POS_SEQ or aliases.get(PAIR_NEG) != PAIR_NEG_SEQ:
        raise RuntimeError(
            f"{PAIR_POS}/{PAIR_NEG} alias value changed: "
            f"{aliases.get(PAIR_POS)!r}/{aliases.get(PAIR_NEG)!r}"
        )
    if _image_count(text) != EXPECTED_IMAGE_COUNT:
        raise RuntimeError(f"Expected {EXPECTED_IMAGE_COUNT} images (no new image), got {_image_count(text)}")
    if text.count("DEFAULT ") != 1:
        raise RuntimeError("Merged .rep must keep exactly one DEFAULT running order.")
    if "(L," in text or 'L "48088' in text:
        raise RuntimeError("L residue present after build.")


def add_preview_ro(repz_path: Path) -> None:
    """在 target repz 追加 40 个找样品 RO 骨架（就地改写，先校验后备份；幂等）。"""
    entries = _read_entries(repz_path)
    rep = _rep_name(entries)
    text = entries[rep].decode("utf-8")

    _reject_old_merge_residue(text)
    aliases = _alias_map(text)
    if aliases.get(PAIR_POS) != PAIR_POS_SEQ or aliases.get(PAIR_NEG) != PAIR_NEG_SEQ:
        raise RuntimeError(
            f"Required Lit Pair aliases {PAIR_POS}/{PAIR_NEG} (= 48030 +/-) not present as expected; "
            f"got {aliases.get(PAIR_POS)!r}/{aliases.get(PAIR_NEG)!r}."
        )

    original_ro_names = _all_ro_names(text)
    new_text = _ensure_blocks(text, _ro_specs())
    changed = new_text != text
    entries[rep] = new_text.encode("utf-8")

    archive_bytes = _repack(entries)
    _self_check(archive_bytes, rep, original_ro_names)  # 无改动也校验，防文件被其他方式漂移后静默通过

    if not changed:
        print("no change (all preview ROs already present and consistent; self-check passed)")
        return

    backup = repz_path.with_suffix(repz_path.suffix + ".bak")
    if not backup.exists():
        shutil.copy2(repz_path, backup)
    repz_path.write_bytes(archive_bytes)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repz", type=Path, default=DEFAULT_REPZ, help="目标 repertoire（默认 patterns/2d_3.5.repz11）")
    args = parser.parse_args()
    add_preview_ro(args.repz.resolve())
    print(f"added preview ROs -> {args.repz}")


if __name__ == "__main__":
    main()

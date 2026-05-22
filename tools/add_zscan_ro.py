"""Append z-scan three-phase Running Orders to patterns/2d_3.5.repz11.

The script is idempotent: existing z-scan RO blocks and sequence aliases are
left in place, missing pieces are added, and the original repertoire is backed
up before the zip archive is rewritten.
"""

from __future__ import annotations

import argparse
import re
import shutil
import tempfile
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPZ = REPO_ROOT / "patterns" / "2d_3.5.repz11"
SDK_SEQUENCE_ROOT = (
    REPO_ROOT
    / "SDK"
    / "R11 CD Bundle Mar 2020"
    / "2020-03"
    / "Software"
    / "Sequences"
    / "QXGA"
)

SEQUENCE_ALIASES = {
    "F+": "48037 1ms 1-bit Lit Pair +.seq11",
    "F-": "48037 1ms 1-bit Lit Pair -.seq11",
    "G+": "48038 2ms 1-bit Lit Pair +.seq11",
    "G-": "48038 2ms 1-bit Lit Pair -.seq11",
    "H+": "48039 3ms 1-bit Lit Pair +.seq11",
    "H-": "48039 3ms 1-bit Lit Pair -.seq11",
}

PRESETS = {
    5: "A",
    8: "F",
    14: "G",
    20: "H",
}

FORMAL_RO_RE = re.compile(r"^\d+_3\.5_2d_\d+ms(?:_ang0)?$")
QUOTED_LINE_RE = re.compile(r'^\s*"(?P<name>[^"]+)"\s*$')


def _ro_names(rep_text: str) -> set[str]:
    names = set()
    for line in rep_text.splitlines():
        match = QUOTED_LINE_RE.match(line)
        if match and FORMAL_RO_RE.match(match.group("name")):
            names.add(match.group("name"))
    return names


def _ensure_sequence_aliases(rep_text: str) -> str:
    lines = rep_text.splitlines()
    try:
        end_index = lines.index("SEQUENCES_END")
    except ValueError as exc:
        raise RuntimeError("SEQUENCES_END not found in .rep text.") from exc

    existing = {}
    for line in lines:
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2 and parts[0] in SEQUENCE_ALIASES:
            existing[parts[0]] = parts[1].strip()

    additions = []
    for alias, filename in SEQUENCE_ALIASES.items():
        expected = f'"{filename}"'
        if alias in existing:
            if existing[alias] != expected:
                raise RuntimeError(f"Sequence alias {alias} already exists with unexpected value {existing[alias]!r}.")
            continue
        additions.append(f'{alias} "{filename}"')

    if additions:
        lines[end_index:end_index] = additions
    return "\n".join(lines) + "\n"


def _zscan_block(name: str, alias: str) -> str:
    phase_terms = " ".join(f"({alias}{sign},{index})" for index in (9, 10, 11) for sign in ("+", "-"))
    return (
        f'\n "{name}"\n'
        "[HWA h\n\n"
        "GPO3 GPO2 GPO1\n"
        f"<GPO=0 t.wait(20) GPO=2 {phase_terms}  >\n\n"
        "]\n"
    )


def _ensure_zscan_blocks(rep_text: str) -> str:
    existing_names = {match.group("name") for match in map(QUOTED_LINE_RE.match, rep_text.splitlines()) if match}
    blocks = []
    for preset, alias in PRESETS.items():
        name = f"488_3.5_2d_zscan3p_{preset}ms"
        if name not in existing_names:
            blocks.append(_zscan_block(name, alias))
    if blocks:
        rep_text = rep_text.rstrip() + "\n" + "".join(blocks)
    return rep_text


def _sequence_source(filename: str) -> Path:
    if filename.startswith("48037 "):
        return DEFAULT_REPZ
    folder = filename.rsplit(" ", 4)[0] + " " + " ".join(filename.rsplit(" ", 4)[1:4])
    candidates = list(SDK_SEQUENCE_ROOT.glob(f"*/{filename}"))
    if not candidates:
        raise RuntimeError(f"Cannot find SDK sequence file for {filename}")
    return candidates[0]


def patch_repertoire(repz_path: Path) -> None:
    backup_path = repz_path.with_suffix(repz_path.suffix + ".bak")
    if not backup_path.exists():
        shutil.copy2(repz_path, backup_path)

    with zipfile.ZipFile(repz_path, "r") as src:
        entries = {info.filename: src.read(info.filename) for info in src.infolist()}
    rep_names = [name for name in entries if name.endswith(".rep")]
    if len(rep_names) != 1:
        raise RuntimeError(f"Expected exactly one .rep file, found {rep_names!r}")
    rep_name = rep_names[0]
    original_text = entries[rep_name].decode("utf-8")
    original_formal_names = _ro_names(original_text)

    updated_text = _ensure_sequence_aliases(original_text)
    updated_text = _ensure_zscan_blocks(updated_text)
    entries[rep_name] = updated_text.encode("utf-8")

    for filename in SEQUENCE_ALIASES.values():
        if filename in entries:
            continue
        source = _sequence_source(filename)
        if source == DEFAULT_REPZ:
            continue
        entries[filename] = source.read_bytes()

    with tempfile.NamedTemporaryFile(delete=False, suffix=".repz11", dir=repz_path.parent) as temp_file:
        temp_path = Path(temp_file.name)
    try:
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED) as dst:
            for name, data in entries.items():
                dst.writestr(name, data)
        with zipfile.ZipFile(temp_path, "r") as check:
            check_entries = set(check.namelist())
            check_text = check.read(rep_name).decode("utf-8")
        new_formal_names = _ro_names(check_text)
        missing_formal = original_formal_names - new_formal_names
        if missing_formal:
            raise RuntimeError(f"Formal SIM RO names were removed: {sorted(missing_formal)}")
        for preset in PRESETS:
            name = f"488_3.5_2d_zscan3p_{preset}ms"
            if name not in check_text:
                raise RuntimeError(f"Missing z-scan RO after patch: {name}")
        for filename in SEQUENCE_ALIASES.values():
            if filename not in check_entries:
                raise RuntimeError(f"Missing sequence file after patch: {filename}")
        shutil.move(str(temp_path), repz_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repz", type=Path, default=DEFAULT_REPZ)
    args = parser.parse_args()
    patch_repertoire(args.repz.resolve())
    print(f"patched {args.repz}")


if __name__ == "__main__":
    main()

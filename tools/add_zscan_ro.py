"""Append multi-wavelength three-phase z-scan ROs to ``2d_3.5.repz11``.

The repertoire already contains every sequence and image used here. This tool
therefore validates those assets and appends only missing RO text blocks; it
never adds a sequence alias, sequence file, image, or FINISH-controlled loop.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPZ = REPO_ROOT / "patterns" / "2d_3.5.repz11"
SEQUENCE_ALIASES = {
    "A+": "48030 500us 1-bit Lit Pair +.seq11",
    "A-": "48030 500us 1-bit Lit Pair -.seq11",
    "D+": "48039 3ms 1-bit Lit Pair +.seq11",
    "D-": "48039 3ms 1-bit Lit Pair -.seq11",
    "F+": "48037 1ms 1-bit Lit Pair +.seq11",
    "F-": "48037 1ms 1-bit Lit Pair -.seq11",
    "G+": "48038 2ms 1-bit Lit Pair +.seq11",
    "G-": "48038 2ms 1-bit Lit Pair -.seq11",
}

PRESETS = {
    5: "A",
    8: "F",
    14: "G",
    20: "D",
}

QUOTED_LINE_RE = re.compile(r'^\s*"(?P<name>[^"]+)"\s*$')
DEFAULT_RO_RE = re.compile(r'^\s*DEFAULT\s+"(?P<name>[^"]+)"\s*$')
ZSCAN_PATTERN_INDICES = {
    405: (0, 1, 2),
    488: (9, 10, 11),
    561: (18, 19, 20),
    647: (27, 28, 29),
}
ZSCAN_WAVELENGTHS = tuple(ZSCAN_PATTERN_INDICES)


def _running_order_names(rep_text: str) -> set[str]:
    """Return all RO names (including the DEFAULT RO), excluding header values."""
    marker = "IMAGES_END"
    marker_index = rep_text.find(marker)
    if marker_index < 0:
        raise RuntimeError("IMAGES_END not found in .rep text.")
    names: set[str] = set()
    for line in rep_text[marker_index + len(marker):].splitlines():
        match = QUOTED_LINE_RE.match(line) or DEFAULT_RO_RE.match(line)
        if match:
            name = match.group("name")
            if name in names:
                raise RuntimeError(f"Duplicate Running Order name: {name}")
            names.add(name)
    return names


def _validate_sequence_aliases(rep_text: str) -> None:
    """Fail closed unless all existing aliases resolve to the expected files."""
    existing: dict[str, str] = {}
    for line in rep_text.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2 and parts[0] in SEQUENCE_ALIASES:
            existing[parts[0]] = parts[1].strip()
    for alias, filename in SEQUENCE_ALIASES.items():
        expected = f'"{filename}"'
        actual = existing.get(alias)
        if actual is None:
            raise RuntimeError(f"Required sequence alias {alias} is missing; refusing to add aliases automatically.")
        if actual != expected:
            raise RuntimeError(
                f"Sequence alias {alias} has unexpected value {actual!r}; expected {expected!r}."
            )


def _validate_sequence_entries(entry_names: set[str]) -> None:
    """Require every sequence file referenced by the fixed aliases."""
    missing = sorted(set(SEQUENCE_ALIASES.values()) - set(entry_names))
    if missing:
        raise RuntimeError(
            "Required sequence archive entries are missing: " + ", ".join(missing)
        )


def _zscan_block(name: str, alias: str, pattern_indices: tuple[int, int, int]) -> str:
    phase_terms = " ".join(
        f"({alias}{sign},{index})"
        for index in pattern_indices
        for sign in ("+", "-")
    )
    return (
        f'\n "{name}"\n'
        "[HWA h\n\n"
        "GPO3 GPO2 GPO1\n"
        f"<GPO=0 t.wait(20) GPO=2 {phase_terms}  >\n\n"
        "]\n"
    )


def _ensure_zscan_blocks(rep_text: str) -> str:
    existing_names = _running_order_names(rep_text)
    blocks = []
    for wavelength in ZSCAN_WAVELENGTHS:
        for preset, alias in PRESETS.items():
            name = f"{wavelength}_3.5_2d_zscan3p_{preset}ms"
            if name not in existing_names:
                blocks.append(_zscan_block(name, alias, ZSCAN_PATTERN_INDICES[wavelength]))
    if blocks:
        rep_text = rep_text.rstrip() + "\n" + "".join(blocks)
    return rep_text


def _rewrite_legacy_zscan_aliases(rep_text: str) -> str:
    marker = '"488_3.5_2d_zscan3p_20ms"'
    start = rep_text.find(marker)
    if start < 0:
        return rep_text
    end = rep_text.find("\n]", start)
    if end < 0:
        raise RuntimeError(f"Malformed z-scan Running Order block: {marker}")
    end += len("\n]")
    block = rep_text[start:end]
    block = block.replace("(H+,", "(D+,").replace("(H-,", "(D-,")
    return rep_text[:start] + block + rep_text[end:]


def _expected_zscan_names() -> set[str]:
    return {
        f"{wavelength}_3.5_2d_zscan3p_{preset}ms"
        for wavelength in ZSCAN_WAVELENGTHS
        for preset in PRESETS
    }


def _validate_patched_archive(
    archive_path: Path,
    *,
    rep_name: str,
    expected_entries: set[str],
    expected_comment: bytes,
    original_ro_names: set[str],
) -> None:
    """Reopen and fully validate the temporary archive before committing it."""
    with zipfile.ZipFile(archive_path, "r") as check:
        corrupt_entry = check.testzip()
        if corrupt_entry is not None:
            raise RuntimeError(f"Corrupt archive entry after patch: {corrupt_entry}")
        names = check.namelist()
        if len(names) != len(set(names)):
            raise RuntimeError("Duplicate archive entry names after z-scan patch.")
        check_entries = set(names)
        if check.comment != expected_comment:
            raise RuntimeError("Archive comment changed while adding z-scan ROs.")
        check_text = check.read(rep_name).decode("utf-8")

    if check_entries != expected_entries:
        raise RuntimeError("Archive entry set changed while adding z-scan ROs.")
    _validate_sequence_entries(check_entries)
    _validate_sequence_aliases(check_text)
    new_ro_names = _running_order_names(check_text)
    missing_original = original_ro_names - new_ro_names
    if missing_original:
        raise RuntimeError(f"Existing Running Orders were removed: {sorted(missing_original)}")
    expected_zscan_names = _expected_zscan_names()
    missing_zscan = expected_zscan_names - new_ro_names
    if missing_zscan:
        raise RuntimeError(f"Missing z-scan RO after patch: {sorted(missing_zscan)}")
    if len(new_ro_names) != len(original_ro_names | expected_zscan_names):
        raise RuntimeError("Unexpected Running Order count after z-scan patch.")


def patch_repertoire(repz_path: Path) -> None:
    with zipfile.ZipFile(repz_path, "r") as src:
        corrupt_entry = src.testzip()
        if corrupt_entry is not None:
            raise RuntimeError(f"Corrupt source archive entry: {corrupt_entry}")
        original_infos = list(src.infolist())
        archive_comment = src.comment
        entries = {info.filename: src.read(info.filename) for info in original_infos}
    if len(original_infos) != len(entries):
        raise RuntimeError("Source archive contains duplicate entry names.")
    _validate_sequence_entries(set(entries))
    rep_names = [name for name in entries if name.lower().endswith(".rep")]
    if len(rep_names) != 1:
        raise RuntimeError(f"Expected exactly one .rep file, found {rep_names!r}")
    rep_name = rep_names[0]
    original_text = entries[rep_name].decode("utf-8")
    original_ro_names = _running_order_names(original_text)

    updated_text = _rewrite_legacy_zscan_aliases(original_text)
    _validate_sequence_aliases(updated_text)
    updated_text = _ensure_zscan_blocks(updated_text)
    _validate_sequence_aliases(updated_text)
    entries[rep_name] = updated_text.encode("utf-8")

    # A second invocation must leave the archive byte-for-byte untouched.
    if updated_text == original_text:
        return

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b",
            prefix=f".{repz_path.name}.",
            suffix=".tmp",
            dir=repz_path.parent,
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            with zipfile.ZipFile(temp_file, "w", compression=zipfile.ZIP_DEFLATED) as dst:
                dst.comment = archive_comment
                for info in original_infos:
                    dst.writestr(info, entries[info.filename])
            temp_file.flush()
            os.fsync(temp_file.fileno())

        _validate_patched_archive(
            temp_path,
            rep_name=rep_name,
            expected_entries=set(entries),
            expected_comment=archive_comment,
            original_ro_names=original_ro_names,
        )

        backup_path = repz_path.with_suffix(repz_path.suffix + ".bak")
        if not backup_path.exists():
            shutil.copy2(repz_path, backup_path)
        os.replace(temp_path, repz_path)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repz", type=Path, default=DEFAULT_REPZ)
    args = parser.parse_args()
    patch_repertoire(args.repz.resolve())
    print(f"patched {args.repz}")


if __name__ == "__main__":
    main()

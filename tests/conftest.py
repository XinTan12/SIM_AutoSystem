from __future__ import annotations

import tempfile
import uuid
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_TEMP_ROOT = PROJECT_ROOT / ".codex_tmp_pyc"


class WorkspaceTemporaryDirectory:
    def __init__(
        self,
        suffix: str | None = None,
        prefix: str | None = None,
        dir: str | Path | None = None,
        ignore_cleanup_errors: bool = False,
    ) -> None:
        root = WORKSPACE_TEMP_ROOT if dir is None else Path(dir)
        root.mkdir(parents=True, exist_ok=True)
        name = f"{prefix or 'tmp'}{uuid.uuid4().hex}{suffix or ''}"
        self.name = str(root / name)
        Path(self.name).mkdir(parents=True, exist_ok=False)
        self.ignore_cleanup_errors = bool(ignore_cleanup_errors)

    def __enter__(self) -> str:
        return self.name

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.cleanup()
        return False

    def cleanup(self) -> None:
        # The current Windows sandbox can create and write these files but may
        # reject unlink/rmtree from the Python process. The root is gitignored.
        return None


tempfile.TemporaryDirectory = WorkspaceTemporaryDirectory

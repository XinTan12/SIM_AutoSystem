from __future__ import annotations

import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class MinimalImportTests(unittest.TestCase):
    def test_pure_submodules_do_not_require_pyqt5(self):
        script = textwrap.dedent(
            """
            import builtins

            real_import = builtins.__import__

            def blocked_import(name, globals=None, locals=None, fromlist=(), level=0):
                if name == "PyQt5" or name.startswith("PyQt5."):
                    raise ImportError("blocked PyQt5 import")
                return real_import(name, globals, locals, fromlist, level)

            builtins.__import__ = blocked_import

            import sim_control.preview_contrast
            import sim_control.focus_metrics
            import sim_control.config_store

            print("ok")
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(PROJECT_ROOT),
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ok", result.stdout)

    def test_top_level_lazy_exports_preserve_public_api(self):
        from sim_control import SimAcquisitionController, SimControlWindow

        self.assertEqual(SimAcquisitionController.__name__, "SimAcquisitionController")
        self.assertEqual(SimControlWindow.__name__, "SimControlWindow")


if __name__ == "__main__":
    unittest.main()

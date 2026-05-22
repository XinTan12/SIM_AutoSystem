"""Tests for the import-safe SIM9 Wiener reconstruction wrapper."""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class SimWienerWrapperTests(unittest.TestCase):
    def test_sim_wiener_import_is_safe_without_running_demo_paths(self):
        import reconstruction.sim_wiener as sim_wiener

        self.assertTrue(callable(sim_wiener.reconstruct_sim9_stack))
        self.assertFalse(hasattr(sim_wiener, "out"))
        self.assertFalse(hasattr(sim_wiener, "out2"))

    def test_reconstruct_sim9_stack_rejects_non_sim9_shape_before_backend_import(self):
        from reconstruction import sim_wiener

        with mock.patch.object(sim_wiener, "_load_backend", side_effect=AssertionError("backend import")):
            with self.assertRaisesRegex(ValueError, "Expected SIM stack shape"):
                sim_wiener.reconstruct_sim9_stack(
                    np.zeros((8, 2, 2), dtype=np.uint16),
                    wavelength_nm=488,
                    otf_path="dummy-otf.tif",
                )

    def test_reconstruct_sim9_stack_rejects_non_uint16_before_backend_import(self):
        from reconstruction import sim_wiener

        with mock.patch.object(sim_wiener, "_load_backend", side_effect=AssertionError("backend import")):
            with self.assertRaisesRegex(TypeError, "uint16"):
                sim_wiener.reconstruct_sim9_stack(
                    np.zeros((9, 2, 2), dtype=np.float32),
                    wavelength_nm=488,
                    otf_path="dummy-otf.tif",
                )

    def test_reconstruct_sim9_stack_reports_missing_otf_before_backend_import(self):
        from reconstruction import sim_wiener

        with mock.patch.object(sim_wiener, "_load_backend", side_effect=AssertionError("backend import")):
            with self.assertRaisesRegex(FileNotFoundError, "OTF"):
                sim_wiener.reconstruct_sim9_stack(
                    np.zeros((9, 2, 2), dtype=np.uint16),
                    wavelength_nm=488,
                    otf_path="missing-otf.tif",
                )

    def test_reconstruct_sim9_stack_reports_missing_torch_or_scipy_dependency(self):
        from reconstruction import sim_wiener

        stack = np.zeros((9, 2, 2), dtype=np.uint16)
        with tempfile.NamedTemporaryFile(suffix=".tif") as otf_file:
            with mock.patch.object(
                sim_wiener.importlib,
                "import_module",
                side_effect=ModuleNotFoundError("No module named 'torch'"),
            ):
                with self.assertRaisesRegex(RuntimeError, "torch"):
                    sim_wiener.reconstruct_sim9_stack(
                        stack,
                        wavelength_nm=488,
                        otf_path=otf_file.name,
                    )

    def test_reconstruct_sim9_stack_success_path_uses_in_memory_backend(self):
        from reconstruction import sim_wiener

        calls = {}

        class FakeOptions:
            def normalize(self, raw_path):
                self.raw_path = str(raw_path)
                self.nphases = 3
                self.nangles = 3
                if not getattr(self, "theta_ratio", None):
                    self.theta_ratio = (1, 1, 1)
                return self

        class FakeTorch:
            @staticmethod
            def device(value):
                return str(value)

            @staticmethod
            def as_tensor(value, device=None, dtype=None):
                return np.asarray(value, dtype=dtype)

        class FakeBaseReconstructor:
            def __init__(self, device=None, dtype=None):
                self.device = str(device)
                self.real_dtype = np.float32
                self.complex_dtype = np.complex64
                self.timings_ms = {}
                self._clock = 0.0

            def _np_dtype(self):
                return np.float32

            def _now(self):
                self._clock += 0.001
                return self._clock

            def _ms(self, t0, t1):
                return (t1 - t0) * 1000.0

            def _record_timing(self, name, value_ms):
                self.timings_ms[name] = float(value_ms)

            def _load_2d_tiff(self, path):
                calls.setdefault("loaded_tiffs", []).append(str(path))
                return np.ones((2, 3), dtype=np.float32)

            def _estimate_parameters(self, cfg, raw_info, raw_gpu, otf_template, param_prefix):
                calls["raw_info"] = dict(raw_info)
                calls["raw_gpu_shape"] = tuple(raw_gpu.shape)
                calls["raw_gpu_dtype"] = raw_gpu.dtype
                self._save_param_files({}, param_prefix, cfg)
                return {"zuobiaox": np.asarray([1.0]), "zuobiaoy": np.asarray([1.0]), "n": np.asarray(2.0)}

            def _wiener_reconstruct(self, cfg, raw_info, raw_gpu, otf_template, background, param):
                recon = np.full((1, raw_info["height"] * 2, raw_info["width"] * 2), 7.0, dtype=np.float32)
                return (
                    recon,
                    np.asarray([0.2, 0.3, 0.4], dtype=np.float32),
                    np.asarray([0.1, 0.2, 0.3], dtype=np.float32),
                    np.asarray([0.9, 0.8, 0.7], dtype=np.float32),
                )

        fake_backend = SimpleNamespace(
            torch=FakeTorch,
            SIMWienerGPUReconstructor=FakeBaseReconstructor,
            hessian_sim_wiener_default_options=FakeOptions,
            _torch_dtype_from_name=lambda _name: np.float32,
            _complex_dtype=lambda _dtype: np.complex64,
            _to_numpy=lambda value: np.asarray(value),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            otf_path = Path(temp_dir) / "otf.tif"
            otf_path.write_bytes(b"fake")
            stack = np.arange(9 * 2 * 3, dtype=np.uint16).reshape(9, 2, 3)

            with mock.patch.object(sim_wiener, "_load_backend", return_value=fake_backend):
                result = sim_wiener.reconstruct_sim9_stack(
                    stack,
                    wavelength_nm=488,
                    otf_path=str(otf_path),
                    device="cpu",
                )

            self.assertEqual(result["reconstruction"].shape, (1, 4, 6))
            self.assertEqual(result["reconstruction"].dtype, np.float32)
            self.assertEqual(calls["raw_info"], {"num_frames": 9, "height": 2, "width": 3})
            self.assertEqual(calls["raw_gpu_shape"], (9, 2, 3))
            self.assertEqual(calls["raw_gpu_dtype"], np.float32)
            self.assertEqual(calls["loaded_tiffs"], [str(otf_path)])
            self.assertEqual(result["metadata"]["algorithm"], "sim_wiener_gpu")
            self.assertEqual(result["metadata"]["reconstruction_shape"], [1, 4, 6])
            self.assertTrue(np.allclose(result["metadata"]["c6"], [0.2, 0.3, 0.4]))
            self.assertEqual(sorted(path.name for path in Path(temp_dir).iterdir()), ["otf.tif"])


if __name__ == "__main__":
    unittest.main()

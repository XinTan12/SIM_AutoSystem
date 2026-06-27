"""Tests for the import-safe SIM9 Wiener reconstruction integration layer.

覆盖 ``sim_control/sim_reconstruction.py``：输入校验顺序（无 torch 也能给出清晰错误）、
fake-backend 成功路径、saved/estimate 分派与 fallback、以及常驻热重建器的实例复用。
不依赖真实 torch/CUDA（用 fake backend）。
"""

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


def _make_fake_backend(calls: dict):
    """构造一个不依赖 torch 的 fake backend，支持 estimate 与 saved-params 两条路径。"""

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

        def _save_param_files(self, param, prefix, cfg):
            return None

        def _estimate_parameters(self, cfg, raw_info, raw_gpu, otf_template, param_prefix):
            calls["estimate_called"] = calls.get("estimate_called", 0) + 1
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

        def _load_estimated_params_file(self, path):
            calls["load_saved_called"] = calls.get("load_saved_called", 0) + 1
            if calls.get("saved_raises"):
                raise FileNotFoundError(f"missing saved params: {path}")
            return ({"n": np.asarray(2.0)}, {"c6": np.asarray([0.5, 0.5, 0.5])})

        def _wiener_reconstruct_with_saved_params(
            self, cfg, raw_info, raw_gpu, otf_template, background, param, saved_estimates
        ):
            calls["saved_recon_called"] = calls.get("saved_recon_called", 0) + 1
            recon = np.full((1, raw_info["height"] * 2, raw_info["width"] * 2), 5.0, dtype=np.float32)
            return (
                recon,
                np.asarray([0.5, 0.5, 0.5], dtype=np.float32),
                np.asarray([0.0, 0.0, 0.0], dtype=np.float32),
                np.asarray([0.6, 0.6, 0.6], dtype=np.float32),
            )

    return SimpleNamespace(
        torch=FakeTorch,
        SIMWienerGPUReconstructor=FakeBaseReconstructor,
        hessian_sim_wiener_default_options=FakeOptions,
        _torch_dtype_from_name=lambda _name: np.float32,
        _complex_dtype=lambda _dtype: np.complex64,
        _to_numpy=lambda value: np.asarray(value),
    )


class SimReconstructionWrapperTests(unittest.TestCase):
    def setUp(self):
        # 进程级后端缓存只在真实 import 成功时写入；测试用 fake，重置保证隔离。
        import sim_control.sim_reconstruction as sr

        sr._BACKEND_CACHE = None

    def test_module_exposes_public_api(self):
        import sim_control.sim_reconstruction as sr

        self.assertTrue(callable(sr.reconstruct_sim9_stack))
        self.assertTrue(hasattr(sr, "WarmSIMReconstructor"))
        # 集成层只 import 引擎模块，绝不 import 会执行整段重建的 demo 脚本。
        self.assertNotIn("reconstruction.sim_wiener", sys.modules)

    def test_rejects_non_sim9_shape_before_backend_import(self):
        import sim_control.sim_reconstruction as sr

        with mock.patch.object(sr, "_load_backend", side_effect=AssertionError("backend import")):
            with self.assertRaisesRegex(ValueError, "Expected SIM stack shape"):
                sr.reconstruct_sim9_stack(
                    np.zeros((8, 2, 2), dtype=np.uint16), wavelength_nm=488, otf_path="dummy-otf.tif"
                )

    def test_rejects_non_uint16_before_backend_import(self):
        import sim_control.sim_reconstruction as sr

        with mock.patch.object(sr, "_load_backend", side_effect=AssertionError("backend import")):
            with self.assertRaisesRegex(TypeError, "uint16"):
                sr.reconstruct_sim9_stack(
                    np.zeros((9, 2, 2), dtype=np.float32), wavelength_nm=488, otf_path="dummy-otf.tif"
                )

    def test_reports_missing_otf_before_backend_import(self):
        import sim_control.sim_reconstruction as sr

        with mock.patch.object(sr, "_load_backend", side_effect=AssertionError("backend import")):
            with self.assertRaisesRegex(FileNotFoundError, "OTF"):
                sr.reconstruct_sim9_stack(
                    np.zeros((9, 2, 2), dtype=np.uint16), wavelength_nm=488, otf_path="missing-otf.tif"
                )

    def test_reports_missing_torch_dependency(self):
        import sim_control.sim_reconstruction as sr

        stack = np.zeros((9, 2, 2), dtype=np.uint16)
        with tempfile.NamedTemporaryFile(suffix=".tif") as otf_file:
            with mock.patch.object(
                sr.importlib,
                "import_module",
                side_effect=ModuleNotFoundError("No module named 'torch'", name="torch"),
            ):
                with self.assertRaisesRegex(RuntimeError, "torch"):
                    sr.reconstruct_sim9_stack(stack, wavelength_nm=488, otf_path=otf_file.name)

    def test_success_path_uses_in_memory_backend_estimate(self):
        import sim_control.sim_reconstruction as sr

        calls = {}
        fake_backend = _make_fake_backend(calls)
        with tempfile.TemporaryDirectory() as temp_dir:
            otf_path = Path(temp_dir) / "otf.tif"
            otf_path.write_bytes(b"fake")
            stack = np.arange(9 * 2 * 3, dtype=np.uint16).reshape(9, 2, 3)

            with mock.patch.object(sr, "_load_backend", return_value=fake_backend):
                result = sr.reconstruct_sim9_stack(
                    stack, wavelength_nm=488, otf_path=str(otf_path), device="cpu"
                )

        self.assertEqual(result["reconstruction"].shape, (1, 4, 6))
        self.assertEqual(result["reconstruction"].dtype, np.float32)
        self.assertEqual(calls["raw_info"], {"num_frames": 9, "height": 2, "width": 3})
        self.assertEqual(calls["raw_gpu_shape"], (9, 2, 3))
        self.assertEqual(calls["raw_gpu_dtype"], np.float32)
        self.assertEqual(calls["loaded_tiffs"], [str(otf_path)])
        self.assertEqual(calls.get("estimate_called"), 1)
        self.assertEqual(result["metadata"]["algorithm"], "sim_wiener_gpu")
        self.assertEqual(result["metadata"]["reconstruction_shape"], [1, 4, 6])
        self.assertFalse(result["metadata"]["use_saved_params"])
        self.assertFalse(result["metadata"]["fallback_used"])
        self.assertTrue(np.allclose(result["metadata"]["c6"], [0.2, 0.3, 0.4]))

    def test_saved_params_path_dispatches_to_saved_reconstruct(self):
        import sim_control.sim_reconstruction as sr

        calls = {}
        fake_backend = _make_fake_backend(calls)
        with tempfile.TemporaryDirectory() as temp_dir:
            otf_path = Path(temp_dir) / "otf.tif"
            otf_path.write_bytes(b"fake")
            mat_path = Path(temp_dir) / "params.mat"
            mat_path.write_bytes(b"fake")
            stack = np.ones((9, 2, 3), dtype=np.uint16)

            with mock.patch.object(sr, "_load_backend", return_value=fake_backend):
                result = sr.reconstruct_sim9_stack(
                    stack,
                    wavelength_nm=488,
                    otf_path=str(otf_path),
                    device="cpu",
                    use_saved_params=True,
                    estimated_params_path=str(mat_path),
                )

        self.assertEqual(calls.get("saved_recon_called"), 1)
        self.assertIsNone(calls.get("estimate_called"))
        self.assertTrue(result["metadata"]["use_saved_params"])
        self.assertFalse(result["metadata"]["fallback_used"])

    def test_saved_params_missing_fails_when_fallback_is_fail(self):
        import sim_control.sim_reconstruction as sr

        calls = {"saved_raises": True}
        fake_backend = _make_fake_backend(calls)
        with tempfile.TemporaryDirectory() as temp_dir:
            otf_path = Path(temp_dir) / "otf.tif"
            otf_path.write_bytes(b"fake")
            mat_path = Path(temp_dir) / "params.mat"
            mat_path.write_bytes(b"fake")
            stack = np.ones((9, 2, 3), dtype=np.uint16)

            with mock.patch.object(sr, "_load_backend", return_value=fake_backend):
                with self.assertRaises(FileNotFoundError):
                    sr.reconstruct_sim9_stack(
                        stack,
                        wavelength_nm=488,
                        otf_path=str(otf_path),
                        device="cpu",
                        use_saved_params=True,
                        estimated_params_path=str(mat_path),
                        saved_params_fallback="fail",
                    )
        self.assertIsNone(calls.get("estimate_called"))

    def test_saved_params_missing_falls_back_to_estimate_when_configured(self):
        import sim_control.sim_reconstruction as sr

        calls = {"saved_raises": True}
        fake_backend = _make_fake_backend(calls)
        with tempfile.TemporaryDirectory() as temp_dir:
            otf_path = Path(temp_dir) / "otf.tif"
            otf_path.write_bytes(b"fake")
            mat_path = Path(temp_dir) / "params.mat"
            mat_path.write_bytes(b"fake")
            stack = np.ones((9, 2, 3), dtype=np.uint16)

            with mock.patch.object(sr, "_load_backend", return_value=fake_backend):
                result = sr.reconstruct_sim9_stack(
                    stack,
                    wavelength_nm=488,
                    otf_path=str(otf_path),
                    device="cpu",
                    use_saved_params=True,
                    estimated_params_path=str(mat_path),
                    saved_params_fallback="estimate",
                )

        self.assertEqual(calls.get("estimate_called"), 1)
        self.assertTrue(result["metadata"]["fallback_used"])
        self.assertTrue(result["metadata"]["use_saved_params"])

    def test_warm_reconstructor_reuses_single_backend_instance(self):
        import sim_control.sim_reconstruction as sr

        calls = {}
        fake_backend = _make_fake_backend(calls)
        created = {"count": 0}
        real_create = sr._create_in_memory_reconstructor

        def counting_create(backend, *, device, dtype):
            created["count"] += 1
            return real_create(backend, device=device, dtype=dtype)

        with tempfile.TemporaryDirectory() as temp_dir:
            otf_path = Path(temp_dir) / "otf.tif"
            otf_path.write_bytes(b"fake")
            stack = np.ones((9, 2, 3), dtype=np.uint16)

            with mock.patch.object(sr, "_load_backend", return_value=fake_backend), mock.patch.object(
                sr, "_create_in_memory_reconstructor", side_effect=counting_create
            ):
                warm = sr.WarmSIMReconstructor(device="cpu", dtype="single")
                first = warm.reconstruct(stack, wavelength_nm=488, otf_path=str(otf_path))
                second = warm.reconstruct(stack + 1, wavelength_nm=488, otf_path=str(otf_path))

        # 跨两次重建只构造一个引擎实例（热复用）；第二次命中 warm cache。
        self.assertEqual(created["count"], 1)
        self.assertFalse(first["metadata"]["warm_cache_hit"])
        self.assertTrue(second["metadata"]["warm_cache_hit"])

    def test_load_backend_reports_missing_saved_params_methods(self):
        """后端 API 变化（缺 saved-params 方法）应在 _load_backend 加载期就清晰报错。"""
        import sim_control.sim_reconstruction as sr

        sr._BACKEND_CACHE = None
        # 有 estimate 路径方法，但缺 _load_estimated_params_file / _wiener_reconstruct_with_saved_params。
        estimate_methods = {
            name: (lambda self, *a, **k: None)
            for name in (
                "_np_dtype",
                "_now",
                "_ms",
                "_record_timing",
                "_load_2d_tiff",
                "_estimate_parameters",
                "_wiener_reconstruct",
            )
        }
        fake_reconstructor = type("FakeReconstructorMissingSaved", (), estimate_methods)
        fake_module = SimpleNamespace(
            hessian_sim_wiener_default_options=lambda: None,
            SIMWienerGPUReconstructor=fake_reconstructor,
            torch=object(),
            _torch_dtype_from_name=lambda _name: None,
            _complex_dtype=lambda _dtype: None,
            _to_numpy=lambda value: value,
        )
        with mock.patch.object(sr.importlib, "import_module", return_value=fake_module):
            with self.assertRaisesRegex(RuntimeError, "API changed"):
                sr._load_backend()
        # 失败不污染进程级缓存。
        self.assertIsNone(sr._BACKEND_CACHE)


if __name__ == "__main__":
    unittest.main()

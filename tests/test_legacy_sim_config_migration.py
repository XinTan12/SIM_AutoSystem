"""旧版 SIM 配置迁移兼容性测试。

作用：
    覆盖项目历次 schema 变更后的迁移正确性，确保用户旧 JSON 配置在新代码下仍能加载：
        1. ``BackendConfig`` 字段已收敛为 ``fusion_bt_sdk_path/slm_sdk_path/simulation_mode``
           三项；旧 ``deprecated_*`` 字段被丢弃。
        2. ``merge_legacy_sim_control_payload`` 在 ``control_wangbo/main.py`` 内的合并
           逻辑同样应忽略已废弃字段。
        3. v1→v2 迁移会补齐 ``backend.simulation_mode=False``；v2→v3 会补
           ``selected_running_order=""``。
        4. ``selected_running_order`` round-trip 通过 ``app_config_*_dict``。
        5. ``config_path`` 字段不会被持久化到 JSON（避免暴露本机绝对路径）；
           但 ``save_app_config`` 仍把它保留在内存 AppConfig 中以便下次保存。
        6. 旧 ``laser_640`` / ``laser_647`` 命名迁移到 ``laser_638``（``selected_laser_nm=640/647 → 638``）。
        7. ``apply_real_hardware_preference``（``control_wangbo/main.py``）能用真实
           设备列表覆盖旧 placeholder 标识，且在无设备时保留原标签。

协作关系：
    上游：``unittest``、``tempfile``、``json``。
    下游：``sim_control.config_store``、``sim_control.models``、``control_wangbo.main``。

维护要点：
    - 每次 ``CURRENT_CONFIG_VERSION`` +1 时必须在此添加对应迁移测试用例。
    - ``control_wangbo`` 模块只读，但其 helper（``merge_legacy_sim_control_payload``、
      ``apply_real_hardware_preference``）的行为锁定在这里。
"""

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
# ``control_wangbo.main`` 用裸 import 导入同目录模块；将其加入 sys.path 让本测试也能 import。
CONTROL_WANGBO_ROOT = PROJECT_ROOT / "control_wangbo"
if str(CONTROL_WANGBO_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_WANGBO_ROOT))


class LegacySimConfigMigrationTests(unittest.TestCase):
    """覆盖旧 SIM 配置 JSON 字段在当前 schema 下的迁移正确性。"""

    def test_backend_config_only_keeps_real_hardware_settings(self):
        """``BackendConfig`` 当前 schema 允许 5 个字段；多余字段就是迁移失败的信号。"""
        from sim_control.models import BackendConfig

        backend = BackendConfig()

        # 默认值与字段集合都不能再多也不能再少。
        self.assertEqual(backend.fusion_bt_sdk_path, "")
        self.assertEqual(backend.slm_sdk_path, "")
        self.assertEqual(backend.ti2_dll_path, "")
        self.assertEqual(backend.ti2_sdk_module_path, "")
        self.assertFalse(backend.simulation_mode)
        self.assertEqual(
            set(vars(backend).keys()),
            {"fusion_bt_sdk_path", "slm_sdk_path", "ti2_dll_path", "ti2_sdk_module_path", "simulation_mode"},
        )

    def test_legacy_backend_payload_ignores_removed_deprecated_flags(self):
        """``merge_legacy_sim_control_payload`` 应保留新 base_config 的值并丢弃旧字段。"""
        from sim_control.models import AppConfig, BackendConfig, CameraConfig

        # 注意：``control_wangbo`` 默认只读，但本测试只 *读* 其 helper 行为。
        from control_wangbo.main import merge_legacy_sim_control_payload

        # 1) base_config：表示"用户已在新 schema 下设置好的当前真相"。
        base_config = AppConfig(
            backend=BackendConfig(
                fusion_bt_sdk_path="E:/sdk/dcam",
                slm_sdk_path="E:/sdk/r11",
            ),
            camera=CameraConfig(
                device_index=1,
                device_label="1: C15440-20UP [S/N: 501680]",
                roi_width=512,
                roi_height=256,
                exposure_us=1200,
            ),
        )
        # 2) legacy_payload：模拟"旧 JSON 还残留 deprecated_* 字段"。
        legacy_payload = {
            "backend": {
                "deprecated_camera_mode": True,
                "deprecated_slm_mode": True,
                "deprecated_daq_mode": True,
                "fusion_bt_sdk_path": "",
                "slm_sdk_path": "",
            },
            "camera": {
                "device_index": 0,
                "device_label": "legacy camera placeholder",
                "roi_width": 608,
                "roi_height": 304,
                "exposure_us": 25,
            },
        }

        merged = merge_legacy_sim_control_payload(base_config, legacy_payload)

        # 3) backend：保留 base_config 设置；旧 deprecated_* 字段不应出现。
        self.assertEqual(merged.backend.fusion_bt_sdk_path, "E:/sdk/dcam")
        self.assertEqual(merged.backend.slm_sdk_path, "E:/sdk/r11")
        self.assertFalse(merged.backend.simulation_mode)
        self.assertEqual(
            set(vars(merged.backend).keys()),
            {"fusion_bt_sdk_path", "slm_sdk_path", "ti2_dll_path", "ti2_sdk_module_path", "simulation_mode"},
        )
        # 4) camera：legacy_payload 的字段覆盖 base_config（这是 ``merge_legacy_*`` 的设计）。
        self.assertEqual(merged.camera.roi_width, 608)
        self.assertEqual(merged.camera.roi_height, 304)
        self.assertEqual(merged.camera.exposure_us, 25)

    def test_app_config_loader_ignores_removed_deprecated_flags(self):
        """``app_config_from_dict`` 应忽略 backend 字典中的 deprecated 字段。"""
        from sim_control.config_store import app_config_from_dict

        config = app_config_from_dict(
            {
                "backend": {
                    "deprecated_camera_mode": True,
                    "deprecated_slm_mode": True,
                    "deprecated_daq_mode": True,
                    "fusion_bt_sdk_path": "E:/sdk/dcam",
                    "slm_sdk_path": "E:/sdk/r11",
                }
            }
        )

        self.assertEqual(config.backend.fusion_bt_sdk_path, "E:/sdk/dcam")
        self.assertEqual(config.backend.slm_sdk_path, "E:/sdk/r11")
        self.assertFalse(config.backend.simulation_mode)
        self.assertEqual(
            set(vars(config.backend).keys()),
            {"fusion_bt_sdk_path", "slm_sdk_path", "ti2_dll_path", "ti2_sdk_module_path", "simulation_mode"},
        )

    def test_v1_config_migration_adds_simulation_mode_default_false(self):
        """v1 → v2 → v3 链式迁移应补齐 ``simulation_mode=False`` 与 ``selected_running_order=""``。"""
        from sim_control.config_store import app_config_from_dict, app_config_to_dict

        # 1) 加载 v1 配置：未含 simulation_mode、未含 selected_running_order。
        config = app_config_from_dict(
            {
                "config_version": 1,
                "backend": {
                    "fusion_bt_sdk_path": "E:/sdk/dcam",
                    "slm_sdk_path": "E:/sdk/r11",
                },
            }
        )

        # 2) 加载后版本号应升到当前版本；缺失字段被补齐为安全默认。
        self.assertEqual(config.config_version, 12)
        self.assertFalse(config.backend.simulation_mode)
        # 3) round-trip 回 dict 时字段仍存在。
        payload = app_config_to_dict(config)
        self.assertFalse(payload["backend"]["simulation_mode"])
        self.assertEqual(payload["selected_running_order"], "")
        self.assertEqual(payload["config_version"], 12)

    def test_v2_config_migration_adds_selected_running_order_default(self):
        """v2 → v3 迁移应只补 ``selected_running_order``，保留已有 simulation_mode。"""
        from sim_control.config_store import app_config_from_dict, app_config_to_dict

        config = app_config_from_dict(
            {
                "config_version": 2,
                "backend": {
                    "fusion_bt_sdk_path": "E:/sdk/dcam",
                    "slm_sdk_path": "E:/sdk/r11",
                    "simulation_mode": True,
                },
            }
        )

        self.assertEqual(config.config_version, 12)
        self.assertEqual(config.selected_running_order, "")
        payload = app_config_to_dict(config)
        self.assertEqual(payload["selected_running_order"], "")
        self.assertEqual(payload["config_version"], 12)

    def test_v4_config_migration_adds_reconstruction_defaults(self):
        """v4 -> v6 应补齐 SIM9 重建配置，并可 round-trip 到 JSON。"""
        from sim_control.config_store import app_config_from_dict, app_config_to_dict

        config = app_config_from_dict({"config_version": 4})

        self.assertEqual(config.config_version, 12)
        self.assertTrue(config.reconstruction.enabled)
        self.assertEqual(config.reconstruction.backend, "sim_wiener_gpu")
        self.assertEqual(config.reconstruction.otf_488_path, "")
        self.assertEqual(config.reconstruction.output_path, "data/reconstruction")
        payload = app_config_to_dict(config)
        self.assertIn("reconstruction", payload)
        self.assertTrue(payload["reconstruction"]["enabled"])
        self.assertEqual(payload["reconstruction"]["output_path"], "data/reconstruction")
        self.assertEqual(payload["reconstruction"]["theta_ratio"], (1, 1, 1))
        self.assertEqual(payload["config_version"], 12)

    def test_v5_config_migration_enables_reconstruction_and_adds_output_path(self):
        """v5 -> v7 应补 output_path，并将无界面开关的重建默认设为启用。"""
        from sim_control.config_store import app_config_from_dict, app_config_to_dict

        config = app_config_from_dict(
            {
                "config_version": 5,
                "reconstruction": {
                    "enabled": False,
                    "backend": "sim_wiener_gpu",
                    "otf_488_path": "E:/calibration/488_otf.tif",
                },
            }
        )

        self.assertEqual(config.config_version, 12)
        self.assertTrue(config.reconstruction.enabled)
        self.assertEqual(config.reconstruction.output_path, "data/reconstruction")
        payload = app_config_to_dict(config)
        self.assertTrue(payload["reconstruction"]["enabled"])
        self.assertEqual(payload["reconstruction"]["output_path"], "data/reconstruction")
        self.assertEqual(payload["config_version"], 12)

    def test_v6_config_migration_converts_output_tiff_to_output_directory(self):
        """v6 -> v7 应把旧 output_path 文件路径迁移成输出目录。"""
        from sim_control.config_store import app_config_from_dict, app_config_to_dict

        config = app_config_from_dict(
            {
                "config_version": 6,
                "reconstruction": {
                    "enabled": True,
                    "backend": "sim_wiener_gpu",
                    "output_path": "E:/data/reconstruction/sim_reconstruction.tif",
                },
            }
        )

        self.assertEqual(config.config_version, 12)
        self.assertEqual(config.reconstruction.output_path, "E:/data/reconstruction")
        payload = app_config_to_dict(config)
        self.assertEqual(payload["reconstruction"]["output_path"], "E:/data/reconstruction")
        self.assertEqual(payload["config_version"], 12)

    def test_v8_config_migration_raises_low_slm_enable_guard(self):
        """v8 -> v9 应把低于推荐值的 slm_enable_guard_us 抬到 1000 µs。

        背景：R11 数据手册 tHWAT（EXT_RUN -> Active Mode）最大 500 µs，旧默认
        guard=50 µs 会丢失首个 SLM trigger（第一帧黑帧 + 图案错位一帧）。
        """
        from sim_control.config_store import app_config_from_dict, app_config_to_dict

        config = app_config_from_dict(
            {
                "config_version": 8,
                "timing": {
                    "sample_rate_hz": 1_000_000,
                    "edge_pulse_us": 50,
                    "inter_frame_gap_us": 50_000,
                    "slm_enable_guard_us": 50,
                },
            }
        )

        self.assertEqual(config.config_version, 12)
        self.assertEqual(config.timing.slm_enable_guard_us, 1000)
        payload = app_config_to_dict(config)
        self.assertEqual(payload["timing"]["slm_enable_guard_us"], 1000)
        self.assertEqual(payload["config_version"], 12)

    def test_v8_config_migration_keeps_guard_at_or_above_recommended(self):
        """v8 -> v9 不应降低用户已设置的 >= 1000 µs guard。"""
        from sim_control.config_store import app_config_from_dict

        config = app_config_from_dict(
            {
                "config_version": 8,
                "timing": {"slm_enable_guard_us": 2_500},
            }
        )

        self.assertEqual(config.config_version, 12)
        self.assertEqual(config.timing.slm_enable_guard_us, 2_500)

    def test_v9_config_keeps_user_low_guard_without_remigration(self):
        """v9 配置前进到当前版本（补 Ti2 路径与重建新字段），不重跑 v8->v9 的 guard 抬升：
        用户显式调低的 guard 原样保留。"""
        from sim_control.config_store import app_config_from_dict

        config = app_config_from_dict(
            {
                "config_version": 9,
                "timing": {"slm_enable_guard_us": 600},
            }
        )

        self.assertEqual(config.config_version, 12)
        self.assertEqual(config.timing.slm_enable_guard_us, 600)

    def test_v9_to_v10_migration_adds_ti2_path_defaults(self):
        """v9 -> v10 应补齐 BackendConfig 的 Ti2 路径字段（默认空串），不改其它字段。"""
        from sim_control.config_store import app_config_from_dict, app_config_to_dict

        config = app_config_from_dict(
            {
                "config_version": 9,
                "backend": {"simulation_mode": True},
            }
        )

        self.assertEqual(config.config_version, 12)
        self.assertEqual(config.backend.ti2_dll_path, "")
        self.assertEqual(config.backend.ti2_sdk_module_path, "")
        self.assertTrue(config.backend.simulation_mode)
        payload = app_config_to_dict(config)
        self.assertEqual(payload["config_version"], 12)
        self.assertEqual(payload["backend"]["ti2_dll_path"], "")

    def test_v10_config_preserves_user_ti2_paths(self):
        """v10 配置已填的 Ti2 路径在加载后原样保留。"""
        from sim_control.config_store import app_config_from_dict

        config = app_config_from_dict(
            {
                "config_version": 10,
                "backend": {
                    "ti2_dll_path": "C:/custom/Ti2_Mic_Driver.dll",
                    "ti2_sdk_module_path": "E:/proj/z-scan/ti2_sdk.py",
                },
            }
        )

        self.assertEqual(config.config_version, 12)
        self.assertEqual(config.backend.ti2_dll_path, "C:/custom/Ti2_Mic_Driver.dll")
        self.assertEqual(config.backend.ti2_sdk_module_path, "E:/proj/z-scan/ti2_sdk.py")

    def test_v10_to_v11_migration_adds_saved_params_defaults_safely(self):
        """v10 -> v11 应补齐 saved-params/异步落盘字段；旧配置无 .mat 时强制 use_saved_params=false。"""
        from sim_control.config_store import app_config_from_dict, app_config_to_dict

        config = app_config_from_dict(
            {
                "config_version": 10,
                "reconstruction": {
                    "enabled": True,
                    "backend": "sim_wiener_gpu",
                    "otf_488_path": "E:/calibration/488_otf.tif",
                },
            }
        )

        self.assertEqual(config.config_version, 12)
        # 仿真优先：旧配置无 estimated_params_*_path -> use_saved_params 必须为 False。
        self.assertFalse(config.reconstruction.use_saved_params)
        self.assertEqual(config.reconstruction.saved_params_fallback, "fail")
        self.assertEqual(config.reconstruction.estimated_params_488_path, "")
        self.assertTrue(config.reconstruction.save_reconstruction_output)
        self.assertTrue(config.reconstruction.async_save_reconstruction_output)
        self.assertEqual(config.reconstruction.save_queue_maxsize, 4)
        payload = app_config_to_dict(config)
        self.assertFalse(payload["reconstruction"]["use_saved_params"])
        self.assertIn("estimated_params_638_path", payload["reconstruction"])

    def test_use_saved_params_requires_existing_mat_for_selected_wavelength(self):
        """启用 saved-params 时，所选波长 .mat 缺失/不存在应被 validate_app_config 拦截。"""
        from sim_control.config_store import validate_app_config
        from sim_control.models import AppConfig, ReconstructionConfig

        config = AppConfig(
            selected_laser_nm=488,
            reconstruction=ReconstructionConfig(
                enabled=True,
                otf_488_path=__file__,  # OTF 用一个存在的文件占位
                use_saved_params=True,
                estimated_params_488_path="",  # 缺失 -> 报错
            ),
        )
        errors = validate_app_config(config)
        self.assertTrue(any("estimated_params_488_path" in error for error in errors))

        config.reconstruction.estimated_params_488_path = "this/path/does/not/exist.mat"
        errors = validate_app_config(config)
        self.assertTrue(any("does not exist" in error for error in errors))

    def test_default_app_config_writes_current_schema_version(self):
        """全新默认 AppConfig 写盘必须标当前 schema 版本，且 models 默认值与之一致。"""
        from sim_control.config_store import CURRENT_CONFIG_VERSION, app_config_to_dict
        from sim_control.models import AppConfig

        self.assertEqual(AppConfig().config_version, CURRENT_CONFIG_VERSION)
        payload = app_config_to_dict(AppConfig())
        self.assertEqual(payload["config_version"], CURRENT_CONFIG_VERSION)

    def test_selected_running_order_round_trips_through_config_dict(self):
        """``selected_running_order`` 应能保存到 dict 后再加载回来。"""
        from sim_control.config_store import app_config_from_dict, app_config_to_dict
        from sim_control.models import AppConfig

        config = AppConfig(selected_running_order="488_3.5_2d_1ms")

        payload = app_config_to_dict(config)
        loaded = app_config_from_dict(payload)

        self.assertEqual(loaded.selected_running_order, "488_3.5_2d_1ms")

    def test_config_dict_omits_machine_specific_config_path(self):
        """``app_config_to_dict`` 不应把 ``config_path``（本机绝对路径）写入 JSON。"""
        from sim_control.config_store import app_config_to_dict
        from sim_control.models import AppConfig

        # 故意填一个 Windows 绝对路径；预期不会出现在 dict 中。
        config = AppConfig(config_path=r"E:\Intelligent_SR\SIM_AutoSystem\config\sim_control_config.json")

        payload = app_config_to_dict(config)

        self.assertNotIn("config_path", payload)

    def test_save_app_config_keeps_runtime_path_without_persisting_it(self):
        """``save_app_config`` 内存中保留 ``config_path``，但写盘 JSON 内不含。"""
        import json
        import tempfile

        from sim_control.config_store import save_app_config
        from sim_control.models import AppConfig

        # 用临时目录保存，避免污染仓库内 config 文件。
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "portable_config.json"
            config = AppConfig()

            saved_path = save_app_config(config, config_path)
            payload = json.loads(config_path.read_text(encoding="utf-8"))

        # 1) save_app_config 返回值 = 实际保存路径；config.config_path 也被更新。
        self.assertEqual(saved_path, config_path)
        self.assertEqual(config.config_path, str(config_path))
        # 2) 但 JSON 内容不应含 config_path 字段。
        self.assertNotIn("config_path", payload)

    def test_app_config_loader_defaults_camera_bit_depth_to_16_for_legacy_payloads(self):
        """旧配置不含 bit_depth 时应默认补 16。"""
        from sim_control.config_store import app_config_from_dict

        config = app_config_from_dict(
            {
                "camera": {
                    "device_index": 0,
                    "device_label": "legacy camera placeholder",
                }
            }
        )

        self.assertEqual(config.camera.bit_depth, 16)

    def test_app_config_loader_migrates_legacy_640_laser_fields_to_638(self):
        """旧 ``laser_640_line`` 与 ``selected_laser_nm=640`` 应自动改名为 638。"""
        from sim_control.config_store import app_config_from_dict, app_config_to_dict

        # 1) 注入 v0 旧字段；迁移后必须看不到 640 命名。
        config = app_config_from_dict(
            {
                "daq": {
                    "device_name": "Dev2",
                    "slm_enable_line": "Dev2/port0/line0",
                    "slm_trigger_line": "Dev2/port0/line1",
                    "slm_finish_line": "Dev2/port0/line2",
                    "camera_trigger_line": "Dev2/port0/line5",
                    "laser_405_line": "Dev2/port0/line8",
                    "laser_488_line": "Dev2/port0/line6",
                    "laser_561_line": "Dev2/port0/line7",
                    "laser_640_line": "Dev2/port0/line9",
                },
                "selected_laser_nm": 640,
            }
        )

        # 2) 数据类层面：``selected_laser_nm`` 改为 638；``laser_638_line`` 字段值正确。
        self.assertEqual(config.selected_laser_nm, 638)
        self.assertEqual(getattr(config.daq, "laser_638_line", None), "Dev2/port0/line9")
        self.assertFalse(hasattr(config.daq, "laser_640_line"))
        self.assertFalse(hasattr(config.daq, "laser_647_line"))

        # 3) round-trip 回 dict 后旧字段也不应出现。
        payload = app_config_to_dict(config)

        self.assertEqual(payload["selected_laser_nm"], 638)
        self.assertEqual(payload["daq"].get("laser_638_line"), "Dev2/port0/line9")
        self.assertNotIn("laser_640_line", payload["daq"])
        self.assertNotIn("laser_647_line", payload["daq"])

    def test_v11_config_migrates_647_laser_and_reconstruction_fields_to_638(self):
        """v11 配置里的 ``647`` 红光字段应升级为 v12 的 ``638`` schema。"""
        from sim_control.config_store import app_config_from_dict, app_config_to_dict

        config = app_config_from_dict(
            {
                "config_version": 11,
                "daq": {
                    "device_name": "Dev2",
                    "slm_enable_line": "Dev2/port0/line0",
                    "slm_trigger_line": "Dev2/port0/line1",
                    "slm_finish_line": "Dev2/port0/line2",
                    "camera_trigger_line": "Dev2/port0/line5",
                    "laser_405_line": "Dev2/port0/line8",
                    "laser_488_line": "Dev2/port0/line6",
                    "laser_561_line": "Dev2/port0/line7",
                    "laser_647_line": "Dev2/port0/line9",
                },
                "selected_laser_nm": 647,
                "reconstruction": {
                    "enabled": True,
                    "backend": "sim_wiener_gpu",
                    "otf_647_path": "E:/calibration/647_otf.tif",
                    "estimated_params_647_path": "E:/calibration/647_params.mat",
                },
            }
        )

        self.assertEqual(config.config_version, 12)
        self.assertEqual(config.selected_laser_nm, 638)
        self.assertEqual(config.daq.laser_638_line, "Dev2/port0/line9")
        self.assertEqual(config.reconstruction.otf_638_path, "E:/calibration/647_otf.tif")
        self.assertEqual(
            config.reconstruction.estimated_params_638_path,
            "E:/calibration/647_params.mat",
        )

        payload = app_config_to_dict(config)
        self.assertNotIn("laser_647_line", payload["daq"])
        self.assertEqual(payload["daq"]["laser_638_line"], "Dev2/port0/line9")
        self.assertNotIn("otf_647_path", payload["reconstruction"])
        self.assertNotIn("estimated_params_647_path", payload["reconstruction"])
        self.assertEqual(payload["reconstruction"]["otf_638_path"], "E:/calibration/647_otf.tif")

    def test_real_device_detection_replaces_stale_camera_identity(self):
        """``apply_real_hardware_preference`` 在真机存在时应替换 placeholder 标签。"""
        from sim_control.models import AppConfig, CameraConfig

        from control_wangbo.main import apply_real_hardware_preference

        # 1) 旧 placeholder 配置；真实设备列表给出真实 Hamamatsu 信息。
        config = AppConfig(
            camera=CameraConfig(
                device_index=0,
                device_label="legacy camera placeholder",
            ),
        )
        camera_devices = [
            {
                "index": 0,
                "display": "0: C15440-20UP [S/N: 501679]",
                "camera_id": "S/N: 501679",
            }
        ]
        slm_devices = [
            {
                "path": r"\\?\usb#vid_19ec&pid_0503#0175000845#{54ed7ac9-cc23-4165-be32-79016bafb950}",
                "display": "0175000845",
            }
        ]
        daq_devices = ["Dev2"]

        updated = apply_real_hardware_preference(
            config,
            camera_devices=camera_devices,
            slm_devices=slm_devices,
            daq_devices=daq_devices,
        )

        # 2) 替换后 device_label 应来自真实设备 display 字符串。
        self.assertEqual(updated.camera.device_index, 0)
        self.assertEqual(updated.camera.device_label, "0: C15440-20UP [S/N: 501679]")

    def test_hardware_preference_preserves_camera_identity_when_no_devices_detected(self):
        """无真实设备时 ``apply_real_hardware_preference`` 必须**保留**旧标签，不清空。"""
        from sim_control.models import AppConfig, CameraConfig

        from control_wangbo.main import apply_real_hardware_preference

        config = AppConfig(
            camera=CameraConfig(
                device_index=0,
                device_label="legacy camera placeholder",
            ),
        )

        updated = apply_real_hardware_preference(
            config,
            camera_devices=[],
            slm_devices=[],
            daq_devices=[],
        )

        # 真机列表为空时不能丢失用户记忆的"上次连接的相机标签"。
        self.assertEqual(updated.camera.device_label, "legacy camera placeholder")


if __name__ == "__main__":
    unittest.main()

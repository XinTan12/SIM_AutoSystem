"""项目启动入口命名与职责的回归测试。

作用：
    本测试文件锁死三个易漂移事实，避免后续重构再次错放入口：
        1. 仓库根 ``app.py`` 必须启动 ``control_wangbo`` 集成主界面，**不**含
           ``SimControlWindow``；并保证旧路径 ``sim_control_app.py`` 已被移除。
        2. Windows 启动脚本 ``start.cmd`` 仍调用 ``.venv\\Scripts\\python.exe app.py``，
           不再调用任何 ``sim_control_app.py`` / 旧脚本名。
        3. 独立 SIM 采集 GUI 入口保留在 ``sim_control/sim_acquisition_app.py``，并支持
           ``--config`` 命令行参数。

协作关系：
    上游：``unittest``、文件系统。
    下游：``app.py``、``start.cmd``、``sim_control/sim_acquisition_app.py``。

维护要点：
    - 2026-05-11 决策日志确认入口命名约定；改动入口时务必同步本测试。
    - 仅做文件存在与文本子串断言，不真正启动 Qt 应用。
"""

from pathlib import Path


# 项目根 = 本测试文件向上两层目录；用 ``resolve()`` 处理 symlink 与相对路径。
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_project_app_entrypoint_targets_integrated_main_window():
    """``app.py`` 应启动 control_wangbo 集成主界面，而不是独立 SIM 窗口。"""
    # 1) 入口文件必须存在；缺失即重构错放。
    app_path = PROJECT_ROOT / "app.py"

    assert app_path.exists()
    source = app_path.read_text(encoding="utf-8")

    # 2) 必含 ``control_wangbo`` 与 ``MainWindow`` 字符串，且不引入 ``SimControlWindow``。
    assert "control_wangbo" in source
    assert "MainWindow" in source
    assert "SimControlWindow" not in source
    # 3) 旧顶层 ``sim_control_app.py`` 必须被删除（2026-05-11 入口迁移决策）。
    assert not (PROJECT_ROOT / "sim_control_app.py").exists()


def test_start_script_runs_project_app_entrypoint():
    """``start.cmd`` 必须调用根目录 ``app.py``，不能误指向旧 ``sim_control_app.py``。"""
    # 1) 文件存在性 + 文本内容双重锁定。
    start_script = PROJECT_ROOT / "start.cmd"

    assert start_script.exists()
    source = start_script.read_text(encoding="utf-8")

    # 2) 必须用项目 ``.venv`` 的 python 启动 ``app.py``；其它路径都视为错。
    assert '"%ROOT%.venv\\Scripts\\python.exe"' in source
    assert '"%ROOT%app.py"' in source
    # 3) 旧入口字符串与旧脚本文件名都不应出现，避免 hybrid 状态。
    assert "sim_control_app.py" not in source
    assert not (PROJECT_ROOT / "start_sim_control.cmd").exists()


def test_sim_acquisition_app_preserves_standalone_sim_gui_entrypoint():
    """独立 SIM 入口必须仍指向 ``SimControlWindow`` 并支持 ``--config``。"""
    sim_app_path = PROJECT_ROOT / "sim_control" / "sim_acquisition_app.py"

    assert sim_app_path.exists()
    source = sim_app_path.read_text(encoding="utf-8")

    # 1) 独立入口启动独立窗口，不应误引集成主界面。
    assert "SimControlWindow" in source
    assert "control_wangbo" not in source
    # 2) ``--config`` 命令行参数保留，便于调试不同配置文件。
    assert "--config" in source

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_project_app_entrypoint_targets_integrated_main_window():
    app_path = PROJECT_ROOT / "app.py"

    assert app_path.exists()
    source = app_path.read_text(encoding="utf-8")

    assert "control_wangbo" in source
    assert "MainWindow" in source
    assert "SimControlWindow" not in source
    assert not (PROJECT_ROOT / "sim_control_app.py").exists()


def test_start_script_runs_project_app_entrypoint():
    start_script = PROJECT_ROOT / "start.cmd"

    assert start_script.exists()
    source = start_script.read_text(encoding="utf-8")

    assert '"%ROOT%.venv\\Scripts\\python.exe"' in source
    assert '"%ROOT%app.py"' in source
    assert "sim_control_app.py" not in source
    assert not (PROJECT_ROOT / "start_sim_control.cmd").exists()


def test_sim_acquisition_app_preserves_standalone_sim_gui_entrypoint():
    sim_app_path = PROJECT_ROOT / "sim_control" / "sim_acquisition_app.py"

    assert sim_app_path.exists()
    source = sim_app_path.read_text(encoding="utf-8")

    assert "SimControlWindow" in source
    assert "control_wangbo" not in source
    assert "--config" in source

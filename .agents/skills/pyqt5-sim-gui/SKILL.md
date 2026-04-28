---
name: pyqt5-sim-gui
description: Use when editing, reviewing, testing, redesigning, or debugging the SIM_AutoSystem PyQt5 GUI, including sim_control_app.py, sim_control/gui.py, sim_control/ui_*.py, .ui files, QThread/pyqtSignal workflows, QSS styling, Qt layouts, generated pyuic5 code, and GUI regression tests.
---

# PyQt5 SIM GUI

## Core Rule

Treat this repository as a PyQt5 desktop control system, not a PyQt6, PySide, or web UI project. Preserve real-time acquisition boundaries and hardware simulation paths while improving GUI code.

## First Checks

1. Read `AGENTS.md`, `PROJECT_MEMORY.md`, and `docs/project_memory/decision_log.md` before project work.
2. Confirm the target file type:
   - Hand-written GUI logic: `sim_control/gui.py`, `sim_control_app.py`, controller/pipeline modules.
   - Generated UI code: `sim_control/ui_*.py` or `control_wangbo/*_ui.py`.
   - Qt Designer source: `.ui` files.
3. If touching generated files, prefer editing the `.ui` source and regenerating with `pyuic5`; only edit generated Python directly when the repo already treats that exact file as hand-maintained or the user asks for a surgical patch.
4. Keep `control_wangbo/` read-only unless the user explicitly asks to modify it.

## PyQt5 Compatibility

Use PyQt5 imports and idioms:

```python
from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtWidgets import QApplication, QWidget

return app.exec_()
```

Avoid PyQt6-only patterns:

```python
# Do not introduce these in this project.
Qt.AlignmentFlag.AlignCenter
Qt.CheckState.Checked
app.exec()
from PyQt6 import QtWidgets
from PySide6 import QtWidgets
```

## Threading And Acquisition

- Keep camera acquisition, DAQ timing, and SLM operations off the GUI thread when they can block.
- Communicate across threads with `pyqtSignal`; do not update widgets directly from worker threads.
- Preserve latest-frame-wins preview behavior: GUI polling may drop intermediate frames to stay responsive.
- Keep the real-time acquisition path free of synchronous disk I/O.
- Shutdown threads explicitly with `quit()` and bounded `wait()` when windows close.

## Layout And Styling

- Prefer real Qt layouts over absolute positioning in new SIM-side UI.
- For fixed legacy generated windows, preserve existing geometry unless the task is explicitly to modernize that surface.
- Use object names for QSS selectors when styling specific controls.
- Verify minimum-size behavior for dialogs and dense controls; this repo has tests for overlap and scroll behavior.
- Keep labels concise and operational. SIM control screens are tools, not marketing pages.

## Configuration And Hardware Boundaries

- Keep device names, SDK paths, DAQ line mappings, timing values, and pattern paths configuration-driven.
- Default SIM-side work belongs in `sim_control/` or new top-level modules, not `control_wangbo/`.
- Preserve simulation mode behavior for camera, SLM, and DAQ adapters when real hardware is absent.
- Do not hard-code local SDK or device paths.

## Verification

Run focused checks after GUI changes:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_ui_regressions.py tests\test_main_window_scroll_area.py -q
```

Run broader checks when shared GUI helpers, configuration, controller, preview, or adapter behavior changes:

```powershell
.venv\Scripts\python.exe -m pytest tests\ -q
```

For visual layout changes, instantiate the target dialog/window and process events in a Qt test, then assert geometry, overlap, visibility, enabled states, and signal behavior.

## Common Mistakes

| Mistake | Fix |
|---|---|
| Introducing PyQt6/PySide imports | Keep PyQt5 imports and `exec_()` |
| Editing generated `ui_*.py` casually | Patch `.ui` and regenerate with `pyuic5` when possible |
| Blocking the GUI thread with hardware or disk work | Move blocking work to adapters/workers and emit signals |
| Hard-coding DAQ or SDK paths | Add or use config fields |
| Styling by broad QSS selectors only | Use object names for targeted controls |
| Assuming `control_wangbo/` is free to refactor | Treat it as read-only unless explicitly requested |

## Optional References

Load `references/sim-gui-map.md` when you need the current module map, verification commands, or project-specific PyQt5 conventions in more detail.

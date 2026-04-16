# SIM_AutoSystem

## Project Overview
This folder is the new top-level workspace for the microfluidics + SIM automation project.

## Structure
- `control_wangbo/`: legacy microfluidics control code from your teammate. Treat it as read-only unless explicitly requested.
- `sim_control/`: standalone SIM control GUI, waveform generation, acquisition controller, and placeholder reconstruction/feature pipeline.
- `sim_control_app.py`: SIM GUI launcher.
- `config/sim_control_config.json`: default SIM GUI configuration file.
- `start_sim_control.cmd`: Windows launcher for the SIM GUI.
- `SDK/`: place vendor SDK DLLs, headers, and related files here when integrating real hardware.

## Working Rules
- Do not modify `control_wangbo/` unless the user explicitly asks for it.
- Keep new SIM-side development in `sim_control/` or new top-level modules under this workspace.
- Prefer configuration-driven changes over hard-coded device paths or line mappings.
- Keep the real-time acquisition path free of synchronous disk I/O.
- The near-term integration target is: microfluidic capture in `control_wangbo/` -> SIM 9-frame acquisition in `sim_control/` -> emit a `(9, H, W)` `numpy.uint16` stack to the reconstruction module -> hand the decision result back to the microfluidic flow for `release/sort`.

## Startup
- Preferred working directory: this folder.
- Default config path: `config/sim_control_config.json`.
- Windows launcher: run `start_sim_control.cmd`.
- Manual launch: `python sim_control_app.py --config config/sim_control_config.json`.

## Environment Notes
- The project now runs from the top-level workspace root `E:\Intelligent_SR\SIM_AutoSystem`.
- Use the dedicated root environment in `.venv` for SIM-side work.
- `start_sim_control.cmd` launches `sim_control_app.py` directly with `.venv\Scripts\python.exe`.
- Do not assume any dependency on `control_wangbo/.venv`; that legacy environment is no longer part of the active workflow.
- Keep Python dependencies for SIM-side work centralized in the root `.venv`, including `PyQt5`, `numpy`, `nidaqmx`, and any vendor SDK bindings.

## Hardware Integration Notes
- The active SIM hardware plan is:
- Hamamatsu ORCA-Fusion BT camera controlled through Hamamatsu `DCAM-API` / `DCAM-SDK4`.
- Kopin / Forth Dimension Displays `QXGA-R11-STR` SLM controlled through `R11CommLib` over the vendor-supported stack.
- NI USB-6423 is used to generate synchronized TTL for `slm_enable_line`, `slm_trigger_line`, `slm_finish_line`, `camera_trigger_line`, and the laser trigger lines.
- `FusionBtCameraAdapter` and `KopinSlmAdapter` currently include simulation mode plus extension points for real SDK bindings.
- Real DLL names, loading paths, and function signatures should be integrated via `sim_control/adapters.py`.
- The `SDK/` folder currently includes Hamamatsu `DCAM-SDK4` material and the FDD `R11` bundle with `R11CommLib`, WinUSB drivers, MetroCon, sequence catalogues, and protocol documentation.

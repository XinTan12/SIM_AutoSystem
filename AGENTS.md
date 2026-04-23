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

## 跨对话记忆机制
- 每次新对话开始时，必须先读取 `AGENTS.md`、`PROJECT_MEMORY.md` 和 `docs/project_memory/decision_log.md`，再开始分析、设计或实施。
- 完成上述记忆文件读取后，如果用户请求涉及具体模块、配置或故障，再补充读取与任务直接相关的代码、配置文件、测试文件，以及必要的相关设计文档或最近变更；不要在无必要时全仓库扫描。
- 将 `PROJECT_MEMORY.md` 视为项目当前状态的工作摘要；如果它与代码、配置文件或更具体的文档冲突，以更新且更具体的事实为准，并在本次会话结束前回写记忆文件。
- 只要本次会话对项目做出了修改，无论是代码、配置、文档还是工作流调整，结束前都必须同步更新 `PROJECT_MEMORY.md`。
- 每次发生项目修改时，至少更新 `PROJECT_MEMORY.md` 的“最近更新”部分；如果修改还影响了项目状态、架构边界、接口、依赖、硬件接入方式、关键待办或风险判断，还必须同步更新对应章节内容。
- 只有在关键设计决策、长期约束或方向性选择发生变化时，才更新 `docs/project_memory/decision_log.md`。每条记录至少包含日期、决策、原因和影响。
- 记忆文件只记录提炼后的事实、约束、决策、待办和下一步，不保存整段聊天记录。
- 不要在记忆文件中记录密钥、许可证、口令、个人隐私或无必要的机器本地敏感信息。
- 该记忆机制不改变现有边界约束：`control_wangbo/` 仍默认视为只读，除非用户明确要求修改。

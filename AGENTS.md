# SIM_AutoSystem AI 主指令文件

`AGENTS.md` 是本项目的唯一主指令文件。`CLAUDE.md`、Cursor/Cline/Codex 等其他 AI 工具入口文件只能引用本文件；任何 AI 工具需要更新项目说明、工作边界、硬件约束或跨对话规则时，只修改 `AGENTS.md`，不要在其他入口文件中复制维护同类内容。

## 项目概述

`SIM_AutoSystem` 是面向微流控细胞分选与结构光照明显微成像（SIM）联动的自动化工程。近期集成目标是打通以下链路：

```text
微流控捕获 -> SIM 9 帧结构光采集 -> (9, H, W) numpy.uint16 图像栈 -> 重建/特征分析 -> 决策回传 release/sort
```

当前重点是保持 SIM GUI、采集控制、波形生成、硬件适配和占位重建/特征 pipeline 在无真实硬件时可通过仿真模式运行，并逐步接入 Hamamatsu 相机、Kopin/FDD SLM 与 NI USB-6423 的真实硬件链路。

## 环境与启动

- 当前仓库根目录：`F:\SIM_AutoSystem`。
- 默认配置路径：`config/sim_control_config.json`。
- SIM 侧统一使用根目录 `.venv` 作为 Python 环境。
- 不依赖 `control_wangbo/.venv`；该旧环境不属于当前主流程。
- 主要 Python 依赖集中在根目录 `.venv`：`numpy`, `PyQt5`, `nidaqmx`, `tifffile`, `opencv-python`, `pandas`, `matplotlib`，以及后续真实厂商 SDK Python 绑定。
- Windows 启动脚本：`start_sim_control.cmd`，它会通过 `.venv\Scripts\python.exe` 启动 `sim_control_app.py`。
- 手动启动：`.\.venv\Scripts\python.exe sim_control_app.py --config config\sim_control_config.json`。

## 常用命令

```powershell
# 启动 GUI
.\.venv\Scripts\python.exe sim_control_app.py --config config\sim_control_config.json

# Windows 启动脚本
.\start_sim_control.cmd

# 基准测试命令
.\.venv\Scripts\python.exe -m unittest discover -s tests -q

# pytest 可用时运行测试
.\.venv\Scripts\python.exe -m pytest tests\ -q

# 从 .ui 重新生成 Python UI 代码
.\.venv\Scripts\pyuic5.exe sim_control\sim_settings_dialog.ui -o sim_control\ui_sim_settings_dialog.py
```

## 目录边界

- `sim_control/`：SIM 侧主开发区，包含 GUI、采集控制、波形生成、预览、适配器、配置读写、数据模型和占位 pipeline。
- `control_wangbo/`：队友历史微流控控制代码，默认只读；除非用户明确要求，否则不要修改。
- `sim_control_app.py`：SIM GUI 顶层启动入口。
- `config/sim_control_config.json`：默认 SIM 配置文件，承载设备、时序、后端、SDK 路径和 DAQ 线位配置。
- `start_sim_control.cmd`：Windows 下优先使用的启动脚本。
- `SDK/`：本地厂商 SDK、驱动和资料目录；厂商 SDK 文件不入版本管理，仓库只保留 `SDK/README.md` 的目录约定。
- `tests/`：回归测试、适配器测试和 UI 行为测试。

## 架构分层

```text
models.py                  数据类定义（AppConfig, DaqLineConfig, CameraConfig, TimingConfig 等）
config_store.py            JSON 配置读写、schema 版本迁移和旧配置兼容
adapters.py                相机、SLM、USB-6423 硬件适配器与真实 SDK 扩展点
sim_adapters.py            无硬件仿真适配器
protocols.py               CameraAdapter / SlmAdapter / DaqAdapter 协议边界
acquisition_core.py        脱离 Qt 的采集核心逻辑
waveform.py                NI USB-6423 DAQ 波形生成与打包
controller.py              采集控制器（QThread worker 模式）
preview.py                 live 预览（线程安全最新帧快照 + QTimer 主线程轮询）
pipeline.py                占位重建/特征/决策 pipeline
gui.py                     SimControlWindow + SimSettingsDialog
summary.py                 SIM 设置摘要文本生成
sim_camera_presets.py      相机 ROI 预设与对齐工具
ui_sim_settings_dialog.py  pyuic5 生成代码，勿手动修改
```

## 关键约束

- 配置驱动：设备路径、TTL 线位、SDK 定位和后端选择不要硬编码，优先走 `config/sim_control_config.json`。
- 实时采集路径禁止同步磁盘 I/O；采集链路应保持低延迟、可中止。
- 仿真优先：无真实硬件时仍应能验证 GUI、配置和控制流程。
- `control_wangbo/` 默认只读，新 SIM 侧开发优先放在 `sim_control/` 或新的顶层模块。
- NI USB-6423 规范接线固定为：`slm_enable=0, slm_trigger=1, slm_finish=2, camera_trigger=5, laser_405=8, laser_488=6, laser_561=7, laser_647=9`。
- 第四路红光统一命名为 `647`，旧 `640` 配置通过迁移逻辑兼容。
- SIM live 预览采用 `latest-frame-wins`：采集线程持续更新最新帧快照，GUI 端轮询显示最新帧，允许丢弃中间帧以避免旧帧积压。
- `ui_sim_settings_dialog.py` 由 `pyuic5` 从 `.ui` 生成；修改 UI 时编辑 `.ui` 后重新生成，不直接手改生成文件。
- `SimSettingsDialog` 不拥有 SLM 生命周期；集成主界面与 SIM controller 必须共享同一个 `slm_adapter`，避免 R11 WinUSB 设备被重复打开。
- 正式 SIM 采集要求 SLM 已连接并选择匹配 Running Order；未连接时应阻止采集，不回退到空 `pattern_files`。
- 后续 SIM9 采集以 NI USB-6423 波形输出为唯一 DAQ 控制路径；不要恢复 `daq_backend`、`pxie7857r`、`rio_lines`、`nifpga`、`NIRioDaqAdapter`、NI-RIO bitfile preflight 或 PXIe-7857R LabVIEW rebuild 入口。

## 硬件接入

- 相机：Hamamatsu ORCA-Fusion BT，通过 `DCAM-API` / `DCAM-SDK4` 控制。`FusionBtCameraAdapter` 已包含仿真模式和真实 SDK 绑定扩展点；真实 DLL/模块名、路径和函数签名通过 `sim_control/adapters.py` 集成。
- SLM：Kopin / Forth Dimension Displays `QXGA-R11-STR`，通过 `R11CommLib` over WinUSB 厂商栈控制。`KopinSlmAdapter` 已包含仿真模式、Running Order 枚举/选择和真实 SDK 扩展点。
- DAQ：当前 SIM9 采集只使用 NI USB-6423 输出同步 TTL，驱动 `slm_enable_line`、`slm_trigger_line`、`slm_finish_line`、`camera_trigger_line` 和各激光触发线。
- `SDK/` 本地材料包含 Hamamatsu `DCAM-SDK4` 与 FDD `R11` bundle，包括 `R11CommLib`、WinUSB 驱动、MetroCon、sequence catalogue 和协议文档；仓库只跟踪 `SDK/README.md` 的目录约定。
- SLM 正式采集路径使用预烧录 Running Order：主界面连接 SLM 后，按当前波长与相机曝光自动选择 `3.5/2d`、非 `_ang0` RO；RO 模式以 `PatternPreparationResult(handles=[-1], metadata["mode"]="running_order")` 表示。
- 当前 `.repz11` RO 的 1ms/10ms/50ms 循环能力由 repertoire 内 `[HWA h]` 与 FINISH-controlled loop 定义提供；不要启用占用 SPI_1/SPI_2 的 RO Selection 替代模式，否则会破坏 TRIGGER/FINISH 语义。

## 编码风格

- Python 3.12+，新 Python 文件使用 `from __future__ import annotations`。
- 类型注解使用 `X | None`，不要新增 `Optional[X]` 风格。
- GUI 使用 PyQt5，遵守现有 QThread / pyqtSignal / QTimer 边界。
- 硬件适配逻辑集中在 `sim_control/adapters.py`，不要散落到 GUI 代码中。
- 测试中 `unittest` 与 `pytest` 风格并存；新增测试优先贴近周边文件风格。

## 跨对话记忆机制

- 每次新对话开始时，必须先读取 `AGENTS.md`、`PROJECT_MEMORY.md` 和 `docs/project_memory/decision_log.md`，再开始分析、设计或实施。
- `AGENTS.md` 记录长期项目规则、架构边界、硬件约束和 AI 工具主指令；不要把它维护成最近更新流水账。
- `PROJECT_MEMORY.md` 记录当前状态、最近更新、风险和待办；如果它与代码、配置或更具体文档冲突，以更新且更具体的事实为准，并在本次会话结束前回写。
- `docs/project_memory/decision_log.md` 仅记录关键设计决策、长期约束或方向性选择。每条记录至少包含日期、决策、原因和影响。
- 完成三件套读取后，如果用户请求涉及具体模块、配置或故障，再补充读取直接相关的代码、配置、测试和必要设计文档；不要无必要全仓库扫描。
- 只要本次会话修改了项目，无论是代码、配置、文档还是工作流调整，结束前必须同步更新 `PROJECT_MEMORY.md`。至少更新“最近更新”；如影响项目状态、架构边界、接口、依赖、硬件接入方式、关键待办或风险判断，也同步更新对应章节。
- 只有关键设计决策、长期约束或方向性选择发生变化时，才更新 `docs/project_memory/decision_log.md`。
- 记忆文件只记录提炼后的事实、约束、决策、待办和下一步，不保存整段聊天记录。
- 不要在记忆文件中记录密钥、许可证、口令、个人隐私或无必要的机器本地敏感信息。
- 该记忆机制不改变 `control_wangbo/` 默认只读边界。

# SIM_AutoSystem AI 主指令文件

`AGENTS.md` 是本项目的唯一主指令文件。`CLAUDE.md`、Cursor/Cline/Codex 等其他 AI 工具入口文件只能引用本文件；任何 AI 工具需要更新项目说明、工作边界、硬件约束或跨对话规则时，只修改 `AGENTS.md`，不要在其他入口文件中复制维护同类内容。

## 项目概述

`SIM_AutoSystem` 是面向微流控细胞分选与结构光照明显微成像（SIM）联动的自动化工程。近期集成目标是打通以下链路：

```text
微流控捕获 -> SIM 9 帧结构光采集 -> (9, H, W) numpy.uint16 图像栈 -> 重建/特征分析 -> 决策回传 release/sort
```

当前重点是保持 SIM GUI、采集控制、波形生成、硬件适配和占位重建/特征 pipeline 在无真实硬件时可通过仿真模式运行，并逐步接入 Hamamatsu 相机、Kopin/FDD SLM 与 NI USB-6423 的真实硬件链路。

## 环境与启动

- 当前仓库根目录以本地实际工作区为准；本机当前工作区为 `E:\Intelligent_SR\SIM_AutoSystem`。
- 默认配置路径：`config/sim_control_config.json`。
- SIM 侧统一使用根目录 `.venv` 作为 Python 环境。
- 不依赖 `control_wangbo/.venv`；该旧环境不属于当前主流程。
- 主要 Python 依赖集中在根目录 `.venv`：`numpy`, `PyQt5`, `nidaqmx`, `tifffile`, `opencv-python`, `pandas`, `matplotlib`，以及后续真实厂商 SDK Python 绑定。
- Windows 启动脚本：`start.cmd`，它会通过 `.venv\Scripts\python.exe` 启动 `app.py`。
- 手动启动项目集成 GUI：`.\.venv\Scripts\python.exe app.py`。
- 手动启动独立 SIM 采集 GUI：`.\.venv\Scripts\python.exe -m sim_control.sim_acquisition_app --config config\sim_control_config.json`。

## 常用命令

```powershell
# 启动 GUI
.\.venv\Scripts\python.exe app.py

# Windows 启动脚本
.\start.cmd

# 启动独立 SIM 采集 GUI
.\.venv\Scripts\python.exe -m sim_control.sim_acquisition_app --config config\sim_control_config.json

# 基准测试命令
.\.venv\Scripts\python.exe -m unittest discover -s tests -q

# pytest 可用时运行测试
.\.venv\Scripts\python.exe -m pytest tests\ -q

# 从 .ui 重新生成 Python UI 代码
.\.venv\Scripts\pyuic5.exe sim_control\sim_settings_dialog.ui -o sim_control\ui_sim_settings_dialog.py
```

## Git 工作流

- `main` / `origin/main` 是稳定主线；只有通过测试、review 和必要真机验收的代码才合入。
- `dev` / `origin/dev` 是当前大改动开发分支；本地默认开发工作区为 `E:\Intelligent_SR\SIM_AutoSystem`。
- `E:\Intelligent_SR\SIM_AutoSystem-stable` 是稳定版 worktree，使用 detached `origin/main`，只用于运行远程稳定版、真机对照和回退参考，不在其中开发或提交。
- 大改动合入流程：`dev` 本地测试通过 -> push `origin/dev` -> GitHub PR -> merge 到 `origin/main` -> stable worktree 更新到最新 `origin/main`。
- 清理分支时保留 `main` 和 `dev`；旧 `codex/*`、`backup/*` 分支仅在确认无依赖后删除。

## 沟通与回复语言

- 每次执行任务前，必须先阅读本节规则。
- 回答用户与展示思考过程时统一使用中文，除非用户在当前请求中明确要求其他语言。
- 代码、命令、文件路径、标识符、API/函数名、英文专有名词和必要的英文术语保持原样，不强行翻译；中文叙述里可正常内嵌这些英文片段。
- 该规则只约束面向用户的自然语言表达，不改变代码、注释、配置或提交信息既有的语言约定。

## 任务执行方式（并行子代理）

- 每次执行任务前，必须先阅读本节规则。
- 对于复杂任务，必须先把任务分解为多个相互独立的子任务，再用多个 subagent 同时并行处理，而不是由主 agent 串行包办。
- 任务分解原则：子任务之间尽量无共享状态、无顺序依赖，可独立完成；有前后依赖的部分按阶段拆分，阶段内并行、阶段间串行。
- 启动并行 subagent 时，在同一条消息中一次性发起多个 subagent 调用，使其真正并发执行。
- 简单或单步任务无需强行拆分；并行化只用于能从分解中获益的复杂任务。
- 如果当前工具环境不支持或无法启动并行 subagent，主 agent 必须在继续前向用户说明原因并请求确认，不得静默用主 agent 串行处理替代。
- 本节与“修改后审查机制”互补：并行子代理用于*执行*复杂任务，独立 subagent review 用于*审查*已完成的修改。
- 例外：codex review 主审查线**不适用本节并行**，须按“Codex 双循环审查工作流”节用主对话 `/codex:rescue` 单线续接（同一 codex 会话）；并行只用于 Claude subagent（执行或旁路核对）。

## 修改后审查机制

- 每次对项目进行了修改后，都必须使用独立 subagent 对本次改动进行 review 审查；审查对象应覆盖实际修改的代码、配置、文档、测试和工作流文件。
- subagent review 必须明确回答三类问题：
  1. 哪些项目标准或用户要求已满足，证据是什么；
  2. 哪些项目标准或用户要求未满足，原因是什么；
  3. 哪些地方需要用户人工确认、真机确认或额外业务判断。
- 如果当前工具环境不支持或无法启动独立 subagent，主 agent 必须在继续前向用户说明原因并请求确认；不得用主 agent 自审静默替代 subagent review。
- 主 agent 在最终回复前应结合 subagent review 结果处理可立即修复的问题；无法自动处理的事项必须在最终回复中列为人工确认项。
- **与 codex 双循环的去重**：若本次改动已完成 codex 双循环**第二循环（实现验证）并通过**，该 codex 实现审查已覆盖“审查已完成修改”的职责，**非高风险**改动**可不再单独做独立 subagent review**；但**高风险改动**（硬件相机/SLM/DAQ 控制、波形/时序逻辑、安全或破坏性操作，判据同「Codex 双循环审查工作流」适用范围）即使 codex 第二循环通过，**仍须做一次独立 subagent review** 作双保险。
- **兜底（硬约束）**：未走 codex 双循环、仅走第一循环（方案审查）而未走第二循环、双循环被豁免（简单改动 / 文档记忆类）或 codex 降级不可用的改动，**仍必须做独立 subagent review**。任何实质改动**至少经过一种实现审查**（codex 第二循环 或 subagent review），**绝不允许两者都跳过**。

## Codex 双循环审查工作流（Claude Code 代码修改的分级审查流程）

- 每次执行任务前，必须先阅读本节规则。本节与“任务执行方式（并行子代理）”“修改后审查机制”互补：codex 双循环是**外部交叉审查**（事前审方案、事后验实现）；codex 双循环与独立 subagent review 的**去重见「修改后审查机制」**——codex 第二循环（实现验证）通过后，非高风险改动可免 subagent review，高风险仍双审。
- **触发主体（关键）**：本工作流的“被审查方”固定为 **Claude Code**——只有当 Claude Code 对本工作区做实质性代码/配置修改、或产出修改方案时才**可能**触发（是否真正走双循环，再按下文「适用范围」的复杂度/风险分级判定），由 codex 作为**外部只读交叉审查者**审 Claude 的方案与实现。**当用户直接用 Codex、或其他 AI 工具直接编写/修改代码时，本工作流不触发**：Codex 不得把本节当作“改代码就要自审”套到自己头上做方案自审；此时是否再审、由谁审，由用户当时决定，本规则不强制。理由：codex 自审自己的方案会丧失“外部交叉”的独立性，与本节初衷相悖。
- 审查由 OpenAI 官方 Codex 插件（marketplace `openai-codex` / plugin `codex`）承担，依赖 PATH 中的 `codex` CLI（`npm install -g @openai/codex`；本机已装 `codex-cli 0.142.0`，复用 `~/.codex` 现有 ChatGPT 登录）。
- 三类审查结论判定准则统一为：**blocking**＝正确性缺陷、安全/硬件风险、与 `AGENTS.md`/`decision_log.md` 既有约束冲突等必须修复项；**non-blocking**＝可选优化与风格建议；**待确认**＝需人工或真机判断的事项。
- **codex 会话连续性（每任务一会话，须串行处理）**：同一个任务的所有 codex 审查（第一循环各轮 + 第二循环各轮）必须在**同一个 codex 会话**内完成——该任务**首次** `/codex:rescue` 调用加 `--fresh` 新开该任务专属会话，**之后每一轮**加 `--resume` 续接，使 codex 始终记得本任务的方案演进与既往意见、不致每轮失忆；**不同任务**各自用 `--fresh` 开独立会话。**关键限制**：本插件 `--resume` 实际映射为 `task --resume-last`，只能续接“**最近一次** codex 调用”，**不能按任务 id 寻址**；因此必须**串行处理任务**——一个任务的双循环全部走完再开下一个，**不要在同一 Claude 会话里交错推进多个任务的 codex 审查**，否则 `--resume` 会接错到别的任务的会话。“不同任务不串台”仅在串行不交错时成立。无状态的 `/codex:review`/`adversarial-review` 不支持续接，仅作可选的结构化 diff 复审，不承担任务主审查线程。
- **承载方式（保证“同一对话”落地）**：同一任务的 codex review（不论第几轮、第一/第二循环）必须由**主 agent 直接在主对话中用 `/codex:rescue` 串行承载**（首轮 `--fresh`、后续每轮 `--resume`），使所有轮次落在**同一个 codex 会话**里。**不得**用 `Workflow`、`parallel` 并行或后台 spawn 跑需要多轮续接的 codex review——那会另开独立 codex 会话、让 `--resume-last` 接不回本任务主线（2026-06-24 reconstruction 验证曾误用 `Workflow` 后台跑 codex，仅因当轮 0 blocking、单轮结束才未暴露断链）。确需并行多视角辅助时只能用 **Claude subagent** 旁路核对，codex review 主线始终是主对话里单独续接的同一条会话。（本条约束的是 `/codex:rescue` 续接的**主审查线程**；第二循环中无状态的 `/codex:review --base` **可选 diff 复审**本就独立会话、不计入主审查线程，不受此约束。）

### 适用范围

- **按改动复杂度/风险分级触发（2026-06-25 调整，取代原“每次实质修改都强制”）**：只有**复杂任务**才走完整 codex 双循环——即需拆解为多个子任务、跨多文件/模块、含非平凡逻辑或行为变化的改动（与“任务执行方式（并行子代理）”对“复杂任务”的界定一致）；**简单或单步、低风险改动**（单处小修、局部参数微调、显式无行为风险的重构等）默认**豁免双循环**，只走“修改后 subagent review”。
- **高风险不因“简单”而豁免**：凡涉及硬件控制（相机/SLM/DAQ）、波形/时序逻辑、安全或破坏性操作的改动，即使改动量小也按复杂任务对待、仍走双循环，第二循环可直接升级 `/codex:adversarial-review`；复杂度拿不准时从严走双循环。
- 原“必须走双循环”的覆盖面（`sim_control/` 等项目源代码、`config/*.json`、`tests/`、波形/时序逻辑、重新生成的 `ui_*`）现作为**判定对象范围**：落在此范围且达到上述复杂/高风险门槛者走双循环，未达门槛者仅走 subagent review。
- 不触发本工作流：Codex 或其他 AI 工具**直接**（非 Claude Code 主导）编写/修改上述文件时——此时不强制 codex 自审，是否再审由用户当时决定。判定基准为“本次改动是否由 Claude Code 主导”：由 Claude Code（含其调度的 Claude subagent）实施改动即触发；用户脱离 Claude Code、直接驱动 Codex 或其他 AI 工具改码即不触发。
- 可豁免（向用户说明后）：纯记忆文件回写（`PROJECT_MEMORY.md`、`decision_log.md`、`memory/`）、注释/文档微调。豁免只跳过双循环，不豁免“修改后 subagent review”。

### 第一循环（方案审查）

1. Claude 先产出**结构化实施方案**，至少含：目标、涉及文件清单、行为变化、测试计划、已知风险/回滚点。
2. 调 `/codex:rescue` 委托 codex 审查方案——**本任务首次调用加 `--fresh` 新开该任务专属 codex 会话，本任务后续每一轮加 `--resume` 续接同一会话**；在转发文本里显式声明 **review-only：只读、按下方三类输出、禁止修改/创建/删除任何文件**（`/codex:rescue` 无 `--write` 等开关，只读靠转发文本约束 + codex 自身 sandbox；可跑 `git diff`、测试等只读命令，其附带写入须落 repo 外或 `.gitignore` 范围、不得改动任何被 git 跟踪文件）。完整方案文本内联在请求中（或写入 repo 外/可丢弃的临时方案文件供其读取）。
3. 要求 codex 按 blocking / non-blocking / 待确认 三类输出。
4. Claude 据反馈改方案后再次提交；**本轮 codex 无 blocking 即视为方案通过**。最终通过的方案文本留存在任务摘要或临时方案文件，供第二循环对照。

### 第二循环（实现验证）

1. Claude 按最终方案改代码。
2. 在第一循环的**同一 codex 会话**中用 `/codex:rescue --resume` 续接，让 codex 对照它已批准的最终方案核对实现——**先为本任务建独立分支或 commit 一个基线**，在转发文本里**同样声明 review-only（只读、禁改文件）**，请 codex 基于本次任务的 `git diff <基线 ref>...` 审查实现是否正确落地，以隔离工作区无关的未提交内容。若另需 native reviewer 的结构化 diff 复审，可用无状态的 `/codex:review --base <基线 ref>`（或 `--scope branch`；独立会话、不计入本任务主审查线程，且不支持 staged/unstaged-only），涉及安全、硬件控制、破坏性操作时升级 `/codex:adversarial-review`。
3. codex 同样按三类输出；有 blocking 则 Claude 改后再审，无 blocking 即通过。
4. 若 diff 审查暴露**方案本身的根本性错误**（架构、需求理解偏差），回到第一循环重审方案，而非在错误方案内反复改。

### 判定、终止与降级

- 通过判定以 codex 的 blocking 项是否清零为准（非由 Claude 主观决定）。codex 反馈疑似误判（与既有约束或硬件事实冲突）时，Claude 可**附理由申辩复审一次**；仍分歧则把分歧点交用户裁决。
- 两个循环各设默认上限 **5 轮**；到上限仍有 blocking 分歧则停止并交用户裁决，不得无限拉锯。
- 用户**显式授权**跳过/简化审查，或 codex 不可用（未装/未登录/报错）且经用户确认继续时，允许降级；降级范围与原因必须记入当次任务摘要。codex 不可用且未获确认时不得静默跳过。
- 通过以本次**实际执行**的审查 blocking 清零为准；当 codex 第二循环与 subagent review **同时执行**（高风险双审，或双循环豁免/降级时仅 subagent review）时，任一存在 blocking 都不算完成、两者结论冲突以更严格者为准并交用户裁决。
- stop-time review gate 默认不启用（全局 hook 会拖慢所有会话）。

### 汇报

- 任务结束时报告：方案审查轮数、实现验证轮数、遗留 non-blocking 意见、需人工/真机确认项、是否发生降级。

## 目录边界

- `sim_control/`：SIM 侧主开发区，包含 GUI、采集控制、波形生成、预览、适配器、配置读写、数据模型和占位 pipeline。
- `control_wangbo/`：队友历史微流控控制代码，默认只读；除非用户明确要求，否则不要修改。
- `app.py`：项目集成 GUI 顶层启动入口，启动 `control_wangbo/main.py` 的主界面。
- `sim_control/sim_acquisition_app.py`：独立 SIM 采集 GUI 启动入口，用于单独调试 SIM 采集链路。
- `config/sim_control_config.json`：默认 SIM 配置文件，承载设备、时序、后端、SDK 路径和 DAQ 线位配置。
- `start.cmd`：Windows 下优先使用的项目集成 GUI 启动脚本。
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
- NI USB-6423 规范接线固定为：`slm_enable=0, slm_trigger=1, slm_finish=2, camera_trigger=8, laser_405=9, laser_488=10, laser_561=11, laser_638=12`。
- 第四路红光统一命名为 `638`，旧 `640` / `647` 配置通过迁移逻辑兼容。
- SIM live 预览采用 `latest-frame-wins`：采集线程持续更新最新帧快照，GUI 端轮询显示最新帧，允许丢弃中间帧以避免旧帧积压。
- `ui_sim_settings_dialog.py` 由 `pyuic5` 从 `.ui` 生成；修改 UI 时编辑 `.ui` 后重新生成，不直接手改生成文件。
- 主 GUI（`control_wangbo` 主界面 `CellSorting`）的界面控件一律先在 `CellSorting_ui.ui` 静态定义、再用 `pyuic5` 重生成 `CellSorting_ui.py`（生成文件不手改）；`control_wangbo/main.py` 只对静态控件做**运行时接线**——信号连接、配置双向同步（含 `blockSignals` 防回环）、带 `itemData` 的下拉项填充、`view()` popup 宽度等无法静态表达的属性、以及 `.ui`/pyuic5 不支持的布局参数（如 `setColumnStretch`：pyuic5 5.15.11 会把 `.ui` 的 `<string>` 形式误生成非法 `setColumnStretch(_translate(...))`，须从 `.ui` 删该 property 改运行时补）。**不再运行期编程创建主 GUI 界面控件**（取代以往多处“纯运行期、不改 `.ui`”做法，2026-06-25 决策）。统一流程：改 `.ui` → `pyuic5` 绝对路径重生成 + before/after 副本 diff 核对零漂移 → `main.py` 接线 → 更新测试。
- `SimSettingsDialog` 不拥有 SLM 生命周期；集成主界面与 SIM controller 必须共享同一个 `slm_adapter`，避免 R11 WinUSB 设备被重复打开。
- 正式 SIM 采集要求 SLM 已连接并选择匹配 Running Order；未连接时应阻止采集，不回退到空 `pattern_files`。
- 后续 SIM9 采集以 NI USB-6423 波形输出为唯一 DAQ 控制路径；不要恢复 `daq_backend`、`pxie7857r`、`rio_lines`、`nifpga`、`NIRioDaqAdapter`、NI-RIO bitfile preflight 或 PXIe-7857R LabVIEW rebuild 入口。

## 硬件接入

- 相机：Hamamatsu ORCA-Fusion BT，通过 `DCAM-API` / `DCAM-SDK4` 控制。`FusionBtCameraAdapter` 已包含仿真模式和真实 SDK 绑定扩展点；真实 DLL/模块名、路径和函数签名通过 `sim_control/adapters.py` 集成。
- SLM：Kopin / Forth Dimension Displays `QXGA-R11-STR`，通过 `R11CommLib` over WinUSB 厂商栈控制。`KopinSlmAdapter` 已包含仿真模式、Running Order 枚举/选择和真实 SDK 扩展点。
- DAQ：当前 SIM9 采集只使用 NI USB-6423 输出同步 TTL，驱动 `slm_enable_line`、`slm_trigger_line`、`slm_finish_line`、`camera_trigger_line` 和各激光触发线。
- `SDK/` 本地材料包含 Hamamatsu `DCAM-SDK4` 与 FDD `R11` bundle，包括 `R11CommLib`、WinUSB 驱动、MetroCon、sequence catalogue 和协议文档；仓库只跟踪 `SDK/README.md` 的目录约定。
- SLM 正式采集路径使用预烧录 Running Order：主界面连接 SLM 后，按当前波长与相机曝光自动选择 `3.5/2d`、非 `_ang0` RO；638 nm 采集优先匹配 `638` RO，若现场 repertoire 仍只有旧命名则允许 fallback 到 `647` RO；RO 模式以 `PatternPreparationResult(handles=[-1], metadata["mode"]="running_order")` 表示。
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

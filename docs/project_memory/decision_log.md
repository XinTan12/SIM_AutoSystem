# 项目决策日志

## 使用说明
- 本文件只记录影响多个后续会话的重要决策、长期约束或方向性选择。
- 每条记录至少包含：日期、决策、原因、影响。
- 普通操作、临时讨论和纯执行细节不写入本文件。

## 2026-05-13

### 决策：每次项目修改后必须使用独立 subagent review
- 原因：
  项目涉及 GUI、硬件适配、采集时序、配置迁移和长期记忆文件，单一 agent 在实现后容易遗漏验收标准、未满足项或需要人工/真机确认的边界。把独立 subagent review 固定为修改后的必经步骤，可以让后续每轮改动都留下更清晰的证据链和风险清单。
- 影响：
  后续任何代码、配置、文档或工作流修改完成后，主 agent 都应调用独立 subagent 审查本次改动。审查结果必须明确列出：已满足的项目标准或用户要求及证据、未满足的标准或要求及原因、需要用户人工确认、真机确认或额外业务判断的事项。主 agent 在最终回复前应处理可立即修复的问题，并把无法自动处理的事项反馈给用户。若当前工具环境无法启动独立 subagent，主 agent 必须先说明原因并请求用户确认，不得静默用自审替代。

## 2026-05-11

### 决策：SIM 正式采集的 inter frame gap 默认 50ms，仅允许更短的相机计算值覆盖
- 原因：
  Hamamatsu 相机按 ROI 和传输接口返回的推荐读出间隔可用于缩短采集周期，但大于等于 50ms 的推荐值不应放慢默认 SIM9 时序；同时 DAQ 设置弹窗不再向用户暴露该参数，避免人工设置与真实相机运行时能力冲突。
- 影响：
  `TimingConfig.inter_frame_gap_us` 默认值为 `50_000 us`；正式采集和 worker 相机配置路径统一通过有效 gap 规则处理：`recommended_inter_frame_gap_us < 50_000` 时使用推荐值，否则使用 `50_000`。主界面 SIM 参数摘要只读显示最终使用的 Timing 参数；后续修改采集时序时应保留该 50ms 默认策略，除非重新做硬件时序决策。

### 决策：使用 `dev` 作为大改动开发分支，并用 detached stable worktree 运行远程稳定版
- 原因：
  当前远程 `origin/main` 是确认可运行的稳定版本，而本地存在较大的未提交开发改动。将开发线固定到 `dev`，并额外保留 detached `origin/main` 的 stable worktree，可以避免在同一目录里反复切换稳定版和开发版，降低误操作风险。
- 影响：
  `main` / `origin/main` 只作为稳定主线；`dev` / `origin/dev` 用于当前和后续大改动开发；`E:\Intelligent_SR\SIM_AutoSystem-stable` 只用于运行远程稳定版、真机对照和回退参考，不在其中开发或提交。大改动合入流程为 `dev` 本地验证 -> push `origin/dev` -> GitHub PR -> merge `origin/main` -> stable worktree 更新到最新 `origin/main`。

## 2026-05-07

### 决策：AI 工具项目说明统一以 `AGENTS.md` 为唯一权威入口
- 原因：
  `CLAUDE.md` 和 `AGENTS.md` 同时维护项目说明会导致 Claude Code、Codex、Cursor、Cline 等工具读到不同的路径、硬件边界和工作规则；统一到单一主指令文件可以避免双源漂移。
- 影响：
  `AGENTS.md` 负责保存稳定的项目说明、长期规则、架构边界、硬件约束和跨对话记忆规则。`CLAUDE.md` 仅保留 Claude Code 兼容薄壳，并通过 `@AGENTS.md` 引用主文件。后续任何 AI 工具需要修改项目说明时只修改 `AGENTS.md`；当前状态和最近更新仍写入 `PROJECT_MEMORY.md`，关键长期决策仍写入本文件。

### 决策：SIM9 采集回到 NI USB-6423 单 DAQ 路线，不再使用 PXIe-7857R / NI-RIO
- 原因：
  项目计划已变更，当前目标只需要通过 USB-6423 采集卡完成 SIM 9 帧图像采集流程；继续维护 PXIe-7857R Python Host、NI-RIO bitfile 诊断和 LabVIEW rebuild 链路会增加无必要的配置面、依赖和硬件联调成本。
- 影响：
  当前代码回退到 `7241893` 的 USB-6423-only 基线，并移除未跟踪的 PXIe-7857R / NI-RIO / LabVIEW rebuild 相关模块、文档和测试。后续 SIM 采集控制只围绕 USB-6423 波形输出、SLM Running Order、相机触发和激光 TTL 同步推进；不要恢复 `daq_backend`、`pxie7857r`、`rio_lines`、`nifpga`、`NIRioDaqAdapter` 或 NI-RIO bitfile preflight 入口。

## 2026-04-26

### 决策：正式 SIM 采集使用预烧录 SLM Running Order，并由主界面共享单个 R11 adapter
- 原因：
  R11 WinUSB 设备不应被 `CellSorting` 主界面和 `SimSettingsDialog` 重复打开；预烧录 `.repz11` 已包含所需 `405/488/561/647 × 1/10/50ms × normal/_ang0` RO，正式采集只需按波长与曝光桶选择非 `_ang0`、`3.5/2d` RO。
- 影响：
  `SimSettingsDialog` 不再管理 SLM 连接或 pattern 文件；`control_wangbo` 主界面负责连接 SLM 并共享 `SimAcquisitionController.slm_adapter`。正式 SIM 采集在 SLM 未连接时阻止并提示，连接后选择 RO 并在采集路径中 activate。

### 决策：RO 循环行为依赖 `.repz11` 内部 FINISH-controlled loop，而不是 SDK 运行时循环模式
- 原因：
  本地 repertoire 中 RO 使用 `[HWA h]`、`t.wait(20)` 和 `{f ...}` 结构，表示硬件触发后循环执行帧组直到收到 SPI_2 / FINISH；启用 SPI 线的 RO Selection 替代功能会占用 TRIGGER/FINISH，破坏当前 DAQ 线位语义。
- 影响：
  保持 `slm_trigger_line` 对应 SPI_1 / TRIGGER，`slm_finish_line` 对应 SPI_2 / FINISH。`1ms RO + 3ms camera exposure` 的预期行为是曝光窗口内重复 RO 直到 FINISH，但仍需真机示波器或图像结果验证。

## 2026-04-23

### 决策：跨对话记忆采用仓库内文档，而不是依赖聊天历史
- 原因：
  大模型本身不提供稳定的项目级永久记忆；将记忆落到仓库文件中，才能被版本控制、审阅和持续更新。
- 影响：
  后续新对话应优先读取 `AGENTS.md`、`PROJECT_MEMORY.md` 和本文件；当项目状态或重要决策变化时，需要在会话结束前回写这些文档。

### 决策：跨对话记忆采用“结构化方案”，即 `PROJECT_MEMORY.md` + `decision_log.md`
- 原因：
  只用单一记忆文件容易把当前状态、长期约束和历史决策混在一起；过重的自动化记忆方案维护成本高，不适合当前阶段。
- 影响：
  `PROJECT_MEMORY.md` 用于保存“当前真相”，本文件用于保存“为什么这么做”，二者配合支撑跨对话延续。

### 决策：`control_wangbo/` 默认保持只读，新的 SIM 侧开发集中在 `sim_control/` 或仓库根目录新增模块
- 原因：
  `control_wangbo/` 属于队友遗留微流控控制代码，默认保持稳定更利于后续联调与责任边界控制。
- 影响：
  除非用户明确要求，否则不修改 `control_wangbo/`；涉及 SIM 集成的新工作优先在 `sim_control/` 或新顶层模块中完成。

### 决策：SIM 侧统一使用仓库根目录 `.venv` 作为当前主环境
- 原因：
  统一环境有助于减少依赖漂移、路径分叉和运行时不一致问题。
- 影响：
  `PyQt5`、`numpy`、`nidaqmx` 及后续 SDK 绑定等依赖均应集中安装在根目录 `.venv`，不再依赖 `control_wangbo/.venv`。

### 决策：近期联调目标固定为“微流控捕获 -> SIM 9 帧采集 -> `(9, H, W)` `numpy.uint16` 栈 -> 重建/分析 -> 决策回传”
- 原因：
  先明确一条最小可用的端到端链路，能够避免多模块并行发散，便于后续按接口推进。
- 影响：
  后续设计、实现和验证工作应优先围绕这条链路展开，尤其是 9 帧采集输出和决策回传接口。

### 决策：新对话的默认启动范围固定为记忆文件三件套，再按任务补读相关实现
- 原因：
  只依赖即时聊天历史不稳定，而一上来就扫描整个仓库成本高、噪声大。先读取 `AGENTS.md`、`PROJECT_MEMORY.md` 和决策日志，可以稳定恢复项目主上下文；之后再按任务读取具体模块，实现成本与效果更平衡。
- 影响：
  后续每次新对话都应先完成这三个文件的读取；如果用户请求涉及具体模块、配置或故障，再补读相关代码、配置、测试和必要设计文档，而不是默认全仓库扫描。

### 决策：只要本次会话修改了项目，结束前必须同步更新 `PROJECT_MEMORY.md`
- 原因：
  如果代码变了但项目记忆不更新，跨对话记忆很快会失真；把回写义务固定下来，才能让项目记忆持续可用。
- 影响：
  后续每次发生项目修改时，至少更新 `PROJECT_MEMORY.md` 的“最近更新”部分；如果修改影响项目状态、约束、接口、依赖、硬件接入或待办，也要同步更新对应章节。长期决策变化时再额外更新本决策日志。

### 决策：NI USB-6423 在项目内固定采用 `0/1/2/5/8/6/7/9` 线位，并将第四路红光统一命名为 `647`
- 原因：
  当前实际接线已明确为 `slm_enable=0`、`slm_trigger=1`、`slm_finish=2`、`camera_trigger=5`、`405=8`、`488=6`、`561=7`、`647=9`；继续保留原先连续 `0..7` 的默认映射和 `640` 命名会造成 GUI 默认值、配置文件和真实硬件之间的偏差。
- 影响：
  `sim_control` 默认配置、GUI 默认回退、波形相关测试和摘要输出均以该映射为准；项目内公共配置 schema 使用 `laser_647_line` 与 `selected_laser_nm=647`，但加载旧 `laser_640_line` / `640` 配置时需要自动迁移以保持兼容。

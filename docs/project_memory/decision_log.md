# 项目决策日志

## 使用说明
- 本文件只记录影响多个后续会话的重要决策、长期约束或方向性选择。
- 每条记录至少包含：日期、决策、原因、影响。
- 普通操作、临时讨论和纯执行细节不写入本文件。

## 2026-06-10

### 决策：slm_enable guard 以 R11 tHWAT 规格为准固定为 ≥1 ms，首帧黑帧归因于 [HWA h] RO 的 EXT_RUN 硬件激活窗口
- 原因：
  DAQ 页 SIM 采集测试出现第一帧黑帧、九图案错位一帧（3+3+2）。SDK 文档闭环显示：repertoire 内全部 RO 声明 `[HWA h]`，只在 EXT_RUN（`slm_enable_line`/SPI_0）拉高后激活（PD0011CA p.16）；软件 `R11_RpcRoActivate` 后 EXT_RUN 未拉高时系统仍在 Maintenance 黑屏（AN0027AD §3.26，状态 0x54 MHW）；EXT_RUN 拉高到 Active Mode 需要 tHWAT=5~500 µs（PD0011CA Table 7-1）。旧 `slm_enable_guard_us=50` 使首个 slm_trigger 落在激活窗口内被丢弃。另据 sequence timing report，TRIGGER 后 tTSS≈3.5 µs 启动 sequence、1-bit Lit Pair 的 `illumStart≈270.19 µs` 图案才点亮，FINISH-loop 架构下无需额外 trigger lead。
- 影响：
  `TimingConfig.slm_enable_guard_us` 默认与推荐值定为 1000 µs（tHWAT 上限 2 倍）；配置 schema 升到 v9，迁移把 <1000 的旧值抬到 1000（用户可事后手动调低）；`waveform.build()/build_z_scan()` 对 guard≤500 µs 发告警而不硬拦截。`R11_RpcRoGetActivationState` 纳入 `_R11CommLib` 绑定与 `KopinSlmAdapter`，采集链路 activate 后上报 `slm_activation_state` 事件，DAQ 页新增「SLM激活时序」诊断测试；R11CommLib 无逐帧时间戳/计数查询，逐帧真实时序验证依赖 sequence timing report、READY/SPO_0 + 示波器与相机 DCAM 时间戳。后续调整 SIM/z-scan 波形时序时不得把 guard 降回 ≤500 µs，除非重新做硬件时序决策。

## 2026-05-25

### 决策：Z-Scan Display ETA 拆分为仅位移与位移+采图两种测试口径
- 原因：
  单一 `estimated_scan_time_ms` 容易把“仅位移台移动”测试和“位移台 + 每层采图”测试混为一谈；真实 stage-only 测试总耗时不包含每层 DAQ/曝光/相机采图，因此用采图口径估算会明显高估。用户要求 Display 同时展示两个明确口径，并让各自与对应测试弹窗的真实总耗时校准。
- 影响：
  Z-Scan 历史记录在兼容旧 step 记录的基础上新增 `record_type="run"` 的分模块 run-summary，记录固定开销、起始定位、按 scan gap 的层间移动、回第一层、采图非移动耗时、最佳焦面移动和 TIFF 写盘耗时。Display 显示 `预估总用时(仅位移)` 与 `预估总用时(位移+采图)`；主界面摘要字段改为 `estimated_move_only_time_ms` 和 `estimated_move_capture_time_ms`。仅位移 ETA 对齐“位移台移动”测试弹窗总耗时；位移+采图 ETA 对齐“位移台 + 每层采图”测试弹窗总耗时。

### 决策：Z-Scan ETA 使用持久化测试历史，正式取消不再回起始层
- 原因：
  固定 25 ms/层的 Z-Scan ETA 只随层数变化，无法反映不同 scan gap 对 Ti2 ZDrive 实际移动耗时的影响；用户需要在测试某组层扫间隔和步数后，把每步耗时沉淀为后续估算依据。同时，正式 SIM 采集取消时自动回起点会引入额外 Z 轴移动和不确定状态，用户要求删除该配置项，并在不开启 Z-Scan 时直接使用当前 Z 轴位置采集 SIM9。
- 影响：
  Z-Scan 测试耗时持久化到 `data/z_scan_timing_history.jsonl`，该运行数据不入 Git；Display ETA 优先使用相同 `scan_gap_nm` 且曝光 preset 匹配的完整 cycle 历史，其次使用相同 gap 的移动中位数，再用已有 gap 做距离模型，无历史时回退默认模型。配置 schema 升到 v8，并删除 `ZScanConfig.return_to_start_on_cancel` 与设置 GUI 中的 `Return to start on cancel`。正式 `run_z_scan()` 遇到取消只传播取消并依赖清理路径保持 DAQ 全低，不再移动回起点；测试模式完成后仍可保留回第一层的安全行为。`z_scan.enabled=False` 时正式 SIM9 不调用 Z-stage 或 `run_z_scan()`，直接在当前 Z 轴位置执行 9 帧采集。

## 2026-05-22

### 决策：SIM9 正式采集 raw stack 与 GUI 状态信号分离
- 原因：
  正式 SIM9 采集得到的 `(9, H, W)` `numpy.uint16` 栈可能接近百 MB，GUI 状态更新只需要 task id、shape、dtype 和 metadata。继续让 GUI 状态槽接收 `AcquisitionBatch` 会让界面层接触并可能暂存大数组，边界不清，也增加后续误加拷贝、预览提取或同步写盘的风险。
- 影响：
  `signal_acquisition_ready(AcquisitionBatch)` 保留为 raw 数据通道，只连接重建、保存或分析 worker；新增 `signal_acquisition_summary_ready(dict)` 作为 GUI 状态通道，payload 固定为 `task_id`、`stack_shape`、`stack_dtype`、`metadata`。controller 的 stop_event 清理也挂到 summary 信号，避免 controller 清理回调接收 raw stack。后续 GUI 状态功能不得通过 raw batch 中转，也不得为了状态显示复制或序列化 stack。

### 决策：SIM 重建配置改为默认启用，并由设置弹窗 Recon 页管理用户级参数与输出 TIFF
- 原因：
  GPU Wiener 重建已经接入 pipeline，后续真实采集流程需要操作者在 GUI 中配置当前波长 OTF、background、Wiener、NA、pixel size 和输出路径，避免继续手改 JSON 或依赖硬编码路径。`device`、`dtype`、`theta_ratio`、`recon_group_batch` 属于算法/运行时调试参数，日常采集暴露这些项容易误改，因此保持内部默认。
- 影响：
  `config/sim_control_config.json` schema 升到 v6，`ReconstructionConfig.enabled` 默认改为 `true`，新增 `output_path`。`SimSettingsDialog` 新增 `Recon` 页，波长与 Laser 页共用 `selected_laser_nm`，保存时要求当前波长 OTF 已配置。`ReconstructionWorker` 在 GPU Wiener 成功后若 `output_path` 非空，会保存 `float32` reconstruction TIFF；写盘失败视为重建失败，不回退占位均值。后续无 OTF 的仿真或 payload 测试需要显式关闭/补齐重建配置，真实使用前需要为各波长填写标定文件路径。

### 决策：SIM 重建输出统一使用目录语义并默认保存到 `data/reconstruction`
- 原因：
  真实采集会重复产生重建 TIFF，固定单文件路径容易覆盖且与用户“保存路径为文件夹”的操作习惯不一致。将 `ReconstructionConfig.output_path` 统一解释为输出目录，可以让 Recon 页只选择文件夹，并由 worker 自动生成含日期时间的文件名，减少误操作。
- 影响：
  `config/sim_control_config.json` schema 升到 v7，`ReconstructionConfig.output_path` 字段名保留但语义改为输出目录，默认 `data/reconstruction`，相对目录统一按项目根目录解析而不是按进程当前工作目录解析。v6->v7 迁移会把空值补成默认目录、把旧 `.tif/.tiff` 文件路径迁移为父目录；新配置校验不接受 `.tif/.tiff` 作为输出路径。`ReconstructionWorker` 写盘文件名固定为 `sim_reconstruction_YYYYMMDD_HHMMSS.tif`。运行时重建 TIFF、测试采集数据等大文件放在 `data/` 下并默认不入版本管理。

## 2026-05-21

### 决策：SIM9 GPU Wiener 重建通过安全内存接口接入 pipeline，release/sort 暂不自动回传
- 原因：
  师兄完成的 GPU Wiener 重建算法原始入口是带硬编码路径和磁盘 I/O 的脚本，不适合由 GUI 或采集 pipeline 直接 import。当前主流程需要把 SIM 9 帧采集得到的 `(9, H, W)` `numpy.uint16` 栈从内存交给重建算法，同时保持 GUI 在未安装 `torch/scipy` 或未配置 OTF 时仍可启动，并避免真实采集路径回退到不可追踪的占位结果。
- 影响：
  对外稳定入口为 `reconstruction.sim_wiener.reconstruct_sim9_stack()`；`sim_wiener_gpu_emdapp_batchInGroup_batchBetGroup.py`、`emd.py`、`emd_fast_torch.py` 作为算法后端保持不改。`config/sim_control_config.json` schema 升到 v5，新增 `reconstruction` 段，默认关闭；启用时必须配置当前波长 OTF。`ReconstructionWorker` 在启用时调用 GPU Wiener，失败即标记重建失败且不回退占位均值；未启用时保留原占位均值以维持仿真和基础流程。独立 SIM GUI 与集成主界面保存 `sim_last_reconstruction_result`；当前不触发 `signal_isTarget(7, ...)`，后续 release/sort 需等目标细胞判据或分类模型明确后再接入。

### 决策：Z-Scan 的 `num_steps` 表示 ZDrive 移动次数，界面层扫间隔以 nm 输入
- 原因：
  用户在真实操作语义中把“层扫步数”理解为位移台移动次数，而不是最终图像层数。因此 `num_steps=10`、`step_um=0.3` 应表示从起始层开始移动 10 次，总位移 `3.0 μm`，并采集起始层加移动后层共 11 张图。GUI 侧以 nm 设置 scan gap 更符合小步长调焦习惯，且 10 nm 单步调整比 0.001 μm/0.1 μm 混合显示更直观。
- 影响：
  内部配置字段继续保留 `step_um` 和 `num_steps` 以兼容 v4 配置，不新增 schema 版本；UI 读写时执行 `nm ↔ μm` 转换。`scan_positions()` 返回 `num_steps + 1` 个位置，Z-Scan 预览、Test B stack/focus curve 校验、主界面摘要和预计用时均按图像层数 `num_steps + 1` 计算。后续代码中看到 `ZScanConfig.num_steps` 时应按“移动次数”理解，而不是图像层数。

## 2026-05-20

### 决策：SIM9 正式采集前增加基于 Ti2 ZDrive 的单帧三相位 Z-Scan 自动对焦
- 原因：
  微流控捕获细胞后 Z 位置存在抖动，直接执行 SIM9 会出现失焦采集。SLM 路径无法提供传统宽场均匀照明，因此 Z-Scan 采用 488 nm 单方向三相位条纹在同一次相机曝光内依次播放，让三相位平均效应在相机积分期间形成近似均匀照明，同时每个 Z 位置只产生 1 张图以减少时延。
- 影响：
  `patterns/2d_3.5.repz11` 是正式 SIM9 与 Z-Scan 共享的 repertoire；新增 `488_3.5_2d_zscan3p_{5,8,14,20}ms` RO 后运行时不再为 Z-Scan 重新烧录单独 `.repz11`。Z-Scan RO 使用 `[HWA h]` 与 `t.wait(20)`，不使用 `{f ...}` FINISH 循环；对应 DAQ 波形不拉 `slm_finish_line`，只要求 `slm_enable_line` 先行 guard 后同步输出 `slm_trigger_line`、`camera_trigger_line` 和 `laser_488_line`，其中 camera/488 高电平持续时间使用 preset 对应实际执行时间 `4884/7884/13884/19884 us`。Z-Scan sequence alias 固定为 5ms=`A±/48030 500us`、8ms=`F±/48037 1ms`、14ms=`G±/48038 2ms`、20ms=`D±/48039 3ms`；不要把 3ms sequence 放到 `H±`，否则 R11 编译/烧录会报 `sequence number (16) out of range`。正式 SIM9 的 FINISH-controlled RO 与 9 帧波形逻辑保持不变。

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

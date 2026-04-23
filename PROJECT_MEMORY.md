# SIM_AutoSystem 项目记忆

## 使用说明
- 新对话开始时，必须优先读取本文件、`AGENTS.md` 和 `docs/project_memory/decision_log.md`，作为项目上下文的固定启动步骤。
- 在完成上述读取后，如果本次任务涉及具体模块、配置或故障，再补充读取与任务直接相关的代码、配置、测试和必要的相关设计文档。
- 本文件用于保存当前有效的项目事实、约束、待办和最近状态，服务于跨对话延续。
- 如果本文件与代码、配置或更具体的文档冲突，以更新且更具体的事实为准，并在本次会话结束前修正本文件。
- 只要本次会话对项目产生了修改，结束前必须同步更新本文件；即使修改范围较小，至少也要更新“最近更新”部分。
- 本文件记录的是提炼后的项目记忆，不保存完整聊天记录。

## 当前状态说明
- 本文件的初始内容基于仓库根目录 `AGENTS.md`、`README.md` 和当前目录结构整理。
- 当前内容可作为后续对话的稳定起点，但仍应以实际代码、配置和硬件接入进展为最终依据。

## 项目目标
- 构建面向微流控捕获与 SIM 成像联动的自动化系统。
- 近期集成目标是打通以下链路：
  `control_wangbo` 微流控捕获 -> `sim_control` 执行 9 帧 SIM 采集 -> 输出 `(9, H, W)` 形状的 `numpy.uint16` 图像栈 -> 重建与特征分析 -> 将决策结果回传给微流控流程执行 `release/sort`

## 目录职责
- `control_wangbo/`
  历史微流控控制代码，默认只读，仅在用户明确要求时修改。
- `sim_control/`
  当前 SIM 侧主开发区域，包含 GUI、采集控制、波形生成、适配器以及占位的重建/特征流程。
- `sim_control_app.py`
  SIM GUI 启动入口。
- `config/sim_control_config.json`
  默认配置文件，承载设备、时序、后端和路径等配置。
- `start_sim_control.cmd`
  Windows 下优先使用的启动脚本。
- `SDK/`
  本地放置厂商 SDK、驱动和资料的目录。

## 当前硬件与集成状态
- 相机方案：Hamamatsu ORCA-Fusion BT，通过 `DCAM-API` / `DCAM-SDK4` 控制。
- SLM 方案：Kopin / Forth Dimension Displays `QXGA-R11-STR`，通过 `R11CommLib` 控制。
- DAQ 方案：NI USB-6423，负责输出 `slm_enable_line`、`slm_trigger_line`、`slm_finish_line`、`camera_trigger_line` 以及激光触发线的同步 TTL。
- 当前 `FusionBtCameraAdapter` 与 `KopinSlmAdapter` 已具备仿真模式和对真实 SDK 绑定的扩展点。
- 真实 DLL 名称、加载路径与函数签名的集成入口集中在 `sim_control/adapters.py`。
- `SDK/` 目录当前已包含 Hamamatsu `DCAM-SDK4` 材料，以及 FDD `R11` 相关工具、驱动和文档。

## 环境与运行约定
- 当前工作目录根为 `E:\Intelligent_SR\SIM_AutoSystem`。
- SIM 侧统一使用根目录 `.venv` 作为项目环境。
- 不依赖 `control_wangbo/.venv`，该旧环境不属于当前主流程。
- `start_sim_control.cmd` 直接通过 `.venv\Scripts\python.exe` 启动 `sim_control_app.py`。
- 默认配置路径为 `config/sim_control_config.json`。

## 当前关键约束
- 默认不修改 `control_wangbo/`。
- 新的 SIM 侧开发优先放在 `sim_control/` 或仓库根目录新增模块中。
- 优先采用配置驱动，不把设备路径、TTL 线位和 SDK 定位写死在代码中。
- 实时采集路径避免同步磁盘 I/O。
- 硬件适配逻辑尽量集中在 `sim_control/adapters.py`。

## 当前工作重点
- 保持 SIM GUI、采集控制与适配器在无真实硬件时仍可通过仿真模式运行。
- 将真实相机与 SLM SDK 逐步接入现有适配器层。
- 打通 9 帧采集到 `(9, H, W)` `numpy.uint16` 输出栈的稳定接口。
- 明确重建模块与特征分析模块的输入输出接口，并把最终决策结果接回微流控流程。

## 已知风险与待确认事项
- 厂商 SDK 的真实 DLL 名称、导出函数和调用签名仍需结合本机安装内容做最终核对。
- NI 触发时序、相机触发、SLM 完成信号和激光线之间的同步细节仍需真实硬件联调验证。
- 重建与特征分析部分当前仍以占位流程为主，尚未形成稳定的生产接口。
- 当前仓库中 README 存在编码展示异常的迹象；如后续需要依赖 README 作为稳定文档源，应先统一编码。

## 当前待办
- 核实 `config/sim_control_config.json` 中与 SDK 路径、设备配置和时序相关的字段是否足以支撑真实硬件接入。
- 在 `sim_control/adapters.py` 中补齐 Hamamatsu `DCAM-SDK4` 的真实绑定实现。
- 在 `sim_control/adapters.py` 中补齐 `R11CommLib` 的真实绑定实现。
- 验证 NI USB-6423 的 TTL 线位与 9 帧采集时序是否满足联动要求。
- 明确重建模块接收 `(9, H, W)` `numpy.uint16` 栈后的接口形式、返回结果和回调链路。
- 在后续每次状态变化后持续维护本文件和决策日志，保证跨对话记忆有效。

## 最近更新
- 2026-04-23：强化跨对话记忆工作流，新对话固定先读取 `AGENTS.md`、`PROJECT_MEMORY.md` 和决策日志；若任务涉及具体模块，再补读相关代码与配置。
- 2026-04-23：约定只要本次会话修改了项目，结束前至少更新本文件“最近更新”部分，并按需要同步修正受影响章节。
- 2026-04-23：建立仓库内跨对话记忆机制，新增 `PROJECT_MEMORY.md` 与 `docs/project_memory/decision_log.md`。
- 2026-04-23：在 `AGENTS.md` 中加入“新对话先读取项目记忆文件、会话结束按需回写”的工作流要求。

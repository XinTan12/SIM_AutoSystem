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
- 当前项目内的 NI USB-6423 规范接线映射固定为：`slm_enable=port0/line0`、`slm_trigger=port0/line1`、`slm_finish=port0/line2`、`camera_trigger=port0/line5`、`laser_405=port0/line8`、`laser_488=port0/line6`、`laser_561=port0/line7`、`laser_647=port0/line9`。
- Hamamatsu runtime timing 中的 `Actual Inter Frame Gap` 当前按保守读出等待计算：`ceil(TIMING_READOUTTIME * 1_000_000) + ceil(TIMING_MINTRIGGERBLANKING * 1_000_000) + 1000 us`；若读不到 `TIMING_READOUTTIME`，采集逻辑回退到配置中的 `inter_frame_gap_us`。
- SIM 侧第四路红光命名已统一为 `647`；默认配置文件使用 `laser_647_line`，加载旧配置时会自动把 `laser_640_line` / `selected_laser_nm=640` 迁移到新命名。
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
- 将 SIM live 预览维持为低延迟 `latest-frame-wins` 模式：采集线程持续取帧，GUI 端只轮询并渲染当前最新快照，允许丢弃中间帧以避免载物台移动或主线程忙碌时积压旧帧。

## 已知风险与待确认事项
- 厂商 SDK 的真实 DLL 名称、导出函数和调用签名仍需结合本机安装内容做最终核对。
- NI 触发时序、相机触发、SLM 完成信号和激光线之间的同步细节仍需真实硬件联调验证。
- 重建与特征分析部分当前仍以占位流程为主，尚未形成稳定的生产接口。
- 当前仓库中 README 存在编码展示异常的迹象；如后续需要依赖 README 作为稳定文档源，应先统一编码。

## 当前待办
- 核实 `config/sim_control_config.json` 中与 SDK 路径、设备配置和时序相关的字段是否足以支撑真实硬件接入。
- 在 `sim_control/adapters.py` 中补齐 Hamamatsu `DCAM-SDK4` 的真实绑定实现。
- 在 `sim_control/adapters.py` 中补齐 `R11CommLib` 的真实绑定实现。
- 在真机上验证 NI USB-6423 规范线位 `0/1/2/5/8/6/7/9` 与 9 帧采集时序是否满足联动要求。
- 明确重建模块接收 `(9, H, W)` `numpy.uint16` 栈后的接口形式、返回结果和回调链路。
- 在后续每次状态变化后持续维护本文件和决策日志，保证跨对话记忆有效。

## 最近更新
- 2026-04-24：`sim_control/gui.py` 局部重构（不涉及 `control_wangbo/`）：保留现有 `_catch_to_error` 装饰器，提取 `read_daq_config_from_line_combos()`、`populate_daq_line_combos()` 和 `browse_pattern_file()` 以消除 SimSettingsDialog 与 SimControlWindow 的重复逻辑；Window 侧 DAQ line 刷新现在也会在设备不匹配时回退到当前设备默认线位。`.venv\Scripts\python.exe -m pytest tests/ -q` 通过，80 条测试全部通过。
- 2026-04-24：架构改进（不涉及 `control_wangbo/`）：新增 `sim_control/protocols.py`（CameraAdapter / SlmAdapter / DaqAdapter 协议接口）、`sim_control/acquisition_core.py`（纯 Python 采集核心，脱离 Qt 依赖）；提取 GUI 配置同步共享函数消除 `gui.py` 中 SimSettingsDialog 与 SimControlWindow 的重复代码；`pipeline.py` 各阶段增加输入校验与接口文档；`preview.py` 移除冗余 `_running` 标志，统一使用线程安全的 `threading.Event` 控制停止；`config_store.py` 引入 `config_version` + 链式迁移机制，旧 640→647 迁移纳入 v0→v1 步骤；`models.py` 的 `AppConfig` 新增 `config_version` 字段。全部 80 条测试通过，无回归。
- 2026-04-23：为 `control_wangbo/main.py` 启动的集成主窗口增加 `QScrollArea` 包装层；生成式 `CellSorting_ui` 内容仍保持 1800x1000 固定设计尺寸，窗口缩小时可通过底部和右侧滚动条查看全部界面内容。
- 2026-04-23：修正 SIM 采集时序显示与 runtime gap 计算：`Actual Inter Frame Gap` 改为基于 Hamamatsu `TIMING_READOUTTIME`、`TIMING_MINTRIGGERBLANKING` 和 1000 us 安全余量的保守读出等待；DAQ 摘要显示名改为 `cam_trigger_line`；SIM 设置窗口 DAQ 页改为更紧凑的双列布局并压缩过大留白，避免通道与 Timing 控件在最小窗口下重叠。
- 2026-04-23：集成主窗口中用户点击 SIM `Abort` 后会立即把预览框清为黑色并清除最后一帧缓存；`control_wangbo/main.py` 启动加载 `lastConfiguration.json` 的路径已统一为 `control_wangbo/lastConfiguration.json`，同时保留原有 `保存了pppppppppp` 调试打印。
- 2026-04-23：将 `sim_control/preview.py` 的 live 预览从逐帧 Qt queued signal 推送改为线程安全的最新帧快照缓存，并在 `control_wangbo/main.py` 中接入基于 `QTimer` 的主线程轮询显示；live 预览现以低延迟优先，只显示最新帧，减少载物台移动时旧帧积压导致的长时间卡顿。
- 2026-04-23：统一 `SimSettingsDialog` 中 Pattern、DAQ 和底部操作区的主要按钮外观，将 `Refresh`、`load`、`Pulse Test`、`Save and Close`、`Cancel` 全部调整为与 SLM 连接切换按钮一致的圆角、固定高度和带左右留白的紧凑按钮样式。
- 2026-04-23：将 Pattern 页 `btn_toggle_slm_connection` 调整为固定宽度按钮，参考 SIM 相机连接按钮的交互，避免 `Connect` / `Disconnect` 文案切换时按钮尺寸变化导致布局抖动。
- 2026-04-23：调整集成主窗口与 `SimSettingsDialog` 的交互，打开 SIM 参数设置前会先挂起 live，关闭后按原请求恢复；同时将 Pattern 页的 SLM 连接区改为设备下拉框右侧 `Refresh` 按钮加单一 `Connect` / `Disconnect` 切换按钮，并把首轮 DAQ/SLM 枚举延后到对话框显示后执行以降低卡顿。
- 2026-04-23：将 SIM 侧第四路红光命名从 `640` 统一为 `647`，并为旧 `laser_640_line` / `selected_laser_nm=640` 配置加入自动迁移兼容。
- 2026-04-23：根据最新实物接线，将项目内 NI USB-6423 默认/规范接线映射更新为 `0/1/2/5/8/6/7/9`，同步修改默认配置与默认映射相关测试。
- 2026-04-23：强化跨对话记忆工作流，新对话固定先读取 `AGENTS.md`、`PROJECT_MEMORY.md` 和决策日志；若任务涉及具体模块，再补读相关代码与配置。
- 2026-04-23：约定只要本次会话修改了项目，结束前至少更新本文件“最近更新”部分，并按需要同步修正受影响章节。
- 2026-04-23：建立仓库内跨对话记忆机制，新增 `PROJECT_MEMORY.md` 与 `docs/project_memory/decision_log.md`。
- 2026-04-23：在 `AGENTS.md` 中加入“新对话先读取项目记忆文件、会话结束按需回写”的工作流要求。

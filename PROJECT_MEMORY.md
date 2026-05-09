# SIM_AutoSystem 项目记忆

## 使用说明
- 新对话开始时，必须优先读取本文件、`AGENTS.md` 和 `docs/project_memory/decision_log.md`，作为项目上下文的固定启动步骤。
- 在完成上述读取后，如果本次任务涉及具体模块、配置或故障，再补充读取与任务直接相关的代码、配置、测试和必要的相关设计文档。
- 本文件用于保存当前有效的项目事实、约束、待办和最近状态，服务于跨对话延续。
- 如果本文件与代码、配置或更具体的文档冲突，以更新且更具体的事实为准，并在本次会话结束前修正本文件。
- 只要本次会话对项目产生了修改，结束前必须同步更新本文件；即使修改范围较小，至少也要更新“最近更新”部分。
- 后续“最近更新”只使用中文；不要再新增英文最近更新章节。
- 本文件记录的是提炼后的项目记忆，不保存完整聊天记录。

## 当前状态说明
- 本文件的初始内容基于仓库根目录 `AGENTS.md`、`README.md` 和当前目录结构整理。
- 当前内容可作为后续对话的稳定起点，但仍应以实际代码、配置和硬件接入进展为最终依据。
- 2026-05-07 起，项目计划回到 NI USB-6423 单 DAQ 路线；不再开发、维护或接入 NI PXIe-7857R / NI-RIO 控制链路。

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
- SIM 9 帧采集流程只使用 USB-6423 波形输出路径；PXIe-7857R / NI-RIO 相关 Python Host、bitfile 诊断、LabVIEW rebuild 工具和配置入口已从当前工作区回退移除。
- 当前项目内的 NI USB-6423 规范接线映射固定为：`slm_enable=port0/line0`、`slm_trigger=port0/line1`、`slm_finish=port0/line2`、`camera_trigger=port0/line5`、`laser_405=port0/line8`、`laser_488=port0/line6`、`laser_561=port0/line7`、`laser_647=port0/line9`。
- Hamamatsu runtime timing 中的 `Actual Inter Frame Gap` 当前按保守读出等待计算：`ceil(TIMING_READOUTTIME * 1_000_000) + ceil(TIMING_MINTRIGGERBLANKING * 1_000_000) + 1000 us`；若读不到 `TIMING_READOUTTIME`，采集逻辑回退到配置中的 `inter_frame_gap_us`。
- SIM 侧第四路红光命名已统一为 `647`；默认配置文件使用 `laser_647_line`，加载旧配置时会自动把 `laser_640_line` / `selected_laser_nm=640` 迁移到新命名。
- 当前 `FusionBtCameraAdapter` 与 `KopinSlmAdapter` 已具备仿真模式和对真实 SDK 绑定的扩展点。
- SLM 正式采集路径已迁移为预烧录 Running Order：主界面连接 SLM 后，按当前波长与相机曝光自动选择 `3.5/2d`、非 `_ang0` RO；RO 模式在内部使用 `PatternPreparationResult(handles=[-1], metadata["mode"]="running_order")` 表示。
- `SimSettingsDialog` 不再拥有 SLM 生命周期；`control_wangbo` 主界面与 SIM controller 必须共享同一个 `slm_adapter`，避免 R11 WinUSB 设备被重复打开。
- 真实 DLL 名称、加载路径与函数签名的集成入口集中在 `sim_control/adapters.py`。
- `SDK/` 目录当前已包含 Hamamatsu `DCAM-SDK4` 材料，以及 FDD `R11` 相关工具、驱动和文档。

## 环境与运行约定
- 当前工作目录根为 `F:\SIM_AutoSystem`。
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
- 不再新增或恢复 `daq_backend`、`pxie7857r`、`rio_lines`、`nifpga`、`NIRioDaqAdapter`、NI-RIO bitfile preflight 或 PXIe-7857R LabVIEW rebuild 相关入口；后续 SIM9 采集以 USB-6423 为唯一 DAQ 控制路径。
- 正式 SIM 采集要求 SLM 已连接并已能选择匹配 RO；未连接时应阻止采集，不再回退到空 `pattern_files`。
- 当前 `.repz11` RO 的 1ms/10ms/50ms 循环能力由 repertoire 内 `[HWA h]` 与 FINISH 控制 loop 定义提供；不要启用占用 SPI_1/SPI_2 的 RO Selection 替代模式，否则会破坏现有 TRIGGER/FINISH 语义。

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
- 在真机上用示波器或图像结果验证 `3ms` 相机曝光 + `1ms RO` 时 SLM 是否持续循环到 `slm_finish_line` FINISH 到来，并确认实际触发/退出时序与 `.repz11` 定义一致。
- 明确重建模块接收 `(9, H, W)` `numpy.uint16` 栈后的接口形式、返回结果和回调链路。
- 在后续每次状态变化后持续维护本文件和决策日志，保证跨对话记忆有效。

## 最近更新
- 2026-05-09：按运行效率与文件管理审计结论执行规范化收尾：`.gitignore` 新增 `.history/`、`test_captures/`、`*_ROI_recordData/`、`*_image/` 和 `crash_frames/` 等运行产物忽略规则，避免编辑器历史、SIM 测试 TIFF、legacy ROI/图像输出和崩溃帧误入版本管理；保留并准备纳入 `tests/test_efficiency_optimizations.py` 作为低拷贝效率优化回归测试。FeatureWorker/OpenCV 统计与预览 OpenCV 转换仅作为后续实验分支候选，不并入当前主线，避免未确认数值/显示语义差异影响可运行状态。
- 2026-05-09：执行低风险运行效率优化：`NIDaqWaveformBuilder.build()` 新增 `include_role_matrix` 参数，默认保留完整 `role_matrix` 诊断行为；正式采集、`SIM采集测试` 和准备实验路径改为 `include_role_matrix=False`，直接构建 packed `uint32` 端口波形，避免为 8 路 TTL role 生成完整 `uint8` 矩阵。新增 `test_packed_only_waveform_matches_full_matrix_plan_without_role_matrix` 验证 packed-only 输出与完整矩阵路径逐样本一致。同步为 `SimControlWindow` 日志控件设置 1000 block 上限，避免长时间运行时 GUI 文本缓冲无界增长，并新增 UI 回归测试覆盖。验证：`.venv\Scripts\python.exe -m pytest tests\ -q` 通过（137 项测试，12 个子测试），`.venv\Scripts\python.exe -m unittest discover -s tests -q` 通过（137 项测试），`.venv\Scripts\python.exe -m compileall -q sim_control control_wangbo tests` 通过。500ms/50ms/1MHz SIM9 本机微基准中，完整矩阵构建均值约 85ms，packed-only 构建均值约 10ms。
- 2026-05-09：继续执行已确认的低风险运行效率优化：`NIDaqWaveformBuilder._pack_port_values()` 改为按各 TTL role 的非零布尔 mask 写入 `uint32` 端口 bit，避免每一路 role matrix 先 `astype(np.uint32)` 产生完整临时数组。新增 `test_waveform_pack_uses_boolean_masks_without_uint32_role_copies` 覆盖该路径；验证：`.venv\Scripts\python.exe -m pytest tests\ -q` 通过（135 项测试，12 个子测试），`.venv\Scripts\python.exe -m unittest discover -s tests -q` 通过（135 项测试），`.venv\Scripts\python.exe -m compileall -q sim_control control_wangbo tests` 通过。标准 500ms/50ms SIM9 波形本机微基准中打包均值约 76ms。
- 2026-05-09：执行低风险运行效率优化：`NIDaqAdapter.play_waveform()` 对已为 `uint32` 的 packed port 波形使用 `astype(copy=False)` 避免额外复制；`FusionBtCameraAdapter.read_frame_sequence()` 读取 DCAM frame data 后直接写入预分配帧栈，减少中间数组；`SimulatedCameraAdapter.read_frame_sequence()` 改为预分配 `(frame_count, H, W)` 栈而非 list + `np.stack`；占位 `ReconstructionWorker` 使用 `uint32` 整数累加/整除生成预览，避免整栈 float32 临时数组。新增 `tests/test_efficiency_optimizations.py` 覆盖这些低拷贝路径；验证：`.venv\Scripts\python.exe -m pytest tests\ -q` 通过（134 项测试，12 个子测试）。
- 2026-05-08：修正 DAQ 页 `SIM采集测试` 的 SIM9 标准测试波形：测试仍使用 500ms 专用相机曝光，同时将测试专用 `inter_frame_gap_us` 固定为 50ms，使上一帧 `SLM finish` 起点到下一帧 `SLM trigger` 起点间隔为 50ms。该 50ms 只作用于 `SIM采集测试` 的克隆 timing，不写回 DAQ 页控件、默认配置或正式采集配置；新增测试覆盖 500ms/50ms 测试波形与 waveform 帧间 TTL 结构。
- 2026-05-08：修复 DAQ 页 `SIM采集测试` 使用真实 Hamamatsu DCAM 时可能误报 `DCAM frame wait failed: TIMEOUT` 的问题；`FusionBtCameraAdapter.read_frame_sequence()` 现在会先读取 DCAM transfer count 并消费已进入 buffer 的帧，只在没有新帧时等待 `FRAMEREADY` 事件，timeout 报错会包含 `captured X/9` 以区分软件等待与真实触发链路故障。同时将 `SIM采集测试` 的测试专用相机曝光固定为 500ms，用于 Running Order 选择、DAQ 波形、相机配置和测试 TIFF 文件名，但不写回主界面或正式采集的全局曝光配置。
- 2026-05-08：修复 `SimSettingsDialog` DAQ 页 `Camera Trigger` / `SIM采集` 测试重复打开 Hamamatsu DCAM camera 0 导致 `FAILOPENCAMERA` 的问题；设置弹窗现在可接收并复用主窗口共享的 `camera_adapter`，测试结束只在不破坏既有连接的前提下清理相机。同步将集成主界面标题更新为 `Intelligent Super-Resolution Imaging`，并从 `control_wangbo/CellSorting_ui.ui` 重新生成 `control_wangbo/CellSorting_ui.py`。
- 2026-05-07：统一 AI 工具项目说明入口：`AGENTS.md` 已重写为唯一主指令文件，合并原 `CLAUDE.md` 与旧 `AGENTS.md` 的有效信息，并明确后续任何 AI 工具修改项目说明时只改 `AGENTS.md`；`CLAUDE.md` 已改为仅说明该规则并通过 `@AGENTS.md` 引用主文件的薄壳。
- 2026-05-07：按项目计划变更回退到 USB-6423-only 代码状态。tracked 文件恢复到 `7241893`（2026-04-28，`Merge pull request #2 from XinTan12/codex/orca-flash-roi`），并删除未跟踪的 PXIe-7857R / NI-RIO / LabVIEW rebuild 相关模块、文档和测试面；当前只保留 USB-6423 波形驱动的 SIM 9 帧采集流程。回退前的工作区 diff、状态、未跟踪清单和被删未跟踪文件副本保存在 `.tmp/rollback-backup/`。
- 2026-05-07：合并 `PROJECT_MEMORY.md` 中英文最近更新记录，删除英文最近更新章节；后续项目记忆更新统一写入中文“最近更新”。
- 2026-04-28：新增 Hamamatsu ORCA-Flash 4.0 ROI 能力适配。`FusionBtCameraAdapter` 通过 `prop_getattr()` 读取 DCAM `SUBARRAYHPOS/HSIZE/VPOS/VSIZE` 属性范围，将请求的 ROI 尺寸和原点裁剪到相机支持的边界与步进，并按避免瞬时非法组合的顺序应用 subarray 大小/偏移变更。适配器返回 `applied_roi`、`sensor_width`、`sensor_height`、`roi_step_px` 和动态 `roi_size_presets`；controller 会从 `applied_roi` 同步 `CameraConfig`，集成的 `control_wangbo` SIM 相机 UI 会刷新图像尺寸选项、ROI spinbox 范围、summary/config 状态，并在连接后持久化硬件裁剪后的 ROI。验证：`.venv\Scripts\python.exe -m unittest tests.test_sim_camera_adapter tests.test_sim_preview_restart -q` 通过（64 项测试）；`.venv\Scripts\python.exe -m unittest discover -s tests -q` 通过（121 项测试）。
- 2026-04-27：在集成的 `control_wangbo` SIM 面板中新增用户可见的相机 bit depth 选项 `8-bit`、`12-bit` 和 `16-bit`。runtime 刷新始终保留这些用户选项，同时兼容硬件 adapter 回退到实际应用 bit depth；仿真模式报告 `[8, 12, 16]`，能应用有效请求，无效请求回退到 16-bit，并将仿真预览/采集帧限制到所选 bit depth。`SimSettingsDialog` 同步刷新为更大的 header、更大的底部按钮、更宽且居中的 Laser 控件，以及更紧凑的 DAQ 间距，以保持 920x600 无重叠回归。
- 2026-04-27：新增 SIM live 预览 `Auto Contrast` 显示模式。集成 SIM 相机预览在 `#max_gray` 附近新增默认关闭的 `chb_sCMOS_autoContrast` 复选框；手动模式仍按 `0..#max_gray -> 0..255` 显示，自动模式使用 `sim_control/preview_contrast.py` 的 0.5/99.5 百分位 Lo/Hi 估计、像素采样上限和平滑 Lo/Hi 状态，以适配 latest-frame-wins 的响应式预览。该功能只影响显示，不改变原始相机帧、9 帧采集栈、重建输入或保存数据；测试覆盖手动转换、自动百分位、离群值、常量帧、平滑、UI 存在性和渲染分支选择。
- 2026-04-27：同步 `control_wangbo/CellSorting_ui.ui` 与集成 runtime SIM 面板：将 `SIM Runtime` Camera/SLM/DAQ 状态组移入 Designer 源文件，保留隐藏的旧 `lbl_SLM_status` 以兼容，恢复 `.ui` 源中的 `cmb_sCMOS_bitDepth`，用 `.venv\Scripts\pyuic5.exe` 重新生成 `control_wangbo/CellSorting_ui.py`，并更新 `control_wangbo/main.py` 绑定静态 runtime 状态控件，避免运行时插入重复组。验证：`.venv\Scripts\python.exe -m pytest tests\test_ui_regressions.py tests\test_main_window_scroll_area.py -q` 通过（8 项测试）；`.venv\Scripts\python.exe -m pytest tests\ -q` 通过（105 项测试，12 个子测试）。
- 2026-04-27：修复 SIM Running Order 曝光分档边界：`<10ms` 选择 `1ms RO`，`10ms.. <50ms` 选择 `10ms RO`，`>=50ms` 选择 `50ms RO`，避免 10ms 被误选到 1ms、50ms 被误选到 10ms。同时修复集成主界面 SIM 相机 ROI/曝光/bit depth 变更后的 runtime timing 刷新：相机已连接且 Live 未运行时立即重新 `apply_camera_config()` 并刷新 `TIMING_READOUTTIME` / `Actual Inter Frame Gap`；Live 运行时沿用重启 Live 路径并在启动前同步刷新 timing。顺手收紧 `SimSettingsDialog` DAQ group 内部上下 margin，消除 600px 最小高度下 DAQ 通道 combo 重叠。验证：`.venv\Scripts\python.exe -m pytest tests\test_running_orders.py tests\test_sim_preview_restart.py tests\test_sim_summary.py -q` 通过（54 项测试，12 个子测试），`.venv\Scripts\python.exe -m pytest tests\ -q` 通过（105 项测试，12 个子测试）。
- 2026-04-27：修复 `control_wangbo/main.py` 集成 SIM GUI 的 6 项交互/UI 问题：曝光时间 spinbox 改为 snap 桶步进（1..10、20/30/40/50、100 起每 50ms），曝光变更在 SLM 已连接时即时刷新 Running Order 且交互过程不频繁写盘，保存设置后立即 apply DAQ 配置以刷新 Runtime DAQ Ready 状态；隐藏 SLM 连接区冗余 RO 标签并移除 SIM Runtime 底部 0/9 进度条。同时压缩 `SimSettingsDialog`，将显式字体统一为 Segoe UI、放大 Laser tab 选项，并在不改变 DAQ 内部布局的前提下把最小高度从 660 收紧到 600；`sim_control/ui_sim_settings_dialog.py` 已由项目 `.venv` 的 `pyuic5` 重新生成。
- 2026-04-27：新增项目专用 Codex/Agents/Claude skill `.agents/skills/pyqt5-sim-gui`，用于约束后续 SIM GUI 开发保持 PyQt5、`pyuic5` 生成文件边界、实时采集线程边界、配置驱动硬件接入和 `control_wangbo/` 默认只读；该 skill 已同步到用户级 `C:\Users\user\.codex\skills\pyqt5-sim-gui`、`C:\Users\user\.agents\skills\pyqt5-sim-gui` 与 Claude Code 个人目录 `C:\Users\user\.claude\skills\pyqt5-sim-gui`。同时为 Claude Code 安装并启用 `python-plugin@laurigates-claude-plugins`、`testing-plugin@laurigates-claude-plugins`、`pyright@claude-code-lsps`，并安装全局 `pyright 1.1.409`；对应 Python/testing skills 也已同步到 Codex/Agents 用户级 skill 目录。
- 2026-04-26：实现 SLM Running Order 迁移。`SimSettingsDialog` 只保留 Laser/DAQ，不再管理 SLM 连接或手动 9-pattern 加载；`control_wangbo` 主界面新增共享 SLM 连接控件，统一使用 `SimAcquisitionController.slm_adapter`。配置 schema 升到 v3 并新增 `selected_running_order`，summary 显示 `Pattern RO`；正式 SIM 采集在 SLM 未连接时阻止并提示，连接后按波长、曝光桶、`pitch == "3.5"`、`mode == "2d"`、非 `_ang0` 自动选择预烧录 RO。adapter 绑定 `R11_RpcRoSetSelected`，仿真 SLM 提供 24 个 RO，RO 模式以 `handles == [-1]` 表示。本地 SDK/repertoire 复核确认当前 `.repz11` 的循环行为来自 `[HWA h]` 与 FINISH 控制的 `{f ...}` 循环，不通过 SDK 运行时切换循环模式；保持 `slm_trigger_line` 对应 SPI_1/TRIGGER、`slm_finish_line` 对应 SPI_2/FINISH。验证：`.venv\Scripts\python.exe -m pytest tests/ -q` 通过（100 项测试，4 个子测试）。
- 2026-04-24：完成 SIM P0 稳健性增强：新增 `BackendConfig.simulation_mode`，配置 schema 升到 v2 且默认 `false`；新增 `sim_control/sim_adapters.py` 用于显式无硬件仿真；增加配置校验与波形时序 warning；通过 `frame_captured` 传递 9 帧采集进度；在 SIM 窗口中新增可复用 LED/进度 UI；修复 `control_wangbo/main.py` 对 controller 返回 `AcquisitionBatch` 对象的处理。验证：`.venv\Scripts\python.exe -m pytest tests/ -q` 通过（90 项测试）。
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

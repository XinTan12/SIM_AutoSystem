# SIM_AutoSystem 项目记忆

## 使用说明
- 新对话开始时，必须优先读取本文件、`AGENTS.md` 和 `docs/project_memory/decision_log.md`，作为项目上下文的固定启动步骤。
- 完成三件套读取后，如果任务涉及具体模块、配置或故障，再补充读取直接相关的代码、配置、测试和必要设计文档。
- 本文件记录当前有效的项目事实、约束、风险、待办和最近状态，不保存完整聊天记录。
- 如果本文件与代码、配置或更具体文档冲突，以更新且更具体的事实为准，并在本次会话结束前修正本文件。
- 只要本次会话对项目产生修改，结束前必须同步更新本文件；至少更新“最近更新”部分。
- 后续“最近更新”只使用中文；不要新增英文最近更新章节。

## 当前状态说明
- 当前内容以仓库内 `AGENTS.md`、当前代码、配置和最近实现结果为准。
- 2026-05-07 起，项目计划回到 NI USB-6423 单 DAQ 路线；不再开发、维护或接入 NI PXIe-7857R / NI-RIO 控制链路。
- 当前重点是让 SIM GUI、采集控制、波形生成、硬件适配和占位重建/特征 pipeline 在无真实硬件时可通过仿真模式运行，并逐步接入真实 Hamamatsu 相机、Kopin/FDD SLM 与 NI USB-6423。
- 当前 Git 工作流固定为：`main` / `origin/main` 保存稳定主线，`dev` / `origin/dev` 保存当前大改动开发线，`E:\Intelligent_SR\SIM_AutoSystem-stable` 作为 detached `origin/main` 的稳定版运行 worktree。

## 项目目标
- 构建面向微流控捕获与 SIM 成像联动的自动化系统。
- 近期集成目标链路：
  `control_wangbo` 微流控捕获 -> `sim_control` 执行 9 帧 SIM 采集 -> 输出 `(9, H, W)` `numpy.uint16` 图像栈 -> 重建与特征分析 -> 将决策结果回传给微流控流程执行 `release/sort`。

## 目录职责
- `sim_control/`：当前 SIM 侧主开发区，包含 GUI、采集控制、波形生成、预览、适配器、配置读写、数据模型和占位 pipeline。
- `control_wangbo/`：队友历史微流控控制代码，默认只读；仅在用户明确要求或 SIM 集成入口必须调整时修改。
- `app.py`：项目集成 GUI 顶层启动入口，启动 `control_wangbo/main.py` 的主界面。
- `sim_control/sim_acquisition_app.py`：独立 SIM 采集 GUI 启动入口，用于单独调试 SIM 采集链路。
- `config/sim_control_config.json`：默认 SIM 配置文件，承载设备、时序、后端、SDK 路径和 DAQ 线位配置。
- `start.cmd`：Windows 下优先使用的项目集成 GUI 启动脚本。
- `SDK/`：本地厂商 SDK、驱动和资料目录；仓库只跟踪 `SDK/README.md` 的目录约定。
- `tests/`：回归测试、适配器测试和 UI 行为测试。

## 当前硬件与集成状态
- 相机方案：Hamamatsu ORCA-Fusion BT，通过 `DCAM-API` / `DCAM-SDK4` 控制。
- SLM 方案：Kopin / Forth Dimension Displays `QXGA-R11-STR`，通过 `R11CommLib` over WinUSB 控制。
- DAQ 方案：NI USB-6423，负责输出 `slm_enable_line`、`slm_trigger_line`、`slm_finish_line`、`camera_trigger_line` 和激光触发线的同步 TTL。
- SIM9 采集只使用 USB-6423 波形输出路径；PXIe-7857R / NI-RIO 相关 Python Host、bitfile 诊断、LabVIEW rebuild 工具和配置入口已从当前主流程移除。
- 当前 NI USB-6423 规范接线固定为：`slm_enable=port0/line0`、`slm_trigger=port0/line1`、`slm_finish=port0/line2`、`camera_trigger=port0/line5`、`laser_405=port0/line8`、`laser_488=port0/line6`、`laser_561=port0/line7`、`laser_647=port0/line9`。
- SIM 侧第四路红光统一命名为 `647`；旧 `640` 配置通过迁移逻辑兼容。
- SLM 正式采集路径使用预烧录 Running Order：主界面连接 SLM 后，按当前波长与相机曝光自动选择 `3.5/2d`、非 `_ang0` RO；RO 模式以 `PatternPreparationResult(handles=[-1], metadata["mode"]="running_order")` 表示。
- 当前 `.repz11` RO 的 1ms/10ms/50ms 循环能力由 repertoire 内 `[HWA h]` 与 FINISH-controlled loop 定义提供；不要启用占用 SPI_1/SPI_2 的 RO Selection 替代模式。

## 环境与运行约定
- 当前工作目录根以本地实际工作区为准；本机当前为 `E:\Intelligent_SR\SIM_AutoSystem`。
- SIM 侧统一使用根目录 `.venv` 作为项目环境。
- 不依赖 `control_wangbo/.venv`，该旧环境不属于当前主流程。
- 默认配置路径为 `config/sim_control_config.json`。
- 启动项目集成 GUI：`.\.venv\Scripts\python.exe app.py`。
- Windows 启动脚本：`.\start.cmd`。
- 启动独立 SIM 采集 GUI：`.\.venv\Scripts\python.exe -m sim_control.sim_acquisition_app --config config\sim_control_config.json`。
- 基准测试命令：`.\.venv\Scripts\python.exe -m unittest discover -s tests -q`。
- pytest 可用时运行：`.\.venv\Scripts\python.exe -m pytest tests\ -q`。

## 当前关键约束
- 默认不修改 `control_wangbo/`；新 SIM 侧开发优先放在 `sim_control/` 或新的顶层模块。
- 日常开发默认在 `dev` 分支进行；`main` 只接收通过测试、review 和必要真机验收后的合并。
- stable worktree 只用于运行远程稳定版和真机对照，不在其中开发或提交。
- 配置驱动优先；设备路径、TTL 线位、SDK 定位和后端选择不要硬编码。
- 实时采集路径避免同步磁盘 I/O。
- 硬件适配逻辑尽量集中在 `sim_control/adapters.py`。
- 不再新增或恢复 `daq_backend`、`pxie7857r`、`rio_lines`、`nifpga`、`NIRioDaqAdapter`、NI-RIO bitfile preflight 或 PXIe-7857R LabVIEW rebuild 相关入口。
- 正式 SIM 采集要求 SLM 已连接并已能选择匹配 RO；未连接时应阻止采集，不回退到空 `pattern_files`。
- SIM 正式采集的 `inter_frame_gap_us` 默认按 `50_000 us` 执行；只有相机根据当前 ROI/传输接口返回的 `recommended_inter_frame_gap_us` 存在且小于 `50_000 us` 时，才使用该计算值。
- `SimSettingsDialog` 不拥有 SLM 生命周期；集成主界面与 SIM controller 必须共享同一个 `slm_adapter`，避免 R11 WinUSB 设备被重复打开。
- SIM live 预览采用 `latest-frame-wins`：采集线程持续更新最新帧快照，GUI 端轮询显示最新帧，允许丢弃中间帧以避免旧帧积压。
- `ui_sim_settings_dialog.py` 由 `pyuic5` 从 `.ui` 生成；修改 UI 时编辑 `.ui` 后重新生成，不直接手改生成文件。
- 每次对项目进行了修改后，必须使用独立 subagent 对本次改动进行 review 审查；审查需明确列出已满足的标准和证据、未满足的标准和原因，以及需要用户人工确认、真机确认或额外业务判断的事项。若当前工具环境无法启动独立 subagent，主 agent 必须先向用户说明原因并请求确认，不得静默自审替代。

## 当前工作重点
- 保持 SIM GUI、采集控制与适配器在无真实硬件时仍可通过仿真模式运行。
- 将真实相机与 SLM SDK 逐步接入现有适配器层。
- 打通 9 帧采集到 `(9, H, W)` `numpy.uint16` 输出栈的稳定接口。
- 明确重建模块与特征分析模块的输入输出接口，并把最终决策结果接回微流控流程。
- 保持 SIM live 预览为低延迟 `latest-frame-wins` 模式。

## 已知风险与待确认事项
- 厂商 SDK 的真实 DLL 名称、导出函数和调用签名仍需结合本机安装内容最终核对。
- NI 触发时序、相机触发、SLM 完成信号和激光线之间的同步细节仍需真实硬件联调验证。
- 重建与特征分析部分当前仍以占位流程为主，尚未形成稳定生产接口。
- README 和部分历史文档曾出现编码显示异常；若后续需要依赖 README 作为稳定文档源，应先统一编码。
- 真机 Hamamatsu + R11 + NI USB-6423 的 Stop 响应、DAQ 全低和下一次采集可启动仍需硬件验收。

## 当前待办
- 核实 `config/sim_control_config.json` 中与 SDK 路径、设备配置和时序相关的字段是否足以支持真实硬件接入。
- 在 `sim_control/adapters.py` 中补齐 Hamamatsu `DCAM-SDK4` 的真实绑定实现。
- 在 `sim_control/adapters.py` 中补齐 `R11CommLib` 的真实绑定实现。
- 在真机上验证 NI USB-6423 规范线位 `0/1/2/5/8/6/7/9` 中 9 帧采集时序是否满足联动要求。
- 在真机上用示波器或图像结果验证 `3ms` 相机曝光 + `1ms RO` 时 SLM 是否持续循环到 `slm_finish_line` FINISH 到来，并确认实际触发/退出时序与 `.repz11` 定义一致。
- 明确重建模块接收 `(9, H, W)` `numpy.uint16` 栈后的接口形式、返回结果和回调链路。

## 最近更新
- 2026-05-13：在 `AGENTS.md` 中新增“修改后审查机制”：每次项目修改后必须使用独立 subagent review，并固定审查输出为“已满足标准及证据、未满足标准及原因、需要用户人工确认、真机确认或额外业务判断的事项”；若当前工具环境无法启动独立 subagent，主 agent 必须先向用户说明原因并请求确认，不得静默自审替代；同步将该长期规则写入 `PROJECT_MEMORY.md` 和决策日志。
- 2026-05-12：按 `/review` 结果清理手写 Python 中文结构注释中的模板化低价值 docstring，将 `封装...小段规则`、`执行本模块...`、`创建本模块...` 等泛化说明替换为具体职责说明或删除；保留模块说明、主要类/函数说明和关键流程块注释，不改变运行逻辑、接口或生成 UI 文件。验证：`.venv\Scripts\python.exe -m compileall -q app.py sim_control control_wangbo\main.py tests` 通过，`.venv\Scripts\python.exe -m unittest discover -s tests -q` 通过（164 项测试）；当前 `.venv` 未安装 `pytest`。
- 2026-05-12：为项目手写 Python 文件补充中文结构注释：覆盖 `app.py`、`sim_control/` 手写模块、`tests/` 和 `control_wangbo/main.py`，增加模块说明、主要类/函数说明和关键流程块注释；未修改生成 UI 文件、`mvsdk.py`、SDK 厂商样例或运行逻辑。验证：`.venv\Scripts\python.exe -m compileall -q app.py sim_control control_wangbo\main.py tests` 通过，`.venv\Scripts\python.exe -m unittest discover -s tests -q` 通过（164 项测试）；当前 `.venv` 未安装 `pytest`，因此 `pytest tests\ -q` 未运行。
- 2026-05-12：增强 `docs/project_file_annotations.html` 的 Python 代码选区解释：新增离线 `PYTHON_CODE_EXPLANATIONS` 预置解释索引，覆盖 SIM 主模块、集成主界面和历史相机/MCU 线程的关键函数、类和常量；右键解释优先展示教学式分节说明，未命中时回退到本地规则解释；仍不调用外部 AI、网络 API 或后端。
- 2026-05-12：审查并修正 `docs/project_file_annotations.html` 模块页渲染：模块“推荐阅读顺序”和“模块下所有文件”只展示本模块文件，跨模块依赖改列为“相关外部文件”，避免配置、脚本、硬件资源等模块误把外部分组文件显示为本组文件。
- 2026-05-12：增强 `docs/project_file_annotations.html` 静态项目注释页：改为左右栏独立滚动的离线阅读工具，新增模块总览、模块内文件关系和阅读顺序、文件完整内容查看、`.repz11` 按 ZIP 资源包解读方式，以及 Python 代码选区右键解释功能；仍不改变运行代码、配置 schema 或硬件控制逻辑。
- 2026-05-12：修正项目文件注释页中 `patterns/2d_3.5.repz11` 的阅读说明：该 R11 repertoire 资源应按 zip 容器处理，复制后改后缀为 `.zip` 并解压查看内部 `.rep`、`.seq11` 和 PNG 图案文件；不要把它作为普通二进制十六进制文件解释。
- 2026-05-12：修正 `docs/project_file_annotations.html` 注释页自身的版本文件说明条目，并让搜索索引覆盖右侧实际展示的阅读建议、协作关系和修改注意事项，避免新增文档入库后覆盖声明失真或搜索漏项。
- 2026-05-12：新增 `docs/project_file_annotations.html` 静态中文项目文件注释页，覆盖当前版本管理内项目文件，面向首次接触项目和代码基础较弱的读者解释每个文件的项目作用、阅读顺序、代码结构、协作关系和修改注意事项；该文档不改变运行代码、配置 schema 或硬件控制逻辑。
- 2026-05-11：将 OMC / oh-my-claudecode 生成的本地项目缓存目录 `.omc/` 加入 `.gitignore`，避免工具元数据污染 `git status` 或被误提交；该目录不属于远程源码内容。
- 2026-05-11：继续优化 SIM 采集设置弹窗 DAQ 页布局：在 `DAQ Wiring` 组内为设备行、线路矩阵、测试行和底部之间增加均衡的垂直弹性间隔，并将线路矩阵行距加大，避免空白集中在组底部；同步重新生成 `ui_sim_settings_dialog.py`，新增 UI 回归断言验证三段垂直空白分布和底部剩余空间。
- 2026-05-11：美化 SIM 采集设置弹窗布局：保留 `Laser` / `DAQ` 双页签和原生 PyQt5 外观，将标题区固定为紧凑高度，页签区域改为主体扩展，DAQ 页移除左右居中 spacer 并让 `DAQ Wiring` 区域铺开；放宽 DAQ 线路与测试目标下拉框宽度，并用底部弹性 spacer 避免内容被垂直分散。已从 `.ui` 重新生成 `ui_sim_settings_dialog.py`，并新增 UI 几何回归断言覆盖标题高度、页签起点、DAQ 组宽度和下拉框宽度。
- 2026-05-11：根据 review 发现的问题补齐 SIM Timing 默认配置修正：`config/sim_control_config.json` 中 `timing.inter_frame_gap_us` 已从 `10_000 us` 改为 `50_000 us`，与 `TimingConfig` 默认值、controller/worker 正式采集生效规则和主界面摘要显示保持一致。
- 2026-05-11：简化 SIM 设置弹窗 DAQ 页面：移除 `Timing` 模块中 `sample_rate_hz`、`edge_pulse_us`、`inter_frame_gap_us`、`slm_enable_guard_us` 四个可编辑控件，保留这些字段作为内部采集时序参数；主界面 SIM 参数摘要最底部新增只读 `Timing` 区块显示四项时序；`inter_frame_gap_us` 默认改为 `50_000 us`，并统一 controller/worker 正式采集路径为“仅当相机推荐 gap 小于 50ms 时才覆盖默认值”。新增/更新 UI、摘要和 controller 回归测试；验证中新增目标用例通过，相关非临时目录用例通过；全量 pytest 在当前 sandbox 中剩余 6 项 `tempfile.TemporaryDirectory()` 写入权限失败，非本次业务断言失败。
- 2026-05-11：整理 GUI 启动入口：根目录默认入口由 `sim_control_app.py` 改为 `app.py`，职责调整为启动 `control_wangbo/main.py` 的项目集成主界面；Windows 快速启动脚本由 `start_sim_control.cmd` 改为 `start.cmd`；原独立 SIM 采集窗口入口迁移到 `sim_control/sim_acquisition_app.py`，通过 `python -m sim_control.sim_acquisition_app --config config\sim_control_config.json` 单独启动；新增入口回归测试覆盖默认入口、启动脚本和独立 SIM 入口命名。
- 2026-05-11：建立当前仓库 Git 工作流：当前大改动开发线迁移到 `dev`，远程开发分支为 `origin/dev`；稳定主线保留为 `main` / `origin/main`；新增 `E:\Intelligent_SR\SIM_AutoSystem-stable` detached `origin/main` worktree 作为远程稳定版运行和真机对照目录；项目说明新增 `dev -> PR -> main -> stable worktree 更新` 的代码更新流程；旧 `codex/*` 和 `backup/*` 分支按用户确认清理。
- 2026-05-11：针对 SIM 主链路防卡死方案的 review 结论继续收敛实现：Hamamatsu DCAM 分片等待现在只对 `TIMEOUT` 继续短轮询，遇到非 timeout SDK 错误会立即抛 `HardwareError`，避免真实硬件错误被拖到总超时才暴露；`run_single_acquisition()` 改为先校验 9 帧 `uint16` stack 与 timestamps，再补发缺失 `frame_captured` 进度，避免无效采集结果造成 9 帧进度噪声；standalone SIM GUI 的 Prepare/Run 正式路径不再在 GUI 线程执行 `initialize_hardware()`、`apply_camera_config()` 或 Running Order 选择，而是通过 controller worker 的 `prepare_only`/正式采集 payload 后台完成；standalone GUI 会从 worker 的 `running_order_selected` 状态同步 `selected_running_order`；集成主界面在 preview 停止失败并阻止正式采集时会恢复原 live preview 请求状态。新增对应 DCAM 非 TIMEOUT、采集校验前帧事件、standalone worker preflight、worker RO 状态同步、controller prepare-only payload 与 preview-stop 状态恢复回归测试。验证：`.venv\Scripts\python.exe -m pytest tests\ -q` 通过（161 项测试，16 个子测试），`.venv\Scripts\python.exe -m unittest discover -s tests -q` 通过（161 项测试），`.venv\Scripts\python.exe -m compileall -q sim_control control_wangbo tests` 通过；真机 Hamamatsu + R11 + NI USB-6423 停止响应和示波器全低仍需硬件验收。
- 2026-05-11：执行 SIM 主链路采集闭环与防卡死修复：`run_single_acquisition()` 增加 `AcquisitionCancelled`、`stop_event` 传播和 9 帧 `uint16` 栈/timestamps 强校验，避免异常相机返回误报成功；`SimAcquisitionController.stop()` 改为只设置取消事件，不再从 GUI/调用线程直接操作 DAQ 或相机，硬件 `disarm` 与 `set_all_low` 统一由采集 worker 的 `finally` 路径执行；NI USB-6423 波形等待改为 `is_task_done()` 短轮询并在取消时 `task.stop()`；Hamamatsu DCAM frame wait 改为 50ms 分片等待并保留总 timeout 与 `captured X/9` 诊断；standalone SIM GUI 默认正式采集路径改为 Running Order；集成主界面正式采集 preflight 移入 worker，并在 preview 未确认停止时阻止采集。验证通过软件测试；真机 Hamamatsu + R11 + NI USB-6423 停止响应和示波器全低仍需硬件验收。
- 2026-05-09：按保持可运行前提的治理策略做低风险规范化修改：默认配置不再持久化本机绝对 `config_path`；手动预览 LUT 增加只读有限缓存并在全量 `uint16` 范围下避免不必要 clip 拷贝；controller payload 类测试改为断开内部 worker 后仅捕获发射 payload，降低普通测试误触发采集 worker/硬件路径的风险；新增 `.editorconfig` 与 `.gitattributes` 统一文本文件编码/换行约定，并把 README/项目说明中的失效机器路径改为相对路径或实际工作区说明。
- 2026-05-09：执行 SIM 实时预览与 FeatureWorker 效率优化：预览显示新增速度优先 helper，按显示窗口先裁剪 `uint16`、再 resize、再 LUT 映射为 `uint8`，集成主界面 SIM 预览改用该路径，保持 `latest-frame-wins` 与原始 `uint16` 数据链路不变；`FusionBtCameraAdapter.read_preview_frame()` 对 DCAM 已返回的 NumPy 帧使用 `np.asarray(..., dtype=np.uint16)` 避免额外复制；`FeatureWorker` 优先使用 OpenCV `meanStdDev` / `minMaxLoc` 计算强度特征，并保留 NumPy fallback。
- 2026-05-09：执行低风险运行效率优化：`NIDaqWaveformBuilder.build()` 新增 `include_role_matrix` 参数，默认保留完整 `role_matrix` 诊断行为；正式采集、`SIM采集测试` 和准备实验路径改为 `include_role_matrix=False`，直接构建 packed `uint32` 端口波形，避免为 8 路 TTL role 生成完整 `uint8` 矩阵。
- 2026-05-08：修复 `SIM采集测试` 使用真实 Hamamatsu DCAM 时可能误报 `DCAM frame wait failed: TIMEOUT` 的问题；`FusionBtCameraAdapter.read_frame_sequence()` 会先读取 DCAM transfer count 并消费已进入 buffer 的帧，只在没有新帧时等待 `FRAMEREADY` 事件，timeout 报错包含 `captured X/9`。
- 2026-05-08：修复 `SimSettingsDialog` DAQ 页 `Camera Trigger` / `SIM采集` 测试重复打开 Hamamatsu DCAM camera 0 导致 `FAILOPENCAMERA` 的问题；设置弹窗现在可接收并复用主窗口共享的 `camera_adapter`。
- 2026-05-07：统一 AI 工具项目说明入口：`AGENTS.md` 已重写为唯一主指令文件；`CLAUDE.md` 改为薄壳引用。后续任一 AI 工具修改项目说明时只改 `AGENTS.md`。
- 2026-05-07：按项目计划变更回退到 USB-6423-only 代码状态，并删除未跟踪的 PXIe-7857R / NI-RIO / LabVIEW rebuild 相关模块、文档和测试面；当前只保留 USB-6423 波形驱动的 SIM9 采集流程。
- 2026-04-26：实现 SLM Running Order 迁移；`SimSettingsDialog` 不再管理 SLM 连接或手动 9-pattern 加载；`control_wangbo` 主界面新增共享 SLM 连接控件，统一使用 `SimAcquisitionController.slm_adapter`。
- 2026-04-23：建立仓库内跨对话记忆机制，新增 `PROJECT_MEMORY.md` 与 `docs/project_memory/decision_log.md`；约定新对话先读取 `AGENTS.md`、`PROJECT_MEMORY.md` 和决策日志。

# 项目决策日志

## 使用说明
- 本文件只记录影响多个后续会话的重要决策、长期约束或方向性选择。
- 每条记录至少包含：日期、决策、原因、影响。
- 普通操作、临时讨论和纯执行细节不写入本文件。

## 2026-06-27

### 决策：第四路红光波长按机器可配置（638/647），取代 2026-06-22「统一迁移为 638」；新增 config schema v13 与 `red_laser_nm` 机器档案
- 原因：
  现有两台 SIM 系统前三档波长相同（405/488/561），第四路红光一台真 `638 nm`、另一台真 `647 nm`，要用同一套代码控制两台。2026-06-22「红光统一迁移为 638」基于当时只有一台、红光标称 638 的前提，会把 647 不可逆压成 638——647 机器无法被真实表示（选不了 647、存不下独立 647 OTF、加载即被抹成 638）。本质问题：红光波长是「每台机器固定、机器间不同」的配置量，而非要并存的第 5 个波长（一台机同一时刻只有一种红光、接同一条 line 12）。
- 影响：
  ① **机器档案 `AppConfig.red_laser_nm`**（638/647，默认 638）表达本机红光身份；`SUPPORTED_LASERS=(405,488,561,638)` 收敛为显式默认常量，新增纯函数 `models.supported_lasers_for(red_laser_nm)` 给所有「按机器红光」的点（GUI 波长下拉、recon 下拉、validate）。`LASER_ROLE_MAP` 中 638 与 647 都映射到中性键 `laser_red_line`（物理 line 12 不变）；DAQ 角色键、`DaqLineConfig`、`DEFAULT_DAQ_LINE_INDICES` 由 `laser_638_line` 全量重命名为 `laser_red_line`。`ReconstructionConfig` 加 `otf_647_path`/`estimated_params_647_path`（与 638 并存，按机器波长取用；NA/pixel/theta 跨机共用）。
  ② **config schema v13**：`_migrate_v12_to_v13` 按 647 证据（selected==647 / 非空 laser_647_line / 非空 otf_647/estimated_params_647）推断 `red_laser_nm` 并保留 647 身份、合并历史红光线键到 `laser_red_line`、校正 `selected∈supported_lasers_for(red)`；最老错误命名 `640` 仍归一到 638。**命门**：移除 `app_config_from_dict` 在迁移链后对 `_migrate_red_laser_aliases` 的二次调用——原会把每次加载的 647 重新压回 638。validate 按机器红光校验 + `red_laser_nm∈{638,647}`。
  ③ **SLM RO / immediate 双向 fallback**（取代 2026-06-25「647 单向兼容 638 GUI 入口」）：`find_best_running_order` 与 `immediate_live_wavelength_matches` 改 638↔647 红光等价双向，优先精确匹配本机红光波长命名、缺失时 fallback 另一红光（warning 动态生成）。
  ④ **主界面运行时切换**（部署=一份配置+界面切换）：新增红光机器切换下拉 `cmb_main_red_laser`（638/647），切换复用波长变更安全流程——绝不提前写 `selected_laser_nm`，只写 `red_laser_nm`+`_apply_sim_red_laser_options`（统一 helper 纯 UI 重建）后委托 `on_sim_camera_setting_changed`（经 sync 检测 old→new）；含 stop 失败硬互锁（关光失败不切 RO）与忙碌 worker 互锁（采集/Z-Scan/DAQ 测试/连接进行中拒绝切换/Load 不部分写入）；Load 路径 stop 先于 merge、helper 先于 findData 回显。DAQ 红光控件 `.ui` 重命名 `cmb/lbl_main_daq_laser_638`→`_red`，走 pyuic5 重生成（零漂移核对）。
  ⑤ **真机/人工确认项**：两台红光 TTL 都接 line 12 需台架确认；现场 v12 已压成 638 的旧配置无法自动恢复 647 标定路径，需人工重填 `otf_647_path`/`estimated_params_647_path`；647 机 repertoire 实际 RO 命名需现场以 `list_running_orders()` 核对。
  审查：高风险（硬件/DAQ/波形/SLM），走 Codex 双循环（方案 4 轮 / 实现 1 轮 0 blocking）+ 独立 subagent review 0 blocking 双审。测试 `unittest discover` 427 OK / `pytest` 433 passed（含新增 `tests/test_red_laser_machine.py` 8 项）。

## 2026-06-26

### 决策：SIM 设置弹窗 DAQ/Recon 两页迁入主 GUI 一级模块、弹窗整体移除；DAQ 诊断测试逻辑下沉到 `sim_control/daq_testing.py`；Recon 配置跨线程下发改 queued signal（硬约束）
- 原因：
  操作者此前改 DAQ 接线/测试与 Recon 重建参数需进二级弹窗 `SimSettingsDialog` 再切 tab，现场不便；该弹窗的 Laser 页、Z-Scan 页此前已分别移除/迁出，仅剩 DAQ/Recon 两页，搬走后弹窗已空。用户要求把这两页全部迁到主界面一级界面、弹窗整体移除，并把 SIM 摘要栏+Load/Save 移到右侧新列、加宽主窗口。同时外部 codex 审查指出原 `control_wangbo/main.py` 在 recon worker 已存在时跨线程裸调 `set_reconstruction_config` 是潜在线程安全隐患（`pipeline.py` 注释明确该 setter 仅同线程用、GUI 跨线程须走 queued `slot_update_reconstruction_config`）。
- 影响：
  ① **配置唯一入口迁主 GUI**：DAQ/Recon 与已迁入的 Z-Scan 模块并列于 `grp_simConfiguration`（顺序 SLM→Z-Scan→DAQ→Recon→SIM Runtime）；SIM 摘要栏+Load/Save 移到新增右列 `grp_simSummary`；主窗口加宽 1800→2160。遵「主 GUI 控件先 `.ui` 静态定义→`pyuic5` 重生成→`main.py` 仅运行时接线」标准流程（2026-06-25 决策）。② **DAQ 测试逻辑下沉**：原 `SimSettingsDialog` 的 `_run_*_test`/`_PulseTestWorker`/结果 dataclass/常量抽到 `sim_control/daq_testing.py` 的 `DaqTestRunner`（注入共享 adapter+配置快照、worker 内不读 Qt、全路径 `set_all_low`、`camera_externally_owned` 只 disarm、选 RO 经 `exclude_indices` 排除 immediate RO 与正式采集一致），`gui.py` 重导出保 `SimControlWindow` 与既有 import 不破。后续 SIM DAQ 测试改动一律走 `daq_testing.py`，不要再写进 GUI 类。③ **弹窗移除**：删 `SimSettingsDialog` 类、`sim_settings_dialog.ui`、`ui_sim_settings_dialog.py`、`generate_ui_py.cmd`、主界面 `btn_openSimSettings` 及打开/`apply_sim_settings` 接线；`SimControlWindow`（独立 SIM 调试 GUI）保留。后续不要恢复该设置弹窗。④ **Recon 跨线程硬约束**：GUI 向常驻 recon worker 下发配置必须经 `signal_reconstruction_config_changed`(pyqtSignal)→`slot_update_reconstruction_config` 发 `ReconstructionConfig.snapshot()` 快照，**禁止跨线程裸 `set_reconstruction_config`**（与 `SimControlWindow` 既有范式一致）。⑤ **Save/Load 覆盖**：Save/Load(`apply_loaded_sim_settings_payload` 合并 sim_control payload 后回填 DAQ/Recon/Z-Scan 三模块控件 + 同步 controller + ensure recon worker)，避免配置与控件/后端状态分裂。⑥ Recon 波长下拉只切换查看/编辑哪个波长的 OTF，**绝不回写采集波长 `selected_laser_nm`**（采集波长唯一入口仍为主 GUI `cmb_sCMOS_laser`）。本决策不改 DAQ 波形/时序/RO 选择/激光安全语义（只是搬运+加 immediate RO 排除）。审查：高风险，走 Codex 双循环（方案 1 轮 / 实现 2 轮 0 blocking）+ 独立 subagent review 0 blocking 双审。

## 2026-06-25

### 决策：主 GUI 界面控件改为 `.ui` 静态定义、`main.py` 仅运行时接线（取代“纯运行期创建”）
- 原因：
  此前主 GUI（`control_wangbo` 主界面 `CellSorting`）多处控件由 `control_wangbo/main.py` 运行期编程创建（波长下拉、Z-Scan 模块、SIM Runtime 状态、Z 位置标签、SLM immediate RO 等），分散、难维护；且运行期以主窗口为父 + 绝对坐标曾引发“缩窗按钮消失”bug（同日 `btn_sim9_acquire` 静态化修复）。用户要求把主 GUI 动态控件全部改为静态组件、功能与位置不变，并确立“以后主 GUI 加控件都用静态组件”的长期规则。
- 影响：
  主 GUI 控件一律先在 `CellSorting_ui.ui` 静态定义、再 `pyuic5` 重生成 `CellSorting_ui.py`，`main.py` 只做运行时接线（信号、配置同步、带 `itemData` 的下拉填充、`view()` popup、`.ui` 不支持的布局参数如 `setColumnStretch`——pyuic5 5.15.11 的 `<string>` columnStretch 会误生成非法 `setColumnStretch(_translate(...))`，须删 `.ui` property 改运行时补）。**分阶段推进**：阶段 1（已完成）静态化波长下拉 `lb_sCMOS_laser`/`cmb_sCMOS_laser`、Z-Scan 模块 `grp_zscan`+10 控件，并清理 `setup_sim_runtime_status_widgets`/`setup_sim_z_position_widgets` 的动态死后备分支（控件已 `.ui` 静态、分支不可达）；`setup_sim_zscan_module` 由“创建+接线”改“静态存在则接线”（前置 group 检查 + `_zscan_module_wired` 接线后置位防重复 connect、`blockSignals+clear` 防重复 items）。阶段 2（**已完成** 2026-06-25）静态化 SLM 连接区 2×2：`.ui` `gridLayout_SLMConnection` 重排 device(0,0)110/connect(0,1)154/refresh(1,0)90/新增 `cmb_SLM_immediateRO`(1,1)154，`lbl_SLM_status` 移到 layout 外作 hidden 子（`grid.indexOf==-1`）；`main.py` 删 removeWidget/addWidget 重排、只补 `setColumnMinimumWidth`/`setColumnStretch`（同 `columnStretch` 不被 pyuic5 静态生成、运行时补）、`view()` popup 宽（`QComboBox.view()` 运行时对象、`.ui` 无法表达）、tooltip/信号；测试 `test_runtime_slm_immediate_ro_layout` 改名 `test_static_slm_2x2_layout_and_wiring`、删字体度量敏感的 `grp.minimumSizeHint<=288` 护栏改用 `grid.minimumSize<=288`（顺带修复该预存失败），全量 409 OK。**至此主 GUI 控件静态化全部完成**（btn_sim9 + 波长/Z-Scan + SLM）。本决策**取代** `PROJECT_MEMORY` 此前多条“纯 `control_wangbo/main.py` 运行期、不改 `.ui`/`CellSorting_ui.py`”做法（那是为避免动队友生成文件的临时约束，现因方向转变失效）。规则写入 `AGENTS.md`「关键约束」。

### 决策：多波长找样品 immediate RO 采用旧 `647` 红光命名并由 `638 nm` GUI 入口兼容
- 原因：
  现场主 GUI 和 DAQ 配置已统一把第四路红光显示/控制为 `638 nm`，但当前 R11 repertoire 内红光图片和正式 RO 仍沿用旧 `647` 文件/RO 命名。用户要求在模仿 488 immediate RO 时生成 `405/561/647` immediate RO；若只生成 `647_..._imm_*` 而不扩展 immediate-live 波长匹配，当前 `638 nm` GUI 入口会把这些 RO 当作波长不匹配而禁用。
- 影响：
  `patterns/2d_3.5.repz11` 的找样品 immediate RO 覆盖 `405/488/561/647` 四个前导波长；红光 RO 名保留 `647_3.5_2d_imm_f1..f9/_3dir` 以复用现有 repertoire 图片命名。immediate-live 波长匹配规则为：前导波长与当前选择严格相等，或当前选择为 `638` 且 RO 前导波长为旧 `647`；该兼容只对红光单向生效，`647` RO 不可用于 405/488/561。实际点灯仍走当前配置的 `LASER_ROLE_MAP[638] -> laser_638_line`。所有新增 RO 继续遵守 2026-06-24 决策：只复用既有 `A+`/`A-` Lit Pair sequence、每个图案正反相成对、不新增 sequence/image、不启用 SPI RO Selection，Python 只写 `.rep` 骨架，最终 `ACT_IMMEDIATE` activation type 必须由 MetroCon GUI 设置、编译并发送到 board。

### 决策：codex 双循环审查从“每次实质修改强制”改为按复杂度/风险分级触发
- 原因：
  用户为降低每轮响应延迟、避免简单改动也被 codex 多轮审查拖慢，要求把 codex 双循环从“对 `sim_control/`、`config/*.json`、`tests/`、波形/时序、`ui_*` 的每次实质修改都强制”放宽为“按改动风险分级——只有复杂任务才调用 codex review”。属对 2026-06-24 所立强制流程的方向性放宽。
- 影响：
  `AGENTS.md`「Codex 双循环审查工作流」节副标题由“代码修改强制流程”改为“代码修改的分级审查流程”；适用范围改为：①只有**复杂任务**（需拆解为多子任务、跨多文件/模块、含非平凡逻辑或行为变化，与“任务执行方式（并行子代理）”章节“复杂任务”界定一致）才走完整双循环；②简单/单步/低风险改动默认豁免双循环、仅走“修改后 subagent review”；③**安全阀**——涉及硬件控制（相机/SLM/DAQ）、波形/时序、安全或破坏性操作的改动即使简单也按复杂任务对待、仍走双循环（第二循环可直接升级 `/codex:adversarial-review`），复杂度拿不准时从严走双循环。原“必须走双循环”的文件覆盖面降级为“判定对象范围”。**未变**：触发主体仍限定 Claude Code；“修改后 subagent review”机制不受本次分级影响（但其与 codex 双循环“无条件叠加”的关系已由同日「codex 实现审查后免除非高风险 subagent review」决策改为“有条件去重”，详见该条）；codex 会话连续性/串行处理/承载方式等执行细则不变。本条规则文本修改本身按“文档微调＋用户显式指令”豁免 codex 双循环（已向用户说明），仅走独立 subagent review。

### 决策：codex 实现审查后免除非高风险改动的独立 subagent review（审查去重）
- 原因：
  原「Codex 双循环审查工作流」与「修改后审查机制」对同一份实现叠加双审——codex 第二循环（实现验证）审一遍、独立 subagent review 再审一遍。用户要求去掉这层重复：codex 已对“执行的修改结果”（第二循环）审查并通过后，不必再单独做独立 subagent review。
- 影响：
  `AGENTS.md`「修改后审查机制」新增去重与兜底两条、codex 节首互补条改为指向去重、「判定终止降级」条改为以实际执行的审查 blocking 为准。**去重规则**：本次改动已完成 codex 第二循环并通过时，**非高风险**改动免独立 subagent review；**高风险**改动（硬件相机/SLM/DAQ、波形/时序、安全/破坏性，判据同适用范围安全阀）即使 codex 通过**仍做一次** subagent review 双保险（经 AskUserQuestion 用户确认）。**兜底硬约束**：简单/低风险改动、文档记忆类、codex 降级不可用、仅走第一循环 等无 codex 实现审查的情形仍必做 subagent review——任何实质改动至少经一种实现审查，**绝不允许 codex 第二循环与 subagent review 两者都跳过**；两者同时执行时任一 blocking 都不算完成、冲突以更严格者为准。本条覆盖同日「分级触发」决策中“subagent review 仍对每次改动执行”的旧表述。本规则文本修改本身属文档/记忆类、豁免 codex 双循环，仅走独立 subagent review。

### 决策：Z-Scan 操作主入口迁到主 GUI、移除设置对话框 Z-Scan 页、SIM9 采集由「是否开启 Z-Scan」开关门控
- 原因：
  操作者此前需进二级设置弹窗才能配置/运行 Z-Scan，现场不便；且主 GUI「SIM9帧采集」按钮长期硬编码不做 z-scan（`z_scan_enabled=False`），与「采集前自动对焦」诉求脱节。用户要求把 Z-Scan 主功能搬到主 GUI 一级界面、对话框页整页移除，并让 Z-Scan 与 SIM9 采集联动。
- 影响：
  ① **可复用纯函数下沉**：Z-Scan「仅移动」与「完整自动对焦」核心抽到 `sim_control/z_scan_core.py` 的 `run_z_scan_stage_only` / `run_z_scan_autofocus`（共享 `preflight_z_scan_positions` 做移动前全量越界预检），供主 GUI 后台 worker 复用；正式采集 worker 的 z-scan 分支仍独立、仅共享底层 `run_z_scan`。后续 Z-Scan 相关改动优先走 `z_scan_core.py`，不要再把编排逻辑写进 GUI。② **对话框不再承载 Z-Scan**：`SimSettingsDialog` 移除 `tab_zscan` 与全部 z-scan 代码，`_sync_config_from_widgets` 不再写 `config.z_scan`（z_scan 仅由主 GUI 编辑、对话框保存时原样保留）；改 UI 仍须改 `.ui` 后 `pyuic5` 重生成。③ **主 GUI 为 Z-Scan 唯一交互入口**：`control_wangbo/main.py` 运行期编程式建 Z-Scan 模块（方向/步进 nm/步数/曝光/「是否开启」开关/「拍图」开关/「运行」按钮），后台经 `_ConnectWorker`+QThread 执行、进度经 pyqtSignal 回主线程；「运行 Z-Scan」拍图 OFF=仅移动停终点、ON=完整对焦移到最佳焦面；移动越界预检在发起任何移动前 raise。④ **SIM9 联动**：「SIM9帧采集」的 `z_scan_enabled` 改读 `z_scan.enabled`（沿用 2026-05-25 的 enabled 语义）——ON 先完整 Z-Scan 选焦面再采 9 帧、OFF 直接采。⑤ **API 约束记录**：camera/SLM 适配器 `is_connected()` 是方法、stage 的 `is_connected` 是布尔属性，z-scan 连接校验须区分调用形式。本决策不改既有 z-scan RO/波形/时序约束（5/8/14/20ms preset、488 三相位、FINISH 语义等）。

## 2026-06-24

### 决策：R11 找样品 immediate RO 的实现约束（DC 平衡成对 + 序列上限 12 + 激活类型由 MetroCon GUI 设）
- 原因：
  为“实时找样品”（相机 free-run、不发 DAQ 触发，需 SLM 持续出图让光透过 MASK）需要 immediate 激活 + 循环显示的 RO。把 `488-3.5mm-9frame-tset.repz11` 的 RO 脚本合并进 `2d_3.5.repz11` 时 MetroCon 报 `sequence number (16) out of range`，深入 SDK（`AN0028BA Introduction to the R11 System`、`QXGA Sequence Catalogue`、各序列 timing report）后厘清了 R11 repertoire 三条硬约束。
- 影响：
  ① **R11 序列上限**：序列槽 0-15，叠加 `2d_3.5.repz11` zip 内 ~20 个 orphan `.seq11`，实际加第 13 个 SEQUENCES 别名一被 RO 引用即撞上限——找样品/预览 RO 必须**复用现有 12 个别名，绝不加新序列**（与 2026-05-20 z-scan “不用 H±” 同源）。② **DC 平衡硬约束**：R11 FLC 微显示净 DC 必须=0、否则物理损坏液晶（`AN0028BA` §3.3.1.2 p.14）；`48030 Lit Pair` 的 `+`/`-` 是同一图案的正/反相（Data `b0` // `/b0`，属 “Deferred Balanced”），**必须在 RO 里成对 `(A+,n) (A-,n)`**、绝不能单独 `(A+,n)` 长期 loop；`Lit Balanced`（48088 等）是自平衡序列、可单帧，但加它会撞序列上限。③ **激活类型由 MetroCon GUI 设、编译进 `.repc`**：`.rep` 文本的 `[HWA ]`(无 h) 在官方 HWRO Example 里其实是 hardware-select（配 `RO_SELECT`、占 SPI 破坏 TRIGGER/FINISH），脚本改 `.rep` 控制不了 immediate；loop 用 `.rep` 多帧列表表达。④ **落地**：`tools/add_preview_ro.py` 只在 `.rep` 追加 RO 骨架（复用 `A+`/`A-`=48030 成对、引用现有 488 图案全局 9-17、`[HWA ]` 骨架、不加序列/图、token 间带空格 + 结尾 ` >`），用户在 MetroCon GUI 把激活类型设 immediate 后编译烧录。后续找样品/预览 RO 相关改动遵循这些约束；正式采集的 `[HWA h]`+FINISH（SPI_1/SPI_2）语义不受影响、不得引入占 SPI 的 `RO_SELECT`。

### 决策：引入 Codex 双循环审查工作流作为代码修改强制流程
- 原因：
  用户要求把“Claude 出方案 → codex 审查方案 → 改 → 循环到通过 → 执行 → codex 验证实现 → 改 → 循环到通过”的双循环固化为长期规则，且明确要求用 OpenAI 官方 Codex 插件实现。本机已通过 `npm install -g @openai/codex` 装好 PATH 版 `codex-cli 0.142.0`（复用 `~/.codex` 现有 ChatGPT 登录；正式版支持 `service_tier="priority"`，无需改 `~/.codex/config.toml`），官方 Codex 插件（marketplace `openai-codex` / plugin `codex`）`setup` 复检 `ready:true`，具备以 `/codex:rescue`（方案审查）与 `/codex:review`（实现验证）做外部交叉审查的条件。
- 影响：
  `AGENTS.md` 新增“Codex 双循环审查工作流（代码修改强制流程）”节：对 `sim_control/` 源码、`config/*.json`、`tests/`、波形/时序、重新生成的 `ui_*` 等实质性修改必须走双循环；纯记忆文件回写与文档微调可豁免双循环但不豁免 subagent review。通过判定以 codex 的 blocking 项清零为准（codex 按 blocking/non-blocking/待确认三类输出），各循环默认上限 5 轮、到限交用户裁决；codex 全程只读、所有修改由 Claude 执行；diff 审查范围限定为本次任务对照基线的改动；codex 误判可附理由申辩复审一次；用户显式授权或 codex 不可用经确认时可降级并记入摘要；stop-time review gate 默认不启用。本规则与既有“修改后审查机制”（subagent review）叠加，任一存在 blocking 都不算完成。该工作流规则本身已用 codex 跑两轮方案审查定稿（v1→18 点意见，v2→0 blocking）。
  （2026-06-24 同日细化）**codex 会话连续性**：同一任务所有审查在同一 codex 会话内（首次 `/codex:rescue --fresh`、后续每轮 `--resume`），不同任务各自 `--fresh`；第二循环主审查从 `/codex:review` 改为 `/codex:rescue --resume`（让 codex 对照它已批准的方案、基于 `git diff <基线 ref>` 审实现），`/codex:review --base` 降为可选独立复审。**限制**：本插件 `--resume` 实为 `task --resume-last`（只续“最近一次”codex 调用、不能按任务 id 寻址），故必须串行处理任务、不在同一 Claude 会话交错多任务，否则 `--resume` 会接错会话——此点由独立 subagent review 抓出（连同节内残留旧句“实现验证统一用 `/codex:review`”的自相矛盾）并已修正。（同日再细化）**承载方式**：同一任务的 codex review（不论第几轮）必须由主 agent 在主对话用 `/codex:rescue` 串行承载，**不得**用 Workflow/并行/后台 spawn 跑需要多轮续接的 codex review（会另开独立会话、`--resume-last` 接不回主线；reconstruction 验证曾误用 Workflow 跑 codex、仅因 0 blocking 单轮未暴露），并行多视角只用 Claude subagent 旁路。
  （2026-06-25 澄清）**触发主体限定为 Claude Code**：本工作流只在 Claude Code 对本工作区做实质性代码/配置修改、或产出方案时触发，由 codex 外部审查；用户直接用 Codex 或其他 AI 工具**直接**改代码时**不触发本工作流、不强制 codex 自审**（codex 自审自己的方案会丧失“外部交叉”的独立性）。原 `AGENTS.md` 标题“代码修改强制流程”与适用范围未限定主体，导致 Codex 读 `AGENTS.md` 时把双循环套到自己头上自审方案——本次在标题、节首新增“触发主体”条、适用范围三处补齐 Claude Code 主体限定予以修正。

### 决策：新增 immediate-RO 实时找样品链路，激光安全以"先确认预览再点灯 + set_line 整 port 互锁"为硬约束；采集波长唯一入口迁到主 GUI
- 原因：
  正式 SIM9 用的 `[HWA h]` RO 是外触发，实时预览（相机 free-run、不发 DAQ 触发）下 SLM 不出图、画面黑，操作者无法据图调样品位置。需要一条"用 immediate（软件激活即显示）RO + 持续开激光"的找样品链路集成进主界面，替代以前手动换 repertoire + 厂商相机软件的土办法。同时把采集波长从设置弹窗 Laser 页挪到主界面便于操作。关键硬件约束：`NIDaqAdapter.set_line()` 是**整 port0 写入**，拉高一路会把其余所有 SIM TTL 写 0，故找样品激光绝不能与正式采集/诊断波形并存。
- 影响：
  immediate RO 识别**只认硬件激活类型 `ACT_IMMEDIATE`(0x01)**（R11 `rpc.h` 枚举：IMMEDIATE=0x01/SOFTWARE=0x02/HARDWARE=0x04），`activation_type=None` fail-closed（绝不按名字猜）；正式 RO 选择 `find_best_running_order` 经 `exclude_indices` 显式排除 immediate（且须经 **worker payload** 与 `select_running_order_for_task` 两条路径都传入）。激光安全状态机长期约束：①激光只在收到 `preview_started`（真正出帧）后点亮，**绝不用早置位的 `sim_preview_active`/`controller.active` 作凭据**，未确认走两阶段 pending（`QTimer.singleShot` 捕获 token、bump-token 失配忽略、点灯前再验相机已连+下拉仍选同 RO）；②`activate_immediate_running_order` **先记 arming line 再 set_line 高**，任何失败 best-effort `set_all_low` 后清态；③`stop_immediate_live` 未激活严格 no-op（不写整 port），并在停 Live/开 SIM9/断 SLM/换波长/开设置弹窗/关窗口/异常/preview error 全部入口强制调用；④`preview_stopped` 区分 planned restart（ROI/曝光内部重启保激光 + reconfirm 看门狗）与异常停止（关激光）；⑤连接前 best-effort 冷启动 `set_all_low`。后续任何改动不得：把激光点亮凭据退回 `sim_preview_active`；在采集/诊断波形期间调 `set_line`/`set_all_low` 之外的整 port 写；恢复设置弹窗 Laser 页作为采集波长入口（采集波长唯一主入口为主 GUI `cmb_sCMOS_laser`，弹窗 Recon 波长下拉仅配 OTF、不回写 `selected_laser_nm`）。immediate RO 命名须用前导波长且不撞正式正则（如 `488_3.5_2d_live_imm`），与正式 `{wl}_{pitch}_{mode}_{exp}ms` RO 同存于 `patterns/2d_3.5.repz11`。immediate-live 运行态只作 controller/GUI 私有，不进 `AppConfig`/JSON（除非将来加 `allow_immediate_live` 配置项）。真机仍须确认 `ACT_IMMEDIATE` 选中是否即显/是否需 `R11_RpcRoActivate`/是否真不需 EXT_RUN（若需则改"一次写多路组合 mask"静态写）。

## 2026-06-22

### 决策：默认 DAQ 接线改为 `Dev1` 新线位，并将第四路红光统一迁移为 `638 nm`
- 原因：
  当前实际 Oxxius L4CC 红光通道标称为 638 nm，现场 DAQ Wiring 也已按 `Dev1/port0/line12` 连接红光数字输入；继续使用旧 `647` 命名和旧 `0/1/2/5/8/6/7/9` 线位会让 GUI、配置、波形输出、重建参数与真实硬件不一致。
- 影响：
  `config_version` 升到 v12；`DaqLineConfig` 使用 `laser_638_line`，默认线位固定为 `slm_enable=0`、`slm_trigger=1`、`slm_finish=2`、`camera_trigger=8`、`laser_405=9`、`laser_488=10`、`laser_561=11`、`laser_638=12`；`SUPPORTED_LASERS=(405,488,561,638)`，`LASER_ROLE_MAP[638]="laser_638_line"`；`ReconstructionConfig` 使用 `otf_638_path` 与 `estimated_params_638_path`。配置迁移必须把旧 `laser_640_line`/`laser_647_line`、`selected_laser_nm=640/647`、`otf_640_path`/`otf_647_path`、`estimated_params_640_path`/`estimated_params_647_path` 统一迁移到 638 字段。GUI、摘要、DAQ 测试、Recon 下拉和配置文件对外显示 `638 nm` / `Laser 638`。SLM Running Order 选择对 638 nm 优先匹配 `638` RO；若现场 repertoire 仍只有旧 `647` 命名，638 请求允许 fallback 到 `647` RO，避免未重命名文件时采集失败。

## 2026-06-16

### 决策：SIM9 GPU Wiener 重建稳定接口从 `reconstruction.sim_wiener` 迁移到 `sim_control/sim_reconstruction.py`，并改为进程内常驻"热"重建
- 原因：
  队友交付的新版 GPU 重建（`reconstruction/` 下 4 个 .py）把 `reconstruction/sim_wiener.py` 变成 import 即执行整段重建、含硬编码 `F:\` 路径的计时 demo，不能再作为库被 pipeline import。同时实测显示"第一次调用慢、第二次快"，根因是进程级一次性 GPU 预热（CUDA context、cuFFT plan 按 shape+dtype 缓存、cuSOLVER、PyTorch caching-allocator、torch/EMD import），这些缓存在进程内跨调用、跨新建 reconstructor 实例都保留，仅进程退出丢失。项目流程是"捕获一个细胞→拍9帧→重建一次→等下一个细胞"，若每次新起进程或每次新建实例就只能拿冷速度；只要重建跑在常驻 GUI 进程并复用同一引擎实例，启动后只有第一个细胞冷、其余皆热。
- 影响：
  新增 `sim_control/sim_reconstruction.py` 作为项目内**唯一**依赖重建引擎私有方法的集成层，只 import 引擎模块 `sim_wiener_gpu_emdapp_batchInGroup_batchBetGroup`、绝不 import demo `sim_wiener.py`；`_load_backend` 幂等并校验后端 API。`WarmSIMReconstructor` 进程内常驻、跨细胞复用同一 `InMemorySIMWienerReconstructor`（自复刻引擎流程、不调引擎 `reconstruct()`、不写 `.mat`、内存注入 `(9,H,W) uint16`），保留 cuFFT plan/allocator/网格缓存，自动按首帧形状预热；`ReconstructionWorker` 持有该热实例，并改为**先 emit 结果再异步落盘**（有界单 writer 线程；落盘失败非致命，只经 `signal_reconstruction_saved` 通知，不回滚已下发结果），避免磁盘 I/O 拖慢决策回传。新增 saved-params 快路径（~45ms）与 estimate 路径（~287ms）双路可切换，`saved_params_fallback=fail|estimate`。`reconstruction/*.py` 由本项目侧保持不修改（引擎为队友交付，PROJECT_MEMORY 旧"保持不改"表述更新为"队友更新版、本项目侧不改"）。后续 SIM 重建相关改动一律走 `sim_control/sim_reconstruction.py`，不要恢复从 `reconstruction.sim_wiener` import 稳定接口。

### 决策：重建配置升 schema v11，生产默认 saved-params 但仓库默认 estimate（仿真优先），缺 `.mat` 不静默降级
- 原因：
  saved-params（~45ms）依赖固定 SLM Running Order + 光路 + 每波长标定 `.mat`，是真实实验现场的快路径；estimate（~287ms）稳健、无需标定，适合仿真/开发/标定/质量复核。两条路径速度差是实时分选的关键，需可配置切换。但"仿真优先"是项目硬约束：仓库默认配置必须无 `.mat` 也能启动与重建。
- 影响：
  `ReconstructionConfig` 新增 `use_saved_params`/`saved_params_fallback`/`estimated_params_{405,488,561,647}_path`/`save_reconstruction_output`/`async_save_reconstruction_output`/`save_queue_maxsize`，`config_version` 10→11，`_migrate_v10_to_v11` setdefault 新字段且旧配置无任何 `estimated_params_*_path` 时强制 `use_saved_params=false`（升级不破坏重建）。`validate_app_config` 在 `use_saved_params=true` 且所选波长 `.mat` 缺失/不存在时报错并阻止正式采集（不静默回退）。仓库 `config/sim_control_config.json` 默认 `use_saved_params=false`；现场生产档为单独的 `config/sim_control_config.production.example.json`（saved+fail），用法见 `docs/reconstruction_saved_params.md`。后续如调整默认值或迁移逻辑，必须同时维持"仓库/仿真默认可无 `.mat` 启动"这一约束。

## 2026-06-11

### 决策：preview stop(wait=True) 弃用 processEvents 泵循环，改为 worker 侧 threading.Event 等待 + generation 代数过滤
- 原因：
  此前 `SimPreviewController.stop(wait=True)` 用 `QCoreApplication.processEvents(ExcludeUserInputEvents)` 循环等待 worker 的 `preview_stopped` queued 信号，本质是 GUI 线程内的事件循环重入，只是"缓解"而非根除（外部 Codex 审查 #8 持续标记）。直接换成 threading.Event 等待会引入新竞态：等待返回后调用方立即重启预览时，上一轮迟到的 `preview_stopped` 会把新一轮 active 状态错误清零。
- 影响：
  `SimPreviewWorker` 新增 `_stopped_event`（slot_start 的 finally 先 emit stopped 再 set，保证等待方醒来时信号已入队）与 `_generation` 代数（slot_start 入口捕获为局部变量，盖在 started/stopped payload 上）；controller `start()` 时代数 +1 经 `prepare_for_start(generation)` 写入，`_handle_worker_status` 丢弃携带旧代数的陈旧信号、对外转发前剥掉 `generation` 键（外部 payload 合同不变，`control_wangbo/main.py` 订阅者无需改动）。`stop(wait=True)` 改为 `wait_until_stopped(2.0)` + 本地收尾。约束：worker 状态信号必须只经 `_handle_worker_status` 过滤后转发，不得恢复 worker→外部的直连；新增预览状态 payload 键时注意 `generation` 为 controller 内部键，外部不可依赖。

### 决策：诊断测试与硬件按钮全面 worker 化 + stop_event 全链路贯通；DCAM 硬件时间戳作为新增字段而非替换
- 原因：
  Codex 审查 #1/#6 持续指出：Z-Scan 仅位移台测试、相机触发/激光脉冲测试无取消检查点，standalone GUI 的 Initialize Camera / Program Patterns 仍在 GUI 线程同步调用可阻塞数秒的 DCAM/SLM 操作。#24 指出 `read_frame_sequence` 返回的时间戳是软件消费时刻 `time.time()`，不能代表曝光时刻，但直接替换会破坏现有合同。
- 影响：
  `pulse_line`（protocol/真实/仿真三处）增加可选 `stop_event`，`adapters._interruptible_sleep` 在 `stop_event=None` 时退化为单次 sleep（行为零变化），提供时分片倒计时、取消即提前拉低；`_run_zscan_stage_only_test`/`_run_camera_trigger_test`/`_run_laser_pulse_test` 增加取消检查点；`SimControlWindow._initialize_camera`/`_program_patterns` 改 `_PulseTestWorker` 模式（GUI 线程快照配置，worker 不读 Qt 控件）。DCAM 路径优先 `buf_getframe` 一次取像素 + `DCAMBUF_FRAME.timestamp`，经新方法 `get_last_hardware_timestamps()` 暴露（缺方法/缺字段/整组全 0 均置 None），返回值软件时间戳语义不变。约束：今后新增诊断测试一律走 `_PulseTestWorker` 模式，不得在 GUI 线程同步调用硬件；取消语义统一为"stop_event 置位 → 检查点抛错/提前返回 → worker 静默吞异常 → finally 安全收尾（DAQ 拉低/回起始层）"。

### 决策：配置 schema 升到 v10——Ti2 SDK 路径配置化，并加 config_version 写盘防御
- 原因：
  Ti2 ZDrive 的 DLL / SDK wrapper 路径此前由 `Ti2ZStageAdapter` 内部硬编码推导，与 AGENTS.md「SDK 定位不要硬编码，优先走配置」不一致；而 `Ti2ZStageAdapter` 构造函数早已支持 `dll_path`/`sdk_module_path` 参数，缺的只是配置接线。另外 `models.AppConfig.config_version` 默认值（8）与 `config_store.CURRENT_CONFIG_VERSION`（9）长期不一致：新建默认配置写盘会标过期版本号、每次加载重跑迁移，未来任何非幂等迁移会误作用于「全新默认配置」。
- 影响：
  `BackendConfig` 新增 `ti2_dll_path`/`ti2_sdk_module_path`（默认空串=沿用 adapter 内部默认推导，保证默认行为不变），`adapter_factory.create_stage_adapter_for_backend` 以「路径 or None」透传；schema 升 v10，`_migrate_v9_to_v10` 对旧配置 `setdefault` 两个空串键（幂等、不覆盖用户已填）。`models.AppConfig.config_version` 默认改 10 并与 `CURRENT_CONFIG_VERSION` 钉死一致（`test_legacy_sim_config_migration` 覆盖），`app_config_to_dict` 写盘前强制 `config_version=CURRENT_CONFIG_VERSION` 作为第二层防御。后续新增 schema 字段仍须同时升 `CURRENT_CONFIG_VERSION`、加迁移函数并同步 `models` 默认值。

### 决策：HardwareError 抽到 `sim_control/errors.py`，真实与仿真适配器共用
- 原因：
  仿真适配器需要与真实适配器抛同一种硬件错误类型，让「SLM/相机未连接被拒」等回归在仿真模式下也能被测出，并让 preview/controller 的 `except HardwareError` 分支在仿真与真实两种模式走同一路径。但若让仿真层直接 `from .adapters import HardwareError`，会使仿真路径反向依赖 1700+ 行的真实适配器模块（连带其 ctypes / SDK import 副作用）。
- 影响：
  `HardwareError(RuntimeError)` 定义移到 `sim_control/errors.py`；`adapters.py` 改为 import 并 re-export（既有 `from sim_control.adapters import HardwareError` 调用点不破）。仿真适配器统一从 `errors` 抛 `HardwareError`；`SimulatedCameraAdapter`/`SimulatedSlmAdapter` 新增 `strict_connection`（默认 False 保持自动连接便利，True 时未连接抛 HardwareError，供测试验证拒绝语义）。`HardwareError` 继承 `RuntimeError`，既有 `except RuntimeError` / `assertRaises(RuntimeError)` 仍命中。约束：`read_frame_sequence` 的取消信号仍须是裸 `RuntimeError("Acquisition cancelled.")`，由 `acquisition_core` 归一化为 `AcquisitionCancelled`，不得改成 `HardwareError`，否则破坏取消语义。

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

### 决策：NI USB-6423 在项目内固定采用 `0/1/2/5/8/6/7/9` 线位，并将第四路红光统一命名为 `647`（已由 2026-06-22 决策取代）
- 原因：
  当前实际接线已明确为 `slm_enable=0`、`slm_trigger=1`、`slm_finish=2`、`camera_trigger=5`、`405=8`、`488=6`、`561=7`、`647=9`；继续保留原先连续 `0..7` 的默认映射和 `640` 命名会造成 GUI 默认值、配置文件和真实硬件之间的偏差。
- 影响：
  `sim_control` 默认配置、GUI 默认回退、波形相关测试和摘要输出均以该映射为准；项目内公共配置 schema 使用 `laser_647_line` 与 `selected_laser_nm=647`，但加载旧 `laser_640_line` / `640` 配置时需要自动迁移以保持兼容。

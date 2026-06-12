---
description: SIM 代码审查报告落地修复全流程：逐条分流问题→保功能修复→跑测试→独立 subagent 复审→同步 PROJECT_MEMORY；不改王波功能、真机/GPU 项不在开发机臆改
argument-hint: "[审查报告路径 | 聚焦项，如 #1 worker化 | 仅复审 | 仅同步记忆]"
allowed-tools: Read, Edit, Write, Grep, Glob, Bash, Task, AskUserQuestion
---

## 本命令的用途

把"拿到代码审查报告之后，逐条落地修复并走完项目强制收尾"这件反复做的大任务固化下来。`sim-code-review`（及 codex `/review`、`/adversarial-review`）只产出**审查报告**、不改代码；本命令是它的**下游另一半**：读报告 → 分流 → 改 → 测 → 独立 subagent 复审 → 回写记忆。

这是 `AGENTS.md` 项目规则的执行流程入口。项目背景、硬件路线、目录边界和长期约束只以 `AGENTS.md` 为唯一主指令来源；本命令只描述"审查报告如何落地修复"的流程，不复制维护项目规则。

**与既有 `sim-autosystem-change-closure` 的分工**：那个 Codex 侧 skill（`.agents/skills/sim-autosystem-change-closure/`）固化的是改动**收尾**清单（验证 → 三问式 subagent review → 记忆回写）。本命令聚焦它不覆盖的**上游半边**——把一份审查报告逐条分流并保功能落地修复；下方"测试 / subagent 复审 / 记忆同步"三步与该收尾清单同义，可直接复用或交给它，不重复另造一套收尾机制。历史上这条链路反复跑、且因为没有固定 playbook 多次撑爆上下文反复重述约束（2026-06-11 会话 `e578e58e`：读 `python-pyqt5-stateful-balloon.md` 报告 → 分流 17 项 → 多轮"还有问题请继续修改" → codex 复审又抛新问题；`c501a6e2`：按 SDK 证据核对并修正 `adapters.py:1504` 注释）。

本次输入/聚焦：**$ARGUMENTS**

## 输入（唯一事实来源）

- **审查报告**：用户给的路径（常在 `C:\Users\user\.claude\plans\`）、`sim-code-review` 的输出、codex 复审输出，或消息里直接粘贴的问题清单。**先把每一条编号问题读全**，不要只看摘要。
- **当前改动**：`git status --short` + `git diff` 看清这一轮已经动了什么，避免重复改或回退别人的在途改动。
- 进入修复前必读：`AGENTS.md`、`PROJECT_MEMORY.md`、`docs/project_memory/decision_log.md`；其中 `AGENTS.md` 是项目规则唯一主入口。再按报告涉及的文件读源码与对应测试。

## 逐条分流模型（先分流，再动手）

对报告里每一条，给出归类 + **代码证据**（`文件:行号`），不要笼统照单全收：

1. **立即修**：纯实现/正确性/线程/资源问题，能在开发机改且不改变业务行为。→ 本轮修。
2. **真机验证项**：需要真硬件/真 GPU 才能确认的，**不在开发机臆改**，原样保留并列入收尾清单。典型：Ti2 线程安全、DAQ 页口径、RO 激活语义、shutdown 超时复现、SLM 时序。
3. **需用户决策**：涉及行为/接口/业务语义取舍，或归属不清（如某项该入哪一节）。→ 用 `AskUserQuestion` 一次性问清，不擅自决定。
4. **证据不足 / 报告有误**：报告与代码或 SDK 事实不符的，**驳回并说明证据**（例：报告称"SDK 直接返回 NumPy 视图"，但 DCAM 样例 `buf_getframe` 每帧 `dcammisc_alloc_ndarray`+`dcambuf_copyframe` 独立分配——应改注释而非"修复"不存在的问题）。

## 执行步骤

1. **读上下文 + 读全报告**（见上）。确认本轮是"落地修复""仅复审已改代码"还是"仅同步记忆"。
2. **分流**：按上面四类逐条归类，每条带 `文件:行号` 证据；归属不清或涉及行为变更的，先 `AskUserQuestion` 问清。
3. **保功能修复**：只改"立即修"项，最小改动；遵守下方护栏。每条改完自己记下"改了什么/为什么/碰了哪些文件"，便于复审。
4. **跑测试**：`.\.venv\Scripts\python.exe -m unittest discover -s tests -q`（以本地实跑 `Ran N tests ... OK` 为准，N 随分支增长、不要焊死数字；字体/DPI 用例两套环境可能不同，对照 `PROJECT_MEMORY`/`dual-workspace-layout` 判断是否环境性失败）。失败先修到绿或解释清楚再继续。
5. **独立 subagent 复审（强制）**：派一个 subagent 只审本轮 diff，**让它把完整复审报告 Write 到指定文件（如 `C:\Users\user\.claude\plans\sim-apply-review-<topic>.md`），最终消息只回一行路径**，主 agent 再 Read 取回（本环境 subagent 最终消息常被吞、`SendMessage` 不可用）。复审要回答：标准是否满足/证据、哪些未满足/原因、哪些需人工或真机确认。
6. **据复审结果收口**：能立即处理的当轮处理；处理不了的进收尾清单。必要时把 diff 再交 codex `/review` 或 `/adversarial-review` 做对抗复审。
7. **同步记忆（强制收尾）**：更新 `PROJECT_MEMORY.md` 的"最近更新"（如影响状态/边界/接口/依赖/硬件接入/待办/风险，同步对应章节）；**仅当**关键设计决策/长期约束/方向变化时才动 `docs/project_memory/decision_log.md`。
8. **最终回复**：单列"需用户人工确认 / 需真机确认 / 需性能基准"清单，并说明测试与复审结论。

## 护栏（来自历史会话与跨会话记忆的真实纠正）

- **项目边界不在本命令复制维护**：每次运行都从 `AGENTS.md` 读取并遵守当前有效的目录边界、硬件路线、实时路径、GUI 线程、配置驱动、仿真优先和编码风格要求；若本命令文字与 `AGENTS.md` 冲突，以 `AGENTS.md` 为准。
- **真机/GPU 项不臆改**：需要真实硬件、GPU 环境或业务判断的问题只列为确认项，不在开发机凭推测改动。
- **编码风格**：类型注解用 `X | None`（不新增 `Optional[X]`）；新 Python 文件 `from __future__ import annotations`；硬件适配集中在 `sim_control/adapters.py`，不散落到 GUI。
- **最小改动**：不输出大段可直接替换的补丁；每条改动留可复核痕迹；不顺手做范围外重构。
- **复审落文件**：subagent 复审/结论一律写文件再读回，不依赖其最终返回文字。
- **收尾必回写记忆**：本轮无论改了代码/配置/文档/工作流，结束前必须更新 `PROJECT_MEMORY.md`。

## 完成判据（停止条件）

所有"立即修"项已修且测试通过（或失败已解释）、独立 subagent 复审无新增 P0/P1、真机/用户决策项已成清单、`PROJECT_MEMORY.md` 已同步——四者齐全方可收工。

---
*由 /reflect-skills 从重复会话提炼生成（SIM 项目 e578e58e 多轮落地修复 + c501a6e2 单项 SDK 核对修正；衔接 sim-code-review / codex review 的下游修复链；实现 AGENTS.md「修改后审查机制」+「跨对话记忆机制」）*

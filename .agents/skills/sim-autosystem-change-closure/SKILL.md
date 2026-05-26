---
name: sim-autosystem-change-closure
description: Use when finishing any SIM_AutoSystem code, config, documentation, test, workflow, or local project-instruction change that needs verification, independent review, project-memory synchronization, or a final handoff.
---

# SIM_AutoSystem Change Closure

## Overview

Use this as the project-specific exit checklist after a change. It keeps verification evidence, independent review, and project memory updates consistent across SIM GUI, acquisition, hardware adapter, reconstruction, Z-Scan, configuration, and documentation work.

## Workflow

1. Re-read the project context if it is not fresh: `AGENTS.md`, `PROJECT_MEMORY.md`, and `docs/project_memory/decision_log.md`.
2. Inspect the current diff and separate your changes from pre-existing user or generated changes. Use the working tree diff for uncommitted changes; use a saved pre-change SHA when available; use `origin/dev..HEAD` only when the task is specifically about pushed branch contents.
3. Map the changed files to project constraints:
   - `.ui` changes must regenerate matching `ui_*.py`.
   - GUI/threading changes must preserve PyQt5, QThread/pyqtSignal, and latest-frame-wins boundaries.
   - Acquisition, DAQ, camera, SLM, ZDrive, and reconstruction changes must preserve simulation-mode paths.
   - Real-time acquisition paths must not add synchronous disk I/O.
   - Raw SIM9 `(9, H, W) uint16` stacks must not be routed through GUI status paths.
   - `control_wangbo/` remains read-only unless the task explicitly requires an integration entry change.
4. Run fresh verification appropriate to the change. Prefer focused tests first, then broader tests when shared behavior changed.
5. Update `PROJECT_MEMORY.md` whenever the project changed. Update `docs/project_memory/decision_log.md` only for new long-term decisions or constraints.
6. Dispatch an independent review subagent using `references/review-prompt.md`. Include changed files, relevant diff context, verification commands and results, and any known hardware/manual limitations. If a generic review skill is also active, override its headings with the three project-required review categories.
7. Fix actionable issues from the review, then re-run the relevant verification. If the review raises only manual or true-hardware confirmations, keep them for the final handoff.
8. Final response must include what changed, verification evidence, review outcome, and remaining manual/true-hardware/business confirmations.

## Verification Selection

Use the smallest command that proves the changed behavior, then broaden when risk crosses module boundaries.

| Change area | Focused checks |
|---|---|
| GUI layout, `.ui`, generated PyQt code | `.venv\Scripts\python.exe -m pytest tests\test_ui_regressions.py tests\test_sim_preview_restart.py -q` |
| Config schema or migration | `.venv\Scripts\python.exe -m pytest tests\test_legacy_sim_config_migration.py tests\test_z_scan_config_migration.py -q` |
| Summary text or GUI status payload | `.venv\Scripts\python.exe -m pytest tests\test_sim_summary.py tests\test_sim_preview_restart.py -q` |
| Z-Scan logic, timing history, focus behavior | `.venv\Scripts\python.exe -m pytest tests\test_z_scan_core.py tests\test_z_scan_timing_history.py -q` |
| Repertoire patching | `.venv\Scripts\python.exe -m pytest tests\test_zscan_repertoire_patch.py -q` |
| Shared acquisition/controller/reconstruction behavior | `.venv\Scripts\python.exe -m pytest tests\ -q` |
| Syntax/import confidence after broad Python edits | `.venv\Scripts\python.exe -m compileall -q app.py sim_control reconstruction control_wangbo tests tools` |

Always read the command output and exit code before claiming success.

## Memory Update Rules

`PROJECT_MEMORY.md` should record concise facts, not a transcript. Read and write project memory as UTF-8. Add a dated Chinese entry under the existing recent-updates heading with:

- Scope of the change.
- Important behavior or interface changes.
- Verification commands and pass/fail status.
- Review result if the change required review.
- Manual, true-hardware, or business confirmations that remain.

Update `docs/project_memory/decision_log.md` only when the change establishes a durable policy, architecture boundary, hardware route, schema meaning, or long-term workflow rule.

Code, configuration, documentation, tests, tracked generated files, and workflow/instruction files require a memory review. Ignored runtime data such as acquisition outputs, timing JSONL, temporary caches, or generated local logs usually does not require memory updates unless the task changed their semantics.

## Review Handoff

Use `references/review-prompt.md` for the reviewer prompt. Keep the reviewer independent:

- Do not ask the reviewer to rubber-stamp your conclusion.
- Provide raw evidence: diff summary, changed paths, commands run, and known constraints.
- Require the three project categories exactly: satisfied standards/evidence, unmet standards/reasons, and manual/true-hardware/business confirmations.
- After review, fix what can be fixed locally before finalizing.

## Common Mistakes

| Mistake | Correction |
|---|---|
| Running tests but not updating `PROJECT_MEMORY.md` | Update memory before final handoff whenever project files changed. |
| Treating a generic code review as satisfying AGENTS.md | The review must answer the three SIM_AutoSystem categories. |
| Forgetting `.ui -> ui_*.py` regeneration | Regenerate and include tests for geometry or object-name expectations. |
| Reporting hardware-dependent behavior as complete | Mark it as software-verified and list true-machine confirmation separately. |
| Letting review receive raw stack data or generated runtime outputs unnecessarily | Pass paths, diffs, summaries, and test logs; avoid large runtime data unless needed. |

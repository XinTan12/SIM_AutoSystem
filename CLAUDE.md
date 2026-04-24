# CLAUDE.md — SIM_AutoSystem

## 项目概述

微流控细胞分选 + 结构光照明显微成像（SIM）联动的自动化系统。核心链路：

```
微流控捕获 → SIM 9帧结构光采集 → (9, H, W) uint16 图像栈 → 重建/特征分析 → 决策回传 release/sort
```

硬件：Hamamatsu ORCA-Fusion BT 相机 (DCAM-SDK4)、Kopin QXGA-R11-STR SLM (R11CommLib)、NI USB-6423 DAQ。

## 常用命令

```bash
# 启动 GUI
.venv/Scripts/python.exe sim_control_app.py --config config/sim_control_config.json

# 运行测试
.venv/Scripts/python.exe -m pytest tests/ -q

# 从 .ui 重新生成 Python 代码
cd sim_control && pyuic5 sim_settings_dialog.ui -o ui_sim_settings_dialog.py
```

## 目录边界

- **`sim_control/`** — SIM 侧主开发区域。新功能放这里或仓库根目录新增模块。
- **`control_wangbo/`** — 队友历史微流控控制代码，**默认只读**，除非用户明确要求修改。
- **`config/sim_control_config.json`** — 默认配置文件，承载设备、时序、后端和路径配置。
- **`SDK/`** — 本地厂商 SDK/DLL，不纳入版本管理（`.gitignore` 已排除）。
- **`tests/`** — 回归测试。修改 `sim_control/` 后确认已有测试不回退。

## 架构分层

```
models.py          数据类定义（AppConfig, DaqLineConfig, CameraConfig, TimingConfig 等）
config_store.py    JSON 配置读写、旧配置迁移
adapters.py        硬件适配器（FusionBtCameraAdapter, KopinSlmAdapter, NIDaqAdapter）
waveform.py        NI DAQ 波形生成与打包
controller.py      采集控制器（QThread worker 模式）
preview.py         live 预览（线程安全最新帧快照 + QTimer 主线程轮询）
pipeline.py        占位重建/特征/决策 pipeline
gui.py             SimControlWindow + SimSettingsDialog
summary.py         SIM 设置摘要文本生成
sim_camera_presets.py  相机 ROI 预设与对齐工具
ui_sim_settings_dialog.py  pyuic5 生成代码，勿手动修改
```

## 关键约束

- **配置驱动**：设备路径、TTL 线位、SDK 定位不硬编码在代码中，走 `config/sim_control_config.json`。
- **实时路径无同步 I/O**：采集链路（`controller.py` → `adapters.py`）禁止同步磁盘读写。
- **仿真优先**：默认支持无真实硬件时的仿真模式验证 UI 和控制流。
- **DAQ 线位映射**：NI USB-6423 规范接线固定为 `slm_enable=0, slm_trigger=1, slm_finish=2, camera_trigger=5, laser_405=8, laser_488=6, laser_561=7, laser_647=9`。
- **激光命名**：第四路红光统一为 `647`（非 `640`），旧配置有自动迁移。
- **预览模式**：latest-frame-wins，允许丢弃中间帧，避免旧帧积压。
- **`ui_sim_settings_dialog.py`** 由 `pyuic5` 从 `.ui` 文件生成，修改 UI 应编辑 `.ui` 文件后重新生成。

## 编码风格

- Python 3.12+，使用 `from __future__ import annotations`。
- 类型注解使用 `X | None` 风格（非 `Optional[X]`）。
- GUI 框架：PyQt5。
- 硬件适配逻辑集中在 `sim_control/adapters.py`，不散落到 GUI 代码中。
- 测试使用 `unittest`（部分文件使用 `pytest` 风格断言）。

## 跨对话记忆

- 每次新对话开始时，先读取 `AGENTS.md`、`PROJECT_MEMORY.md`。
- 本次会话若修改了项目，结束前同步更新 `PROJECT_MEMORY.md` 的"最近更新"部分。
- `PROJECT_MEMORY.md` 记录提炼后的事实和待办，不保存聊天记录。

## 环境

- Windows 10/11，统一使用根目录 `.venv` 作为 Python 环境。
- 不依赖 `control_wangbo/.venv`。
- 主要依赖：`numpy`, `PyQt5`, `nidaqmx`, `tifffile`, `opencv-python`, `pandas`, `matplotlib`。

# SIM_AutoSystem

## 项目简介

`SIM_AutoSystem` 是一个面向微流控分选与结构光照明显微成像（SIM）联动的自动化工程。

当前仓库的重点是将以下链路逐步打通：

1. 微流控系统捕获目标细胞
2. SIM 侧完成 9 帧结构光采集
3. 输出 `(9, H, W)` 形状的 `numpy.uint16` 图像栈
4. 将图像栈交给重建与特征分析模块
5. 根据分析结果把决策返回给微流控系统执行 `release/sort`

目前 `sim_control/` 已包含独立的 SIM 控制 GUI、波形生成、采集控制和硬件适配层；`control_wangbo/` 保留了队友的历史微流控控制代码，用于后续联调参考。

## 主要内容

本仓库当前主要包含以下内容：

- 项目集成 GUI 与独立 SIM 采集 GUI 启动入口
- 相机、SLM、DAQ 的适配器封装
- 结构光采集流程控制与波形计划
- 预览、占位重建与特征分析流程接口
- 与历史微流控代码对接所需的目录结构

项目默认优先支持仿真模式，便于在没有真实硬件时验证 UI、配置和控制流程；接入真实设备时，通过配置或 SDK 路径覆盖切换到真实硬件后端。

## 目录结构

- `sim_control/`
  SIM 控制主模块，包含 GUI、控制器、波形生成、预览、适配器和数据模型。
- `app.py`
  项目集成 GUI 启动脚本，启动 `control_wangbo/main.py` 的主界面。
- `sim_control/sim_acquisition_app.py`
  独立 SIM 采集 GUI 启动脚本，用于单独调试 SIM 采集链路。
- `config/sim_control_config.json`
  默认配置文件，包含 DAQ 线位、相机 ROI、时序、后端仿真开关和 SDK 路径配置。
- `start.cmd`
  Windows 下的项目集成 GUI 快速启动脚本。
- `tests/`
  基础回归测试与适配器测试。
- `SDK/`
  本地放置厂商 SDK、驱动和相关资料的目录。仓库默认不跟踪这些厂商文件，只保留说明文件。
- `control_wangbo/`
  历史微流控控制代码。除非明确需要，否则建议视为只读。

## 运行环境

推荐环境如下：

- Windows
- Python 3.12 左右的独立虚拟环境
- 根目录 `.venv` 作为当前项目统一环境

主要 Python 依赖见 [requirements.txt](requirements.txt)：

- `numpy`
- `PyQt5`
- `nidaqmx`
- `tifffile`
- `opencv-python`
- `pandas`
- `matplotlib`

建议在仓库根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

如果需要运行现有测试，可执行：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

## 推荐 Git 工作流

本项目当前使用两条长期分支：

- `main` / `origin/main`：稳定主线，用于保存已验证可运行版本。
- `dev` / `origin/dev`：当前大改动开发分支。

推荐本地目录分工：

```text
E:\Intelligent_SR\SIM_AutoSystem
  开发工作区，checkout 到 dev

E:\Intelligent_SR\SIM_AutoSystem-stable
  稳定版 worktree，detached 到 origin/main
  只用于运行远程稳定版、真机对照和回退参考
```

常用命令：

```powershell
# 创建/更新开发分支
git switch dev
git push -u origin dev

# 创建稳定版 worktree
git fetch origin
git worktree add --detach E:\Intelligent_SR\SIM_AutoSystem-stable origin/main

# GitHub PR 合并 dev 到 main 后，更新稳定版 worktree
git fetch origin
cd E:\Intelligent_SR\SIM_AutoSystem-stable
git switch --detach origin/main
```

大改动合入流程：

```text
dev 本地测试通过 -> push origin/dev -> GitHub PR -> merge 到 origin/main -> stable worktree 更新到 origin/main
```

## 快速启动

### 方式一：使用批处理脚本

```powershell
start.cmd
```

该脚本会直接使用根目录 `.venv\Scripts\python.exe` 启动项目集成 GUI。

### 方式二：手动启动

```powershell
.\.venv\Scripts\python.exe app.py
```

默认配置文件位于：

`config/sim_control_config.json`

### 独立 SIM 采集 GUI

```powershell
.\.venv\Scripts\python.exe -m sim_control.sim_acquisition_app --config config\sim_control_config.json
```

## 配置说明

默认配置文件包含以下几类信息：

- `daq`
  DAQ 设备名和各条 TTL 线位映射
- `camera`
  相机索引、标签、ROI、曝光和触发模式
- `timing`
  采样率、边沿脉冲宽度、帧间隔和 SLM 保护时间
- `backend`
  是否启用仿真模式，以及相机/SLM SDK 路径覆盖
- `pattern_files`
  9 帧结构光图案文件路径

当 `backend.fusion_bt_sdk_path` 和 `backend.slm_sdk_path` 为空时，程序会按仓库内约定的 `SDK/` 本地目录结构查找默认 SDK 位置。

## SDK 安装说明

### 总体原则

本仓库默认**不提交厂商 SDK、驱动和安装包**。请在每台本地 Windows 电脑上自行安装或放置所需 SDK，并保持 `SDK/` 目录结构与本文档一致，或者在配置文件中显式指定 SDK 路径。

更详细的本地目录约定见 [SDK/README.md](SDK/README.md)。

### 1. Hamamatsu ORCA-Fusion BT 相机

本项目使用 Hamamatsu 的 `DCAM-API` / `DCAM-SDK4` 作为相机控制基础。

官方入口：

- [Hamamatsu driver/software hub](https://www.hamamatsu.com/us/en/product/cameras/software/driver-software.html)
- [Hamamatsu DCAM-SDK4](https://www.hamamatsu.com/us/en/product/cameras/software/driver-software/dcam-sdk4.html)

代码默认查找的本地目录是：

`SDK/Hamamatsu_DCAMSDK4_v25056964/dcamsdk4/samples/python/`

该目录下至少需要有：

- `dcam.py`
- `dcamapi4.py`

如果你的 SDK 放在别的位置，可以在 `config/sim_control_config.json` 中设置：

- `backend.fusion_bt_sdk_path`

### 2. Kopin / Forth Dimension Displays QXGA-R11-STR SLM

本项目使用 `R11CommLib` 与 `QXGA-R11-STR` SLM 通信。

官方入口：

- [Kopin / FDD 入口页](https://www.kopin.com/forth-dimension-displays-redirect/)
- [Kopin 联系页](https://www.kopin.com/about/contact/)

`R11CommLib` 通常需要向厂商获取，不一定提供公开自助下载。若本地没有对应包，建议通过厂商联系页申请当前可用的 Windows 版本。

代码默认查找的本地 DLL 目录是：

`SDK/R11 CD Bundle Mar 2020/2020-03/Software/R11CommLib/R11CommLib-1.8.189.118/examples/msvc/lib/`

当前代码支持查找：

- `R11CommLib-1.8-x64.dll`
- `R11CommLib-1.8-x86.dll`

如果 DLL 放在别的位置，可以在 `config/sim_control_config.json` 中设置：

- `backend.slm_sdk_path`

### 3. NI USB-6423 与 NI-DAQmx

DAQ 侧使用 `NI USB-6423` 输出同步 TTL 信号，用于驱动：

- `slm_enable_line`
- `slm_trigger_line`
- `slm_finish_line`
- `camera_trigger_line`
- 各激光触发线

官方入口：

- [NI device drivers 下载页](https://www.ni.com/en/support/downloads/drivers/download.ni-device-drivers.html)
- [NI Python resources](https://www.ni.com/en/support/documentation/supplemental/16/python-resources-for-ni-hardware-and-software.html)

本地需要完成两部分：

1. 安装 `NI-DAQmx` 驱动
2. 在 Python 环境中安装 `nidaqmx` 包

## 当前集成目标

项目当前的近期目标是完成以下联调链路：

`control_wangbo` 微流控捕获 -> `sim_control` 执行 9 帧 SIM 采集 -> 输出 `(9, H, W)` 的 `numpy.uint16` stack -> 重建/特征分析 -> 决策回传微流控执行 `release/sort`

在这个过程中，建议优先保持：

- 配置驱动而非硬编码路径
- 采集实时链路避免同步磁盘 I/O
- 硬件适配逻辑集中在 `sim_control/adapters.py`

## 说明

- `control_wangbo/` 为历史代码目录，默认不建议直接修改
- `SDK/` 下的厂商文件应保留在本地，不纳入仓库版本管理
- 仓库中的默认配置更偏向开发与仿真验证，接入真实硬件时请按实际设备更新配置

# SIM9 重建：saved-params 快路径与预热说明

本说明面向真实实验现场，介绍如何让 SIM9 GPU Wiener 重建在每个细胞上跑出"热"速度，
以及如何启用 saved-params（标定参数复用）快路径。

## 为什么第一次慢、第二次快

第一次调用慢的开销不在重建算法本身，而是**进程级**一次性 GPU 预热：CUDA context 初始化、
cuFFT plan 构建（按 shape+dtype 缓存）、cuSOLVER 初始化、PyTorch caching-allocator 扩张、
`import torch` / EMD 模块首次加载。这些缓存在**进程内**保留，跨调用、跨"新建重建器实例"
都生效，只在进程退出时丢失。

因此关键是：**重建必须跑在长期存活的进程里**。项目 GUI 的 `ReconstructionWorker` 常驻一个
`QThread`，整个 GUI 生命周期不退出。配合常驻热重建器（`sim_control.sim_reconstruction.WarmSIMReconstructor`，
跨细胞复用同一个引擎实例），启动后只有第一个细胞冷、其余每个细胞都是热速度。

> 不要每个细胞都新起一个 python 进程（例如反复运行 `reconstruction/sim_wiener.py`），那样每次都吃
> 冷启动，还要再加上 import + CUDA context 的额外开销。

## 两条参数路径（可切换）

| 模式 | 配置 | 热速度（参考，依 GPU 而定） | 适用 |
|---|---|---|---|
| 每帧估计 estimate | `use_saved_params=false` | ~287ms | 仿真/开发/标定/质量复核；无需 `.mat`，开箱即用 |
| 标定复用 saved-params | `use_saved_params=true` | ~45ms | 真实实时分选；需固定 SLM RO + 光路 + 每波长 `.mat` |

saved-params 之所以成立：pattern 参数（条纹角度/相位/调制度）依赖照明（SLM Running Order）与
光路，而不是样本。固定装置 + 每波长一份标定 `.mat` 时复用是正确的（注意 c6 调制度可能随样本
轻微漂移，质量敏感时仍可回到 estimate 复核）。

## 启用 saved-params 的步骤

1. **标定生成 `.mat`**：当前阶段用引擎 estimate 模式离线产出每波长 `_estimated_params.mat`
   （例如运行引擎脚本对一组结构清晰的标定图重建；`reconstruction/image/` 下已有示例 `.mat`）。
   GUI 内置标定入口为后续阶段任务。
2. **填配置**：把 `config/sim_control_config.production.example.json` 复制为
   `config/sim_control_config.json`，按真机填好每波长 `otf_*_path` 与 `estimated_params_*_path`，
   设置 `use_saved_params=true`。
3. **校验**：保存配置时会校验——`use_saved_params=true` 且所选波长 `.mat` 缺失或不存在时，
   会报清晰错误并**阻止正式采集**（不静默降级）。

## `saved_params_fallback`

- `fail`（生产默认）：`.mat` 缺失或结构/长度不匹配时直接报错，避免悄悄变慢或参数来源不明。
- `estimate`：`.mat` 出问题时对该细胞回退每帧估计并记 warning；调试期可用。

每次重建的 `metadata` 会记录 `use_saved_params`、`params_path`、`fallback_used`、`warm_cache_hit`，
便于事后判断"为什么这次慢""用了哪套参数"。

## 预热与第一个细胞

预热策略为**自动按首帧形状预热**：每个新的采集尺寸 `(H,W)` 的第一帧会顺带建好该尺寸的 cuFFT
plan，之后同尺寸帧均为热速度。GUI 启动后还会在重建线程触发一次轻量预热（import 后端 + 初始化
CUDA 上下文），进一步缩短首个真实细胞的冷启动。若希望第一个细胞也完全热，可在已知相机 ROI 时
按该尺寸预热（后续可配置项）。

## 仓库默认 vs 生产档

为遵守项目"仿真优先"硬约束，仓库内 `config/sim_control_config.json` 默认 `use_saved_params=false`
（无 `.mat` 也能在仿真/开发环境启动与重建）。saved-params 是现场生产档：标定就绪后再开启。
旧配置（v10）升级到 v11 时，如果没有任何 `estimated_params_*_path`，迁移会强制
`use_saved_params=false`，避免升级即不可重建。

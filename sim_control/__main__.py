"""sim_control 包形式启动时的兼容入口。

当使用 python -m sim_control 启动时，解释器会执行这个文件。它只转交到历史入口函数，便于保留旧调用方式；独立 SIM 采集 GUI 的当前入口在 sim_control.sim_acquisition_app。
"""

from sim_control_app import main

if __name__ == "__main__":
    raise SystemExit(main())

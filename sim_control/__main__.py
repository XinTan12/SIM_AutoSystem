"""``python -m sim_control`` 形式启动时的历史兼容入口。

作用：
    当外部用 ``python -m sim_control`` 这种「按包名运行」的方式启动时，
    Python 解释器会自动执行包内 ``__main__.py``。本文件原本是为了把这种调用
    形式转交到旧顶层模块 ``sim_control_app.main()``。

    现状（2026-05-11 之后）：顶层 ``sim_control_app.py`` 已被废弃迁移；
    新的标准入口分别是仓库根 ``app.py`` 与
    ``sim_control/sim_acquisition_app.py``。下面的 ``from sim_control_app
    import main`` **会在没有兼容 shim 的环境中触发 ``ModuleNotFoundError``**，
    属于保留给历史调用方式的失效占位。

    新代码请改用：
        - 集成主界面：``python app.py`` 或 ``start.cmd``。
        - 独立 SIM 采集 GUI：
          ``python -m sim_control.sim_acquisition_app --config config\\sim_control_config.json``。

协作关系：
    上游：仅历史调用方（``python -m sim_control``）。
    下游：原 ``sim_control_app.main``（已迁移，调用会失败）。

维护要点：
    - 不要在这里加任何 GUI 初始化或硬件调用；若需要彻底淘汰这一入口，
      请同时改造说明文档与外部脚本，再删除本文件。
    - 修改此文件前确认没有任何 CI/批处理脚本仍用 ``python -m sim_control`` 启动。
"""

# 历史 import：上面 docstring 已说明，2026-05-11 后该模块已不存在，本行保留
# 仅为兼容旧调用形式。真正可用的入口在 ``app.py`` 与 ``sim_control.sim_acquisition_app``。
from sim_control_app import main

if __name__ == "__main__":
    # 把旧 ``main()`` 的返回值作为进程退出码；当 ``main`` 不可用时上面 import 已抛错。
    raise SystemExit(main())

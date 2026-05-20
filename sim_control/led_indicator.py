"""SIM 硬件状态指示灯控件（PyQt5 自绘圆点）。

作用：
    本文件提供一个非常小的 PyQt5 ``QWidget`` 控件，用于在 SIM 设置弹窗
    和集成主界面以圆点形式展示相机、SLM、DAQ 等硬件的连接/就绪/错误状态。
    控件本身不知道是哪种硬件，只接受一个状态字符串（gray/yellow/green/red），
    再在 ``paintEvent`` 里映射到颜色。这种解耦让 GUI 层不必关心绘制细节。

协作关系：
    上游：``sim_control/gui.py``（``SimSettingsDialog`` 与 ``SimControlWindow``）、
          ``control_wangbo/main.py``（集成主界面共享版本一致）。
    下游：仅依赖 PyQt5 标准组件，无项目内部依赖。

关键概念：
    - 状态字符串约定：``gray``=未连接/未初始化，``yellow``=连接中或警告，
      ``green``=连接 + 就绪，``red``=错误。
    - 自绘 paintEvent：通过 ``QPainter`` 抗锯齿绘制内缩 2px 的圆点；
      固定 16×16 尺寸保证多处复用时视觉一致。

维护要点：
    - 不要在本控件里加业务逻辑（例如"绿色就允许采集"），状态判定应由
      调用方完成，控件只负责显示。
    - 新增状态颜色时同步更新 ``COLORS`` 字典；未列出的状态会被 ``set_state``
      回退到 ``gray``，保持安全默认。
"""

from __future__ import annotations

from PyQt5.QtCore import QSize, Qt
from PyQt5.QtGui import QColor, QPainter
from PyQt5.QtWidgets import QWidget


class LedIndicator(QWidget):
    """用固定尺寸圆点展示硬件连接、准备、错误等状态。

    职责：
        - 维护一个内部状态字符串 ``self._state``。
        - 接受外部 ``set_state(...)`` 调用并触发重绘。
        - 在 ``paintEvent`` 中按状态映射颜色，绘制抗锯齿圆点。

    协作：
        被 GUI 层创建，作为相机/SLM/DAQ 等控件的小图标使用；不向外回调。
    """

    # 颜色查找表：键是合法状态字符串，值是对应的 ``QColor``。
    # ``set_state()`` 收到未列出的状态会自动回退到 ``"gray"``，避免运行期异常。
    COLORS = {
        "gray": QColor("#8b949e"),
        "yellow": QColor("#f2b705"),
        "green": QColor("#2da44e"),
        "red": QColor("#cf222e"),
    }

    def __init__(self, state: str = "gray", parent: QWidget | None = None):
        # 1) 调父类构造，确保 QWidget 生命周期被 Qt 正确管理。
        super().__init__(parent)
        # 2) 把状态字段初始化为默认 ``"gray"``，再走 ``set_state`` 触发一次合法化。
        self._state = "gray"
        self.set_state(state)
        # 3) 固定尺寸 16×16，使主界面排版可预测，且无须额外 layout 调整。
        self.setFixedSize(16, 16)

    def set_state(self, state: str) -> None:
        """设置新的状态字符串，并触发重绘。

        参数：
            state: 期望状态。若不在 ``COLORS`` 键集合中，会自动回退到 ``"gray"``。

        副作用：
            修改 ``self._state``，并调用 ``self.update()`` 让 Qt 在下一次事件循环重绘。
        """
        # 合法化输入：未知状态视为 gray，避免越界查表。
        self._state = state if state in self.COLORS else "gray"
        # ``update()`` 不会立即重绘，而是排队一次 paintEvent。
        self.update()

    def state(self) -> str:
        """返回当前状态字符串，便于外部断言与测试。"""
        return self._state

    def sizeHint(self) -> QSize:  # noqa: N802
        # Qt 在排版时调用本方法获取建议尺寸；与 ``setFixedSize`` 保持一致以确保稳定布局。
        return QSize(16, 16)

    def paintEvent(self, event) -> None:  # noqa: N802
        """绘制圆点：开抗锯齿、不画边框、按状态填色画圆。"""
        # 1) 创建 QPainter 并打开抗锯齿，使小尺寸圆点边缘平滑。
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        # 2) 在控件内部留出 2px 的视觉边距，让圆点在 16×16 范围内显得居中。
        rect = self.rect().adjusted(2, 2, -2, -2)
        # 3) 关闭描边，直接用纯色填充对应状态颜色。
        painter.setPen(Qt.NoPen)
        painter.setBrush(self.COLORS[self._state])
        # 4) 绘制圆形（``drawEllipse`` 在矩形为正方形时会得到圆）。
        painter.drawEllipse(rect)

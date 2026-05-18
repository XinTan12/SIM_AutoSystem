"""SIM 硬件状态指示灯控件。

这个小控件用 PyQt5 自绘圆点表达 camera、SLM、DAQ 等状态。主界面只需设置 gray/yellow/green/red 等状态字符串，paintEvent 会把状态映射成颜色。
"""

from __future__ import annotations

from PyQt5.QtCore import QSize, Qt
from PyQt5.QtGui import QColor, QPainter
from PyQt5.QtWidgets import QWidget


class LedIndicator(QWidget):
    """用固定尺寸圆点展示硬件连接、准备、错误等状态。"""
    COLORS = {
        "gray": QColor("#8b949e"),
        "yellow": QColor("#f2b705"),
        "green": QColor("#2da44e"),
        "red": QColor("#cf222e"),
    }

    def __init__(self, state: str = "gray", parent: QWidget | None = None):
        super().__init__(parent)
        self._state = "gray"
        self.set_state(state)
        self.setFixedSize(16, 16)

    def set_state(self, state: str) -> None:
        self._state = state if state in self.COLORS else "gray"
        self.update()

    def state(self) -> str:
        return self._state

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(16, 16)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(2, 2, -2, -2)
        painter.setPen(Qt.NoPen)
        painter.setBrush(self.COLORS[self._state])
        painter.drawEllipse(rect)

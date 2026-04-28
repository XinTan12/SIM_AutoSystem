from __future__ import annotations

from PyQt5.QtCore import QSize, Qt
from PyQt5.QtGui import QColor, QPainter
from PyQt5.QtWidgets import QWidget


class LedIndicator(QWidget):
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

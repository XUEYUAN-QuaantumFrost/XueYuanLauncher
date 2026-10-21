# -*- coding: utf-8 -*-
"""主窗口：无边框深色窗口 + 自绘标题栏 + QWebEngineView 承载前端"""
import os
import sys
from pathlib import Path

from PyQt5.QtCore import Qt, QUrl, QEvent, QPoint
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QPushButton, QLabel, QApplication,
)
from PyQt5.QtWebEngineWidgets import QWebEngineView
from PyQt5.QtWebChannel import QWebChannel

from core.config import app_dir


def resource_path(rel: str) -> str:
    """兼容 PyInstaller 打包后的资源路径"""
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS)
    else:
        base = app_dir()
    return str(base / rel)


class TitleBar(QWidget):
    """自绘标题栏：拖动移动窗口 + 最小化/最大化/关闭"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._parent = parent
        self.setFixedHeight(40)
        self.setStyleSheet("background:#10131A;")
        self.setAttribute(Qt.WA_StyledBackground, True)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 0, 8, 0)
        lay.setSpacing(6)
        # 左侧：标题
        title = QLabel("XueYuan Launcher")
        title.setStyleSheet("color:#9BA4B4;font-size:13px;font-weight:600;background:transparent;")
        lay.addWidget(title)
        lay.addStretch(1)

        btn_style = (
            "QPushButton{color:#9BA4B4;background:transparent;border:none;"
            "font-size:12px;border-radius:6px;width:36px;height:26px;}"
            "QPushButton:hover{background:#232936;color:#E8ECF1;}"
        )
        self.btn_min = QPushButton("─")
        self.btn_max = QPushButton("□")
        self.btn_close = QPushButton("✕")
        for b in (self.btn_min, self.btn_max):
            b.setStyleSheet(btn_style)
        self.btn_close.setStyleSheet(
            "QPushButton{color:#9BA4B4;background:transparent;border:none;"
            "font-size:12px;border-radius:6px;width:36px;height:26px;}"
            "QPushButton:hover{background:#C42B1C;color:#fff;}"
        )
        self.btn_min.clicked.connect(self._parent.showMinimized)
        self.btn_max.clicked.connect(self._toggle_max)
        self.btn_close.clicked.connect(self._parent.close)
        lay.addWidget(self.btn_min)
        lay.addWidget(self.btn_max)
        lay.addWidget(self.btn_close)

    def _toggle_max(self):
        if self._parent.isMaximized():
            self._parent.showNormal()
        else:
            self._parent.showMaximized()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag_pos = e.globalPos() - self._parent.frameGeometry().topLeft()
            e.accept()

    def mouseMoveEvent(self, e):
        if hasattr(self, "_drag_pos") and e.buttons() & Qt.LeftButton:
            self._parent.move(e.globalPos() - self._drag_pos)
            e.accept()

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._toggle_max()


class MainWindow(QMainWindow):
    def __init__(self, bridge):
        super().__init__()
        self.setWindowTitle("XueYuan Launcher")
        self.resize(1080, 720)
        self.setMinimumSize(880, 600)
        self.setWindowFlags(Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        central = QWidget(self)
        central.setStyleSheet("background:#10131A;border-radius:10px;")
        central.setAttribute(Qt.WA_StyledBackground, True)
        vbox = QVBoxLayout(central)
        vbox.setContentsMargins(1, 1, 1, 1)
        vbox.setSpacing(0)

        self.titlebar = TitleBar(self)
        vbox.addWidget(self.titlebar)

        self.web = QWebEngineView(central)
        self.web.setStyleSheet("background:#10131A;")
        self.web.page().setBackgroundColor(Qt.transparent)
        vbox.addWidget(self.web, 1)

        self.setCentralWidget(central)

        # QWebChannel 桥接
        self.channel = QWebChannel(self.web.page())
        self.bridge = bridge
        self.channel.registerObject("bridge", bridge)
        self.web.page().setWebChannel(self.channel)
        self.web.setUrl(QUrl.fromLocalFile(resource_path("ui/index.html")))

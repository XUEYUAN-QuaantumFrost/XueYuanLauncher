# -*- coding: utf-8 -*-
"""主窗口：无边框深色窗口 + 自绘标题栏 + QWebEngineView 承载前端

v1.4 修复：
- 接管 JavaScript 的 alert / confirm / prompt
  （QtWebEngine 默认不实现这三个对话框：confirm 永远返回 false、prompt 返回空，
   于是"点安装没反应"、目录选择框弹不出来。这里改成原生 QMessageBox / QInputDialog）
- 窗口支持右下角拖拽缩放，并记住上次的大小和位置
- 前端 JS 报错会写进日志文件，排查问题时不用靠猜
"""
import os
import sys
from pathlib import Path

from PyQt5.QtCore import Qt, QUrl, QRect
from PyQt5.QtGui import QGuiApplication
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QPushButton, QLabel,
    QMessageBox, QInputDialog, QLineEdit, QSizeGrip,
)
from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEnginePage
from PyQt5.QtWebChannel import QWebChannel

from core.config import app_dir


def resource_path(rel: str) -> str:
    """兼容 PyInstaller 打包后的资源路径"""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", app_dir()))
    else:
        base = app_dir()
    return str(base / rel)


class LauncherPage(QWebEnginePage):
    """给网页补上 QtWebEngine 缺失的 alert / confirm / prompt"""

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self._window = window

    def javaScriptAlert(self, securityOrigin, msg):  # noqa: N802 - Qt 接口命名
        QMessageBox.information(self._window, "提示", msg)

    def javaScriptConfirm(self, securityOrigin, msg):  # noqa: N802
        return QMessageBox.question(
            self._window, "确认", msg,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        ) == QMessageBox.Yes

    def javaScriptPrompt(self, securityOrigin, msg, defaultValue):  # noqa: N802
        text, ok = QInputDialog.getText(self._window, "输入", msg, QLineEdit.Normal,
                                        defaultValue or "")
        if ok and text:
            return True, text
        return False, ""

    def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
        try:
            print(f"[JS] {source}:{line} {message}", flush=True)
        except Exception:
            pass


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
        title = QLabel("雪原启动器")
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
        self.btn_min.setToolTip("最小化")
        self.btn_max.setToolTip("最大化 / 还原")
        self.btn_close.setToolTip("关闭")
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
        self.btn_max.setText("❐" if self._parent.isMaximized() else "□")

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and not self._parent.isMaximized():
            self._drag_pos = e.globalPos() - self._parent.frameGeometry().topLeft()
            e.accept()

    def mouseMoveEvent(self, e):
        if hasattr(self, "_drag_pos") and e.buttons() & Qt.LeftButton:
            if self._parent.isMaximized():
                self._parent.showNormal()
                self._drag_pos = e.globalPos() - self._parent.frameGeometry().topLeft()
            self._parent.move(e.globalPos() - self._drag_pos)
            e.accept()

    def mouseReleaseEvent(self, e):
        if hasattr(self, "_drag_pos"):
            del self._drag_pos

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._toggle_max()


class CentralWidget(QWidget):
    """带右下角缩放手柄的容器（无边框窗口需要手动实现 resize）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.grip = QSizeGrip(self)
        self.grip.setFixedSize(16, 16)
        self.grip.setStyleSheet("background:transparent;")
        self.grip.raise_()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.grip.move(self.width() - self.grip.width() - 2,
                       self.height() - self.grip.height() - 2)
        self.grip.raise_()


class MainWindow(QMainWindow):
    def __init__(self, bridge, config=None):
        super().__init__()
        self.bridge = bridge
        self.config = config
        self.setWindowTitle("雪原启动器")
        self.resize(1120, 740)
        self.setMinimumSize(880, 600)
        self.setWindowFlags(Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        central = CentralWidget(self)
        central.setStyleSheet("background:#10131A;border-radius:10px;")
        central.setAttribute(Qt.WA_StyledBackground, True)
        vbox = QVBoxLayout(central)
        vbox.setContentsMargins(1, 1, 1, 1)
        vbox.setSpacing(0)

        self.titlebar = TitleBar(self)
        vbox.addWidget(self.titlebar)

        self.web = QWebEngineView(central)
        self.page = LauncherPage(self, self.web)
        self.web.setPage(self.page)
        self.web.setStyleSheet("background:#10131A;")
        self.web.setContextMenuPolicy(Qt.NoContextMenu)
        self.web.page().setBackgroundColor(Qt.transparent)
        vbox.addWidget(self.web, 1)

        self.setCentralWidget(central)

        # QWebChannel 桥接
        self.channel = QWebChannel(self.web.page())
        self.channel.registerObject("bridge", bridge)
        self.web.page().setWebChannel(self.channel)
        self.web.setUrl(QUrl.fromLocalFile(resource_path("ui/index.html")))

        if bridge is not None:
            bridge.set_window(self)
        self._restore_geometry()

    # ------------------------------------------------------------ 窗口状态
    def _restore_geometry(self):
        if self.config is None:
            self._center()
            return
        geo = self.config.get("window_geometry") or []
        restored = False
        if isinstance(geo, list) and len(geo) == 4:
            x, y, w, h = [int(v) for v in geo]
            if w >= 880 and h >= 600 and self._visible_on_screen(x, y, w, h):
                self.setGeometry(x, y, w, h)
                restored = True
        if not restored:
            self._center()
        if self.config.get("window_maximized"):
            self.showMaximized()

    @staticmethod
    def _visible_on_screen(x, y, w, h) -> bool:
        try:
            for screen in QGuiApplication.screens():
                if screen.availableGeometry().intersects(QRect(x, y, w, h)):
                    return True
        except Exception:
            return True
        return False

    def _center(self):
        screen = QGuiApplication.primaryScreen()
        if not screen:
            return
        rect = screen.availableGeometry()
        w, h = min(1120, rect.width() - 80), min(740, rect.height() - 80)
        self.resize(w, h)
        self.move(rect.center().x() - w // 2, rect.center().y() - h // 2)

    def closeEvent(self, e):
        if self.config is not None:
            self.config.set("window_maximized", self.isMaximized())
            if not self.isMaximized():
                geo = self.geometry()
                self.config.set("window_geometry",
                                [geo.x(), geo.y(), geo.width(), geo.height()])
        super().closeEvent(e)

# -*- coding: utf-8 -*-
"""XueYuan Launcher 入口"""
import sys

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QApplication

from core.config import Config
from core.auth import AuthManager
from core.launcher import Launcher
from bridge import Bridge
from main_window import MainWindow


def main():
    # 高分屏适配
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    app.setApplicationName("XueYuan Launcher")

    config = Config()
    auth = AuthManager(config)
    launcher = Launcher(config, auth)
    bridge = Bridge(config, auth, launcher)
    win = MainWindow(bridge)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

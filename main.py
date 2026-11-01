# -*- coding: utf-8 -*-
"""雪原启动器 入口"""
import os
import sys
import traceback

# ===== 云电脑 / 无独立显卡环境兼容设置（必须在 PyQt5 导入之前设置）=====
# 想启用硬件加速时，设置环境变量 XUEYUAN_GPU=1 再启动即可
if os.environ.get("XUEYUAN_GPU", "0") != "1":
    os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
    os.environ.setdefault("QT_OPENGL", "software")
    os.environ.setdefault("QTWEBENGINE_USE_SOFTWARE_RENDERING", "1")
os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")


def get_log_path():
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "error.log")


# ===== 所有输出 / 崩溃信息都写到 error.log，方便排查 =====
try:
    # 行缓冲：进程被强杀时日志也不会是空的
    log_file = open(get_log_path(), "w", encoding="utf-8", buffering=1)
except Exception:
    log_file = None

if log_file is not None:
    sys.stdout = log_file
    sys.stderr = log_file

print("=== 雪原启动器 启动 ===")
print(f"Python: {sys.version}")
print(f"打包模式: {getattr(sys, 'frozen', False)}")
print(f"工作目录: {os.getcwd()}")


def _flush_log():
    if log_file is not None:
        try:
            log_file.flush()
        except Exception:
            pass


def install_excepthook():
    """没被捕获的异常也写进日志，并弹窗提示，而不是无声无息地退出"""
    def hook(exc_type, exc_value, exc_tb):
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        print("!!! 未捕获异常 !!!\n" + text)
        _flush_log()
        try:
            show_error_dialog("雪原启动器 出错了",
                              f"{exc_type.__name__}: {exc_value}")
        except Exception:
            pass
    sys.excepthook = hook


def show_error_dialog(title, content):
    """崩溃时弹错误框，不要直接闪退"""
    try:
        from PyQt5.QtWidgets import QApplication, QMessageBox
        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.critical(None, title, str(content)[:2000])
    except Exception:
        pass  # 实在弹不出来就算了


def _is_ascii(text: str) -> bool:
    try:
        return text.isascii()
    except AttributeError:  # Python < 3.7
        return all(ord(ch) < 128 for ch in text)


def _app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def check_install_path() -> bool:
    """Qt 5.15 的网页引擎在含中文的安装路径下会直接崩（Qt5Core 0xc0000409），
    这里提前拦下来，弹一句人话，而不是无声闪退。"""
    folder = _app_dir()
    if _is_ascii(folder):
        return True
    print(f"!!! 安装路径含非 ASCII 字符：{folder} !!!")
    _flush_log()
    from PyQt5.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication(sys.argv)
    QMessageBox.critical(
        None, "雪原启动器 无法启动",
        "启动器所在的文件夹路径里有中文或特殊字符：\n\n"
        f"{folder}\n\n"
        "Qt 网页引擎在中文路径下会崩溃，请把整个文件夹移动或改名成纯英文路径，例如：\n\n"
        "D:\\XueYuanLauncher\\\n\n"
        "然后再双击 雪原启动器.exe（文件夹本身叫什么都行，只要不含中文即可）。")
    return False


def main():
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QApplication
    # QWebEngineWidgets 必须在 QApplication 创建之前导入，否则会崩
    from PyQt5.QtWebEngineWidgets import QWebEngineView  # noqa: F401

    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName("雪原启动器")
    app.setApplicationDisplayName("雪原启动器")
    app.setOrganizationName("XueYuan")

    if not check_install_path():
        return 1

    print("正在加载核心模块...")
    from core.config import Config
    from core.auth import AuthManager
    from core.launcher import Launcher
    from bridge import Bridge
    from main_window import MainWindow

    print("正在初始化配置...")
    config = Config()
    auth = AuthManager(config)
    launcher = Launcher(config, auth)
    bridge = Bridge(config, auth, launcher)

    print("正在创建主窗口...")
    win = MainWindow(bridge, config)
    win.show()
    print("启动成功，进入事件循环")
    return app.exec_()


if __name__ == "__main__":
    install_excepthook()
    try:
        code = main()
        _flush_log()
        sys.exit(code)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        err = traceback.format_exc()
        print(f"!!! 启动失败 !!!\n{err}")
        _flush_log()
        show_error_dialog("雪原启动器 启动失败", f"{e}\n\n详细信息见 error.log")
        try:
            input("按回车键退出...")
        except Exception:
            pass
        sys.exit(1)

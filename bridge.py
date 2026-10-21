# -*- coding: utf-8 -*-
"""QWebChannel 桥接层：把 Python 后端能力暴露给网页 UI"""
import json
import os
import threading
import traceback

from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot, QUrl
from PyQt5.QtGui import QDesktopServices

from core.config import Config, default_minecraft_dir
from core.auth import AuthManager, AuthError
from core.launcher import Launcher, LaunchError
from core import downloader, mods as mods_mod


class Bridge(QObject):
    """所有前端调用都经过这里；耗时任务放线程，结果通过 event 信号推送"""

    event = pyqtSignal(str)  # {"type": ..., ...}

    def __init__(self, config: Config, auth: AuthManager, launcher: Launcher):
        super().__init__()
        self.config = config
        self.auth = auth
        self.launcher = launcher
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 工具
    def _emit(self, payload: dict):
        try:
            self.event.emit(json.dumps(payload, ensure_ascii=False))
        except Exception:
            pass

    def _thread(self, fn):
        def wrap():
            try:
                fn()
            except Exception as e:
                self._emit({"type": "error", "error": str(e)})
        threading.Thread(target=wrap, daemon=True).start()

    # ------------------------------------------------------------ 初始化
    @pyqtSlot(result=str)
    def get_init(self):
        """前端加载完成后调用，返回初始快照"""
        mc_dir = str(self.config.get("minecraft_directory", ""))
        installed = downloader.get_installed_versions(mc_dir)
        auth = self.auth.current()
        java = ""
        try:
            java = self.launcher.find_java(self.config)
        except Exception:
            pass
        return json.dumps({
            "settings": self.config.data,
            "installed_versions": installed,
            "auth": auth,
            "java": java,
            "default_minecraft_dir": str(default_minecraft_dir()),
        }, ensure_ascii=False)

    # ------------------------------------------------------------ 版本
    @pyqtSlot()
    def refresh_version_list(self):
        """拉取官方/镜像版本清单（线程内）"""
        def run():
            source = str(self.config.get("download_source", "official"))
            versions = downloader.get_version_list(source, force=True)
            installed = downloader.get_installed_versions(str(self.config.get("minecraft_directory", "")))
            self._emit({"type": "version_list", "versions": versions,
                        "installed": [v["id"] for v in installed], "source": source})
        self._thread(run)

    @pyqtSlot(str)
    def install_version(self, version_id):
        def run():
            mc_dir = str(self.config.get("minecraft_directory", ""))
            source = str(self.config.get("download_source", "official"))
            self._emit({"type": "install_start", "version": version_id})
            try:
                downloader.install_version(
                    version_id, mc_dir, source,
                    callback=lambda p: self._emit({"type": "install_progress", **p}))
                self._emit({"type": "install_done", "version": version_id})
                installed = downloader.get_installed_versions(mc_dir)
                self._emit({"type": "installed_refresh", "installed": [v["id"] for v in installed]})
            except Exception as e:
                self._emit({"type": "install_error", "version": version_id, "error": str(e)})
        self._thread(run)

    # ------------------------------------------------------------ 启动
    @pyqtSlot(str)
    def launch(self, version_id):
        def run():
            try:
                self.launcher.launch(version_id, callback=lambda p: self._emit(p))
            except LaunchError as e:
                self._emit({"type": "error", "error": str(e)})
            except Exception as e:
                self._emit({"type": "error", "error": f"启动异常：{e}"})
        self._thread(run)

    @pyqtSlot()
    def stop_game(self):
        self.launcher.stop()

    @pyqtSlot(result=str)
    def is_game_running(self):
        return "true" if self.launcher.is_running() else "false"

    # ------------------------------------------------------------ 登录
    @pyqtSlot(str)
    def login_offline(self, name):
        try:
            info = self.auth.offline_login(name)
            self._emit({"type": "auth_updated", "auth": self.auth.current(), "info": info})
        except AuthError as e:
            self._emit({"type": "error", "error": str(e)})

    @pyqtSlot(result=str)
    def microsoft_login_start(self):
        try:
            url = self.auth.start_microsoft_login()
            return json.dumps({"ok": True, "url": url})
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)})

    @pyqtSlot(str)
    def microsoft_login_complete(self, callback_url):
        def run():
            try:
                info = self.auth.complete_microsoft_login(callback_url)
                self._emit({"type": "auth_updated", "auth": self.auth.current(),
                            "info": info, "ok": True})
            except AuthError as e:
                self._emit({"type": "error", "error": str(e)})
        self._thread(run)

    @pyqtSlot(str, str)
    def authlib_login(self, username, server):
        try:
            info = self.auth.authlib_login(username, server)
            self._emit({"type": "auth_updated", "auth": self.auth.current(), "info": info})
        except AuthError as e:
            self._emit({"type": "error", "error": str(e)})

    @pyqtSlot()
    def logout(self):
        self.auth.logout()
        self._emit({"type": "auth_updated", "auth": self.auth.current()})

    # ------------------------------------------------------------ 模组
    @pyqtSlot(str)
    def get_loader_versions(self, loader_id):
        def run():
            mc_dir = str(self.config.get("minecraft_directory", ""))
            installed = downloader.get_installed_versions(mc_dir)
            # 优先用最近安装的原版版本
            target = ""
            for v in installed:
                if not v.get("inheritsFrom"):
                    target = v["id"]
                    break
            if not target and installed:
                target = installed[0]["id"]
            result = mods_mod.get_loader_versions(loader_id, target)
            self._emit({"type": "loader_versions", "loader": loader_id,
                        "mc_version": target, **result})
        self._thread(run)

    @pyqtSlot(str, str, str)
    def install_loader(self, loader_id, mc_version, loader_version):
        def run():
            mc_dir = str(self.config.get("minecraft_directory", ""))
            java = self.launcher.find_java(self.config)
            self._emit({"type": "loader_start", "loader": loader_id})
            try:
                mods_mod.install_loader(
                    loader_id, mc_version, mc_dir, java,
                    loader_version or None,
                    callback=lambda p: self._emit({"type": "loader_event", **p}))
                self._emit({"type": "loader_done", "loader": loader_id})
            except mods_mod.ModError as e:
                self._emit({"type": "loader_error", "loader": loader_id, "error": str(e)})
        self._thread(run)

    @pyqtSlot(str)
    def import_modpack(self, path):
        def run():
            mc_dir = str(self.config.get("minecraft_directory", ""))
            self._emit({"type": "loader_start", "loader": "modpack"})
            try:
                mods_mod.import_modpack(path, mc_dir,
                                        callback=lambda p: self._emit({"type": "loader_event", **p}))
            except mods_mod.ModError as e:
                self._emit({"type": "loader_error", "loader": "modpack", "error": str(e)})
        self._thread(run)

    @pyqtSlot(str)
    def open_folder(self, path):
        p = os.path.expanduser(path)
        if os.path.isdir(p):
            QDesktopServices.openUrl(QUrl.fromLocalFile(p))
        else:
            self._emit({"type": "error", "error": f"目录不存在：{p}"})

    @pyqtSlot(result=str)
    def get_mods_folder(self):
        mc_dir = str(self.config.get("minecraft_directory", ""))
        mods_dir = os.path.join(mc_dir, "mods")
        os.makedirs(mods_dir, exist_ok=True)
        return json.dumps({"path": mods_dir})

    # ------------------------------------------------------------ 设置
    @pyqtSlot(str)
    def save_settings(self, settings_json):
        try:
            data = json.loads(settings_json)
        except Exception:
            self._emit({"type": "error", "error": "设置格式错误"})
            return
        for k, v in data.items():
            self.config.set(k, v)
        self._emit({"type": "settings_saved", "settings": self.config.data})

    @pyqtSlot(result=str)
    def choose_java(self):
        from PyQt5.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            None, "选择 Java 可执行文件",
            "", "Java (*.exe;*)")
        if path:
            self.config.set("java_path", path)
            return json.dumps({"ok": True, "path": path})
        return json.dumps({"ok": False})

    @pyqtSlot(result=str)
    def detect_java(self):
        try:
            path = self.launcher.find_java(self.config)
            return json.dumps({"ok": True, "path": path,
                               "version": self.launcher.java_info(path)["version"]})
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)})

# -*- coding: utf-8 -*-
"""QWebChannel 桥接层：把 Python 后端能力暴露给网页 UI

v1.4 修复：
- 网页里的 confirm/prompt 在 QtWebEngine 中原本不可用（点了没反应），
  现在由主窗口的 QWebEnginePage 接管
- 选择目录 / 选择整合包 / 选择 Java 全部改用系统原生对话框
  （网页 file input 拿不到真实路径，v1.3 的整合包导入其实是坏的）
- 安装类任务加了忙碌锁，连点两次不会开出两个下载线程
- Java 检测放到后台线程，启动界面不再等待
"""
import json
import os
import threading
import traceback

from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot, QUrl
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import QFileDialog

from core.config import (Config, default_minecraft_dir, total_memory_mb,
                         max_memory_mb)
from core.auth import AuthManager, AuthError, check_offline_name
from core.launcher import Launcher, LaunchError, java_info
from core import downloader, mods as mods_mod


class Bridge(QObject):
    """所有前端调用都经过这里；耗时任务放后台线程，结果通过 event 信号推送"""

    event = pyqtSignal(str)  # {"type": ..., ...}

    def __init__(self, config: Config, auth: AuthManager, launcher: Launcher):
        super().__init__()
        self.config = config
        self.auth = auth
        self.launcher = launcher
        self.window = None
        self._busy_lock = threading.Lock()
        self._busy_task = ""

    def set_window(self, window):
        self.window = window

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
            except Exception as e:  # noqa: BLE001
                print(traceback.format_exc())
                self._emit({"type": "error", "error": str(e)})
        threading.Thread(target=wrap, daemon=True).start()

    def _begin_busy(self, task: str) -> bool:
        with self._busy_lock:
            if self._busy_task:
                self._emit({"type": "error",
                            "error": f"正在{self._busy_task}，请等它完成后再操作"})
                return False
            self._busy_task = task
            return True

    def _end_busy(self):
        with self._busy_lock:
            self._busy_task = ""

    # ------------------------------------------------------------ 初始化
    @pyqtSlot(result=str)
    def get_init(self):
        """前端加载完成后调用，返回初始快照"""
        mc_dir = self._mc_dir()
        try:
            os.makedirs(mc_dir, exist_ok=True)
        except Exception:
            pass
        installed = downloader.get_installed_versions(mc_dir)
        java = ""
        try:
            java = self.launcher.find_java_fast()
        except Exception:
            java = ""
        auth_snapshot = self.auth.current()
        auth_warning = ""
        if auth_snapshot.get("type") == "offline":
            saved_name = (auth_snapshot.get("data") or {}).get("username", "")
            if saved_name:
                try:
                    check_offline_name(saved_name)
                except AuthError as e:
                    auth_warning = str(e)
        return json.dumps({
            "settings": self.config.data,
            "installed_versions": installed,
            "auth": auth_snapshot,
            "auth_warning": auth_warning,
            "java": java,
            "java_version": java_info(java)["version"] if java else "",
            "default_minecraft_dir": str(default_minecraft_dir()),
            "total_memory_mb": total_memory_mb(),
            "max_memory_mb": max_memory_mb(),
        }, ensure_ascii=False)

    def _mc_dir(self) -> str:
        return str(self.config.get("minecraft_directory", "") or "")

    @pyqtSlot()
    def detect_java_bg(self):
        """后台完整扫描 Java，扫描完推送 java_detected"""
        def run():
            path = ""
            version = ""
            try:
                path = self.launcher.find_java()
                version = java_info(path)["version"]
            except Exception:
                path = ""
            self._emit({"type": "java_detected", "java": path, "version": version})
        self._thread(run)

    # ------------------------------------------------------------ 版本下载
    @pyqtSlot()
    def refresh_version_list(self):
        """拉取官方/镜像版本清单（线程内）"""
        def run():
            source = str(self.config.get("download_source", "official"))
            versions = downloader.get_version_list(source, force=True)
            installed = downloader.get_installed_versions(self._mc_dir())
            self._emit({"type": "version_list", "versions": versions,
                        "installed": [v["id"] for v in installed], "source": source})
        self._thread(run)

    @pyqtSlot(str)
    def install_version(self, version_id):
        if not self._begin_busy("安装游戏版本"):
            return

        def run():
            mc_dir = self._mc_dir()
            source = str(self.config.get("download_source", "official"))
            self._emit({"type": "install_start", "version": version_id})
            try:
                downloader.install_version(version_id, mc_dir, source,
                                           callback=self._emit)
                self.config.set("last_version", version_id)
                self._emit({"type": "install_done", "version": version_id})
                installed = downloader.get_installed_versions(mc_dir)
                self._emit({"type": "installed_refresh",
                            "installed": [v["id"] for v in installed]})
            except Exception as e:  # noqa: BLE001
                self._emit({"type": "install_error", "version": version_id,
                            "error": str(e)})
            finally:
                self._end_busy()

        self._thread(run)

    # ------------------------------------------------------------ 启动
    @pyqtSlot(str)
    def launch(self, version_id):
        def run():
            try:
                self.launcher.launch(version_id, callback=self._emit)
            except LaunchError as e:
                self._emit({"type": "error", "error": str(e)})
            except AuthError as e:
                self._emit({"type": "error", "error": str(e)})
            except Exception as e:  # noqa: BLE001
                print(traceback.format_exc())
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
            self._emit({"type": "auth_updated", "auth": self.auth.current(),
                        "info": info, "message": "离线登录成功"})
        except AuthError as e:
            self._emit({"type": "error", "error": str(e)})

    @pyqtSlot(result=str)
    def microsoft_login_start(self):
        try:
            url = self.auth.start_microsoft_login()
            return json.dumps({"ok": True, "url": url}, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)

    @pyqtSlot(str)
    def microsoft_login_complete(self, callback_url):
        def run():
            try:
                info = self.auth.complete_microsoft_login(callback_url)
                self._emit({"type": "auth_updated", "auth": self.auth.current(),
                            "info": info, "message": "微软正版登录成功"})
            except AuthError as e:
                self._emit({"type": "error", "error": str(e)})
        self._thread(run)

    @pyqtSlot(str, str, str)
    def authlib_login(self, username, password, server):
        def run():
            try:
                info = self.auth.authlib_login(username, password, server)
                self._emit({"type": "auth_updated", "auth": self.auth.current(),
                            "info": info, "message": "外置登录成功"})
            except AuthError as e:
                self._emit({"type": "error", "error": str(e)})
            except Exception as e:  # noqa: BLE001
                self._emit({"type": "error", "error": f"外置登录异常：{e}"})
        self._thread(run)

    @pyqtSlot()
    def logout(self):
        self.auth.logout()
        self._emit({"type": "auth_updated", "auth": self.auth.current(),
                    "message": "已退出登录"})

    # ------------------------------------------------------------ 模组
    @pyqtSlot(str)
    def get_loader_versions(self, loader_id):
        def run():
            installed = downloader.get_installed_versions(self._mc_dir())
            target = ""
            for v in installed:
                if not v.get("inheritsFrom"):
                    target = v["id"]
                    break
            if not target and installed:
                target = installed[0]["id"]
            result = mods_mod.get_loader_versions(
                loader_id, target, str(self.config.get("download_source", "official")))
            self._emit({"type": "loader_versions", "loader": loader_id,
                        "mc_version": target, **result})
        self._thread(run)

    @pyqtSlot(str, str, str)
    def install_loader(self, loader_id, mc_version, loader_version):
        if not self._begin_busy("安装模组加载器"):
            return

        def run():
            self._emit({"type": "loader_start", "loader": loader_id})
            try:
                # 加载器安装器要用「这个版本要求的 Java」（比如 NeoForge 1.21 要 21）
                java = ""
                try:
                    java = self.launcher.ensure_java_for_version(mc_version, self._emit)
                except Exception as e:  # noqa: BLE001
                    self._emit({"type": "log", "line": f"未能按版本准备 Java：{e}"})
                if not java:
                    try:
                        java = self.launcher.find_java()
                    except LaunchError:
                        java = ""
                installed = mods_mod.install_loader(
                    loader_id, mc_version, self._mc_dir(), java,
                    loader_version or None,
                    callback=self._emit,
                    source=str(self.config.get("download_source", "official")))
                if installed:
                    self.config.set("last_version", installed)
                self._emit({"type": "installed_refresh",
                            "installed": [v["id"] for v in downloader.get_installed_versions(self._mc_dir())]})
            except mods_mod.ModError as e:
                self._emit({"type": "loader_error", "loader": loader_id, "error": str(e)})
            except Exception as e:  # noqa: BLE001
                print(traceback.format_exc())
                self._emit({"type": "loader_error", "loader": loader_id, "error": str(e)})
            finally:
                self._end_busy()

        self._thread(run)

    @pyqtSlot(str)
    def import_modpack(self, path):
        if not self._begin_busy("导入整合包"):
            return

        def run():
            self._emit({"type": "loader_start", "loader": "modpack"})
            try:
                installed = mods_mod.import_modpack(
                    path, self._mc_dir(), callback=self._emit,
                    source=str(self.config.get("download_source", "official")))
                if installed:
                    self.config.set("last_version", installed)
                self._emit({"type": "installed_refresh",
                            "installed": [v["id"] for v in downloader.get_installed_versions(self._mc_dir())]})
            except mods_mod.ModError as e:
                self._emit({"type": "loader_error", "loader": "modpack", "error": str(e)})
            except Exception as e:  # noqa: BLE001
                print(traceback.format_exc())
                self._emit({"type": "loader_error", "loader": "modpack", "error": str(e)})
            finally:
                self._end_busy()

        self._thread(run)

    @pyqtSlot(result=str)
    def choose_modpack(self):
        """系统原生文件对话框选整合包（网页 file input 拿不到真实路径）"""
        path, _ = QFileDialog.getOpenFileName(
            self.window, "选择整合包", "",
            "整合包 (*.mrpack *.zip);;Modrinth 整合包 (*.mrpack);;Zip 整合包 (*.zip);;所有文件 (*.*)")
        return json.dumps({"ok": bool(path), "path": path}, ensure_ascii=False)

    @pyqtSlot(str)
    def open_folder(self, path):
        p = os.path.expanduser(path or "")
        if p and os.path.isdir(p):
            QDesktopServices.openUrl(QUrl.fromLocalFile(p))
        else:
            self._emit({"type": "error", "error": f"目录不存在：{p or '(空)'}"})

    @pyqtSlot(result=str)
    def get_mods_folder(self):
        mods_dir = os.path.join(self._mc_dir(), "mods")
        try:
            os.makedirs(mods_dir, exist_ok=True)
        except Exception:
            pass
        return json.dumps({"path": mods_dir}, ensure_ascii=False)

    @pyqtSlot(str)
    def open_url(self, url):
        if isinstance(url, str) and url.startswith("http"):
            QDesktopServices.openUrl(QUrl(url))

    # ------------------------------------------------------------ 设置
    @pyqtSlot(str)
    def save_settings(self, settings_json):
        try:
            data = json.loads(settings_json)
        except Exception:
            self._emit({"type": "error", "error": "设置格式错误"})
            return
        if not isinstance(data, dict):
            self._emit({"type": "error", "error": "设置格式错误"})
            return
        requested_memory = data.get("memory_mb")
        self.config.update(data)
        notice = ""
        try:
            requested = int(requested_memory) if requested_memory is not None else 0
        except (TypeError, ValueError):
            requested = 0
        actual = int(self.config.get("memory_mb") or 0)
        if requested and actual and requested != actual:
            notice = (f"本机物理内存约 {total_memory_mb() / 1024:.1f} GB，"
                      f"内存分配已自动调整为 {actual} MB（超过物理内存反而会卡）")
        try:
            os.makedirs(self._mc_dir(), exist_ok=True)
        except Exception:
            pass
        self._emit({"type": "settings_saved", "settings": self.config.data,
                    "notice": notice})

    @pyqtSlot(str, result=str)
    def choose_folder(self, initial):
        path = QFileDialog.getExistingDirectory(
            self.window, "选择 .minecraft 游戏目录", initial or self._mc_dir())
        return json.dumps({"ok": bool(path), "path": path}, ensure_ascii=False)

    @pyqtSlot(result=str)
    def choose_java(self):
        path, _ = QFileDialog.getOpenFileName(
            self.window, "选择 Java 可执行文件", "",
            "Java (java.exe java);;所有文件 (*.*)")
        if path:
            self.config.set("java_path", path)
            return json.dumps({"ok": True, "path": path,
                               "version": java_info(path)["version"]}, ensure_ascii=False)
        return json.dumps({"ok": False}, ensure_ascii=False)

    @pyqtSlot(result=str)
    def detect_java(self):
        try:
            path = self.launcher.find_java()
            info = java_info(path)
            return json.dumps({"ok": True, "path": path,
                               "version": info["version"],
                               "major": info["major"]}, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)

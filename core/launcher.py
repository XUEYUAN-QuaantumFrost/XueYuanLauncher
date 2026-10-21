# -*- coding: utf-8 -*-
"""启动核心：Java 检测与自动安装、JVM/游戏参数组装、游戏进程管理"""
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

import minecraft_launcher_lib.command as mll_cmd
import minecraft_launcher_lib.runtime as mll_runtime
import minecraft_launcher_lib.utils as mll_utils

from .auth import AuthManager

_java_cache = {}


class LaunchError(Exception):
    pass


def _run(cmd, timeout=15):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        return p.returncode == 0, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------- Java 检测
def detect_java_paths() -> list:
    """返回候选 Java 路径列表（系统 PATH、常见安装位置、Mojang 运行时）"""
    cands = []
    # PATH 中的 java
    p = shutil.which("java")
    if p:
        cands.append(os.path.abspath(p))
    # Mojang 自动下载的运行时目录
    for base in [Path.home() / ".minecraft", Path(os.environ.get("APPDATA", "")) / ".minecraft"]:
        rt = base / "runtime"
        if rt.exists():
            for j in rt.rglob("bin/java.exe") if os.name == "nt" else rt.rglob("bin/java"):
                cands.append(str(j))
    # Windows 常见目录
    if os.name == "nt":
        for p in [
            r"C:\Program Files\Java",
            r"C:\Program Files (x86)\Java",
            r"C:\Program Files\Eclipse Adoptium",
            r"C:\Program Files\Microsoft",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Eclipse Adoptium"),
        ]:
            if os.path.isdir(p):
                for root, _, files in os.walk(p):
                    if "java.exe" in files:
                        cands.append(os.path.join(root, "java.exe"))
    out = []
    for c in cands:
        if c not in out:
            out.append(c)
    return out


def java_info(path: str) -> dict:
    """返回 Java 版本信息：{path, version, major, ok}"""
    if path in _java_cache:
        return _java_cache[path]
    ok, text = _run([path, "-version"])
    m = re.search(r'version "([^"]+)"', text)
    version = m.group(1) if m else "未知"
    major = 0
    mm = re.match(r"(\d+)", version)
    if mm:
        major = int(mm.group(1))
        if version.startswith("1."):
            m2 = re.match(r"1\.(\d+)", version)
            if m2:
                major = int(m2.group(1))
    info = {"path": path, "version": version, "major": major, "ok": ok}
    _java_cache[path] = info
    return info


def find_java(config) -> str:
    """按配置找 Java；找不到抛错"""
    cfg_path = config.get("java_path", "") or ""
    if cfg_path and os.path.isfile(cfg_path):
        return cfg_path
    for c in detect_java_paths():
        info = java_info(c)
        if info["ok"]:
            return c
    raise LaunchError(
        "未找到 Java。请安装 Java 17/21（如 Adoptium Temurin），或在设置里手动指定 Java 路径。")


def ensure_java_runtime(version_id, minecraft_dir, callback=None):
    """Minecraft 1.17+ 需要较新 Java：自动下载 Mojang 官方运行时"""
    try:
        info = mll_runtime.get_version_runtime_information(version_id, minecraft_dir)
    except Exception:
        info = None
    if not info:
        return None
    name = info.get("name")
    if not name:
        return None
    path = mll_runtime.get_executable_path(name, minecraft_dir)
    if path and os.path.isfile(path):
        return path
    if callback:
        callback({"type": "log", "line": f"该版本需要 Java 运行时 {name}，正在自动下载（约 100MB，请耐心等待）…"})
    mll_runtime.install_jvm_runtime(name, minecraft_dir, mll_utils_empty_cb(callback))
    path = mll_runtime.get_executable_path(name, minecraft_dir)
    if not path or not os.path.isfile(path):
        raise LaunchError("Java 运行时自动安装失败，请在设置里手动指定 Java 路径")
    return path


def mll_utils_empty_cb(callback):
    if not callback:
        return None
    return {"setStatus": lambda s: callback({"type": "log", "line": s}),
            "setProgress": lambda p: None, "setMax": lambda m: None}


# ---------------------------------------------------------------- 参数组装
def build_options(config, auth_info: dict, minecraft_dir, version_id,
                  jvm_extra=None) -> dict:
    opts = {
        "username": auth_info.get("username", ""),
        "uuid": auth_info.get("uuid", ""),
        "token": auth_info.get("token", "0"),
        "launcherName": config.get("launcher_name", "XueYuan Launcher"),
        "launcherVersion": config.get("launcher_version", "1.0.0"),
        "gameDirectory": minecraft_dir,
    }
    jvm = []
    mem = int(config.get("memory_mb", 2048) or 2048)
    if mem > 0:
        jvm += ["-Xmx{}M".format(mem)]
        if mem <= 4096:
            jvm += ["-Xms256M"]
    if config.get("resolution_enabled"):
        opts["customResolution"] = True
        opts["resolutionWidth"] = str(config.get("resolution_width", 854))
        opts["resolutionHeight"] = str(config.get("resolution_height", 480))
    extra = (config.get("jvm_args", "") or "").strip()
    if extra:
        jvm += extra.split()
    if jvm_extra:
        jvm += jvm_extra
    if jvm:
        opts["jvmArguments"] = jvm
    return opts


# ---------------------------------------------------------------- 启动
class GameProcess:
    def __init__(self, proc, thread):
        self.proc = proc
        self.thread = thread
        self.exit_code = None


class Launcher:
    def __init__(self, config, auth: AuthManager):
        self.config = config
        self.auth = auth
        self.game = None
        self._lock = threading.Lock()

    def find_java(self, config=None):
        """兼容 bridge 调用的实例方法"""
        return find_java(config or self.config)

    def java_info(self, path: str) -> dict:
        return java_info(path)

    def is_running(self):
        return bool(self.game and self.game.proc and self.game.proc.poll() is None)

    def launch(self, version_id, callback=None):
        """启动游戏：校验登录 → Java → 组装命令 → 拉起进程 → 转发日志"""
        with self._lock:
            if self.is_running():
                raise LaunchError("游戏已在运行中")
            mc_dir = str(self.config.get("minecraft_directory", ""))
            if not os.path.isdir(os.path.join(mc_dir, "versions", version_id)):
                raise LaunchError(f"版本 {version_id} 未安装，请先下载")
            auth_info = self.auth.launch_info(mc_dir)

            # 外置登录需要 authlib-injector
            jvm_extra = None
            if auth_info["type"] == "authlib":
                jar = self.auth.ensure_authlib_injector(mc_dir)
                server = (self.config.get("auth_data", {}).get("server", ""))
                jvm_extra = [f"-javaagent:{jar}={server}"]
                # 预填外置登录信息，避免部分服务器验证失败
                if auth_info.get("uuid"):
                    jvm_extra.append(f"-Dauthlibinjector.yggdrasil.prefetched={auth_info['uuid']}={auth_info['token']}")

            # 先尝试用系统/配置 Java，再尝试 Mojang 运行时
            java_path = None
            try:
                java_path = find_java(self.config)
            except LaunchError:
                pass
            if not java_path:
                try:
                    java_path = ensure_java_runtime(version_id, mc_dir, callback)
                except LaunchError as e:
                    raise LaunchError(str(e))
            if not java_path:
                raise LaunchError("找不到可用的 Java，请安装 Java 17/21 或到设置里指定路径")

            options = build_options(self.config, auth_info, mc_dir, version_id, jvm_extra)
            try:
                command = mll_cmd.get_minecraft_command(version_id, mc_dir, options)
            except Exception as e:
                raise LaunchError(f"生成启动命令失败：{e}")

            if callback:
                callback({"type": "log", "line": "> " + " ".join(command[:6]) + " …"})
            try:
                proc = subprocess.Popen(
                    command,
                    cwd=mc_dir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8", errors="replace",
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            except Exception as e:
                raise LaunchError(f"启动失败：{e}")
            self.game = GameProcess(proc, None)
            t = threading.Thread(target=self._pipe_log, args=(proc, callback), daemon=True)
            self.game.thread = t
            t.start()
            if callback:
                callback({"type": "game_state", "state": "running", "version": version_id})
            return True

    def _pipe_log(self, proc, callback):
        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                if callback:
                    callback({"type": "log", "line": line})
        except Exception:
            pass
        finally:
            self.game = None
            if callback:
                callback({"type": "game_state", "state": "stopped"})

    def stop(self):
        if self.game and self.game.proc and self.game.proc.poll() is None:
            try:
                self.game.proc.terminate()
            except Exception:
                pass

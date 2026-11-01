# -*- coding: utf-8 -*-
"""启动核心：Java 检测与自动安装、JVM/游戏参数组装、游戏进程管理

v1.4 关键修复：
- 启动命令真正使用「设置里指定的 / 自动检测到的 Java」
  （v1.3 生成的命令其实忽略了这些，玩家选了 Java 也没用）
- 按版本 json 的 javaVersion 校验 Java 主版本；不匹配时自动下载 Mojang
  官方运行时，不会再出现 "Unsupported class version" 这种看不懂的报错
- 老版本（1.16 及更早）优先挑 Java 8/11，避免用 Java 21 跑 1.12 直接崩
- 停止游戏先温和结束、超时再强杀，不会残留 java.exe 占内存
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path

import minecraft_launcher_lib.command as mll_cmd

from . import downloader
from .auth import AuthManager

_java_cache = {}
_java_lock = threading.Lock()

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


class LaunchError(Exception):
    pass


def _run(cmd, timeout=20):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           creationflags=_NO_WINDOW)
        return p.returncode == 0, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------- Java 检测
def _walk_java(root: Path, max_depth=3):
    """在 root 下按深度查找 java.exe / java，避免全盘递归卡住界面"""
    root = Path(root)
    if not root.is_dir():
        return
    exe = "java.exe" if os.name == "nt" else "java"
    stack = [(root, 0)]
    while stack:
        cur, depth = stack.pop()
        if depth > max_depth:
            continue
        try:
            entries = list(cur.iterdir())
        except Exception:
            continue
        for entry in entries:
            try:
                if entry.is_file() and entry.name.lower() == exe:
                    yield entry
                elif entry.is_dir() and depth < max_depth:
                    stack.append((entry, depth + 1))
            except Exception:
                continue


def detect_java_paths(minecraft_dir=None) -> list:
    """返回候选 Java 路径列表（JAVA_HOME、PATH、常见安装位置、Mojang 运行时）"""
    cands = []

    java_home = os.environ.get("JAVA_HOME", "")
    if java_home:
        cands.append(str(Path(java_home) / "bin" / ("java.exe" if os.name == "nt" else "java")))

    which = shutil.which("java")
    if which:
        cands.append(os.path.abspath(which))

    # Mojang 自动下载的运行时：runtime/<组件>/<平台>/<组件>/bin/java
    mc_dirs = []
    if minecraft_dir:
        mc_dirs.append(Path(minecraft_dir))
    mc_dirs.append(Path.home() / ".minecraft")
    if os.name == "nt":
        mc_dirs.append(Path(os.environ.get("APPDATA", str(Path.home()))) / ".minecraft")
    for base in mc_dirs:
        rt = base / "runtime"
        if not rt.is_dir():
            continue
        exe = "java.exe" if os.name == "nt" else "java"
        # 兼容两种目录结构：
        #   runtime/<组件>/<平台>/<组件>/bin/java.exe（Mojang 新结构）
        #   runtime/<组件>/bin/java.exe（旧结构 / 微软商店版）
        for p in rt.rglob(exe):
            if p.parent.name == "bin":
                cands.append(str(p))

    if os.name == "nt":
        for root in [
            os.path.expandvars(r"%ProgramFiles%\Java"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Java"),
            os.path.expandvars(r"%ProgramFiles%\Eclipse Adoptium"),
            os.path.expandvars(r"%ProgramFiles%\Microsoft"),
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Eclipse Adoptium"),
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft"),
        ]:
            if os.path.isdir(root):
                for jar in _walk_java(Path(root), max_depth=3):
                    cands.append(str(jar))

    out = []
    for c in cands:
        if c and c not in out:
            out.append(c)
    return out


def _parse_major(version: str) -> int:
    m = re.match(r"(\d+)", version or "")
    if not m:
        return 0
    major = int(m.group(1))
    if version.startswith("1."):
        m2 = re.match(r"1\.(\d+)", version)
        if m2:
            major = int(m2.group(1))
    return major


def java_info(path: str) -> dict:
    """返回 Java 版本信息：{path, version, major, ok}"""
    key = str(path)
    with _java_lock:
        cached = _java_cache.get(key)
    if cached is not None:
        return cached
    ok, text = _run([key, "-version"], timeout=20)
    m = re.search(r'version "([^"]+)"', text) or re.search(r'version\s+(\S+)', text)
    version = m.group(1) if m else "未知"
    info = {"path": key, "version": version, "major": _parse_major(version), "ok": ok}
    with _java_lock:
        _java_cache[key] = info
    return info


def _score_candidates(candidates, required_major):
    """在候选 Java 中挑最合适的：优先精确匹配版本，其次最低的更高版本"""
    usable = [(info["major"], info["path"]) for info in candidates if info["ok"]]
    if not usable:
        return None
    if required_major:
        exact = [c for c in usable if c[0] == required_major]
        if exact:
            return exact[0][1]
        above = sorted([c for c in usable if c[0] > required_major])
        if above:
            return above[0][1]
        return max(usable)[1]
    # 老版本没有 javaVersion 信息：Java 8/11/16 最稳，别用 21 硬跑 1.12
    legacy = sorted([c for c in usable if 8 <= c[0] <= 17])
    if legacy:
        return legacy[0][1]
    return max(usable)[1]


def find_java(config, required_major=0) -> str:
    """按配置找 Java；找不到抛错"""
    cfg_path = (config.get("java_path", "") or "").strip()
    if cfg_path and os.path.isfile(cfg_path):
        info = java_info(cfg_path)
        if info["ok"] and (not required_major or info["major"] >= required_major):
            return cfg_path
        # 指定的 Java 版本不够新：继续找其它候选

    mc_dir = str(config.get("minecraft_directory", "") or "")
    infos = [java_info(c) for c in detect_java_paths(mc_dir)]
    chosen = _score_candidates(infos, required_major)
    if chosen:
        return chosen
    raise LaunchError(
        "未找到可用的 Java。请安装 Java 17/21（推荐 Adoptium Temurin），"
        "或在「设置」里手动指定 java.exe 路径。")


def find_java_fast(config) -> str:
    """不启动子进程的快速探测，用于界面初始化（完整检测放到后台线程）"""
    cfg_path = (config.get("java_path", "") or "").strip()
    if cfg_path and os.path.isfile(cfg_path):
        return cfg_path
    with _java_lock:
        for info in _java_cache.values():
            if info.get("ok"):
                return info["path"]
    which = shutil.which("java")
    if which:
        return os.path.abspath(which)
    return ""


# ---------------------------------------------------------------- Java 版本要求
def _read_version_json(minecraft_dir, version_id):
    p = Path(minecraft_dir) / "versions" / version_id / f"{version_id}.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def version_java_info(version_id, minecraft_dir):
    """读取版本（含继承链）需要的 Java：{"name": 组件, "major": 主版本} 或 None"""
    seen = set()
    current = version_id
    while current and current not in seen:
        seen.add(current)
        data = _read_version_json(minecraft_dir, current)
        if not data:
            return None
        java_version = data.get("javaVersion") or {}
        if java_version.get("component"):
            try:
                major = int(java_version.get("majorVersion") or 0)
            except (TypeError, ValueError):
                major = 0
            return {"name": java_version["component"], "major": major}
        current = data.get("inheritsFrom")
    return None


def _legacy_assets_dir(version_id, minecraft_dir):
    """1.6 及以前版本的资源目录不在 assets 里（map_to_resources / virtual），
    返回正确的 --assetsDir 路径；普通版本返回空串"""
    seen = set()
    current = version_id
    index_name = ""
    while current and current not in seen:
        seen.add(current)
        data = _read_version_json(minecraft_dir, current)
        if not data:
            break
        if data.get("assetIndex"):
            index_name = data["assetIndex"].get("id") or data.get("assets") or ""
            break
        current = data.get("inheritsFrom")
    if not index_name:
        return ""
    index_path = Path(minecraft_dir) / "assets" / "indexes" / f"{index_name}.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if index.get("map_to_resources"):
        return str(Path(minecraft_dir) / "resources")
    if index.get("virtual"):
        return str(Path(minecraft_dir) / "assets" / "virtual" / index_name)
    return ""


# ---------------------------------------------------------------- 参数组装
def build_options(config, auth_info: dict, minecraft_dir, version_id,
                  java_path, jvm_extra=None) -> dict:
    opts = {
        "username": auth_info.get("username", ""),
        "uuid": auth_info.get("uuid", ""),
        "token": auth_info.get("token", "0"),
        "launcherName": config.get("launcher_name", "XueYuan Launcher"),
        "launcherVersion": config.get("launcher_version", "1.4.0"),
        "gameDirectory": str(minecraft_dir),
        "executablePath": str(java_path),
        "enableLoggingConfig": True,
    }

    mem = int(config.get("memory_mb", 4096) or 4096)
    mem = max(512, min(32768, mem))
    jvm = [f"-Xmx{mem}M", f"-Xms{min(512, mem)}M"]
    jvm.append("-Dfile.encoding=UTF-8")

    extra = (config.get("jvm_args", "") or "").strip()
    if extra:
        try:
            jvm += [t for t in shlex.split(extra, posix=False) if t]
        except Exception:
            jvm += extra.split()

    if jvm_extra:
        jvm += jvm_extra

    if config.get("resolution_enabled"):
        opts["customResolution"] = True
        opts["resolutionWidth"] = str(int(config.get("resolution_width", 854) or 854))
        opts["resolutionHeight"] = str(int(config.get("resolution_height", 480) or 480))

    opts["jvmArguments"] = jvm
    return opts


# ---------------------------------------------------------------- 进程管理
def ensure_java_for_version(config, version_id, minecraft_dir, callback=None) -> str:
    """为某个版本准备一个可用的 Java（安装 Forge/Fabric 安装器时也要用）"""
    requirement = version_java_info(version_id, minecraft_dir)
    needed = requirement["major"] if requirement else 0
    try:
        path = find_java(config, needed)
        if path and (not needed or java_info(path)["major"] >= needed):
            return path
    except LaunchError:
        pass
    if requirement and requirement.get("name"):
        return downloader.ensure_java_runtime(requirement["name"], minecraft_dir, callback)
    raise LaunchError("未找到可用的 Java，请安装 Java 17/21 或在设置里手动指定")


class GameProcess:
    def __init__(self, proc, version_id):
        self.proc = proc
        self.version_id = version_id
        self.started = time.time()


class Launcher:
    def __init__(self, config, auth: AuthManager):
        self.config = config
        self.auth = auth
        self.game = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------ 兼容 bridge
    def find_java(self, config=None, required_major=0):
        return find_java(config or self.config, required_major)

    def find_java_fast(self, config=None):
        return find_java_fast(config or self.config)

    def java_info(self, path: str) -> dict:
        return java_info(path)

    def ensure_java_for_version(self, version_id, callback=None) -> str:
        """给安装加载器之类的操作准备 Java"""
        return ensure_java_for_version(self.config, version_id,
                                       str(self.config.get("minecraft_directory", "") or ""),
                                       callback)

    def is_running(self):
        with self._lock:
            proc = self.game.proc if self.game else None
        return bool(proc and proc.poll() is None)

    # ------------------------------------------------------------ 启动
    def launch(self, version_id, callback=None):
        """启动游戏：校验版本 → 登录 → Java → 组装命令 → 拉起进程 → 转发日志"""
        log = (lambda text: callback({"type": "log", "line": text})) if callback else (lambda text: None)

        with self._lock:
            if self.is_running():
                raise LaunchError("游戏已经在运行了")

            mc_dir = str(self.config.get("minecraft_directory", "") or "")
            if not mc_dir or not os.path.isdir(os.path.join(mc_dir, "versions", version_id)):
                raise LaunchError(f"版本 {version_id} 还没安装，请先到「下载」页安装")

            # 1. 登录信息（微软账号会自动续期）
            auth_info = self.auth.launch_info(mc_dir)

            # 2. Java 选择
            requirement = version_java_info(version_id, mc_dir)
            required_major = requirement["major"] if requirement else 0
            component = requirement["name"] if requirement else ""
            configured = (self.config.get("java_path", "") or "").strip()
            user_forced = bool(configured and os.path.isfile(configured))

            java_path = ""
            try:
                java_path = find_java(self.config, required_major)
            except LaunchError:
                java_path = ""
            current_major = java_info(java_path)["major"] if java_path else 0

            # 需要官方运行时的情况：
            #   a) 本机找不到 Java
            #   b) 本机 Java 太旧
            #   c) 老版本需要精确的 Java 8/16（用 Java 21 跑 1.12 会崩）
            #   d) 整个版本没写 javaVersion，但只有 Java 21，老版本同样跑不动
            need_runtime = ""
            if not java_path:
                need_runtime = component or "jre-legacy"
            elif required_major and current_major < required_major:
                need_runtime = component
            elif (required_major and current_major != required_major
                  and required_major < 17 and not user_forced):
                need_runtime = component
            elif not required_major and current_major > 17 and not user_forced:
                need_runtime = "jre-legacy"

            if need_runtime:
                if java_path:
                    log(f"该版本更适合 Java {required_major or 8}，当前是 Java {current_major}，"
                        f"正在准备官方运行时…")
                else:
                    log("本机没有找到可用的 Java，正在下载 Mojang 官方运行时…")
                try:
                    java_path = downloader.ensure_java_runtime(need_runtime, mc_dir, callback)
                except Exception as e:  # noqa: BLE001
                    if not java_path:
                        raise LaunchError(str(e))
                    log(f"官方运行时不可用，继续使用本机 Java：{e}")

            if not java_path:
                raise LaunchError(
                    "找不到可用的 Java。请安装 Java 17/21"
                    "（推荐 Adoptium Temurin），或在「设置」里手动指定 java.exe 路径。")
            info = java_info(java_path)
            memory_mb = int(self.config.get("memory_mb", 4096) or 4096)
            log(f"使用 Java {info['version']}（{java_path}），内存 {memory_mb} MB")

            # 3. 外置登录需要的 authlib-injector 参数
            jvm_extra = []
            if auth_info.get("type") == "authlib":
                jar = self.auth.ensure_authlib_injector(mc_dir)
                server = auth_info.get("server", "")
                if jar and server:
                    jvm_extra.append(f"-javaagent:{jar}={server}")
                prefetched = auth_info.get("prefetched", "")
                if prefetched:
                    jvm_extra.append(f"-Dauthlibinjector.yggdrasil.prefetched={prefetched}")
                log("已启用外置登录（authlib-injector）")

            # 4. 组装命令
            options = build_options(self.config, auth_info, mc_dir, version_id,
                                    java_path, jvm_extra)
            try:
                command = mll_cmd.get_minecraft_command(version_id, mc_dir, options)
            except Exception as e:  # noqa: BLE001
                raise LaunchError(f"生成启动命令失败：{e}")

            # 老版本资源目录修正（1.6 以前是 resources/，1.6 是 assets/virtual/legacy）
            legacy_assets = _legacy_assets_dir(version_id, mc_dir)
            if legacy_assets:
                for idx, arg in enumerate(command):
                    if arg == "--assetsDir" and idx + 1 < len(command):
                        command[idx + 1] = legacy_assets
                        break

            log("启动命令：" + " ".join(command[:6]) + " …")
            try:
                proc = subprocess.Popen(
                    command,
                    cwd=mc_dir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=_NO_WINDOW,
                )
            except Exception as e:  # noqa: BLE001
                raise LaunchError(f"启动失败：{e}")

            self.game = GameProcess(proc, version_id)
            threading.Thread(target=self._pipe_log, args=(proc, callback, version_id),
                             daemon=True).start()
            if callback:
                callback({"type": "game_state", "state": "running", "version": version_id})
            return True

    def _pipe_log(self, proc, callback, version_id):
        """把游戏输出转发到界面，同时落一份文件，方便事后排查崩溃"""
        log_handle = None
        try:
            log_dir = Path(self.config.get("minecraft_directory", "") or "") / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_handle = open(log_dir / f"xueyuan-{version_id}.log", "w",
                              encoding="utf-8", buffering=1)
            log_handle.write(
                f"=== XueYuan Launcher 启动 {version_id}  "
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        except Exception:
            log_handle = None
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                text = line.rstrip("\r\n")
                if log_handle is not None:
                    try:
                        log_handle.write(text + "\n")
                    except Exception:
                        pass
                if callback and text:
                    callback({"type": "log", "line": text})
        except Exception:
            pass
        finally:
            if log_handle is not None:
                try:
                    log_handle.close()
                except Exception:
                    pass
            try:
                code = proc.wait(timeout=5)
            except Exception:
                code = None
            with self._lock:
                if self.game and self.game.proc is proc:
                    self.game = None
            if callback:
                callback({"type": "game_state", "state": "stopped", "exit_code": code})

    def stop(self):
        with self._lock:
            game = self.game
        if not game or game.proc.poll() is not None:
            return False
        proc = game.proc
        try:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except Exception:
                proc.kill()
        except Exception:
            pass
        return True

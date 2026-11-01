# -*- coding: utf-8 -*-
"""配置持久化：JSON 文件，位于程序目录 config.json

设计要点：
- 配置目录不可写（例如装在 Program Files）时，自动回退到 %APPDATA%\\XueYuanLauncher
- 旧版本写下的空字符串目录会被纠正为默认 .minecraft 目录，
  避免游戏文件被装到启动器自己的文件夹里
- 保存使用临时文件 + 原子替换，断电或崩溃不会留下半截 JSON
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def app_dir() -> Path:
    """程序所在目录（打包后为 exe 目录，开发时为项目根目录）"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


_data_dir_cache = None


def data_dir() -> Path:
    """配置可写目录：优先程序目录，不可写时退回用户目录"""
    global _data_dir_cache
    if _data_dir_cache is not None:
        return _data_dir_cache
    candidate = app_dir()
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        _data_dir_cache = candidate
        return candidate
    except Exception:
        fallback = Path(os.environ.get("APPDATA") or str(Path.home())) / "XueYuanLauncher"
        fallback.mkdir(parents=True, exist_ok=True)
        _data_dir_cache = fallback
        return fallback


def default_minecraft_dir() -> Path:
    """默认 .minecraft 目录"""
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", str(Path.home()))) / ".minecraft"
    return Path.home() / ".minecraft"


def total_memory_mb() -> int:
    """物理内存大小（MB）；取不到返回 0"""
    try:
        if os.name == "nt":
            import ctypes

            class _MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MemoryStatusEx()
            status.dwLength = ctypes.sizeof(_MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys // (1024 * 1024))
        elif sys.platform == "darwin":
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                                 text=True, timeout=5)
            return int(out.stdout.strip()) // (1024 * 1024)
        else:
            with open("/proc/meminfo", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 0


def max_memory_mb() -> int:
    """建议的 -Xmx 上限：物理内存的 65%，按 256MB 取整
    （给游戏分超过物理内存的内存只会拖慢整机，甚至让内置服务器卡死）"""
    total = total_memory_mb()
    if total <= 0:
        return 8192
    return max(2048, min(32768, int(total * 0.65) // 256 * 256))


VALID_SOURCES = ("official", "bmclapi", "mcbbs")

DEFAULTS = {
    "minecraft_directory": str(default_minecraft_dir()),
    "download_source": "official",          # official | bmclapi | mcbbs
    "java_path": "",                         # 空 = 自动检测
    "memory_mb": 4096,
    "resolution_enabled": False,
    "resolution_width": 854,
    "resolution_height": 480,
    "jvm_args": "",
    "auth_type": "offline",                  # offline | microsoft | authlib
    "auth_data": {},
    "last_version": "",
    "launcher_name": "雪原启动器",
    "launcher_version": "1.5.0",
    "window_geometry": [],
    "window_maximized": False,
}


def _as_int(value, fallback: int, lo: int, hi: int) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(lo, min(hi, num))


class Config:
    """简单的 JSON 配置管理，任何字段改动立即落盘"""

    def __init__(self, path=None):
        self.path = Path(path) if path else (data_dir() / "config.json")
        self.data = dict(DEFAULTS)
        self.load()

    # ------------------------------------------------------------ 读写
    def load(self):
        if not self.path.exists():
            self._normalize()
            return
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            # 配置损坏：备份一份，然后用默认值启动
            try:
                self.path.replace(self.path.with_name(self.path.name + ".broken"))
            except Exception:
                pass
            self._normalize()
            return
        if isinstance(saved, dict):
            for key, value in saved.items():
                if key in DEFAULTS or isinstance(value, (str, int, float, bool, dict, list)):
                    self.data[key] = value
        self._normalize()

    def save(self):
        """原子写盘，失败不抛异常（避免因为配置写不进去而崩掉整个启动器）"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=".config-", suffix=".tmp", dir=str(self.path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=2)
                os.replace(tmp_name, str(self.path))
            finally:
                if os.path.exists(tmp_name):
                    try:
                        os.unlink(tmp_name)
                    except Exception:
                        pass
        except Exception:
            pass

    # ------------------------------------------------------------ 数据校验
    def _normalize(self):
        # 游戏目录：空值 / 非法值一律回到默认目录
        mc = str(self.data.get("minecraft_directory") or "").strip().strip('"')
        if not mc:
            mc = str(default_minecraft_dir())
        self.data["minecraft_directory"] = mc

        if self.data.get("download_source") not in VALID_SOURCES:
            self.data["download_source"] = "official"

        self.data["memory_mb"] = _as_int(self.data.get("memory_mb"), 4096, 512, max_memory_mb())
        self.data["resolution_width"] = _as_int(self.data.get("resolution_width"), 854, 320, 7680)
        self.data["resolution_height"] = _as_int(self.data.get("resolution_height"), 480, 240, 4320)
        self.data["resolution_enabled"] = bool(self.data.get("resolution_enabled", False))

        if not isinstance(self.data.get("auth_data"), dict):
            self.data["auth_data"] = {}
        if self.data.get("auth_type") not in ("offline", "microsoft", "authlib"):
            self.data["auth_type"] = "offline"

        geo = self.data.get("window_geometry")
        if not (isinstance(geo, list) and len(geo) == 4 and all(isinstance(v, int) for v in geo)):
            self.data["window_geometry"] = []

    # ------------------------------------------------------------ 访问
    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        self._normalize()
        self.save()

    def update(self, values: dict):
        self.data.update(values)
        self._normalize()
        self.save()

# -*- coding: utf-8 -*-
"""配置持久化：JSON 文件，位于程序目录 config.json"""
import json
import os
import sys
from pathlib import Path


def app_dir() -> Path:
    """程序所在目录（打包后为 exe 目录，开发时为项目根目录）"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def default_minecraft_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", str(Path.home()))) / ".minecraft"
    return Path.home() / ".minecraft"


DEFAULTS = {
    "minecraft_directory": str(default_minecraft_dir()),
    "download_source": "official",          # official | bmclapi | mcbbs
    "java_path": "",                         # 空 = 自动检测
    "memory_mb": 2048,
    "resolution_enabled": False,
    "resolution_width": 854,
    "resolution_height": 480,
    "jvm_args": "",
    "auth_type": "offline",                  # offline | microsoft | authlib
    "auth_data": {},
    "last_version": "",
    "launcher_name": "XueYuan Launcher",
    "launcher_version": "1.0.0",
}


class Config:
    """简单的 JSON 配置管理，任何字段改动立即落盘"""

    def __init__(self, path=None):
        self.path = Path(path) if path else (app_dir() / "config.json")
        self.data = dict(DEFAULTS)
        self.load()

    def load(self):
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    self.data.update(saved)
            except Exception:
                pass

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        self.save()

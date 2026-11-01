# -*- coding: utf-8 -*-
"""国内镜像适配

为什么需要这个模块：
- minecraft-launcher-lib 里 Forge / NeoForge 的 maven 地址是写死的，
  `maven.neoforged.net` 在国内经常直接连不上（实测超时），
  导致 NeoForge 版本列表和安装完全不可用；
- BMCLAPI 提供了一个 maven 镜像 `https://bmclapi2.bangbang93.com/maven`，
  可以正常拿到 NeoForge / Forge 的 metadata 和安装器。

所以这里自己解析 maven-metadata，并在选择 BMCLAPI 源时把安装器地址换成镜像；
NeoForge 的安装也由这里直接完成（下载 installer -> java -jar --install-client）。
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import downloader

MAVEN_MIRROR = "https://bmclapi2.bangbang93.com/maven"

_OFFICIAL_BASE = {
    "forge": "https://maven.minecraftforge.net",
    "neoforge": "https://maven.neoforged.net/releases",
}
_GROUP_PATH = {
    "forge": "net/minecraftforge/forge",
    "neoforge": "net/neoforged/neoforge",
}

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
_metadata_cache = {}
_metadata_cache_time = {}
_METADATA_TTL = 600  # 10 分钟，避免一直在跑老列表


# ---------------------------------------------------------------- metadata
def _fetch_metadata(kind: str, source: str, timeout=30):
    """按换源顺序拉 maven-metadata.xml，返回版本列表（maven 顺序：旧 → 新）"""
    import time as _time

    key = (kind, source)
    cached = _metadata_cache.get(key)
    if cached and _time.time() - _metadata_cache_time.get(key, 0) < _METADATA_TTL:
        return cached

    group = _GROUP_PATH[kind]
    bases = []
    if source == "bmclapi":
        bases.append(MAVEN_MIRROR)
    bases.append(_OFFICIAL_BASE[kind])

    last_error = None
    for base in bases:
        url = f"{base}/{group}/maven-metadata.xml"
        try:
            r = downloader._session().get(url, timeout=(10, timeout),
                                          headers={"Accept": "application/xml"})
            r.raise_for_status()
            versions = re.findall(r"<version>([^<]+)</version>", r.text)
            if versions:
                _metadata_cache[key] = versions
                _metadata_cache_time[key] = _time.time()
                return versions
        except Exception as e:  # noqa: BLE001
            last_error = e
    raise RuntimeError(f"获取 {kind} 版本列表失败（官方与镜像都不可用）：{last_error}")


def _version_key(value: str):
    """把 47.4.28 / 21.1.265 这类版本号变成可比较的 key
    （maven metadata 是字符串排序，47.4.28 会排在 47.4.5 前面，不能直接用）"""
    key = []
    for part in re.split(r"[.\-+]", value or ""):
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part.lower()))
    return key


def _newest_first(versions):
    return sorted(versions, key=_version_key, reverse=True)


def _neoforge_mc_candidates(loader_version: str) -> list:
    """NeoForge 版本号 -> 可能对应的 MC 版本
    21.1.80   -> 1.21.1（老命名）
    20.2.4    -> 1.20.2
    26.1.0.0  -> 26.1（新命名，2026 起 MC 直接用 26.x）
    """
    m = re.match(r"^(\d+)\.(\d+)", loader_version or "")
    if not m or m.group(1) == "0":
        return []
    major, minor = m.group(1), m.group(2)
    base = f"{major}" if minor == "0" else f"{major}.{minor}"
    return [f"1.{base}", base]


def _neoforge_mc_version(loader_version: str) -> str:
    candidates = _neoforge_mc_candidates(loader_version)
    return candidates[0] if candidates else ""


def _neoforge_matches(loader_version: str, mc_version: str) -> bool:
    return mc_version in _neoforge_mc_candidates(loader_version)


def neoforge_versions(mc_version: str, source="official", stable_only=False) -> list:
    versions = _fetch_metadata("neoforge", source)
    matched = []
    for v in versions:
        if stable_only and any(tag in v for tag in ("alpha", "beta", "snapshot")):
            continue
        if _neoforge_matches(v, mc_version):
            matched.append(v)
    return _newest_first(matched)


def forge_versions(mc_version: str, source="official") -> list:
    """返回 build 号列表，新的在前"""
    versions = [v for v in _fetch_metadata("forge", source) if "-" in v]
    builds = [v.split("-", 1)[1] for v in versions if v.split("-", 1)[0] == mc_version]
    return _newest_first(builds)


# ---------------------------------------------------------------- 安装器 URL
def neoforge_installer_url(loader_version: str, source="official") -> str:
    name = f"neoforge-{loader_version}-installer.jar"
    if source == "bmclapi":
        return f"{MAVEN_MIRROR}/net/neoforged/neoforge/{loader_version}/{name}"
    return f"{_OFFICIAL_BASE['neoforge']}/net/neoforged/neoforge/{loader_version}/{name}"


def forge_installer_url(mc_version: str, loader_version: str, source="official") -> str:
    full = f"{mc_version}-{loader_version}"
    name = f"forge-{full}-installer.jar"
    if source == "bmclapi":
        return f"{MAVEN_MIRROR}/net/minecraftforge/forge/{full}/{name}"
    return f"{_OFFICIAL_BASE['forge']}/net/minecraftforge/forge/{full}/{name}"


# ---------------------------------------------------------------- Forge 换源
_original_forge_metadata = None
_original_forge_installer = None


def prepare_forge(source="official"):
    """把 mll 的 Forge maven 地址临时换到镜像（选择官方源时换回去）"""
    global _original_forge_metadata, _original_forge_installer
    try:
        from minecraft_launcher_lib.mod_loader import _forge as mll_forge_mod
    except Exception:
        return
    if _original_forge_metadata is None:
        _original_forge_metadata = mll_forge_mod._MAVEN_METADATA_URL
    if _original_forge_installer is None:
        _original_forge_installer = mll_forge_mod.Forge.get_installer_url

    if source == "bmclapi":
        mll_forge_mod._MAVEN_METADATA_URL = f"{MAVEN_MIRROR}/net/minecraftforge/forge/maven-metadata.xml"

        def _mirror_installer(self, mc_version, loader_version):
            return forge_installer_url(mc_version, loader_version, "bmclapi")

        mll_forge_mod.Forge.get_installer_url = _mirror_installer
    else:
        mll_forge_mod._MAVEN_METADATA_URL = _original_forge_metadata
        mll_forge_mod.Forge.get_installer_url = _original_forge_installer


def install_forge(mc_version, minecraft_dir, loader_version, java, source="official",
                  callback=None):
    """用 Forge 官方安装器装客户端。

    为什么不用 mll 自己拼装的流程：
    Forge 1.13+ 需要在本地生成 srg / extra / patched client 几个 jar，
    这一步由官方安装器的 processor 完成。自己实现容易漏文件——
    表现就是"版本装好了，一启动就退出码 1"。
    """
    log = (lambda text: callback({"type": "log", "line": text})) if callback else (lambda text: None)
    mc_dir = str(minecraft_dir)
    if not loader_version:
        versions = forge_versions(mc_version, source)
        if not versions:
            raise RuntimeError(f"Forge 没有支持 Minecraft {mc_version} 的版本")
        loader_version = versions[0]

    _ensure_launcher_profiles(mc_dir)
    url = forge_installer_url(mc_version, loader_version, source)
    tmpdir = tempfile.mkdtemp(prefix="forge_")
    installer = os.path.join(tmpdir, "forge-installer.jar")
    try:
        log(f"下载 Forge 安装器 {mc_version}-{loader_version}…")
        downloader.download_file(url, installer, timeout=300, retries=2)
        command = [java or "java", "-jar", installer, "--installClient", mc_dir]
        if source == "bmclapi":
            command += ["--mirror", MAVEN_MIRROR]
        log("运行 Forge 安装器（要在本地打补丁，第一次比较慢）…")
        try:
            proc = subprocess.run(
                command, cwd=tmpdir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=2400, creationflags=_NO_WINDOW,
                text=True, encoding="utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"运行 Forge 安装器失败：{e}")
        output = (proc.stdout or "").strip()
        if proc.returncode != 0:
            tail = "\n".join(output.splitlines()[-10:])
            raise RuntimeError(f"Forge 安装器返回错误（代码 {proc.returncode}）：\n{tail}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    installed = f"{mc_version}-forge-{loader_version}"
    version_json = Path(mc_dir) / "versions" / installed / f"{installed}.json"
    if not version_json.is_file():
        raise RuntimeError("Forge 安装器执行完了，但没有生成版本文件")
    return installed


# ---------------------------------------------------------------- NeoForge 安装
def _ensure_launcher_profiles(minecraft_dir: str):
    path = Path(minecraft_dir) / "launcher_profiles.json"
    if path.is_file():
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "profiles": {},
            "selectedProfile": "",
            "clientToken": "",
            "authenticationDatabase": {},
            "launcherVersion": {"name": "XueYuan Launcher", "format": 21},
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def install_neoforge(mc_version, minecraft_dir, loader_version, java, source="official",
                     callback=None):
    """NeoForge 安装：下载 installer 并执行 --install-client"""
    log = (lambda text: callback({"type": "log", "line": text})) if callback else (lambda text: None)
    mc_dir = str(minecraft_dir)
    if not loader_version:
        versions = neoforge_versions(mc_version, source)
        if not versions:
            hint = ""
            if mc_version == "1.20.1":
                hint = ("\n提示：NeoForge 1.20.1 的安装器只发布在官方 maven"
                        "（maven.neoforged.net），国内网络经常连不上。"
                        "1.20.1 建议改用 Forge，或升级到 1.20.2+ 再装 NeoForge。")
            raise RuntimeError(f"NeoForge 没有支持 Minecraft {mc_version} 的版本{hint}")
        loader_version = versions[0]

    _ensure_launcher_profiles(mc_dir)
    url = neoforge_installer_url(loader_version, source)
    tmpdir = tempfile.mkdtemp(prefix="neoforge_")
    installer = os.path.join(tmpdir, "neoforge-installer.jar")
    try:
        log(f"下载 NeoForge 安装器 {loader_version}…")
        downloader.download_file(url, installer, timeout=300, retries=2)
        log("运行 NeoForge 安装器（需要一点时间）…")
        try:
            proc = subprocess.run(
                [java or "java", "-jar", installer, "--install-client", mc_dir],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                cwd=tmpdir, timeout=1800, creationflags=_NO_WINDOW,
                text=True, encoding="utf-8", errors="replace",
            )
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"运行 NeoForge 安装器失败：{e}")
        output = (proc.stdout or "").strip()
        if proc.returncode != 0:
            tail = "\n".join(output.splitlines()[-8:])
            raise RuntimeError(f"NeoForge 安装器返回错误（代码 {proc.returncode}）：\n{tail}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    installed = f"neoforge-{loader_version}"
    if not downloader.is_installed(installed, mc_dir):
        # 少数版本生成的目录名不同，扫一遍找 neoforge 的版本
        for item in downloader.get_installed_versions(mc_dir):
            if item["id"].startswith("neoforge-") and loader_version in item["id"]:
                installed = item["id"]
                break
        else:
            raise RuntimeError(
                "NeoForge 安装器执行完了，但没有找到生成的版本文件。\n"
                "请确认游戏目录正确，或改用官方源重试。")
    return installed

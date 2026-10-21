# -*- coding: utf-8 -*-
"""下载引擎：版本清单、镜像源 URL 改写、版本安装、资源下载

支持三种下载源：
  official - Mojang 官方（默认，最稳，符合规范）
  bmclapi  - BMCLAPI 国内镜像
  mcbbs    - MCBBS 镜像（实验性）
"""
import hashlib
import json
import os
import threading
import time
from pathlib import Path

import requests
import minecraft_launcher_lib.install as mll_install
import minecraft_launcher_lib.utils as mll_utils

# ---------------------------------------------------------------- 源定义
SOURCES = {
    "official": {
        "name": "Mojang 官方",
        "manifest": "https://piston-meta.mojang.com/mc/game/version_manifest.json",
        "rewrite": [],
    },
    "bmclapi": {
        "name": "BMCLAPI 镜像",
        "manifest": "https://bmclapi2.bangbang93.com/mc/game/version_manifest.json",
        "rewrite": [
            ("https://resources.download.minecraft.net", "https://bmclapi2.bangbang93.com/assets"),
            ("https://launcher.mojang.com", "https://bmclapi2.bangbang93.com"),
            ("https://piston-data.mojang.com", "https://bmclapi2.bangbang93.com"),
            ("https://piston-meta.mojang.com", "https://bmclapi2.bangbang93.com"),
            ("https://launchermeta.mojang.com", "https://bmclapi2.bangbang93.com"),
            ("https://libraries.minecraft.net", "https://bmclapi2.bangbang93.com/libraries"),
        ],
    },
    "mcbbs": {
        "name": "MCBBS 镜像",
        "manifest": "https://download.mcbbs.net/mc/game/version_manifest.json",
        "rewrite": [
            ("https://resources.download.minecraft.net", "https://download.mcbbs.net/assets"),
            ("https://launcher.mojang.com", "https://download.mcbbs.net"),
            ("https://piston-data.mojang.com", "https://download.mcbbs.net"),
            ("https://piston-meta.mojang.com", "https://download.mcbbs.net"),
            ("https://launchermeta.mojang.com", "https://download.mcbbs.net"),
            ("https://libraries.minecraft.net", "https://download.mcbbs.net/libraries"),
        ],
    },
}

ASSETS_BASE_OFFICIAL = "https://resources.download.minecraft.net"
_MANIFEST_CACHE = {}
_MANIFEST_CACHE_TIME = {}


def get_source(source: str) -> dict:
    return SOURCES.get(source, SOURCES["official"])


def rewrite_url(url: str, source: str) -> str:
    """按下载源改写 URL"""
    src = get_source(source)
    if not src["rewrite"] or not url:
        return url
    for old, new in src["rewrite"]:
        if url.startswith(old):
            return new + url[len(old):]
    return url


# ---------------------------------------------------------------- 基础下载
def _sha1(path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def download_file(url, path, sha1=None, callback=None, overwrite=False, timeout=30):
    """下载单个文件：已存在且校验通过则跳过；带进度回调"""
    path = Path(path)
    if path.exists() and not overwrite:
        if sha1 is None or _sha1(str(path)) == sha1:
            return True
        try:
            path.unlink()
        except Exception:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    if callback:
        callback({"type": "download", "url": url, "file": path.name, "status": "开始下载"})
    try:
        with requests.get(url, stream=True, timeout=timeout) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            done = 0
            with open(path, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    if callback and total:
                        callback({"type": "download_progress", "file": path.name,
                                  "done": done, "total": total})
    except Exception as e:
        hint = ""
        if "bmclapi" in url or "mcbbs" in url:
            hint = "（镜像源似乎不可用，请到「设置」切换为 Mojang 官方源重试）"
        raise RuntimeError(f"下载失败 {url}: {e}{hint}")
    if sha1 and _sha1(str(path)) != sha1:
        raise RuntimeError(f"校验失败 {path.name}")
    return True


def fetch_json(url, timeout=15, retries=2):
    """拉取 JSON，带重试"""
    for i in range(retries + 1):
        try:
            r = requests.get(url, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if i == retries:
                raise RuntimeError(f"网络请求失败 {url}: {e}")
            time.sleep(1.0)


# ---------------------------------------------------------------- 版本清单
def get_version_list(source="official", force=False):
    """官方/镜像版本清单，10 分钟缓存"""
    key = source
    now = time.time()
    if not force and key in _MANIFEST_CACHE and now - _MANIFEST_CACHE_TIME.get(key, 0) < 600:
        return _MANIFEST_CACHE[key]
    url = get_source(source)["manifest"]
    data = fetch_json(url)
    versions = []
    for v in data.get("versions", []):
        versions.append({
            "id": v.get("id", ""),
            "type": v.get("type", "release"),
            "releaseTime": v.get("releaseTime", ""),
        })
    _MANIFEST_CACHE[key] = versions
    _MANIFEST_CACHE_TIME[key] = now
    return versions


def get_installed_versions(minecraft_dir) -> list:
    """扫描已安装版本"""
    out = []
    vdir = Path(minecraft_dir) / "versions"
    if vdir.exists():
        for p in vdir.iterdir():
            if p.is_dir() and (p / f"{p.name}.json").exists():
                try:
                    j = json.loads((p / f"{p.name}.json").read_text(encoding="utf-8"))
                    out.append({
                        "id": p.name,
                        "type": j.get("type", "unknown"),
                        "mainClass": j.get("mainClass", ""),
                        "inheritsFrom": j.get("inheritsFrom", ""),
                        "installed": True,
                    })
                except Exception:
                    out.append({"id": p.name, "type": "unknown", "installed": True})
    out.sort(key=lambda x: x["id"], reverse=True)
    return out


def is_installed(version_id, minecraft_dir) -> bool:
    p = Path(minecraft_dir) / "versions" / version_id / f"{version_id}.json"
    return p.exists()


# ---------------------------------------------------------------- 安装
def make_callback(raw_callback=None):
    """把前端回调包装成 minecraft-launcher-lib 的 CallbackDict"""
    cb = {}
    if raw_callback:
        cb["setStatus"] = lambda s: raw_callback({"type": "status", "status": s})
        cb["setProgress"] = lambda p: raw_callback({"type": "progress", "progress": p})
        cb["setMax"] = lambda m: raw_callback({"type": "max", "max": m})
    return cb


def install_version(version_id, minecraft_dir, source="official", callback=None):
    """安装一个版本（含依赖库、资源、游戏本体）"""
    minecraft_dir = str(minecraft_dir)
    src = get_source(source)
    cb = make_callback(callback)
    if callback:
        callback({"type": "status", "status": f"开始安装 {version_id}（{src['name']}）"})
    if source == "official":
        mll_install.install_minecraft_version(version_id, minecraft_dir, cb)
    else:
        _install_via_mirror(version_id, minecraft_dir, source, callback)
    if callback:
        callback({"type": "installed", "version": version_id})


def _install_via_mirror(version_id, minecraft_dir, source, callback):
    """镜像安装：拉取镜像清单与版本 json，改写 URL 后自行编排下载"""
    manifest = get_version_list(source)
    entry = next((v for v in manifest if v["id"] == version_id), None)
    if not entry:
        raise RuntimeError(f"镜像源里没有版本 {version_id}")
    # 版本 json 的下载地址（清单里可能已是镜像地址，统一再改写一遍保险）
    vurl = rewrite_url(entry.get("url", ""), source)
    if not vurl:
        raise RuntimeError(f"版本 {version_id} 缺少下载地址")

    data = fetch_json(vurl)
    vdir = Path(minecraft_dir) / "versions" / version_id
    vdir.mkdir(parents=True, exist_ok=True)

    # 改写 json 内容里的所有下载地址
    _rewrite_json_urls(data, source)

    # 处理继承（Forge 等加载器版本）
    if data.get("inheritsFrom"):
        parent = data["inheritsFrom"]
        if callback:
            callback({"type": "status", "status": f"需要先安装父版本 {parent}"})
        if not is_installed(parent, minecraft_dir):
            install_version(parent, minecraft_dir, source, callback)
        data = mll_install.inherit_json(data, minecraft_dir)

    # 写版本 json（改写后的）
    (vdir / f"{version_id}.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")

    if callback:
        callback({"type": "status", "status": "下载依赖库…"})
    mll_install.install_libraries(data.get("id", version_id), data.get("libraries", []),
                                  minecraft_dir, make_callback(callback))

    if callback:
        callback({"type": "status", "status": "下载资源文件…"})
    _install_assets_mirror(data, minecraft_dir, source, callback)

    # 日志配置
    logging_cfg = data.get("logging", {})
    if logging_cfg and logging_cfg.get("client", {}).get("file", {}).get("url"):
        lf = logging_cfg["client"]["file"]
        log_path = Path(minecraft_dir) / "assets" / "log_configs" / lf.get("id", "client.xml")
        download_file(rewrite_url(lf["url"], source), log_path, sha1=lf.get("sha1"), callback=callback)

    # 游戏本体 jar
    dl = data.get("downloads", {})
    if dl.get("client", {}).get("url"):
        jar_path = vdir / f"{version_id}.jar"
        download_file(rewrite_url(dl["client"]["url"], source), jar_path,
                      sha1=dl["client"].get("sha1"), callback=callback)


def _rewrite_json_urls(data, source):
    """递归改写 json 里的字符串 URL"""
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, str) and v.startswith("http"):
                data[k] = rewrite_url(v, source)
            else:
                _rewrite_json_urls(v, source)
    elif isinstance(data, list):
        for item in data:
            _rewrite_json_urls(item, source)


def _install_assets_mirror(data, minecraft_dir, source, callback):
    """镜像资源安装：下载 assetIndex，再并发下载所有 objects"""
    ai = data.get("assetIndex") or {}
    index_name = ai.get("id") or "legacy"
    index_path = Path(minecraft_dir) / "assets" / "indexes" / f"{index_name}.json"
    if ai.get("url"):
        download_file(rewrite_url(ai["url"], source), index_path,
                      sha1=ai.get("sha1"), callback=callback)
    if not index_path.exists():
        return
    index = json.loads(index_path.read_text(encoding="utf-8"))
    objects = index.get("objects", {})
    base = ASSETS_BASE_OFFICIAL if source == "official" else get_source(source)["rewrite"][0][1]

    items = []
    for key, obj in objects.items():
        h = obj.get("hash", "")
        if not h:
            continue
        rel = f"{h[:2]}/{h}"
        dest = Path(minecraft_dir) / "assets" / "objects" / rel
        if dest.exists():
            try:
                if _sha1(str(dest)) == h:
                    continue
            except Exception:
                pass
        items.append((f"{base}/{rel}", dest, h))

    total = len(items)
    if callback:
        callback({"type": "assets_start", "total": total})
    ok, fail = 0, 0
    for i, (url, dest, h) in enumerate(items):
        try:
            download_file(url, dest, sha1=h)
            ok += 1
        except Exception:
            fail += 1
        if callback and (i % 50 == 0 or i == total - 1):
            callback({"type": "assets_progress", "done": i + 1, "total": total, "ok": ok, "fail": fail})
    if fail and callback:
        callback({"type": "status", "status": f"资源下载完成：成功 {ok}，失败 {fail}（失败的可在下次启动时自动补齐）"})

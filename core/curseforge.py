# -*- coding: utf-8 -*-
"""CurseForge 整合包模组下载（不需要 API Key）

思路：
1. 整合包 manifest 里只有 projectID / fileID，没有文件名；
2. 用 cfwidget 的公开项目接口拿到该项目的文件列表（id -> 文件名）；
3. 用 CurseForge 官方 CDN 规则拼直链下载：
   https://mediafilez.forgecdn.net/files/<fileID//1000>/<fileID%1000三位>/<文件名>

另外提供 modid 去重：整合包 overrides 里自带的 mod 和 CF 下载的 mod 可能重复，
Forge 遇到重复 modid 会直接崩，这里把重复的移到 mods-disabled/ 目录。
"""
import json
import re
import shutil
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import downloader

CFWIDGET = "https://api.cfwidget.com/{project_id}"
CDN_HOSTS = ("https://edge.forgecdn.net", "https://mediafilez.forgecdn.net")
CDN_PATH = "files/{major}/{minor:03d}/{name}"
PROJECT_PAGE = "https://www.curseforge.com/projects/{project_id}"

_meta_lock = threading.Lock()
_meta_cache = {}          # project_id -> (files dict, title)
_CACHE_TTL = 7 * 86400    # 项目文件列表缓存 7 天


class CurseForgeError(Exception):
    pass


# ---------------------------------------------------------------- 元数据
def _cache_file(minecraft_dir, project_id) -> Path:
    folder = Path(minecraft_dir) / "launchermeta" / "curseforge"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{project_id}.json"


def _fetch_project(minecraft_dir, project_id, timeout=30):
    """返回 ({file_id: (name, size)}, 原始文件列表, 项目名)"""
    key = int(project_id)
    with _meta_lock:
        cached = _meta_cache.get(key)
    if cached:
        return cached

    data = None
    cache_path = _cache_file(minecraft_dir, key)
    try:
        if cache_path.is_file() and time.time() - cache_path.stat().st_mtime < _CACHE_TTL:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        data = None

    if data is None:
        last_error = None
        for _ in range(3):
            try:
                r = downloader._session().get(
                    CFWIDGET.format(project_id=key),
                    timeout=(10, timeout),
                    headers={"Accept": "application/json",
                             "User-Agent": downloader.USER_AGENT})
                r.raise_for_status()
                data = r.json()
                break
            except Exception as e:  # noqa: BLE001
                last_error = e
                time.sleep(1.0)
        if data is None:
            raise CurseForgeError(f"查询 CurseForge 项目 {key} 失败：{last_error}")
        try:
            cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    raw_files = data.get("files") or []
    files = {}
    for item in raw_files:
        fid = item.get("id")
        name = item.get("name") or item.get("display")
        if fid and name:
            files[int(fid)] = (name, item.get("filesize"))
    result = (files, raw_files, data.get("title") or "")
    with _meta_lock:
        _meta_cache[key] = result
    return result


def _fallback_file(raw_files, mc_version):
    """manifest 指定的文件没了：找同 MC 版本里最新的那个"""
    if not mc_version:
        return None
    candidates = []
    for item in raw_files:
        versions = item.get("versions") or []
        name = item.get("name") or item.get("display")
        if not name or mc_version not in versions:
            continue
        if "beta" in (item.get("type") or ""):
            continue
        candidates.append((int(item.get("id") or 0), name, item.get("filesize")))
    if not candidates:
        return None
    return max(candidates, key=lambda c: c[0])


def _cdn_urls(file_id, name):
    """CurseForge CDN 直链（两个域名都试：mediafilez 偶尔会 403，edge 一般可用）"""
    fid = int(file_id)
    rel = CDN_PATH.format(major=fid // 1000, minor=fid % 1000, name=name)
    return [f"{host}/{rel}" for host in CDN_HOSTS]


# ---------------------------------------------------------------- 下载
def install_mods(entries, minecraft_dir, callback=None, workers=4, mc_version=""):
    """entries: [(projectID, fileID)]；返回统计结果"""
    mc_dir = str(minecraft_dir)
    mods_dir = Path(mc_dir) / "mods"
    mods_dir.mkdir(parents=True, exist_ok=True)

    emit = (lambda payload: callback(payload)) if callback else (lambda payload: None)
    total = len(entries)
    emit({"type": "mods_start", "total": total})
    if not total:
        return {"resolved": 0, "downloaded": 0, "skipped": 0, "failed": [], "names": []}

    # 1. 解析文件名
    resolved = {}      # (pid, fid) -> (name, size, 实际用于下载的 file_id)
    failed = []
    fallbacks = []
    done = 0
    lock = threading.Lock()

    def resolve(item):
        pid, fid = item
        try:
            files, raw_files, _title = _fetch_project(mc_dir, pid)
            name, size = files.get(int(fid), (None, None))
            if name:
                return item, (name, size, int(fid)), None
            # manifest 指定的文件已失效：退而求其次用同版本最新文件
            alt = _fallback_file(raw_files, mc_version)
            if alt:
                return item, (alt[1], alt[2], int(alt[0])), None
            raise CurseForgeError(f"项目里找不到文件 {fid}（可能已被作者删除）")
        except Exception as e:  # noqa: BLE001
            return item, None, str(e)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(resolve, item) for item in entries]
        for future in futures:
            item, value, error = future.result()
            done += 1
            if value:
                resolved[item] = (value[0], value[1], value[2])
                if value[2] != int(item[1]):
                    fallbacks.append((item[0], item[1], value[0]))
            else:
                failed.append((item[0], item[1], error))
            if done % 10 == 0 or done == total:
                emit({"type": "mods_progress", "phase": "resolve",
                      "done": done, "total": total, "ok": len(resolved),
                      "fail": len(failed)})

    for pid, fid, name in fallbacks:
        emit({"type": "log",
              "line": f"⚠ 项目 {pid} 的文件 {fid} 已失效，改用同版本最新文件 {name}"})

    # 2. 下载
    to_download = []
    skipped = 0
    for (pid, fid), (name, size, cdn_fid) in resolved.items():
        target = mods_dir / name
        if target.is_file():
            skipped += 1
            continue
        to_download.append((pid, cdn_fid, name, size))

    downloaded = 0
    done = 0
    dl_lock = threading.Lock()

    def download_one(task):
        pid, fid, name, size = task
        target = mods_dir / name
        last_error = None
        for url in _cdn_urls(fid, name):
            try:
                downloader.download_file(url, target, timeout=300, retries=2)
                return name, None
            except Exception as e:  # noqa: BLE001
                last_error = e
                # 403/404 时换下一个 CDN 域名再试
                try:
                    target.unlink()
                except Exception:
                    pass
        return name, str(last_error)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(download_one, task) for task in to_download]
        for future in futures:
            name, error = future.result()
            done += 1
            if error:
                failed.append(("", "", f"{name}: {error}"))
            else:
                with dl_lock:
                    downloaded += 1
            if done % 10 == 0 or done == len(to_download):
                emit({"type": "mods_progress", "phase": "download",
                      "done": done, "total": len(to_download),
                      "ok": downloaded, "fail": len(failed), "name": name or ""})

    emit({"type": "mods_done", "total": total, "downloaded": downloaded,
          "skipped": skipped, "failed": len(failed)})
    return {"resolved": len(resolved), "downloaded": downloaded,
            "skipped": skipped, "failed": failed,
            "names": [v[0] for v in resolved.values()]}


# ---------------------------------------------------------------- modid 去重
_IGNORED_MOD_IDS = {"forge", "minecraft", "neoforge", "java", "fml"}

_MOD_ID_LINE = re.compile(r'modId\s*=\s*["\']([^"\']+)["\']')


def _toml_mod_ids(text):
    """只取 [[mods]] 段里的 modId，别把 [[dependencies.xxx]] 里的依赖当成自己的 id"""
    ids = set()
    in_mods = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            # 段头可能带行内注释，例如： [[mods]] #mandatory
            header = line.split("#", 1)[0].strip()
            if header.startswith("[[") and header.endswith("]]"):
                in_mods = header[2:-2].strip().lower() == "mods"
            else:
                in_mods = False
            continue
        if in_mods:
            match = _MOD_ID_LINE.search(line)
            if match:
                ids.add(match.group(1).strip().lower())
    return ids


def _mod_ids(jar_path):
    ids = set()
    try:
        with zipfile.ZipFile(jar_path) as zf:
            names = set(zf.namelist())
            for meta in ("META-INF/mods.toml", "META-INF/neoforge.mods.toml"):
                if meta in names:
                    try:
                        ids |= _toml_mod_ids(zf.read(meta).decode("utf-8", "replace"))
                    except Exception:
                        pass
            for meta in ("fabric.mod.json", "quilt.mod.json"):
                if meta not in names:
                    continue
                try:
                    data = json.loads(zf.read(meta).decode("utf-8", "replace"))
                except Exception:
                    continue
                if not isinstance(data, dict):
                    continue
                if isinstance(data.get("id"), str):
                    ids.add(data["id"].strip().lower())
                loader = data.get("quilt_loader")
                if isinstance(loader, dict) and isinstance(loader.get("id"), str):
                    ids.add(loader["id"].strip().lower())
    except Exception:
        pass
    ids = {i for i in ids if i and not any(ch in i for ch in "${}()")}
    return ids - _IGNORED_MOD_IDS


def dedupe_mods(minecraft_dir, preferred_names=None, callback=None):
    """同一 modid 只保留一个 jar；优先保留整合包 manifest 指定的文件"""
    mc_dir = str(minecraft_dir)
    mods_dir = Path(mc_dir) / "mods"
    if not mods_dir.is_dir():
        return []
    preferred = {n.lower() for n in (preferred_names or [])}
    disabled = Path(mc_dir) / "mods-disabled"
    owners = {}
    moved = []
    for jar in sorted(mods_dir.glob("*.jar")):
        ids = _mod_ids(jar)
        if not ids:
            continue
        conflict = None
        for mod_id in ids:
            owner = owners.get(mod_id)
            if owner is not None and owner != jar:
                conflict = owner
                break
        if conflict is None:
            for mod_id in ids:
                owners.setdefault(mod_id, jar)
            continue
        # 决定保留谁：优先 manifest 里指定的那个
        keep_new = (jar.name.lower() in preferred) and (conflict.name.lower() not in preferred)
        loser = conflict if keep_new else jar
        winner = jar if keep_new else conflict
        try:
            disabled.mkdir(parents=True, exist_ok=True)
            shutil.move(str(loser), str(disabled / loser.name))
            moved.append(loser.name)
            for mod_id, owner in list(owners.items()):
                if owner == loser:
                    owners.pop(mod_id, None)
            for mod_id in ids:
                owners.setdefault(mod_id, winner)
            if callback:
                callback({"type": "log",
                          "line": f"⚠ 重复模组（{', '.join(sorted(ids))}）："
                                  f"{loser.name} 已移到 mods-disabled/"})
        except Exception:
            pass
    return moved

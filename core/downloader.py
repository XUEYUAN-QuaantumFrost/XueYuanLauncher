# -*- coding: utf-8 -*-
"""下载引擎：版本清单、镜像源 URL 改写、版本安装、资源下载

支持三种下载源：
  official - Mojang 官方（默认，最稳）
  bmclapi  - BMCLAPI 国内镜像
  mcbbs    - MCBBS 镜像（实验性）

相比 v1.3 的改进：
- 所有请求带 UA + 超时 + 自动重试，慢网络不会"卡死无响应"
- 断点续传（先写 .part 文件，完整校验通过后才改名），中断后重试可接着下
- sha1 校验失败会自动删掉重下，而不是留下坏文件让游戏启动时报错
- 依赖库 / 资源文件并发下载，进度事件做了节流，界面不会被刷爆
- 完整支持 natives 解压、旧版资源虚拟目录（assets/virtual、resources），
  1.6 以前的老版本也能启动
- 依赖库下载失败会明确报错并提示换源，不再"假装安装成功"
"""
import hashlib
import json
import lzma
import os
import platform
import re
import shutil
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
import minecraft_launcher_lib.install as mll_install
import minecraft_launcher_lib.runtime as mll_runtime

USER_AGENT = "XueYuanLauncher/1.4.0 (Minecraft Java Launcher)"

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
_cache_lock = threading.Lock()

# 每个线程一个 Session：复用连接，避免并发时共享 Session 出问题
_tls = threading.local()


def _session() -> requests.Session:
    s = getattr(_tls, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Encoding": "identity",  # 便于用 content-length 算进度
        })
        adapter = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=16, max_retries=0)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _tls.session = s
    return s


# ---------------------------------------------------------------- 源工具
def get_source(source: str) -> dict:
    return SOURCES.get(source, SOURCES["official"])


def rewrite_url(url: str, source: str) -> str:
    """按下载源改写 URL"""
    if not url:
        return url
    src = get_source(source)
    for old, new in src["rewrite"]:
        if url.startswith(old):
            return new + url[len(old):]
    return url


def _mirror_hint(url: str) -> str:
    if "bmclapi" in url or "mcbbs" in url:
        return "（镜像源可能不可用，可到「设置」切换为 Mojang 官方源重试）"
    return "（可到「设置」切换为 BMCLAPI 镜像后重试）"


# ---------------------------------------------------------------- 基础工具
def _sha1(path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _emit(callback, payload):
    if callback:
        try:
            callback(payload)
        except Exception:
            pass


def _ensure_dir(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- 单文件下载
def download_file(url, path, sha1=None, callback=None, overwrite=False,
                  timeout=60, retries=2) -> bool:
    """下载单个文件：已存在且校验通过则跳过；支持断点续传、重试、sha1 校验。

    返回 True 表示文件已就绪（新下载或本来就完整）。
    """
    path = Path(path)
    part = path.with_name(path.name + ".part")

    if path.exists() and not overwrite:
        try:
            if sha1 is None or _sha1(str(path)) == sha1:
                return True
        except Exception:
            pass
        # 文件存在但内容不对：删掉重下，避免把坏文件留给游戏
        try:
            path.unlink()
        except Exception:
            pass

    _ensure_dir(path)
    _emit(callback, {"type": "download", "url": url, "file": path.name,
                     "status": "开始下载"})
    last_error = None
    for attempt in range(retries + 1):
        try:
            _fetch_to(url, part, timeout=timeout, callback=callback)
            if sha1:
                actual = _sha1(str(part))
                if actual != sha1:
                    # 内容不对：删掉，下一次重新整份下载
                    try:
                        part.unlink()
                    except Exception:
                        pass
                    raise RuntimeError("SHA-1 校验失败")
            os.replace(str(part), str(path))
            return True
        except Exception as e:  # noqa: BLE001 - 需要把各种网络错误统一转成中文提示
            last_error = e
            _emit(callback, {"type": "log", "line": f"下载重试（{attempt + 1}/{retries + 1}）：{path.name} - {e}"})
            if attempt < retries:
                time.sleep(min(2.0, 0.8 * (attempt + 1)))
    raise RuntimeError(f"下载失败 {url}: {last_error}{_mirror_hint(url)}")


def _fetch_to(url, part: Path, timeout=60, callback=None):
    """把 url 写进 part 文件；part 已存在时用 Range 续传"""
    existing = part.stat().st_size if part.exists() else 0
    headers = {}
    if existing > 0:
        headers["Range"] = f"bytes={existing}-"
    rate_last = [0.0]

    with _session().get(url, stream=True, timeout=(15, timeout), headers=headers) as r:
        if r.status_code == 416 and existing > 0:
            # 服务器认为已经下完；截断重来最稳
            try:
                part.unlink()
            except Exception:
                pass
            raise RuntimeError("续传位置无效，重新下载")
        r.raise_for_status()
        resuming = r.status_code == 206 and existing > 0
        remaining = int(r.headers.get("content-length") or 0)
        total = remaining + (existing if resuming else 0)
        done = existing if resuming else 0
        mode = "ab" if resuming else "wb"
        with open(part, mode) as f:
            for chunk in r.iter_content(1 << 16):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                now = time.time()
                if callback and total and (now - rate_last[0] > 0.2 or done >= total):
                    rate_last[0] = now
                    _emit(callback, {"type": "download_progress", "file": part.name,
                                     "done": done, "total": total})
        if total and done < total:
            raise RuntimeError(f"下载不完整（{done}/{total} 字节）")


def fetch_json(url, timeout=30, retries=3, callback=None):
    """拉取 JSON，带超时和重试；失败时给出可读的中文错误"""
    last = None
    for i in range(retries + 1):
        try:
            r = _session().get(url, timeout=(15, timeout),
                               headers={"Accept": "application/json"})
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries:
                time.sleep(0.8 * (i + 1))
    raise RuntimeError(f"网络请求失败 {url}: {last}{_mirror_hint(url)}")


# ---------------------------------------------------------------- 版本清单
def get_version_list(source="official", force=False):
    """官方/镜像版本清单，10 分钟缓存"""
    key = source
    now = time.time()
    with _cache_lock:
        if (not force and key in _MANIFEST_CACHE
                and now - _MANIFEST_CACHE_TIME.get(key, 0) < 600):
            return _MANIFEST_CACHE[key]

    url = get_source(source)["manifest"]
    data = fetch_json(url)
    versions = []
    for v in data.get("versions", []):
        versions.append({
            "id": v.get("id", ""),
            "type": v.get("type", "release"),
            "releaseTime": v.get("releaseTime", ""),
            "url": v.get("url", ""),   # 版本 json 的下载地址，镜像安装必需
        })
    with _cache_lock:
        _MANIFEST_CACHE[key] = versions
        _MANIFEST_CACHE_TIME[key] = now
    return versions


def _find_manifest_entry(version_id, source):
    for entry in get_version_list(source):
        if entry.get("id") == version_id:
            return entry
    # 缓存里没有（比如镜像清单过期）：强制刷新一次
    for entry in get_version_list(source, force=True):
        if entry.get("id") == version_id:
            return entry
    return None


def get_installed_versions(minecraft_dir) -> list:
    """扫描已安装版本"""
    out = []
    if not minecraft_dir:
        return out
    vdir = Path(minecraft_dir) / "versions"
    if not vdir.is_dir():
        return out
    for p in vdir.iterdir():
        json_path = p / f"{p.name}.json"
        if not (p.is_dir() and json_path.exists()):
            continue
        info = {"id": p.name, "type": "unknown", "inheritsFrom": "", "installed": True}
        try:
            j = json.loads(json_path.read_text(encoding="utf-8"))
            info["type"] = j.get("type", "unknown")
            info["inheritsFrom"] = j.get("inheritsFrom", "")
            info["releaseTime"] = j.get("releaseTime", "")
        except Exception:
            pass
        out.append(info)
    out.sort(key=lambda x: x["id"], reverse=True)
    return out


def is_installed(version_id, minecraft_dir) -> bool:
    if not minecraft_dir:
        return False
    p = Path(minecraft_dir) / "versions" / version_id / f"{version_id}.json"
    return p.exists()


def version_java_component(version_id, minecraft_dir):
    """读取版本（含继承链）需要哪个 Java 运行时组件，读不到返回空串"""
    seen = set()
    current = version_id
    while current and current not in seen:
        seen.add(current)
        p = Path(minecraft_dir) / "versions" / current / f"{current}.json"
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return ""
        java_version = data.get("javaVersion") or {}
        if java_version.get("component"):
            return java_version["component"]
        current = data.get("inheritsFrom")
    return ""


def missing_libraries(version_id, minecraft_dir) -> list:
    """检查一个版本（含父版本）的依赖库是否齐全，返回缺失的本地路径列表。

    Forge 这类加载器的 srg / extra / client jar 是安装器生成的，
    如果生成步骤中断，版本看起来"装好了"，启动却会直接崩，所以装完要检查一遍。
    """
    mc_dir = str(minecraft_dir or "")
    if not mc_dir:
        return []
    json_path = Path(mc_dir) / "versions" / version_id / f"{version_id}.json"
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if data.get("inheritsFrom"):
        try:
            data = mll_install.inherit_json(data, mc_dir)
        except Exception:
            pass
    missing = []
    for lib in data.get("libraries") or []:
        try:
            for task in _library_tasks(lib, mc_dir, version_id):
                dest = Path(task[1])
                if not dest.is_file():
                    missing.append(str(dest))
        except Exception:
            continue
    return missing


# ---------------------------------------------------------------- 安装回调
def make_callback(raw_callback=None):
    """把前端回调包装成 minecraft-launcher-lib 的 CallbackDict"""
    cb = {}
    if raw_callback:
        cb["setStatus"] = lambda s: raw_callback({"type": "status", "status": s})
        cb["setProgress"] = lambda p: raw_callback({"type": "progress", "progress": p})
        cb["setMax"] = lambda m: raw_callback({"type": "max", "max": m})
    return cb


# ---------------------------------------------------------------- 平台规则
def _platform_os() -> str:
    s = platform.system().lower()
    return "osx" if s == "darwin" else s


def _arch_bits() -> str:
    return "32" if platform.architecture()[0] == "32bit" else "64"


def _os_version() -> str:
    if platform.system() == "Windows":
        try:
            ver = sys.getwindowsversion()
            return f"{ver.major}.{ver.minor}"
        except Exception:
            return "10.0"
    if platform.system() == "Darwin":
        return platform.mac_ver()[0]
    return platform.uname().release


def _rule_pass(rule: dict) -> bool:
    """单条规则的判定（沿用官方启动器的做法：不匹配的规则"不反对"）"""
    action_allow = rule.get("action") == "allow"
    not_applicable = not action_allow  # allow 规则不匹配 → 不放行；disallow 规则不匹配 → 放行
    os_rule = rule.get("os") or {}
    cur_os = _platform_os()
    is64 = _arch_bits() == "64"

    name = os_rule.get("name")
    if name and name != cur_os:
        return not_applicable
    arch = os_rule.get("arch")
    if arch == "x86" and is64:
        return not_applicable
    if arch == "x86_64" and not is64:
        return not_applicable
    if arch == "arm64" and platform.machine().lower() not in ("arm64", "aarch64"):
        return not_applicable
    version = os_rule.get("version")
    if version:
        try:
            if not re.match(version, _os_version()):
                return not_applicable
        except re.error:
            return not_applicable
    if rule.get("features"):
        # 安装 / 启动阶段没有 demo、quickPlay 这些特性
        return not_applicable
    return action_allow


def _rules_allowed(lib: dict) -> bool:
    """按 rules 判断当前平台是否需要该库；没有 rules 视为需要"""
    rules = lib.get("rules")
    if not rules:
        return True
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if not _rule_pass(rule):
            return False
    return True


def _native_key(lib: dict) -> str:
    """返回当前平台的 natives 标识，如 natives-windows / natives-windows-x86_64"""
    natives = lib.get("natives") or {}
    value = natives.get(_platform_os()) or ""
    return value.replace("${arch}", _arch_bits())


def _extract_natives(jar: Path, dest_dir: Path, exclude):
    """把 natives jar 里除 META-INF 等之外的文件解压到 dest_dir"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(jar)) as zf:
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            if any(name.startswith(prefix) for prefix in (exclude or [])):
                continue
            target = dest_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)


# ---------------------------------------------------------------- 依赖库
def _library_tasks(lib: dict, minecraft_dir: str, version_id: str):
    """把一条 library 转成待下载任务 [(url, 本地路径, sha1, 解压目录或 None, extract 配置)]"""
    if not _rules_allowed(lib):
        return []
    name = lib.get("name", "")
    parts = name.split(":")
    if len(parts) < 3:
        return []
    pkg, lib_name, version = parts[0], parts[1], parts[2]
    ext = "jar"
    if "@" in version:
        version, ext = version.split("@", 1)

    tasks = []
    downloads = lib.get("downloads")
    artifact = (downloads or {}).get("artifact") or {}
    lib_dir = Path(minecraft_dir) / "libraries"
    rel_dir = Path(*pkg.split(".")) / lib_name / version
    default_dest = lib_dir / rel_dir / f"{lib_name}-{version}.{ext}"
    base_url = (lib.get("url") or "https://libraries.minecraft.net").rstrip("/")
    maven_url = f"{base_url}/{'/'.join(pkg.split('.'))}/{lib_name}/{version}"

    if artifact.get("url"):
        dest = lib_dir / artifact.get("path", str(rel_dir / f"{lib_name}-{version}.{ext}"))
        tasks.append((artifact["url"], dest, artifact.get("sha1"), None, None))
    elif downloads is None:
        # 最早期的版本 json 没有 downloads 字段，按 maven 路径拼 URL
        tasks.append((f"{maven_url}/{lib_name}-{version}.{ext}", default_dest, None, None, None))
    # 只带 classifiers 的库（如 lwjgl-platform）本来就没有普通 jar，不能瞎猜 URL

    native = _native_key(lib)
    if native:
        classifiers = (downloads or {}).get("classifiers") or {}
        info = classifiers.get(native) or {}
        extract_dir = Path(minecraft_dir) / "versions" / version_id / "natives"
        if info.get("url"):
            dest = lib_dir / info.get("path", str(rel_dir / f"{lib_name}-{version}-{native}.jar"))
            tasks.append((info["url"], dest, info.get("sha1"), extract_dir, lib.get("extract")))
        elif downloads is None:
            dest = lib_dir / rel_dir / f"{lib_name}-{version}-{native}.jar"
            tasks.append((f"{maven_url}/{lib_name}-{version}-{native}.jar", dest, None,
                          extract_dir, lib.get("extract")))
        # 该平台没有发布 natives 的库（例如只给 osx 的 nightly 版本）直接跳过
    return tasks


def _install_libraries(libraries, minecraft_dir, version_id, source, callback,
                       max_workers=4):
    """并发下载依赖库；关键失败会汇总成一个明确错误抛出去"""
    tasks = []
    for lib in libraries or []:
        try:
            tasks.extend(_library_tasks(lib, minecraft_dir, version_id))
        except Exception:
            continue

    # 同一个文件可能出现在多条 library 里（旧版本 json 有重复项），去重后再下
    unique = []
    seen = set()
    for task in tasks:
        key = str(task[1]).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(task)
    tasks = unique

    total = len(tasks)
    _emit(callback, {"type": "libraries_start", "total": total})
    if not tasks:
        return

    fails = []
    lock = threading.Lock()
    done = 0

    def one(task):
        url, dest, sha1 = task[0], task[1], task[2]
        extract_dir = task[3] if len(task) > 3 else None
        extract_cfg = task[4] if len(task) > 4 else None
        try:
            download_file(rewrite_url(url, source), dest, sha1=sha1,
                          timeout=90, retries=2)
            if extract_dir is not None:
                _extract_natives(Path(dest), Path(extract_dir), extract_cfg)
        except Exception as e:  # noqa: BLE001
            with lock:
                fails.append(f"{Path(dest).name}: {str(e)[:150]}")

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(one, t) for t in tasks]
        for _ in futures:
            done += 1
            if callback and (done % 10 == 0 or done == total):
                _emit(callback, {"type": "libraries_progress", "done": done, "total": total})

    if fails:
        detail = "；".join(fails[:5])
        more = f"（另有 {len(fails) - 5} 个）" if len(fails) > 5 else ""
        raise RuntimeError(
            f"{len(fails)} 个依赖库下载失败：{detail}{more}\n"
            "可在「设置」里切换下载源后重试；已下载成功的部分不会重复下载。")


# ---------------------------------------------------------------- 资源文件
def _install_assets(data, minecraft_dir, source, callback, max_workers=8):
    """资源文件：索引 + objects；兼容旧版 virtual / map_to_resources"""
    ai = data.get("assetIndex") or {}
    if not ai.get("url"):
        return
    index_name = ai.get("id") or data.get("assets") or "legacy"
    assets_dir = Path(minecraft_dir) / "assets"
    index_path = assets_dir / "indexes" / f"{index_name}.json"
    download_file(rewrite_url(ai["url"], source), index_path,
                  sha1=ai.get("sha1"), timeout=90, callback=callback)
    if not index_path.exists():
        raise RuntimeError("资源索引下载失败")
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError(f"资源索引损坏：{e}")

    objects = index.get("objects") or {}
    base = rewrite_url(ASSETS_BASE_OFFICIAL, source)
    hashes = {}
    for key, obj in objects.items():
        h = obj.get("hash", "")
        if h:
            hashes.setdefault(h, key)

    items = list(hashes.keys())
    total = len(items)
    _emit(callback, {"type": "assets_start", "total": total})
    if not items:
        return

    ok = 0
    fail = []
    lock = threading.Lock()

    def one(h):
        nonlocal ok
        dest = assets_dir / "objects" / h[:2] / h
        try:
            download_file(f"{base}/{h[:2]}/{h}", dest, sha1=h, timeout=90, retries=2)
            with lock:
                ok += 1
        except Exception as e:  # noqa: BLE001
            with lock:
                fail.append(f"{h[:10]}…: {str(e)[:100]}")
        with lock:
            n = ok + len(fail)
            if callback and (n % 50 == 0 or n == total):
                _emit(callback, {"type": "assets_progress", "done": n, "total": total,
                                 "ok": ok, "fail": len(fail)})

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(one, h) for h in items]
        for _ in futures:
            pass

    # 旧版本（1.6 之前）需要的虚拟资源目录
    if index.get("virtual"):
        virtual_root = assets_dir / "virtual" / index_name
        for key, obj in objects.items():
            h = obj.get("hash")
            if not h:
                continue
            src = assets_dir / "objects" / h[:2] / h
            dst = virtual_root / key
            if src.exists() and not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copyfile(str(src), str(dst))
                except Exception:
                    pass
    if index.get("map_to_resources"):
        res_root = Path(minecraft_dir) / "resources"
        for key, obj in objects.items():
            h = obj.get("hash")
            if not h:
                continue
            src = assets_dir / "objects" / h[:2] / h
            dst = res_root / key
            if src.exists() and not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copyfile(str(src), str(dst))
                except Exception:
                    pass

    if fail:
        raise RuntimeError(
            f"{len(fail)} 个资源文件下载失败（成功 {ok}/{total}）：{fail[0]}\n"
            "请检查网络后重试；已下载的文件不会被重复下载。")


# ---------------------------------------------------------------- Java 运行时
def _patch_mll_downloader():
    """minecraft-launcher-lib 自带的下载器没有超时，这里换成我们的实现，
    让 Java 运行时下载同样具备超时 / 重试 / 校验能力。"""
    def _replacement(url, path, callback=None, sha1=None, lzma_compressed=False,
                     session=None, minecraft_directory=None, overwrite=False):
        target = Path(path)
        if target.exists() and not overwrite:
            try:
                if sha1 is None or _sha1(str(target)) == sha1:
                    return False
            except Exception:
                pass
        if lzma_compressed:
            tmp = target.with_name(target.name + ".lzma")
            download_file(url, tmp, timeout=180, retries=2)
            try:
                data_bytes = lzma.decompress(tmp.read_bytes())
            finally:
                try:
                    tmp.unlink()
                except Exception:
                    pass
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data_bytes)
            if sha1 and _sha1(str(target)) != sha1:
                raise RuntimeError(f"校验失败 {target.name}")
            return True
        download_file(url, target, sha1=sha1, overwrite=overwrite, timeout=180, retries=2)
        return True

    try:
        mll_runtime.download_file = _replacement
    except Exception:
        pass


_patch_mll_downloader()


def _find_runtime_java(component, minecraft_dir) -> str:
    """在游戏目录里找某个 Java 运行时（兼容 mll 不认识的旧目录布局
    runtime/<组件>/bin/java.exe）"""
    exe = "java.exe" if os.name == "nt" else "java"
    base = Path(minecraft_dir) / "runtime" / component
    try:
        for p in base.rglob(exe):
            if p.parent.name == "bin" and p.is_file():
                return str(p)
    except Exception:
        pass
    return ""


def ensure_java_runtime(component, minecraft_dir, callback=None) -> str:
    """确保 Mojang 官方 Java 运行时已安装，返回 java.exe 路径"""
    if not component:
        raise RuntimeError("该版本没有指定 Java 运行时组件")
    try:
        path = mll_runtime.get_executable_path(component, minecraft_dir)
    except Exception:
        path = None
    if path and os.path.isfile(path):
        return path
    # mll 只认新目录结构；已经装过官方旧结构运行时的机器不用再下一遍
    legacy = _find_runtime_java(component, minecraft_dir)
    if legacy:
        return legacy

    _emit(callback, {"type": "status", "status": f"正在下载 Java 运行时 {component}（约 100~200MB）…"})
    cb = make_callback(callback)
    try:
        mll_runtime.install_jvm_runtime(component, minecraft_dir, cb)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"Java 运行时 {component} 下载失败：{e}\n"
            "可以在「设置」里手动指定已安装的 Java 17/21 路径。")
    path = mll_runtime.get_executable_path(component, minecraft_dir)
    if not path or not os.path.isfile(path):
        raise RuntimeError("Java 运行时安装后未找到 java 可执行文件，请在设置里手动指定 Java 路径")
    return path


# ---------------------------------------------------------------- 安装入口
def install_version(version_id, minecraft_dir, source="official", callback=None):
    """安装一个版本（含依赖库、资源、游戏本体、需要时 Java 运行时）"""
    minecraft_dir = str(minecraft_dir or "").strip()
    if not minecraft_dir:
        raise RuntimeError("还没有设置游戏目录，请到「设置」里选择 .minecraft 目录")
    if not version_id:
        raise RuntimeError("版本号为空")
    Path(minecraft_dir).mkdir(parents=True, exist_ok=True)

    src = get_source(source)
    _emit(callback, {"type": "status",
                     "status": f"开始安装 {version_id}（{src['name']}）"})
    _install_vanilla(version_id, minecraft_dir, source, callback)
    _emit(callback, {"type": "installed", "version": version_id})


def _install_vanilla(version_id, minecraft_dir, source, callback):
    """安装编排：版本 json → 父版本 → 依赖库 → 资源 → 日志配置 → 客户端 jar → Java 运行时"""
    entry = _find_manifest_entry(version_id, source)
    if not entry:
        raise RuntimeError(f"下载源 {get_source(source)['name']} 里没有版本 {version_id}")
    vurl = entry.get("url", "")
    data = fetch_json(rewrite_url(vurl, source))

    vdir = Path(minecraft_dir) / "versions" / version_id
    vdir.mkdir(parents=True, exist_ok=True)

    # 父版本（Forge/NeoForge/Fabric 等）先装好
    if data.get("inheritsFrom"):
        parent = data["inheritsFrom"]
        if not is_installed(parent, minecraft_dir):
            _emit(callback, {"type": "status", "status": f"先安装父版本 {parent}…"})
            install_version(parent, minecraft_dir, source, callback)
        try:
            merged = mll_install.inherit_json(data, minecraft_dir)
        except Exception as e:
            raise RuntimeError(f"读取父版本 {parent} 失败：{e}")
    else:
        merged = data

    # 写回本地版本 json：保留 inheritsFrom 原始结构，启动时由库自行解析
    (vdir / f"{version_id}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    _emit(callback, {"type": "status", "status": "正在下载依赖库…"})
    _install_libraries(merged.get("libraries", []), minecraft_dir, version_id,
                       source, callback)

    _emit(callback, {"type": "status", "status": "正在下载资源文件…"})
    _install_assets(merged, minecraft_dir, source, callback)

    # 日志配置（可以让游戏把日志更好读）
    logging_cfg = merged.get("logging") or {}
    log_file = ((logging_cfg.get("client") or {}).get("file") or {}) if logging_cfg else {}
    if log_file.get("url"):
        log_path = Path(minecraft_dir) / "assets" / "log_configs" / log_file.get("id", "client.xml")
        try:
            download_file(rewrite_url(log_file["url"], source), log_path,
                          sha1=log_file.get("sha1"), timeout=60, callback=callback)
        except Exception as e:  # noqa: BLE001
            _emit(callback, {"type": "status", "status": f"日志配置下载失败（不影响启动）：{e}"})

    # 游戏本体 jar
    client = (merged.get("downloads") or {}).get("client") or {}
    jar_path = vdir / f"{version_id}.jar"
    if client.get("url"):
        _emit(callback, {"type": "status", "status": "正在下载游戏本体…"})
        download_file(rewrite_url(client["url"], source), jar_path,
                      sha1=client.get("sha1"), timeout=300, callback=callback)
    elif data.get("inheritsFrom"):
        # 部分加载器不自带 jar：从父版本复制一份过去
        parent_jar = Path(minecraft_dir) / "versions" / data["inheritsFrom"] / f"{data['inheritsFrom']}.jar"
        if parent_jar.is_file() and not jar_path.exists():
            try:
                shutil.copyfile(str(parent_jar), str(jar_path))
            except Exception:
                pass

    # Java 运行时（1.17+ 版本需要）
    java_version = merged.get("javaVersion") or {}
    if java_version.get("component"):
        try:
            ensure_java_runtime(java_version["component"], minecraft_dir, callback)
        except Exception as e:  # noqa: BLE001
            # 运行时下载失败不阻塞安装：玩家可能已有自己的 Java
            _emit(callback, {"type": "status",
                             "status": f"Java 运行时未安装成功（可稍后在设置里指定 Java）：{e}"})

    _emit(callback, {"type": "status", "status": f"{version_id} 安装完成"})

# -*- coding: utf-8 -*-
"""模组加载器安装（Fabric/Forge/NeoForge/Quilt）与整合包导入

整合包支持：
  .mrpack        - Modrinth 格式
  .zip (MultiMC/HMCL) - 解析 mmc-pack.json 恢复版本与加载器，合并数据
  .zip (CurseForge)   - 解析 manifest.json，合并 overrides，并按 projectID/fileID
                        自动从 CurseForge CDN 下载模组

v1.5 修复（打包整合包相关问题）：
- Forge 安装改用官方安装器（--installClient），并在装完后校验依赖库是否齐全；
  之前用 mll 自己拼的流程，processor 中断时会"看起来装好、一启动就退出码 1"
- 整合包导入时加载器安装失败不再假装成功，会明确报错
- CurseForge 整合包现在会自动下载 manifest 里的模组（不需要 API Key）
- 自动按 modid 去重，避免 overrides 自带 mod 和下载的 mod 冲突导致 Forge 崩
- zip 解压做了路径校验，避免恶意压缩包把文件写到目录外面
"""
import json
import shutil
import tempfile
import zipfile
from pathlib import Path

import minecraft_launcher_lib.mrpack as mll_mrpack
from minecraft_launcher_lib.mod_loader import get_mod_loader

from . import curseforge, downloader, mirrors


class ModError(Exception):
    pass


LOADERS = {
    "fabric": "Fabric",
    "forge": "Forge",
    "neoforge": "NeoForge",
    "quilt": "Quilt",
}

# MultiMC / HMCL mmc-pack.json 里的组件 uid → 加载器 id
MMC_LOADER_UIDS = {
    "net.fabricmc.fabric-loader": "fabric",
    "org.quiltmc.quilt-loader": "quilt",
    "net.minecraftforge": "forge",
    "net.neoforged": "neoforge",
}


def list_supported_loaders() -> list:
    return [{"id": k, "name": v} for k, v in LOADERS.items()]


# ---------------------------------------------------------------- 版本工具
def resolve_base_version(minecraft_dir, version_id) -> str:
    """把 Fabric/Forge 之类的继承版本还原成它依赖的原版版本"""
    current = version_id
    seen = set()
    while current and current not in seen:
        seen.add(current)
        json_path = Path(minecraft_dir) / "versions" / current / f"{current}.json"
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            return current
        parent = data.get("inheritsFrom")
        if not parent:
            return current
        current = parent
    return version_id


def get_loader_versions(loader_id, mc_version, source="official") -> dict:
    """返回某个加载器对某 MC 版本可用的加载器版本列表（新的在前）"""
    if loader_id not in LOADERS:
        return {"ok": False, "error": f"未知加载器 {loader_id}", "versions": [], "latest": ""}

    try:
        if loader_id == "forge":
            versions = mirrors.forge_versions(mc_version, source)
            return {"ok": True, "versions": versions,
                    "latest": versions[0] if versions else ""}
        if loader_id == "neoforge":
            versions = mirrors.neoforge_versions(mc_version, source)
            return {"ok": True, "versions": versions,
                    "latest": versions[0] if versions else ""}
        loader = get_mod_loader(loader_id)
        versions = loader.get_loader_versions(mc_version, False) or []
        latest = ""
        try:
            latest = loader.get_latest_loader_version(mc_version) or ""
        except Exception:
            latest = versions[0] if versions else ""
        return {"ok": True, "versions": versions, "latest": latest}
    except Exception as e:  # noqa: BLE001
        # 官方 maven 不通时，用镜像再来一次
        try:
            if loader_id == "forge":
                versions = mirrors.forge_versions(mc_version, "bmclapi")
            elif loader_id == "neoforge":
                versions = mirrors.neoforge_versions(mc_version, "bmclapi")
            else:
                versions = []
            if versions:
                return {"ok": True, "versions": versions,
                        "latest": versions[0], "warning": str(e)}
        except Exception:
            pass
        return {"ok": False, "error": str(e), "versions": [], "latest": ""}


# ---------------------------------------------------------------- 加载器安装
def _callbacks(raw):
    if not raw:
        return {}
    return {
        "setStatus": lambda s: raw({"type": "log", "line": s}),
        "setProgress": lambda p: raw({"type": "loader_progress", "progress": p}),
        "setMax": lambda m: raw({"type": "loader_max", "max": m}),
    }


def _forge_build_of(installed_version: str) -> str:
    """1.20.1-forge-47.3.22 -> 47.3.22"""
    marker = "-forge-"
    if marker in (installed_version or ""):
        return installed_version.split(marker, 1)[1]
    return ""


def _verify_loader_install(loader_id, base, installed, mc_dir, java_path,
                           loader_version, source, callback):
    """装完检查依赖库；Forge 缺文件时用官方安装器再修一次"""
    log = (lambda text: callback({"type": "log", "line": text})) if callback else (lambda text: None)
    missing = downloader.missing_libraries(installed, mc_dir)
    if not missing:
        return
    log(f"⚠ 检测到 {len(missing)} 个依赖库缺失，尝试自动修复…")
    if loader_id == "forge":
        build = _forge_build_of(installed) or (loader_version or "").split("-")[-1]
        try:
            mirrors.install_forge(base, mc_dir, build, java_path, source, callback)
        except Exception as e:  # noqa: BLE001
            log(f"修复失败：{e}")
        missing = downloader.missing_libraries(installed, mc_dir)
    if missing:
        names = ", ".join(Path(p).name for p in missing[:3])
        raise ModError(
            f"{LOADERS.get(loader_id, loader_id)} 安装不完整：还缺 {len(missing)} 个依赖库"
            f"（例如 {names}）。\n"
            "这通常是下载中途网络中断导致的。请换下载源后重新安装一次。")


def install_loader(loader_id, mc_version, minecraft_dir, java, loader_version=None,
                   callback=None, source="official"):
    """安装加载器到已安装的 MC 版本，返回安装后的版本 id"""
    if loader_id not in LOADERS:
        raise ModError(f"未知加载器 {loader_id}")

    mc_dir = str(minecraft_dir)
    base = resolve_base_version(mc_dir, mc_version) or mc_version

    # 母版本没装就先装（很多玩家会跳过这一步）
    if not downloader.is_installed(base, mc_dir):
        if callback:
            callback({"type": "log", "line": f"母版本 {base} 未安装，先自动安装…"})
        downloader.install_version(base, mc_dir, source, callback)

    java_path = java or shutil.which("java") or "java"
    if callback:
        callback({"type": "log", "line": f"开始安装 {LOADERS[loader_id]}（MC {base}）…"})

    installed = ""
    errors = []
    if loader_id == "forge":
        # 首选官方安装器：能正确处理 processors（srg/extra/client jar）
        try:
            installed = mirrors.install_forge(base, mc_dir, loader_version,
                                             java_path, source, callback)
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))
            if callback:
                callback({"type": "log", "line": f"官方安装器失败，尝试兼容安装方式：{e}"})
            try:
                mirrors.prepare_forge(source)
                if not loader_version:
                    candidates = mirrors.forge_versions(base, source)
                    loader_version = candidates[0] if candidates else None
                loader = get_mod_loader("forge")
                installed = loader.install(
                    base, mc_dir,
                    loader_version=loader_version or None,
                    callback=_callbacks(callback),
                    java=java_path)
            except Exception as e2:  # noqa: BLE001
                errors.append(str(e2))
    elif loader_id == "neoforge":
        try:
            installed = mirrors.install_neoforge(base, mc_dir, loader_version,
                                                 java_path, source, callback)
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))
    else:
        try:
            loader = get_mod_loader(loader_id)
            installed = loader.install(
                base, mc_dir,
                loader_version=loader_version or None,
                callback=_callbacks(callback),
                java=java_path)
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))

    if not installed:
        detail = "；".join(errors) if errors else "未知原因"
        raise ModError(
            f"安装 {LOADERS.get(loader_id, loader_id)} 失败：{detail}\n"
            "常见原因：网络问题（可换下载源重试）、Java 版本不匹配、所选 MC 版本没有对应加载器。")

    _verify_loader_install(loader_id, base, installed, mc_dir, java_path,
                           loader_version, source, callback)

    if callback:
        callback({"type": "log", "line": f"✅ {LOADERS[loader_id]} 安装完成：{installed}"})
        callback({"type": "loader_done", "loader": loader_id,
                  "installed_version": installed, "mc_version": base})
    return installed


# ---------------------------------------------------------------- 整合包
def import_modpack(path, minecraft_dir, callback=None, source="official"):
    path = Path(path)
    if not path.exists():
        raise ModError("整合包文件不存在")
    suffix = path.suffix.lower()
    if suffix == ".mrpack":
        return _import_mrpack(path, minecraft_dir, callback)
    if suffix == ".zip":
        return _import_zip(path, minecraft_dir, callback, source)
    raise ModError("仅支持 .mrpack 或 .zip 整合包")


def _import_mrpack(path, minecraft_dir, callback):
    log = (lambda text: callback({"type": "log", "line": text})) if callback else (lambda text: None)
    info = mll_mrpack.get_mrpack_information(str(path))
    name = info.get("name", path.stem)
    mc_version = info.get("minecraftVersion", "")
    mod_loader = info.get("modLoader", {}) or {}
    log(f"Modrinth 整合包：{name}（MC {mc_version}，加载器 {mod_loader.get('id', '未知')}）")
    log("开始安装依赖与模组（下载量可能较大，请耐心等待）…")
    try:
        mll_mrpack.install_mrpack(str(path), str(minecraft_dir),
                                  callback=_callbacks(callback))
    except Exception as e:  # noqa: BLE001
        raise ModError(f"整合包安装失败：{e}")
    try:
        launch_version = mll_mrpack.get_mrpack_launch_version(str(path))
    except Exception:
        launch_version = mc_version
    log(f"✅ 整合包 {name} 安装完成，可启动版本：{launch_version}")
    if callback:
        callback({"type": "loader_done", "loader": "modpack",
                  "installed_version": launch_version})
    return launch_version


def _import_zip(path, minecraft_dir, callback, source):
    tmp = Path(tempfile.mkdtemp(prefix="mcmodpack_"))
    try:
        with zipfile.ZipFile(path) as z:
            _safe_extract(z, tmp)
        mmc = _find_file(tmp, "mmc-pack.json")
        cf = _find_file(tmp, "manifest.json")
        if mmc:
            return _import_multimc(mmc, minecraft_dir, callback, source)
        if cf:
            return _import_curseforge(tmp, cf, minecraft_dir, callback, source)
        raise ModError("无法识别的整合包格式（缺少 mmc-pack.json 或 manifest.json）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _safe_extract(zf: zipfile.ZipFile, dest: Path):
    """防止 zip 里的 ../ 把文件解压到目标目录之外"""
    dest = dest.resolve()
    for member in zf.infolist():
        target = (dest / member.filename).resolve()
        if not str(target).startswith(str(dest)):
            continue
        if member.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(member) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)


def _find_file(root: Path, name: str, max_depth=2):
    if (root / name).is_file():
        return root / name
    for depth in range(1, max_depth + 1):
        hits = list(root.glob("/".join(["*"] * depth) + "/" + name))
        if hits:
            return hits[0]
    return None


def _merge_tree(src: Path, dst: Path):
    """把整合包数据目录并入游戏目录（不删除玩家已有的其它文件）"""
    if not src.is_dir():
        return 0
    count = 0
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            count += _merge_tree(item, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(item), str(target))
            count += 1
    return count


def _install_pack_runtime(mc_version, loader_id, loader_version, minecraft_dir,
                          callback, source, log):
    """整合包导入后把对应的 MC 版本与加载器装好

    返回 (installed_version, error_message)；失败时 error_message 非空，
    由调用方决定怎么提示（不能像以前那样默默忽略）。
    """
    if mc_version and not downloader.is_installed(mc_version, minecraft_dir):
        log(f"正在安装整合包使用的 MC 版本 {mc_version}…")
        try:
            downloader.install_version(mc_version, minecraft_dir, source, callback)
        except Exception as e:  # noqa: BLE001
            return "", f"安装 MC {mc_version} 失败：{e}"

    if not loader_id:
        return mc_version, ""

    # 安装器本身需要 Java，而且要是该版本要求的那一版
    java = ""
    component = downloader.version_java_component(mc_version, minecraft_dir) if mc_version else ""
    if component:
        try:
            java = downloader.ensure_java_runtime(component, minecraft_dir, callback)
        except Exception as e:  # noqa: BLE001
            log(f"准备官方 Java 运行时失败：{e}")
    if not java:
        java = shutil.which("java") or ""

    log(f"正在安装加载器 {LOADERS.get(loader_id, loader_id)}…")
    try:
        installed = install_loader(
            loader_id, mc_version, minecraft_dir,
            java=java, loader_version=loader_version,
            callback=callback, source=source)
        return installed, ""
    except Exception as e:  # noqa: BLE001
        return "", str(e)


def _import_multimc(mmc_path, minecraft_dir, callback, source):
    log = (lambda text: callback({"type": "log", "line": text})) if callback else (lambda text: None)
    data = json.loads(mmc_path.read_text(encoding="utf-8"))
    components = data.get("components", [])
    mc_version = ""
    loader_id = ""
    loader_version = ""
    for comp in components:
        uid = comp.get("uid", "")
        if uid == "net.minecraft":
            mc_version = comp.get("version", "")
        elif uid in MMC_LOADER_UIDS:
            loader_id = MMC_LOADER_UIDS[uid]
            loader_version = comp.get("version", "")
    if not mc_version:
        raise ModError("mmc-pack.json 里没有 Minecraft 版本信息")
    log(f"MultiMC/HMCL 整合包：MC {mc_version}，加载器 {LOADERS.get(loader_id, '无')}")

    root = mmc_path.parent
    merged = 0
    for name in ("minecraft", ".minecraft"):
        merged += _merge_tree(root / name, Path(minecraft_dir))
    if merged:
        log(f"✅ 已合并整合包数据（{merged} 个文件：mods/config/saves 等）")
    else:
        log("注意：整合包里没有 minecraft/ 数据目录")

    if loader_id and loader_version and "-" in loader_version and loader_id == "forge":
        loader_version = loader_version.split("-", 1)[1]

    installed, error = _install_pack_runtime(mc_version, loader_id, loader_version,
                                             minecraft_dir, callback, source, log)
    if error:
        if callback:
            callback({"type": "loader_error", "loader": "modpack",
                      "error": f"数据已导入，但版本/加载器安装失败：{error}"})
        return installed or mc_version
    if callback:
        callback({"type": "loader_done", "loader": "modpack",
                  "installed_version": installed or mc_version})
    return installed or mc_version


def _import_curseforge(tmp, manifest_path, minecraft_dir, callback, source):
    log = (lambda text: callback({"type": "log", "line": text})) if callback else (lambda text: None)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    mc_info = data.get("minecraft", {}) or {}
    mc_version = mc_info.get("version", "")
    loaders = mc_info.get("modLoaders") or []
    loader_id = ""
    loader_version = ""
    for item in loaders:
        raw = item.get("id", "")
        if "-" in raw:
            name, ver = raw.split("-", 1)
            loader_id = name.lower()
            loader_version = ver
            break
    files = data.get("files", []) or []
    log(f"CurseForge 整合包：MC {mc_version}，加载器 {loader_id or '无'}，"
        f"manifest 里 {len(files)} 个模组文件")
    log(f"pack 名称：{data.get('name', '')} {data.get('version', '')}".rstrip())

    merged = _merge_tree(tmp / "overrides", Path(minecraft_dir))
    if merged:
        log(f"✅ 已合并 overrides 数据（{merged} 个文件：mods/config/kubejs 等）")

    installed, error = _install_pack_runtime(mc_version, loader_id, loader_version,
                                             minecraft_dir, callback, source, log)
    if error:
        if callback:
            callback({"type": "loader_error", "loader": "modpack",
                      "error": f"overrides 已导入，但版本/加载器安装失败：{error}"})
        return installed or mc_version

    # 从 CurseForge 下载 manifest 里的模组（不需要 API Key）
    entries = []
    for item in files:
        project_id = item.get("projectID") or item.get("projectId")
        file_id = item.get("fileID") or item.get("fileId")
        if project_id and file_id:
            entries.append((int(project_id), int(file_id)))

    failed = []
    if entries:
        log(f"开始从 CurseForge 下载模组（{len(entries)} 个）…")
        try:
            result = curseforge.install_mods(entries, minecraft_dir, callback,
                                             mc_version=mc_version)
            failed = result.get("failed") or []
            log(f"模组下载：成功 {result.get('downloaded', 0)}，"
                f"已存在 {result.get('skipped', 0)}，失败 {len(failed)}")
            curseforge.dedupe_mods(minecraft_dir, result.get("names"), callback)
        except Exception as e:  # noqa: BLE001
            log(f"⚠ 下载模组时出错：{e}")
            failed = [("", "", str(e))]

    if failed:
        lines = []
        for project_id, _file_id, reason in failed[:8]:
            link = curseforge.PROJECT_PAGE.format(project_id=project_id) if project_id else ""
            lines.append(f"{link} {reason}".strip())
        detail = "\n".join(lines)
        log(f"⚠ 有 {len(failed)} 个模组没能下载：\n{detail}")
        if callback:
            callback({"type": "loader_error", "loader": "modpack",
                      "error": f"{len(failed)} 个模组下载失败，整合包可能无法启动。"
                               f"可手动到 CurseForge 下载后放进 mods/：\n{detail}"})
        return installed or mc_version

    if callback:
        callback({"type": "loader_done", "loader": "modpack",
                  "installed_version": installed or mc_version})
    return installed or mc_version

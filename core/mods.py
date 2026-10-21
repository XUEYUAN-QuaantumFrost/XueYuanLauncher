# -*- coding: utf-8 -*-
"""模组加载器安装（Fabric/Forge/NeoForge/Quilt）与整合包导入

整合包支持：
  .mrpack        - Modrinth 格式（库原生支持）
  .zip (MultiMC/HMCL) - 解析 mmc-pack.json 恢复版本与加载器，合并数据
  .zip (CurseForge)   - 解析 manifest.json，合并 overrides；模组下载需 API Key，提示用户
"""
import json
import shutil
import tempfile
import zipfile
from pathlib import Path

import minecraft_launcher_lib.fabric as mll_fabric
import minecraft_launcher_lib.forge as mll_forge
import minecraft_launcher_lib.mrpack as mll_mrpack
import minecraft_launcher_lib.quilt as mll_quilt
from minecraft_launcher_lib.mod_loader import get_mod_loader


class ModError(Exception):
    pass


LOADERS = {
    "fabric": "Fabric",
    "forge": "Forge",
    "neoforge": "NeoForge",
    "quilt": "Quilt",
}


def list_supported_loaders() -> list:
    return [{"id": k, "name": v} for k, v in LOADERS.items()]


def get_loader_versions(loader_id, mc_version) -> dict:
    """返回某个加载器对某 MC 版本可用的加载器版本列表"""
    try:
        if loader_id == "fabric":
            versions = [l["version"] for l in mll_fabric.get_all_loader_versions()]
            return {"ok": True, "versions": versions,
                    "latest": mll_fabric.get_latest_loader_version()}
        if loader_id == "quilt":
            versions = [l["version"] for l in mll_quilt.get_all_loader_versions()]
            return {"ok": True, "versions": versions,
                    "latest": mll_quilt.get_latest_loader_version()}
        if loader_id == "forge":
            versions = [mll_forge.forge_to_installed_version(v)
                        for v in mll_forge.list_forge_versions()]
            return {"ok": True, "versions": versions,
                    "latest": versions[-1] if versions else ""}
        if loader_id == "neoforge":
            nf = get_mod_loader("neoforge")
            versions = nf.get_loader_versions(mc_version, False)
            return {"ok": True, "versions": versions,
                    "latest": nf.get_latest_loader_version()}
    except Exception as e:
        return {"ok": False, "error": str(e), "versions": [], "latest": ""}
    return {"ok": False, "error": "未知加载器", "versions": [], "latest": ""}


def install_loader(loader_id, mc_version, minecraft_dir, java, loader_version=None, callback=None):
    """安装加载器到已安装的 MC 版本"""
    cb = {}
    if callback:
        cb = {"setStatus": lambda s: callback({"type": "log", "line": s}),
              "setProgress": lambda p: callback({"type": "loader_progress", "progress": p}),
              "setMax": lambda m: callback({"type": "loader_max", "max": m})}
    try:
        if loader_id == "fabric":
            mll_fabric.install_fabric(mc_version, minecraft_dir, loader_version, cb, java)
        elif loader_id == "quilt":
            mll_quilt.install_quilt(mc_version, minecraft_dir, loader_version, cb, java)
        elif loader_id == "forge":
            if not loader_version:
                loader_version = mll_forge.find_forge_version(mc_version)
            if not loader_version:
                raise ModError(f"Forge 没有支持 MC {mc_version} 的版本")
            mll_forge.install_forge_version(loader_version, minecraft_dir, cb, java)
        elif loader_id == "neoforge":
            nf = get_mod_loader("neoforge")
            if not loader_version:
                loader_version = nf.get_latest_loader_version()
            nf.install(mc_version, minecraft_dir, cb, java, loader_version)
        else:
            raise ModError(f"未知加载器 {loader_id}")
    except Exception as e:
        raise ModError(f"安装 {LOADERS.get(loader_id, loader_id)} 失败：{e}")
    if callback:
        callback({"type": "log", "line": f"✅ {LOADERS.get(loader_id, loader_id)} 安装完成"})
        callback({"type": "loader_done", "loader": loader_id})


# ---------------------------------------------------------------- 整合包
def import_modpack(path, minecraft_dir, callback=None):
    path = Path(path)
    if not path.exists():
        raise ModError("文件不存在")
    suffix = path.suffix.lower()
    if suffix == ".mrpack":
        _import_mrpack(path, minecraft_dir, callback)
    elif suffix == ".zip":
        _import_zip(path, minecraft_dir, callback)
    else:
        raise ModError("仅支持 .mrpack 或 .zip 整合包")


def _import_mrpack(path, minecraft_dir, callback):
    if callback:
        callback({"type": "log", "line": "解析 Modrinth 整合包…"})
    info = mll_mrpack.get_mrpack_information(str(path))
    name = info.get("name", path.stem)
    if callback:
        callback({"type": "log", "line": f"整合包：{name}（MC {info.get('minecraftVersion')}，"
                                         f"加载器 {info.get('modLoader', {}).get('id', '未知')}）"})
    if callback:
        callback({"type": "log", "line": "开始安装依赖与模组（下载量可能较大）…"})
    mll_mrpack.install_mrpack(str(path), str(minecraft_dir), callback=(
        {"setStatus": lambda s: callback({"type": "log", "line": s}),
         "setProgress": lambda p: callback({"type": "loader_progress", "progress": p}),
         "setMax": lambda m: callback({"type": "loader_max", "max": m})} if callback else None))
    if callback:
        callback({"type": "log", "line": f"✅ 整合包 {name} 安装完成"})
        callback({"type": "loader_done", "loader": "modpack"})


def _import_zip(path, minecraft_dir, callback):
    """识别 MultiMC/HMCL 或 CurseForge 格式的 zip 整合包"""
    tmp = Path(tempfile.mkdtemp(prefix="mcmodpack_"))
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            z.extractall(tmp)
        mmc = tmp / "mmc-pack.json"
        cf = tmp / "manifest.json"
        if mmc.exists():
            _import_multimc(tmp, mmc, minecraft_dir, callback)
        elif cf.exists():
            _import_curseforge(tmp, cf, minecraft_dir, callback)
        else:
            raise ModError("无法识别的整合包格式（缺少 mmc-pack.json 或 manifest.json）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _import_multimc(tmp, mmc_path, minecraft_dir, callback):
    data = json.loads(mmc_path.read_text(encoding="utf-8"))
    comps = data.get("components", [])
    mc_ver = None
    loader = None
    for c in comps:
        if c.get("uid") == "net.minecraft":
            mc_ver = c.get("version")
        if c.get("uid") in ("net.fabricmc.fabric-loader", "net.neoforged", "net.minecraftforge", "org.quiltmc.quilt-loader"):
            loader = c.get("uid")
    if not mc_ver:
        raise ModError("mmc-pack.json 中没有 Minecraft 版本信息")
    if callback:
        callback({"type": "log", "line": f"MultiMC 整合包：MC {mc_ver}，加载器 {loader or '无'}"})
    # 合并 minecraft/ 目录数据（mods、config、saves 等）
    mc_src = tmp / "minecraft"
    if mc_src.is_dir():
        dest = Path(minecraft_dir)
        for item in mc_src.iterdir():
            target = dest / item.name
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            shutil.move(str(item), str(target))
        if callback:
            callback({"type": "log", "line": "✅ 已合并整合包数据（mods/config/saves 等）"})
    else:
        if callback:
            callback({"type": "log", "line": "注意：zip 中没有 minecraft/ 数据目录"})
    if callback:
        callback({"type": "loader_done", "loader": "modpack", "mc_version": mc_ver, "loader": loader})


def _import_curseforge(tmp, cf_path, minecraft_dir, callback):
    data = json.loads(cf_path.read_text(encoding="utf-8"))
    mc_ver = (data.get("minecraft", {}) or {}).get("version")
    loader = (data.get("modLoader", {}) or {}).get("id")
    files = data.get("files", [])
    if callback:
        callback({"type": "log", "line": f"CurseForge 整合包：MC {mc_ver}，加载器 {loader or '无'}，模组 {len(files)} 个"})
    if callback:
        callback({"type": "log", "line": "⚠ CurseForge 模组下载需要 API Key，本版本无法自动下载模组；已合并 overrides 数据。"})
    ov = tmp / "overrides"
    if ov.is_dir():
        dest = Path(minecraft_dir)
        for item in ov.iterdir():
            target = dest / item.name
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            shutil.move(str(item), str(target))
        if callback:
            callback({"type": "log", "line": "✅ 已合并 overrides 数据"})
    if callback:
        callback({"type": "loader_done", "loader": "modpack", "mc_version": mc_ver, "loader": loader})

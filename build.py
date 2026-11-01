# -*- coding: utf-8 -*-
"""雪原启动器 打包脚本

用法：双击 build.bat，或在本目录执行 `python build.py`
产物：dist\\雪原启动器\\雪原启动器.exe（发布时把整个 dist\\雪原启动器 文件夹压缩发出去）
"""
import shutil
import subprocess
import sys
from pathlib import Path

APP_NAME = "雪原启动器"          # 显示名（exe 文件名、窗口标题）
BUILD_NAME = "XueYuanLauncher"    # PyInstaller 输出名，必须是纯 ASCII：
                                  # Qt 5.15 的网页引擎在中文路径下会崩，程序目录不能带中文
ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"

# 运行时依赖（模块名 -> pip 包名）
DEPENDENCIES = [
    ("PyQt5", "PyQt5>=5.15,<5.16"),
    ("PyQt5.QtWebEngineWidgets", "PyQtWebEngine>=5.15,<5.16"),
    ("requests", "requests>=2.28,<3"),
    ("minecraft_launcher_lib", "minecraft-launcher-lib>=8.0,<9.0"),
]


def log(message=""):
    print(message, flush=True)


def run(command):
    log("  > " + " ".join(str(part) for part in command))
    return subprocess.run(command, cwd=str(ROOT)).returncode


def check_dependencies():
    missing = []
    for module, package in DEPENDENCIES:
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    if missing:
        log(f"  缺少依赖：{', '.join(missing)}，正在安装…")
        if run([sys.executable, "-m", "pip", "install", *missing]) != 0:
            return False
    try:
        import PyInstaller  # noqa: F401
        log("  ✓ PyInstaller 已安装")
    except ImportError:
        log("  正在安装 PyInstaller…")
        if run([sys.executable, "-m", "pip", "install", "pyinstaller"]) != 0:
            return False
    return True


def build():
    log("=" * 46)
    log(f"  {APP_NAME} 打包脚本")
    log("=" * 46)

    log("\n[1/6] 清理旧文件…")
    shutil.rmtree(BUILD, ignore_errors=True)
    shutil.rmtree(DIST, ignore_errors=True)
    for spec in ROOT.glob("*.spec"):
        try:
            spec.unlink()
        except Exception:
            pass
    log("  ✓ 已清理 build/ dist/ *.spec")

    log("\n[2/6] 检查 Python…")
    if sys.version_info < (3, 10):
        log(f"  ✗ 需要 Python 3.10 及以上，当前 {sys.version.split()[0]}")
        return 1
    log(f"  ✓ Python {sys.version.split()[0]}")

    log("\n[3/6] 检查依赖…")
    if not check_dependencies():
        log("  ✗ 依赖安装失败，请检查网络（可以试试国内镜像）：")
        log("    pip install -r requirements.txt pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple")
        return 1

    log("\n[4/6] 开始打包（第一次会比较慢，请耐心等待）…")
    code = run([
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--windowed",
        "--name", BUILD_NAME, "--noupx",
        "--add-data", "ui;ui",
        "--collect-all", "PyQt5.QtWebEngineWidgets",
        "--collect-all", "PyQt5.QtWebEngineCore",
        "--collect-all", "PyQt5.QtWebChannel",
        "--collect-submodules", "minecraft_launcher_lib",
        "--hidden-import", "minecraft_launcher_lib.microsoft_account",
        "--hidden-import", "minecraft_launcher_lib.runtime",
        "--hidden-import", "minecraft_launcher_lib.mod_loader",
        "--hidden-import", "minecraft_launcher_lib.mrpack",
        "main.py",
    ])
    if code != 0:
        log("\n  ✗ 打包失败，请看上面的报错信息")
        return code

    log("\n[5/6] 整理发布目录…")
    folder = DIST / BUILD_NAME
    folder.mkdir(parents=True, exist_ok=True)
    config = folder / "config.json"
    if not config.is_file():
        config.write_text("{}", encoding="utf-8")

    # 把 exe 改成中文显示名（文件夹保持英文，否则 QtWebEngine 会崩）
    ascii_exe = folder / f"{BUILD_NAME}.exe"
    exe = folder / f"{APP_NAME}.exe"
    if ascii_exe.is_file() and not exe.is_file():
        ascii_exe.rename(exe)
    if exe.is_file():
        log(f"  ✓ {exe.name} 已生成")

    log("\n[6/6] 完成！")
    if not exe.is_file():
        log(f"  ⚠ 没有找到 {exe.name}，请检查上面的输出")
        return 1
    log(f"  程序：{exe}")
    log(f"  发布：把整个文件夹压缩发给玩家 → {folder}")
    log("  提示：文件夹名请保持英文；玩家解压后也不用改名，放英文路径下即可。")
    log("        玩家第一次启动需要联网下载游戏文件，不需要装 Python。")
    return 0


if __name__ == "__main__":
    exit_code = 1
    try:
        exit_code = build()
    except KeyboardInterrupt:
        log("\n已取消")
    except Exception as exc:  # noqa: BLE001
        log(f"\n打包脚本出错：{exc}")
    sys.exit(exit_code)

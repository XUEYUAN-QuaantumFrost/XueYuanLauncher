@echo off
chcp 65001 >nul
title XueYuan Launcher 一键打包
echo ============================================
echo   XueYuan Launcher 一键打包脚本 (Windows)
echo ============================================
echo.

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.10+ 并勾选 "Add to PATH"
    pause
    exit /b 1
)

echo [1/3] 安装运行依赖...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络后重试
    pause
    exit /b 1
)

echo [2/3] 安装打包工具 PyInstaller...
python -m pip install pyinstaller
if errorlevel 1 (
    echo [错误] PyInstaller 安装失败
    pause
    exit /b 1
)

echo [3/3] 开始打包（约 1-3 分钟）...
if exist app.ico (
    python -m PyInstaller -F -w -n XueYuanLauncher --add-data "ui/index.html;ui" --icon app.ico main.py
) else (
    python -m PyInstaller -F -w -n XueYuanLauncher --add-data "ui/index.html;ui" main.py
)

if errorlevel 1 (
    echo [错误] 打包失败，请把上面的报错信息发给开发者
    pause
    exit /b 1
)

echo.
echo ============================================
echo   打包完成！产物在：dist\XueYuanLauncher.exe
echo ============================================
echo 提示：把这个 exe 拷给玩家直接双击就能用，
echo       不需要对方装 Python。
pause

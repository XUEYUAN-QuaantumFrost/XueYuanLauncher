@echo off
setlocal EnableExtensions
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found in PATH.
    echo Install Python 3.10+ and enable "Add python.exe to PATH".
    echo.
    pause
    exit /b 1
)

python build.py
set "CODE=%errorlevel%"
echo.
if not "%CODE%"=="0" (
    echo [ERROR] Build failed with exit code %CODE%.
    echo See the messages above.
) else (
    echo Build finished. Check the dist folder.
)
echo.
pause
exit /b %CODE%

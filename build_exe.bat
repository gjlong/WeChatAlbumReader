@echo off
chcp 65001 >nul
echo ============================================
echo   WeChat Album Reader - Build EXE (PyInstaller)
echo ============================================
echo.

cd /d "%~dp0"

REM Check PyInstaller
where pyinstaller >nul 2>nul
if %errorlevel% neq 0 (
    echo 正在检查 PyInstaller...
    python -m pip install pyinstaller
    if %errorlevel% neq 0 (
        echo.
        echo 错误: PyInstaller 安装失败
        pause
        exit /b 1
    )
)

echo.
echo 开始打包...
echo 这可能需要几分钟，请耐心等待...
echo.

python -m PyInstaller ^
    --onefile ^
    --name WeChatAlbumReader ^
    --hidden-import scraper ^
    app.py

if %errorlevel% neq 0 (
    echo.
    echo 打包失败，请查看日志
    pause
    exit /b 1
)

echo.
echo ============================================
echo 打包完成!
echo.
echo 生成的 EXE 在 dist\WeChatAlbumReader.exe
echo ============================================
echo.

pause
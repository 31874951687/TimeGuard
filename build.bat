@echo off
rem ============================================================
rem  TimeGuard 一键打包脚本（生成 dist\TimeGuard\TimeGuard.exe）
rem  使用前请确保已安装依赖：
rem      pip install -r requirements.txt -r requirements-dev.txt
rem ============================================================
setlocal
cd /d "%~dp0"

echo [1/4] 生成图标...
python tools\make_icon.py || goto :error

echo [2/4] 自检...
python -m timeguard --selftest || goto :error

echo [3/4] 清理旧的构建产物...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo [4/4] 使用 PyInstaller 打包...
pyinstaller TimeGuard.spec --noconfirm || goto :error

echo.
echo ============================================================
echo  打包完成：dist\TimeGuard\TimeGuard.exe
echo  可以把整个 dist\TimeGuard 文件夹复制到任意位置使用，
echo  首次运行会在 %%APPDATA%%\TimeGuard 生成配置与使用记录。
echo ============================================================
goto :eof

:error
echo.
echo *** 打包失败，请查看上面的错误信息 ***
exit /b 1

@echo off
rem ============================================================
rem  TimeGuard 一键生成安装包 + 桌面快捷方式
rem
rem  前置条件：
rem    1) 已安装依赖：pip install -r requirements.txt -r requirements-dev.txt
rem    2) 已用 PyInstaller 打包过：build.bat（若未打包，本脚本会提示）
rem    3) 已安装 7-Zip（用于生成单文件自解压安装程序）
rem
rem  产物：dist\TimeGuard-Setup.exe / dist\TimeGuard_portable.zip
rem        桌面\TimeGuard 时间管家.lnk + 程序目录 + 安装包副本
rem ============================================================
setlocal
cd /d "%~dp0"

if not exist "dist\TimeGuard\TimeGuard.exe" (
    echo [提示] 还没有打包好的程序，先执行打包...
    if exist build.bat (
        call build.bat || goto :error
    ) else (
        python tools\make_icon.py || goto :error
        pyinstaller TimeGuard.spec --noconfirm || goto :error
    )
)

echo.
echo 正在生成安装包与桌面快捷方式...
powershell -NoProfile -ExecutionPolicy Bypass -File "packaging\build_installer.ps1" || goto :error

echo.
echo 全部完成，请看上面的产物路径。
pause
goto :eof

:error
echo.
echo *** 生成失败，请查看上面的错误信息 ***
pause
exit /b 1

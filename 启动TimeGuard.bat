@echo off
rem 双击即可启动 TimeGuard（源码运行，无控制台窗口）
rem 需要先执行：pip install -r requirements.txt
cd /d "%~dp0"
start "" pythonw.exe -m timeguard

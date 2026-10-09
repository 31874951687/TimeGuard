@echo off
rem 开发用：安装依赖（含打包工具）+ 运行自检 + 运行单元测试
setlocal
cd /d "%~dp0"

echo [1/3] 安装依赖...
python -m pip install -r requirements.txt -r requirements-dev.txt || goto :error

echo [2/3] 环境自检...
python -m timeguard --selftest

echo [3/3] 运行单元测试...
python tests\test_core.py || goto :error

echo.
echo 全部完成。启动程序：python -m timeguard
goto :eof

:error
echo *** 步骤失败 ***
exit /b 1

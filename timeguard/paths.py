"""统一路径管理。

* 数据目录（配置、数据库、日志、图标缓存）默认位于 ``%APPDATA%\\TimeGuard``，
  便于 PyInstaller 打包成 exe 后仍有可写目录。
* 设置环境变量 ``TIMEGUARD_DATA_DIR`` 可覆盖数据目录（测试、便携模式会用到）。
* 资源目录 ``resources`` 用于存放图标等只读文件，兼容源码运行与 exe 运行。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "TimeGuard"

#: 环境变量名：自定义数据目录
ENV_DATA_DIR = "TIMEGUARD_DATA_DIR"


def data_dir() -> Path:
    """返回数据目录（不存在则创建）。"""
    override = os.environ.get(ENV_DATA_DIR, "").strip()
    if override:
        base = Path(override).expanduser()
    else:
        appdata = os.environ.get("APPDATA")
        if appdata:
            base = Path(appdata) / APP_NAME
        else:  # 极少数无 APPDATA 的环境，退化为用户主目录
            base = Path.home() / f".{APP_NAME.lower()}"
    base.mkdir(parents=True, exist_ok=True)
    return base


def resource_dir() -> Path:
    """返回资源目录（图标等只读文件）。"""
    if getattr(sys, "frozen", False):  # PyInstaller 解包目录
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    # paths.py -> timeguard/ -> 项目根目录
    return Path(__file__).resolve().parent.parent


def config_file() -> Path:
    """用户设置 JSON 文件路径。"""
    return data_dir() / "config.json"


def database_file() -> Path:
    """SQLite 数据库文件路径。"""
    return data_dir() / "usage.db"


def log_file() -> Path:
    """运行日志文件路径。"""
    return data_dir() / "timeguard.log"


def icon_file() -> Path:
    """托盘 / 窗口图标（.ico）。"""
    return resource_dir() / "resources" / "icon.ico"


def png_icon_file() -> Path:
    """png 图标（Pillow 生成托盘图像时作为后备）。"""
    return resource_dir() / "resources" / "icon.png"

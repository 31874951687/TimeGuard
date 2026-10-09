"""开机自启管理（Windows 注册表 ``Run`` 键，使用标准库 ``winreg``，无需额外依赖）。

写入位置（当前用户，不需要管理员权限）::

    HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run
        值名：TimeGuard
        值内容："C:\\path\\TimeGuard.exe"            （打包成 exe 时）
              "C:\\...\\pythonw.exe" "C:\\...\\run.pyw"  （源码运行时）

提供的函数：
* :func:`is_supported`  当前环境是否支持（非 Windows 返回 False）
* :func:`is_enabled`    是否已开启自启
* :func:`current_command` 注册表里当前记录的命令行
* :func:`enable` / :func:`disable` / :func:`apply`
* :func:`build_command` 计算应该写入的命令行（便于测试与展示）
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "TimeGuard"

try:  # pragma: no cover - 非 Windows 环境没有 winreg
    import winreg
except ImportError:  # pragma: no cover
    winreg = None  # type: ignore[assignment]


def is_supported() -> bool:
    """当前系统是否支持注册表自启。"""
    return sys.platform == "win32" and winreg is not None


def _quote(path: str) -> str:
    """给含空格的路径加引号。"""
    return f'"{path}"' if " " in path else path


def build_command() -> str:
    """计算用于自启的命令行。

    * PyInstaller 打包后（``sys.frozen``）：直接用 exe 路径；
    * 源码运行：用 ``pythonw.exe``（无控制台窗口）+ 项目入口 ``run.pyw``。
    """
    if getattr(sys, "frozen", False):
        return _quote(sys.executable)

    project_root = Path(__file__).resolve().parent.parent
    entry = project_root / "run.pyw"
    if not entry.exists():                      # 退化为 -m timeguard
        return f'{_quote(sys.executable)} -m timeguard'

    pythonw = Path(sys.executable).with_name("pythonw.exe")
    interpreter = pythonw if pythonw.exists() else Path(sys.executable)
    return f"{_quote(str(interpreter))} {_quote(str(entry))}"


# ---------------------------------------------------------------- 注册表读写
def _open_key():
    """打开（或创建）Run 键。"""
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_ALL_ACCESS)  # type: ignore[union-attr]


def current_command() -> str:
    """读取注册表中已记录的命令行（未设置返回空串）。"""
    if not is_supported():
        return ""
    try:
        with _open_key() as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)  # type: ignore[union-attr]
            return str(value)
    except FileNotFoundError:
        return ""
    except OSError as exc:
        log.warning("读取开机自启注册表失败: %s", exc)
        return ""


def is_enabled() -> bool:
    """是否已设置开机自启。"""
    return bool(current_command())


def enable(command: str | None = None) -> bool:
    """开启开机自启（写入注册表）。"""
    if not is_supported():
        log.info("当前系统不支持注册表自启，已跳过")
        return False
    command = command or build_command()
    try:
        with _open_key() as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)  # type: ignore[union-attr]
        log.info("已开启开机自启: %s", command)
        return True
    except OSError as exc:
        log.warning("写入开机自启注册表失败: %s", exc)
        return False


def disable() -> bool:
    """关闭开机自启（删除注册表值）；本来就没有也返回 True。"""
    if not is_supported():
        return False
    try:
        with _open_key() as key:
            winreg.DeleteValue(key, VALUE_NAME)  # type: ignore[union-attr]
        log.info("已关闭开机自启")
        return True
    except FileNotFoundError:
        return True
    except OSError as exc:
        log.warning("删除开机自启注册表失败: %s", exc)
        return False


def apply(enabled: bool) -> bool:
    """按需开启 / 关闭，返回最终是否已开启。"""
    if enabled:
        enable()
    else:
        disable()
    return is_enabled()


def reconcile(enabled: bool) -> bool:
    """让注册表与设置保持一致。

    * ``enabled=True`` 且注册表内容与当前程序路径不同（例如换了安装位置），
      会自动改写为最新路径；
    * ``enabled=False`` 且存在残留项，则删除。

    返回最终是否开启自启。调用方（界面/启动流程）拿到返回值后可以回写设置。
    """
    if not is_supported():
        return False
    if not enabled:
        if is_enabled():
            disable()
        return False

    desired = build_command()
    existing = current_command()
    if existing != desired:
        if existing:
            log.info("开机自启路径已变化，正在更新注册表")
        enable(desired)
    return is_enabled()


def status_text() -> str:
    """给界面展示的一句话状态描述。"""
    if not is_supported():
        return "当前系统不支持（仅 Windows）"
    if not is_enabled():
        return "未开启"
    command = current_command()
    return f"已开启　{command if len(command) <= 60 else command[:57] + '…'}"

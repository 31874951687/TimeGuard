"""Windows 系统 API 封装：前台窗口、窗口标题、进程解析、DPI、全屏检测。

优先使用 ``pywin32``；如果用户环境没装（或导入失败），自动退回到 ``ctypes``
实现，保证程序在最小依赖下仍可运行。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import os
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- 可选依赖探测
try:  # pragma: no cover - 取决于运行环境
    import win32api
    import win32con
    import win32gui
    import win32process

    HAS_PYWIN32 = True
except Exception:  # noqa: BLE001 - 任何导入异常都退化为 ctypes
    win32api = win32con = win32gui = win32process = None  # type: ignore[assignment]
    HAS_PYWIN32 = False

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_user32.GetForegroundWindow.restype = wt.HWND
_user32.GetWindowTextLengthW.argtypes = [wt.HWND]
_user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
_user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
_user32.IsWindowVisible.argtypes = [wt.HWND]
_user32.IsIconic.argtypes = [wt.HWND]
_user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
_user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
_user32.GetShellWindow.restype = wt.HWND
_user32.GetDesktopWindow.restype = wt.HWND
_user32.MonitorFromWindow.restype = wt.HANDLE
_user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]

_kernel32.OpenProcess.restype = wt.HANDLE
_kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_kernel32.CloseHandle.argtypes = [wt.HANDLE]
_kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
_kernel32.ProcessIdToSessionId.argtypes = [wt.DWORD, ctypes.POINTER(wt.DWORD)]

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MONITOR_DEFAULTTONEAREST = 0x00000002

#: 真全屏时窗口类名通常是这两个之一
SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Windows.UI.Core.CoreWindow"}


# ---------------------------------------------------------------- DPI

def set_dpi_awareness() -> None:
    """开启 DPI 感知，避免高分屏下 tkinter 界面模糊。"""
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE = 2
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        log.debug("设置 DPI 感知失败（可忽略）")


def scale_factor() -> float:
    """返回主屏缩放比例（1.0 = 100%）。"""
    try:
        return max(1.0, ctypes.windll.user32.GetDpiForSystem() / 96.0)
    except Exception:  # noqa: BLE001
        try:
            dc = _user32.GetDC(0)
            dpi = ctypes.windll.gdi32.GetDeviceCaps(dc, 88)  # LOGPIXELSX
            _user32.ReleaseDC(0, dc)
            return max(1.0, dpi / 96.0)
        except Exception:  # noqa: BLE001
            return 1.0


# ---------------------------------------------------------------- 前台窗口

def get_foreground_window() -> int:
    """前台窗口句柄（失败返回 0）。"""
    try:
        return int(_user32.GetForegroundWindow() or 0)
    except Exception:  # noqa: BLE001
        return 0


def get_window_title(hwnd: int) -> str:
    """获取窗口标题文本。"""
    if not hwnd:
        return ""
    # 优先 pywin32（对某些 UWP/无边框窗口更稳），失败再用 ctypes
    if HAS_PYWIN32:
        try:
            title = win32gui.GetWindowText(hwnd)
            if title:
                return title.strip()
        except Exception:  # noqa: BLE001
            pass
    try:
        length = _user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value.strip()
    except Exception:  # noqa: BLE001
        return ""


def get_window_class(hwnd: int) -> str:
    """获取窗口类名（用于识别全屏 / 系统窗口）。"""
    if not hwnd:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, buffer, 256)
        return buffer.value
    except Exception:  # noqa: BLE001
        return ""


def get_window_pid(hwnd: int) -> int:
    """获取窗口所属进程 PID。"""
    if not hwnd:
        return 0
    try:
        pid = wt.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)
    except Exception:  # noqa: BLE001
        return 0


def is_window_minimized(hwnd: int) -> bool:
    """窗口是否最小化。"""
    if not hwnd:
        return False
    try:
        return bool(_user32.IsIconic(hwnd))
    except Exception:  # noqa: BLE001
        return False


def get_window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """窗口矩形 (left, top, right, bottom)。"""
    if not hwnd:
        return None
    try:
        rect = wt.RECT()
        if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        return (rect.left, rect.top, rect.right, rect.bottom)
    except Exception:  # noqa: BLE001
        return None


def get_monitor_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """窗口所在显示器的矩形（用于判断真全屏）。"""
    if not hwnd:
        return None
    try:
        class _MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wt.DWORD),
                ("rcMonitor", wt.RECT),
                ("rcWork", wt.RECT),
                ("dwFlags", wt.DWORD),
            ]

        monitor = _user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        if not monitor:
            return None
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if not _user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        r = info.rcMonitor
        return (r.left, r.top, r.right, r.bottom)
    except Exception:  # noqa: BLE001
        return None


def is_fullscreen(hwnd: int | None = None) -> bool:
    """判断当前前台窗口是否处于“真全屏”（覆盖整块屏幕且无边框）。"""
    hwnd = hwnd or get_foreground_window()
    if not hwnd:
        return False
    if get_window_class(hwnd) in SHELL_CLASSES:
        return False
    rect = get_window_rect(hwnd)
    monitor = get_monitor_rect(hwnd)
    if not rect or not monitor:
        return False
    tol = 2
    return (
        abs(rect[0] - monitor[0]) <= tol
        and abs(rect[1] - monitor[1]) <= tol
        and abs(rect[2] - monitor[2]) <= tol
        and abs(rect[3] - monitor[3]) <= tol
    )


def is_session_locked() -> bool:
    """当前会话是否被锁屏（锁屏时不应计时）。"""
    try:
        hdesk = _user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_READOBJECTS
        if not hdesk:
            return True  # 打开输入桌面失败，基本可以判定为锁屏/切换用户
        _user32.CloseDesktop(hdesk)
        return False
    except Exception:  # noqa: BLE001
        return False


def get_work_area(widget=None) -> tuple[int, int]:
    """返回屏幕可用工作区尺寸 ``(宽, 高)``（已排除任务栏）。

    用 ``SystemParametersInfoW(SPI_GETWORKAREA)`` 读取；失败则退回
    ``winfo_screenwidth/height``。小屏笔记本（例如 1366x768）上按工作区算尺寸，
    窗口才不会被任务栏裁掉。
    """
    try:
        rect = wt.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
            width = int(rect.right - rect.left)
            height = int(rect.bottom - rect.top)
            if width > 200 and height > 200:
                return width, height
    except Exception:  # noqa: BLE001
        log.debug("读取屏幕工作区失败", exc_info=True)
    if widget is not None:
        try:
            return int(widget.winfo_screenwidth()), int(widget.winfo_screenheight())
        except Exception:  # noqa: BLE001
            pass
    return 1920, 1080


def get_process_image_path(pid: int) -> str:
    """查询进程完整映像路径（无权限时返回空串）。"""
    if not pid:
        return ""
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wt.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return ""
    except Exception:  # noqa: BLE001
        return ""
    finally:
        try:
            _kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- 综合信息

@dataclass
class ForegroundInfo:
    """一次前台窗口采样的结果快照。"""

    hwnd: int = 0
    pid: int = 0
    title: str = ""
    exe: str = ""            # 进程名，例如 chrome.exe
    path: str = ""           # 完整映像路径，例如 C:\\...\\chrome.exe
    minimized: bool = False
    fullscreen: bool = False
    locked: bool = False

    @property
    def alive(self) -> bool:
        """是否拿到有效窗口与进程。"""
        return self.hwnd > 0 and bool(self.exe)


def sample_foreground(need_path: bool = False) -> ForegroundInfo:
    """采样当前前台窗口信息（轻量，可每秒调用）。

    :param need_path: 是否需要完整映像路径（仅用于按路径匹配，代价略高）。
    """
    info = ForegroundInfo(locked=is_session_locked())
    if info.locked:
        return info
    hwnd = get_foreground_window()
    if not hwnd:
        return info
    info.hwnd = hwnd
    info.title = get_window_title(hwnd)
    info.minimized = is_window_minimized(hwnd)
    info.pid = get_window_pid(hwnd)
    if info.pid:
        info.exe = get_process_name(info.pid)
        if need_path:
            info.path = get_process_image_path(info.pid)
    info.fullscreen = is_fullscreen(hwnd)
    return info


# ---------------------------------------------------------------- 进程名解析

_proc_name_cache: dict[int, tuple[float, str]] = {}
_CACHE_TTL = 60.0  # 秒


def get_process_name(pid: int) -> str:
    """通过 PID 获取进程可执行文件名（带 60 秒缓存，降低系统调用开销）。

    先尝试 ``psutil``（可处理 UWP 等特殊情况），失败再走 Win32 API。
    """
    now = time.monotonic()
    cached = _proc_name_cache.get(pid)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]

    name = ""
    try:
        import psutil  # 延迟导入，便于模块单独测试

        name = psutil.Process(pid).name()
    except Exception:  # noqa: BLE001 - 进程已退出 / 权限不足
        name = ""
    if not name:
        path = get_process_image_path(pid)
        if path:
            name = os.path.basename(path)
    if name:
        if len(_proc_name_cache) > 512:  # 简单防膨胀
            _proc_name_cache.clear()
        _proc_name_cache[pid] = (now, name)
    return name


def clear_process_cache() -> None:
    """清空进程名缓存（进程重启后立即刷新时使用）。"""
    _proc_name_cache.clear()


# ---------------------------------------------------------------- 关屏 / 待机
WM_POWERBROADCAST = 0x0218
PBT_APMSUSPEND = 0x0004
PBT_APMRESUMESUSPEND = 0x0007


def wake_timer_resolution() -> None:
    """把系统定时器精度提到 1ms，让采样间隔更准（退出时自动恢复）。"""
    try:
        _winmm = ctypes.WinDLL("winmm")  # noqa: F841
        _winmm.timeBeginPeriod(1)
    except Exception:  # noqa: BLE001
        pass

"""端到端验证（开发用）：开机待办提醒是否真的弹出来。

流程：
  1. 造一个隔离的数据目录，预置 3 条任务（1 条超期、1 条今天到期、1 条已完成）；
  2. 以 ``python -m timeguard --check-tasks`` 启动真实程序（与开机自启完全相同的装配路径）；
  3. 用 Win32 枚举窗口，确认标题含「待办提醒」的窗口真的出现了；
  4. 截图存到 tools/_shots/，然后结束子进程。

用法：python tools/e2e_task_reminder.py
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "tools" / "_shots"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def seed_tasks(data_dir: Path) -> list[str]:
    """在隔离数据目录里预置演示任务，返回任务标题列表。"""
    os.environ["TIMEGUARD_DATA_DIR"] = str(data_dir)
    from timeguard.database import UsageStore

    store = UsageStore()
    now = datetime.now()
    store.add_task("把季度总结写完发给主管", now - timedelta(hours=3), priority=2)   # 已超期
    store.add_task("晚饭后散步 30 分钟", now + timedelta(hours=2))                    # 今天到期
    store.add_task("预约体检", now + timedelta(days=2))                               # 未到期
    done = store.add_task("交电费", now - timedelta(days=1))
    store.complete_task(done, True)
    titles = [t.title for t in store.pending_tasks()]
    store.close()
    return titles


def find_window(title_part: str) -> tuple[int, str]:
    """枚举所有顶层窗口，返回 (句柄, 标题) —— 标题包含指定文字且可见；没有则 (0, "")。"""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    found: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def callback(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if title_part in buffer.value and user32.IsWindowVisible(hwnd):
                found.append((int(hwnd), buffer.value))
        return True

    user32.EnumWindows(callback, 0)
    return found[0] if found else (0, "")


def capture(hwnd: int, path: Path) -> None:
    """抓取指定窗口的客户区（与 ui_smoke 里同样的 GDI 方案）。"""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    user32.GetDC.argtypes = [wt.HWND]
    user32.GetDC.restype = wt.HDC
    user32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
    gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
    gdi32.CreateCompatibleDC.restype = wt.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wt.HBITMAP
    gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
    gdi32.SelectObject.restype = wt.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wt.HDC]
    gdi32.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, ctypes.c_uint, ctypes.c_uint,
                                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                    ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                    ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                    ("biClrImportant", wt.DWORD)]

    rect = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return

    hdc_window = user32.GetDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_window)
    hbitmap = gdi32.CreateCompatibleBitmap(hdc_window, width, height)
    gdi32.SelectObject(hdc_mem, hbitmap)
    user32.PrintWindow(hwnd, hdc_mem, 2)

    header = BITMAPINFOHEADER()
    header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    header.biWidth = width
    header.biHeight = -height
    header.biPlanes = 1
    header.biBitCount = 32
    buffer = ctypes.create_string_buffer(width * height * 4)
    gdi32.GetDIBits(hdc_mem, hbitmap, 0, height, buffer, ctypes.byref(header), 0)

    from PIL import Image

    Image.frombuffer("RGBA", (width, height), buffer, "raw", "BGRA", 0, 1).convert("RGB").save(path)
    gdi32.DeleteObject(hbitmap)
    gdi32.DeleteDC(hdc_mem)
    print(f"截图已保存: {path}")


def main() -> int:
    data_dir = Path(tempfile.mkdtemp(prefix="tg_e2e_task_"))
    titles = seed_tasks(data_dir)
    print(f"数据目录: {data_dir}")
    print(f"预置待办任务: {titles}")

    env = dict(os.environ)
    env["TIMEGUARD_DATA_DIR"] = str(data_dir)
    env["PYTHONIOENCODING"] = "utf-8"
    # 测试不要往 Windows 通知中心塞真实气泡（用户看到会以为程序在乱弹）
    env.setdefault("TIMEGUARD_NO_TOAST", "1")
    print("启动程序: python -m timeguard --check-tasks")
    process = subprocess.Popen(
        [sys.executable, "-m", "timeguard", "--check-tasks"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    hwnd = 0
    title = ""
    deadline = time.time() + 25
    while time.time() < deadline:
        # v1.3 起开机弹窗合并为「开机提醒」（周期任务补发 + 未完成待办），
        # 仍兼容旧标题，避免脚本因改名失效。
        for keyword in ("开机提醒", "待办提醒"):
            hwnd, title = find_window(keyword)
            if hwnd:
                break
        if hwnd:
            break
        if process.poll() is not None:
            print("子进程已退出，可能启动失败")
            break
        time.sleep(0.5)

    ok = bool(hwnd)
    if ok:
        print(f"检测到待办提醒窗口: hwnd={hwnd} 标题={title!r}")
        time.sleep(1.0)                     # 等界面画完
        capture(hwnd, OUT_DIR / "09_e2e_task_reminder.png")
    else:
        print("未检测到「TimeGuard 开机提醒」窗口")

    # 顺手看一眼日志，确认走了完整流程（等一小会儿让日志落盘）
    time.sleep(1.0)
    log_file = data_dir / "timeguard.log"
    if log_file.exists():
        lines = [line.strip() for line in log_file.read_text(encoding="utf-8", errors="replace").splitlines()
                 if "开机检查" in line or "待办" in line or "ERROR" in line]
        print("关键日志:")
        for line in lines[-6:]:
            print("   ", line)

    if process.poll() is None:
        # 必须彻底结束子进程：否则它会一直占着安装目录里的文件，
        # 导致后续重新部署时"文件被占用"（曾因此留下 6 个驻留进程）
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=8)
        print("已结束被测进程")
    print("结果:", "成功：待办提醒弹窗已弹出" if ok else "失败：未弹出待办提醒")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

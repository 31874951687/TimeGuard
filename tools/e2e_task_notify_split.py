"""端到端验证（开发用）：待办提醒的“分工”是否正确。

需求：系统通知与软件弹窗不要同时出现。
分工设计：
    * 软件弹窗  —— 每次启动列出全部未完成任务；
    * 系统通知  —— 只在任务“临近截止”（默认 24 小时内）时发，且同一任务默认 6 小时不重复。

本脚本用真实启动路径（python -m timeguard --check-tasks）跑两轮：

    第一轮：任务都在 30 小时以后到期  -> 只弹软件窗口，不发系统通知
    第二轮：任务 3 小时后到期 / 已超期 -> 软件窗口 + 系统通知都要出现

判定方式：软件窗口用 Win32 枚举窗口；系统通知看数据库里的 ``notified_at``
是否被写入（应用就是在成功发送通知后才写这个字段的，比抓通知窗口更可靠）。

用法：python tools/e2e_task_notify_split.py
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def find_window(title_part: str) -> tuple[int, str]:
    """枚举可见顶层窗口，返回标题包含关键字的 (句柄, 标题)。"""
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


def seed(data_dir: Path, plan: list[tuple[str, timedelta]]) -> list[str]:
    """在隔离数据目录里预置任务，返回标题列表。"""
    os.environ["TIMEGUARD_DATA_DIR"] = str(data_dir)
    from timeguard.database import UsageStore

    store = UsageStore()
    now = datetime.now()
    titles = []
    for title, delta in plan:
        store.add_task(title, now + delta)
        titles.append(title)
    store.close()
    return titles


def notified_titles(data_dir: Path) -> set[str]:
    """读取数据库里“已发过系统通知”的任务标题。"""
    db = data_dir / "usage.db"
    if not db.exists():
        return set()
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute("SELECT title FROM tasks WHERE notified_at IS NOT NULL").fetchall()
    finally:
        conn.close()
    return {row[0] for row in rows}


def run_once(data_dir: Path, label: str) -> tuple[bool, set[str]]:
    """启动一次真实程序，返回 (软件弹窗是否出现, 已通知的任务标题集合)。"""
    env = dict(os.environ)
    env["TIMEGUARD_DATA_DIR"] = str(data_dir)
    env["PYTHONIOENCODING"] = "utf-8"
    # 测试不要往 Windows 通知中心塞真实气泡（"发没发"看库里的 notified_at）
    env.setdefault("TIMEGUARD_NO_TOAST", "1")
    print(f"\n--- {label} ---")
    print(f"  数据目录: {data_dir}")
    process = subprocess.Popen(
        [sys.executable, "-m", "timeguard", "--check-tasks"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    dialog_shown = False
    deadline = time.time() + 25
    while time.time() < deadline:
        # v1.3 起开机弹窗合并成"开机提醒"（周期任务补发 + 未完成待办）；
        # 仍兼容旧标题"待办提醒"，避免脚本因改名失效。
        hwnd = None
        for keyword in ("开机提醒", "待办提醒"):
            hwnd, _title = find_window(keyword)
            if hwnd:
                break
        if hwnd:
            dialog_shown = True
            break
        if process.poll() is not None:
            break
        time.sleep(0.5)
    # 给系统通知留一点时间（应用发完通知才写 notified_at）
    time.sleep(2.5)
    notified = notified_titles(data_dir)

    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=8)

    print(f"  软件弹窗出现 : {dialog_shown}")
    print(f"  系统通知(已通知任务): {sorted(notified) or '（无）'}")
    log = data_dir / "timeguard.log"
    if log.exists():
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            if "开机检查" in line or "系统通知" in line:
                print("  日志:", line.split("] ", 1)[-1])
    return dialog_shown, notified


def main() -> int:
    ok = True

    # ---------------- 第一轮：都不临近截止 ----------------
    case_a = Path(tempfile.mkdtemp(prefix="tg_split_far_"))
    seed(case_a, [("下周提交报告", timedelta(days=5)), ("月底交房租", timedelta(days=30))])
    dialog_a, notified_a = run_once(case_a, "第一轮：任务都在 30 小时以后到期（预期：只弹窗，不发通知）")
    if not dialog_a:
        print("  [失败] 软件弹窗没有出现")
        ok = False
    if notified_a:
        print(f"  [失败] 不该发系统通知，却通知了：{sorted(notified_a)}")
        ok = False
    else:
        print("  [通过] 未临近截止 -> 只弹软件窗口，没有系统通知")

    # ---------------- 第二轮：有临近截止 + 已超期 ----------------
    case_b = Path(tempfile.mkdtemp(prefix="tg_split_near_"))
    seed(case_b, [
        ("3 小时后交作业", timedelta(hours=3)),
        ("已超期的任务", -timedelta(hours=2)),
        ("下周才到期", timedelta(days=5)),
    ])
    dialog_b, notified_b = run_once(case_b, "第二轮：有 3 小时后到期 + 已超期（预期：弹窗 + 系统通知）")
    expected = {"3 小时后交作业", "已超期的任务"}
    if not dialog_b:
        print("  [失败] 软件弹窗没有出现")
        ok = False
    if notified_b != expected:
        print(f"  [失败] 系统通知的任务不对：期望 {sorted(expected)}，实际 {sorted(notified_b)}")
        ok = False
    else:
        print("  [通过] 临期/超期任务发了系统通知，不临期的没有")

    # ---------------- 第三轮：间隔内再启动一次，不应重复通知 ----------------
    dialog_c, notified_c = run_once(case_b, "第三轮：立刻再启动一次（预期：弹窗仍在，但 6 小时内不重复通知）")
    if not dialog_c:
        print("  [失败] 软件弹窗没有出现（每次启动都应列出待办）")
        ok = False
    if notified_c != expected:
        print(f"  [失败] 通知标记被重复刷新：{sorted(notified_c)}")
        ok = False
    else:
        print("  [通过] 间隔内没有重复打扰（notified_at 未被刷新）")

    print("\n结果:", "全部通过" if ok else "存在失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

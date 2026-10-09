"""端到端验证（开发用）：用极低上限触发真实的“超限提醒”链路。

以 `python -m timeguard` 完全相同的装配方式启动（Config/UsageStore/Notifier/
UsageEngine/MainWindow + 引擎线程），把“当前前台进程”临时加入监控名单，
把每日上限设为 0 分钟，从而在几秒内真实触发：
    引擎 _check_limits -> Notifier 系统通知 -> PopupManager -> 置顶弹窗
最后退出，供人工/程序化确认弹窗确实出现。

用法：python tools/e2e_reminder.py [运行秒数]
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEMO_DIR = Path(tempfile.mkdtemp(prefix="tg_e2e_"))
os.environ["TIMEGUARD_DATA_DIR"] = str(DEMO_DIR)
os.environ.setdefault("TIMEGUARD_NO_TOAST", "1")  # 测试不发真实系统通知（别往通知中心塞气泡）

from timeguard import winapi  # noqa: E402
from timeguard.config import Config  # noqa: E402
from timeguard.database import UsageStore  # noqa: E402
from timeguard.engine import UsageEngine  # noqa: E402
from timeguard.notifier import Notifier  # noqa: E402
from timeguard.ui import MainWindow  # noqa: E402

RUN_SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0
LIMIT_MINUTES = int(sys.argv[2]) if len(sys.argv) > 2 else 0
# 每日上限最小为 1 分钟（daily_limit_seconds 有 60 秒下限），
# 因此验证“超限提醒”需要让计时真的走过 60 秒，或把已有累计时间预先写入数据库。
PRESEED_SECONDS = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0


def own_exe_name() -> str:
    """当前进程的 exe 名（把它加进监控名单，前台采样时就会被命中）。"""
    return winapi.get_process_name(os.getpid()) or "python.exe"


def main() -> int:
    winapi.set_dpi_awareness()
    config = Config()
    settings = config.settings
    settings.daily_limit_minutes = LIMIT_MINUTES   # 0 会被夹到 1 分钟上限
    settings.reminder_interval_minutes = 30
    settings.remind_immediately_on_exceed = True
    settings.enable_popup = True
    settings.enable_toast = True
    settings.suppress_popup_fullscreen = False  # 确保不被全屏逻辑暂缓
    exe = own_exe_name()
    settings.game_processes = [exe]             # 只监控自己，避免命中别的程序
    config.save()

    store = UsageStore()
    if PRESEED_SECONDS > 0:
        # 预置一点“已使用时长”，让引擎启动后立刻处于超限状态
        store.add_seconds(exe, PRESEED_SECONDS, kind="game")
        print(f"预置今日已用: {PRESEED_SECONDS:.0f}s（上限 {max(1, LIMIT_MINUTES) * 60:.0f}s）")
    notifier = Notifier()
    engine = UsageEngine(config, store, notifier)
    window = MainWindow(config, store, engine, notifier)

    events: list[str] = []
    original_callback = window._on_limit_exceeded  # noqa: SLF001

    def traced(target, used, title, message, detail):
        events.append(f"超限回调: target={getattr(target, 'name', target)} used={used:.1f}s")
        return original_callback(target, used, title, message, detail)

    window.engine.on_limit_exceeded = traced

    def sample_foreground(_delay: float) -> None:
        info = winapi.sample_foreground(need_path=False)
        print(f"  前台采样: exe={info.exe!r} title={info.title[:40]!r} locked={info.locked}")

    def pump_check(at: float) -> None:
        manager = window.popup_manager
        active = manager.active
        print(f"[{at:>4.1f}s] 弹窗可见={active} 队列={manager.pending.qsize()} "
              f"提醒次数={manager.popup_count_today}")
        if active:
            hwnd = ctypes.windll.user32.GetAncestor(manager._active.win.winfo_id(), 2)  # noqa: SLF001
            rect = wt.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            print(f"         弹窗窗口句柄={hwnd} 位置=({rect.left},{rect.top})-({rect.right},{rect.bottom}) "
                  f"置顶={bool(ctypes.windll.user32.GetWindowLongW(hwnd, -8) & 0x8)}")

    engine.start()
    print(f"数据目录: {DEMO_DIR}")
    print(f"监控进程: {exe}    每日上限: {settings.daily_limit_minutes} 分钟（0 = 立即超限）")
    print(f"通知后端: {notifier.backend}")
    print("-" * 64)

    for delay in (2.0, 3.0, 4.0):
        window.root.after(int(delay * 1000), lambda d=delay: sample_foreground(d))
    check_at = [t for t in (2.0, 4.0, 6.0, 8.0, 10.0, 15.0, 20.0, 30.0, 45.0, 60.0, 70.0, 80.0)
                if t < RUN_SECONDS]
    print(f"（引擎已启动，监控进程 {exe}；将在 {check_at} 秒时检查弹窗）")
    for at in check_at:
        window.root.after(int(at * 1000), lambda a=at: pump_check(a))

    def finish() -> None:
        print("-" * 64)
        snapshot = engine.snapshot()
        print(f"今日累计: {snapshot.total_seconds:.1f}s  超限={snapshot.over_limit}  "
              f"提醒次数={window.popup_manager.popup_count_today}")
        print(f"事件记录: {events}")
        print(f"数据库记录: {[(t.name, round(t.seconds, 1)) for t in store.today_targets()]}")
        # 测试模式下系统通知只记录不真发（避免往 Windows 通知中心塞气泡），
        # 但"请求发过通知"这件事仍然可以断言 —— 否则抑制就等于把这条链路测没了。
        titles = [title for title, _msg in notifier.sent]
        print(f"系统通知请求: {notifier.sent}")
        print(f"（测试模式 suppress={notifier.suppress}，所以只记录不真发）")
        print(f"后台通知后端: {notifier.backend}")
        # 主动关闭弹窗，确认关闭回调能解除冷却
        if window.popup_manager.active:
            window.popup_manager._active.close()  # noqa: SLF001
        print(f"关闭后 active={window.popup_manager.active} "
              f"（随后可再次提醒={window.popup_manager.request('t', 'm', 'd', cooldown=0)}）")
        if events and titles:
            result = "成功：已触发超限提醒（通知 + 弹窗）"
        elif events:
            result = "成功：已触发超限提醒（但没请求系统通知）"
        else:
            result = "失败：未触发超限提醒"
        print(f"结果: {result}")
        engine.stop()
        window._stop_tray()  # noqa: SLF001
        store.close()
        window.root.quit()
        window.root.destroy()

    window.root.after(int(RUN_SECONDS * 1000), finish)
    window.run()
    print("端到端验证结束")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

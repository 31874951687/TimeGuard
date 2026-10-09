"""端到端验证：开机提醒弹窗的新规矩。

用户反馈与要求：

1. 弹窗右上角的叉号"点了没反应" → 除了系统标题栏那个小叉号，**窗口里再放一个大号 ✕**；
2. 弹窗**每天只在首次启动软件时**显示一次（同一天再启动不弹，但错过的周期提醒照旧派发）；
3. 弹出后 **5 秒**没被关闭就自动关闭，并且窗口里要有**明显的倒计时**；
4. 「最小化到托盘」的通知**只提示一次**，并且提示里说明"以后不再自动提示"。

用法::

    python tools/e2e_startup_dialog.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["TIMEGUARD_DATA_DIR"] = tempfile.mkdtemp(prefix="tg_e2e_dialog_")
os.environ.setdefault("TIMEGUARD_NO_TOAST", "1")  # 测试不发真实系统通知

OUT = ROOT / "tools" / "_shots"


def save_shot(widget, name: str) -> None:
    """整窗截图（屏幕抓图：PrintWindow 对这类窗口的客户区可能是空白）。"""
    try:
        import ctypes
        import ctypes.wintypes as wt

        from PIL import ImageGrab

        widget.update_idletasks()
        widget.update()
        hwnd = ctypes.windll.user32.GetAncestor(widget.winfo_id(), 2)
        rect = wt.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        image = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom))
        OUT.mkdir(parents=True, exist_ok=True)
        image.save(OUT / f"{name}.png")
        print(f"  截图已保存: {OUT / (name + '.png')}（{image.width}x{image.height}）")
    except Exception as exc:  # noqa: BLE001
        print(f"  截图失败（忽略）: {exc}")

from timeguard import winapi  # noqa: E402
from timeguard.config import Config  # noqa: E402
from timeguard.database import UsageStore  # noqa: E402
from timeguard.engine import UsageEngine  # noqa: E402
from timeguard.notifier import Notifier  # noqa: E402
from timeguard.tasks import StartupCheckDialog  # noqa: E402
from timeguard.ui import MainWindow  # noqa: E402
from timeguard.utils import today_str  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []
STATE = {"done": False}
WATCHDOG_MS = 120000


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  [通过] {label}{('　' + detail) if detail else ''}")
        PASS.append(label)
    else:
        print(f"  [失败] {label}{('　' + detail) if detail else ''}")
        FAIL.append(label)


def main() -> int:
    winapi.set_dpi_awareness()
    cfg = Config()
    # 开机提醒弹窗由 enable_popup 控制（关掉弹窗时只静默派发），这里要它弹
    cfg.settings.enable_popup = True
    cfg.settings.task_check_on_start = True
    cfg.settings.task_reminder_toast = False
    cfg.settings.recurring_reminder_enabled = False
    cfg.settings.startup_dialog_date = ""
    cfg.settings.tray_notice_shown = False
    cfg.save()

    store = UsageStore()
    store.add_task("交周报", datetime.now() + timedelta(hours=3))
    store.add_task("复习高数", datetime.now() - timedelta(hours=2))
    notifier = Notifier()
    engine = UsageEngine(cfg, store, notifier)
    win = MainWindow(cfg, store, engine, notifier)

    import traceback as _tb

    def on_tk_error(exc_type, exc_value, exc_tb) -> None:  # noqa: ANN001
        print("!! Tk 回调里发生异常，脚本中止 !!")
        _tb.print_exception(exc_type, exc_value, exc_tb)
        FAIL.append(f"Tk 回调异常: {exc_value!r}")
        finish(1)

    win.root.report_callback_exception = on_tk_error  # type: ignore[assignment]

    def pump(seconds: float = 1.0) -> None:
        end = time.time() + seconds
        while time.time() < end:
            win.root.update()
            time.sleep(0.02)

    def dialogs() -> list:
        return [child for child in win.root.winfo_children()
                if child.winfo_class() == "Toplevel" and "开机提醒" in str(child.title())]

    def run() -> None:
        def watchdog() -> None:
            if STATE["done"]:
                return
            print(f"!! 超过 {WATCHDOG_MS / 1000:.0f} 秒仍未跑完，判定为卡死 !!")
            FAIL.append("流程超时")
            finish(1)

        win.root.after(WATCHDOG_MS, watchdog)
        # 主界面自己在 1700ms 也会跑一次开机检查；由脚本控制节奏，先把它挡掉
        win._startup_check_done = True          # noqa: SLF001
        pump(2.5)
        print("=== 端到端：开机提醒弹窗 ===")

        # ---- 1) 弹出 + 大号关叉 + 倒计时 ----
        win._startup_check_done = False          # noqa: SLF001
        win.startup_check()
        tips = dialogs()                         # 弹窗是同步创建的，不用等
        check("弹窗弹出来了", len(tips) == 1, f"{len(tips)} 个")
        if not tips:
            finish(1)
            return
        tip = tips[0]

        check("弹窗标题栏的关闭按钮已接上（WM_DELETE_WINDOW → close）",
              "close" in str(tip.protocol("WM_DELETE_WINDOW")), str(tip.protocol("WM_DELETE_WINDOW")))
        button = getattr(tip, "close_button", None)
        check("窗口里有一个自己画的大号 ✕ 按钮", button is not None and str(button.cget("text")) == "✕")
        if button is not None:
            tip.update_idletasks()
            check("✕ 按钮足够大（不再依赖又小又贴边的系统按钮）",
                  button.winfo_reqwidth() >= 30 and button.winfo_reqheight() >= 26,
                  f"{button.winfo_reqwidth()}x{button.winfo_reqheight()}px")
        check("倒计时秒数按需求是 5 秒",
              StartupCheckDialog.AUTO_CLOSE_SECONDS == 5,
              str(StartupCheckDialog.AUTO_CLOSE_SECONDS))
        check("窗口里明确写出『5 秒后自动关闭』",
              "5 秒后自动关闭" in str(tip.countdown_var.get()), str(tip.countdown_var.get()))
        save_shot(tip, "24_startup_dialog")

        # 倒计时真的在走
        first = str(tip.countdown_var.get())
        pump(1.2)
        second = str(tip.countdown_var.get())
        check("倒计时在递减", first != second and "秒后自动关闭" in second, f"{first!r} → {second!r}")

        # ---- 2) 大号 ✕ 真的能关 ----
        if button is not None:
            button.invoke()
        pump(1)
        check("点大号 ✕ 能立刻关闭弹窗", not dialogs())
        if dialogs():
            for item in dialogs():
                item.close()

        # ---- 3) 每天只在首次启动时弹一次 ----
        check("弹过一次后记录了日期",
              str(cfg.settings.startup_dialog_date)[:10] == today_str(),
              str(cfg.settings.startup_dialog_date))
        win._startup_check_done = False          # noqa: SLF001 - 模拟"今天又启动了一次"
        win.startup_check()
        pump(1.5)
        check("同一天再次启动**不再弹**弹窗", not dialogs(), f"{len(dialogs())} 个")

        # 清掉日期（相当于到了新的一天）→ 应该又能弹
        cfg.settings.startup_dialog_date = ""
        win._startup_check_done = False          # noqa: SLF001
        win.startup_check()
        pump(1.5)
        check("换一天（清掉日期）后又能弹", len(dialogs()) == 1, f"{len(dialogs())} 个")

        # ---- 4) 5 秒到点自动关闭（把剩余秒数改小，快速验证真实定时器链路）----
        tip = dialogs()[0] if dialogs() else None
        if tip is not None:
            tip._remaining = 1                    # noqa: SLF001 - 只缩短等待，逻辑照旧
            pump(2.5)
            check("倒计时归零后弹窗自动关闭", not dialogs(), f"{len(dialogs())} 个")
        else:
            check("倒计时归零后弹窗自动关闭", False, "没有弹窗可测")

        # ---- 5) 「最小化到托盘」的通知只提示一次 ----
        notifier.sent.clear()
        win.hide_to_tray(manual=True)
        pump(1)
        check("第一次收进托盘会提示一次",
              len(notifier.sent) == 1 and "已在后台运行" in notifier.sent[0][0],
              str(notifier.sent))
        check("提示里说明了『以后不再自动提示』",
              bool(notifier.sent) and "不再自动提示" in notifier.sent[0][1],
              notifier.sent[0][1].replace("\n", " ") if notifier.sent else "")
        check("提示状态被持久化到配置", bool(cfg.settings.tray_notice_shown))

        win.show_window()
        pump(1)
        notifier.sent.clear()
        win.hide_to_tray(manual=True)
        pump(1)
        check("之后再收进托盘不再弹提示", notifier.sent == [], str(notifier.sent))
        check("但窗口确实收进托盘了", win._hidden is True)  # noqa: SLF001

        finish(0)

    def finish(code: int) -> None:
        if STATE["done"]:
            return
        STATE["done"] = True
        print("-" * 60)
        print(f"结果: {'全部通过' if not FAIL else '有失败项'}（通过 {len(PASS)} / 失败 {len(FAIL)}）")
        for item in FAIL:
            print(f"  失败: {item}")
        try:
            engine.stop()
            win.task_scheduler.stop()
            win._stop_tray()  # noqa: SLF001
            store.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            win.root.quit()
            win.root.destroy()
        except Exception:  # noqa: BLE001
            pass
        raise SystemExit(code)

    win.root.after(1200, run)
    win.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

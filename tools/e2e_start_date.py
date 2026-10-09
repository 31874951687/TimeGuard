"""端到端验证：新建周期任务时"今天的时间已经过了" → 从明天开始，而不是立刻算逾期。

用户反馈：晚上 21 点建一条「每天 06:00~08:00」，软件立刻把它显示成"今天已超期"，
但站在用户角度，他是想**从明天开始**做这件事。

现在的口径（``tasks.start_date``）：

* 新建周期任务时，如果**今天的时间窗已经结束**，生效日就是明天 ——
  今天既不排班、也不算逾期，列表里它落在「本周待办」，并写明「从 10-09 开始」；
* 时间窗还没到的（比如 21 点建 22:00 的任务），今天照常算；
* 对话框底部那行预览会直接说"（今天的时间已过，从 10-09 开始）"，不用用户去猜。

用法::

    python tools/e2e_start_date.py
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
os.environ["TIMEGUARD_DATA_DIR"] = tempfile.mkdtemp(prefix="tg_e2e_startdate_")
os.environ.setdefault("TIMEGUARD_NO_TOAST", "1")  # 测试不发真实系统通知

from timeguard import recurrence as R  # noqa: E402
from timeguard import tasks as tasks_mod  # noqa: E402
from timeguard import winapi  # noqa: E402
from timeguard.config import Config  # noqa: E402
from timeguard.database import UsageStore  # noqa: E402
from timeguard.engine import UsageEngine  # noqa: E402
from timeguard.notifier import Notifier  # noqa: E402
from timeguard.scheduler import compute_schedule  # noqa: E402
from timeguard.tasks import GROUP_TODAY, GROUP_WEEK, build_task_rows  # noqa: E402
from timeguard.ui import MainWindow  # noqa: E402
from timeguard.utils import today_str  # noqa: E402

OUT = ROOT / "tools" / "_shots"


def save_shot(widget, name: str) -> None:
    """整窗截图（屏幕抓图，PrintWindow 对某些窗口客户区是空白）。"""
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
    now = datetime.now()
    cfg = Config()
    cfg.settings.enable_popup = False
    cfg.settings.task_check_on_start = False
    cfg.settings.task_reminder_toast = False
    cfg.settings.recurring_reminder_enabled = False
    cfg.save()

    store = UsageStore()
    notifier = Notifier()
    engine = UsageEngine(cfg, store, notifier)
    win = MainWindow(cfg, store, engine, notifier)
    panel = win.task_panel

    real_dialog = tasks_mod.TaskDialog

    def auto_dialog(fill):                       # noqa: ANN001, ANN202
        """返回一个 TaskDialog 子类：创建后自动填表并确认（驱动真实控件）。"""

        class Auto(real_dialog):                 # type: ignore[valid-type,misc]
            def __init__(self, master, **kw):    # noqa: ANN001, ANN003
                super().__init__(master, **kw)
                self.after(20, lambda: (fill(self), self._confirm()))

        return Auto

    def pump(seconds: float = 1.0) -> None:
        end = time.time() + seconds
        while time.time() < end:
            win.root.update()
            time.sleep(0.02)

    def run() -> None:
        def watchdog() -> None:
            if STATE["done"]:
                return
            print(f"!! 超过 {WATCHDOG_MS / 1000:.0f} 秒仍未跑完，判定为卡死 !!")
            FAIL.append("流程超时")
            finish(1)

        win.root.after(WATCHDOG_MS, watchdog)
        pump(2.5)
        print("=== 端到端：周期任务的生效起始日 ===")
        print(f"  现在: {now:%Y-%m-%d %H:%M}")

        # ---- 1) 已经过去的时间窗（用户反馈的场景）----
        if now.hour < 1:
            print("  [跳过] 刚过零点，今天没有『已经过去』的同日窗口可测")
        else:
            end_dt = now - timedelta(minutes=5)
            start_dt = end_dt - timedelta(hours=1)
            past_start, past_end = start_dt.strftime("%H:%M"), end_dt.strftime("%H:%M")
            print(f"  场景一：现在建「每天 {past_start}~{past_end}」（今天这个窗口早已结束）")

            captured: dict = {}

            def fill(dlg) -> None:               # noqa: ANN001
                dlg.title_var.set("晚上才建的每日跑步")
                dlg.type_var.set(R.TYPE_LABELS[R.TYPE_DAILY])
                dlg._on_type_change()            # noqa: SLF001
                dlg.start_field.set(past_start)
                dlg.end_field.set(past_end)
                dlg._update_preview()            # noqa: SLF001 - 设完时间段再取预览
                captured["preview"] = dlg.preview_var.get()

            tasks_mod.TaskDialog = auto_dialog(fill)     # type: ignore[assignment]
            panel.add_task()
            tasks_mod.TaskDialog = real_dialog           # type: ignore[assignment]
            pump(1.5)

            check("对话框预览明确说了『今天的时间已过，从 … 开始』",
                  "今天的时间已过" in captured.get("preview", ""),
                  captured.get("preview", ""))

            task = next((t for t in store.recurring_tasks()
                         if t.title == "晚上才建的每日跑步"), None)
            check("任务建出来了", task is not None)
            if task is not None:
                tomorrow = (now + timedelta(days=1)).date()
                check("生效起始日是明天（不是今天）",
                      task.rule.start_date == tomorrow, str(task.rule.start_date))
                check("今天不排班", task.rule.occurs_on(now.date()) is False)
                check("计划文案写明从哪天开始",
                      f"{tomorrow:%m-%d} 开始" in task.schedule_text, task.schedule_text)

                groups = build_task_rows(store, "pending", now)
                today_rows = [row.task.title for row in groups[GROUP_TODAY]]
                week_rows = [row.task.title for row in groups[GROUP_WEEK]]
                check("列表里它在『本周待办』，不在『今日待办』",
                      "晚上才建的每日跑步" in week_rows
                      and "晚上才建的每日跑步" not in today_rows,
                      f"今日 {today_rows} / 本周 {week_rows}")
                week_row = next((row for row in groups[GROUP_WEEK]
                                 if row.task.title == "晚上才建的每日跑步"), None)
                check("没有任何一行被标成『已超期』",
                      week_row is not None and "已超期" not in week_row.status_text,
                      week_row.status_text if week_row else "（没找到这一行）")

                counts = store.task_counts()
                check("今天的统计里不算待办、也不算超期",
                      counts.overdue == 0 and counts.due_today == 0 and counts.pending == 0,
                      counts.summary_text())
                stats = {item.day: item for item in store.task_stats_by_day(7)}
                check("7 天图表里今天没有逾期柱",
                      stats[today_str()].overdue == 0,
                      str(stats[today_str()]))

                # 调度器也不会在今天为它排任何提醒：下一次唤醒在明天
                state = compute_schedule(store.recurring_tasks(), {}, now,
                                         catchup_minutes=5, catchup_enabled=True)
                check("调度器下一次唤醒落在明天（今天不再打扰）",
                      state.batch is None and state.wakeup_at is not None
                      and state.wakeup_at.date() == tomorrow,
                      f"batch={state.batch} wakeup={state.wakeup_at}")
                panel.filter_var.set("pending")
                win.show_tasks_tab()          # 切到待办页再截图（否则拍到的是统计页）
                panel.refresh()
                pump(1.0)
                save_shot(win.root, "25_start_from_tomorrow")

                # 编辑对话框回填时也不该把它说成"今天要做"
                dlg = real_dialog(win.root, font_family=win.font_family, task=task)
                dlg.update_idletasks()
                check("编辑时回填的时间段与预览都正确",
                      dlg.start_field.get_text() == past_start
                      and "今天的时间已过" in dlg.preview_var.get(),
                      dlg.preview_var.get())
                dlg.destroy()

        # ---- 2) 时间窗还没到：今天照常算 ----
        if now.hour >= 22:
            print("  [跳过] 已过 22 点，构造不出『今天还没到』的窗口")
        else:
            ahead_start = (now + timedelta(minutes=10)).strftime("%H:%M")
            ahead_end = (now + timedelta(minutes=70)).strftime("%H:%M")
            print(f"  场景二：现在建「每天 {ahead_start}~{ahead_end}」（今天还没到）")

            def fill2(dlg) -> None:              # noqa: ANN001
                dlg.title_var.set("稍后要做的每日任务")
                dlg.type_var.set(R.TYPE_LABELS[R.TYPE_DAILY])
                dlg._on_type_change()            # noqa: SLF001
                dlg.start_field.set(ahead_start)
                dlg.end_field.set(ahead_end)

            tasks_mod.TaskDialog = auto_dialog(fill2)    # type: ignore[assignment]
            panel.add_task()
            tasks_mod.TaskDialog = real_dialog           # type: ignore[assignment]
            pump(1.5)

            task2 = next((t for t in store.recurring_tasks()
                          if t.title == "稍后要做的每日任务"), None)
            check("时间窗没到 → 生效日就是今天",
                  task2 is not None and task2.rule.start_date == now.date(),
                  str(task2.rule.start_date) if task2 else "（没建出来）")
            groups = build_task_rows(store, "pending", now)
            today_rows = [row.task.title for row in groups[GROUP_TODAY]]
            check("它在『今日待办』里", "稍后要做的每日任务" in today_rows, f"今日 {today_rows}")
            counts = store.task_counts()
            check("今天算它一条待办，但不算超期",
                  counts.pending == 1 and counts.overdue == 0, counts.summary_text())

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

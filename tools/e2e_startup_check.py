"""端到端验证：开机"错过与补发"闭环（阶段 3）。

真实开关两次主界面，验证避坑 #1 的完整链路：

1. **合并成一次打扰**：错过的周期任务 + 未完成的待办 → 只弹**一个**"开机提醒"窗；
2. **不会重复补发**：同一台机器再启动一次（模拟关机重开）→ 不再提醒同一次发生；
3. **超过窗口不打扰**：补发窗口设成 0（或开始时间已过很久）→ 不提醒；
4. **可关闭补发**：`recurring_late_catchup=false` → 不补发；
5. **单次任务不受影响**：待办列表里不会混进周期任务（否则两个区块重复展示）。

用法::

    python tools/e2e_startup_check.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA_DIR = tempfile.mkdtemp(prefix="tg_e2e_startup_")
os.environ["TIMEGUARD_DATA_DIR"] = DATA_DIR
os.environ.setdefault("TIMEGUARD_NO_TOAST", "1")  # 测试不发真实系统通知（别往通知中心塞气泡）

from timeguard import recurrence as R  # noqa: E402
from timeguard import winapi  # noqa: E402
from timeguard.config import Config  # noqa: E402
from timeguard.database import UsageStore  # noqa: E402
from timeguard.engine import UsageEngine  # noqa: E402
from timeguard.notifier import Notifier  # noqa: E402
from timeguard.ui import MainWindow  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  [通过] {label}{('　' + detail) if detail else ''}")
        PASS.append(label)
    else:
        print(f"  [失败] {label}{('　' + detail) if detail else ''}")
        FAIL.append(label)


def reminder_dialogs(window) -> list:
    """当前所有"提醒"类弹窗。"""
    result = []
    for child in window.root.winfo_children():
        if child.winfo_class() == "Toplevel" and "提醒" in str(child.title()):
            result.append(child)
    return result


def pump(window, seconds: float = 3.0) -> None:
    """跑事件循环若干秒（让 after 回调都执行完）。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        window.root.update()
        time.sleep(0.02)


def close_dialogs(window) -> None:
    for dialog in reminder_dialogs(window):
        try:
            # 用 close() 而不是 destroy()：它会先把倒计时定时器取消掉，
            # 免得窗口没了之后 after 回调还去调用已删除的命令（刷一堆 Tcl 报错）。
            dialog.close()
        except Exception:  # noqa: BLE001
            try:
                dialog.destroy()
            except Exception:  # noqa: BLE001
                pass
    pump(window, 0.3)


def run_session(store, *, catchup: bool = True, catchup_minutes: int = 5,
                window_seconds: float = 3.2, fresh_day: bool = True):
    """"开一次机"：构造主窗口，跑一会儿，返回 (window, dialogs 标题列表)。

    :param fresh_day: 是否清掉"今天已经弹过开机提醒"的标记。
        开机提醒弹窗**每天只在首次启动软件时**显示一次（v1.4 起），
        所以想在"同一天"里连开几次机都看到弹窗，就得每次重置这个日期标记；
        想验证"同一天第二次启动不弹"时传 ``False``。
    """
    config = Config()
    settings = config.settings
    settings.task_check_on_start = True
    settings.task_reminder_toast = False          # 只验证弹窗，避免系统通知干扰
    settings.recurring_late_catchup = catchup
    settings.recurring_catchup_minutes = catchup_minutes
    if fresh_day:
        settings.startup_dialog_date = ""
    config.save()

    notifier = Notifier()
    engine = UsageEngine(config, store, notifier)
    window = MainWindow(config, store, engine, notifier)
    pump(window, window_seconds)
    titles = [str(dialog.title()) for dialog in reminder_dialogs(window)]
    return window, titles


def shutdown(window) -> None:
    try:
        # 这台脚本一个进程里要"开好几次机"，所以必须像真实退出那样先停掉
        # 已排队的周期回调（真实退出走 quit_app / run 的 finally，这边要自己来），
        # 否则会留下一堆 "invalid command name ..._tick_ui" 噪音。
        window._shutting_down = True      # noqa: SLF001
        window._cancel_periodic_callbacks()  # noqa: SLF001
        window.task_scheduler.stop()
        window.engine.stop()
        window._stop_tray()  # noqa: SLF001
    except Exception:  # noqa: BLE001
        pass
    try:
        window.root.quit()
        window.root.destroy()
    except Exception:  # noqa: BLE001
        pass
    pump_cleanup()


def pump_cleanup() -> None:
    """销毁后让 Tk 有机会收尾。"""
    time.sleep(0.2)


def main() -> int:
    winapi.set_dpi_awareness()
    store = UsageStore()
    now = datetime.now()
    # 一个"已经过了开始时间、但仍在补发窗口内"的每日任务（模拟关机错过）
    start = (now - timedelta(minutes=2)).strftime("%H:%M")
    end = (now + timedelta(minutes=90)).strftime("%H:%M")
    run_id = store.add_task("每日跑步（已错过）", task_type=R.TYPE_DAILY,
                            time_start=start, time_end=end, remind_before_minutes=15)
    study_id = store.add_task("高数学习（已错过）", task_type=R.TYPE_DAILY,
                              time_start=start, time_end=end, remind_before_minutes=15)
    single_id = store.add_task("交周报", due_at=now + timedelta(hours=3))

    print("=== 端到端：开机『错过与补发』闭环 ===")
    print(f"  数据目录: {DATA_DIR}")
    print(f"  周期任务时间窗: {start}~{end}（提前 15 分钟，现在 {now:%H:%M} 已过开始）")
    print("  补发窗口: 5 分钟　·　单次任务: 交周报")

    # ================================================================ 第一次开机
    print("\n-- 第 1 次开机（应补发并合并成一个窗）--")
    window, titles = run_session(store)
    check("只弹出 1 个提醒窗（合并成一次打扰）", len(titles) == 1, str(titles))
    check("弹窗是『开机提醒』", titles == ["TimeGuard 开机提醒"], str(titles))

    # 内容检查：弹窗里应当同时有周期任务与待办
    dialogs = reminder_dialogs(window)
    if dialogs:
        tip = dialogs[0]
        check("弹窗同时展示周期任务与待办两部分",
              bool(tip.recurring_items) and bool(tip.tasks),
              f"周期 {len(tip.recurring_items)} 项 / 待办 {len(tip.tasks)} 条")
        check("补发内容被标记为迟到",
              all(item.is_late for item in tip.recurring_items),
              tip.recurring_items[0].kind_label if tip.recurring_items else "")
        check("待办区块没有混进周期任务",
              all(not task.is_recurring for task in tip.tasks),
              "、".join(task.title for task in tip.tasks))
        check("待办区块含未完成的单次任务",
              any(task.id == single_id for task in tip.tasks))
    close_dialogs(window)

    logs = {row.task_id: row for row in store.task_logs_on(date.today())}
    check("两个周期任务都写入发生记录并标为 late",
          all(logs.get(i) is not None and logs[i].remind_kind == "late"
              for i in (run_id, study_id)),
          str([(i, logs[i].remind_kind) for i in (run_id, study_id) if i in logs]))
    shutdown(window)

    # ================================================================ 第二次开机
    print("\n-- 第 2 次开机（同一次发生不应重复补发）--")
    window, titles = run_session(store)
    check("不再重复补发（只剩未完成待办那一个窗）", len(titles) == 1, str(titles))
    dialogs = reminder_dialogs(window)
    if dialogs:
        tip = dialogs[0]
        check("第二次开机没有周期任务的补发内容", not tip.recurring_items,
              f"周期 {len(tip.recurring_items)} 项")
        check("第二次开机仍展示未完成待办", bool(tip.tasks),
              f"待办 {len(tip.tasks)} 条")
    close_dialogs(window)
    shutdown(window)

    # ================================================================ 同一天第二次启动
    print("\n-- 同一天再启动一次（弹窗每天只弹一次，但不该丢提醒）--")
    window, titles = run_session(store, fresh_day=False)
    check("同一天第二次启动不再弹开机提醒窗", titles == [], str(titles))
    close_dialogs(window)
    shutdown(window)

    # ================================================================ 补发窗口很窄
    print("\n-- 补发窗口设为 0 分钟（不该补发）--")
    today = date.today().strftime("%Y-%m-%d")
    for task_id in (run_id, study_id):
        store._conn.execute(  # noqa: SLF001
            "UPDATE task_logs SET remind_at = NULL, remind_kind = NULL "
            "WHERE task_id = ? AND occur_date = ?", (task_id, today))
    store._conn.commit()  # noqa: SLF001
    window, titles = run_session(store, catchup_minutes=0)
    dialogs = reminder_dialogs(window)
    check("补发窗口为 0 时不补发周期任务",
          all(not tip.recurring_items for tip in dialogs),
          f"弹窗 {titles}")
    close_dialogs(window)
    shutdown(window)

    # ================================================================ 关闭补发功能
    print("\n-- 关闭『开机补发』开关（不该补发）--")
    for task_id in (run_id, study_id):
        store._conn.execute(  # noqa: SLF001
            "UPDATE task_logs SET remind_at = NULL, remind_kind = NULL "
            "WHERE task_id = ? AND occur_date = ?", (task_id, today))
    store._conn.commit()  # noqa: SLF001
    window, titles = run_session(store, catchup=False, catchup_minutes=5)
    dialogs = reminder_dialogs(window)
    check("补发开关关闭时不补发周期任务",
          all(not tip.recurring_items for tip in dialogs),
          f"弹窗 {titles}")
    close_dialogs(window)
    shutdown(window)

    # ================================================================ 数据层口径
    print("\n-- 数据层口径 --")
    pending_ids = {task.id for task in store.pending_tasks()}
    check("pending_tasks() 不含周期任务", run_id not in pending_ids and study_id not in pending_ids)
    check("pending_tasks() 含未完成单次任务", single_id in pending_ids)
    recurring_ids = {task.id for task in store.list_tasks("all", recurring=True)}
    check("list_tasks(recurring=True) 只返回周期任务",
          {run_id, study_id} <= recurring_ids and single_id not in recurring_ids)

    print("-" * 60)
    print(f"结果: {'全部通过' if not FAIL else '有失败项'}（通过 {len(PASS)} / 失败 {len(FAIL)}）")
    for item in FAIL:
        print(f"  失败: {item}")
    store.close()
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

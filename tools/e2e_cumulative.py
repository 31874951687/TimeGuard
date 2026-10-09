"""端到端验证：累计打卡任务（v1.5）。

用户给的功能需求 + 5 条避坑指南就是验收标准，所以断言直接按那 5 条组织：

1. **完成后停止提醒**：达标之后调度器不再排任何定时器（不弹"该跑步了"）
2. **自然日边界**：00:10 打卡算新的一天；同一天只能打一次卡
3. 与时间监控的联动：v1.5 是"手动一键打卡"（诚信机制），不做自动判定
4. **数据结构**：打卡历史进 check_in_logs，进度条与热力图都从它来
5. **逾期定义**：今天 > 截止日 且没攒够才算逾期；截止日当天仍可打卡（压哨完成）

跑法::

    python tools/e2e_cumulative.py
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
os.environ["TIMEGUARD_DATA_DIR"] = tempfile.mkdtemp(prefix="tg_e2e_cumulative_")
os.environ.setdefault("TIMEGUARD_NO_TOAST", "1")   # 测试不发真实系统通知

from timeguard import recurrence as R  # noqa: E402
from timeguard import winapi  # noqa: E402
from timeguard.config import Config  # noqa: E402
from timeguard.database import UsageStore  # noqa: E402
from timeguard.engine import Snapshot, UsageEngine  # noqa: E402
from timeguard.notifier import Notifier  # noqa: E402
from timeguard.scheduler import KIND_CHECKIN, compute_schedule  # noqa: E402
from timeguard.tasks import GROUP_CHECKIN, build_task_rows  # noqa: E402
from timeguard.ui import MainWindow  # noqa: E402
from timeguard.utils import use_utf8_console  # noqa: E402

OUT = ROOT / "tools" / "_shots"
PASS: list[str] = []
FAIL: list[str] = []
STATE = {"done": False}
WATCHDOG_MS = 180000


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  [通过] {label}{('　' + detail) if detail else ''}")
        PASS.append(label)
    else:
        print(f"  [失败] {label}{('　' + detail) if detail else ''}")
        FAIL.append(label)


def save_shot(widget, name: str) -> None:
    """整窗截图（屏幕抓图：PrintWindow 对部分窗口客户区是空白）。"""
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


def main() -> int:
    use_utf8_console()
    winapi.set_dpi_awareness()
    today = date.today()

    cfg = Config()
    cfg.settings.enable_popup = False
    cfg.settings.task_check_on_start = False
    cfg.settings.task_reminder_toast = False
    cfg.settings.recurring_reminder_enabled = True
    cfg.settings.recurring_late_catchup = True
    cfg.settings.recurring_catchup_minutes = 5
    cfg.save()

    store = UsageStore()
    notifier = Notifier()
    engine = UsageEngine(cfg, store, notifier)
    win = MainWindow(cfg, store, engine, notifier)
    panel = win.task_panel

    def pump(seconds: float = 1.0) -> None:
        end = time.time() + seconds
        while time.time() < end:
            win.root.update()
            time.sleep(0.02)

    def select_task(task_id: int) -> str:
        """选中某条任务（每次都要重新查 item id：refresh() 会重建整棵树）。"""
        item = next(i for i, r in panel._row_map.items()   # noqa: SLF001
                    if r.task.id == task_id)
        panel.tree.selection_set(item)
        panel._update_hint()                               # noqa: SLF001
        return item

    def run() -> None:
        print("=== 端到端：累计打卡任务（v1.5）===")
        deadline = today + timedelta(days=60)

        # ---------------------------------------------------------- 建任务（真实对话框）
        print("-- 1) 用真实对话框建一条「年末前完成 60 次两公里跑」--")
        import timeguard.tasks as tasks_mod

        real_dialog = tasks_mod.TaskDialog
        captured: dict = {}

        class Auto(real_dialog):                       # type: ignore[valid-type,misc]
            def __init__(self, master, **kw):          # noqa: ANN001, ANN003
                super().__init__(master, **kw)
                self.after(20, self._fill)

            def _fill(self) -> None:
                self.title_var.set("年末前完成 60 次两公里跑")
                self.type_var.set(R.TYPE_LABELS[R.TYPE_CUMULATIVE])
                self._on_type_change()                 # noqa: SLF001
                self.target_var.set("60")
                self.deadline_picker.set_date(deadline)
                self.remind_time_field.set("18:00")
                self._update_preview()                 # noqa: SLF001
                captured["preview"] = self.preview_var.get()
                self.update_idletasks()                # 让 grid 布局落定，下面才读得到真实状态
                # 用 grid_info() 判断"这一栏有没有被网格管理"：grid_remove 之后它是空的。
                # 不用 winfo_ismapped()，那个要求窗口已经映射到屏幕，容易误判。
                captured["frames"] = (bool(self.once_frame.grid_info()),
                                      bool(self.recur_frame.grid_info()),
                                      bool(self.cum_frame.grid_info()))
                self._confirm()

        tasks_mod.TaskDialog = Auto                     # type: ignore[assignment]
        panel.add_task()
        tasks_mod.TaskDialog = real_dialog              # type: ignore[assignment]
        pump(1.2)

        tasks = store.cumulative_tasks()
        check("对话框建出了累计打卡任务", len(tasks) == 1, f"{[t.title for t in tasks]}")
        if not tasks:
            finish(1)
            return
        task = tasks[0]
        # 把创建时间往前挪 90 天：下面要造 45 次历史打卡，而"补签"规则不允许补到
        # 任务创建之前（数据层的真实约束），所以这条演示任务必须"早就建好了"。
        store._conn.execute(                       # noqa: SLF001
            "UPDATE tasks SET created_at = ? WHERE id = ?",
            ((datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S"), task.id))
        store._conn.commit()                       # noqa: SLF001
        task = store.get_task(task.id)
        check("切换类型时只显示累计打卡那一栏",
              captured.get("frames") == (False, False, True), str(captured.get("frames")))
        check("预览写清了目标/截止/提醒，并说明不会提前算逾期",
              "目标 60 次" in captured.get("preview", "")
              and "还剩 60 天" in captured.get("preview", "")
              and "不会算逾期" in captured.get("preview", ""),
              captured.get("preview", ""))
        check("字段正确落库（目标/截止/每日提醒）",
              task.rule.target_count == 60 and task.rule.deadline == deadline
              and task.rule.remind_time is not None,
              f"{task.rule.target_count} / {task.rule.deadline} / {task.rule.remind_time}")
        check("周期字段留空（没有多出一份假计划）",
              task.rule.time_start is None or task.due_at is None,
              f"due_at={task.due_at}")
        row = store._conn.execute(                       # noqa: SLF001
            "SELECT time_start, time_end, days_of_week, due_at FROM tasks WHERE id = ?",
            (task.id,)).fetchone()
        check("库里 time_start/time_end/days_of_week/due_at 都是空",
              all(row[key] is None for key in ("time_start", "time_end", "days_of_week", "due_at")),
              str(dict(row)))

        # ---------------------------------------------------------- 打卡
        print("-- 2) 打卡：次数、防重复、撤销 --")
        target_item = next(item for item, r in panel._row_map.items()   # noqa: SLF001
                           if r.task.id == task.id)
        panel.tree.selection_set(target_item)
        panel._update_hint()                                            # noqa: SLF001
        check("选中打卡任务时「打卡」按钮可用且文案正确",
              "disabled" not in panel.checkin_button.state()
              and panel.checkin_button.cget("text") == "✓ 今日打卡",
              panel.checkin_button.cget("text"))
        panel.check_in_selected()          # 走界面路径（不是直接调数据层）
        pump(0.4)
        check("界面按钮打卡成功", store.checkin_count(task.id) == 1,
              f"{store.checkin_count(task.id)} 次")
        check("打完卡按钮变成「撤销今日打卡」",
              panel.checkin_button.cget("text") == "↩ 撤销今日打卡",
              panel.checkin_button.cget("text"))
        panel.check_in_selected()          # 再点一次 = 撤销
        pump(0.4)
        check("再点一次撤销今天的打卡", store.checkin_count(task.id) == 0)
        panel.check_in_selected()          # 撤销回来，后面按"今天已打卡"继续
        pump(0.4)
        again, again_msg = store.check_in(task.id)
        check("同一天再打一次被拦下", again is False and "已经打过卡" in again_msg, again_msg)
        check("已打卡次数正确", store.checkin_count(task.id) == 1)

        # 造历史：过去 44 天里隔天打一次 → 共 45 次
        for offset in range(1, 45):
            store.check_in(task.id, today - timedelta(days=offset))
        panel.refresh()
        pump(0.6)
        status = store.task_status(task.id)
        check("进度算对（45/60）", status.total == 45 and status.remaining == 15,
              status.progress_text())
        check("状态是「进行中」，文案里没有「逾期/失败」",
              status.state == R.STATE_RUNNING and "逾期" not in status.status_text()
              and "失败" not in status.status_text(), status.status_text())

        rows = build_task_rows(store, "pending", datetime.now()).get(GROUP_CHECKIN) or []
        check("列表里单独成组「累计打卡」", len(rows) == 1 and rows[0].is_cumulative)
        check("行里带进度与剩余天数（紧凑文案，列宽放得下）",
              "45/60" in rows[0].countdown_text and "还差 15" in rows[0].countdown_text
              and "剩 60 天" in rows[0].countdown_text, rows[0].countdown_text)
        check("计划列写明每日提醒时间与截止日",
              "18:00" in rows[0].schedule_text and "截止" in rows[0].schedule_text,
              rows[0].schedule_text)
        check("状态列是短文案（完整句子留给提示条与详情面板）",
              rows[0].status_text in ("✔ 已打卡", "○ 未打卡"), rows[0].status_text)
        counts = store.task_counts()
        check("截止日之前：今天的统计里没有「已超期」", counts.overdue == 0, counts.summary_text())
        check("今天已打卡 → 计入「已完成」，且不算逾期",
              counts.completed == 1 and counts.overdue == 0, counts.summary_text())
        # 撤销今天的打卡 → 应该变成"待办 / 今天到期"，但仍然不算逾期（这才是核心诉求）
        store.undo_check_in(task.id, today)
        counts = store.task_counts()
        check("今天没打卡 → 计入「待办 / 今天到期」，仍不算逾期",
              counts.pending == 1 and counts.due_today == 1 and counts.overdue == 0,
              counts.summary_text())
        store.check_in(task.id, today)          # 打回来，继续后面的流程
        stats = store.task_stats_by_day(7)
        check("7 天图表把打卡算成完成", sum(item.on_time for item in stats) >= 6,
              str([(s.day, s.on_time) for s in stats][-3:]))
        win.show_tasks_tab()                    # 先切到待办页，再截图（否则拍到的是统计页）
        pump(0.8)
        # 选中打卡任务 → 下半窗格应换成"进度条 + 热力图"
        select_task(task.id)
        pump(0.8)
        check("选中打卡任务后下半窗格换成打卡详情（图表让位）",
              panel._detail_visible is True                              # noqa: SLF001
              and panel.detail.status is not None
              and "45/60" in getattr(panel.detail.progress, "text", ""),
              f"detail_visible={panel._detail_visible} 进度条文字="       # noqa: SLF001
              f"{getattr(panel.detail.progress, 'text', '（无）')!r}")
        save_shot(win.root, "26_cumulative_panel")

        # ---------------------------------------------------------- 提醒
        print("-- 3) 每日提醒：到点提醒一次、打卡后顺延、达标后彻底停止 --")
        moments = datetime.combine(today, task.rule.remind_time)
        before = moments - timedelta(minutes=1)
        state = compute_schedule([task], {}, before, checkin_counts=store.checkin_counts(),
                                checked_today=store.checkin_ids_on(today))
        check("提醒时刻之前不打扰，定时器排到 18:00",
              state.batch is None and state.wakeup_at == moments, f"wakeup={state.wakeup_at}")

        # 注意：这里必须传"今天还没打卡"（checked_today 为空集）来模拟提醒时刻的真实场景；
        # 传真实的 checkin_ids_on(today) 会因为今天已打卡而被正确跳过（那是另一条断言）。
        state = compute_schedule([task], {}, moments, checkin_counts=store.checkin_counts(),
                                checked_today=set())
        check("到点派发「该打卡了」", state.batch is not None
              and state.batch.items[0].kind == KIND_CHECKIN
              and "已打卡 45/60 次" in state.batch.items[0].detail_text,
              state.batch.items[0].detail_text if state.batch else "（没有批次）")
        check("提醒文案说明了还差几次、还剩几天",
              state.batch is not None and "还差 15 次" in state.batch.items[0].detail_text
              and "还剩" in state.batch.items[0].detail_text,
              state.batch.items[0].detail_text if state.batch else "（没有批次）")

        store.check_in(task.id, today)              # 今天打卡
        state = compute_schedule([task], {}, moments + timedelta(minutes=30),
                                checkin_counts=store.checkin_counts(),
                                checked_today=store.checkin_ids_on(today))
        check("今天已打卡 → 今天不再提醒，顺延到明天",
              state.batch is None and state.wakeup_at is not None
              and state.wakeup_at.date() == today + timedelta(days=1),
              f"wakeup={state.wakeup_at}")

        # 补满到 60 次 → 必须彻底停止提醒（避坑 #1）
        for offset in range(45, 60):
            store.check_in(task.id, today + timedelta(days=0) - timedelta(days=offset))
        finished = store.task_status(task.id)
        check("攒够 60 次 → 提前完成", finished.state == R.STATE_DONE_EARLY
              and finished.reached_on is not None,
              f"{finished.state_label}（{finished.reached_on} 达标）")
        state = compute_schedule([task], {}, moments, checkin_counts=store.checkin_counts(),
                                checked_today=store.checkin_ids_on(today))
        check("达标后不再有任何提醒（连定时器都不排）",
              state.batch is None and state.wakeup_at is None, f"wakeup={state.wakeup_at}")
        panel.refresh()
        pump(0.6)
        check("达标后列表状态是「提前完成」",
              "提前完成" in (build_task_rows(store, "all", datetime.now())
                             .get(GROUP_CHECKIN) or [None])[0].status_text,
              (build_task_rows(store, "all", datetime.now())
               .get(GROUP_CHECKIN) or [None])[0].status_text)
        save_shot(win.root, "27_cumulative_finished")

        # ---------------------------------------------------------- 自然日 & 逾期边界
        print("-- 4) 自然日边界与逾期判定（纯逻辑，用注入的日期）--")
        ok_night, _ = store.check_in(task.id, now=datetime.combine(today, datetime.min.time())
                                     .replace(hour=23, minute=50))
        # 上面这次会被"同一天只能打一次"挡下（今天已经打过），正好验证防重复
        check("23:50 打卡仍算「今天」（被同一天去重规则挡下）", ok_night is False)

        # 压哨：把"最后一天"设成今天，前两次打卡放在过去（未来日期数据层会拒绝）
        fresh = store.add_task("压哨测试", task_type=R.TYPE_CUMULATIVE, target_count=3,
                               deadline=today.strftime("%Y-%m-%d"), remind_time="18:00")
        store._conn.execute(                       # noqa: SLF001
            "UPDATE tasks SET created_at = ? WHERE id = ?",
            ((datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S"), fresh))
        store._conn.commit()                       # noqa: SLF001
        for day in (today - timedelta(days=2), today - timedelta(days=1)):
            ok_day, msg_day = store.check_in(fresh, day)
            check(f"补上 {day:%m-%d} 那一次打卡", ok_day, msg_day)
        rule = store.get_task(fresh).rule
        dates = store.checkin_dates(fresh)
        on_deadline = R.cumulative_status(rule, dates, today)
        check("截止日当天没攒够 → 仍是「进行中」（今天还能打）",
              on_deadline.state == R.STATE_RUNNING and "逾期" not in on_deadline.status_text(),
              on_deadline.status_text())
        ok_last, msg_last = store.check_in(fresh, today)
        after = R.cumulative_status(rule, store.checkin_dates(fresh), today)
        check("截止日当天打满 → 压哨完成（不是逾期）",
              ok_last and after.state == R.STATE_DONE_DEADLINE, f"{after.state_label}（{msg_last}）")

        late_task = store.add_task("逾期测试", task_type=R.TYPE_CUMULATIVE, target_count=3,
                                   deadline=(today - timedelta(days=1)).strftime("%Y-%m-%d"),
                                   remind_time="18:00")
        store._conn.execute(                       # noqa: SLF001
            "UPDATE tasks SET created_at = ? WHERE id = ?",
            ((datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S"), late_task))
        store._conn.commit()                       # noqa: SLF001
        store.check_in(late_task, today - timedelta(days=2))
        late_rule = store.get_task(late_task).rule
        late_status = R.cumulative_status(late_rule, store.checkin_dates(late_task), today)
        check("今天 > 截止日 且没攒够 → 这时才判「逾期未达标」",
              late_status.state == R.STATE_MISSED and "逾期未达标" in late_status.status_text(),
              late_status.status_text())
        blocked, blocked_msg = store.check_in(late_task, today)
        check("过了截止日不能再打卡", blocked is False and "已过截止日期" in blocked_msg, blocked_msg)
        check("逾期后不计入「提前完成」", store.get_task(late_task).completed is False)

        # ---------------------------------------------------------- 第 4 条：数据结构
        print("-- 5) 打卡历史表（热力图/图表的数据来源）--")
        logs = store.checkin_logs(task.id)
        check("打卡历史按日期存了 60 条", len(logs) == 60, f"{len(logs)} 条")
        check("每条都有打卡时刻（精确到秒）",
              all(log.checkin_at is not None for log in logs))
        recent = store.checkin_logs(task.id, since=today - timedelta(days=6), until=today)
        check("能按区间取历史（热力图只用最近几周）", len(recent) == 7, f"{len(recent)} 条")
        check("批量取次数与历史（列表刷新不 N+1）",
              store.checkin_counts().get(task.id) == 60
              and len(store.checkin_dates_map().get(task.id, [])) == 60)

        # ---------------------------------------------------------- 补签（v1.5.1）
        print("-- 6) 补签：手动选历史日期 + 备注 --")
        bf_task = store.add_task("补签演示：每天背 30 个单词", task_type=R.TYPE_CUMULATIVE,
                                 target_count=30,
                                 deadline=(today + timedelta(days=45)).strftime("%Y-%m-%d"),
                                 remind_time="20:00")
        # 把创建时间往前挪 20 天，模拟"这条任务早就建好了"（否则没有可补的日子）
        store._conn.execute(                       # noqa: SLF001
            "UPDATE tasks SET created_at = ? WHERE id = ?",
            ((datetime.now() - timedelta(days=20)).strftime("%Y-%m-%d %H:%M:%S"), bf_task))
        store._conn.commit()                       # noqa: SLF001
        panel.refresh()
        select_task(bf_task)
        pump(0.5)

        real_backfill = tasks_mod.BackfillDialog
        bf_captured: dict = {}

        class AutoBackfill(real_backfill):          # type: ignore[valid-type,misc]
            def __init__(self, master, task, store_, **kw):     # noqa: ANN001, ANN003
                super().__init__(master, task, store_, **kw)
                self.after(20, self._fill)

            def _fill(self) -> None:
                bf_captured["default_day"] = self.picker.get_date()
                self.picker.set_date(today - timedelta(days=3))
                self.note_var.set("三天前出差忘了打卡")
                self._confirm()

        tasks_mod.BackfillDialog = AutoBackfill          # type: ignore[assignment]
        panel.backfill_selected()                        # 走右键菜单那条路径
        tasks_mod.BackfillDialog = real_backfill         # type: ignore[assignment]
        pump(0.8)

        check("补签对话框默认给的是昨天",
              bf_captured.get("default_day") == today - timedelta(days=1),
              str(bf_captured.get("default_day")))
        bf_log = store.checkin_log(bf_task, today - timedelta(days=3))
        check("补签写进了打卡历史", bf_log is not None)
        if bf_log is not None:
            check("来源标记为「补签」，备注也存下来了",
                  bf_log.source == "backfill" and bf_log.source_label == "补签"
                  and bf_log.note == "三天前出差忘了打卡",
                  f"{bf_log.source_label} / {bf_log.note}")
        check("补签算进进度", store.checkin_count(bf_task) == 1)
        bf_status = store.task_status(bf_task)
        check("补签不影响「今天」的状态（今天仍未打卡）",
              bf_status.today_checked is False and "今日未打卡" in bf_status.status_text(),
              bf_status.status_text())
        ok_future, msg_future = store.check_in(bf_task, today + timedelta(days=1))
        check("补签不能补未来", ok_future is False and "还没到" in msg_future, msg_future)
        ok_pre, msg_pre = store.check_in(bf_task, today - timedelta(days=30))
        check("补签不能补任务创建之前", ok_pre is False and "早于任务创建日期" in msg_pre, msg_pre)

        # ---------------------------------------------------------- 自动打卡（v1.5.1）
        print("-- 7) 监控时长达标自动打卡（可选功能）--")
        auto_task = store.add_task("高数刷题（自动打卡）", task_type=R.TYPE_CUMULATIVE,
                                   target_count=10,
                                   deadline=(today + timedelta(days=30)).strftime("%Y-%m-%d"),
                                   remind_time="21:00", auto_target="高数", auto_minutes=30)
        auto_rule = store.get_task(auto_task).rule
        check("自动打卡的配置落库并读得出来",
              auto_rule.auto_enabled and auto_rule.auto_minutes == 30
              and auto_rule.auto_checkin_text() == "自动打卡：高数 今日累计满 30 分钟",
              auto_rule.auto_checkin_text())

        def run_auto_pass(targets) -> None:          # noqa: ANN001
            # 用**真实的 Snapshot**（不是自己搓的假对象）：界面每秒的 tick 会读它的
            # 其它字段，缺字段会让刷新直接抛异常（踩过一次）。
            win.snapshot = Snapshot(day=today.strftime("%Y-%m-%d"), target_seconds=targets)
            win._auto_check_in_pass()                # noqa: SLF001 - 直接跑那一趟
            pump(0.3)

        run_auto_pass({"高数": 20 * 60, "游戏": 3600})          # 还没到阈值
        check("时长不够时不会自动打卡", store.checkin_count(auto_task) == 0,
              f"{store.checkin_count(auto_task)} 次")
        ready, why = R.auto_checkin_ready(auto_rule, 20 * 60)
        check("规则也给出「还差多少分钟」的说明", ready is False and "还差 10 分钟" in why, why)

        run_auto_pass({"高数 - 学习": 20 * 60, "高数": 11 * 60})  # 两个对象加起来 31 分钟
        check("时长够了就自动打卡（多个匹配对象时长相加）",
              store.checkin_count(auto_task) == 1, f"{store.checkin_count(auto_task)} 次")
        auto_log = store.checkin_log(auto_task, today)
        check("来源标记为「自动打卡」，备注写明是谁触发的",
              auto_log is not None and auto_log.source == "auto"
              and "自动打卡" in auto_log.note and "高数" in auto_log.note,
              f"{auto_log.source_label if auto_log else '—'} / {auto_log.note if auto_log else '—'}")

        run_auto_pass({"高数": 99 * 60})                          # 已经打过卡了
        check("同一天不会再自动打第二次", store.checkin_count(auto_task) == 1,
              f"{store.checkin_count(auto_task)} 次")

        ignore_task = store.add_task("没开自动打卡的", task_type=R.TYPE_CUMULATIVE,
                                     target_count=5,
                                     deadline=(today + timedelta(days=30)).strftime("%Y-%m-%d"),
                                     remind_time="21:00")
        run_auto_pass({"高数": 99 * 60})
        check("没配自动打卡的任务不会被误打", store.checkin_count(ignore_task) == 0)

        # 快照是昨天的 → 整轮跳过（绝不能拿昨天的数据写今天的卡）
        win.snapshot = Snapshot(day=(today - timedelta(days=1)).strftime("%Y-%m-%d"),
                                target_seconds={"高数": 99 * 60})
        store.undo_check_in(auto_task, today)
        store.undo_check_in(auto_task, today)          # 撤两次，第二次无事发生
        win._auto_check_in_pass()                      # noqa: SLF001
        pump(0.3)
        check("快照不是今天的 → 不写库（防串日）", store.checkin_count(auto_task) == 0,
              f"{store.checkin_count(auto_task)} 次")
        # 打回来，继续后面的统计断言
        run_auto_pass({"高数": 31 * 60})
        check("换回今天的快照后又能自动打卡",
              store.checkin_count(auto_task) == 1, f"{store.checkin_count(auto_task)} 次")
        panel.refresh()
        pump(0.5)
        select_task(auto_task)
        pump(0.6)
        check("详情面板把自动打卡规则写在计划行里",
              "自动打卡" in panel.detail.plan_var.get()
              or "自动打卡" in panel.detail.status_var.get()
              or "30 分钟" in panel.detail.plan_var.get(),
              panel.detail.plan_var.get())
        # 排版断言：详情面板最下面那行按钮不能被裁掉（实测短窗口下会只剩一条边）
        # 用**屏幕绝对坐标**比较：按钮底边不能超过详情面板的底边
        detail = panel.detail
        btn_bottom = detail.backfill_button.winfo_rooty() + detail.backfill_button.winfo_height()
        frame_bottom = panel.detail_frame.winfo_rooty() + panel.detail_frame.winfo_height()
        check("详情面板的按钮行没被裁切（在可见区域内）",
              detail.backfill_button.winfo_ismapped() and 0 < btn_bottom <= frame_bottom,
              f"按钮底边 {btn_bottom} / 面板底边 {frame_bottom}"
              f"（面板高 {panel.detail_frame.winfo_height()}px，"
              f"需要 {detail.winfo_reqheight()}px）")
        check("进度条与热力图都画出来了",
              detail.progress.winfo_ismapped() and detail.heatmap.winfo_ismapped()
              and len(detail.heatmap.canvas.find_all()) > 0,
              f"热力图元素 {len(detail.heatmap.canvas.find_all())} 个")
        save_shot(win.root, "29_cumulative_auto")

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

    def watchdog() -> None:
        if STATE["done"]:
            return
        print(f"!! 超过 {WATCHDOG_MS / 1000:.0f} 秒仍未跑完，判定为卡死 !!")
        FAIL.append("流程超时")
        finish(1)

    win.root.after(WATCHDOG_MS, watchdog)
    win.root.after(1200, run)
    win.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

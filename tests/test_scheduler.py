"""周期性任务调度器单元测试（阶段 2）。

覆盖：
  * :func:`compute_schedule` 纯函数 —— 提前窗口 / 迟到补发窗口 / 合并成批 / 去重
  * :class:`TaskScheduler` —— 单个定时器的排程、到点派发、重复不打扰

所有时间都是**注入**的，因此结果确定、不受运行时刻影响。
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path

os.environ["TIMEGUARD_DATA_DIR"] = tempfile.mkdtemp(prefix="timeguard_sched_test_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from timeguard import recurrence as R  # noqa: E402
from timeguard.utils import use_utf8_console  # noqa: E402
from timeguard.database import UsageStore  # noqa: E402
from timeguard.scheduler import (  # noqa: E402
    KIND_ADVANCE,
    KIND_CHECKIN,
    KIND_LATE,
    TaskScheduler,
    compute_schedule,
)


def _store() -> UsageStore:
    return UsageStore(Path(tempfile.mkdtemp(prefix="tg_sched_case_")) / "usage.db")


def _recurring(store: UsageStore, title: str, **kwargs) -> int | None:
    """建一条周期任务，供"固定日期推演"的调度用例使用。

    ``start_date=""``：这些用例都在模拟某个**固定的过去/未来日期**（例如
    "2026-10-08 12:00 时应该几点唤醒"），必须绕开 v1.4 的"新建时若今天时间窗已过
    就从明天开始"的自动生效日 —— 否则脚本在晚上跑，18:00~20:00 的任务会被判成
    "明天才生效"，整套推演全部落空。
    """
    kwargs.setdefault("start_date", "")
    return store.add_task(title, **kwargs)


def _at(*args) -> datetime:
    return datetime(*args)


class _FakeSettings:
    """只带调度器关心的字段。"""

    def __init__(self, **kwargs) -> None:
        self.recurring_reminder_enabled = kwargs.get("recurring_reminder_enabled", True)
        self.recurring_late_catchup = kwargs.get("recurring_late_catchup", True)
        self.recurring_catchup_minutes = kwargs.get("recurring_catchup_minutes", 5)
        self.scheduler_max_sleep_minutes = kwargs.get("scheduler_max_sleep_minutes", 30)


class _FakeRoot:
    """假 Tk root：记录排程，允许手动"触发到点"。"""

    def __init__(self) -> None:
        self.scheduled: list[tuple[int, object]] = []
        self.cancelled: list[str] = []
        self._seq = 0

    def after(self, ms: int, callback) -> str:
        self._seq += 1
        token = f"after{self._seq}"
        self.scheduled.append((int(ms), callback))
        self._token_for = getattr(self, "_token_for", {})
        self._token_for[token] = (int(ms), callback)
        return token

    def after_cancel(self, token: str) -> None:
        self.cancelled.append(token)
        self._token_for.pop(token, None)

    def pop_pending(self) -> list:
        """取出当前待执行的回调。"""
        items = [cb for _ms, cb in self.scheduled]
        self.scheduled.clear()
        return items


def _make_scheduler(store, settings=None, now=None, on_batch=None):
    """构造调度器，时间可控。"""
    clock = {"now": now or _at(2026, 10, 8, 12, 0)}
    root = _FakeRoot()
    batches: list = []
    scheduler = TaskScheduler(
        root, store,
        on_batch or (lambda batch: batches.append(batch)),
        settings or _FakeSettings(),
        now_provider=lambda: clock["now"],
    )
    scheduler._test_clock = clock           # noqa: SLF001 - 测试用
    scheduler._test_root = root             # noqa: SLF001
    scheduler._test_batches = batches       # noqa: SLF001
    return scheduler


# ================================================================ 纯函数：累计打卡任务
def _cumulative(store, title: str = "年末前完成 60 次两公里跑", target: int = 60,
                deadline: str = "2026-12-31", remind: str = "18:00") -> int:
    """建一条累计打卡任务（固定日期用，跟 _recurring 一样绕开自动生效日）。

    顺便把创建时间往前挪：``check_in`` 会拒绝"早于任务创建日期"的补签
    （那是真实约束），而这些用例要造历史打卡记录，任务必须"早就建好了"。
    """
    task_id = store.add_task(title, task_type=R.TYPE_CUMULATIVE, target_count=target,
                             deadline=deadline, remind_time=remind)
    assert task_id is not None
    stamp = (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d %H:%M:%S")
    store._conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?",  # noqa: SLF001
                        (stamp, task_id))
    store._conn.commit()                                                  # noqa: SLF001
    return task_id


def _sched(store, now, **kwargs):
    """用给定时刻算一次调度（把累计打卡任务的次数也一并读出来）。"""
    tasks = store.recurring_tasks() + store.cumulative_tasks()
    logs = {}
    for offset in (-1, 0, 1):
        day = (now + timedelta(days=offset)).date()
        for log_row in store.task_logs_on(day):
            logs[(log_row.task_id, log_row.occur_date)] = log_row
    return compute_schedule(tasks, logs, now,
                            checkin_counts=store.checkin_counts(),
                            checked_today=store.checkin_ids_on(now.date()),
                            **kwargs)


def test_cumulative_waits_until_remind_time() -> None:
    """18:00 提醒：17:59 不打扰，但定时器要排到 18:00。"""
    store = _store()
    _cumulative(store)
    state = _sched(store, _at(2026, 11, 20, 17, 59))
    assert state.batch is None
    assert state.wakeup_at == _at(2026, 11, 20, 18, 0)
    store.close()


def test_cumulative_fires_at_remind_time() -> None:
    """到点派发一条"该打卡了"，并带上进度文案。"""
    store = _store()
    _cumulative(store)
    today = date.today()
    for offset in (1, 2):                       # 先打两次卡（只能是过去的日子）
        assert store.check_in(store.cumulative_tasks()[0].id,
                              today - timedelta(days=offset))[0]
    state = _sched(store, _at(2026, 11, 20, 18, 0))
    assert state.batch is not None and state.batch.count == 1
    item = state.batch.items[0]
    assert item.kind == KIND_CHECKIN
    assert item.is_cumulative and item.occur is None
    assert item.kind_label == "该打卡了"
    assert item.done_count == 2
    assert "已打卡 2/60 次" in item.detail_text and "还差 58 次" in item.detail_text
    assert "还剩 41 天" in item.detail_text
    assert state.batch.headline() == "该打卡了：年末前完成 60 次两公里跑"
    store.close()


def test_cumulative_reminder_stops_once_target_reached() -> None:
    """避坑 #1：达标之后**不再有任何提醒**（连定时器都不排）。"""
    store = _store()
    task_id = _cumulative(store, target=3, deadline="2026-12-31")
    for offset in range(3):
        assert store.check_in(task_id, date.today() - timedelta(days=3 - offset))[0]
    state = _sched(store, _at(2026, 11, 20, 18, 0))
    assert state.batch is None, "达标后还在提醒 = 骚扰"
    assert state.wakeup_at is None
    assert state.has_work is False
    store.close()


def test_cumulative_reminder_stops_after_deadline() -> None:
    """过了截止日也不再提醒（任务已结束）。"""
    store = _store()
    _cumulative(store, target=60, deadline="2026-11-19")
    assert _sched(store, _at(2026, 11, 19, 18, 0)).batch is not None      # 最后一天还能提醒
    after = _sched(store, _at(2026, 11, 20, 18, 0))
    assert after.batch is None and after.wakeup_at is None
    store.close()


def test_cumulative_not_reminded_again_today() -> None:
    """同一天只提醒一次：已经提醒过 → 今天不再弹，定时器排到明天。"""
    store = _store()
    task_id = _cumulative(store)
    assert store.log_occurrence(task_id, date(2026, 11, 20),
                                remind_at=_at(2026, 11, 20, 18, 0),
                                remind_kind=KIND_CHECKIN) is True
    state = _sched(store, _at(2026, 11, 20, 19, 30))
    assert state.batch is None
    assert state.wakeup_at == _at(2026, 11, 21, 18, 0)
    store.close()


def test_cumulative_already_checked_in_today_skips_to_tomorrow() -> None:
    """今天已经打过卡 → 今天不再提醒。"""
    store = _store()
    task_id = _cumulative(store)
    today = date.today()
    assert store.check_in(task_id, today)[0] is True
    state = _sched(store, datetime.combine(today, time(18, 30)))
    assert state.batch is None
    assert state.wakeup_at == datetime.combine(today + timedelta(days=1), time(18, 0))
    store.close()


def test_cumulative_late_catchup_within_window() -> None:
    """刚错过几分钟（开机补发窗口内）→ 补一条"迟到提醒"。"""
    store = _store()
    _cumulative(store)
    state = _sched(store, _at(2026, 11, 20, 18, 3), catchup_minutes=5)
    assert state.batch is not None and state.batch.count == 1
    item = state.batch.items[0]
    assert item.kind == KIND_LATE and item.overdue_minutes == 3
    assert "迟到提醒" in item.kind_label


def test_cumulative_missed_long_ago_does_not_nag() -> None:
    """错过太久（18:00 的提醒，21:00 才开机）→ 今天不再打扰，直接排到明天。"""
    store = _store()
    _cumulative(store)
    state = _sched(store, _at(2026, 11, 20, 21, 0), catchup_minutes=5)
    assert state.batch is None
    assert state.wakeup_at == _at(2026, 11, 21, 18, 0)
    store.close()


def test_cumulative_and_periodic_merge_into_one_batch() -> None:
    """核心交互不变量：同一刻到点的周期任务与打卡任务**只弹一个窗**。"""
    store = _store()
    _recurring(store, "跑步", task_type=R.TYPE_DAILY, time_start="18:00", time_end="20:00",
               remind_before_minutes=0)
    _cumulative(store, remind="18:00")
    state = _sched(store, _at(2026, 11, 20, 18, 0))
    assert state.batch is not None and state.batch.count == 2, state.batch
    kinds = {item.kind for item in state.batch.items}
    assert kinds == {KIND_ADVANCE, KIND_CHECKIN}
    assert state.batch.headline() == "2 项任务即将开始"     # 混合批次用中性标题
    lines = state.batch.summary_lines()
    assert any("该打卡了" in line for line in lines)
    assert any("即将开始" in line for line in lines)
    store.close()


def test_scheduler_dispatch_marks_and_does_not_repeat() -> None:
    """通过 TaskScheduler 走完整流程：派发 → 写 task_logs → 不再重复提醒。"""
    store = _store()
    _cumulative(store, target=60, deadline="2026-12-31")
    scheduler = _make_scheduler(store, now=_at(2026, 11, 20, 18, 0))
    scheduler.start()
    assert len(scheduler._test_batches) == 1                # noqa: SLF001
    batch = scheduler._test_batches[0]                      # noqa: SLF001
    assert batch.items[0].kind == KIND_CHECKIN
    log_row = store.get_task_log(batch.items[0].task.id, date(2026, 11, 20))
    assert log_row is not None and log_row.remind_at is not None
    assert log_row.remind_kind == KIND_CHECKIN
    # 同一时刻再算一次：不该重复提醒，而是排到明天
    scheduler._test_clock["now"] = _at(2026, 11, 20, 18, 30)  # noqa: SLF001
    state = scheduler.state()
    assert state.batch is None
    assert state.wakeup_at == _at(2026, 11, 21, 18, 0)
    store.close()


def test_scheduler_upcoming_includes_cumulative() -> None:
    """下一次提醒预览也要包含累计打卡任务（界面用）。"""
    store = _store()
    _cumulative(store)
    scheduler = _make_scheduler(store, now=_at(2026, 11, 20, 9, 0))
    upcoming = scheduler.upcoming()
    assert len(upcoming) == 1
    task, moment = upcoming[0]
    assert task.is_cumulative and moment == _at(2026, 11, 20, 18, 0)
    # 打满目标后不再出现在预览里
    task_id = task.id
    for offset in range(60):
        assert store.check_in(task_id, date.today() - timedelta(days=60 - offset))[0]
    assert scheduler.upcoming() == []
    store.close()


# ================================================================ 纯函数：提前提醒
def test_no_reminder_before_window() -> None:
    store = _store()
    _recurring(store, "跑步", task_type=R.TYPE_DAILY, time_start="18:00", time_end="20:00",
                   remind_before_minutes=15)
    tasks = store.recurring_tasks()
    state = compute_schedule(tasks, {}, _at(2026, 10, 8, 12, 0))
    assert state.batch is None                                  # 还早，不打扰
    assert state.wakeup_at == _at(2026, 10, 8, 17, 45)          # 下一次唤醒 = 提前提醒时刻
    store.close()


def test_reminder_fires_exactly_at_lead_time() -> None:
    store = _store()
    _recurring(store, "跑步", task_type=R.TYPE_DAILY, time_start="18:00", time_end="20:00",
                   remind_before_minutes=15)
    tasks = store.recurring_tasks()
    # 17:44:59 还差一秒 → 不派发
    state = compute_schedule(tasks, {}, _at(2026, 10, 8, 17, 44, 59))
    assert state.batch is None
    # 17:45:00 正好到点 → 派发
    state = compute_schedule(tasks, {}, _at(2026, 10, 8, 17, 45, 0))
    assert state.batch is not None and state.batch.count == 1
    item = state.batch.items[0]
    assert item.title == "跑步"
    assert item.kind == KIND_ADVANCE
    assert item.is_late is False
    assert item.window_text == "18:00~20:00"
    assert item.remind_at == _at(2026, 10, 8, 17, 45)
    store.close()


def test_remind_before_zero_fires_at_start() -> None:
    store = _store()
    _recurring(store, "准点", task_type=R.TYPE_DAILY, time_start="18:00", time_end="19:00",
                   remind_before_minutes=0)
    tasks = store.recurring_tasks()
    assert compute_schedule(tasks, {}, _at(2026, 10, 8, 17, 59)).batch is None
    state = compute_schedule(tasks, {}, _at(2026, 10, 8, 18, 0))
    assert state.batch is not None and state.batch.count == 1
    store.close()


# ================================================================ 纯函数：合并与去重
def test_simultaneous_tasks_merge_into_one_batch() -> None:
    """避坑 #5：同一时刻到点的多个任务必须合并成一批（一个弹窗）。"""
    store = _store()
    _recurring(store, "跑步", task_type=R.TYPE_DAILY, time_start="18:00", time_end="20:00")
    _recurring(store, "背单词", task_type=R.TYPE_DAILY, time_start="18:00", time_end="20:00")
    _recurring(store, "高数", task_type=R.TYPE_DAILY, time_start="18:00", time_end="20:00")
    tasks = store.recurring_tasks()
    state = compute_schedule(tasks, {}, _at(2026, 10, 8, 17, 45))
    assert state.batch is not None
    assert state.batch.count == 3                       # 一次回调带 3 条
    assert state.batch.titles == ["跑步", "背单词", "高数"]
    assert "3 项任务即将开始" in state.batch.headline()
    assert "跑步" in state.batch.notify_text()
    store.close()


def test_already_reminded_is_not_repeated() -> None:
    store = _store()
    task_id = _recurring(store, "跑步", task_type=R.TYPE_DAILY, time_start="18:00", time_end="20:00")
    store.log_occurrence(task_id, date(2026, 10, 8), remind_at=_at(2026, 10, 8, 17, 45),
                         remind_kind=KIND_ADVANCE)
    tasks = store.recurring_tasks()
    logs = {(log_row.task_id, log_row.occur_date): log_row
            for log_row in store.task_logs_on(date(2026, 10, 8))}
    state = compute_schedule(tasks, logs, _at(2026, 10, 8, 17, 50))
    assert state.batch is None                          # 提醒过就不再打扰
    assert state.wakeup_at == _at(2026, 10, 9, 17, 45)  # 直接排到明天
    store.close()


def test_reminder_not_suppressed_by_completion() -> None:
    """"今天已完成"不该压掉提醒（打卡不等于"不用提醒我了"）。"""
    store = _store()
    task_id = _recurring(store, "跑步", task_type=R.TYPE_DAILY, time_start="18:00", time_end="20:00")
    store.complete_occurrence(task_id, date(2026, 10, 8))
    tasks = store.recurring_tasks()
    logs = {(log_row.task_id, log_row.occur_date): log_row
            for log_row in store.task_logs_on(date(2026, 10, 8))}
    # 17:45 正是今天的提前提醒时刻：打卡记录里没有 remind_at，所以照常派发
    state = compute_schedule(tasks, logs, _at(2026, 10, 8, 17, 45))
    assert state.batch is not None and state.batch.count == 1
    # 派发之后的下一次唤醒是明天同一时刻
    logs[(task_id, "2026-10-08")] = store.get_task_log(task_id, date(2026, 10, 8))
    store.log_occurrence(task_id, date(2026, 10, 8), remind_at=_at(2026, 10, 8, 17, 45),
                         remind_kind=KIND_ADVANCE)
    logs = {(log_row.task_id, log_row.occur_date): log_row
            for log_row in store.task_logs_on(date(2026, 10, 8))}
    after = compute_schedule(tasks, logs, _at(2026, 10, 8, 17, 46))
    assert after.batch is None
    assert after.wakeup_at == _at(2026, 10, 9, 17, 45)
    store.close()


# ================================================================ 纯函数：迟到补发
def test_late_catchup_inside_window() -> None:
    """避坑 #1：关机错过提醒后，开机 5 分钟内补发一次并标记为迟到。"""
    store = _store()
    _recurring(store, "跑步", task_type=R.TYPE_DAILY, time_start="18:00", time_end="20:00",
                   remind_before_minutes=15)
    tasks = store.recurring_tasks()
    # 18:03（开始后 3 分钟，仍在 5 分钟补发窗口内）
    state = compute_schedule(tasks, {}, _at(2026, 10, 8, 18, 3), catchup_minutes=5)
    assert state.batch is not None and state.batch.count == 1
    item = state.batch.items[0]
    assert item.kind == KIND_LATE
    assert item.is_late is True
    assert item.overdue_minutes == 3
    assert "迟到" in item.kind_label
    assert state.batch.has_late is True
    store.close()


def test_no_catchup_outside_window() -> None:
    """超过补发窗口就不再补发（避免开机后弹一堆过期提醒）。"""
    store = _store()
    _recurring(store, "跑步", task_type=R.TYPE_DAILY, time_start="18:00", time_end="20:00",
                   remind_before_minutes=15)
    tasks = store.recurring_tasks()
    state = compute_schedule(tasks, {}, _at(2026, 10, 8, 18, 30), catchup_minutes=5)
    assert state.batch is None
    assert state.wakeup_at == _at(2026, 10, 9, 17, 45)     # 直接排到明天
    store.close()


def test_catchup_can_be_disabled() -> None:
    store = _store()
    _recurring(store, "跑步", task_type=R.TYPE_DAILY, time_start="18:00", time_end="20:00")
    tasks = store.recurring_tasks()
    state = compute_schedule(tasks, {}, _at(2026, 10, 8, 18, 3),
                             catchup_minutes=5, catchup_enabled=False)
    assert state.batch is None
    store.close()


def test_catchup_ignores_already_reminded() -> None:
    """提前提醒已经发过了，进入窗口期也不该再补发一条迟到提醒。"""
    store = _store()
    task_id = _recurring(store, "跑步", task_type=R.TYPE_DAILY, time_start="18:00", time_end="20:00")
    store.log_occurrence(task_id, date(2026, 10, 8), remind_at=_at(2026, 10, 8, 17, 45),
                         remind_kind=KIND_ADVANCE)
    tasks = store.recurring_tasks()
    logs = {(row.task_id, row.occur_date): row for row in store.task_logs_on(date(2026, 10, 8))}
    state = compute_schedule(tasks, logs, _at(2026, 10, 8, 18, 3), catchup_minutes=5)
    assert state.batch is None
    store.close()


def test_inside_window_after_start_uses_late_kind() -> None:
    """已经开始但还没结束、又没提醒过 → 按迟到提醒处理（而不是静默跳过）。"""
    store = _store()
    _recurring(store, "跑步", task_type=R.TYPE_DAILY, time_start="18:00", time_end="20:00")
    tasks = store.recurring_tasks()
    state = compute_schedule(tasks, {}, _at(2026, 10, 8, 19, 0), catchup_minutes=5)
    # 19:00 已经超过 5 分钟补发窗口 → 不打扰
    assert state.batch is None
    # 但补发窗口设大一点就会提醒（说明判定看的是窗口，不是"窗口是否进行中"）
    state = compute_schedule(tasks, {}, _at(2026, 10, 8, 19, 0), catchup_minutes=90)
    assert state.batch is not None and state.batch.items[0].kind == KIND_LATE
    store.close()


# ================================================================ 纯函数：每周任务
def test_weekly_task_only_reminds_on_its_days() -> None:
    """周三/周五 18:00：周二不提醒，周三提醒。"""
    store = _store()
    _recurring(store, "高数", task_type=R.TYPE_WEEKLY, days_of_week="3,5",
                   time_start="18:00", time_end="20:00", remind_before_minutes=15)
    tasks = store.recurring_tasks()

    # 2026-10-06 是周二 → 不派发，下次唤醒是周三 17:45
    tuesday = compute_schedule(tasks, {}, _at(2026, 10, 6, 17, 45))
    assert tuesday.batch is None
    assert tuesday.wakeup_at == _at(2026, 10, 7, 17, 45)

    # 2026-10-07 周三 → 派发
    wednesday = compute_schedule(tasks, {}, _at(2026, 10, 7, 17, 45))
    assert wednesday.batch is not None and wednesday.batch.count == 1
    assert wednesday.batch.items[0].title == "高数"
    store.close()


def test_weekly_skips_missed_day_to_next_scheduled() -> None:
    """周三错过了（超过窗口）→ 下次唤醒应落到周五，而不是继续盯着周三。"""
    store = _store()
    _recurring(store, "高数", task_type=R.TYPE_WEEKLY, days_of_week="3,5",
                   time_start="18:00", time_end="20:00")
    tasks = store.recurring_tasks()
    state = compute_schedule(tasks, {}, _at(2026, 10, 7, 20, 30), catchup_minutes=5)
    assert state.batch is None
    assert state.wakeup_at == _at(2026, 10, 9, 17, 45)      # 周五
    store.close()


def test_once_tasks_are_ignored_by_recurring_scheduler() -> None:
    """单次任务走原有"临近截止"通知，不该被周期调度器重复提醒。"""
    store = _store()
    store.add_task("单次任务", due_at=_at(2026, 10, 8, 18, 0))
    tasks = store.recurring_tasks()
    assert tasks == []
    state = compute_schedule(tasks, {}, _at(2026, 10, 8, 17, 45))
    assert state.batch is None and state.wakeup_at is None
    store.close()


def test_wakeup_is_earliest_across_tasks() -> None:
    store = _store()
    _recurring(store, "晚的", task_type=R.TYPE_DAILY, time_start="20:00", time_end="21:00",
                   remind_before_minutes=15)
    _recurring(store, "早的", task_type=R.TYPE_DAILY, time_start="09:00", time_end="10:00",
                   remind_before_minutes=30)
    tasks = store.recurring_tasks()
    state = compute_schedule(tasks, {}, _at(2026, 10, 8, 6, 0))
    assert state.batch is None
    assert state.wakeup_at == _at(2026, 10, 8, 8, 30)       # 09:00 - 30 分钟，更早
    store.close()


def test_empty_state_has_no_work() -> None:
    store = _store()
    state = compute_schedule([], {}, _at(2026, 10, 8, 12, 0))
    assert state.batch is None
    assert state.wakeup_at is None
    assert state.has_work is False
    store.close()


# ================================================================ TaskScheduler 定时器
def test_scheduler_schedules_single_timer_to_next_reminder() -> None:
    store = _store()
    _recurring(store, "跑步", task_type=R.TYPE_DAILY, time_start="18:00", time_end="20:00",
                   remind_before_minutes=15)
    scheduler = _make_scheduler(store, now=_at(2026, 10, 8, 12, 0))
    sleep_ms = scheduler.reschedule(reason="test")
    # 真实目标是 17:45（5 小时 45 分后），但单次睡眠被 30 分钟上限截断，
    # 到点后会重新计算 —— 这样休眠/改时钟都不会让提醒漂移。
    assert sleep_ms == 30 * 60 * 1000
    assert scheduler.state().wakeup_at == _at(2026, 10, 8, 17, 45)
    assert scheduler.running is True                            # 自动启动（幂等）
    assert len(scheduler._test_root.scheduled) == 1             # 只有一个定时器
    assert scheduler._test_batches == []                        # 还没到点，不派发
    store.close()


def test_scheduler_sleep_not_capped_when_close() -> None:
    """离提醒很近时不该被上限影响，睡眠就是精确的剩余时间。"""
    store = _store()
    _recurring(store, "跑步", task_type=R.TYPE_DAILY, time_start="18:00", time_end="20:00",
                   remind_before_minutes=15)
    scheduler = _make_scheduler(store, now=_at(2026, 10, 8, 17, 40))   # 还差 5 分钟
    assert scheduler.reschedule(reason="test") == 5 * 60 * 1000
    store.close()


def test_scheduler_dispatches_when_timer_fires() -> None:
    store = _store()
    _recurring(store, "跑步", task_type=R.TYPE_DAILY, time_start="18:00", time_end="20:00")
    _recurring(store, "背单词", task_type=R.TYPE_DAILY, time_start="18:00", time_end="20:00")
    scheduler = _make_scheduler(store, now=_at(2026, 10, 8, 12, 0))
    scheduler.start()

    # 到点：把时钟拨到 17:45，然后执行待回调
    scheduler._test_clock["now"] = _at(2026, 10, 8, 17, 45)     # noqa: SLF001
    for callback in scheduler._test_root.pop_pending():
        callback()

    assert len(scheduler._test_batches) == 1                    # 只回调一次
    batch = scheduler._test_batches[0]
    assert batch.count == 2                                     # 但包含两条任务
    # 已被标记"已提醒"，再次到点不会再发
    logs = store.task_logs_on(date(2026, 10, 8))
    assert len(logs) == 2
    assert all(log_row.remind_kind == KIND_ADVANCE for log_row in logs)
    assert all(log_row.remind_at is not None for log_row in logs)

    scheduler._test_clock["now"] = _at(2026, 10, 8, 17, 50)     # noqa: SLF001
    scheduler.reschedule(reason="again")
    assert len(scheduler._test_batches) == 1                    # 没有第二次
    store.close()


def test_scheduler_marks_before_callback_so_bad_ui_cannot_duplicate() -> None:
    """回调抛异常也必须已经标记过，避免界面报错导致无限重复提醒。"""
    store = _store()
    _recurring(store, "跑步", task_type=R.TYPE_DAILY, time_start="18:00", time_end="20:00")

    def boom(_batch):
        raise RuntimeError("界面炸了")

    scheduler = _make_scheduler(store, now=_at(2026, 10, 8, 17, 45), on_batch=boom)
    sleep_ms = scheduler.reschedule(reason="test")
    logs = store.task_logs_on(date(2026, 10, 8))
    assert len(logs) == 1 and logs[0].remind_at is not None      # 已标记
    # 下一次是明天 17:45（远），被 30 分钟上限截断
    assert sleep_ms == 30 * 60 * 1000
    assert scheduler.state().wakeup_at == _at(2026, 10, 9, 17, 45)
    store.close()


def test_scheduler_respects_max_sleep_cap() -> None:
    """单次睡眠不能太长，否则休眠/改时钟后会漂移。"""
    store = _store()
    _recurring(store, "跑步", task_type=R.TYPE_DAILY, time_start="18:00", time_end="20:00")
    settings = _FakeSettings(scheduler_max_sleep_minutes=30)
    scheduler = _make_scheduler(store, settings, now=_at(2026, 10, 8, 12, 0))
    sleep_ms = scheduler.reschedule(reason="test")
    assert sleep_ms == 30 * 60 * 1000                            # 被 30 分钟上限截断
    store.close()


def test_scheduler_disabled_by_settings() -> None:
    store = _store()
    _recurring(store, "跑步", task_type=R.TYPE_DAILY, time_start="18:00", time_end="20:00")
    settings = _FakeSettings(recurring_reminder_enabled=False)
    scheduler = _make_scheduler(store, settings, now=_at(2026, 10, 8, 17, 45))
    assert scheduler.reschedule(reason="test") is None
    assert scheduler._test_batches == []                        # 关闭后完全不动
    assert "关闭" in scheduler.status_text()
    store.close()


def test_scheduler_fire_now_for_testing() -> None:
    """"测试提醒"按钮用：立刻派发当前到点的那批。"""
    store = _store()
    _recurring(store, "跑步", task_type=R.TYPE_DAILY, time_start="18:00", time_end="20:00")
    scheduler = _make_scheduler(store, now=_at(2026, 10, 8, 17, 45))
    batch = scheduler.fire_now()
    assert batch is not None and batch.count == 1
    assert scheduler._test_batches[0] is batch
    assert scheduler.fire_now() is None                         # 再点一次没有内容了
    store.close()


def test_scheduler_stop_cancels_timer() -> None:
    store = _store()
    _recurring(store, "跑步", task_type=R.TYPE_DAILY, time_start="18:00", time_end="20:00")
    scheduler = _make_scheduler(store, now=_at(2026, 10, 8, 12, 0))
    scheduler.start()
    assert len(scheduler._test_root.scheduled) == 1
    scheduler.stop()
    assert scheduler.running is False
    assert scheduler._test_root.cancelled                     # 取消了定时器
    # stop 之后，到点回调即使被执行也不再排程（防止已取消的定时器复活）
    for callback in scheduler._test_root.pop_pending():
        callback()
    assert scheduler._test_root.scheduled == []
    store.close()


def test_scheduler_upcoming_preview() -> None:
    store = _store()
    _recurring(store, "晚", task_type=R.TYPE_DAILY, time_start="20:00", time_end="21:00",
                   remind_before_minutes=15)
    _recurring(store, "早", task_type=R.TYPE_DAILY, time_start="09:00", time_end="10:00",
                   remind_before_minutes=15)
    scheduler = _make_scheduler(store, now=_at(2026, 10, 8, 6, 0))
    items = scheduler.upcoming(limit=4)
    assert items[0][0].title == "早"
    assert items[0][1] == _at(2026, 10, 8, 8, 45)
    assert items[1][0].title == "晚"
    store.close()


def test_scheduler_status_text_mentions_next_wakeup() -> None:
    store = _store()
    _recurring(store, "跑步", task_type=R.TYPE_DAILY, time_start="18:00", time_end="20:00")
    scheduler = _make_scheduler(store, now=_at(2026, 10, 8, 12, 0))
    scheduler.start()
    text = scheduler.status_text()
    assert "下次唤醒" in text and "17:45" in text
    store.close()


def test_scheduler_handles_no_tasks() -> None:
    store = _store()
    scheduler = _make_scheduler(store, now=_at(2026, 10, 8, 12, 0))
    assert scheduler.reschedule(reason="test") is None
    assert scheduler._test_root.scheduled == []
    assert scheduler.state().has_work is False
    assert "暂无" in scheduler.status_text()
    store.close()


# ================================================================ 延后派发 / 静默派发（阶段 3）
def test_collect_due_does_not_dispatch() -> None:
    """:meth:`collect_due` 只取不派发 —— 供开机统一检查合并展示。"""
    store = _store()
    _recurring(store, "跑步", task_type=R.TYPE_DAILY, time_start="18:00", time_end="20:00")
    scheduler = _make_scheduler(store, now=_at(2026, 10, 8, 17, 45))
    batch = scheduler.collect_due()
    assert batch is not None and batch.count == 1
    assert scheduler._test_batches == []                     # 没有回调
    assert store.task_logs_on(date(2026, 10, 8)) == []        # 也没写库
    store.close()


def test_reschedule_deferred_does_not_dispatch() -> None:
    """开机时先 deferred 排程，把派发留给统一检查（否则会多弹一个窗）。"""
    store = _store()
    _recurring(store, "跑步", task_type=R.TYPE_DAILY, time_start="18:00", time_end="20:00")
    # 12:00 还没进提醒窗口（17:45 才到点），所以这里只有排程、没有派发内容
    scheduler = _make_scheduler(store, now=_at(2026, 10, 8, 12, 0))
    sleep_ms = scheduler.reschedule(reason="start", deferred=True)
    assert scheduler._test_batches == []
    assert store.task_logs_on(date(2026, 10, 8)) == []
    assert sleep_ms == 30 * 60 * 1000                        # 被最长睡眠上限截断
    assert scheduler.state().wakeup_at == _at(2026, 10, 8, 17, 45)
    store.close()


def test_reschedule_deferred_skips_dispatch_but_arms_timer() -> None:
    """deferred 在"到点那一刻"也必须只排程（把派发让给统一检查）。"""
    store = _store()
    _recurring(store, "跑步", task_type=R.TYPE_DAILY, time_start="18:00", time_end="20:00")
    scheduler = _make_scheduler(store, now=_at(2026, 10, 8, 17, 45))
    sleep_ms = scheduler.reschedule(reason="start", deferred=True)
    assert scheduler._test_batches == []                     # 没回调
    assert store.task_logs_on(date(2026, 10, 8)) == []       # 也没写标记
    # 定时器仍排好了（按"这批已派发"的虚拟状态算），不会退化成"不再唤醒"
    assert sleep_ms == 30 * 60 * 1000
    assert scheduler.running is True
    # 内容仍在：交给统一检查显式派发
    batch = scheduler.collect_due()
    assert batch is not None and batch.count == 1
    assert scheduler.dispatch(batch, silent=True) is True
    assert len(store.task_logs_on(date(2026, 10, 8))) == 1
    # 派发之后：不再有内容，下一次唤醒落到明天
    assert scheduler.collect_due() is None
    assert scheduler.state().wakeup_at == _at(2026, 10, 9, 17, 45)
    store.close()


def test_silent_dispatch_marks_without_callback() -> None:
    """静默派发：只写"已提醒"，不再回调（内容已由开机弹窗展示）。"""
    store = _store()
    _recurring(store, "跑步", task_type=R.TYPE_DAILY, time_start="18:00", time_end="20:00")
    scheduler = _make_scheduler(store, now=_at(2026, 10, 8, 17, 45))
    batch = scheduler.collect_due()
    assert scheduler.dispatch(batch, silent=True) is True
    assert scheduler._test_batches == []                     # 没有回调 → 不会多弹一个窗
    logs = store.task_logs_on(date(2026, 10, 8))
    assert len(logs) == 1 and logs[0].remind_at is not None  # 但已标记
    # 再收集就没了（已提醒过）
    assert scheduler.collect_due() is None
    store.close()


def test_dispatch_none_is_noop() -> None:
    store = _store()
    scheduler = _make_scheduler(store, now=_at(2026, 10, 8, 12, 0))
    assert scheduler.dispatch(None) is False
    assert scheduler.collect_due() is None
    store.close()


# ================================================================ 回归：审查发现的坑
def test_no_reminder_at_exact_lead_time_is_not_dropped() -> None:
    """回归：正好落在提醒时刻（remind_at == now）必须派发。

    曾经的写法要求 `remind_at < now`，于是"正好到点"这一秒会被判成"还没到点"、
    排到未来；下一次唤醒时它已经过期，于是**永远不会提醒**。
    """
    store = _store()
    _recurring(store, "跑步", task_type=R.TYPE_DAILY, time_start="18:00", time_end="20:00",
                   remind_before_minutes=15)
    tasks = store.recurring_tasks()
    state = compute_schedule(tasks, {}, _at(2026, 10, 8, 17, 45))     # 正好 17:45
    assert state.batch is not None and state.batch.count == 1
    assert state.batch.items[0].kind == KIND_ADVANCE
    store.close()


def test_wakeup_is_always_in_the_future() -> None:
    """回归：下一次唤醒时刻必须严格在未来。

    曾经已提醒过的那次会被当成"下次唤醒"，算出过去的时刻 → 定时器被夹到 1 秒
    → 每秒空转。
    """
    store = _store()
    task_id = _recurring(store, "跑步", task_type=R.TYPE_DAILY, time_start="18:00",
                             time_end="20:00")
    store.log_occurrence(task_id, date(2026, 10, 8), remind_at=_at(2026, 10, 8, 17, 45),
                         remind_kind=KIND_ADVANCE)
    tasks = store.recurring_tasks()
    logs = {(row.task_id, row.occur_date): row for row in store.task_logs_on(date(2026, 10, 8))}
    for moment in (_at(2026, 10, 8, 17, 46), _at(2026, 10, 8, 19, 0), _at(2026, 10, 8, 21, 0)):
        state = compute_schedule(tasks, logs, moment)
        assert state.wakeup_at is not None, moment
        assert state.wakeup_at > moment, (moment, state.wakeup_at)
    store.close()


def test_large_remind_before_fires_at_window_start() -> None:
    """回归（S4）：提前 1440 分钟时，提醒必须在上一次窗口结束**之前**就发出。

    曾经的实现只锚定"当前那次发生"，导致下一次发生的提醒被推迟到上个窗口结束
    （实测晚 2 小时），极端配置下完全失去"提前"的意义。
    """
    store = _store()
    task_id = _recurring(store, "跑步", task_type=R.TYPE_DAILY, time_start="18:00",
                             time_end="20:00", remind_before_minutes=1440)
    # 10-07 那次已经提醒过
    store.log_occurrence(task_id, date(2026, 10, 7), remind_at=_at(2026, 10, 6, 18, 0),
                         remind_kind=KIND_ADVANCE)
    tasks = store.recurring_tasks()
    logs = {(row.task_id, row.occur_date): row for row in store.task_logs_on(date(2026, 10, 7))}
    # 10-08 那次的 remind_at 正是 10-07 18:00
    state = compute_schedule(tasks, logs, _at(2026, 10, 7, 18, 0))
    assert state.batch is not None and state.batch.count == 1
    assert state.batch.items[0].occur.date_str == "2026-10-08"
    assert state.batch.items[0].kind == KIND_ADVANCE
    store.close()


def test_zero_lead_time_dispatches_at_start_even_with_catchup_off() -> None:
    """回归（M2）：提前 0 分钟 + 关闭迟到补发，仍必须在开始时刻提醒一次。"""
    store = _store()
    _recurring(store, "准点", task_type=R.TYPE_DAILY, time_start="18:00", time_end="19:00",
                   remind_before_minutes=0)
    tasks = store.recurring_tasks()
    state = compute_schedule(tasks, {}, _at(2026, 10, 8, 18, 0), catchup_enabled=False)
    assert state.batch is not None and state.batch.count == 1
    assert state.batch.items[0].occur.date_str == "2026-10-08"
    store.close()


def test_timer_exception_does_not_kill_scheduler() -> None:
    """回归（M1）：定时器回调里抛异常后必须再排一个兜底定时器，不能永久停摆。"""
    store = _store()
    _recurring(store, "跑步", task_type=R.TYPE_DAILY, time_start="18:00", time_end="20:00")
    scheduler = _make_scheduler(store, now=_at(2026, 10, 8, 12, 0))
    scheduler.start()
    root = scheduler._test_root                                   # noqa: SLF001
    root.scheduled.clear()

    def boom(*_args, **_kwargs):
        raise RuntimeError("数据库炸了")

    scheduler.reschedule = boom                                    # type: ignore[method-assign]
    for callback in root.pop_pending() or [scheduler._check]:      # noqa: SLF001
        callback()
    # 抛异常后仍应排了一个兜底定时器
    assert root.scheduled, "异常后调度器没有重排，会永久停摆"
    store.close()


def test_reschedule_after_toggle_back_on() -> None:
    """回归（S2）：关掉再打开"周期提醒"后，定时器必须重新武装。"""
    store = _store()
    _recurring(store, "跑步", task_type=R.TYPE_DAILY, time_start="18:00", time_end="20:00")
    settings = _FakeSettings(recurring_reminder_enabled=False)
    scheduler = _make_scheduler(store, settings, now=_at(2026, 10, 8, 12, 0))
    assert scheduler.reschedule(reason="off") is None            # 关闭时不排程
    assert scheduler._test_root.scheduled == []

    settings.recurring_reminder_enabled = True                    # 重新打开
    sleep_ms = scheduler.reschedule(reason="on")
    assert sleep_ms is not None
    assert scheduler._test_root.scheduled, "重新打开后没有排定时器"
    store.close()


# ================================================================ 简易执行器
def _main() -> int:
    use_utf8_console()          # 英文系统下打印中文不再抛 UnicodeEncodeError
    import traceback

    tests = [
        (name, func)
        for name, func in sorted(globals().items())
        if name.startswith("test_") and callable(func)
    ]
    passed, failed = 0, 0
    for name, func in tests:
        try:
            func()
            print(f"[PASS] {name}")
            passed += 1
        except Exception:  # noqa: BLE001
            print(f"[FAIL] {name}")
            traceback.print_exc()
            failed += 1
    print("-" * 60)
    print(f"通过 {passed} 个，失败 {failed} 个")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())

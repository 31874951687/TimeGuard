"""累计打卡任务单元测试（v1.5）。

按用户给出的 **5 条避坑指南** 组织，每条都对应"如果不这么做会很难用"的具体场景：

1. 完成后必须停止每日提醒（否则第 50 天达标后每天 18:00 还在弹窗骚扰）
2. 自然日边界 + 同一天只能打一次卡（凌晨 00:10 算新的一天，不是补昨天）
3. 与时间监控的联动：v1.5 先做"手动一键打卡"（诚信机制），不做自动判定
4. 数据结构必须能画热力图（要有 check_in_logs 这张表，而不是一个计数字段）
5. "逾期"的定义：**今天 > 截止日** 且次数不够才算；截止日当天仍可打卡（压哨完成）

运行方式::

    python tests/test_cumulative.py
    python -m pytest tests -q
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

os.environ["TIMEGUARD_DATA_DIR"] = tempfile.mkdtemp(prefix="timeguard_cumulative_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from timeguard import recurrence as R  # noqa: E402
from timeguard.database import UsageStore  # noqa: E402
from timeguard.utils import use_utf8_console  # noqa: E402

DEADLINE = date(2026, 12, 31)


def _store() -> UsageStore:
    """每个用例一份独立数据库。"""
    return UsageStore(Path(tempfile.mkdtemp(prefix="tg_cum_case_")) / "usage.db")


def _task(store: UsageStore, target: int = 60, deadline: date = DEADLINE,
          title: str = "年末前完成 60 次两公里跑") -> int:
    task_id = store.add_task(title, task_type=R.TYPE_CUMULATIVE, target_count=target,
                             deadline=deadline.strftime("%Y-%m-%d"), remind_time="18:00")
    assert task_id is not None
    return task_id


def _fill(store: UsageStore, task_id: int, count: int, *, last_day: date,
          gap_days: int = 1) -> list[date]:
    """从 ``last_day`` 往前每隔 ``gap_days`` 天打一次卡，共 ``count`` 次。"""
    days = [last_day - timedelta(days=i * gap_days) for i in range(count)]
    for day in reversed(days):                     # 正序写入，像真实使用那样
        ok, msg = store.check_in(task_id, day)
        assert ok, msg
    return sorted(days)


# ============================================================ 5. 逾期定义的严谨性
def test_running_before_deadline_never_says_overdue() -> None:
    """避坑 #5（核心诉求）：截止日之前，**绝不能出现"逾期/失败"字样**。"""
    store = _store()
    task_id = _task(store)
    _fill(store, task_id, 59, last_day=date(2026, 12, 20))       # 差 1 次

    for today in (date(2026, 12, 20), date(2026, 12, 21), DEADLINE):
        status = R.cumulative_status(store.get_task(task_id).rule,
                                     store.checkin_dates(task_id), today)
        assert status.state == R.STATE_RUNNING, f"{today} 不该是 {status.state}"
        text = status.status_text()
        assert "逾期" not in text and "失败" not in text and "未达标" not in text, text
        assert "还差 1 次" in text, text
        assert status.state_label == "进行中"
        assert status.countdown_text() in ("还剩 11 天", "还剩 10 天", "今天是最后一天")
    store.close()


def test_only_the_day_after_deadline_becomes_missed() -> None:
    """避坑 #5：只有 今天 > 截止日 且 次数不够，才判逾期。"""
    store = _store()
    task_id = _task(store, target=3, deadline=date(2026, 12, 31))
    _fill(store, task_id, 2, last_day=date(2026, 12, 30))
    rule = store.get_task(task_id).rule
    dates = store.checkin_dates(task_id)

    assert R.cumulative_status(rule, dates, date(2026, 12, 31)).state == R.STATE_RUNNING
    missed = R.cumulative_status(rule, dates, date(2027, 1, 1))
    assert missed.state == R.STATE_MISSED
    assert "逾期未达标" in missed.state_label
    assert "逾期" in missed.status_text()            # 这时候才允许出现"逾期"
    assert missed.remaining == 1
    assert missed.countdown_text() == "已过期 1 天"
    store.close()


def test_checkin_on_deadline_day_is_allowed_and_finishes_on_time() -> None:
    """避坑 #5：截止日 23:00 打卡仍然算数 → 压哨完成，而不是逾期。"""
    store = _store()
    task_id = _task(store, target=3, deadline=DEADLINE)
    _fill(store, task_id, 2, last_day=date(2026, 12, 29))

    late_night = datetime(2026, 12, 31, 23, 0, 0)          # 截止日当晚 23:00
    ok, msg = store.check_in(task_id, now=late_night)
    assert ok, msg
    assert store.checkin_log(task_id, DEADLINE) is not None

    status = store.task_status(task_id)                    # 用真实今天算状态
    status_on_deadline = R.cumulative_status(store.get_task(task_id).rule,
                                             store.checkin_dates(task_id), DEADLINE)
    assert status_on_deadline.state == R.STATE_DONE_DEADLINE
    assert status_on_deadline.state_label == "压哨完成"
    assert "压哨完成" in status_on_deadline.status_text()
    assert status.finished is True
    store.close()


def test_finish_before_deadline_is_early_finish() -> None:
    """避坑 #1 的前半段：截止日之前攒够 → 提前完成，并记住是哪天达标的。"""
    store = _store()
    task_id = _task(store, target=60, deadline=DEADLINE)
    days = _fill(store, task_id, 60, last_day=date(2026, 11, 20))
    status = R.cumulative_status(store.get_task(task_id).rule,
                                 store.checkin_dates(task_id), date(2026, 11, 25))
    assert status.state == R.STATE_DONE_EARLY
    assert status.reached_on == days[-1] == date(2026, 11, 20)
    assert "提前完成" in status.status_text()
    assert "11-20 达标" in status.status_text()
    assert status.remaining == 0 and status.ratio == 1.0 and status.percent == 100
    store.close()


def test_extra_checkins_do_not_downgrade_early_finish() -> None:
    """达标之后再打卡，不会把"提前完成"改成"压哨完成"。

    判定取的是**第 N 次打卡那一天**（N = 目标次数），不是"最后一次打卡"。
    """
    store = _store()
    task_id = _task(store, target=3, deadline=date(2026, 12, 31))
    _fill(store, task_id, 3, last_day=date(2026, 12, 10))       # 12-10 就达标了
    for day in (date(2026, 12, 25), date(2026, 12, 31)):        # 又打了两天
        ok, msg = store.check_in(task_id, day)
        assert ok, msg
    status = R.cumulative_status(store.get_task(task_id).rule,
                                 store.checkin_dates(task_id), date(2027, 1, 2))
    assert status.total == 5
    assert status.state == R.STATE_DONE_EARLY
    assert status.reached_on == date(2026, 12, 10)
    store.close()


# ============================================================ 2. 自然日与防重复
def test_same_day_can_only_check_in_once() -> None:
    """避坑 #2：同一天只能打一次卡（数据库 UNIQUE 兜底，不靠界面自觉）。"""
    store = _store()
    task_id = _task(store, target=10)
    ok, msg = store.check_in(task_id)
    assert ok and "打卡成功" in msg
    again, msg2 = store.check_in(task_id)
    assert again is False
    assert "已经打过卡" in msg2, msg2
    assert store.checkin_count(task_id) == 1
    # 数据库层也挡住：绕过 API 直接插第二条会撞 UNIQUE 约束
    try:
        store._conn.execute(                       # noqa: SLF001
            "INSERT INTO check_in_logs(task_id, checkin_date, checkin_at) VALUES(?,?,?)",
            (task_id, date.today().strftime("%Y-%m-%d"), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        store._conn.commit()                       # noqa: SLF001
        raise AssertionError("同一天插入了两条打卡记录，UNIQUE 约束没生效")
    except sqlite3.IntegrityError:
        pass
    store.close()


def test_natural_day_boundary_uses_local_date() -> None:
    """避坑 #2：00:10 打卡算**新的一天**，23:50 算当天 —— 按本地自然日切分。"""
    store = _store()
    task_id = _task(store, target=10, deadline=date(2026, 12, 31))

    ok, _ = store.check_in(task_id, now=datetime(2026, 10, 8, 23, 50, 0))
    assert ok
    assert [d for d in store.checkin_dates(task_id)] == [date(2026, 10, 8)]

    ok, msg = store.check_in(task_id, now=datetime(2026, 10, 9, 0, 10, 0))     # 跨过午夜
    assert ok, msg
    assert store.checkin_dates(task_id) == [date(2026, 10, 8), date(2026, 10, 9)]
    assert store.checkin_count(task_id) == 2
    # 打卡时间戳保留到秒，便于展示与审计
    log_row = store.checkin_log(task_id, date(2026, 10, 9))
    assert log_row is not None and log_row.checkin_at == datetime(2026, 10, 9, 0, 10, 0)
    assert log_row.time_text == "00:10:00"
    store.close()


def test_cannot_check_in_after_deadline() -> None:
    """过了截止日就不能再打卡（状态已经是"逾期未达标"，再打也没有意义）。"""
    store = _store()
    task_id = _task(store, target=10, deadline=date(2026, 10, 31))
    ok, msg = store.check_in(task_id, day=date(2026, 11, 1))
    assert ok is False and "已过截止日期" in msg, msg
    assert store.checkin_count(task_id) == 0
    ok2, _ = store.check_in(task_id, day=date(2026, 10, 31))         # 截止日当天可以
    assert ok2 is True
    store.close()


def test_undo_check_in() -> None:
    """点错了可以撤销当天打卡。"""
    store = _store()
    task_id = _task(store, target=10)
    assert store.check_in(task_id)[0] is True
    assert store.checkin_count(task_id) == 1
    assert store.undo_check_in(task_id) is True
    assert store.checkin_count(task_id) == 0
    assert store.checkin_log(task_id, date.today()) is None
    assert store.undo_check_in(task_id) is False            # 没得撤了
    store.close()


# ============================================================ 1. 完成后停止提醒
def test_reminder_stops_after_target_reached() -> None:
    """避坑 #1（用户最在意的骚扰问题）：达标后 next_checkin_reminder 必须返回 None。"""
    now = datetime(2026, 11, 21, 9, 0)
    rule = R.TaskRule.cumulative(60, DEADLINE, "18:00")
    assert rule.next_checkin_reminder(now) == datetime(2026, 11, 21, 18, 0)   # 没达标 → 今天 18:00
    assert rule.next_checkin_reminder(now, day_done=True) == datetime(2026, 11, 22, 18, 0)
    assert rule.next_checkin_reminder(now, finished=True) is None             # 达标 → 不再提醒


def test_reminder_stops_after_deadline() -> None:
    """过了截止日也不再提醒（任务已经结束，再提醒只是噪音）。"""
    rule = R.TaskRule.cumulative(60, date(2026, 10, 31), "18:00")
    assert rule.next_checkin_reminder(datetime(2026, 10, 31, 9, 0)) == datetime(2026, 10, 31, 18, 0)
    assert rule.next_checkin_reminder(datetime(2026, 11, 1, 9, 0)) is None
    assert rule.is_expired(date(2026, 10, 31)) is False        # 截止日当天没过期
    assert rule.is_expired(date(2026, 11, 1)) is True


def test_reminder_after_todays_time_already_passed() -> None:
    """今天的提醒时刻已经过了且还没打卡：纯函数给明天，调度器负责"补发"今天那次。"""
    rule = R.TaskRule.cumulative(60, DEADLINE, "18:00")
    now = datetime(2026, 11, 20, 20, 30)
    assert rule.next_checkin_reminder(now) == datetime(2026, 11, 21, 18, 0)
    assert rule.checkin_reminder_on(date(2026, 11, 20)) == datetime(2026, 11, 20, 18, 0)


# ============================================================ 4. 数据结构（热力图）
def test_checkin_history_supports_heatmap() -> None:
    """避坑 #4：check_in_logs 要能按日期取历史 —— 热力图才有数据可画。"""
    store = _store()
    task_id = _task(store, target=30)
    days = [date(2026, 10, 1) + timedelta(days=i * 2) for i in range(10)]     # 隔天打卡
    for day in days:
        assert store.check_in(task_id, day)[0] is True
    logs = store.checkin_logs(task_id)
    assert [log.checkin_date for log in logs] == [d.strftime("%Y-%m-%d") for d in days]
    assert all(log.source == "manual" for log in logs)
    # 区间查询（热力图只画最近 N 周）
    recent = store.checkin_logs(task_id, since=days[5], until=days[7])
    assert [log.checkin_date for log in recent] == [d.strftime("%Y-%m-%d") for d in days[5:8]]
    # 全任务的批量查询（图表用一次查完）
    assert len(store.checkin_logs_between(days[0], days[-1])) == 10
    # 次数批量取（列表刷新不能 N+1）
    assert store.checkin_counts() == {task_id: 10}
    store.close()


def test_check_in_never_touches_tasks_completed() -> None:
    """不变量：累计打卡**只写 check_in_logs**，不碰 tasks.completed。

    跟周期任务同一个道理 —— 一旦写进去，"完成任务"就会变成永久状态。
    """
    store = _store()
    task_id = _task(store, target=1)
    assert store.check_in(task_id)[0] is True
    task = store.get_task(task_id)
    assert task.completed is False and task.completed_at is None
    assert task in store.cumulative_tasks()
    # 也不该混进单次任务的待办列表
    assert task_id not in {t.id for t in store.pending_tasks()}
    store.close()


# ============================================================ 统计与查询联动
def test_task_counts_do_not_count_running_task_as_overdue() -> None:
    """核心诉求在统计口径上的体现：截止日之前只有"待办/今天到期"，没有"已超期"。"""
    store = _store()
    task_id = _task(store, target=10, deadline=date.today() + timedelta(days=30))
    counts = store.task_counts()
    assert counts.overdue == 0, counts.summary_text()
    assert counts.pending == 1 and counts.due_today == 1
    assert counts.total == counts.pending + counts.completed

    assert store.check_in(task_id)[0] is True              # 今天打过卡
    counts2 = store.task_counts()
    assert counts2.overdue == 0 and counts2.pending == 0 and counts2.completed == 1

    # 过了截止日还没达标 → 这时候才计入"已超期"
    overdue_id = _task(store, target=10, deadline=date.today() - timedelta(days=1))
    counts3 = store.task_counts()
    assert counts3.overdue == 1, counts3.summary_text()
    assert store.get_task(overdue_id).rule.is_expired(date.today()) is True
    store.close()


def test_task_counts_ignore_finished_task() -> None:
    """已达标的任务不再算进"今天要做的事"（否则"已完成"会天天 +1）。"""
    store = _store()
    task_id = _task(store, target=2, deadline=date.today() + timedelta(days=10))
    assert store.check_in(task_id)[0] is True
    assert store.check_in(task_id, day=date.today() - timedelta(days=1))[0] is True
    status = store.task_status(task_id)
    assert status.finished is True
    counts = store.task_counts()
    assert counts.pending == 0 and counts.completed == 0 and counts.overdue == 0
    store.close()


def test_stats_by_day_counts_checkins_and_marks_overdue_on_deadline() -> None:
    """7 天图表：每次打卡算一次完成；没达标只在**截止日那天**记一笔逾期。"""
    store = _store()
    task_id = _task(store, target=10, deadline=date.today() - timedelta(days=1))
    for offset in (2, 1):
        day = date.today() - timedelta(days=offset)
        assert store.check_in(task_id, day)[0] is True
    stats = {item.day: item for item in store.task_stats_by_day(7)}
    for offset in (2, 1):
        day = (date.today() - timedelta(days=offset)).strftime("%Y-%m-%d")
        assert stats[day].on_time == 1, stats[day]
    deadline_key = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    assert stats[deadline_key].overdue == 1, stats[deadline_key]
    # 到期日之后的日子不该再重复记逾期（同一个失败只数一次）
    assert stats[date.today().strftime("%Y-%m-%d")].overdue == 0
    store.close()


# ============================================================ 字段与迁移
def test_new_task_stores_cumulative_fields_cleanly() -> None:
    """累计打卡任务的周期字段必须留空，避免多出一份"假计划"。"""
    store = _store()
    task_id = _task(store, target=60, deadline=DEADLINE)
    row = store._conn.execute(                       # noqa: SLF001
        "SELECT task_type, due_at, time_start, time_end, days_of_week, start_date,"
        " target_count, deadline, remind_time FROM tasks WHERE id = ?", (task_id,)).fetchone()
    data = dict(row)
    assert data["task_type"] == R.TYPE_CUMULATIVE
    assert data["target_count"] == 60 and data["deadline"] == "2026-12-31"
    assert data["remind_time"] == "18:00"
    for empty in ("due_at", "time_start", "time_end", "days_of_week", "start_date"):
        assert data[empty] is None, f"{empty} 应该是空的：{data[empty]}"
    task = store.get_task(task_id)
    assert task.is_cumulative is True and task.is_recurring is False
    assert task.schedule_text.startswith("累计打卡 60 次 · 截止 2026-12-31"), task.schedule_text
    assert "18:00 提醒" in task.schedule_text
    assert task.occurrence_on(date.today()) is None      # 没有"时间窗"这一说
    store.close()


def test_cumulative_requires_deadline_and_clamps_target() -> None:
    """没有截止日期不能建；目标次数会被夹到合法区间。"""
    store = _store()
    assert store.add_task("没截止日期", task_type=R.TYPE_CUMULATIVE, target_count=5) is None
    assert store.add_task("坏日期", task_type=R.TYPE_CUMULATIVE, deadline="不是日期") is None
    low = store.add_task("目标 0", task_type=R.TYPE_CUMULATIVE, target_count=0, deadline="2026-12-31")
    assert store.get_task(low).rule.target_count == 1
    high = store.add_task("目标超大", task_type=R.TYPE_CUMULATIVE,
                          target_count=999999, deadline="2026-12-31")
    assert store.get_task(high).rule.target_count == R.MAX_TARGET_COUNT
    store.close()


def test_switching_task_type_clears_the_other_fields() -> None:
    """每天 → 累计打卡：周期字段清空；累计打卡 → 每天：打卡字段清空。"""
    store = _store()
    daily_id = store.add_task("每天跑步", task_type=R.TYPE_DAILY,
                              time_start="06:00", time_end="08:00")
    assert store.update_task(daily_id, task_type=R.TYPE_CUMULATIVE, target_count=30,
                             deadline="2026-12-31", remind_time="07:00") is True
    converted = store.get_task(daily_id)
    assert converted.is_cumulative is True
    assert converted.rule.target_count == 30 and converted.rule.deadline == date(2026, 12, 31)
    row = store._conn.execute(                       # noqa: SLF001
        "SELECT time_start, time_end, days_of_week FROM tasks WHERE id = ?", (daily_id,)).fetchone()
    assert row["time_start"] is None and row["time_end"] is None

    assert store.update_task(daily_id, task_type=R.TYPE_DAILY,
                             time_start="06:00", time_end="08:00") is True
    back = store.get_task(daily_id)
    assert back.rule.task_type == R.TYPE_DAILY
    assert back.rule.target_count is None and back.rule.deadline is None
    assert back.rule.remind_time is None

    # 改成累计打卡但库里本来就没有截止日期 → 拒绝（避免造出永远算不出状态的任务）
    once_id = store.add_task("单次", due_at=datetime.now() + timedelta(hours=1))
    assert store.update_task(once_id, task_type=R.TYPE_CUMULATIVE) is False
    # 清空周期字段后没有残留的 due_at
    assert store.get_task(daily_id).due_at is None
    store.close()


def test_legacy_database_migration_adds_cumulative_columns() -> None:
    """老库（v1.4 及更早）打开后自动补列 + 建 check_in_logs，且不动既有数据。"""
    path = Path(tempfile.mkdtemp(prefix="tg_cum_legacy_")) / "usage.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE tasks(
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, due_at TEXT,
            created_at TEXT NOT NULL, completed INTEGER NOT NULL DEFAULT 0,
            completed_at TEXT, priority INTEGER NOT NULL DEFAULT 1,
            remind_count INTEGER NOT NULL DEFAULT 0, last_remind_at TEXT, note TEXT,
            task_type TEXT NOT NULL DEFAULT 'once', time_start TEXT, time_end TEXT,
            days_of_week TEXT, remind_before_minutes INTEGER NOT NULL DEFAULT 15);
        INSERT INTO tasks(title, created_at, task_type, time_start, time_end)
            VALUES('老库里的每日任务', '2026-10-01 08:00:00', 'daily', '06:00', '08:00');
        """
    )
    conn.commit()
    conn.close()

    store = UsageStore(path)
    columns = [row["name"] for row in store._conn.execute("PRAGMA table_info(tasks)")]  # noqa: SLF001
    for column in ("target_count", "deadline", "remind_time"):
        assert column in columns, f"{column} 没补上：{columns}"
    tables = [row["name"] for row in store._conn.execute(               # noqa: SLF001
        "SELECT name FROM sqlite_master WHERE type = 'table'")]
    assert "check_in_logs" in tables, tables
    legacy = store.recurring_tasks()[0]
    assert legacy.title == "老库里的每日任务" and legacy.rule.time_start.hour == 6
    assert legacy.rule.target_count is None and legacy.rule.deadline is None
    assert store.checkin_count(legacy.id) == 0
    assert store.cumulative_tasks() == []
    store.close()


# ============================================================ 简易执行器
def _main() -> int:
    """无 pytest 时的极简执行器。"""
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

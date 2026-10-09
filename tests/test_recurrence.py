"""周期性任务单元测试（阶段 1：数据层 + 周期规则）。

覆盖两块：
  * ``timeguard/recurrence.py`` —— 纯函数：星期解析、时间窗、下一次/上一次发生
  * ``timeguard/database.py``   —— 规则与发生记录分离、打卡、统计合并、旧库迁移

运行方式::

    python tests/test_recurrence.py
    python -m pytest tests -q
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path

os.environ["TIMEGUARD_DATA_DIR"] = tempfile.mkdtemp(prefix="timeguard_recur_test_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from timeguard import recurrence as R  # noqa: E402
from timeguard.utils import use_utf8_console  # noqa: E402
from timeguard.database import UsageStore  # noqa: E402


def _store() -> UsageStore:
    """新建一个独立数据库（每个用例一份）。"""
    return UsageStore(Path(tempfile.mkdtemp(prefix="tg_recur_case_")) / "usage.db")


def _at(*args) -> datetime:
    """构造一个确定的时间点（便于断言）。"""
    return datetime(*args)


# ================================================================ 星期 / 时间解析
def test_parse_iso_weekdays_normalizes() -> None:
    assert R.parse_iso_weekdays("1,3,5") == (1, 3, 5)
    assert R.parse_iso_weekdays("5,3,1,3") == (1, 3, 5)      # 去重 + 排序
    assert R.parse_iso_weekdays("1，3") == (1, 3)            # 中文逗号
    assert R.parse_iso_weekdays(" 2 , 4 ") == (2, 4)
    assert R.parse_iso_weekdays("0,8,abc,3") == (3,)         # 非法值忽略
    assert R.parse_iso_weekdays("") == ()
    assert R.parse_iso_weekdays(None) == ()


def test_format_iso_weekdays() -> None:
    assert R.format_iso_weekdays([3, 1, 1]) == "1,3"
    assert R.format_iso_weekdays(()) == ""
    assert R.format_iso_weekdays(None) == ""
    assert R.format_iso_weekdays([9, 0]) == ""               # 全部非法 → 空


def test_weekdays_text() -> None:
    assert R.weekdays_text((1, 3, 5)) == "周一、周三、周五"
    assert R.weekdays_text((1, 2, 3, 4, 5, 6, 7)) == "每天"
    assert R.weekdays_text(()) == "未设置"


def test_parse_hhmm_with_fallback() -> None:
    assert R.parse_hhmm("06:30", "18:00") == time(6, 30)
    assert R.parse_hhmm("6:05", "18:00") == time(6, 5)
    assert R.parse_hhmm("23:59:59", "18:00") == time(23, 59)
    assert R.parse_hhmm("", "07:15") == time(7, 15)           # 空 → 用兜底
    assert R.parse_hhmm(None, "07:15") == time(7, 15)
    assert R.parse_hhmm("25:00", "08:00") == time(8, 0)       # 非法小时 → 兜底
    assert R.parse_hhmm("10:99", "08:00") == time(8, 0)       # 非法分钟 → 兜底
    assert R.parse_hhmm("garbage", "09:00") == time(9, 0)
    assert R.parse_hhmm("garbage", None) == time(18, 0)       # 连兜底都没有 → 18:00


def test_format_hhmm_pads() -> None:
    assert R.format_hhmm(time(6, 5)) == "06:05"
    assert R.format_hhmm(time(23, 59)) == "23:59"


# ================================================================ TaskRule 基本行为
def test_rule_daily_occurs_every_day() -> None:
    rule = R.TaskRule.daily("06:00", "08:00", before=15)
    assert rule.task_type == R.TYPE_DAILY
    assert rule.is_recurring is True
    assert rule.type_label == "每天"
    assert rule.remind_before_minutes == 15
    assert rule.schedule_text() == "每天 06:00~08:00"
    for offset in range(7):
        day = date(2026, 10, 5) + timedelta(days=offset)      # 周一起连续 7 天
        assert rule.occurs_on(day) is True


def test_rule_weekly_only_on_selected_days() -> None:
    rule = R.TaskRule.weekly("3,4", "18:00", "20:00", before=30)
    assert rule.task_type == R.TYPE_WEEKLY
    assert rule.days_of_week == (3, 4)
    assert rule.schedule_text() == "周三、周四 18:00~20:00"
    assert rule.occurs_on(date(2026, 10, 7)) is True          # 周三
    assert rule.occurs_on(date(2026, 10, 8)) is True          # 周四
    assert rule.occurs_on(date(2026, 10, 9)) is False         # 周五
    assert rule.occurs_on(date(2026, 10, 5)) is False         # 周一


def test_rule_once_never_occurs() -> None:
    rule = R.TaskRule()
    assert rule.task_type == R.TYPE_ONCE
    assert rule.is_recurring is False
    assert rule.occurs_on(date(2026, 10, 7)) is False
    assert rule.window_on(date(2026, 10, 7)) is None
    assert rule.next_occurrence(_at(2026, 10, 7, 12, 0)) is None
    assert rule.schedule_text() == "单次"


def test_rule_from_row_handles_missing_columns() -> None:
    """老库没有这些列时，from_row 必须给出安全默认值而不是抛异常。"""
    rule = R.TaskRule.from_row({})
    assert rule.task_type == R.TYPE_ONCE
    assert rule.time_start == time(18, 0)
    assert rule.time_end == time(20, 0)
    assert rule.days_of_week == ()
    assert rule.remind_before_minutes == R.DEFAULT_REMIND_BEFORE


def test_rule_from_row_repairs_bad_values() -> None:
    rule = R.TaskRule.from_row({
        "task_type": "unknown",            # 非法类型 → 退化为单次
        "time_start": "abc",
        "time_end": "05:00",               # 结束早于开始 → 视为"到当天结束"
        "days_of_week": "3,3,9,x",
        "remind_before_minutes": "99999",  # 超出上限 → 夹紧
    })
    assert rule.task_type == R.TYPE_ONCE
    assert rule.time_start == time(18, 0)
    assert rule.time_end == time(23, 59)
    assert rule.days_of_week == (3,)
    assert rule.remind_before_minutes == R.MAX_REMIND_BEFORE


def test_rule_from_row_clamps_negative_remind() -> None:
    rule = R.TaskRule.from_row({"task_type": "daily", "remind_before_minutes": -30})
    assert rule.remind_before_minutes == R.MIN_REMIND_BEFORE


# ================================================================ 发生窗口与推算
def test_occurrence_window_and_helpers() -> None:
    window = R.Occurrence(date(2026, 10, 7), _at(2026, 10, 7, 18, 0), _at(2026, 10, 7, 20, 0))
    assert window.date_str == "2026-10-07"
    assert window.contains(_at(2026, 10, 7, 19, 0)) is True
    assert window.contains(_at(2026, 10, 7, 17, 59)) is False
    assert window.remind_at(15) == _at(2026, 10, 7, 17, 45)
    assert window.remind_at(0) == window.start
    assert window.is_late(_at(2026, 10, 7, 20, 1)) is True
    assert window.is_late(_at(2026, 10, 7, 19, 0)) is False


def test_next_occurrence_daily_prefers_today_window() -> None:
    rule = R.TaskRule.daily("18:00", "20:00")
    # 今天 12:00：今天的窗口还没结束 → 就是今天
    nxt = rule.next_occurrence(_at(2026, 10, 7, 12, 0))
    assert nxt is not None and nxt.date_str == "2026-10-07"
    # 今天 21:00：今天已过 → 明天
    nxt = rule.next_occurrence(_at(2026, 10, 7, 21, 0))
    assert nxt is not None and nxt.date_str == "2026-10-08"


def test_next_occurrence_weekly_skips_to_right_day() -> None:
    """周三/周四 18:00~20:00，周二查询应落到周三。"""
    rule = R.TaskRule.weekly("3,4", "18:00", "20:00")
    tuesday = _at(2026, 10, 6, 10, 0)                    # 2026-10-06 是周二
    assert tuesday.isoweekday() == 2
    nxt = rule.next_occurrence(tuesday)
    assert nxt is not None and nxt.date_str == "2026-10-07"   # 周三
    # 周三 21:00（当天窗口已过）→ 周四
    nxt = rule.next_occurrence(_at(2026, 10, 7, 21, 0))
    assert nxt is not None and nxt.date_str == "2026-10-08"
    # 周四 21:00 → 下周三
    nxt = rule.next_occurrence(_at(2026, 10, 8, 21, 0))
    assert nxt is not None and nxt.date_str == "2026-10-14"


def test_next_occurrence_spans_month_boundary() -> None:
    """月末跨月：10-31（周六）→ 下一个周三应在 11 月。"""
    rule = R.TaskRule.weekly("3", "18:00", "20:00")
    nxt = rule.next_occurrence(_at(2026, 10, 31, 22, 0))
    assert nxt is not None
    assert nxt.day.month == 11
    assert nxt.day.isoweekday() == 3


def test_current_or_next_includes_running_window() -> None:
    rule = R.TaskRule.daily("18:00", "20:00")
    # 19:00 正在窗口内 → 返回今天
    current = rule.current_or_next_occurrence(_at(2026, 10, 7, 19, 0))
    assert current is not None and current.date_str == "2026-10-07"
    # 12:00 还没开始 → 今天 18:00 那次
    current = rule.current_or_next_occurrence(_at(2026, 10, 7, 12, 0))
    assert current is not None and current.date_str == "2026-10-07"


def test_previous_occurrence_finds_last_finished() -> None:
    rule = R.TaskRule.daily("06:00", "08:00")
    # 今天 07:00：今天窗口还没结束 → 上一次是昨天
    prev = rule.previous_occurrence(_at(2026, 10, 7, 7, 0))
    assert prev is not None and prev.date_str == "2026-10-06"
    # 今天 09:00：今天窗口已结束 → 上一次是今天
    prev = rule.previous_occurrence(_at(2026, 10, 7, 9, 0))
    assert prev is not None and prev.date_str == "2026-10-07"


def test_occurrences_between_counts_expected_days() -> None:
    monday = R.TaskRule.weekly("1,3,5", "18:00", "20:00")
    items = monday.occurrences_between(_at(2026, 10, 5, 0, 0), _at(2026, 10, 11, 23, 59))
    assert [item.date_str for item in items] == ["2026-10-05", "2026-10-07", "2026-10-09"]
    # 反向区间返回空
    assert monday.occurrences_between(_at(2026, 10, 11), _at(2026, 10, 5)) == []


# ================================================================ 数据层：规则字段
def test_add_daily_task_stores_rule() -> None:
    store = _store()
    task_id = store.add_task("每天跑步 2 公里", task_type=R.TYPE_DAILY,
                             time_start="06:00", time_end="08:00",
                             remind_before_minutes=20,
                             start_date="")   # 本用例只关心规则字段，不受"生效起始日"影响
    assert task_id is not None
    task = store.get_task(task_id)
    assert task is not None
    assert task.is_recurring is True
    assert task.rule.time_start == time(6, 0)
    assert task.rule.time_end == time(8, 0)
    assert task.rule.days_of_week == ()          # 每天任务不留星期字段
    assert task.rule.remind_before_minutes == 20
    assert task.schedule_text == "每天 06:00~08:00"
    store.close()


def test_add_weekly_task_normalizes_weekdays() -> None:
    store = _store()
    task_id = store.add_task("高数学习", task_type=R.TYPE_WEEKLY,
                             days_of_week="4,3,3", time_start="18:00", time_end="20:00",
                             start_date="")   # 本用例只关心星期归一化
    task = store.get_task(task_id)
    assert task is not None
    assert task.rule.days_of_week == (3, 4)
    assert task.schedule_text == "周三、周四 18:00~20:00"
    store.close()


def test_add_weekly_task_without_days_is_rejected() -> None:
    store = _store()
    assert store.add_task("没选星期", task_type=R.TYPE_WEEKLY, days_of_week="") is None
    assert store.add_task("非法星期", task_type=R.TYPE_WEEKLY, days_of_week="9,abc") is None
    store.close()


def test_add_once_task_ignores_recurrence_fields() -> None:
    """单次任务即使传了周期字段也不落库，避免语义混淆。"""
    store = _store()
    task_id = store.add_task("临时任务", task_type=R.TYPE_ONCE,
                             time_start="06:00", time_end="08:00", days_of_week="1,2")
    task = store.get_task(task_id)
    assert task is not None
    assert task.is_recurring is False
    assert task.rule.time_start == time(18, 0)   # 落库为 NULL → 默认值
    assert task.rule.days_of_week == ()
    store.close()


def test_default_remind_before_is_15_minutes() -> None:
    store = _store()
    task_id = store.add_task("默认提前提醒", task_type=R.TYPE_DAILY)
    task = store.get_task(task_id)
    assert task is not None and task.rule.remind_before_minutes == 15
    store.close()


def test_scheduled_on_filters_by_weekday() -> None:
    store = _store()
    # start_date=""：本用例按**固定日期**（2026-10-05 等）查排班，
    # 不能让"今天才建 → 从明天开始"的自动生效日把它挡住
    store.add_task("每天跑步", task_type=R.TYPE_DAILY, time_start="06:00", time_end="08:00",
                   start_date="")
    store.add_task("周一三五", task_type=R.TYPE_WEEKLY, days_of_week="1,3,5",
                   time_start="18:00", time_end="20:00", start_date="")
    store.add_task("周二", task_type=R.TYPE_WEEKLY, days_of_week="2",
                   time_start="18:00", time_end="20:00", start_date="")
    store.add_task("单次任务", due_at=datetime.now() + timedelta(hours=1))

    monday = store.scheduled_on(date(2026, 10, 5))          # 周一
    assert {t.title for t in monday} == {"每天跑步", "周一三五"}
    tuesday = store.scheduled_on(date(2026, 10, 6))         # 周二
    assert {t.title for t in tuesday} == {"每天跑步", "周二"}
    # 也接受 'YYYY-MM-DD' 字符串
    assert len(store.scheduled_on("2026-10-05")) == 2
    store.close()


# ================================================================ 数据层：发生记录
def test_recurring_complete_writes_log_not_task() -> None:
    """周期任务打卡必须写 task_logs，绝不动 tasks.completed。"""
    store = _store()
    task_id = store.add_task("每天跑步", task_type=R.TYPE_DAILY,
                             time_start="00:00", time_end="23:59")
    assert store.complete_task(task_id) is True

    task = store.get_task(task_id)
    assert task is not None
    assert task.completed is False               # 规则本身永远不算"完成"
    assert task.completed_at is None
    assert store.is_occurrence_done(task_id) is True
    log_row = store.get_task_log(task_id, date.today())
    assert log_row is not None and log_row.completed is True
    assert log_row.occur_date == date.today().strftime("%Y-%m-%d")
    store.close()


def test_completing_recurring_twice_is_idempotent() -> None:
    store = _store()
    task_id = store.add_task("每天背单词", task_type=R.TYPE_DAILY,
                             time_start="00:00", time_end="23:59")
    assert store.complete_occurrence(task_id) is True
    assert store.complete_occurrence(task_id) is True
    logs = store.task_logs_on(date.today())
    assert len(logs) == 1                        # UNIQUE(task_id, occur_date) 生效
    store.close()


def test_uncomplete_recurring_clears_today_only() -> None:
    store = _store()
    task_id = store.add_task("每天跑步", task_type=R.TYPE_DAILY,
                             time_start="00:00", time_end="23:59")
    store.complete_occurrence(task_id, date(2026, 10, 6))
    store.complete_occurrence(task_id, date(2026, 10, 7))
    assert store.complete_occurrence(task_id, date.today(), completed=False) is True
    assert store.is_occurrence_done(task_id, date.today()) is False
    assert store.is_occurrence_done(task_id, date(2026, 10, 6)) is True   # 昨天不受影响
    assert store.is_occurrence_done(task_id, date(2026, 10, 7)) is True
    store.close()


def test_complete_task_false_clears_completed_at() -> None:
    """取消单次任务的完成时，completed_at 必须一起清掉，否则图表仍算它完成。"""
    store = _store()
    task_id = store.add_task("单次任务", due_at=datetime.now() + timedelta(hours=1))
    assert store.complete_task(task_id) is True
    task = store.get_task(task_id)
    assert task is not None and task.completed_at is not None

    assert store.complete_task(task_id, completed=False) is True
    task = store.get_task(task_id)
    assert task is not None
    assert task.completed is False
    assert task.completed_at is None
    store.close()


def test_log_occurrence_upserts_fields() -> None:
    store = _store()
    task_id = store.add_task("每天跑步", task_type=R.TYPE_DAILY,
                             time_start="06:00", time_end="08:00")
    day = date(2026, 10, 7)
    store.log_occurrence(task_id, day, remind_at=_at(2026, 10, 7, 5, 45), remind_kind="advance")
    store.log_occurrence(task_id, day, completed=True, completed_at=_at(2026, 10, 7, 7, 30),
                         was_late=False)
    log_row = store.get_task_log(task_id, day)
    assert log_row is not None
    assert log_row.completed is True
    assert log_row.remind_kind == "advance"
    assert log_row.remind_at == _at(2026, 10, 7, 5, 45)
    assert log_row.completed_at == _at(2026, 10, 7, 7, 30)
    assert log_row.kind_label == "提前提醒"
    assert len(store.task_logs_on(day)) == 1
    store.close()


def test_mark_occurrences_reminded_only_touches_given() -> None:
    store = _store()
    first = store.add_task("任务A", task_type=R.TYPE_DAILY, time_start="18:00", time_end="20:00")
    second = store.add_task("任务B", task_type=R.TYPE_DAILY, time_start="18:00", time_end="20:00")
    day = date(2026, 10, 7)
    assert store.mark_occurrences_reminded([(first, day)], kind="late",
                                           when=_at(2026, 10, 7, 18, 5)) == 1
    log_a = store.get_task_log(first, day)
    log_b = store.get_task_log(second, day)
    assert log_a is not None and log_a.remind_kind == "late"
    assert log_a.remind_at == _at(2026, 10, 7, 18, 5)
    assert log_b is None                          # 没传进去的不该被标记
    store.close()


def test_reminder_pending_on_excludes_already_reminded() -> None:
    store = _store()
    first = store.add_task("已提醒", task_type=R.TYPE_DAILY, time_start="18:00", time_end="20:00",
                           start_date="")   # 固定按 2026-10-07 查，绕开自动生效日
    second = store.add_task("待提醒", task_type=R.TYPE_DAILY, time_start="18:00", time_end="20:00",
                            start_date="")
    day = date(2026, 10, 7)
    store.mark_occurrences_reminded([(first, day)])
    pending = store.reminder_pending_on(day)
    assert {task.title for task, _log_row in pending} == {"待提醒"}
    # 已完成的仍然算"已提醒过"（remind_at 有值），不会重复打扰
    store.log_occurrence(second, day, remind_at=_at(2026, 10, 7, 17, 45))
    assert store.reminder_pending_on(day) == []
    store.close()


def test_delete_task_cascades_logs() -> None:
    store = _store()
    task_id = store.add_task("每天跑步", task_type=R.TYPE_DAILY,
                             time_start="06:00", time_end="08:00")
    store.complete_occurrence(task_id, date(2026, 10, 6))
    store.complete_occurrence(task_id, date(2026, 10, 7))
    assert len(store.task_logs_since("2026-01-01")) == 2
    assert store.delete_task(task_id) is True
    assert store.task_logs_since("2026-01-01") == []
    store.close()


def test_update_task_switches_type_and_clears_due() -> None:
    store = _store()
    task_id = store.add_task("原本是单次", due_at=datetime.now() + timedelta(hours=2))
    assert store.update_task(task_id, task_type=R.TYPE_DAILY, time_start="07:00",
                             time_end="09:00", remind_before_minutes=25,
                             clear_due=True) is True
    task = store.get_task(task_id)
    assert task is not None
    assert task.is_recurring is True
    assert task.due_at is None
    assert task.rule.time_start == time(7, 0)
    assert task.rule.remind_before_minutes == 25

    # 再切回单次：周期字段必须清干净
    assert store.update_task(task_id, task_type=R.TYPE_ONCE) is True
    task = store.get_task(task_id)
    assert task is not None and task.is_recurring is False
    assert task.rule.days_of_week == ()
    store.close()


# ================================================================ 数据层：统计合并
def test_task_counts_include_today_recurring() -> None:
    """today 维度统计：今天该做的周期任务要算进 pending，打卡后转到 completed。"""
    store = _store()
    store.add_task("每天跑步", task_type=R.TYPE_DAILY, time_start="00:00", time_end="23:59")
    store.add_task("每周一", task_type=R.TYPE_WEEKLY, days_of_week="1",
                   time_start="00:00", time_end="23:59")
    daily_id = store.add_task("每天背单词", task_type=R.TYPE_DAILY,
                              time_start="00:00", time_end="23:59")
    today = date.today()
    # 今天该做几条：两条每天任务一定命中，每周一任务只在周一命中
    expected_today = 2 if today.isoweekday() != 1 else 3

    counts = store.task_counts()
    assert counts.due_today == expected_today
    assert counts.pending == expected_today
    assert counts.completed == 0
    assert counts.total == expected_today          # total = pending + completed
    # 没有单次任务时，"今天不排班的周期规则"不该被算进来
    if today.isoweekday() != 1:
        assert counts.total == 2

    # 打卡一条之后：待办 -1、已完成 +1、今天待办 -1，总量不变
    store.complete_occurrence(daily_id)
    after = store.task_counts()
    assert after.due_today == expected_today - 1
    assert after.pending == expected_today - 1
    assert after.completed == 1
    assert after.total == expected_today
    store.close()


def test_task_counts_still_count_once_tasks() -> None:
    """周期任务升级不能改变单次任务在总览里的口径。"""
    store = _store()
    store.add_task("单次A", due_at=datetime.now() + timedelta(hours=2))
    store.add_task("单次B")                                   # 无期限
    store.add_task("每天跑步", task_type=R.TYPE_DAILY, time_start="00:00", time_end="23:59")
    counts = store.task_counts()
    assert counts.pending == 3                                # 2 条单次 + 1 条今天该做
    assert counts.completed == 0
    assert counts.total == 3
    store.close()


def test_task_stats_by_day_counts_recurring_checkins() -> None:
    """周期任务的打卡要出现在 7 天图里（"每日跑步的打卡记录"）。"""
    store = _store()
    task_id = store.add_task("每天跑步", task_type=R.TYPE_DAILY,
                             time_start="00:00", time_end="23:59")
    today = date.today()
    yesterday = today - timedelta(days=1)
    # 昨天按时打卡、今天迟到打卡
    store.log_occurrence(task_id, yesterday, completed=True, was_late=False,
                         completed_at=datetime.combine(yesterday, time(10, 0)))
    store.log_occurrence(task_id, today, completed=True, was_late=True,
                         completed_at=datetime.combine(today, time(22, 0)))

    stats = {item.day: item for item in store.task_stats_by_day(7)}
    key_yesterday = yesterday.strftime("%Y-%m-%d")
    key_today = today.strftime("%Y-%m-%d")
    assert stats[key_yesterday].on_time == 1
    assert stats[key_yesterday].late == 0
    assert stats[key_today].late == 1
    assert stats[key_today].on_time == 0
    store.close()


def test_task_stats_by_day_flags_missed_recurring_as_overdue() -> None:
    """计划内、没打卡、当天已过结束时间 → 记成逾期未完成。"""
    store = _store()
    # start_date=""：模拟"这条任务两天前就存在了"，不受"才建 → 从明天开始"影响
    store.add_task("每天跑步", task_type=R.TYPE_DAILY, time_start="00:01", time_end="00:02",
                   start_date="")
    # 把创建时间往前挪，模拟"这个每日任务两天前就存在了"
    old_day = date.today() - timedelta(days=2)
    store._conn.execute(  # noqa: SLF001
        "UPDATE tasks SET created_at = ? WHERE task_type = ?",
        (datetime.combine(old_day, time(0, 0)).strftime("%Y-%m-%d %H:%M:%S"), R.TYPE_DAILY))
    store._conn.commit()  # noqa: SLF001
    stats = {item.day: item for item in store.task_stats_by_day(7)}
    assert stats[old_day.strftime("%Y-%m-%d")].overdue == 1
    store.close()


def test_task_stats_do_not_backfill_before_creation() -> None:
    """任务创建之前的日子不该被算成逾期（今天是新建的，不该"回溯"出 6 天逾期）。"""
    store = _store()
    store.add_task("今天新建的每日任务", task_type=R.TYPE_DAILY,
                   time_start="00:01", time_end="00:02", start_date="")
    stats = store.task_stats_by_day(7)
    overdue_days = [item.day for item in stats if item.overdue]
    assert overdue_days == [date.today().strftime("%Y-%m-%d")], overdue_days
    store.close()


def _passed_window_end(now: datetime) -> str | None:
    """构造一个"今天已经结束"的时间窗结束时刻（``HH:MM``）；构造不出时返回 ``None``。

    用"当前这一分钟"最稳：``combine(今天, HH:MM) <= now`` 恒成立，而且一定还在今天
    —— 不能用 ``now - 1 分钟``，那样在 00:00:xx 会退到昨天，用例就会因为跨零点而挂。
    只有 00:00 这一分钟真的构造不出来（结束时刻不能早于开始时刻 00:00）。
    """
    if now.hour == 0 and now.minute == 0:
        return None
    return now.strftime("%H:%M")


def test_new_task_with_passed_window_is_not_overdue_today() -> None:
    """新建的周期任务如果今天的时间窗已经过去，**今天不算逾期**，从明天开始。

    用户反馈的场景：晚上 21 点建「每天 06:00~08:00」，列表立刻显示"今天已超期"。
    现在的口径：这条任务今天根本不排班（``start_date`` 是明天），所以
    列表里它在"本周待办"、"今天到期/超期"计数里也不出现。
    """
    store = _store()
    now = datetime.now()
    end = _passed_window_end(now)                        # 已经过去的窗口
    if end is None:
        print("  [跳过] 正好 00:00，构造不出『今天已过去』的时间窗")
        store.close()
        return
    task_id = store.add_task("晚上才建的每日跑步", task_type=R.TYPE_DAILY,
                             time_start="00:00", time_end=end)
    task = store.get_task(task_id)
    assert task is not None
    assert task.rule.start_date == now.date() + timedelta(days=1)
    assert task.rule.occurs_on(now.date()) is False      # 今天不排班
    assert task.rule.occurs_on(now.date() + timedelta(days=1)) is True

    counts = store.task_counts()
    assert counts.overdue == 0, f"不该算今天超期：{counts}"
    assert counts.pending == 0 and counts.due_today == 0
    # 图表里也不该出现"今天逾期"
    stats = {item.day: item for item in store.task_stats_by_day(7)}
    assert stats[now.date().strftime("%Y-%m-%d")].overdue == 0
    # 发生的"下一次"是明天
    occurrence = task.rule.next_occurrence(now)
    assert occurrence is not None and occurrence.day == now.date() + timedelta(days=1)
    store.close()


def test_editing_window_to_passed_time_pushes_start_date() -> None:
    """把已有任务的时间段改成"今天已经过去"的，同样从明天开始（但已打卡的不动）。"""
    store = _store()
    now = datetime.now()
    end = _passed_window_end(now)
    if end is None:
        print("  [跳过] 正好 00:00，构造不出『今天已过去』的时间窗")
        store.close()
        return
    task_id = store.add_task("每日跑步", task_type=R.TYPE_DAILY,
                             time_start="00:00", time_end="23:59", start_date="")
    assert store.get_task(task_id).rule.start_date is None

    assert store.update_task(task_id, time_start="00:00", time_end=end) is True
    assert store.get_task(task_id).rule.start_date == now.date() + timedelta(days=1)

    # 已经打过卡的今天不能被抹掉：先打卡，再把时间段改成已过去的 → 不推生效日
    other = store.add_task("每日阅读", task_type=R.TYPE_DAILY,
                           time_start="00:00", time_end="23:59", start_date="")
    assert store.complete_occurrence(other, now.date(), completed=True) is True
    assert store.update_task(other, time_start="00:00", time_end=end) is True
    assert store.get_task(other).rule.start_date is None
    assert store.is_occurrence_done(other, now.date()) is True
    store.close()


def test_once_task_stats_unaffected_by_recurring_merge() -> None:
    """单次任务的原有统计口径不能被改动。"""
    store = _store()
    due = datetime.now() - timedelta(hours=1)
    task_id = store.add_task("已超期单次", due_at=due)
    store.add_task("还早的单次", due_at=datetime.now() + timedelta(days=3))
    stats = {item.day: item for item in store.task_stats_by_day(7)}
    today_key = date.today().strftime("%Y-%m-%d")
    assert stats[today_key].overdue == 1
    # 完成它 → 变成"迟到完成"
    store.complete_task(task_id)
    stats = {item.day: item for item in store.task_stats_by_day(7)}
    assert stats[today_key].overdue == 0
    assert stats[today_key].late == 1
    store.close()


def test_pending_tasks_excludes_recurring() -> None:
    """周期任务不能混进待办列表，否则会和"错过的周期任务"重复展示。"""
    store = _store()
    daily = store.add_task("每天跑步", task_type=R.TYPE_DAILY,
                           time_start="06:00", time_end="08:00")
    weekly = store.add_task("每周高数", task_type=R.TYPE_WEEKLY, days_of_week="1,3",
                            time_start="18:00", time_end="20:00")
    single = store.add_task("交周报", due_at=datetime.now() + timedelta(hours=2))

    pending_ids = {task.id for task in store.pending_tasks()}
    assert single in pending_ids
    assert daily not in pending_ids
    assert weekly not in pending_ids

    # 明确要周期任务时再给（供待办页的"周期任务"区块使用）
    recurring_ids = {task.id for task in store.list_tasks("all", recurring=True)}
    assert {daily, weekly} <= recurring_ids
    assert single not in recurring_ids

    # 老库遗留行：迁移时靠 DEFAULT 'once' 补的值，必须仍被当作单次任务
    cursor = store._conn.execute(  # noqa: SLF001
        "INSERT INTO tasks(title, created_at, completed, priority) VALUES(?, ?, 0, 1)",
        ("老库遗留任务", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    legacy_id = int(cursor.lastrowid or 0)
    store._conn.commit()  # noqa: SLF001
    legacy = store.get_task(legacy_id)
    assert legacy is not None and legacy.rule.task_type == R.TYPE_ONCE
    assert legacy_id in {task.id for task in store.pending_tasks()}
    store.close()


def test_completed_occurrence_not_counted_as_overdue() -> None:
    """回归（S3）：今天打卡完成后不能同时算"已完成"和"已超期"。"""
    store = _store()
    task_id = store.add_task("每天跑步", task_type=R.TYPE_DAILY,
                             time_start="00:01", time_end="00:02",   # 窗口早已结束
                             start_date="")   # 固定"今天有这一次"，绕开自动生效日
    before = store.task_counts()
    assert before.overdue == 1 and before.completed == 0

    store.complete_occurrence(task_id)
    after = store.task_counts()
    assert after.completed == 1
    assert after.overdue == 0, "打卡完成后仍被算成超期（同一次发生双计）"
    assert after.pending == 0
    assert after.total == after.pending + after.completed
    # 总览也不该把同一次算成"迟到 + 超期"两份
    overview = store.task_overview()
    assert overview["overdue"] == 0
    store.close()


# ================================================================ 旧库迁移
_LEGACY_SCHEMA = """
CREATE TABLE daily_target (
    day TEXT NOT NULL, target_name TEXT NOT NULL, target_kind TEXT NOT NULL DEFAULT 'process',
    seconds REAL NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, PRIMARY KEY (day, target_name)
);
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    due_at TEXT,
    created_at TEXT NOT NULL,
    completed INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT,
    priority INTEGER NOT NULL DEFAULT 1,
    remind_count INTEGER NOT NULL DEFAULT 0,
    last_remind_at TEXT,
    note TEXT
);
"""


def test_legacy_database_migration_adds_recurrence_columns() -> None:
    """模拟 v1.1 老库：缺 v1.3 的周期字段与 task_logs 表。"""
    db_dir = Path(tempfile.mkdtemp(prefix="tg_legacy_recur_"))
    db_path = db_dir / "usage.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(_LEGACY_SCHEMA)
    conn.execute("INSERT INTO daily_target VALUES('2026-10-01','game.exe','game',3600,'x')")
    conn.execute(
        "INSERT INTO tasks(title, due_at, created_at, completed, priority) "
        "VALUES('老任务', '2026-10-01 09:00:00', '2026-10-01 08:00:00', 0, 2)"
    )
    conn.commit()
    conn.close()

    store = UsageStore(db_path)
    # 旧数据原样保留
    assert round(store.total_seconds("2026-10-01"), 1) == 3600.0
    legacy = store.get_task(1)
    assert legacy is not None
    assert legacy.title == "老任务"
    assert legacy.priority == 2
    assert legacy.is_recurring is False                     # 老任务退化为单次
    assert legacy.rule.remind_before_minutes == 15          # 默认值生效
    assert legacy.due_at is not None and legacy.due_at.hour == 9

    # 新列与新表都补齐了
    columns = {row[1] for row in sqlite3.connect(db_path).execute("PRAGMA table_info(tasks)")}
    assert {"task_type", "time_start", "time_end", "days_of_week",
            "remind_before_minutes"} <= columns
    tables = {row[0] for row in sqlite3.connect(db_path).execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "task_logs" in tables

    # 新功能在升级后的库上可用
    task_id = store.add_task("升级后新增的每日任务", task_type=R.TYPE_DAILY,
                             time_start="06:00", time_end="08:00")
    assert task_id is not None
    assert store.complete_occurrence(task_id, date(2026, 10, 7)) is True
    assert store.is_occurrence_done(task_id, date(2026, 10, 7)) is True
    store.close()

    # 再次打开（幂等）：不应报错、数据仍在
    again = UsageStore(db_path)
    assert again.get_task(1) is not None
    assert again.is_occurrence_done(task_id, date(2026, 10, 7)) is True
    again.close()


def test_task_logs_survive_reopen() -> None:
    store = _store()
    task_id = store.add_task("每天跑步", task_type=R.TYPE_DAILY,
                             time_start="06:00", time_end="08:00")
    store.log_occurrence(task_id, date(2026, 10, 7), completed=True, remind_kind="late")
    path = store.path
    store.close()

    reopened = UsageStore(path)
    log_row = reopened.get_task_log(task_id, date(2026, 10, 7))
    assert log_row is not None
    assert log_row.completed is True
    assert log_row.remind_kind == "late"
    reopened.close()


# ================================================================ 生效起始日（v1.4）
def test_suggested_start_date_is_tomorrow_when_window_passed() -> None:
    """今天的时间窗已经过去 → 从明天开始；还没到 → 今天就算。

    用户反馈：晚上 21 点建一条「每天 06:00~08:00」，列表立刻显示"今天已超期"，
    但用户的预期是"这个任务从明天开始"。
    """
    now = datetime(2026, 10, 8, 21, 0)                  # 周四晚上 21:00
    assert R.suggested_start_date(R.TYPE_DAILY, "06:00", "08:00", now) == "2026-10-09"
    assert R.suggested_start_date(R.TYPE_DAILY, "22:00", "23:00", now) == "2026-10-08"
    # 正好卡在结束时刻也算"已过"（边界）
    assert R.suggested_start_date(R.TYPE_DAILY, "19:00", "21:00", now) == "2026-10-09"
    # 逆序时间段（22:00~06:00，按规则归一化成到 23:59）→ 当天还没结束
    assert R.suggested_start_date(R.TYPE_DAILY, "22:00", "06:00", now) == "2026-10-08"
    # 每周任务同理；单次任务不参与
    assert R.suggested_start_date(R.TYPE_WEEKLY, "06:00", "08:00", now) == "2026-10-09"
    assert R.suggested_start_date(R.TYPE_ONCE, "06:00", "08:00", now) == ""
    # 上午 9 点时，"今天 06:00~08:00"也已经过去了
    morning = datetime(2026, 10, 8, 9, 0)
    assert R.suggested_start_date(R.TYPE_DAILY, "06:00", "08:00", morning) == "2026-10-09"
    early = datetime(2026, 10, 8, 5, 0)
    assert R.suggested_start_date(R.TYPE_DAILY, "06:00", "08:00", early) == "2026-10-08"


def test_rule_start_date_blocks_earlier_days() -> None:
    """生效日之前的那些天，规则一律"不排班"（列表/统计/调度都不会算它逾期）。"""
    now = datetime(2026, 10, 8, 21, 0)
    rule = R.TaskRule.daily("06:00", "08:00", start_date=date(2026, 10, 9))
    assert rule.occurs_on(date(2026, 10, 8)) is False
    assert rule.window_on(date(2026, 10, 8)) is None
    assert rule.occurs_on(date(2026, 10, 9)) is True
    assert rule.starts_later_than(date(2026, 10, 8)) is True

    assert rule.next_occurrence(now) == R.Occurrence(
        date(2026, 10, 9), datetime(2026, 10, 9, 6, 0), datetime(2026, 10, 9, 8, 0))
    # 往前找也一样：生效日之前没有"上一次"
    assert rule.previous_occurrence(now) is None
    assert rule.occurrences_between(datetime(2026, 10, 7, 0, 0),
                                    datetime(2026, 10, 9, 23, 59)) == [
        R.Occurrence(date(2026, 10, 9), datetime(2026, 10, 9, 6, 0), datetime(2026, 10, 9, 8, 0))]
    # 文案里写明从哪天开始，免得看着像"已经生效但没做"。
    # 注意：schedule_text() 要和**真实的今天**比，所以这里不能用上面那个固定日期
    # —— 用例写死 2026-10-09 时，一旦真的到了 10-09（跨零点）就会挂，
    # CI 上就是这么红过一次。改用相对今天的动态日期。
    tomorrow = date.today() + timedelta(days=1)
    future = R.TaskRule.daily("06:00", "08:00", start_date=tomorrow)
    assert "开始）" in future.schedule_text(), future.schedule_text()
    assert f"{tomorrow:%m-%d} 开始" in future.schedule_text(), future.schedule_text()
    # 生效日就是今天（或过去）→ 不该有多余说明
    today_rule = R.TaskRule.daily("06:00", "08:00", start_date=date.today())
    assert "开始）" not in today_rule.schedule_text(), today_rule.schedule_text()


def test_rule_from_row_handles_start_date_and_legacy_null() -> None:
    """``start_date`` 列能读出来；老库没有这一列（NULL）时按"一直有效"处理。"""
    row = {"task_type": R.TYPE_DAILY, "time_start": "06:00", "time_end": "08:00",
           "days_of_week": None, "remind_before_minutes": 15, "start_date": "2026-10-09"}
    rule = R.TaskRule.from_row(row)
    assert rule.start_date == date(2026, 10, 9)
    assert rule.occurs_on(date(2026, 10, 8)) is False

    for legacy in (None, "", "不是日期"):
        row["start_date"] = legacy
        old = R.TaskRule.from_row(row)
        assert old.start_date is None, legacy
        assert old.occurs_on(date(2020, 1, 1)) is True        # 老数据行为不变

    # 缺列（比 v1.4 更老的库，连列都没有）
    bare = {"task_type": R.TYPE_DAILY, "time_start": "06:00", "time_end": "08:00"}
    assert R.TaskRule.from_row(bare).start_date is None


# ================================================================ 简易执行器
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

"""累计打卡任务单元测试（v1.5 / v1.5.1）。

按用户给出的 **5 条避坑指南** 组织，每条都对应"如果不这么做会很难用"的具体场景：

1. 完成后必须停止每日提醒（否则第 50 天达标后每天 18:00 还在弹窗骚扰）
2. 自然日边界 + 同一天只能打一次卡（凌晨 00:10 算新的一天，不是补昨天）
3. 与时间监控的联动：v1.5.1 起支持"监控时长达标自动打卡"（可选，默认关）
4. 数据结构必须能画热力图（要有 check_in_logs 这张表，而不是一个计数字段）
5. "逾期"的定义：**今天 > 截止日** 且次数不够才算；截止日当天仍可打卡（压哨完成）

v1.5.1 新增的两件事也在这里测：

* **补签**：只能补"任务创建之后、今天之前"的日子，且要能看出是补的（``source``）；
* **自动打卡**：监控时长够了自动打一次，同一天不重复，达标/过期后不自动打。

时间口径说明：所有用例都用**相对今天**的日期（``today - N``），不写死 2026-xx-xx
—— 写死日期的用例会随着真实时间流逝变成"未来日期"或"过去日期"，CI 上红过一次。
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
from timeguard.utils import match_target_seconds, use_utf8_console  # noqa: E402

TODAY = date.today()


def _store() -> UsageStore:
    """每个用例一份独立数据库。"""
    return UsageStore(Path(tempfile.mkdtemp(prefix="tg_cum_case_")) / "usage.db")


def _backdate(store: UsageStore, task_id: int, days: int = 30) -> None:
    """把创建时间往前挪，模拟"这条任务早就建好了"。

    必须这么做才能补签：``check_in`` 会拒绝"早于任务创建日期"的日子
    （那是真实存在的约束，不是测试的麻烦）。
    """
    stamp = (datetime.now() - timedelta(days=max(1, int(days)))).strftime("%Y-%m-%d %H:%M:%S")
    store._conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?",  # noqa: SLF001
                        (stamp, int(task_id)))
    store._conn.commit()                                                  # noqa: SLF001


def _task(store: UsageStore, target: int = 60, deadline: date | None = None,
          title: str = "年末前完成 60 次两公里跑", *, backdate_days: int = 400,
          **extra) -> int:
    """建一条累计打卡任务（默认把创建时间往前挪，方便补签历史）。"""
    deadline = deadline or (TODAY + timedelta(days=60))
    task_id = store.add_task(title, task_type=R.TYPE_CUMULATIVE, target_count=target,
                             deadline=deadline.strftime("%Y-%m-%d"), remind_time="18:00",
                             **extra)
    assert task_id is not None
    if backdate_days:
        _backdate(store, task_id, backdate_days)
    return task_id


def _fill(store: UsageStore, task_id: int, count: int, *, last_day: date,
          gap_days: int = 1) -> list[date]:
    """从 ``last_day`` 往前每隔 ``gap_days`` 天打一次卡，共 ``count`` 次（全是过去的日子）。"""
    days = [last_day - timedelta(days=i * gap_days) for i in range(count)]
    for day in reversed(days):                     # 正序写入，像真实使用那样
        ok, msg = store.check_in(task_id, day)
        assert ok, f"{day} 打卡失败：{msg}"
    return sorted(days)


# ============================================================ 5. 逾期定义的严谨性
def test_running_before_deadline_never_says_overdue() -> None:
    """避坑 #5（核心诉求）：截止日之前，**绝不能出现"逾期/失败"字样**。"""
    store = _store()
    task_id = _task(store, deadline=TODAY + timedelta(days=60))
    _fill(store, task_id, 59, last_day=TODAY - timedelta(days=1))       # 差 1 次

    for offset in (0, 1, 2, 30, 60):
        day = TODAY + timedelta(days=offset)
        status = R.cumulative_status(store.get_task(task_id).rule,
                                     store.checkin_dates(task_id), day)
        assert status.state == R.STATE_RUNNING, f"{day} 不该是 {status.state}"
        text = status.status_text()
        assert "逾期" not in text and "失败" not in text and "未达标" not in text, text
        assert "还差 1 次" in text, text
        assert status.state_label == "进行中"
    store.close()


def test_only_the_day_after_deadline_becomes_missed() -> None:
    """避坑 #5：只有 今天 > 截止日 且 次数不够，才判逾期。"""
    store = _store()
    deadline = TODAY + timedelta(days=2)
    task_id = _task(store, target=3, deadline=deadline)
    _fill(store, task_id, 2, last_day=TODAY - timedelta(days=1))
    rule = store.get_task(task_id).rule
    dates = store.checkin_dates(task_id)

    assert R.cumulative_status(rule, dates, deadline).state == R.STATE_RUNNING
    missed = R.cumulative_status(rule, dates, deadline + timedelta(days=1))
    assert missed.state == R.STATE_MISSED
    assert "逾期未达标" in missed.state_label
    assert "逾期" in missed.status_text()            # 这时候才允许出现"逾期"
    assert missed.remaining == 1
    assert missed.countdown_text() == "已过期 1 天"
    store.close()


def test_checkin_on_deadline_day_is_allowed_and_finishes_on_time() -> None:
    """避坑 #5：截止日 23:00 打卡仍然算数 → 压哨完成，而不是逾期。"""
    store = _store()
    task_id = _task(store, target=3, deadline=TODAY)          # 今天就是最后一天
    _fill(store, task_id, 2, last_day=TODAY - timedelta(days=2))

    ok, msg = store.check_in(task_id, now=datetime.now().replace(hour=23, minute=0, second=0))
    assert ok, msg
    assert store.checkin_log(task_id, TODAY) is not None

    status = store.task_status(task_id)
    assert status.state == R.STATE_DONE_DEADLINE
    assert status.state_label == "压哨完成"
    assert "压哨完成" in status.status_text()
    assert status.finished is True
    store.close()


def test_finish_before_deadline_is_early_finish() -> None:
    """避坑 #1 的前半段：截止日之前攒够 → 提前完成，并记住是哪天达标的。"""
    store = _store()
    task_id = _task(store, target=60, deadline=TODAY + timedelta(days=60))
    days = _fill(store, task_id, 60, last_day=TODAY - timedelta(days=1))
    status = R.cumulative_status(store.get_task(task_id).rule,
                                 store.checkin_dates(task_id), TODAY)
    assert status.state == R.STATE_DONE_EARLY
    assert status.reached_on == days[-1] == TODAY - timedelta(days=1)
    assert "提前完成" in status.status_text()
    assert status.remaining == 0 and status.ratio == 1.0 and status.percent == 100
    store.close()


def test_extra_checkins_do_not_downgrade_early_finish() -> None:
    """达标之后再打卡，不会把"提前完成"改成"压哨完成"。

    判定取的是**第 N 次打卡那一天**（N = 目标次数），不是"最后一次打卡"。
    """
    store = _store()
    deadline = TODAY + timedelta(days=2)
    task_id = _task(store, target=3, deadline=deadline)
    _fill(store, task_id, 3, last_day=TODAY - timedelta(days=3))       # 3 天前就达标了
    assert store.check_in(task_id, TODAY)[0] is True                   # 今天再打一次
    status = R.cumulative_status(store.get_task(task_id).rule,
                                 store.checkin_dates(task_id), TODAY)
    assert status.total == 4
    assert status.state == R.STATE_DONE_EARLY
    assert status.reached_on == TODAY - timedelta(days=3)
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
            (task_id, TODAY.strftime("%Y-%m-%d"),
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        store._conn.commit()                       # noqa: SLF001
        raise AssertionError("同一天插入了两条打卡记录，UNIQUE 约束没生效")
    except sqlite3.IntegrityError:
        pass
    store.close()


def test_natural_day_boundary_uses_local_date() -> None:
    """避坑 #2：00:10 打卡算**新的一天**，23:50 算当天 —— 按本地自然日切分。"""
    store = _store()
    task_id = _task(store, target=10)

    ok, _ = store.check_in(task_id, now=datetime.combine(TODAY - timedelta(days=1),
                                                         datetime.min.time().replace(hour=23, minute=50)))
    assert ok
    assert store.checkin_dates(task_id) == [TODAY - timedelta(days=1)]

    ok, msg = store.check_in(task_id, now=datetime.combine(TODAY,
                                                           datetime.min.time().replace(hour=0, minute=10)))
    assert ok, msg
    assert store.checkin_dates(task_id) == [TODAY - timedelta(days=1), TODAY]
    assert store.checkin_count(task_id) == 2
    # 打卡时间戳保留到秒，便于展示与审计
    log_row = store.checkin_log(task_id, TODAY)
    assert log_row is not None
    assert log_row.checkin_at.hour == 0 and log_row.checkin_at.minute == 10
    assert log_row.time_text == "00:10:00"
    store.close()


def test_cannot_check_in_after_deadline() -> None:
    """过了截止日就不能再打卡（状态已经是"逾期未达标"，再打也没有意义）。"""
    store = _store()
    task_id = _task(store, target=10, deadline=TODAY)
    ok, msg = store.check_in(task_id, day=TODAY + timedelta(days=1))
    assert ok is False and ("已过截止日期" in msg or "还没到" in msg), msg
    assert store.checkin_count(task_id) == 0
    ok2, msg2 = store.check_in(task_id, day=TODAY)                   # 截止日当天可以
    assert ok2 is True, msg2
    store.close()


def test_undo_check_in() -> None:
    """点错了可以撤销当天打卡。"""
    store = _store()
    task_id = _task(store, target=10)
    assert store.check_in(task_id)[0] is True
    assert store.checkin_count(task_id) == 1
    assert store.undo_check_in(task_id) is True
    assert store.checkin_count(task_id) == 0
    assert store.checkin_log(task_id, TODAY) is None
    assert store.undo_check_in(task_id) is False            # 没得撤了
    store.close()


# ============================================================ 补签（v1.5.1）
def test_backfill_past_day_with_note() -> None:
    """补签：可以补"任务创建之后、今天之前"的日子，并留下备注与来源标记。"""
    store = _store()
    task_id = _task(store, target=10, backdate_days=30)
    yesterday = TODAY - timedelta(days=1)

    ok, message = store.check_in(task_id, yesterday, note="昨天忘了打卡，补上")
    assert ok is True, message
    assert "补签成功" in message, message
    log_row = store.checkin_log(task_id, yesterday)
    assert log_row is not None
    assert log_row.source == "backfill" and log_row.source_label == "补签"
    assert log_row.note == "昨天忘了打卡，补上"
    # 补签算进度（热力图与进度条都会体现）
    assert store.checkin_count(task_id) == 1
    status = store.task_status(task_id)
    assert status.total == 1 and status.today_checked is False
    assert status.status_text().startswith("○ 今日未打卡")
    store.close()


def test_backfill_rejects_future_pre_creation_and_duplicate() -> None:
    """补签的三条红线：不能补未来、不能补任务创建之前、同一天只能补一次。"""
    store = _store()
    task_id = _task(store, target=10, backdate_days=10)
    created = store.get_task(task_id).created_at.date()

    ok_future, msg_future = store.check_in(task_id, TODAY + timedelta(days=1))
    assert ok_future is False and "还没到" in msg_future, msg_future

    ok_old, msg_old = store.check_in(task_id, created - timedelta(days=1))
    assert ok_old is False and "早于任务创建日期" in msg_old, msg_old

    yesterday = TODAY - timedelta(days=1)
    assert store.check_in(task_id, yesterday)[0] is True
    ok_dup, msg_dup = store.check_in(task_id, yesterday)
    assert ok_dup is False and "已经打过卡" in msg_dup, msg_dup
    assert store.checkin_count(task_id) == 1
    store.close()


def test_backfill_respects_deadline() -> None:
    """截止日之前的日子都能补；过了截止日就不能再补了。"""
    store = _store()
    task_id = _task(store, target=5, deadline=TODAY - timedelta(days=1), backdate_days=30)
    assert store.check_in(task_id, TODAY - timedelta(days=1))[0] is True      # 截止日当天
    ok, msg = store.check_in(task_id, TODAY)                                  # 截止日之后
    assert ok is False and "已过截止日期" in msg, msg
    store.close()


# ============================================================ 监控时长自动打卡（v1.5.1）
def test_auto_checkin_rule_thresholds() -> None:
    """自动打卡的判定：时长不够不打、够了才打、已打卡/过期/没开都不打。"""
    rule = R.TaskRule.cumulative(60, TODAY + timedelta(days=30), "18:00",
                                 auto_target="高数", auto_minutes=30)
    assert rule.auto_enabled is True
    assert rule.auto_checkin_text() == "自动打卡：高数 今日累计满 30 分钟"

    ready, why = R.auto_checkin_ready(rule, 0)
    assert ready is False and "还差 30 分钟" in why, why
    ready, why = R.auto_checkin_ready(rule, 29 * 60 + 59)
    assert ready is False, why
    ready, why = R.auto_checkin_ready(rule, 30 * 60)                 # 正好到阈值
    assert ready is True and "自动打卡" in why, why
    ready, why = R.auto_checkin_ready(rule, 2 * 60 * 60)             # 超了当然也算
    assert ready is True, why

    # 今天已经打过卡 → 不再自动打（同一天只算一次）
    ready, why = R.auto_checkin_ready(rule, 99 * 60, checked_today=True)
    assert ready is False and "已经打过卡" in why, why

    # 过期任务不再自动打卡
    expired = R.TaskRule.cumulative(60, TODAY - timedelta(days=1), "18:00",
                                    auto_target="高数", auto_minutes=30)
    ready, why = R.auto_checkin_ready(expired, 99 * 60)
    assert ready is False and "已过截止日期" in why, why

    # 没开这个功能的任务：永远不自动打卡
    plain = R.TaskRule.cumulative(60, TODAY + timedelta(days=30), "18:00")
    assert plain.auto_enabled is False and plain.auto_checkin_text() == ""
    ready, why = R.auto_checkin_ready(plain, 999 * 60)
    assert ready is False and "没有开启" in why, why
    # 周期任务即使填了监控对象也不算（只有累计打卡支持）
    daily = R.TaskRule.daily("06:00", "08:00")
    object.__setattr__(daily, "auto_target", "高数")
    assert daily.auto_enabled is False


def test_auto_target_matching() -> None:
    """监控对象名匹配：相同 / 名字含关键词 / 关键词含名字，多个命中时长相加。"""
    targets = {"高数 - 学习": 600.0, "高数": 300.0, "游戏": 60.0}
    name, seconds = match_target_seconds(targets, "高数")
    assert name == "高数 - 学习" and seconds == 900.0        # 两个都命中 → 相加
    assert match_target_seconds(targets, "游戏") == ("游戏", 60.0)
    assert match_target_seconds(targets, " 高数 ") == ("高数 - 学习", 900.0)   # 忽略空格
    # 关键词写全了也算命中（名字含关键词 + 关键词含名字，两边都会算上）
    assert match_target_seconds(targets, "高数 - 学习") == ("高数 - 学习", 900.0)
    assert match_target_seconds(targets, "哔哩哔哩动画") is None
    assert match_target_seconds({"哔哩哔哩": 120.0}, "哔哩哔哩动画") == ("哔哩哔哩", 120.0)
    assert match_target_seconds({}, "高数") is None
    assert match_target_seconds(targets, "") is None


def test_auto_checkin_fields_stored_and_used() -> None:
    """自动打卡的字段能落库、能被规则读出来，也能关掉。"""
    store = _store()
    task_id = _task(store, target=10, auto_target="高数", auto_minutes=45)
    task = store.get_task(task_id)
    assert task.rule.auto_target == "高数" and task.rule.auto_minutes == 45
    assert task.rule.auto_enabled is True

    # 只给对象不给分钟 → 用默认 30 分钟
    other = _task(store, target=10, title="只给对象", auto_target="背单词")
    assert store.get_task(other).rule.auto_minutes == R.DEFAULT_AUTO_MINUTES

    # 关掉自动打卡
    assert store.update_task(task_id, clear_auto=True) is True
    assert store.get_task(task_id).rule.auto_enabled is False

    # 改成周期任务时，自动打卡字段也要一起清掉（否则库里留着脏数据）
    assert store.update_task(task_id, task_type=R.TYPE_DAILY,
                             time_start="06:00", time_end="08:00") is True
    row = store._conn.execute(                                    # noqa: SLF001
        "SELECT auto_target, auto_minutes FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row["auto_target"] is None and row["auto_minutes"] is None
    store.close()


# ============================================================ 1. 完成后停止提醒
def test_reminder_stops_after_target_reached() -> None:
    """避坑 #1（用户最在意的骚扰问题）：达标后 next_checkin_reminder 必须返回 None。"""
    now = datetime.combine(TODAY, datetime.min.time().replace(hour=9))
    rule = R.TaskRule.cumulative(60, TODAY + timedelta(days=30), "18:00")
    assert rule.next_checkin_reminder(now) == datetime.combine(
        TODAY, datetime.min.time().replace(hour=18))
    assert rule.next_checkin_reminder(now, day_done=True) == datetime.combine(
        TODAY + timedelta(days=1), datetime.min.time().replace(hour=18))
    assert rule.next_checkin_reminder(now, finished=True) is None       # 达标 → 不再提醒


def test_reminder_stops_after_deadline() -> None:
    """过了截止日也不再提醒（任务已经结束，再提醒只是噪音）。"""
    deadline = TODAY + timedelta(days=1)
    rule = R.TaskRule.cumulative(60, deadline, "18:00")
    assert rule.next_checkin_reminder(datetime.combine(
        deadline, datetime.min.time().replace(hour=9))) == datetime.combine(
        deadline, datetime.min.time().replace(hour=18))
    assert rule.next_checkin_reminder(datetime.combine(
        deadline + timedelta(days=1), datetime.min.time().replace(hour=9))) is None
    assert rule.is_expired(deadline) is False        # 截止日当天没过期
    assert rule.is_expired(deadline + timedelta(days=1)) is True


def test_reminder_after_todays_time_already_passed() -> None:
    """今天的提醒时刻已经过了且还没打卡：纯函数给明天，调度器负责"补发"今天那次。"""
    rule = R.TaskRule.cumulative(60, TODAY + timedelta(days=30), "18:00")
    now = datetime.combine(TODAY, datetime.min.time().replace(hour=20, minute=30))
    assert rule.next_checkin_reminder(now) == datetime.combine(
        TODAY + timedelta(days=1), datetime.min.time().replace(hour=18))
    assert rule.checkin_reminder_on(TODAY) == datetime.combine(
        TODAY, datetime.min.time().replace(hour=18))


# ============================================================ 4. 数据结构（热力图）
def test_checkin_history_supports_heatmap() -> None:
    """避坑 #4：check_in_logs 要能按日期取历史 —— 热力图才有数据可画。"""
    store = _store()
    task_id = _task(store, target=30, backdate_days=60)
    days = [TODAY - timedelta(days=19 - i * 2) for i in range(10)]     # 隔天打卡（全是过去）
    for day in days:
        assert store.check_in(task_id, day)[0] is True
    logs = store.checkin_logs(task_id)
    assert [log.checkin_date for log in logs] == [d.strftime("%Y-%m-%d") for d in days]
    assert all(log.source == "backfill" for log in logs)               # 过去的算补签
    # 区间查询（热力图只画最近 N 周）
    recent = store.checkin_logs(task_id, since=days[5], until=days[7])
    assert [log.checkin_date for log in recent] == [d.strftime("%Y-%m-%d") for d in days[5:8]]
    # 全任务的批量查询（图表用一次查完）
    assert len(store.checkin_logs_between(days[0], days[-1])) == 10
    # 次数与日期批量取（列表刷新不能 N+1）
    assert store.checkin_counts() == {task_id: 10}
    assert len(store.checkin_dates_map().get(task_id, [])) == 10
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
    task_id = _task(store, target=10, deadline=TODAY + timedelta(days=30))
    counts = store.task_counts()
    assert counts.overdue == 0, counts.summary_text()
    assert counts.pending == 1 and counts.due_today == 1
    assert counts.total == counts.pending + counts.completed

    assert store.check_in(task_id)[0] is True              # 今天打过卡
    counts2 = store.task_counts()
    assert counts2.overdue == 0 and counts2.pending == 0 and counts2.completed == 1

    # 过了截止日还没达标 → 这时候才计入"已超期"
    overdue_id = _task(store, target=10, title="已经过期的", deadline=TODAY - timedelta(days=1))
    counts3 = store.task_counts()
    assert counts3.overdue == 1, counts3.summary_text()
    assert store.get_task(overdue_id).rule.is_expired(TODAY) is True
    store.close()


def test_task_counts_ignore_finished_task() -> None:
    """已达标的任务不再算进"今天要做的事"（否则"已完成"会天天 +1）。"""
    store = _store()
    task_id = _task(store, target=2, deadline=TODAY + timedelta(days=10))
    assert store.check_in(task_id)[0] is True
    assert store.check_in(task_id, TODAY - timedelta(days=1))[0] is True
    status = store.task_status(task_id)
    assert status.finished is True
    counts = store.task_counts()
    assert counts.pending == 0 and counts.completed == 0 and counts.overdue == 0
    store.close()


def test_stats_by_day_counts_checkins_and_marks_overdue_on_deadline() -> None:
    """7 天图表：每次打卡算一次完成；没达标只在**截止日那天**记一笔逾期。"""
    store = _store()
    deadline = TODAY - timedelta(days=1)
    task_id = _task(store, target=10, deadline=deadline)
    for offset in (2, 1):
        day = TODAY - timedelta(days=offset)
        assert store.check_in(task_id, day)[0] is True
    stats = {item.day: item for item in store.task_stats_by_day(7)}
    for offset in (2, 1):
        day = (TODAY - timedelta(days=offset)).strftime("%Y-%m-%d")
        assert stats[day].on_time == 1, stats[day]
    assert stats[deadline.strftime("%Y-%m-%d")].overdue == 1, stats[deadline.strftime("%Y-%m-%d")]
    # 到期日之后的日子不该再重复记逾期（同一个失败只数一次）
    assert stats[TODAY.strftime("%Y-%m-%d")].overdue == 0
    store.close()


# ============================================================ 字段与迁移
def test_new_task_stores_cumulative_fields_cleanly() -> None:
    """累计打卡任务的周期字段必须留空，避免多出一份"假计划"。"""
    store = _store()
    task_id = _task(store, target=60, deadline=TODAY + timedelta(days=60))
    row = store._conn.execute(                       # noqa: SLF001
        "SELECT task_type, due_at, time_start, time_end, days_of_week, start_date,"
        " target_count, deadline, remind_time, auto_target, auto_minutes"
        " FROM tasks WHERE id = ?", (task_id,)).fetchone()
    data = dict(row)
    assert data["task_type"] == R.TYPE_CUMULATIVE
    assert data["target_count"] == 60
    assert data["deadline"] == (TODAY + timedelta(days=60)).strftime("%Y-%m-%d")
    assert data["remind_time"] == "18:00"
    assert data["auto_target"] is None and data["auto_minutes"] is None
    for empty in ("due_at", "time_start", "time_end", "days_of_week", "start_date"):
        assert data[empty] is None, f"{empty} 应该是空的：{data[empty]}"
    task = store.get_task(task_id)
    assert task.is_cumulative is True and task.is_recurring is False
    assert task.schedule_text.startswith("累计打卡 60 次 · 截止 ")
    assert "18:00 提醒" in task.schedule_text
    assert task.occurrence_on(TODAY) is None             # 没有"时间窗"这一说
    store.close()


def test_cumulative_requires_deadline_and_clamps_target() -> None:
    """没有截止日期不能建；目标次数会被夹到合法区间。"""
    store = _store()
    assert store.add_task("没截止日期", task_type=R.TYPE_CUMULATIVE, target_count=5) is None
    assert store.add_task("坏日期", task_type=R.TYPE_CUMULATIVE, deadline="不是日期") is None
    low = store.add_task("目标 0", task_type=R.TYPE_CUMULATIVE, target_count=0,
                         deadline=(TODAY + timedelta(days=10)).strftime("%Y-%m-%d"))
    assert store.get_task(low).rule.target_count == 1
    high = store.add_task("目标超大", task_type=R.TYPE_CUMULATIVE, target_count=999999,
                          deadline=(TODAY + timedelta(days=10)).strftime("%Y-%m-%d"))
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
    for column in ("target_count", "deadline", "remind_time", "auto_target", "auto_minutes"):
        assert column in columns, f"{column} 没补上：{columns}"
    tables = [row["name"] for row in store._conn.execute(               # noqa: SLF001
        "SELECT name FROM sqlite_master WHERE type = 'table'")]
    assert "check_in_logs" in tables, tables
    legacy = store.recurring_tasks()[0]
    assert legacy.title == "老库里的每日任务" and legacy.rule.time_start.hour == 6
    assert legacy.rule.target_count is None and legacy.rule.deadline is None
    assert legacy.rule.auto_enabled is False
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

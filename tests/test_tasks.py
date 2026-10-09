"""待办任务模块单元测试（数据库 / 任务模型 / 鼓励语 / 开机自启 / 日期选择器）。

运行方式::

    python tests/test_tasks.py         # 自带简易执行器
    python -m pytest tests -q          # 有 pytest 时

每个用例使用独立的临时数据目录，不会污染真实使用记录。
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

os.environ["TIMEGUARD_DATA_DIR"] = tempfile.mkdtemp(prefix="timeguard_task_test_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from timeguard import autostart, phrases  # noqa: E402
from timeguard.utils import use_utf8_console  # noqa: E402
from timeguard.database import UsageStore  # noqa: E402
from timeguard.datepicker import quick_datetime  # noqa: E402
from timeguard.tasks import build_reminder_text  # noqa: E402


def _store() -> UsageStore:
    """新建一个独立数据库（每个用例一份）。"""
    return UsageStore(Path(tempfile.mkdtemp(prefix="tg_task_case_")) / "usage.db")


def _ts(value: datetime) -> str:
    """转成数据库里的时间字符串。"""
    return value.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 增删改查
def test_add_task_with_and_without_due() -> None:
    store = _store()
    due = datetime.now() + timedelta(hours=3)
    task_id = store.add_task("写周报", due, priority=2)
    assert task_id is not None

    task = store.get_task(task_id)
    assert task is not None
    assert task.title == "写周报"
    assert task.completed is False
    assert task.priority == 2
    assert task.priority_cn == "重要"
    assert task.due_at is not None and abs((task.due_at - due).total_seconds()) < 60
    assert task.completed_at is None

    # 截止时间可以留空（无期限）
    plain_id = store.add_task("整理桌面素材")
    plain = store.get_task(plain_id)
    assert plain is not None and plain.due_at is None
    assert plain.due_text() == "无期限"
    assert plain.countdown_text() == "无期限"

    # 空标题不允许
    assert store.add_task("   ") is None
    store.close()


def test_complete_and_reopen_task() -> None:
    store = _store()
    task_id = store.add_task("跑步 30 分钟", datetime.now() + timedelta(hours=1))

    assert store.complete_task(task_id, True) is True
    task = store.get_task(task_id)
    assert task.completed is True and task.completed_at is not None
    assert task.countdown_text().startswith("已完成")

    # 取消完成
    assert store.complete_task(task_id, False) is True
    task = store.get_task(task_id)
    assert task.completed is False and task.completed_at is None
    assert "还剩" in task.countdown_text()
    store.close()


def test_update_and_delete_task() -> None:
    store = _store()
    task_id = store.add_task("旧标题", datetime.now() + timedelta(hours=2), priority=1)

    new_due = datetime.now() + timedelta(days=2)
    assert store.update_task(task_id, title="新标题", due_at=new_due, priority=2) is True
    task = store.get_task(task_id)
    assert task.title == "新标题" and task.priority == 2
    assert task.due_at is not None and abs((task.due_at - new_due).total_seconds()) < 60

    # 空标题不更新
    assert store.update_task(task_id, title="  ") is False
    # 什么都不传也不报错
    assert store.update_task(task_id) is False

    assert store.delete_task(task_id) is True
    assert store.get_task(task_id) is None
    store.close()


def test_list_tasks_filters_and_order() -> None:
    store = _store()
    now = datetime.now()
    early = store.add_task("最早到期", now + timedelta(hours=1))
    middle = store.add_task("中间到期", now + timedelta(hours=5))
    store.add_task("无期限任务")
    done = store.add_task("已完成任务", now + timedelta(hours=2))
    store.complete_task(done, True)

    pending = store.list_tasks("pending")
    titles = [t.title for t in pending]
    assert titles[:2] == ["最早到期", "中间到期"]      # 按截止时间升序
    assert titles[-1] == "无期限任务"                   # 无期限排最后
    assert "已完成任务" not in titles

    completed = store.list_tasks("completed")
    assert [t.title for t in completed] == ["已完成任务"]

    everything = store.list_tasks("all")
    assert len(everything) == 4
    assert store.pending_tasks()[0].id == early
    assert middle is not None
    store.close()


def test_clear_completed_tasks() -> None:
    store = _store()
    for index in range(3):
        task_id = store.add_task(f"任务{index}", datetime.now() + timedelta(hours=index + 1))
        if index < 2:
            store.complete_task(task_id, True)
    assert store.clear_completed_tasks() == 2
    assert len(store.list_tasks("all")) == 1
    store.close()


# ---------------------------------------------------------------- 状态与文案
def test_overdue_and_late_detection() -> None:
    store = _store()
    now = datetime.now()

    overdue_id = store.add_task("已经超期的任务", now - timedelta(hours=2))
    future_id = store.add_task("还早的任务", now + timedelta(hours=2))
    overdue = store.get_task(overdue_id)
    future = store.get_task(future_id)
    assert overdue is not None and overdue.is_overdue()
    assert future is not None and not future.is_overdue()
    assert overdue.countdown_text().startswith("已超期")
    assert future.countdown_text().startswith("还剩")

    # 超期后才完成 -> was_late
    store.complete_task(overdue_id, True)
    late_task = store.get_task(overdue_id)
    assert late_task is not None and late_task.was_late()
    assert "超期" in late_task.countdown_text()

    # 在截止前完成 -> 不算超期
    store.complete_task(future_id, True)
    on_time_task = store.get_task(future_id)
    assert on_time_task is not None and not on_time_task.was_late()
    assert on_time_task.countdown_text() == "已完成"
    store.close()


def test_due_text_labels() -> None:
    store = _store()
    now = datetime.now().replace(second=0, microsecond=0)
    today_id = store.add_task("今天", now + timedelta(hours=1))
    tomorrow_id = store.add_task("明天", (now + timedelta(days=1)).replace(hour=9))
    task_today = store.get_task(today_id)
    task_tomorrow = store.get_task(tomorrow_id)
    assert task_today is not None and task_today.due_text().startswith("今天")
    assert task_tomorrow is not None and task_tomorrow.due_text().startswith("明天")
    assert ":" in task_today.due_text()      # 精确到分钟
    store.close()


def test_remaining_seconds() -> None:
    store = _store()
    task_id = store.add_task("倒计时", datetime.now() + timedelta(minutes=10))
    task = store.get_task(task_id)
    assert task is not None
    remain = task.remaining_seconds()
    assert remain is not None and 500 < remain <= 600
    assert task.remaining_seconds(datetime.now() + timedelta(hours=1)) is not None
    assert store.get_task(store.add_task("无期限")) is not None
    store.close()


# ---------------------------------------------------------------- 统计与图表数据
def test_task_counts() -> None:
    store = _store()
    now = datetime.now()
    store.add_task("超期未完成", now - timedelta(hours=3))
    store.add_task("今天到期", now + timedelta(hours=2))
    store.add_task("无期限")
    done_id = store.add_task("已完成", now + timedelta(hours=1))
    store.complete_task(done_id, True)

    counts = store.task_counts()
    assert counts.total == 4
    assert counts.pending == 3
    assert counts.completed == 1
    assert counts.overdue == 1
    # “今天到期”= 未完成且截止日期是今天（超期那条也算今天到期；已完成的不算）
    assert counts.due_today == 2
    summary = counts.summary_text()
    assert "待办 3" in summary and "已超期 1" in summary
    store.close()


def test_task_stats_by_day_buckets() -> None:
    """近 7 天统计：按时完成 / 超期完成 / 逾期未完成 三类分桶。"""
    store = _store()
    now = datetime.now()
    yesterday = now - timedelta(days=1)

    # 直接写库，构造“昨天创建”的历史数据
    def insert(title: str, created: datetime, due: datetime | None,
               completed: bool, completed_at: datetime | None) -> None:
        store._conn.execute(  # noqa: SLF001 - 测试需要构造历史数据
            "INSERT INTO tasks(title, due_at, created_at, completed, completed_at) VALUES(?,?,?,?,?)",
            (title, _ts(due) if due else None, _ts(created),
             1 if completed else 0, _ts(completed_at) if completed_at else None),
        )
        store._conn.commit()

    # 昨天：1 个按时完成 + 1 个超期完成 + 1 个逾期未完成
    insert("昨天按时", yesterday, yesterday + timedelta(hours=4), True, yesterday + timedelta(hours=2))
    insert("昨天超期完成", yesterday, yesterday + timedelta(hours=1), True, yesterday + timedelta(hours=6))
    insert("昨天逾期", yesterday, yesterday + timedelta(hours=1), False, None)
    # 今天：1 个未完成但还没到期（不计入任何失败桶）
    insert("今天还早", now, now + timedelta(hours=6), False, None)

    stats = store.task_stats_by_day(7)
    assert len(stats) == 7
    day_str = yesterday.strftime("%Y-%m-%d")
    bucket = next(s for s in stats if s.day == day_str)
    assert (bucket.on_time, bucket.late, bucket.overdue) == (1, 1, 1)
    assert bucket.total == 3

    overview = store.task_overview(7)
    assert overview["on_time"] == 1
    assert overview["late"] == 1
    assert overview["overdue"] >= 1
    assert overview["total"] == 4
    store.close()


def test_mark_tasks_reminded() -> None:
    store = _store()
    task_id = store.add_task("提醒计数", datetime.now() + timedelta(hours=1))
    store.mark_tasks_reminded([task_id])
    store.mark_tasks_reminded([task_id])
    task = store.get_task(task_id)
    assert task is not None and task.remind_count == 2
    assert task.last_remind_at is not None
    store.mark_tasks_reminded([])          # 空列表不应报错
    store.close()


# ---------------------------------------------------------------- 鼓励语
def test_phrases_library() -> None:
    assert 15 <= len(phrases.ENCOURAGEMENTS) <= 20        # 需求：内置 15~20 句
    assert len(set(phrases.ENCOURAGEMENTS)) == len(phrases.ENCOURAGEMENTS)
    for _ in range(50):
        text = phrases.pick()
        assert text in phrases.ENCOURAGEMENTS

    # exclude 生效：连续两次不会拿到同一句
    first = phrases.pick()
    for _ in range(20):
        assert phrases.pick(exclude=first) != first

    many = phrases.pick_many(3)
    assert len(many) == 3 and len(set(many)) == 3
    assert len(phrases.pick_many(999)) == len(phrases.ENCOURAGEMENTS)


def test_postpone_uses_timedelta() -> None:
    """延后逻辑（界面用）：截止时间按时长平移。"""
    store = _store()
    due = datetime.now() + timedelta(hours=1)
    task_id = store.add_task("延后测试", due)
    store.update_task(task_id, due_at=due + timedelta(minutes=60))
    task = store.get_task(task_id)
    assert task is not None and task.due_at is not None
    assert abs((task.due_at - (due + timedelta(minutes=60))).total_seconds()) < 60
    store.close()


# ---------------------------------------------------------------- 提醒文案
def test_build_reminder_text_contains_name_due_and_quote() -> None:
    store = _store()
    store.add_task("提交季度报告", datetime.now() + timedelta(days=1))
    store.add_task("预约体检", datetime.now() + timedelta(hours=4))
    tasks = store.pending_tasks()

    text = build_reminder_text(tasks)
    assert "2 条待办" in text
    assert "提交季度报告" in text
    assert "截止" in text
    # 鼓励语必须来自内置库
    assert any(line in text for line in phrases.ENCOURAGEMENTS)

    # 超过 3 条时折叠显示
    for index in range(4):
        store.add_task(f"任务{index}", datetime.now() + timedelta(hours=index + 1))
    text_many = build_reminder_text(store.pending_tasks())
    assert "还有" in text_many
    assert build_reminder_text([]) == ""
    store.close()


# ---------------------------------------------------------------- 日期选择器
def test_quick_datetime_presets() -> None:
    base = datetime(2026, 10, 6, 14, 30, 0)
    assert quick_datetime("1 小时后", base) == base + timedelta(hours=1)
    assert quick_datetime("3 小时后", base) == base + timedelta(hours=3)
    assert quick_datetime("明天此时", base) == base + timedelta(days=1)
    assert quick_datetime("今晚 20:00", base) == base.replace(hour=20, minute=0)
    # 已经过了 20:00 -> 顺延到明天 20:00
    late = datetime(2026, 10, 6, 21, 15, 0)
    assert quick_datetime("今晚 20:00", late) == datetime(2026, 10, 7, 20, 0)
    assert quick_datetime("明早 09:00", base) == datetime(2026, 10, 7, 9, 0)
    # 周末（周日 20:00）：2026-10-06 是周二 -> 10-11 周日
    assert quick_datetime("本周末", base) == datetime(2026, 10, 11, 20, 0)
    # 未知文案退化为 1 小时后
    assert quick_datetime("不存在的选项", base) == base + timedelta(hours=1)


# ---------------------------------------------------------------- 系统通知（临近截止 + 间隔）
def test_notification_window_and_interval() -> None:
    """系统通知只挑“临近截止”的任务，并受间隔与阈值变化影响。"""
    store = _store()
    now = datetime.now()
    soon = store.add_task("3 小时后到期", now + timedelta(hours=3))
    far = store.add_task("5 天后到期", now + timedelta(days=5))
    over = store.add_task("已超期 2 小时", now - timedelta(hours=2))
    store.add_task("无期限任务")
    done = store.add_task("已完成", now + timedelta(hours=1))
    store.complete_task(done, True)

    # 默认提前 24 小时：只有“临近”的会被选中（含已超期）
    picked = {t.title for t in store.tasks_due_for_notification(24, 6)}
    assert picked == {"3 小时后到期", "已超期 2 小时"}

    # 提前窗口缩小到 1 小时：只剩已超期的那条
    assert {t.title for t in store.tasks_due_for_notification(1, 6)} == {"已超期 2 小时"}

    # 刚通知过 -> 间隔内不再重复
    store.mark_tasks_notified([soon, over], 24)
    assert [t.title for t in store.tasks_due_for_notification(24, 6)] == []

    # 阈值被调大（24 -> 48）-> 重新提醒一次
    assert {t.title for t in store.tasks_due_for_notification(48, 6)} == {"3 小时后到期", "已超期 2 小时"}

    # 把通知时间往前挪 7 小时 -> 超过默认 6 小时间隔，可以再次提醒
    old = (now - timedelta(hours=7)).strftime("%Y-%m-%d %H:%M:%S")
    store._conn.execute("UPDATE tasks SET notified_at = ? WHERE id IN (?, ?)", (old, soon, over))  # noqa: SLF001
    store._conn.commit()  # noqa: SLF001
    assert {t.title for t in store.tasks_due_for_notification(24, 6)} == {"3 小时后到期", "已超期 2 小时"}

    # 很久以后才到期的任务永远不进入通知窗口
    assert far not in {t.id for t in store.tasks_due_for_notification(24, 6)}
    store.close()


def test_mark_tasks_notified_only_touches_given_ids() -> None:
    """只标记真正被通知到的任务：第 4 条之后的任务不该被静默。"""
    store = _store()
    now = datetime.now()
    ids = [store.add_task(f"任务{index}", now + timedelta(hours=index + 1)) for index in range(5)]
    urgent = store.tasks_due_for_notification(24, 6)
    assert len(urgent) == 5

    shown = urgent[:3]                      # 通知里只列前 3 条
    store.mark_tasks_notified([t.id for t in shown], 24)

    again = {t.id for t in store.tasks_due_for_notification(24, 6)}
    assert again == set(ids[3:]), "只有被通知过的任务才应进入间隔冷却"
    store.close()


def test_task_from_row_survives_missing_notify_columns() -> None:
    """老库（没有 notified_at 列）构造 Task 时不应崩溃。"""
    store = _store()
    task_id = store.add_task("老数据", datetime.now() + timedelta(hours=2))
    row = store._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()  # noqa: SLF001
    keys = list(row.keys())
    assert "notified_at" in keys and "notified_within_hours" in keys
    task = store.get_task(task_id)
    assert task is not None and task.notified_at is None
    assert task.notified_within_hours is None
    store.close()


def test_legacy_database_migration_adds_notify_columns() -> None:
    """模拟 v1.1 的老库：tasks 表没有新列，打开时应自动补列且不丢数据。"""
    import sqlite3

    base = Path(tempfile.mkdtemp(prefix="tg_legacy_"))
    db_path = base / "usage.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, due_at TEXT,
            created_at TEXT NOT NULL, completed INTEGER NOT NULL DEFAULT 0, completed_at TEXT,
            priority INTEGER NOT NULL DEFAULT 1, remind_count INTEGER NOT NULL DEFAULT 0,
            last_remind_at TEXT, note TEXT
        );
        CREATE TABLE daily_target (
            day TEXT, target_name TEXT, target_kind TEXT, seconds REAL, updated_at TEXT,
            PRIMARY KEY (day, target_name)
        );
        INSERT INTO tasks(title, created_at, due_at)
            VALUES('老库里的任务', '2026-10-01 10:00:00', '2026-10-01 18:00:00');
        INSERT INTO daily_target VALUES('2026-10-01', 'game.exe', 'game', 3600, '2026-10-01 12:00:00');
        """
    )
    conn.commit()
    conn.close()

    store = UsageStore(db_path)                       # 触发迁移
    columns = [row["name"] for row in store._conn.execute("PRAGMA table_info(tasks)")]  # noqa: SLF001
    assert "notified_at" in columns and "notified_within_hours" in columns
    indexes = [row["name"] for row in store._conn.execute("PRAGMA index_list('tasks')")]  # noqa: SLF001
    assert "idx_tasks_notify" in indexes

    # 老数据（任务 + 时长）都必须原样保留
    tasks = store.list_tasks("all")
    assert len(tasks) == 1 and tasks[0].title == "老库里的任务"
    assert round(store.total_seconds("2026-10-01"), 1) == 3600.0

    # 迁移后新功能可用：新任务会进入通知窗口
    # （老库那条任务的截止时间是 2026-10-01、且从未通知过，属于“已超期未提醒”，
    #   一起被选中是正确行为，这里把这点也断言上）
    new_id = store.add_task("迁移后新增", datetime.now() + timedelta(hours=2))
    assert new_id is not None
    picked = {t.id for t in store.tasks_due_for_notification(24, 6)}
    assert new_id in picked
    assert len(picked) == 2, f"应同时提醒老库超期任务与新任务，实际 {picked}"
    store.close()


# ---------------------------------------------------------------- 开机自启
def test_build_command_points_to_project() -> None:
    command = autostart.build_command()
    assert command
    assert "python" in command.lower() or command.lower().endswith(".exe")
    if os.name == "nt" and not getattr(sys, "frozen", False):
        assert "run.pyw" in command or "-m timeguard" in command


def test_status_text() -> None:
    text = autostart.status_text()
    assert text
    if not autostart.is_supported():
        assert "Windows" in text


def test_enable_disable_roundtrip_preserves_previous_value() -> None:
    """真实读写注册表；运行前后会恢复原值，避免影响用户设置。"""
    if not autostart.is_supported():
        return
    original = autostart.current_command()
    try:
        assert autostart.enable() is True
        assert autostart.is_enabled() is True
        assert autostart.current_command() == autostart.build_command()
        # 重复开启应当幂等
        assert autostart.apply(True) is True

        assert autostart.disable() is True
        assert autostart.is_enabled() is False
        # 再删一次也应成功
        assert autostart.disable() is True
    finally:
        if original:
            autostart.enable(original)
        else:
            autostart.disable()


# ---------------------------------------------------------------- 列表分组（阶段四）
def _fixed_now() -> datetime:
    """用一个固定的周三 12:00 做基准，分组的边界才可复现。"""
    return datetime(2026, 10, 7, 12, 0)      # 2026-10-07 是周三


def test_build_task_rows_groups_single_tasks_by_due_date() -> None:
    """单次任务按截止时间落到 今日 / 本周 / 其他 三组。"""
    from timeguard.tasks import GROUP_OTHER, GROUP_TODAY, GROUP_WEEK, build_task_rows

    store = _store()
    now = _fixed_now()
    store.add_task("今天到期", now + timedelta(hours=3))
    store.add_task("已经超期", now - timedelta(hours=5))
    store.add_task("三天后", now + timedelta(days=3))
    store.add_task("三十天后", now + timedelta(days=30))
    store.add_task("无期限", None)

    groups = build_task_rows(store, "pending", now)
    today = {row.task.title for row in groups[GROUP_TODAY]}
    week = {row.task.title for row in groups[GROUP_WEEK]}
    other = {row.task.title for row in groups[GROUP_OTHER]}

    assert today == {"今天到期", "已经超期"}, today
    assert week == {"三天后"}, week
    assert other == {"三十天后", "无期限"}, other
    # 行文案：超期那条必须是红色标签 + 已超期状态
    overdue_row = next(row for row in groups[GROUP_TODAY] if row.task.title == "已经超期")
    assert overdue_row.overdue is True and "已超期" in overdue_row.status_text
    assert overdue_row.tags() == ("overdue",)
    store.close()


def test_build_task_rows_recurring_today_and_this_week() -> None:
    """周期任务：今天排班的进"今日"，今天不排班但 7 天内排班的进"本周"。"""
    from timeguard.tasks import GROUP_TODAY, GROUP_WEEK, build_task_rows

    store = _store()
    now = _fixed_now()                       # 周三
    # start_date=""：本用例按固定日期（2026-10-07 周三）推演排班，
    # 不能受"今天才建、时间已过 → 从明天开始"的自动生效日影响
    store.add_task("每天早上跑步", task_type="daily", time_start="06:00", time_end="08:00",
                   start_date="")
    store.add_task("周三周五健身", task_type="weekly", days_of_week="3,5",
                   time_start="18:00", time_end="20:00", start_date="")
    store.add_task("周日复盘", task_type="weekly", days_of_week="7",
                   time_start="20:00", time_end="21:00", start_date="")

    groups = build_task_rows(store, "pending", now)
    today = {row.task.title for row in groups[GROUP_TODAY]}
    week = {row.task.title for row in groups[GROUP_WEEK]}

    assert today == {"每天早上跑步", "周三周五健身"}, today
    # 周日那条今天不排班 → 本周；"周三周五"今天已排班，不能重复出现在本周
    assert week == {"周日复盘"}, week
    weekly_row = next(row for row in groups[GROUP_TODAY] if row.task.title == "周三周五健身")
    assert weekly_row.is_recurring and "18:00~20:00" in weekly_row.schedule_text
    store.close()


def test_build_task_rows_hides_checked_in_occurrence_in_pending() -> None:
    """已打卡的那一次在"未完成"里消失，在"已完成/全部"里出现（周期任务不写 tasks.completed）。"""
    from timeguard.tasks import GROUP_TODAY, build_task_rows

    store = _store()
    now = _fixed_now()
    task_id = store.add_task("每天喝水", task_type="daily", time_start="06:00", time_end="08:00",
                             start_date="")
    assert task_id is not None

    pending = build_task_rows(store, "pending", now)[GROUP_TODAY]
    assert [row.task.title for row in pending] == ["每天喝水"]
    assert pending[0].done is False

    assert store.complete_occurrence(task_id, now.date(), completed=True) is True
    assert build_task_rows(store, "pending", now)[GROUP_TODAY] == []
    completed = build_task_rows(store, "completed", now)[GROUP_TODAY]
    assert [row.task.title for row in completed] == ["每天喝水"]
    assert completed[0].done is True and "已打卡" in completed[0].status_text
    everything = build_task_rows(store, "all", now)[GROUP_TODAY]
    assert len(everything) == 1 and everything[0].done is True

    # 关键不变式：打卡只写 task_logs，规则本身不能被标记成"永久完成"
    assert store.get_task(task_id).completed is False
    store.close()


def test_recurring_countdown_states() -> None:
    """周期任务的倒计时文案：未开始 / 进行中 / 已超期 / 已完成（迟到）。"""
    from timeguard.tasks import periodic_countdown

    store = _store()
    now = _fixed_now()
    task_id = store.add_task("晚间阅读", task_type="daily", time_start="20:00", time_end="21:00",
                             start_date="")   # 按固定日期推演，绕开自动生效日
    task = store.get_task(task_id)
    assert task is not None
    window = task.occurrence_on(now.date())
    assert window is not None

    text, overdue, _late = periodic_countdown(task, window, None, now)
    assert text.startswith("还剩") and overdue is False
    text, overdue, _late = periodic_countdown(task, window, None, now.replace(hour=20, minute=30))
    assert "进行中" in text and overdue is False
    text, overdue, _late = periodic_countdown(task, window, None, now.replace(hour=22, minute=0))
    assert text.startswith("已超期") and overdue is True

    # 迟到打卡：完成时间晚于窗口结束
    store.log_occurrence(task_id, now.date(), completed=True, was_late=True,
                         completed_at=now.replace(hour=22, minute=30))
    log_row = store.get_task_log(task_id, now.date())
    text, overdue, late = periodic_countdown(task, window, log_row, now.replace(hour=23, minute=0))
    assert "已打卡" in text and "迟到" in text and late is True and overdue is False
    store.close()


def test_add_recurring_task_does_not_store_due_at() -> None:
    """周期任务不该带 due_at（否则单次任务的口径与图表都会被污染）。"""
    store = _store()
    task_id = store.add_task("每天跑步", datetime.now() + timedelta(hours=1),
                             task_type="daily", time_start="06:00", time_end="08:00")
    task = store.get_task(task_id)
    assert task is not None
    assert task.due_at is None
    assert task.is_recurring is True and task.rule.time_start.hour == 6
    # 单次任务反过来不该保留周期字段
    single_id = store.add_task("交报告", datetime.now() + timedelta(hours=2),
                              task_type="once", time_start="06:00", time_end="08:00")
    single = store.get_task(single_id)
    assert single is not None and single.is_recurring is False
    row = store._conn.execute("SELECT time_start, days_of_week FROM tasks WHERE id = ?",  # noqa: SLF001
                              (single_id,)).fetchone()
    assert row["time_start"] is None and row["days_of_week"] is None
    store.close()


def test_task_draft_preview_matches_saved_rule() -> None:
    """对话框预览的文本必须与真正入库后的规则一致（避免"预览骗人"）。"""
    from timeguard.tasks import TaskDraft

    now = _fixed_now()
    draft = TaskDraft(title="健身", task_type="weekly", days_of_week="3,5",
                      time_start="18:00", time_end="20:00", remind_before_minutes=30)
    assert draft.is_recurring is True and draft.type_label == "每周"
    text = draft.preview_text(now)
    assert "每周任务" in text and "周三、周五 18:00~20:00" in text
    assert "开始前 30 分钟提醒" in text
    # 周三 12:00 的下一次提醒 = 周三 17:30
    assert now.strftime("%m-%d") in text

    single = TaskDraft(title="交报告", due_at=now + timedelta(hours=2))
    assert single.is_recurring is False and single.rule().task_type == "once"
    assert "单次任务" in single.preview_text(now)

    untitled_weekly = TaskDraft(title="没选星期", task_type="weekly", days_of_week="")
    assert "还没选星期几" in untitled_weekly.preview_text(now)


def test_update_task_to_daily_drops_days_of_week() -> None:
    """每周 → 每天：即使调用方把旧的星期几一起传进来，也必须被丢掉。

    真实 bug：``update_task`` 先把 ``days_of_week`` 置成 NULL，紧接着又执行了
    "如果传了 days_of_week 就写进去"，于是星期几被**覆盖回来**，
    库里留下"每天任务却还写着周三周五"的脏数据。
    """
    store = _store()
    task_id = store.add_task("健身", task_type="weekly", days_of_week="3,5",
                             time_start="18:00", time_end="20:00", start_date="")
    assert store.update_task(task_id, task_type="daily", days_of_week="3,5") is True
    task = store.get_task(task_id)
    assert task is not None and task.rule.task_type == "daily"
    assert task.rule.days_of_week == (), f"星期几没被清掉: {task.rule.days_of_week}"
    # 用 startswith 而不是全等：改成"每天 18:00~20:00"之后，如果这次编辑发生在
    # 20:00 之后，库里会自动带上"（10-09 开始）"这种生效日说明 —— 那是另一条规则，
    # 这里只关心"星期几被清干净、时间段保留"。
    assert task.schedule_text.startswith("每天 18:00~20:00"), task.schedule_text
    store.close()


def test_update_task_back_to_single_clears_recurring_fields() -> None:
    """周期 → 单次：周期字段必须清干净，并且能重新设置截止时间。"""
    store = _store()
    task_id = store.add_task("健身", task_type="weekly", days_of_week="3,5",
                             time_start="18:00", time_end="20:00")
    due = _fixed_now() + timedelta(days=1)
    assert store.update_task(task_id, task_type="once", due_at=due, clear_due=False) is True
    task = store.get_task(task_id)
    assert task is not None and task.is_recurring is False
    assert task.due_at == due
    # 反向：单次 → 周期，due_at 要被清掉（面板正是这么调的）
    assert store.update_task(task_id, task_type="daily", clear_due=True,
                             time_start="06:00", time_end="08:00") is True
    task = store.get_task(task_id)
    assert task is not None and task.is_recurring is True and task.due_at is None
    store.close()


def test_export_csv_includes_recurring_rules_and_checkins() -> None:
    """CSV 导出必须把周期任务分开说：规则一行，打卡一次一行。

    升级前的错误行为：周期任务被导出成"无期限 / 未完成"，因为导出只看了
    ``tasks.due_at`` 与 ``tasks.completed`` —— 而周期任务这两列根本没有意义。
    """
    import csv as _csv

    store = _store()
    now = _fixed_now()
    # 单次任务的"状态"是按**真实当前时间**算的，所以截止时间要用真实时间往后推
    single_id = store.add_task("交周报", datetime.now() + timedelta(hours=2))
    daily_id = store.add_task("每天跑步", task_type="daily",
                              time_start="06:00", time_end="08:00", remind_before_minutes=15)
    assert store.complete_occurrence(daily_id, now.date(), completed=True) is True
    store.log_occurrence(daily_id, now.date() - timedelta(days=1), completed=False,
                         remind_at=now - timedelta(days=1), remind_kind="advance")

    target = Path(tempfile.mkdtemp(prefix="tg_csv_")) / "out.csv"
    total = store.export_csv(target, days=7)
    assert total >= 4, total

    text = target.read_text(encoding="utf-8-sig")
    rows = list(_csv.reader(text.splitlines()))
    flat = ["|".join(r) for r in rows]

    assert any("【任务定义】" in r for r in flat)
    assert any("【周期任务打卡记录】" in r for r in flat)
    # 周期任务那一行：类型/计划正确，且**不能**被写成"无期限 / 未完成"
    rule_row = next(r for r in rows if r and r[0] == str(daily_id))
    assert rule_row[2] == "每天" and "06:00~08:00" in rule_row[3], rule_row
    assert rule_row[5] == "—" and "打卡记录" in rule_row[7], rule_row
    # 单次任务仍然按原口径
    single_row = next(r for r in rows if r and r[0] == str(single_id))
    assert single_row[2] == "单次" and single_row[5] == "否" and single_row[7] == "进行中", single_row
    # 打卡记录：今天已打卡 + 昨天提醒过但没打卡
    log_rows = [r for r in rows if len(r) >= 8 and r[0].count("-") == 2]
    assert len(log_rows) == 2, log_rows
    today_row = next(r for r in log_rows if r[0] == now.strftime("%Y-%m-%d"))
    assert today_row[1] == "每天跑步" and today_row[3] == "是", today_row
    yesterday_row = next(r for r in log_rows if r[0] != now.strftime("%Y-%m-%d"))
    assert yesterday_row[3] == "否" and yesterday_row[7] == "提前提醒", yesterday_row
    store.close()


# ---------------------------------------------------------------- 兼容性
def test_time_monitor_tables_survive_upgrade() -> None:
    """新增 tasks 表不能破坏原有的时长监控数据表。"""
    store = _store()
    store.add_seconds("game.exe", 120, kind="game")
    store.add_seconds("哔哩哔哩", 60, kind="web")
    # 模拟“老库升级”：重新打开（会再次执行 CREATE TABLE IF NOT EXISTS）
    path = store.path
    store.close()
    reopened = UsageStore(path)
    assert round(reopened.total_seconds(), 1) == 180.0
    assert reopened.get_task(1) is None           # tasks 表可查询（空表）
    assert reopened.add_task("升级后的新任务") is not None
    targets = {t.name for t in reopened.today_targets()}
    assert {"game.exe", "哔哩哔哩"} <= targets
    reopened.close()


# ---------------------------------------------------------------- 简易执行器
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

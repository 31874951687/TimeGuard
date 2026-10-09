"""SQLite 数据层：使用记录 + 待办任务。

设计要点
--------
1. 表结构：
   * ``daily_target`` —— 每日 × 每个监控对象的累计秒数（时长图表取这张表）。
   * ``tick_log``     —— 时长明细流水，便于日后扩展分析（默认保留 30 天）。
   * ``hourly``       —— 小时级时长分布。
   * ``meta``         —— 键值杂项。
   * ``tasks``        —— 待办任务（任务内容 / 截止时间 / 创建时间 / 是否完成 …）。
2. 所有写操作通过一把线程锁串行化；引擎线程和界面线程共用一个连接
   （``check_same_thread=False``），开启 WAL 提升并发读性能。
3. 计时以“增量 flush”方式写入：引擎每几秒把本轮的秒数累加进当天记录，
   因此即使程序被强杀，最多损失几秒数据。
4. 建表全部使用 ``CREATE TABLE IF NOT EXISTS``，老版本数据库打开时会自动补表，
   不会影响既有的时长记录。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from . import paths
from .recurrence import (
    DEFAULT_AUTO_MINUTES,
    DEFAULT_END,
    DEFAULT_REMIND_BEFORE,
    DEFAULT_REMIND_TIME,
    DEFAULT_START,
    DEFAULT_TARGET_COUNT,
    MAX_AUTO_MINUTES,
    MAX_REMIND_BEFORE,
    MAX_TARGET_COUNT,
    MIN_AUTO_MINUTES,
    MIN_REMIND_BEFORE,
    TYPE_CUMULATIVE,
    TYPE_DAILY,
    TYPE_ONCE,
    TYPE_WEEKLY,
    TASK_TYPES,
    CumulativeStatus,
    TaskRule,
    cumulative_status,
    format_hhmm,
    format_iso_weekdays,
    parse_hhmm,
    parse_iso_weekdays,
    suggested_start_date,
)
from .utils import last_n_days, now_str, today_str

log = logging.getLogger(__name__)

#: 任务相关的时间字符串格式（精确到秒，界面只用到分钟）
TS_FMT = "%Y-%m-%d %H:%M:%S"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_target (
    day         TEXT NOT NULL,
    target_name TEXT NOT NULL,
    target_kind TEXT NOT NULL DEFAULT 'process',
    seconds     REAL NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (day, target_name)
);

CREATE TABLE IF NOT EXISTS tick_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    day         TEXT NOT NULL,
    ts          TEXT NOT NULL,
    target_name TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    seconds     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tick_day ON tick_log(day);

CREATE TABLE IF NOT EXISTS hourly (
    day         TEXT NOT NULL,
    hour        INTEGER NOT NULL,
    seconds     REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, hour)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- ============================ 待办任务 ============================
-- tasks 只存【规则】：这个任务是什么、什么时候该做
CREATE TABLE IF NOT EXISTS tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,  -- 任务 ID
    title         TEXT    NOT NULL,                   -- 任务内容
    due_at        TEXT,                               -- 截止时间 'YYYY-MM-DD HH:MM:SS'（可空=无期限；仅单次任务使用）
    created_at    TEXT    NOT NULL,                   -- 创建时间
    completed     INTEGER NOT NULL DEFAULT 0,         -- 是否完成：0 未完成 / 1 已完成（仅单次任务使用）
    completed_at  TEXT,                               -- 完成时间（为空表示未完成）
    priority      INTEGER NOT NULL DEFAULT 1,         -- 1 普通 / 2 重要
    remind_count  INTEGER NOT NULL DEFAULT 0,         -- 被开机提醒过几次
    last_remind_at TEXT,                              -- 最近一次提醒时间
    notified_at   TEXT,                               -- 最近一次发 Windows 通知的时间（间隔控制用）
    notified_within_hours REAL,                       -- 发通知时用的“临近截止”阈值（阈值改了要重新提醒）
    note          TEXT,                               -- 备注（预留）
    -- ↓ v1.3 周期性任务：规则字段（老库由 _TASK_COLUMN_MIGRATIONS 补列）
    task_type              TEXT    NOT NULL DEFAULT 'once',  -- once 单次 / daily 每天 / weekly 每周
    time_start             TEXT,                             -- 周期任务时间窗开始 'HH:MM'
    time_end               TEXT,                             -- 周期任务时间窗结束 'HH:MM'
    days_of_week           TEXT,                             -- 每周任务：ISO 星期，如 '1,3,5'（1=周一）
    remind_before_minutes  INTEGER NOT NULL DEFAULT 15,      -- 提前多少分钟提醒
    start_date             TEXT,                             -- 生效起始日 'YYYY-MM-DD'（空=一直有效）
    -- ↓ v1.5 累计打卡任务（例如"年末前完成 60 次两公里跑"）
    target_count           INTEGER,                          -- 总目标次数，如 60
    deadline               TEXT,                             -- 最终截止日期 'YYYY-MM-DD'（含当天）
    remind_time            TEXT,                             -- 每日提醒时间 'HH:MM'
    auto_target            TEXT,                             -- 【可选】监控对象名（达标自动打卡）
    auto_minutes           INTEGER                           -- 【可选】自动打卡的时长阈值（分钟）
);
CREATE INDEX IF NOT EXISTS idx_tasks_completed ON tasks(completed);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(due_at);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);

-- ============================ 周期任务的发生记录 ============================
-- task_logs 存【发生记录】：这个任务哪一天做了没有、提醒过没有
-- 周期任务的状态只认这张表，绝不写 tasks.completed（否则"今天完成"会变成"永久完成"）
CREATE TABLE IF NOT EXISTS task_logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      INTEGER NOT NULL,                    -- 对应 tasks.id
    occur_date   TEXT    NOT NULL,                    -- 这一次发生在哪一天 'YYYY-MM-DD'
    completed    INTEGER NOT NULL DEFAULT 0,          -- 该次是否已打卡完成
    completed_at TEXT,                                -- 打卡时间
    was_late     INTEGER NOT NULL DEFAULT 0,          -- 是否迟到完成（超过 time_end）
    remind_at    TEXT,                                -- 本次提前提醒实际发出时间（为空=还没提醒）
    remind_kind  TEXT,                                -- advance 正常提前 / late 开机补发
    note         TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    UNIQUE (task_id, occur_date)                      -- 数据库级防重复打卡 / 重复提醒
);
CREATE INDEX IF NOT EXISTS idx_task_logs_date ON task_logs(occur_date);
CREATE INDEX IF NOT EXISTS idx_task_logs_task ON task_logs(task_id, occur_date);

-- ============================ 累计打卡任务的打卡历史 ============================
-- 一条记录 = 某一天打的一次卡。这是画进度条与热力图**唯一**的数据来源
-- （只存一个 current_count 字段是不够的：图表需要知道你是哪一天打的卡）。
--
-- UNIQUE(task_id, checkin_date) 是"同一天只能打一次卡"的数据库级保证：
-- 界面上多点两下、逻辑里算错一次，都写不进第二条 —— 与 task_logs 同一思路。
CREATE TABLE IF NOT EXISTS check_in_logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      INTEGER NOT NULL,                     -- 对应 tasks.id
    checkin_date TEXT    NOT NULL,                     -- 打卡归属的自然日 'YYYY-MM-DD'（本地时区）
    checkin_at   TEXT    NOT NULL,                     -- 实际打卡时刻（精确到秒，用于展示/审计）
    note         TEXT,                                 -- 备注
    source       TEXT    NOT NULL DEFAULT 'manual',    -- manual 手动 / backfill 补签（预留）
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    UNIQUE (task_id, checkin_date)
);
CREATE INDEX IF NOT EXISTS idx_checkin_task_date ON check_in_logs(task_id, checkin_date);
CREATE INDEX IF NOT EXISTS idx_checkin_date ON check_in_logs(checkin_date);
"""

#: 老库升级用：v1.1 之后的 tasks 表新增列（``CREATE TABLE IF NOT EXISTS`` 不会补列，需要 ALTER）
#: 注意：SQLite 的 ADD COLUMN 只能用常量默认值（这里的默认值都合法）
_TASK_COLUMN_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("notified_at", "TEXT"),
    ("notified_within_hours", "REAL"),
    # v1.3 周期任务
    ("task_type", "TEXT NOT NULL DEFAULT 'once'"),
    ("time_start", "TEXT"),
    ("time_end", "TEXT"),
    ("days_of_week", "TEXT"),
    ("remind_before_minutes", "INTEGER NOT NULL DEFAULT 15"),
    # v1.4：生效起始日（"晚上才建的每日任务"从明天开始算，不再立刻显示逾期）
    ("start_date", "TEXT"),
    # v1.5 累计打卡任务
    ("target_count", "INTEGER"),
    ("deadline", "TEXT"),
    ("remind_time", "TEXT"),
    # v1.5.1 监控时长达标自动打卡（可选）
    ("auto_target", "TEXT"),
    ("auto_minutes", "INTEGER"),
)

#: 依赖新增列的索引：必须在补列之后再建，否则老库打开时会报 no such column
_TASK_INDEX_MIGRATIONS: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_tasks_notify ON tasks(notified_at)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_type ON tasks(task_type)",
)


@dataclass(frozen=True)
class TargetStat:
    """单个监控对象的累计统计。"""

    name: str
    kind: str
    seconds: float


@dataclass(frozen=True)
class DayStat:
    """某一天的总计。"""

    day: str
    seconds: float


# ---------------------------------------------------------------- 待办任务模型
@dataclass
class Task:
    """一条待办任务（tasks 表的一行 = 一条【规则】）。"""

    id: int
    title: str
    due_at: datetime | None
    created_at: datetime
    completed: bool
    completed_at: datetime | None
    priority: int = 1
    remind_count: int = 0
    last_remind_at: datetime | None = None
    notified_at: datetime | None = None
    notified_within_hours: float | None = None
    note: str = ""
    #: 周期规则（单次任务为 ``TaskRule(TYPE_ONCE)``，其字段无意义）
    rule: TaskRule = field(default_factory=TaskRule)

    # ---- 展示辅助 ----
    @property
    def priority_cn(self) -> str:
        """优先级中文名。"""
        return "重要" if self.priority >= 2 else "普通"

    def remaining_seconds(self, now: datetime | None = None) -> float | None:
        """距离截止时间还剩多少秒（无期限返回 None，已超期为负数）。"""
        if self.due_at is None:
            return None
        return (self.due_at - (now or datetime.now())).total_seconds()

    def is_overdue(self, now: datetime | None = None) -> bool:
        """未完成且已过截止时间。"""
        if self.completed or self.due_at is None:
            return False
        return (now or datetime.now()) > self.due_at

    def was_late(self) -> bool:
        """已完成，但完成时间晚于截止时间。"""
        if not self.completed or self.completed_at is None or self.due_at is None:
            return False
        return self.completed_at > self.due_at

    def countdown_text(self, now: datetime | None = None) -> str:
        """状态列文案：``还剩 2小时15分`` / ``已超期 3小时`` / ``已完成`` / ``无期限``。"""
        from .utils import fmt_duration  # 局部导入，避免模块级循环依赖

        now = now or datetime.now()
        if self.completed:
            if self.was_late():
                late = fmt_duration((self.completed_at - self.due_at).total_seconds(), with_seconds=False)  # type: ignore[operator]
                return f"已完成（超期 {late}）"
            return "已完成"
        if self.due_at is None:
            return "无期限"
        remain = (self.due_at - now).total_seconds()
        if remain >= 0:
            return f"还剩 {fmt_duration(remain, with_seconds=False)}"
        return f"已超期 {fmt_duration(-remain, with_seconds=False)}"

    def due_text(self) -> str:
        """截止时间展示文案。"""
        if self.due_at is None:
            return "无期限"
        today = datetime.now().date()
        prefix = ""
        if self.due_at.date() == today:
            prefix = "今天 "
        elif self.due_at.date() == today + timedelta(days=1):
            prefix = "明天 "
        elif self.due_at.date() == today - timedelta(days=1):
            prefix = "昨天 "
        return f"{prefix}{self.due_at:%Y-%m-%d %H:%M}"

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Task":
        """从数据库行构造任务对象。"""

        def parse(value: str | None) -> datetime | None:
            if not value:
                return None
            for fmt in (TS_FMT, "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    return datetime.strptime(value, fmt)
                except ValueError:
                    continue
            return None

        return cls(
            id=int(row["id"]),
            title=row["title"] or "",
            due_at=parse(row["due_at"]),
            created_at=parse(row["created_at"]) or datetime.now(),
            completed=bool(row["completed"]),
            completed_at=parse(row["completed_at"]),
            priority=int(row["priority"] or 1),
            remind_count=int(row["remind_count"] or 0),
            last_remind_at=parse(row["last_remind_at"]),
            notified_at=parse(_row_get(row, "notified_at")),
            notified_within_hours=_row_get(row, "notified_within_hours"),
            note=row["note"] or "",
            rule=TaskRule.from_row(row),
        )

    # ------------------------------------------------------------------ 周期任务
    @property
    def is_recurring(self) -> bool:
        """是否为周期任务（每天 / 每周）。"""
        return self.rule.is_recurring

    @property
    def type_label(self) -> str:
        """任务类型中文名：单次 / 每天 / 每周。"""
        return self.rule.type_label

    @property
    def schedule_text(self) -> str:
        """给界面用的一句话计划描述。

        注意这里必须返回**字符串**：以前非周期分支返回的是 ``self.due_text``
        （一个绑定方法对象），一旦被塞进 f-string，界面上就会出现
        ``<bound method Task.due_text of ...>`` 这种东西。
        """
        if self.is_recurring or self.is_cumulative:
            return self.rule.schedule_text()
        return self.due_text()

    def occurrence_on(self, day: date):
        """``day`` 这一天的发生窗口（单次任务或不在计划内返回 ``None``）。"""
        return self.rule.window_on(day) if self.is_recurring else None

    @property
    def is_cumulative(self) -> bool:
        """是否为累计打卡任务。"""
        return self.rule.is_cumulative


def _coerce_auto(auto_target, auto_minutes) -> tuple[str | None, int | None]:
    """归一化"自动打卡"的两个字段：没填监控对象就等于没开这个功能。

    只填了对象没填分钟数 → 用默认值（30 分钟）；分钟数越界 → 夹到合法范围。
    """
    name = (str(auto_target).strip() if auto_target else "")
    if not name:
        return None, None
    try:
        minutes = int(auto_minutes) if auto_minutes not in (None, "") else DEFAULT_AUTO_MINUTES
    except (TypeError, ValueError):
        minutes = DEFAULT_AUTO_MINUTES
    return name[:100], max(MIN_AUTO_MINUTES, min(MAX_AUTO_MINUTES, minutes))


def _coerce_date(raw) -> date | None:
    """把 ``"2026-12-31"`` / ``date`` / ``datetime`` 统一成 ``date``（失败返回 None）。"""
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    try:
        return datetime.strptime(str(raw).strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


@dataclass(frozen=True)
class CheckInLog:
    """累计打卡任务的一次打卡（check_in_logs 表的一行）。"""

    id: int
    task_id: int
    checkin_date: str                  # 'YYYY-MM-DD'（自然日，本地时区）
    checkin_at: datetime | None = None  # 实际打卡时刻
    note: str = ""
    source: str = "manual"             # manual 手动 / backfill 补签

    @property
    def day(self) -> date:
        return datetime.strptime(self.checkin_date[:10], "%Y-%m-%d").date()

    @property
    def time_text(self) -> str:
        return self.checkin_at.strftime("%H:%M:%S") if self.checkin_at else ""

    @property
    def source_label(self) -> str:
        return {"manual": "手动打卡", "backfill": "补签"}.get(self.source, self.source)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "CheckInLog":
        return cls(
            id=int(row["id"]),
            task_id=int(row["task_id"]),
            checkin_date=str(row["checkin_date"]),
            checkin_at=_parse_ts(_row_get(row, "checkin_at")),
            note=_row_get(row, "note") or "",
            source=_row_get(row, "source") or "manual",
        )


@dataclass(frozen=True)
class TaskLog:
    """周期任务某一次发生的记录（task_logs 表的一行）。"""
    id: int
    task_id: int
    occur_date: str            # 'YYYY-MM-DD'
    completed: bool = False
    completed_at: datetime | None = None
    was_late: bool = False
    remind_at: datetime | None = None
    remind_kind: str = ""      # advance 正常提前 / late 开机补发
    note: str = ""

    @property
    def kind_label(self) -> str:
        return {"advance": "提前提醒", "late": "迟到补发"}.get(self.remind_kind, "")

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "TaskLog":
        def parse(value: str | None) -> datetime | None:
            if not value:
                return None
            for fmt in (TS_FMT, "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    return datetime.strptime(value, fmt)
                except ValueError:
                    continue
            return None

        return cls(
            id=int(row["id"]),
            task_id=int(row["task_id"]),
            occur_date=str(row["occur_date"]),
            completed=bool(row["completed"]),
            completed_at=parse(_row_get(row, "completed_at")),
            was_late=bool(_row_get(row, "was_late") or 0),
            remind_at=parse(_row_get(row, "remind_at")),
            remind_kind=str(_row_get(row, "remind_kind") or ""),
            note=str(_row_get(row, "note") or ""),
        )


@dataclass(frozen=True)
class TaskDayStat:
    """某一天的任务完成情况（用于 7 天柱状图）。"""

    day: str
    on_time: int = 0      # 按时完成
    late: int = 0         # 超期完成
    overdue: int = 0      # 已超期且仍未完成

    @property
    def total(self) -> int:
        return self.on_time + self.late + self.overdue


@dataclass(frozen=True)
class TaskCounts:
    """任务总数统计。"""

    total: int = 0
    pending: int = 0
    completed: int = 0
    overdue: int = 0
    due_today: int = 0

    def summary_text(self) -> str:
        """一行摘要，供界面/托盘展示。"""
        parts = [f"共 {self.total} 条", f"待办 {self.pending}"]
        if self.overdue:
            parts.append(f"已超期 {self.overdue}")
        if self.due_today:
            parts.append(f"今天到期 {self.due_today}")
        parts.append(f"已完成 {self.completed}")
        return "　·　".join(parts)


def _row_get(row: sqlite3.Row, key: str, default=None):
    """安全读取行里的某列：老库可能还没有这一列（迁移前就构造对象时）。"""
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def _parse_ts(value: str | None) -> datetime | None:
    """解析数据库里的时间字符串（容忍 ``YYYY-MM-DD HH:MM`` 与纯日期）。"""
    if not value:
        return None
    for fmt in (TS_FMT, "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


class UsageStore:
    """使用记录存储（SQLite）。"""

    RETENTION_DAYS = 30  # 明细保留天数

    def __init__(self, db_path: Path | None = None) -> None:
        self.path = Path(db_path) if db_path else paths.database_file()
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    # ------------------------------------------------------------------ 初始化
    def _init_schema(self) -> None:
        with self._lock:
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.Error as exc:  # pragma: no cover - 磁盘异常
                log.warning("设置数据库 PRAGMA 失败: %s", exc)

            # 建表：即使某条语句失败也要继续往下走，让 _migrate_columns 有机会修复
            try:
                self._conn.executescript(_SCHEMA)
            except sqlite3.Error as exc:
                log.warning("初始化表结构时出现问题（稍后尝试迁移修复）: %s", exc)

            self._migrate_columns()
            try:
                self._conn.commit()
            except sqlite3.Error as exc:  # pragma: no cover - 磁盘异常
                log.error("提交数据库初始化失败: %s", exc)

    def _migrate_columns(self) -> None:
        """给老版本数据库补上 tasks 表新增的列。

        ``CREATE TABLE IF NOT EXISTS`` 只在表不存在时建表，老库不会自动加列，
        因此这里用 ``PRAGMA table_info`` 检查后逐列 ``ALTER TABLE ADD COLUMN``。
        只做“加列”，不改动任何既有数据（时长记录与任务内容都原样保留）。
        """
        try:
            existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(tasks)")}
        except sqlite3.Error as exc:
            log.warning("读取 tasks 表结构失败: %s", exc)
            return
        for column, column_type in _TASK_COLUMN_MIGRATIONS:
            if column in existing:
                continue
            try:
                self._conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {column_type}")
                log.info("数据库迁移：tasks 表新增列 %s %s", column, column_type)
            except sqlite3.Error as exc:
                log.warning("迁移 tasks.%s 失败: %s", column, exc)

        # 列补齐之后再建依赖它们的索引
        for statement in _TASK_INDEX_MIGRATIONS:
            try:
                self._conn.execute(statement)
            except sqlite3.Error as exc:
                log.warning("建立任务索引失败: %s", exc)

    # ------------------------------------------------------------------ 写入
    def add_seconds(
        self,
        target_name: str,
        seconds: float,
        kind: str = "process",
        day: str | None = None,
        write_detail: bool = True,
    ) -> None:
        """把一段时长累加到当天记录（增量写入，可高频调用）。"""
        seconds = float(seconds)
        if seconds <= 0:
            return
        day = day or today_str()
        ts = now_str()
        hour = int(ts[11:13])
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO daily_target(day, target_name, target_kind, seconds, updated_at)
                    VALUES(?, ?, ?, ?, ?)
                    ON CONFLICT(day, target_name) DO UPDATE SET
                        seconds    = seconds + excluded.seconds,
                        target_kind= excluded.target_kind,
                        updated_at = excluded.updated_at
                    """,
                    (day, target_name, kind, seconds, ts),
                )
                if write_detail:
                    self._conn.execute(
                        "INSERT INTO tick_log(day, ts, target_name, target_kind, seconds) VALUES(?,?,?,?,?)",
                        (day, ts, target_name, kind, seconds),
                    )
                self._conn.execute(
                    """
                    INSERT INTO hourly(day, hour, seconds) VALUES(?, ?, ?)
                    ON CONFLICT(day, hour) DO UPDATE SET seconds = seconds + excluded.seconds
                    """,
                    (day, hour, seconds),
                )
                self._conn.commit()
            except sqlite3.Error as exc:
                log.warning("写入使用记录失败: %s", exc)

    # ------------------------------------------------------------------ 查询
    def total_seconds(self, day: str | None = None) -> float:
        """某天所有监控对象的合计秒数。"""
        day = day or today_str()
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(seconds), 0) AS s FROM daily_target WHERE day = ?", (day,)
            ).fetchone()
        return float(row["s"] or 0.0)

    def today_targets(self, day: str | None = None) -> list[TargetStat]:
        """某天按对象拆分的统计，时长从多到少排序。"""
        day = day or today_str()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT target_name, target_kind, SUM(seconds) AS s
                FROM daily_target WHERE day = ?
                GROUP BY target_name, target_kind
                ORDER BY s DESC
                """,
                (day,),
            ).fetchall()
        return [TargetStat(r["target_name"], r["target_kind"], float(r["s"] or 0)) for r in rows]

    def day_summary(self, day: str | None = None) -> dict[str, float]:
        """某天汇总：总秒数 + 各对象明细。"""
        day = day or today_str()
        targets = {t.name: t.seconds for t in self.today_targets(day)}
        return {"day": day, "total": sum(targets.values()), **targets}  # type: ignore[dict-item]

    def recent_days(self, days: int = 7) -> list[DayStat]:
        """最近 N 天（含今天，升序）的总时长。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT day, SUM(seconds) AS s FROM daily_target
                WHERE day >= date('now', 'localtime', ?)
                GROUP BY day ORDER BY day ASC
                """,
                (f"-{max(1, int(days)) - 1} day",),
            ).fetchall()
        found = {r["day"]: float(r["s"] or 0.0) for r in rows}

        from .utils import last_n_days  # 局部导入避免循环依赖

        return [DayStat(day, found.get(day, 0.0)) for day in last_n_days(days)]

    def recent_days_by_target(self, days: int = 7, top: int = 6) -> dict[str, dict[str, float]]:
        """最近 N 天，每天按时长前 top 的对象拆分（用于堆叠柱状图）。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT day, target_name, SUM(seconds) AS s FROM daily_target
                WHERE day >= date('now', 'localtime', ?)
                GROUP BY day, target_name ORDER BY day ASC
                """,
                (f"-{max(1, int(days)) - 1} day",),
            ).fetchall()
        totals: dict[str, float] = {}
        for r in rows:
            totals[r["target_name"]] = totals.get(r["target_name"], 0.0) + float(r["s"] or 0.0)
        keep = {name for name, _ in sorted(totals.items(), key=lambda kv: -kv[1])[:top]}
        result: dict[str, dict[str, float]] = {}
        for r in rows:
            name = r["target_name"] if r["target_name"] in keep else "其他"
            result.setdefault(name, {})[r["day"]] = float(r["s"] or 0.0)
        return result

    def hourly_today(self, day: str | None = None) -> dict[int, float]:
        """今天 0-23 点的使用分布。"""
        day = day or today_str()
        with self._lock:
            rows = self._conn.execute(
                "SELECT hour, seconds FROM hourly WHERE day = ?", (day,)
            ).fetchall()
        return {int(r["hour"]): float(r["seconds"] or 0.0) for r in rows}

    # ------------------------------------------------------------------ 待办任务
    def add_task(self, title: str, due_at: datetime | None = None,
                 priority: int = 1, note: str = "",
                 task_type: str = TYPE_ONCE,
                 time_start: str | None = None,
                 time_end: str | None = None,
                 days_of_week: str | None = None,
                 remind_before_minutes: int = DEFAULT_REMIND_BEFORE,
                 start_date: str | None = None,
                 target_count: int | None = None,
                 deadline: str | date | None = None,
                 remind_time: str | None = None,
                 auto_target: str | None = None,
                 auto_minutes: int | None = None) -> int | None:
        """新增待办任务，返回新任务 ID（失败返回 None）。

        :param task_type: ``once`` 单次 / ``daily`` 每天 / ``weekly`` 每周 / ``cumulative`` 累计打卡
        :param time_start: 周期任务时间窗开始 ``'HH:MM'``
        :param time_end:   周期任务时间窗结束 ``'HH:MM'``
        :param days_of_week: 每周任务用，ISO 星期如 ``'1,3,5'``（1=周一）
        :param remind_before_minutes: 提前多少分钟提醒（默认 15）
        :param start_date: 从哪天开始生效（``'YYYY-MM-DD'``）。
            ``None``（默认）= 自动判断：**今天的时间窗已经过去就从明天开始**；
            传空串表示不限制（老数据就是这种，任何一天都排班）。
        :param target_count: 累计打卡任务的总目标次数（如 60）
        :param deadline: 累计打卡任务的最终截止日期（**含当天**）
        :param remind_time: 累计打卡任务的每日提醒时间 ``'HH:MM'``
        :param auto_target: 【可选】监控对象名：今日累计时长达标就自动打卡
        :param auto_minutes: 【可选】自动打卡的时长阈值（分钟，默认 30）
        """
        title = (title or "").strip()
        if not title:
            return None
        if task_type not in TASK_TYPES:
            task_type = TYPE_ONCE
        if task_type == TYPE_ONCE:
            # 单次任务不保留周期字段，避免语义混淆
            time_start = time_end = days_of_week = None
            start_date = None
            target_count, deadline, remind_time = None, None, None
            auto_target, auto_minutes = None, None
        elif task_type == TYPE_CUMULATIVE:
            # 累计打卡任务：只认"目标次数 + 截止日期 + 每日提醒时间"，
            # time_start/time_end/days_of_week 一律留空 —— 它没有"今天几点到几点做"的概念，
            # 留着会让窗口判定、列表文案、统计口径通通多出一份假计划。
            due_at = None
            time_start = time_end = days_of_week = None
            start_date = None
            try:
                target = int(target_count) if target_count is not None else DEFAULT_TARGET_COUNT
            except (TypeError, ValueError):
                target = DEFAULT_TARGET_COUNT
            target_count = max(1, min(MAX_TARGET_COUNT, target))
            deadline_date = _coerce_date(deadline)
            if deadline_date is None:
                log.warning("新增累计打卡任务失败：没有有效的截止日期")
                return None
            deadline = deadline_date.strftime("%Y-%m-%d")
            remind_time = format_hhmm(parse_hhmm(remind_time, DEFAULT_REMIND_TIME))
            auto_target, auto_minutes = _coerce_auto(auto_target, auto_minutes)
        else:
            target_count, deadline, remind_time = None, None, None
            auto_target, auto_minutes = None, None
            # 周期任务的时间由 time_start/time_end 表达，due_at 没有意义：
            # 留着它会让"单次任务按 due_at 排列表/统计"的口径被污染（图表里凭空多一条）。
            due_at = None
            if task_type == TYPE_DAILY:
                days_of_week = None
            else:
                days_of_week = format_iso_weekdays(parse_iso_weekdays(days_of_week))
                if not days_of_week:
                    # 每周任务必须至少选一天，否则退化成"没有计划"，直接拒绝
                    log.warning("新增每周任务失败：没有选择星期几")
                    return None
            time_start = format_hhmm(parse_hhmm(time_start, DEFAULT_START))
            time_end = format_hhmm(parse_hhmm(time_end, DEFAULT_END))
            # 今天的时间窗已经过了就从明天开始（详见 recurrence.suggested_start_date）：
            # 否则晚上建一条"每天早上 6 点"的任务，列表会立刻显示"今天已超期"。
            # 传 start_date="" 表示不限制（测试与老数据用）。
            if start_date is None:
                start_date = suggested_start_date(task_type, time_start, time_end) or None
        try:
            before = int(remind_before_minutes)
        except (TypeError, ValueError):
            before = DEFAULT_REMIND_BEFORE
        before = max(MIN_REMIND_BEFORE, min(MAX_REMIND_BEFORE, before))

        with self._lock:
            try:
                cursor = self._conn.execute(
                    """
                    INSERT INTO tasks(title, due_at, created_at, completed, priority, note,
                                      task_type, time_start, time_end, days_of_week,
                                      remind_before_minutes, start_date,
                                      target_count, deadline, remind_time,
                                      auto_target, auto_minutes)
                    VALUES(?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        title[:200],
                        due_at.strftime(TS_FMT) if due_at else None,
                        datetime.now().strftime(TS_FMT),
                        max(1, int(priority)),
                        (note or "")[:500],
                        task_type,
                        time_start,
                        time_end,
                        days_of_week,
                        before,
                        start_date,
                        target_count,
                        deadline,
                        remind_time,
                        auto_target,
                        auto_minutes,
                    ),
                )
                self._conn.commit()
                return int(cursor.lastrowid or 0)
            except sqlite3.Error as exc:
                log.warning("新增任务失败: %s", exc)
                return None

    def get_task(self, task_id: int) -> Task | None:
        """按 ID 取单条任务。"""
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
        return Task.from_row(row) if row else None

    def list_tasks(self, status: str = "pending", recurring: bool = False) -> list[Task]:
        """列出任务。

        :param status: ``pending`` 未完成（默认，含已超期）/ ``completed`` 已完成 / ``all`` 全部
        :param recurring: 是否**只**列周期任务（每天 / 每周）。
            默认 ``False`` 表示**只列单次任务** —— 周期任务的"完成"只在
            ``task_logs`` 里，把它们混进待办列表会既分不清也重复展示
            （周期任务的今日状态由 :meth:`scheduled_on` 负责）。
        """
        conditions = []
        params: list[object] = []
        if recurring:
            conditions.append("COALESCE(task_type, 'once') IN (?, ?)")
            params.extend([TYPE_DAILY, TYPE_WEEKLY])
        else:
            conditions.append("COALESCE(task_type, 'once') = 'once'")
        if status == "pending":
            conditions.append("completed = 0")
        elif status == "completed":
            conditions.append("completed = 1")
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM tasks {where}
                ORDER BY (due_at IS NULL) ASC, due_at ASC, priority DESC, id ASC
                """,
                params,
            ).fetchall()
        return [Task.from_row(row) for row in rows]

    def pending_tasks(self) -> list[Task]:
        """未完成的**单次**任务（按截止时间从近到远，无期限排最后）。

        不含周期任务：它们的"今天做没做"由 ``task_logs`` 决定，
        提醒由调度器负责（见 ``timeguard.scheduler``）。
        """
        return self.list_tasks("pending")

    # ------------------------------------------------------------------ 周期任务：规则查询
    def recurring_tasks(self) -> list[Task]:
        """全部周期任务（每天 / 每周），按开始时间排序。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM tasks
                WHERE COALESCE(task_type, 'once') IN (?, ?)
                ORDER BY time_start ASC, id ASC
                """,
                (TYPE_DAILY, TYPE_WEEKLY),
            ).fetchall()
        return [Task.from_row(row) for row in rows]

    def scheduled_on(self, day: date | str) -> list[Task]:
        """``day`` 这一天需要做的**周期任务**（"每天"总是命中；"每周"看星期几）。"""
        if isinstance(day, str):
            day = datetime.strptime(day[:10], "%Y-%m-%d").date()
        return [task for task in self.recurring_tasks() if task.rule.occurs_on(day)]

    # ------------------------------------------------------------------ 累计打卡任务
    def cumulative_tasks(self) -> list[Task]:
        """全部累计打卡任务（按截止日期从近到远）。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM tasks
                WHERE COALESCE(task_type, 'once') = ?
                ORDER BY (deadline IS NULL) ASC, deadline ASC, id ASC
                """,
                (TYPE_CUMULATIVE,),
            ).fetchall()
        return [Task.from_row(row) for row in rows]

    def checkin_dates(self, task_id: int) -> list[date]:
        """某任务已打卡的自然日（升序、去重）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT checkin_date FROM check_in_logs WHERE task_id = ? ORDER BY checkin_date ASC",
                (int(task_id),),
            ).fetchall()
        result: list[date] = []
        for row in rows:
            try:
                result.append(datetime.strptime(str(row["checkin_date"])[:10], "%Y-%m-%d").date())
            except ValueError:
                continue
        return result

    def checkin_count(self, task_id: int) -> int:
        """某任务累计打卡次数。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM check_in_logs WHERE task_id = ?", (int(task_id),)
            ).fetchone()
        return int(row["n"] or 0)

    def checkin_counts(self) -> dict[int, int]:
        """**一次查询**取回所有任务的打卡次数（列表刷新时避免 N+1 查询）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT task_id, COUNT(*) AS n FROM check_in_logs GROUP BY task_id"
            ).fetchall()
        return {int(row["task_id"]): int(row["n"] or 0) for row in rows}

    def checkin_dates_map(self) -> dict[int, list[date]]:
        """**一次查询**取回所有任务的打卡日期列表（列表刷新用，避免逐条查）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT task_id, checkin_date FROM check_in_logs ORDER BY checkin_date ASC"
            ).fetchall()
        result: dict[int, list[date]] = {}
        for row in rows:
            try:
                day = datetime.strptime(str(row["checkin_date"])[:10], "%Y-%m-%d").date()
            except ValueError:
                continue
            result.setdefault(int(row["task_id"]), []).append(day)
        return result

    def checkin_log(self, task_id: int, day: date | str) -> CheckInLog | None:
        """某任务某一天的打卡记录（没有则 ``None``）。"""
        key = day.strftime("%Y-%m-%d") if isinstance(day, date) else str(day)[:10]
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM check_in_logs WHERE task_id = ? AND checkin_date = ?",
                (int(task_id), key),
            ).fetchone()
        return CheckInLog.from_row(row) if row else None

    def checkin_logs(self, task_id: int, since: date | None = None,
                     until: date | None = None) -> list[CheckInLog]:
        """某任务的打卡历史（可选日期区间，升序）—— 画热力图/日历用。"""
        sql = "SELECT * FROM check_in_logs WHERE task_id = ?"
        params: list[object] = [int(task_id)]
        if since is not None:
            sql += " AND checkin_date >= ?"
            params.append(since.strftime("%Y-%m-%d"))
        if until is not None:
            sql += " AND checkin_date <= ?"
            params.append(until.strftime("%Y-%m-%d"))
        sql += " ORDER BY checkin_date ASC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [CheckInLog.from_row(row) for row in rows]

    def monitored_target_names(self, days: int = 30) -> list[str]:
        """最近监控过的对象名（按累计时长从多到少）—— 给"自动打卡"下拉框做候选。

        没有这一项的话，用户得凭记忆把监控对象名一字不差地敲进去，很容易打不上卡。
        """
        start = last_n_days(max(1, int(days)))[0]
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT target_name, SUM(seconds) AS s FROM daily_target
                WHERE day >= ? AND target_name <> ''
                GROUP BY target_name ORDER BY s DESC LIMIT 40
                """,
                (start,),
            ).fetchall()
        return [str(row["target_name"]) for row in rows]

    def checkin_ids_on(self, day: date | str) -> set[int]:
        """``day`` 这一天打过卡的任务 id 集合（一次查询，列表/统计用）。"""
        key = day.strftime("%Y-%m-%d") if isinstance(day, date) else str(day)[:10]
        with self._lock:
            rows = self._conn.execute(
                "SELECT task_id FROM check_in_logs WHERE checkin_date = ?", (key,)
            ).fetchall()
        return {int(row["task_id"]) for row in rows}

    def checkin_logs_between(self, start: date, end: date) -> list[CheckInLog]:
        """某个日期区间内**所有**任务的打卡记录（统计图表用，一次查完）。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM check_in_logs
                WHERE checkin_date >= ? AND checkin_date <= ?
                ORDER BY checkin_date ASC, task_id ASC
                """,
                (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")),
            ).fetchall()
        return [CheckInLog.from_row(row) for row in rows]

    def task_status(self, task) -> "CumulativeStatus | None":
        """累计打卡任务的进度与状态（``task`` 可以是 ``Task`` 或 task_id）。"""
        task_obj = self.get_task(int(task)) if isinstance(task, int) else task
        if task_obj is None or not task_obj.is_cumulative:
            return None
        return cumulative_status(task_obj.rule, self.checkin_dates(task_obj.id))

    def check_in(self, task_id: int, day: date | str | None = None,
                 note: str = "", source: str = "manual",
                 now: datetime | None = None) -> tuple[bool, str]:
        """给累计打卡任务打卡（**同一天只能成功一次**）。

        返回 ``(是否写入成功, 给用户看的一句话)``。

        为什么要返回一句话：失败原因有好几种（不是打卡任务、今天已经打过、已过截止日），
        界面要原样告诉用户，不能默默什么都不做。

        日期边界（避坑 #2）：``day`` 缺省时取**本地时间** ``date.today()``，
        也就是自然日 00:00~23:59:59 —— 凌晨 00:10 打卡算新的一天，不会算到昨天。

        关于**补签**（``day`` 传过去的某一天、``source="backfill"``）：
        允许补，但只允许补"任务创建之后、今天之前"的日子 —— 未来还没发生，
        创建之前这条任务根本不存在，两种都不给补（界面会挡，这里也挡一道）。
        """
        task = self.get_task(int(task_id))
        if task is None:
            return False, "任务不存在"
        if not task.is_cumulative:
            return False, "这不是累计打卡任务"
        stamp = now or datetime.now()
        # 日期一律取**本地时间**的自然日：给了 now 就从 now 取（这样"凌晨 00:10 打卡算
        # 新的一天"是可以被测试证明的），否则用 date.today()。不用 UTC，不四舍五入。
        if isinstance(day, date):
            target_day = day
        elif day:
            try:
                target_day = datetime.strptime(str(day)[:10], "%Y-%m-%d").date()
            except ValueError:
                return False, f"日期格式不对：{day}"
        else:
            target_day = stamp.date()
        if task.rule.is_expired(target_day):
            return False, f"已过截止日期（{task.rule.deadline:%Y-%m-%d}），不能再打卡"
        today = date.today()
        if target_day > today:
            return False, f"{target_day:%m-%d} 还没到，不能提前打卡"
        backfill = target_day < today
        if backfill and task.created_at is not None and target_day < task.created_at.date():
            return False, (f"{target_day:%m-%d} 早于任务创建日期"
                           f"（{task.created_at:%Y-%m-%d}），不能补签")
        if backfill and not source or source == "manual" and backfill:
            source = "backfill"          # 补签自动打上标记，方便日后分辨
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO check_in_logs(task_id, checkin_date, checkin_at, note, source)
                    VALUES(?, ?, ?, ?, ?)
                    """,
                    (int(task_id), target_day.strftime("%Y-%m-%d"),
                     stamp.strftime(TS_FMT), (note or "")[:200], source or "manual"),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                # UNIQUE(task_id, checkin_date) → 今天已经打过卡了
                return False, f"{target_day:%m-%d} 已经打过卡了（同一天只能打一次）"
            except sqlite3.Error as exc:
                log.warning("打卡失败: %s", exc)
                return False, "打卡失败（数据库错误，详见日志）"
        total = self.checkin_count(task_id)
        status = cumulative_status(task.rule, self.checkin_dates(task_id))
        kind = "补签" if source == "backfill" else ("自动打卡" if source == "auto" else "打卡")
        log.info("累计打卡：任务 #%s %s %s成功（%s/%s，%s）",
                 task_id, target_day, kind, total, status.target, status.state)
        prefix = f"{target_day:%m-%d} {kind}成功！" if backfill else f"{kind}成功！"
        if status.finished:
            return True, f"{prefix}{status.progress_text()} —— {status.state_label}"
        return True, f"{prefix}{status.progress_text()}，还差 {status.remaining} 次"

    def undo_check_in(self, task_id: int, day: date | str | None = None) -> bool:
        """撤销某一天的打卡（点错了可以退回；也用于测试）。"""
        key = day.strftime("%Y-%m-%d") if isinstance(day, date) else (
            str(day)[:10] if day else date.today().strftime("%Y-%m-%d"))
        with self._lock:
            try:
                cursor = self._conn.execute(
                    "DELETE FROM check_in_logs WHERE task_id = ? AND checkin_date = ?",
                    (int(task_id), key),
                )
                self._conn.commit()
            except sqlite3.Error as exc:
                log.warning("撤销打卡失败: %s", exc)
                return False
        removed = int(cursor.rowcount or 0) > 0
        if removed:
            log.info("累计打卡：任务 #%s 撤销了 %s 的打卡", task_id, key)
        return removed

    # ------------------------------------------------------------------ 周期任务：发生记录
    def get_task_log(self, task_id: int, occur_date: date | str) -> TaskLog | None:
        """取某任务某一天的发生记录。"""
        key = occur_date.strftime("%Y-%m-%d") if isinstance(occur_date, date) else str(occur_date)[:10]
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM task_logs WHERE task_id = ? AND occur_date = ?",
                (int(task_id), key),
            ).fetchone()
        return TaskLog.from_row(row) if row else None

    def task_logs_since(self, start_day: str) -> list[TaskLog]:
        """取 ``start_day`` 起的全部发生记录（含）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM task_logs WHERE occur_date >= ? ORDER BY occur_date ASC, task_id ASC",
                (str(start_day)[:10],),
            ).fetchall()
        return [TaskLog.from_row(row) for row in rows]

    def task_logs_on(self, day: date | str) -> list[TaskLog]:
        """取某一天的全部发生记录。"""
        key = day.strftime("%Y-%m-%d") if isinstance(day, date) else str(day)[:10]
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM task_logs WHERE occur_date = ? ORDER BY task_id ASC", (key,)
            ).fetchall()
        return [TaskLog.from_row(row) for row in rows]

    def log_occurrence(self, task_id: int, occur_date: date | str, *,
                       completed: bool | None = None,
                       completed_at: datetime | None = None,
                       was_late: bool | None = None,
                       remind_at: datetime | None = None,
                       remind_kind: str | None = None) -> bool:
        """写入 / 更新某任务某一天的发生记录（只动传入的字段）。

        用 ``UNIQUE(task_id, occur_date)`` + upsert 保证不会重复打卡、重复提醒。
        """
        key = occur_date.strftime("%Y-%m-%d") if isinstance(occur_date, date) else str(occur_date)[:10]
        fields = ["task_id", "occur_date"]
        values: list[object] = [int(task_id), key]
        updates: list[str] = []
        if completed is not None:
            fields.append("completed")
            values.append(1 if completed else 0)
            updates.append("completed = excluded.completed")
            if completed:
                fields.append("completed_at")
                values.append((completed_at or datetime.now()).strftime(TS_FMT))
                updates.append("completed_at = excluded.completed_at")
            else:
                updates.append("completed_at = NULL")
        if was_late is not None:
            fields.append("was_late")
            values.append(1 if was_late else 0)
            updates.append("was_late = excluded.was_late")
        if remind_at is not None:
            fields.append("remind_at")
            values.append(remind_at.strftime(TS_FMT))
            updates.append("remind_at = excluded.remind_at")
        if remind_kind is not None:
            fields.append("remind_kind")
            values.append(str(remind_kind)[:20])
            updates.append("remind_kind = excluded.remind_kind")

        if not updates:                      # 只保证记录存在
            updates.append("task_id = task_logs.task_id")
        placeholders = ", ".join("?" for _ in fields)
        sql = (
            f"INSERT INTO task_logs({', '.join(fields)}) VALUES({placeholders}) "
            f"ON CONFLICT(task_id, occur_date) DO UPDATE SET {', '.join(updates)}"
        )
        with self._lock:
            try:
                self._conn.execute(sql, values)
                self._conn.commit()
                return True
            except sqlite3.Error as exc:
                log.warning("写入任务发生记录失败: %s", exc)
                return False

    def mark_occurrences_reminded(self, entries: list[tuple[int, date | str]],
                                  kind: str = "advance",
                                  when: datetime | None = None) -> int:
        """把一批发生记录标记为"已提醒"（只标记真正进入提醒内容的那几条）。"""
        stamp = when or datetime.now()
        count = 0
        for task_id, occur_date in entries:
            if self.log_occurrence(task_id, occur_date, remind_at=stamp, remind_kind=kind):
                count += 1
        return count

    def reminder_pending_on(self, day: date | str) -> list[tuple[Task, TaskLog | None]]:
        """``day`` 这一天计划内、且**还没提醒过**的周期任务（连着已有记录一起返回）。"""
        if isinstance(day, str):
            day = datetime.strptime(day[:10], "%Y-%m-%d").date()
        logs = {log.task_id: log for log in self.task_logs_on(day)}
        result: list[tuple[Task, TaskLog | None]] = []
        for task in self.scheduled_on(day):
            log_row = logs.get(task.id)
            if log_row is not None and log_row.remind_at is not None:
                continue
            result.append((task, log_row))
        return result

    def complete_occurrence(self, task_id: int, occur_date: date | str | None = None,
                            completed: bool = True, was_late: bool | None = None) -> bool:
        """给周期任务的某一天打卡（默认今天）。"""
        day = occur_date or date.today()
        if isinstance(day, str):
            day = datetime.strptime(day[:10], "%Y-%m-%d").date()
        if not completed:
            # 撤销打卡时把 was_late 一起清掉，否则会留下
            # "completed=False 但 was_late=True" 这种语义脏数据
            return self.log_occurrence(task_id, day, completed=False, was_late=False)
        if was_late is None:
            task = self.get_task(int(task_id))
            window = task.occurrence_on(day) if task else None
            was_late = bool(window and window.is_late(datetime.now()))
        return self.log_occurrence(task_id, day, completed=completed, was_late=was_late)

    def is_occurrence_done(self, task_id: int, occur_date: date | str | None = None) -> bool:
        """某任务某一天是否已打卡完成。"""
        log_row = self.get_task_log(int(task_id), occur_date or date.today())
        return bool(log_row and log_row.completed)

    def _has_log_today(self, task_id: int) -> bool:
        """今天是不是已经有这条任务的发生记录（提醒过或打过卡都算）。"""
        return self.get_task_log(int(task_id), date.today()) is not None

    def delete_task_logs(self, task_id: int) -> int:
        """删除某任务的全部发生记录（配合 delete_task 使用）。"""
        with self._lock:
            try:
                cursor = self._conn.execute("DELETE FROM task_logs WHERE task_id = ?",
                                            (int(task_id),))
                self._conn.commit()
                return int(cursor.rowcount or 0)
            except sqlite3.Error as exc:
                log.warning("删除任务发生记录失败: %s", exc)
                return 0

    def update_task(self, task_id: int, title: str | None = None,
                    due_at: datetime | None = None, priority: int | None = None,
                    note: str | None = None, task_type: str | None = None,
                    time_start: str | None = None, time_end: str | None = None,
                    days_of_week: str | None = None,
                    remind_before_minutes: int | None = None,
                    target_count: int | None = None,
                    deadline: str | date | None = None,
                    remind_time: str | None = None,
                    auto_target: str | None = None,
                    auto_minutes: int | None = None,
                    clear_auto: bool = False,
                    clear_due: bool = False) -> bool:
        """修改任务（只更新传入的字段）。

        :param clear_due: 把 ``due_at`` 置空（切换成周期任务时需要）
        :param target_count: 累计打卡任务的目标次数
        :param deadline: 累计打卡任务的截止日期
        :param remind_time: 累计打卡任务的每日提醒时间
        :param auto_target: 【可选】监控对象名（达标自动打卡）
        :param auto_minutes: 【可选】自动打卡的时长阈值（分钟）
        :param clear_auto: 关掉"自动打卡"（把两个字段都清空）
        """
        fields: list[str] = []
        values: list[object] = []
        if title is not None:
            title = title.strip()
            if not title:
                return False
            fields.append("title = ?")
            values.append(title[:200])
        if clear_due:
            fields.append("due_at = NULL")
        elif due_at is not None:
            fields.append("due_at = ?")
            values.append(due_at.strftime(TS_FMT))
        if priority is not None:
            fields.append("priority = ?")
            values.append(max(1, int(priority)))
        if note is not None:
            fields.append("note = ?")
            values.append(note[:500])
        if task_type is not None:
            if task_type not in TASK_TYPES:
                return False
            if task_type == TYPE_WEEKLY:
                # 每周任务必须至少有一个星期，否则会变成"永不发生"的静默死任务
                # （add_task 也是这么校验的）。这里允许本次一起传入 days_of_week，
                # 否则要看库里已有的值。
                incoming = (format_iso_weekdays(parse_iso_weekdays(days_of_week))
                            if days_of_week is not None else "")
                if not incoming:
                    existing = self.get_task(int(task_id))
                    if existing is None or not existing.rule.days_of_week:
                        log.warning("改成每周任务失败：没有选择星期几（会变成永不提醒的死任务）")
                        return False
            fields.append("task_type = ?")
            values.append(task_type)
            if task_type == TYPE_ONCE:
                # 变回单次任务：清掉周期字段，避免语义混淆
                fields += ["time_start = NULL", "time_end = NULL", "days_of_week = NULL",
                           "target_count = NULL", "deadline = NULL", "remind_time = NULL",
                           "auto_target = NULL", "auto_minutes = NULL"]
            elif task_type == TYPE_CUMULATIVE:
                # 改成累计打卡：周期字段必须清掉，否则会多出一份"假计划"，
                # 让窗口判定 / 列表文案 / 统计口径全部对不上。
                fields += ["time_start = NULL", "time_end = NULL", "days_of_week = NULL"]
                time_start = time_end = days_of_week = None
                existing_cum = self.get_task(int(task_id))
                has_deadline = deadline not in (None, "") or (
                    existing_cum is not None and existing_cum.rule.deadline is not None)
                if not has_deadline:
                    log.warning("改成累计打卡任务失败：没有截止日期")
                    return False
            elif task_type == TYPE_DAILY:
                fields.append("days_of_week = NULL")
                # 关键：把入参也一并丢弃，否则下面那段通用的
                # "if days_of_week is not None" 会紧接着再写一次，
                # 把刚清掉的星期几**覆盖回去**（每周 → 每天 时必然发生）。
                days_of_week = None
            if task_type in (TYPE_DAILY, TYPE_WEEKLY):
                # 从"累计打卡"改回周期任务：打卡字段必须清掉，
                # 否则库里留着 target_count/deadline，界面和统计会各读一半。
                fields += ["target_count = NULL", "deadline = NULL", "remind_time = NULL",
                           "auto_target = NULL", "auto_minutes = NULL"]
                target_count = deadline = remind_time = None
                auto_target = auto_minutes = None
        if time_start is not None:
            fields.append("time_start = ?")
            values.append(format_hhmm(parse_hhmm(time_start, DEFAULT_START)))
        if time_end is not None:
            fields.append("time_end = ?")
            values.append(format_hhmm(parse_hhmm(time_end, DEFAULT_END)))
        if days_of_week is not None:
            fields.append("days_of_week = ?")
            values.append(format_iso_weekdays(parse_iso_weekdays(days_of_week)))
        if remind_before_minutes is not None:
            try:
                before = int(remind_before_minutes)
            except (TypeError, ValueError):
                before = DEFAULT_REMIND_BEFORE
            fields.append("remind_before_minutes = ?")
            values.append(max(MIN_REMIND_BEFORE, min(MAX_REMIND_BEFORE, before)))

        # ---- 累计打卡任务：目标次数 / 截止日期 / 每日提醒时间 ----
        if target_count is not None:
            try:
                target = int(target_count)
            except (TypeError, ValueError):
                target = DEFAULT_TARGET_COUNT
            fields.append("target_count = ?")
            values.append(max(1, min(MAX_TARGET_COUNT, target)))
        if deadline is not None:
            deadline_date = _coerce_date(deadline)
            if deadline_date is None:
                log.warning("修改失败：截止日期无法解析（%r）", deadline)
                return False
            fields.append("deadline = ?")
            values.append(deadline_date.strftime("%Y-%m-%d"))
        if remind_time is not None:
            fields.append("remind_time = ?")
            values.append(format_hhmm(parse_hhmm(remind_time, DEFAULT_REMIND_TIME)))
        if clear_auto:
            fields += ["auto_target = NULL", "auto_minutes = NULL"]
        elif auto_target is not None:
            # 只填了对象没填分钟 → 用默认阈值；填了对象就一起写两个字段
            name, minutes = _coerce_auto(auto_target, auto_minutes)
            fields += ["auto_target = ?", "auto_minutes = ?"]
            values += [name, minutes]
        elif auto_minutes is not None:
            fields.append("auto_minutes = ?")
            values.append(max(MIN_AUTO_MINUTES, min(MAX_AUTO_MINUTES, int(auto_minutes))))

        # ---- 生效起始日：改了"周期/时间段"就要重算 ----
        # 用户预期：把时间段改成"今天已经过去"的那种（比如晚上改成 06:00~08:00），
        # 应该从明天开始算，而不是立刻显示"今天已超期"。
        schedule_changed = any(x is not None for x in (task_type, time_start, time_end, days_of_week))
        if schedule_changed:
            existing_task = self.get_task(int(task_id))
            if existing_task is not None:
                eff_type = task_type if task_type is not None else existing_task.rule.task_type
                eff_start = time_start or format_hhmm(existing_task.rule.time_start)
                eff_end = time_end or format_hhmm(existing_task.rule.time_end)
                suggested = suggested_start_date(eff_type, eff_start, eff_end)
                old_start = existing_task.rule.start_date
                if (suggested and (old_start is None or suggested > old_start.strftime("%Y-%m-%d"))
                        and not self._has_log_today(int(task_id))):
                    # 只有当"今天这一次还没被碰过"时才往前推：已经提醒过 / 已经打过卡的
                    # 今天不能被抹掉（否则图表里的打卡记录会变成"计划外"的幽灵数据）。
                    fields.append("start_date = ?")
                    values.append(suggested)

        if not fields:
            return False
        values.append(int(task_id))
        with self._lock:
            try:
                self._conn.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?", values)
                self._conn.commit()
                return True
            except sqlite3.Error as exc:
                log.warning("修改任务失败: %s", exc)
                return False

    def complete_task(self, task_id: int, completed: bool = True) -> bool:
        """标记 / 取消“已完成”。

        * **单次任务**：直接写 ``tasks.completed``（原有逻辑，行为不变）。
        * **周期任务**：改写**今天那一次**的发生记录（``task_logs``），
          绝不写 ``tasks.completed`` —— 否则"今天完成"会变成"永久完成"。
        * **累计打卡任务**：这里直接拒绝。它的进度只认 ``check_in_logs``，
          写成 ``tasks.completed`` 会把"打了 45 次卡"变成"任务永久完成"。
          请改用 :meth:`check_in`。
        """
        task = self.get_task(int(task_id))
        if task is None:
            return False
        if task.is_cumulative:
            log.warning("拒绝把累计打卡任务 #%s 标记为完成：请用 check_in()", task_id)
            return False
        if task.is_recurring:
            return self.complete_occurrence(task.id, date.today(), completed=completed)
        return self.set_task_completed(task.id, completed)

    def set_task_completed(self, task_id: int, completed: bool,
                           when: datetime | None = None) -> bool:
        """写入单次任务的完成状态（周期任务请用 :meth:`complete_occurrence`）。

        取消完成时必须把 ``completed_at`` 一起清掉，否则图表会继续把这条
        已经"撤销完成"的任务算成按时/迟到完成。
        """
        with self._lock:
            try:
                self._conn.execute(
                    "UPDATE tasks SET completed = ?, completed_at = ? WHERE id = ?",
                    (1 if completed else 0,
                     (when or datetime.now()).strftime(TS_FMT) if completed else None,
                     int(task_id)),
                )
                self._conn.commit()
                return True
            except sqlite3.Error as exc:
                log.warning("更新任务状态失败: %s", exc)
                return False

    def delete_task(self, task_id: int) -> bool:
        """删除任务（连同它的周期发生记录一起删除）。"""
        with self._lock:
            try:
                self._conn.execute("DELETE FROM task_logs WHERE task_id = ?", (int(task_id),))
                self._conn.execute("DELETE FROM tasks WHERE id = ?", (int(task_id),))
                self._conn.commit()
                return True
            except sqlite3.Error as exc:
                log.warning("删除任务失败: %s", exc)
                return False

    def clear_completed_tasks(self) -> int:
        """清空所有已完成任务，返回删除条数。"""
        with self._lock:
            try:
                cursor = self._conn.execute("DELETE FROM tasks WHERE completed = 1")
                self._conn.commit()
                return int(cursor.rowcount or 0)
            except sqlite3.Error as exc:
                log.warning("清空已完成任务失败: %s", exc)
                return 0

    def mark_tasks_reminded(self, task_ids: list[int]) -> None:
        """记录“这些任务已经被开机提醒过”。"""
        if not task_ids:
            return
        ts = datetime.now().strftime(TS_FMT)
        with self._lock:
            try:
                self._conn.executemany(
                    "UPDATE tasks SET remind_count = remind_count + 1, last_remind_at = ? WHERE id = ?",
                    [(ts, int(tid)) for tid in task_ids],
                )
                self._conn.commit()
            except sqlite3.Error as exc:
                log.warning("记录提醒次数失败: %s", exc)

    def tasks_due_for_notification(self, within_hours: float, interval_hours: float,
                                   now: datetime | None = None) -> list[Task]:
        """挑出"该发 Windows 通知"的**单次**任务。

        规则（与用户设置一一对应）：
        1. 未完成、且有截止时间；
        2. 截止时间在 ``within_hours`` 小时以内（含已超期）—— 即"临近截止"才打扰；
        3. 距上次发通知已超过 ``interval_hours`` 小时（``notified_at`` 为空表示从未发过）；
        4. 若用户把阈值调大了（例如 12h → 24h），即使刚发过通知也重新提醒一次。

        **只处理单次任务**：每日/每周任务的"开始前 N 分钟提醒"由
        :mod:`timeguard.scheduler` 负责，两者互斥，同一条任务不会发两次通知。

        :param within_hours: 提前多少小时开始提醒（默认 24）
        :param interval_hours: 两次通知的最小间隔（默认 6）
        """
        now = now or datetime.now()
        within_hours = max(0.5, float(within_hours))
        interval_hours = max(0.5, float(interval_hours))
        horizon = (now + timedelta(hours=within_hours)).strftime(TS_FMT)
        interval_cutoff = (now - timedelta(hours=interval_hours)).strftime(TS_FMT)

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM tasks
                WHERE completed = 0
                  AND COALESCE(task_type, 'once') = 'once'          -- 周期任务交给调度器
                  AND due_at IS NOT NULL
                  AND due_at <= ?                                  -- 已进入提醒窗口（含已超期）
                  AND (notified_at IS NULL                          -- 从没提醒过
                       OR notified_at <= ?                          -- 距上次超过间隔
                       OR notified_within_hours IS NULL             -- 老数据没有阈值记录
                       OR notified_within_hours < ?)                -- 阈值被调大了，重新提醒
                ORDER BY due_at ASC, priority DESC, id ASC
                """,
                (horizon, interval_cutoff, within_hours),
            ).fetchall()
        return [Task.from_row(row) for row in rows]

    def mark_tasks_notified(self, task_ids: list[int], within_hours: float) -> None:
        """记录“这些任务刚发过 Windows 通知”（用于间隔控制）。"""
        if not task_ids:
            return
        ts = datetime.now().strftime(TS_FMT)
        with self._lock:
            try:
                self._conn.executemany(
                    "UPDATE tasks SET notified_at = ?, notified_within_hours = ? WHERE id = ?",
                    [(ts, float(within_hours), int(tid)) for tid in task_ids],
                )
                self._conn.commit()
            except sqlite3.Error as exc:
                log.warning("记录通知时间失败: %s", exc)

    def task_counts(self, day: str | None = None) -> TaskCounts:
        """任务总览统计（**今天维度**，满足 ``total = pending + completed``）。

        口径说明（周期任务升级后的约定）：

        * **单次任务**：沿用原有口径（``tasks.completed`` / ``due_at``）。
        * **周期任务**：状态只在 ``task_logs`` 里，"今天该做"= 今天在计划内且还没打卡；
          超过今天的 ``time_end`` 仍未打卡 = 本次已超期。
        * ``total`` 指**今天要做的事情总量**（不含今天不排班的周期规则），
          这样主界面的"共 X 条 / 待办 Y / 已完成 Z"才能自洽。
        """
        day = day or today_str()
        # 注意：这里只统计**单次任务**（COALESCE 兼容老库没有 task_type 的情况），
        # 周期任务的数字全部来自下面按"今天"叠加的部分，否则会重复计数。
        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                    COUNT(*)                                        AS total,
                    SUM(CASE WHEN completed = 0 THEN 1 ELSE 0 END)  AS pending,
                    SUM(CASE WHEN completed = 1 THEN 1 ELSE 0 END)  AS completed,
                    SUM(CASE WHEN completed = 0 AND due_at IS NOT NULL
                              AND due_at < ? THEN 1 ELSE 0 END)     AS overdue,
                    SUM(CASE WHEN completed = 0 AND due_at IS NOT NULL
                              AND substr(due_at, 1, 10) = ? THEN 1 ELSE 0 END) AS due_today
                FROM tasks
                WHERE COALESCE(task_type, 'once') = 'once'
                """,
                (now_str(), day),
            ).fetchone()

        # ---- 叠加周期任务（今天维度）----
        day_date = datetime.strptime(day[:10], "%Y-%m-%d").date()
        now = datetime.now()
        recurring_scheduled = 0
        recurring_done = 0
        recurring_overdue = 0
        done_ids = {log_row.task_id for log_row in self.task_logs_on(day_date) if log_row.completed}
        for task in self.scheduled_on(day_date):
            recurring_scheduled += 1
            if task.id in done_ids:
                # 今天已经打卡：算"已完成"，**不能再算超期**
                # （否则同一次发生会既进 completed 又进 overdue，界面显示自相矛盾）
                recurring_done += 1
                continue
            window = task.occurrence_on(day_date)
            if window is not None and window.end < now:
                recurring_overdue += 1
        recurring_pending = max(0, recurring_scheduled - recurring_done)

        # ---- 叠加累计打卡任务（今天维度）----
        # 核心口径（用户诉求）：**截止日之前绝不进"已超期"**，只可能是
        # "今天还没打卡"（pending + due_today）或"今天已打卡"（completed）。
        # 这里只用两次批量查询（次数表 + 今天打过卡的任务集合），不做 N+1。
        totals = self.checkin_counts()
        checked_today = self.checkin_ids_on(day_date)
        cumulative_pending = cumulative_done = cumulative_overdue = cumulative_due_today = 0
        for task in self.cumulative_tasks():
            target = max(1, int(task.rule.target_count or 1))
            if int(totals.get(task.id, 0)) >= target:
                # 已达成目标：不是"今天要做的事"，不计入今天的三项数字
                # （否则"已完成"会天天 +1，跟周期任务当年那个 bug 一个味道）
                continue
            if task.rule.is_expired(day_date):
                cumulative_pending += 1      # 仍然是未完成的待办
                cumulative_overdue += 1      # 且只在过了截止日之后才计入超期
                continue
            if task.id in checked_today:
                cumulative_done += 1
            else:
                cumulative_pending += 1
                cumulative_due_today += 1    # 今天还没打卡 → 算"今天到期"

        pending = (row["pending"] or 0) + recurring_pending + cumulative_pending
        completed = (row["completed"] or 0) + recurring_done + cumulative_done
        return TaskCounts(
            # total 取 pending + completed，保证 TaskCounts 的不变式成立
            # （主界面显示"共 X 条 / 待办 Y / 已完成 Z"，三个数必须自洽）
            total=pending + completed,
            pending=pending,
            completed=completed,
            overdue=int(row["overdue"] or 0) + recurring_overdue + cumulative_overdue,
            # “今天到期”只统计还没完成的（已完成的今天到期任务不该继续提醒）
            due_today=(int(row["due_today"] or 0) + recurring_pending + cumulative_due_today),
        )

    def task_stats_by_day(self, days: int = 7) -> list[TaskDayStat]:
        """最近 N 天的任务完成情况，直接用于柱状图。

        两个来源合并：

        * **单次任务**：按创建日期归集，沿用原有口径（按时 / 超期完成 / 逾期未完成）；
        * **周期任务**：按 ``task_logs.occur_date`` 归集 —— 这就是"每日跑步的打卡记录"。
          打卡记为按时/迟到完成；计划内但没打卡、且当天已过 ``time_end`` 的记为逾期未完成。
        """
        first_day = last_n_days(days)[0]
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT substr(created_at, 1, 10) AS day, completed, due_at, completed_at
                FROM tasks
                WHERE substr(created_at, 1, 10) >= ?
                  AND COALESCE(task_type, 'once') = 'once'
                """,
                (first_day,),
            ).fetchall()

        buckets: dict[str, dict[str, int]] = {
            day: {"on_time": 0, "late": 0, "overdue": 0} for day in last_n_days(days)
        }
        now = datetime.now()
        for row in rows:
            day = row["day"]
            bucket = buckets.get(day)
            if bucket is None:
                continue
            due_raw, done_raw = row["due_at"], row["completed_at"]
            due = _parse_ts(due_raw)
            done = _parse_ts(done_raw)

            if row["completed"]:
                if due is None or (done is not None and done <= due):
                    bucket["on_time"] += 1
                else:
                    bucket["late"] += 1
            elif due is not None and due < now:
                bucket["overdue"] += 1
            else:
                # 未完成但未超期：不计入（避免把“还早”的任务算成失败）
                continue

        # ---- 叠加周期任务的打卡记录（每日跑步这类就看这里）----
        recurring = self.recurring_tasks()
        if recurring:
            logs = self.task_logs_since(first_day)
            done_keys: set[tuple[int, str]] = set()
            for log_row in logs:
                bucket = buckets.get(log_row.occur_date)
                if bucket is None:
                    continue
                if not log_row.completed:
                    continue
                done_keys.add((log_row.task_id, log_row.occur_date))
                if log_row.was_late:
                    bucket["late"] += 1
                else:
                    bucket["on_time"] += 1
            # 计划内、没打卡、且当天已过结束时间 → 逾期未完成
            for day in buckets:
                try:
                    day_date = datetime.strptime(day, "%Y-%m-%d").date()
                except ValueError:
                    continue
                for task in recurring:
                    # 任务创建之前的日子不算逾期：否则今天刚建的每日任务会
                    # 立刻"回溯"出 6 天逾期，图表和总览都很难看
                    if task.created_at and day_date < task.created_at.date():
                        continue
                    if not task.rule.occurs_on(day_date):
                        continue
                    if (task.id, day) in done_keys:
                        continue
                    window = task.occurrence_on(day_date)
                    if window is not None and window.end < now:
                        buckets[day]["overdue"] += 1

        # ---- 叠加累计打卡任务的打卡记录（"累计打卡的完成情况"就看这里）----
        # 每一次打卡都算一次"按时完成"（累计任务没有迟到一说：截止日之前哪天打都算），
        # 逾期只在**截止日那一天**记一笔 —— 记在每一天会把图表刷成一片红，
        # 而且是同一个失败被数了很多次。
        first_day_date = datetime.strptime(first_day, "%Y-%m-%d").date()
        today = now.date()
        for log_row in self.checkin_logs_between(first_day_date, today):
            bucket = buckets.get(log_row.checkin_date)
            if bucket is not None:
                bucket["on_time"] += 1
        for task in self.cumulative_tasks():
            deadline = task.rule.deadline
            if deadline is None or deadline >= today:
                continue                     # 还没到截止日 → 绝不记逾期
            target = max(1, int(task.rule.target_count or 1))
            if self.checkin_count(task.id) >= target:
                continue                     # 已经达标（含压哨）
            key = deadline.strftime("%Y-%m-%d")
            if key in buckets:
                buckets[key]["overdue"] += 1  # 只在截止日那天记一笔"没达标"
        return [TaskDayStat(day, **buckets[day]) for day in last_n_days(days)]

    def task_overview(self, days: int = 7) -> dict[str, int]:
        """近 N 天任务总览：按时完成 / 超期完成 / 逾期未完成 / 未到期。"""
        stats = self.task_stats_by_day(days)
        counts = self.task_counts()
        overdue = counts.overdue
        on_time = sum(s.on_time for s in stats)
        late = sum(s.late for s in stats)
        return {
            "on_time": on_time,
            "late": late,
            "overdue": overdue,
            "pending_future": max(0, counts.pending - overdue),
            "total": counts.total,
        }

    # ------------------------------------------------------------------ 维护
    def reset_day(self, day: str | None = None) -> None:
        """清空某天的记录（界面上的“清零今日统计”）。"""
        day = day or today_str()
        with self._lock:
            try:
                self._conn.execute("DELETE FROM daily_target WHERE day = ?", (day,))
                self._conn.execute("DELETE FROM tick_log WHERE day = ?", (day,))
                self._conn.execute("DELETE FROM hourly WHERE day = ?", (day,))
                self._conn.commit()
            except sqlite3.Error as exc:
                log.warning("清空记录失败: %s", exc)

    def purge_old(self, keep_days: int | None = None) -> None:
        """清理过期的明细流水（图表用的 daily_target 不删）。"""
        keep = int(keep_days or self.RETENTION_DAYS)
        with self._lock:
            try:
                self._conn.execute(
                    "DELETE FROM tick_log WHERE day < date('now', 'localtime', ?)", (f"-{keep} day",)
                )
                self._conn.commit()
            except sqlite3.Error as exc:
                log.warning("清理历史数据失败: %s", exc)

    def export_csv(self, csv_path: Path, days: int = 30) -> int:
        """导出最近 N 天使用明细 + 任务定义 + 周期任务打卡记录到 CSV。

        用 UTF-8-**SIG** 写：不带 BOM 的话 Excel 会把中文当乱码（老问题）。

        周期任务单独分两段：
        * 【任务定义】每行一条规则，``是否完成/完成时间`` 对周期任务留 "—" —— 周期任务
          的"做完没有"是**按天**的，塞进一行里没有意义（这正是升级前的错误行为：
          周期任务被导出成"无期限、未完成"）；
        * 【周期任务打卡记录】每行 = 某任务某一天，这才是"今天做了没有"的真相。
        """
        import csv

        start = last_n_days(days)[0]
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT day, target_name, target_kind, SUM(seconds) AS s
                FROM daily_target WHERE day >= ?
                GROUP BY day, target_name, target_kind ORDER BY day ASC, s DESC
                """,
                (start,),
            ).fetchall()
            task_rows = self._conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC"
            ).fetchall()
        logs = self.task_logs_since(start)
        tasks = [Task.from_row(r) for r in task_rows]
        title_by_id = {task.id: task for task in tasks}

        with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["【使用时长】"])
            writer.writerow(["日期", "对象", "类型", "时长(分钟)"])
            for r in rows:
                writer.writerow([r["day"], r["target_name"], r["target_kind"], round(float(r["s"] or 0) / 60, 2)])
            writer.writerow([])

            writer.writerow([f"【任务定义】（最近 {days} 天创建/在用的任务）"])
            writer.writerow(["ID", "任务内容", "类型", "计划 / 截止", "创建时间",
                             "是否完成", "完成时间", "状态", "备注"])
            for task in tasks:
                if task.is_cumulative:
                    # 累计打卡任务：完成情况看 check_in_logs，"是否完成"用达成/未达成表达，
                    # 并且**绝不**在截止日之前写"逾期"（用户的核心诉求）。
                    status = cumulative_status(task.rule, self.checkin_dates(task.id))
                    writer.writerow([
                        task.id, task.title, task.type_label, task.rule.checkin_schedule_text(),
                        task.created_at.strftime("%Y-%m-%d %H:%M"),
                        "是" if status.finished else "否",
                        status.reached_on.strftime("%Y-%m-%d") if status.reached_on else "",
                        f"{status.state_label}：{status.progress_text()}，{status.countdown_text()}",
                        task.note,
                    ])
                    continue
                if task.is_recurring:
                    # 周期任务：完成情况按天记在 task_logs，不能在这一行里下结论
                    writer.writerow([
                        task.id, task.title, task.type_label, task.rule.schedule_text(),
                        task.created_at.strftime("%Y-%m-%d %H:%M"),
                        "—", "", "周期任务（完成情况见下方打卡记录）", task.note,
                    ])
                    continue
                if not task.completed:
                    status = "已超期" if task.is_overdue() else "进行中"
                else:
                    status = "超期完成" if task.was_late() else "按时完成"
                writer.writerow([
                    task.id, task.title, task.type_label,
                    task.due_at.strftime("%Y-%m-%d %H:%M") if task.due_at else "无期限",
                    task.created_at.strftime("%Y-%m-%d %H:%M"),
                    "是" if task.completed else "否",
                    task.completed_at.strftime("%Y-%m-%d %H:%M") if task.completed_at else "",
                    status, task.note,
                ])

            writer.writerow([])
            writer.writerow([f"【周期任务打卡记录】（最近 {days} 天）"])
            writer.writerow(["日期", "任务", "类型", "是否打卡", "打卡时间", "是否迟到",
                             "提醒时间", "提醒类型", "备注"])
            for log_row in logs:
                task = title_by_id.get(log_row.task_id)
                writer.writerow([
                    log_row.occur_date,
                    task.title if task else f"(已删除的任务 #{log_row.task_id})",
                    task.type_label if task else "",
                    "是" if log_row.completed else "否",
                    log_row.completed_at.strftime("%Y-%m-%d %H:%M") if log_row.completed_at else "",
                    "是" if log_row.was_late else "",
                    log_row.remind_at.strftime("%Y-%m-%d %H:%M") if log_row.remind_at else "",
                    log_row.kind_label,
                    log_row.note,
                ])

            writer.writerow([])
            writer.writerow([f"【累计打卡记录】（最近 {days} 天）"])
            writer.writerow(["日期", "任务", "类型", "打卡时间", "来源", "备注"])
            for log_row in self.checkin_logs_between(
                    datetime.strptime(start, "%Y-%m-%d").date(), date.today()):
                task = title_by_id.get(log_row.task_id)
                writer.writerow([
                    log_row.checkin_date,
                    task.title if task else f"(已删除的任务 #{log_row.task_id})",
                    task.type_label if task else "",
                    log_row.checkin_at.strftime("%Y-%m-%d %H:%M:%S") if log_row.checkin_at else "",
                    log_row.source_label,
                    log_row.note,
                ])
        return len(rows) + len(task_rows) + len(logs)

    def close(self) -> None:
        """关闭数据库连接。"""
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

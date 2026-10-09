"""周期性任务的时间规则：纯函数，不碰数据库、不碰界面，方便单独测试。

设计约定
--------
1. **规则与发生记录分离**
   ``tasks`` 表只存"这个任务是什么、什么时候该做"（规则），
   "哪一天做了没有"记在 ``task_logs``（发生记录）。周期任务**永远不写**
   ``tasks.completed``，否则"今天完成"会变成"永久完成"。
2. **一次发生 = 一个日期 + 一个时间窗**
   每日任务：每一天都有一个 (start, end) 窗口。
   每周任务：只有 ``days_of_week`` 里列出的星期才有窗口。
   单次任务：沿用 ``due_at``，不走本模块。
3. **星期编码为 ISO**：1=周一 … 7=周日（``datetime.isoweekday()`` 同口径），
   在库里存成 ``"1,3,5"`` 这种可读字符串。
4. **不轮询**：对外只提供 ``next_occurrence`` / ``previous_occurrence`` 这类
   "算出确切时刻"的纯函数，调度器据此排一个定时器即可。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

log = logging.getLogger(__name__)

#: 任务类型
TYPE_ONCE = "once"
TYPE_DAILY = "daily"
TYPE_WEEKLY = "weekly"
TASK_TYPES = (TYPE_ONCE, TYPE_DAILY, TYPE_WEEKLY)

TYPE_LABELS = {TYPE_ONCE: "单次", TYPE_DAILY: "每天", TYPE_WEEKLY: "每周"}

#: 星期中文名（ISO：1=周一）
WEEKDAY_CN = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}

#: 提前提醒的默认与取值范围（分钟）
DEFAULT_REMIND_BEFORE = 15
MIN_REMIND_BEFORE = 0
MAX_REMIND_BEFORE = 24 * 60

#: 默认时间窗（用户没填时用）
DEFAULT_START = "18:00"
DEFAULT_END = "20:00"

#: 开机补发窗口（分钟）：错过了提前提醒时，最多在开机后这么久内补发一次
DEFAULT_LATE_CATCHUP_MINUTES = 5


def coerce_iso_weekdays(days) -> tuple[int, ...]:
    """把各种输入统一成 ``(1, 3, 5)``。

    接受 ``"1,3,5"``（含中文逗号）、``[3, 1]``、``(1, 3)``、``None`` 等，
    去重排序、忽略非法值（越界 / 非数字）。
    """
    if days is None:
        return ()
    if isinstance(days, str):
        chunks = days.replace("，", ",").split(",")
    elif isinstance(days, (int, float)):
        chunks = [str(int(days))]
    else:
        try:
            chunks = [str(item) for item in days]
        except TypeError:
            return ()
    values: set[int] = set()
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            value = int(chunk)
        except ValueError:
            continue
        if 1 <= value <= 7:
            values.add(value)
    return tuple(sorted(values))


def parse_iso_weekdays(raw: str | None) -> tuple[int, ...]:
    """把 ``"1,3,5"`` 解析成 ``(1, 3, 5)``（去重、排序、忽略非法值）。"""
    return coerce_iso_weekdays(raw)


def _parse_date(raw) -> date | None:
    """解析 ``"2026-10-09"``（也容忍 ``"2026-10-09 08:00:00"`` 这种带时间的写法）。"""
    if not raw:
        return None
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    text = str(raw).strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def format_iso_weekdays(days) -> str:
    """把 ``"3,4"`` / ``(1, 3, 5)`` / ``[3, 1]`` 归一化成 ``"1,3,5"``。"""
    parsed = coerce_iso_weekdays(days)
    return ",".join(str(d) for d in parsed)


def parse_hhmm(raw: str | None, fallback: str) -> time:
    """解析 ``"HH:MM"``；允许 ``"H:MM"`` / ``"HH:MM:SS"``，失败返回 ``fallback``。"""
    for text in (raw, fallback):
        if not text:
            continue
        parts = str(text).strip().split(":")
        if len(parts) < 2:
            continue
        try:
            hour, minute = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return time(hour, minute)
    return time(18, 0)


def format_hhmm(value: time) -> str:
    """``time`` → ``"HH:MM"``。"""
    return f"{value.hour:02d}:{value.minute:02d}"


def weekdays_text(days) -> str:
    """``(1,3,5)`` → ``"周一、周三、周五"``；每天/无则给相应文案。"""
    parsed = parse_iso_weekdays(format_iso_weekdays(days))
    if not parsed:
        return "未设置"
    if len(parsed) == 7:
        return "每天"
    return "、".join(WEEKDAY_CN[d] for d in parsed)


def suggested_start_date(task_type: str, time_start: str, time_end: str,
                         now: datetime | None = None) -> str:
    """新建 / 改时间段时，这条周期任务**从哪天开始生效**（``YYYY-MM-DD``；单次任务返回空串）。

    为什么需要它（用户反馈）：晚上 21 点新建一条「每天 06:00~08:00」跑步任务，
    今天那次显然不可能完成了，可列表却立刻显示成"**今天已超期**" —— 用户的预期是
    "这个任务从明天开始"，而不是一建出来就欠了一笔。

    规则很简单：**今天的时间窗已经过去了，就从明天开始**；否则今天就算。
    只影响"今天这一次算不算数"，之后每天都照常。
    """
    if task_type not in (TYPE_DAILY, TYPE_WEEKLY):
        return ""
    rule = TaskRule(task_type=task_type,
                    time_start=parse_hhmm(time_start, DEFAULT_START),
                    time_end=parse_hhmm(time_end, DEFAULT_END))
    now = now or datetime.now()
    today = now.date()
    if datetime.combine(today, rule.time_end) <= now:
        return (today + timedelta(days=1)).strftime("%Y-%m-%d")
    return today.strftime("%Y-%m-%d")


@dataclass(frozen=True)
class Occurrence:
    """一次具体的"该做这件事"的时间点。"""

    day: date
    start: datetime
    end: datetime

    @property
    def date_str(self) -> str:
        return self.day.strftime("%Y-%m-%d")

    def contains(self, moment: datetime) -> bool:
        """``moment`` 是否落在 [start, end] 内。"""
        return self.start <= moment <= self.end

    def remind_at(self, before_minutes: int) -> datetime:
        """提前提醒应当触发的时刻。"""
        return self.start - timedelta(minutes=max(0, int(before_minutes)))

    def is_late(self, moment: datetime) -> bool:
        """``moment`` 是否已超过结束时间（用于判定"迟到完成"）。"""
        return moment > self.end


@dataclass(frozen=True)
class TaskRule:
    """一条周期任务的调度规则（由 ``tasks`` 表的行构造）。"""

    task_type: str = TYPE_ONCE
    time_start: time = time(18, 0)
    time_end: time = time(20, 0)
    days_of_week: tuple[int, ...] = ()
    remind_before_minutes: int = DEFAULT_REMIND_BEFORE
    #: 从哪一天开始生效（``None`` = 一直有效，老数据就是这种）。
    #: 用来实现"现在才建、时间已过 → 从明天开始"，见 :func:`suggested_start_date`。
    start_date: date | None = None

    def __post_init__(self) -> None:
        """归一化：结束时间不得早于/等于开始时间（否则当天那次会被跳过）。

        放在这里而不是只放在读取侧，是为了让 ``TaskRule.daily()/weekly()`` 这类
        直接构造出来的规则与"从库里读出来的"行为一致（否则预览/测试会骗人）。
        """
        if self.is_recurring and self.time_end <= self.time_start:
            object.__setattr__(self, "time_end", time(23, 59))

    # ---------------------------------------------------------------- 构造
    @classmethod
    def from_row(cls, row) -> "TaskRule":
        """从数据库行构造（兼容缺列的老库）。"""

        def get(name: str, default=None):
            try:
                return row[name]
            except (IndexError, KeyError):
                return default

        task_type = str(get("task_type") or TYPE_ONCE)
        if task_type not in TASK_TYPES:
            task_type = TYPE_ONCE
        raw_start = get("time_start") or DEFAULT_START
        raw_end = get("time_end") or DEFAULT_END
        start = parse_hhmm(raw_start, DEFAULT_START)
        end = parse_hhmm(raw_end, DEFAULT_END)
        if end <= start:                     # 跨零点或误填：按"到当天结束"处理
            end = time(23, 59)
        try:
            before = int(get("remind_before_minutes"))
        except (TypeError, ValueError):
            before = DEFAULT_REMIND_BEFORE
        return cls(
            task_type=task_type,
            time_start=start,
            time_end=end,
            days_of_week=parse_iso_weekdays(get("days_of_week")),
            remind_before_minutes=max(MIN_REMIND_BEFORE, min(MAX_REMIND_BEFORE, before)),
            start_date=_parse_date(get("start_date")),
        )

    @classmethod
    def daily(cls, start: str = DEFAULT_START, end: str = DEFAULT_END,
              before: int = DEFAULT_REMIND_BEFORE,
              start_date: date | None = None) -> "TaskRule":
        return cls(TYPE_DAILY, parse_hhmm(start, DEFAULT_START),
                   parse_hhmm(end, DEFAULT_END), (), before, start_date)

    @classmethod
    def weekly(cls, days, start: str = DEFAULT_START, end: str = DEFAULT_END,
               before: int = DEFAULT_REMIND_BEFORE,
               start_date: date | None = None) -> "TaskRule":
        return cls(TYPE_WEEKLY, parse_hhmm(start, DEFAULT_START),
                   parse_hhmm(end, DEFAULT_END), parse_iso_weekdays(format_iso_weekdays(days)),
                   before, start_date)

    # ---------------------------------------------------------------- 查询
    @property
    def is_recurring(self) -> bool:
        """是否为周期任务（每日 / 每周）。"""
        return self.task_type in (TYPE_DAILY, TYPE_WEEKLY)

    @property
    def type_label(self) -> str:
        return TYPE_LABELS.get(self.task_type, self.task_type)

    def occurs_on(self, day: date) -> bool:
        """``day`` 这一天是否需要做这件事（生效日之前一律为否）。"""
        if self.start_date is not None and day < self.start_date:
            return False
        if self.task_type == TYPE_DAILY:
            return True
        if self.task_type == TYPE_WEEKLY:
            return day.isoweekday() in self.days_of_week
        return False

    def starts_later_than(self, day: date) -> bool:
        """生效日是否晚于 ``day``（界面用来提示"从明天开始"）。"""
        return self.start_date is not None and self.start_date > day

    def window_on(self, day: date) -> Occurrence | None:
        """``day`` 这一天的发生窗口（不在计划内返回 ``None``）。"""
        if not self.occurs_on(day):
            return None
        return Occurrence(day,
                          datetime.combine(day, self.time_start),
                          datetime.combine(day, self.time_end))

    def schedule_text(self) -> str:
        """给界面用的一句话描述，例如 ``每天 18:00~20:00``。"""
        span = f"{format_hhmm(self.time_start)}~{format_hhmm(self.time_end)}"
        if self.task_type == TYPE_DAILY:
            text = f"每天 {span}"
        elif self.task_type == TYPE_WEEKLY:
            text = f"{weekdays_text(self.days_of_week)} {span}"
        else:
            return "单次"
        if self.start_date is not None and self.start_date > date.today():
            # 生效日还没到（例如晚上才建的"每天早上 6 点"）：说清楚从哪天开始
            text += f"（{self.start_date:%m-%d} 开始）"
        return text

    # ---------------------------------------------------------------- 推算
    NEXT_SEARCH_DAYS = 15          # 每周任务最多往后找 15 天（够覆盖 7 天周期）

    def next_occurrence(self, now: datetime) -> Occurrence | None:
        """严格晚于 ``now`` 的下一次发生（用于排定"下一次提醒"）。

        * 今天还没结束的窗口 → 返回今天；
        * 今天已结束或今天不在计划内 → 往后逐天找；
        * 超过 ``NEXT_SEARCH_DAYS`` 天仍没有（理论上不会）→ 返回 ``None``。
        """
        if not self.is_recurring:
            return None
        for offset in range(self.NEXT_SEARCH_DAYS + 1):
            day = (now + timedelta(days=offset)).date()
            window = self.window_on(day)
            if window is None:
                continue
            if window.end > now:
                return window
        return None

    def current_or_next_occurrence(self, now: datetime) -> Occurrence | None:
        """包含"正在进行中"的那次发生：窗口还没结束就返回今天那次。

        调度器用这个判断"现在是否处在某个窗口内"，与
        :meth:`next_occurrence` 的区别是它**包含已经开始但未结束的窗口**。
        """
        if not self.is_recurring:
            return None
        window = self.window_on(now.date())
        if window is not None and window.end >= now:
            return window
        return self.next_occurrence(now)

    def previous_occurrence(self, now: datetime) -> Occurrence | None:
        """最近一次已经**结束**的发生（用于开机补发判断）。"""
        if not self.is_recurring:
            return None
        for offset in range(self.NEXT_SEARCH_DAYS + 1):
            day = (now - timedelta(days=offset)).date()
            window = self.window_on(day)
            if window is None:
                continue
            if window.end < now:
                return window
        return None

    def occurrences_between(self, start_dt: datetime, end_dt: datetime) -> list[Occurrence]:
        """``[start_dt, end_dt]`` 区间内的所有发生（按时间正序）。"""
        if not self.is_recurring or end_dt < start_dt:
            return []
        result: list[Occurrence] = []
        day = start_dt.date()
        last_day = end_dt.date()
        while day <= last_day:
            window = self.window_on(day)
            if window is not None and window.end >= start_dt and window.start <= end_dt:
                result.append(window)
            day += timedelta(days=1)
        return result

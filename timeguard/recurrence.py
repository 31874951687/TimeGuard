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
#: 累计打卡任务：在最终截止日期前攒够 N 次（例如"年末前完成 60 次两公里跑"）。
#: 它跟每天/每周最大的区别是**没有"今天必须做"的概念** —— 哪一天打卡由用户自己决定，
#: 系统只负责每天到点提醒一次、记录次数、算进度，**在截止日之前绝不能说"逾期"**。
TYPE_CUMULATIVE = "cumulative"
TASK_TYPES = (TYPE_ONCE, TYPE_DAILY, TYPE_WEEKLY, TYPE_CUMULATIVE)

TYPE_LABELS = {TYPE_ONCE: "单次", TYPE_DAILY: "每天", TYPE_WEEKLY: "每周",
               TYPE_CUMULATIVE: "累计打卡"}

#: 累计打卡任务的状态（用户的核心诉求：截止日之前只有"进行中"，没有"逾期"）
STATE_RUNNING = "running"            # 进行中：还没到截止日，也没攒够次数
STATE_DONE_EARLY = "done_early"      # 提前完成：截止日之前就攒够了
STATE_DONE_DEADLINE = "done"         # 压哨完成：截止日当天攒够（当天 23:59 打卡都算）
STATE_MISSED = "missed"              # 逾期未达标：**今天 > 截止日** 且次数不够

STATE_LABELS = {
    STATE_RUNNING: "进行中",
    STATE_DONE_EARLY: "提前完成",
    STATE_DONE_DEADLINE: "压哨完成",
    STATE_MISSED: "逾期未达标",
}

#: 已完成（不论提前还是压哨）—— 到这个状态就必须停掉每日提醒（避坑 #1）
STATE_FINISHED = (STATE_DONE_EARLY, STATE_DONE_DEADLINE)

#: 累计打卡任务的默认值
DEFAULT_TARGET_COUNT = 30
MAX_TARGET_COUNT = 9999
DEFAULT_REMIND_TIME = "18:00"

#: "监控时长达标就自动打卡"（可选功能）的默认与取值范围（分钟）
DEFAULT_AUTO_MINUTES = 30
MIN_AUTO_MINUTES = 1
MAX_AUTO_MINUTES = 24 * 60

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


@dataclass(frozen=True)
class CumulativeStatus:
    """累计打卡任务的进度与状态（**纯数据**，由 :func:`cumulative_status` 算出）。

    界面只认这个对象，不许自己再写一套判断 —— 否则"列表说进行中、图表说逾期"
    这种自相矛盾迟早会出现。
    """

    total: int                     # 已打卡次数
    target: int                    # 目标次数
    state: str                     # STATE_* 之一
    deadline: date | None = None   # 最终截止日期
    today: date | None = None      # 计算时的"今天"（测试用）
    today_checked: bool = False    # 今天是否已经打卡
    reached_on: date | None = None # 达成目标的那一天（没达成为 None）

    @property
    def remaining(self) -> int:
        """还差几次。"""
        return max(0, self.target - self.total)

    @property
    def finished(self) -> bool:
        """是否已达成目标（提前完成 / 压哨完成都算）。"""
        return self.state in STATE_FINISHED

    @property
    def missed(self) -> bool:
        """是否已判定逾期未达标。"""
        return self.state == STATE_MISSED

    @property
    def is_running(self) -> bool:
        return self.state == STATE_RUNNING

    @property
    def days_left(self) -> int | None:
        """距截止日还有几天（今天 = 0；已过期为负数；没有截止日返回 None）。"""
        if self.deadline is None or self.today is None:
            return None
        return (self.deadline - self.today).days

    @property
    def ratio(self) -> float:
        """完成比例 0.0 ~ 1.0（给进度条用）。"""
        return min(1.0, self.total / self.target) if self.target else 0.0

    @property
    def percent(self) -> int:
        """完成百分比（整数，给文字用）。"""
        return int(round(self.ratio * 100))

    @property
    def state_label(self) -> str:
        return STATE_LABELS.get(self.state, self.state)

    def status_text(self) -> str:
        """任务列表里"状态"那一列的文案。

        硬性要求（用户原话）：**截止日期到来之前，绝不能出现"逾期"或"失败"字样**，
        只能显示"今日未打卡"或"距离目标还差 X 次"，状态保持"进行中"。
        """
        if self.state == STATE_RUNNING:
            head = "✓ 今日已打卡" if self.today_checked else "○ 今日未打卡"
            if self.remaining > 0:
                return f"{head} · 还差 {self.remaining} 次"
            return head
        if self.state == STATE_DONE_EARLY:
            when = f"（{self.reached_on:%m-%d} 达标）" if self.reached_on else ""
            return f"★ 提前完成{when}"
        if self.state == STATE_DONE_DEADLINE:
            return "★ 压哨完成（截止日当天达标）"
        return f"✗ 逾期未达标（差 {self.remaining} 次）"

    def countdown_text(self) -> str:
        """"剩余 / 超期"那一列的文案。"""
        if self.state == STATE_MISSED:
            over = abs(self.days_left or 0)
            return f"已过期 {over} 天"
        if self.finished:
            return "已达成目标"
        left = self.days_left
        if left is None:
            return "无截止日期"
        if left == 0:
            return "今天是最后一天"
        return f"还剩 {left} 天"

    def progress_text(self) -> str:
        """``已打卡 45/60 次（75%）``。"""
        return f"已打卡 {self.total}/{self.target} 次（{self.percent}%）"


def cumulative_status(rule: TaskRule, checkin_dates, today: date | None = None) -> CumulativeStatus:
    """算出累计打卡任务此刻的进度与状态（纯函数，不碰数据库）。

    ``checkin_dates`` 是这条任务**已打卡的自然日**列表（可以乱序、可重复，内部会去重）。

    判定规则（严格按用户的核心诉求，也是避坑 #5）::

        今天 > 截止日 且 已打卡 < 目标   → 逾期未达标（唯一会显示"逾期"的情况）
        已打卡 >= 目标，达成日 <  截止日 → 提前完成（并停止后续提醒）
        已打卡 >= 目标，达成日 >= 截止日 → 压哨完成（截止日当天 23:59 打卡也算完成）
        其余（含截止日当天还没攒够）     → 进行中，只显示"今日未打卡 / 还差 X 次"

    注意"达成日"取的是**第 N 次打卡那一天**（N = 目标次数），不是"最后一次打卡"：
    第 61、62 次补打卡不会把"提前完成"改成"压哨完成"。
    """
    today = today or date.today()
    target = max(1, int(rule.target_count or 1))
    days = sorted({d for d in checkin_dates if isinstance(d, date)})
    total = len(days)
    reached_on = days[target - 1] if total >= target else None

    if reached_on is not None:
        if rule.deadline is not None and reached_on >= rule.deadline:
            state = STATE_DONE_DEADLINE
        else:
            state = STATE_DONE_EARLY
    elif rule.deadline is not None and today > rule.deadline:
        state = STATE_MISSED
    else:
        state = STATE_RUNNING

    return CumulativeStatus(total=total, target=target, state=state,
                            deadline=rule.deadline, today=today,
                            today_checked=today in days, reached_on=reached_on)


def auto_checkin_ready(rule: TaskRule, seconds_today: float, *,
                       checked_today: bool = False, today: date | None = None) -> tuple[bool, str]:
    """判断"监控时长够了，该自动打卡了"（纯函数，方便单测）。

    必须**全部**满足才自动打卡：

    1. 这条任务开了自动打卡（配了 ``auto_target`` 与阈值）；
    2. 今天还没打过卡 —— 同一天只能打一次卡，自动打卡也不能例外；
    3. 任务还没达标、也没过期（与手动提醒同一套口径，用的还是
       :func:`cumulative_status` 的状态机）；
    4. 今天的监控时长已经达到阈值。

    返回 ``(是否可以打卡, 说明)``；说明直接给界面/备注用。
    """
    if not rule.auto_enabled:
        return False, "这条任务没有开启「监控时长达标自动打卡」"
    today = today or date.today()
    if rule.is_expired(today):
        return False, f"已过截止日期（{rule.deadline:%Y-%m-%d}），不再自动打卡"
    threshold = (rule.auto_minutes or DEFAULT_AUTO_MINUTES) * 60
    minutes = max(0.0, float(seconds_today or 0.0)) / 60
    if checked_today:
        return False, "今天已经打过卡了（同一天只算一次）"
    if seconds_today < threshold:
        need = (threshold - max(0.0, float(seconds_today or 0.0))) / 60
        return False, (f"{rule.auto_target} 今日已用 {minutes:.0f} 分钟，"
                       f"还差 {max(1, int(need + 0.999))} 分钟自动打卡")
    return True, (f"{rule.auto_target} 今日已用 {minutes:.0f} 分钟"
                  f"（≥ {threshold // 60} 分钟），自动打卡")


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
    # ---- 以下是"累计打卡"专用（其它类型一律为 None）----
    #: 总目标次数（例如 60 次）
    target_count: int | None = None
    #: 最终截止日期（含当天：截止日 23:59 打卡仍然算数，见 :func:`cumulative_status`）
    deadline: date | None = None
    #: 每日提醒时间（一个时间点，不是时间窗）
    remind_time: time | None = None
    #: 【可选】监控对象名（如"高数"）：今日累计时长达到 auto_minutes 就自动打卡
    auto_target: str | None = None
    #: 【可选】自动打卡的时长阈值（分钟）
    auto_minutes: int | None = None

    def __post_init__(self) -> None:
        """归一化：结束时间不得早于/等于开始时间（否则当天那次会被跳过）。

        放在这里而不是只放在读取侧，是为了让 ``TaskRule.daily()/weekly()`` 这类
        直接构造出来的规则与"从库里读出来的"行为一致（否则预览/测试会骗人）。
        """
        if self.is_recurring and self.time_end <= self.time_start:
            object.__setattr__(self, "time_end", time(23, 59))
        if self.is_cumulative:
            # 目标次数至少 1，否则"攒够 0 次"会立刻变成已完成
            target = self.target_count if self.target_count else DEFAULT_TARGET_COUNT
            object.__setattr__(self, "target_count", max(1, min(MAX_TARGET_COUNT, int(target))))

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
        try:
            target = int(get("target_count"))
        except (TypeError, ValueError):
            target = None
        try:
            auto_minutes = int(get("auto_minutes"))
        except (TypeError, ValueError):
            auto_minutes = None
        auto_target = (get("auto_target") or "").strip() or None
        return cls(
            task_type=task_type,
            time_start=start,
            time_end=end,
            days_of_week=parse_iso_weekdays(get("days_of_week")),
            remind_before_minutes=max(MIN_REMIND_BEFORE, min(MAX_REMIND_BEFORE, before)),
            start_date=_parse_date(get("start_date")),
            target_count=target,
            deadline=_parse_date(get("deadline")),
            remind_time=(parse_hhmm(get("remind_time"), DEFAULT_REMIND_TIME)
                         if get("remind_time") else None),
            auto_target=auto_target,
            auto_minutes=(None if auto_target is None else
                          max(MIN_AUTO_MINUTES, min(MAX_AUTO_MINUTES,
                                                    auto_minutes or DEFAULT_AUTO_MINUTES))),
        )

    @classmethod
    def cumulative(cls, target_count: int, deadline: date | str | None,
                   remind_time: str = DEFAULT_REMIND_TIME,
                   auto_target: str | None = None,
                   auto_minutes: int | None = None) -> "TaskRule":
        """构造一条累计打卡规则（界面与测试都用这个，保证跟库里的读法一致）。"""
        deadline_date = _parse_date(deadline) if not isinstance(deadline, date) else deadline
        return cls(task_type=TYPE_CUMULATIVE,
                   target_count=max(1, min(MAX_TARGET_COUNT, int(target_count))),
                   deadline=deadline_date,
                   remind_time=parse_hhmm(remind_time, DEFAULT_REMIND_TIME),
                   auto_target=(auto_target or "").strip() or None,
                   auto_minutes=auto_minutes)

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
    def is_cumulative(self) -> bool:
        """是否为累计打卡任务。"""
        return self.task_type == TYPE_CUMULATIVE

    @property
    def type_label(self) -> str:
        return TYPE_LABELS.get(self.task_type, self.task_type)

    # ------------------------------------------------- 累计打卡：提醒与期限
    def checkin_reminder_on(self, day: date) -> datetime:
        """``day`` 这一天的"该打卡了"提醒时刻。"""
        return datetime.combine(day, self.remind_time or time(18, 0))

    def is_expired(self, today: date) -> bool:
        """**已经过了**截止日（截止日当天返回 False —— 当天仍可打卡）。"""
        return self.deadline is not None and today > self.deadline

    def next_checkin_reminder(self, now: datetime, *, day_done: bool = False,
                              finished: bool = False) -> datetime | None:
        """严格晚于 ``now`` 的下一次"该打卡了"提醒；不该再提醒时返回 ``None``。

        这就是避坑 #1 要求的"提前完成后停止骚扰"：
        ``finished=True``（已攒够次数）或已过截止日 → 直接 ``None``，调度器不会再排定时器。
        当天已经打过卡 → 下一次顺延到明天（不再提醒今天）。
        """
        if finished or self.deadline is None:
            return None
        today = now.date()
        if self.is_expired(today):
            return None
        for offset in range(0, 3):                 # 今天 / 明天 / 后天，足够跨过"今天已打卡"
            day = today + timedelta(days=offset)
            if self.is_expired(day):
                return None
            if offset == 0 and day_done:
                continue
            moment = self.checkin_reminder_on(day)
            if moment > now:
                return moment
        return None

    def checkin_schedule_text(self) -> str:
        """累计打卡任务的一句话描述，例如 ``累计打卡 60 次 · 截止 12-31 · 每天 18:00 提醒``。"""
        target = self.target_count or 1
        text = f"累计打卡 {target} 次"
        if self.deadline is not None:
            text += f" · 截止 {self.deadline:%Y-%m-%d}"
        text += f" · 每天 {format_hhmm(self.remind_time or time(18, 0))} 提醒"
        return text

    @property
    def auto_enabled(self) -> bool:
        """是否开启了"监控时长达标自动打卡"。"""
        return bool(self.auto_target) and self.is_cumulative

    def auto_checkin_text(self) -> str:
        """自动打卡的一句话说明（没开启时返回空串）。"""
        if not self.auto_enabled:
            return ""
        return f"自动打卡：{self.auto_target} 今日累计满 {self.auto_minutes or DEFAULT_AUTO_MINUTES} 分钟"

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
        if self.is_cumulative:
            return self.checkin_schedule_text()
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

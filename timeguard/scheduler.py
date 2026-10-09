"""周期性任务的提醒调度器。

设计目标（对应"避坑指南"第 3、5 条）
------------------------------------
1. **不轮询**：不写 ``while True``、不做"每秒遍历全表"。
   调度器只做一件事：算出"下一次该响的准确时刻"，然后排**一个** ``after`` 定时器。
   * :func:`compute_schedule` 是**纯函数**（不碰 Tk、不碰数据库），负责算时刻；
   * :class:`TaskScheduler` 只负责"等到点 → 派发 → 重排"。
2. **弹窗不风暴**：同一批到点的任务**合并成一个** ``ReminderBatch``，
   调用方只收到一次回调，弹一个窗、发一条通知。
3. **休眠/改时钟健壮**：定时器单次最长睡眠 ``scheduler_max_sleep_minutes``
   （默认 30 分钟）。即使系统休眠、时钟被改，醒来后重新计算即可纠正，
   不依赖"一次排到明天"的长睡眠。
4. **错过补发**（阶段 3 核心）：开机后若某个提前提醒已经错过、且仍在
   ``recurring_catchup_minutes``（默认 5 分钟）窗口内，补发一次并标记为
   ``late``（迟到提醒）；超过窗口就不再打扰。

去重口径
--------
"哪一次发生"由 **occur_date + 当前时刻** 唯一确定：

* 提前提醒窗口 ``[S - remind_before, S)`` 与当前时刻同一天，用**今天**做键；
* 迟到补发窗口 ``[S, S + catchup]`` 同理；
* 一旦进入这两个窗口，该次发生就不再改期（明天才有新的 occurrence），
  所以用今天做键是稳定的，不会重复打扰。

每个任务是否已经提醒过，看 ``task_logs(task_id, occur_date).remind_at`` ——
``UNIQUE(task_id, occur_date)`` 保证一次发生最多一条记录。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .database import Task, TaskLog, UsageStore
from .recurrence import DEFAULT_LATE_CATCHUP_MINUTES

log = logging.getLogger(__name__)

#: 提醒类型：正常提前 / 迟到补发
KIND_ADVANCE = "advance"
KIND_LATE = "late"

#: 定时器最短与最长睡眠（毫秒）
MIN_SLEEP_MS = 1000
MAX_SLEEP_MS = 24 * 60 * 60 * 1000


@dataclass(frozen=True)
class ReminderItem:
    """一条"该提醒了"的待办事项。"""

    task: Task
    occur: object                      # recurrence.Occurrence
    remind_at: datetime                # 本该提醒的时刻
    kind: str                          # advance / late
    overdue_minutes: int = 0           # 迟到补发时：已经过了多少分钟

    @property
    def title(self) -> str:
        return self.task.title

    @property
    def is_late(self) -> bool:
        return self.kind == KIND_LATE

    @property
    def window_text(self) -> str:
        """``'18:00~20:00'``。"""
        return f"{self.occur.start:%H:%M}~{self.occur.end:%H:%M}"

    @property
    def kind_label(self) -> str:
        if self.kind == KIND_LATE:
            return f"迟到提醒（已过 {self.overdue_minutes} 分钟）"
        return "即将开始"


@dataclass
class ReminderBatch:
    """一批同时到点的提醒（合并成一个弹窗 / 一条通知）。"""

    items: list[ReminderItem] = field(default_factory=list)
    fired_at: datetime = field(default_factory=datetime.now)

    @property
    def count(self) -> int:
        return len(self.items)

    @property
    def has_late(self) -> bool:
        return any(item.is_late for item in self.items)

    @property
    def titles(self) -> list[str]:
        return [item.title for item in self.items]

    def headline(self) -> str:
        """弹窗标题用的一句话。"""
        if self.count == 1:
            return f"即将开始：{self.items[0].title}"
        return f"{self.count} 项任务即将开始"

    def summary_lines(self) -> list[str]:
        """列表展示用的多行文本。"""
        lines = []
        for index, item in enumerate(self.items, 1):
            prefix = f"{index}. " if self.count > 1 else ""
            lines.append(f"{prefix}{item.title}　{item.window_text}　{item.kind_label}")
        return lines

    def notify_text(self) -> str:
        """系统通知正文（合并成一条）。"""
        if self.count == 1:
            item = self.items[0]
            return f"{item.title}　{item.window_text}"
        head = "\n".join(f"{i}. {item.title}" for i, item in enumerate(self.items[:5], 1))
        if self.count > 5:
            head += f"\n…另外 {self.count - 5} 项"
        return head


@dataclass(frozen=True)
class ScheduleState:
    """调度器算出来的"下一次该做什么"。"""

    batch: ReminderBatch | None = None     # 此刻就该派发的（合并后）
    wakeup_at: datetime | None = None      # 下一次需要唤醒的时刻

    @property
    def has_work(self) -> bool:
        return bool(self.batch and self.batch.items) or self.wakeup_at is not None


def _occur_key(occur) -> str:
    return occur.date_str


def compute_schedule(tasks: list[Task],
                     logs: dict[tuple[int, str], TaskLog],
                     now: datetime,
                     catchup_minutes: int = DEFAULT_LATE_CATCHUP_MINUTES,
                     catchup_enabled: bool = True) -> ScheduleState:
    """纯函数：算出"此刻该派发的提醒"与"下一次唤醒时刻"。

    :param tasks: 候选任务（周期任务；单次任务由原有的临近截止通知负责）
    :param logs: ``{(task_id, occur_date): TaskLog}``，用于判断是否已提醒过
    :param now: 当前时刻（注入以便测试）
    :param catchup_minutes: 迟到补发窗口（分钟）
    :param catchup_enabled: 是否启用迟到补发

    实现要点（容易踩坑）：判定**不能**只看"当前那一次发生"。
    当 ``remind_before`` 很大时（例如 1440 分钟），下一次发生的提醒时刻会落在
    **当前这次发生结束之前**，那时"当前这次"已经提醒过、于是被跳过，
    而下一这次又不在"当前"锚点上 —— 结果提醒被推迟到上个窗口结束（实测晚 2 小时）。
    所以这里统一改成：**从 now 起向后扫描若干次发生，谁进入窗口就派发谁**。
    """
    due: list[ReminderItem] = []
    future: list[datetime] = []
    catchup = timedelta(minutes=max(0, int(catchup_minutes)))

    for task in tasks:
        if not task.is_recurring:
            continue

        before = task.rule.remind_before_minutes
        # 按天推进（而不是靠 next_occurrence 迭代）：即使"今天这次还没结束"，
        # 也必须走到下一天，否则会在同一次发生上打转（实测踩到）。
        found_future = False
        for offset in range(task.rule.NEXT_SEARCH_DAYS + 2):
            day = (now + timedelta(days=offset)).date()
            occur = task.rule.window_on(day)
            if occur is None:
                continue

            key = (task.id, _occur_key(occur))
            already = logs.get(key)
            if already is not None and already.remind_at:
                # 这次已经提醒过 → 直接看下一天那一次（不要用 next_occurrence
                # 反查"下一次"，因为今天窗口还没结束时它会返回**今天自己**，
                # 结果把过去的 remind_at 当成下次唤醒 → 每秒空转）
                continue

            remind_at = occur.remind_at(before)
            # 注意顺序：先判"窗口内该派发"，再判"还没到点"。
            # 提前提醒窗口是 [S - before, S)：
            #   · now < remind_at → 还没到点，排程等它（第 3 个分支）
            #   · remind_at <= now < S → **现在就该派发**（第 1 个分支）
            # 若把"还没到点"放到前面，会出现"到了点却永远不派发"的漏提醒。
            if now < occur.start and remind_at <= now:
                # 1) 提前提醒窗口 [S - before, S)：现在就该提醒
                due.append(ReminderItem(task, occur, remind_at, KIND_ADVANCE))
                break
            if (occur.start <= now <= occur.end
                    and ((catchup_enabled and now <= occur.start + catchup)
                         or remind_at >= now)):
                # 2) 已经到开始时刻：
                #    · 开了迟到补发且在补发窗口内 → 算"迟到提醒"（关机/休眠错过）
                #    · 否则只要 not 还没过提醒时刻（remind_before=0 时 remind_at == start）
                #      就照常提醒 —— 否则"提前 0 分钟 + 关闭补发"会变成整天不提醒
                late = catchup_enabled and now <= occur.start + catchup and remind_at < now
                overdue = max(0, int((now - occur.start).total_seconds() // 60)) if late else 0
                due.append(ReminderItem(task, occur, remind_at,
                                        KIND_LATE if late else KIND_ADVANCE, overdue))
                break
            if remind_at > now:
                # 3) 还没到点：它就是下一次唤醒时刻
                future.append(remind_at)
                found_future = True
                break
            # 4) 这次已经整体过去（例如超过补发窗口）→ 看下一天

        if not found_future and not any(item.task.id == task.id for item in due):
            # 扫完整个搜索窗都没找到未来时刻：兜底排一次，避免"永远不再唤醒"
            future.append(now + timedelta(minutes=1))

    batch = ReminderBatch(due, now) if due else None
    wakeup = min(future) if future else None
    return ScheduleState(batch=batch, wakeup_at=wakeup)


class TaskScheduler:
    """把 :func:`compute_schedule` 接到 Tk 的定时器上（**只有一个** ``after``）。

    线程约定：全部在 Tk 主线程调用（``after`` 回调本来就在主线程）。
    """

    def __init__(self, root, store: UsageStore, on_batch, settings,
                 now_provider=None) -> None:
        """
        :param root: 任意 Tk widget（用它的 ``after`` / ``after_cancel``）
        :param store: 数据层（读取周期任务与发生记录）
        :param on_batch: 回调，签名 ``(ReminderBatch) -> None``，合并后只调用一次
        :param settings: ``AppSettings``（读取补发窗口 / 最长睡眠等）
        :param now_provider: 便于测试注入时间
        """
        self.root = root
        self.store = store
        self.on_batch = on_batch
        self.settings = settings
        self._now = now_provider or datetime.now
        self._after_id: str | None = None
        self._stopped = True
        self._last_wakeup_at: datetime | None = None
        self._fired_count = 0

    # ------------------------------------------------------------------ 生命周期
    def start(self, deferred: bool = False) -> None:
        """启动调度（幂等）。

        :param deferred: 只排程、不派发（见 :meth:`reschedule`）
        """
        self._stopped = False
        self.reschedule(reason="start", deferred=deferred)

    def stop(self) -> None:
        """停止调度并取消定时器。"""
        self._stopped = True
        self._cancel()

    @property
    def running(self) -> bool:
        return not self._stopped

    def _cancel(self) -> None:
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except Exception:  # noqa: BLE001 - 控件已销毁时忽略
                pass
            self._after_id = None

    # ------------------------------------------------------------------ 计算 / 排程
    def _load(self) -> tuple[list[Task], dict[tuple[int, str], TaskLog]]:
        """读取候选任务与"今天 ±1 天"的发生记录。

        只取近几天的记录，避免把历史全表读进来（周期任务的表会一直增长）。
        """
        tasks = self.store.recurring_tasks()
        if not tasks:
            return [], {}
        today = self._now().date()
        logs: dict[tuple[int, str], TaskLog] = {}
        for offset in (-1, 0, 1):
            for log_row in self.store.task_logs_on(today + timedelta(days=offset)):
                logs[(log_row.task_id, log_row.occur_date)] = log_row
        return tasks, logs

    def state(self, now: datetime | None = None) -> ScheduleState:
        """算一次调度状态（不派发、不排程）。"""
        moment = now or self._now()
        tasks, logs = self._load()
        return compute_schedule(
            tasks, logs, moment,
            catchup_minutes=int(getattr(self.settings, "recurring_catchup_minutes",
                                        DEFAULT_LATE_CATCHUP_MINUTES)),
            catchup_enabled=bool(getattr(self.settings, "recurring_late_catchup", True)),
        )

    def next_wakeup_in(self) -> int | None:
        """距离下一次唤醒还有多少毫秒（已到点则返回 ``None``）。"""
        state = self.state()
        if state.batch is not None:
            return None
        if state.wakeup_at is None:
            return None
        delta = (state.wakeup_at - self._now()).total_seconds() * 1000
        return max(0, int(delta))

    def reschedule(self, reason: str = "manual", deferred: bool = False) -> int | None:
        """重排定时器：立刻派发到点的提醒，并把定时器排到下一个时刻。

        语义上**会自动启动**调度（幂等）：调用方（新增任务、改设置、到点回调）
        只需要无脑调 ``reschedule()``，不必记得先 ``start()``。
        真正停止请用 :meth:`stop`。

        :param deferred: 只排程、**不派发**。开机统一检查需要它：先由
            :meth:`collect_due` 把"错过的周期任务"收集去和待办合并成一个弹窗，
            再由 :meth:`dispatch` 正式派发；否则调度器启动时就把批次发掉了，
            统一检查就拿不到内容（会变成两个弹窗）。
        :return: 本次实际排定的睡眠毫秒数（``None`` 表示无需唤醒）
        """
        self._cancel()
        if not bool(getattr(self.settings, "recurring_reminder_enabled", True)):
            return None
        self._stopped = False                # 自动启动，避免"忘了 start 就不排程"

        state = self.state()

        if state.batch is not None and not deferred:
            # 到点了：先派发，再按派发后的状态重排
            self._dispatch(state.batch)
            state = self.state()
        elif state.batch is not None and deferred:
            # 延后派发：定时器仍要按"这批已经派发掉"的状态排程，
            # 否则 wakeup 会停在"这批本身"（已到点）上 → 定时器不武装。
            # 这里只做**虚拟标记**，不写库、不回调（真正的派发由 collect_due+dispatch 完成）。
            tasks, logs = self._load()
            virtual = dict(logs)
            for item in state.batch.items:
                virtual[(item.task.id, item.occur.date_str)] = TaskLog(
                    id=0, task_id=item.task.id, occur_date=item.occur.date_str,
                    remind_at=state.batch.fired_at, remind_kind=item.kind)
            state = compute_schedule(
                tasks, virtual, self._now(),
                catchup_minutes=int(getattr(self.settings, "recurring_catchup_minutes",
                                            DEFAULT_LATE_CATCHUP_MINUTES)),
                catchup_enabled=bool(getattr(self.settings, "recurring_late_catchup", True)),
            )

        if state.wakeup_at is None:
            return None

        sleep_ms = int((state.wakeup_at - self._now()).total_seconds() * 1000)
        cap = max(1, int(getattr(self.settings, "scheduler_max_sleep_minutes", 30))) * 60 * 1000
        sleep_ms = max(MIN_SLEEP_MS, min(sleep_ms, cap, MAX_SLEEP_MS))
        log.debug("调度器重排（%s）：%.1f 秒后唤醒（下次 %s）",
                  reason, sleep_ms / 1000, state.wakeup_at)
        self._after_id = self.root.after(sleep_ms, self._check)
        return sleep_ms

    def _check(self) -> None:
        """定时器到点：重新计算（本来就会重新算，所以休眠/改时钟都能自愈）。

        **必须容错**：整个设计"不轮询"，一旦这里抛异常又没重新排定时器，
        调度器就会永久停摆。所以异常时退避重排一个短定时器。
        """
        self._after_id = None
        self._last_wakeup_at = self._now()
        if self._stopped:
            return
        try:
            self.reschedule(reason="timer")
        except Exception:  # noqa: BLE001
            log.exception("调度器重排失败，退避 60 秒后重试（避免永久停摆）")
            try:
                self._after_id = self.root.after(60_000, self._check)
            except Exception:  # noqa: BLE001 - 控件已销毁
                pass

    # ------------------------------------------------------------------ 派发
    def _dispatch(self, batch: ReminderBatch, silent: bool = False) -> None:
        """派发一批提醒：先标记"已提醒"，再回调（避免回调异常导致重复提醒）。

        :param silent: 只标记、不回调。开机统一检查用它 —— 内容已经并进
            "开机提醒"弹窗展示了，再回调就会多弹一个周期任务窗口。
        """
        self._fired_count += 1
        marked = 0
        for item in batch.items:
            if self.store.log_occurrence(item.task.id, item.occur.date_str,
                                         remind_at=batch.fired_at, remind_kind=item.kind):
                marked += 1
        if marked != len(batch.items):
            # 写库失败时标记会缺失，下一次唤醒会重复提醒 —— 至少留下线索
            log.warning("周期任务提醒标记写入不完整：%d/%d 条成功（可能重复提醒一次）",
                        marked, len(batch.items))
        log.info("周期任务提醒派发（%s%s）：%s", batch.fired_at.strftime("%H:%M:%S"),
                 "，静默" if silent else "", "、".join(item.title for item in batch.items))
        if silent:
            return
        try:
            self.on_batch(batch)
        except Exception:  # noqa: BLE001 - 界面异常不能让调度停摆
            log.exception("周期任务提醒回调异常")

    def fire_now(self) -> ReminderBatch | None:
        """立刻派发当前到点的提醒（用于"测试提醒"按钮与端到端验证）。"""
        state = self.state()
        if state.batch is None:
            return None
        self._dispatch(state.batch)
        return state.batch

    def collect_due(self) -> ReminderBatch | None:
        """**只取不派发**：算出当前到点的批次，但不写库、不回调。

        开机统一提醒用它收集"错过的周期任务"，再和未完成待办合并成一个弹窗；
        派发由调用方通过 :meth:`dispatch` 完成（两者配对使用）。
        """
        state = self.state()
        return state.batch

    def dispatch(self, batch: ReminderBatch | None, silent: bool = False) -> bool:
        """派发一个由 :meth:`collect_due` 取到的批次。

        :param silent: 只写"已提醒"标记、不回调（内容已由调用方展示，例如合并进
            "开机提醒"弹窗）。不传则照常回调，会另外弹一个周期任务窗口。
        """
        if batch is None or not batch.items:
            return False
        self._dispatch(batch, silent=silent)
        return True

    # ------------------------------------------------------------------ 状态展示
    def status_text(self) -> str:
        """给界面/日志用的一句话状态。"""
        if not bool(getattr(self.settings, "recurring_reminder_enabled", True)):
            return "周期提醒已关闭"
        if self._stopped:
            return "调度器未运行"
        state = self.state()
        if state.wakeup_at is None:
            return f"暂无周期任务待提醒（已提醒 {self._fired_count} 次）"
        delta = max(0, int((state.wakeup_at - self._now()).total_seconds()))
        minutes, seconds = divmod(delta, 60)
        hours, minutes = divmod(minutes, 60)
        when = f"{hours} 小时 {minutes} 分" if hours else f"{minutes} 分 {seconds} 秒"
        return f"下次唤醒 {state.wakeup_at:%m-%d %H:%M}（还有 {when}，已提醒 {self._fired_count} 次）"

    def upcoming(self, limit: int = 8) -> list[tuple[Task, datetime]]:
        """列出接下来几次提醒（用于界面预览，最多每个任务 2 次）。"""
        now = self._now()
        tasks, logs = self._load()
        result: list[tuple[Task, datetime]] = []
        for task in tasks:
            probe = now
            found = 0
            for _ in range(24):
                occur = task.rule.next_occurrence(probe)
                if occur is None:
                    break
                key = (task.id, occur.date_str)
                log_row = logs.get(key)
                if not (log_row and log_row.remind_at):
                    remind_at = occur.remind_at(task.rule.remind_before_minutes)
                    if remind_at > now:
                        result.append((task, remind_at))
                        found += 1
                        if found >= 2:
                            break
                probe = occur.end
        result.sort(key=lambda pair: pair[1])
        return result[:limit]

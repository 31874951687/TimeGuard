"""待办任务模块的界面：任务面板（增删改查 + 完成情况图表）+ 开机提醒弹窗。

对外提供两个类：
* :class:`TaskPanel` —— 嵌入主窗口「✅ 待办任务」标签页的面板。
* :class:`StartupReminderDialog` —— 开机（或首次运行）时的待办提醒弹窗。

设计上刻意与 `ui.py` 解耦：本模块只依赖 ttk 样式名（Card.TFrame 等）与注入进来的
``store``（数据库）、``font_family``，方便单独测试与复用。
"""

from __future__ import annotations

import calendar
import logging
import tkinter as tk
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from tkinter import messagebox, ttk

from . import phrases
from .database import Task, TaskLog, UsageStore
from .datepicker import DatePicker, DateTimePicker, backend_name, quick_datetime
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
    STATE_DONE_DEADLINE as R_STATE_DONE_DEADLINE,
    STATE_DONE_EARLY as R_STATE_DONE_EARLY,
    STATE_MISSED as R_STATE_MISSED,
    TASK_TYPES,
    TYPE_CUMULATIVE,
    TYPE_DAILY,
    TYPE_LABELS,
    TYPE_ONCE,
    TYPE_WEEKLY,
    WEEKDAY_CN,
    CumulativeStatus,
    Occurrence,
    TaskRule,
    cumulative_status,
    format_hhmm,
    format_iso_weekdays,
    parse_hhmm,
    suggested_start_date,
)
from .utils import fmt_duration

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- 配色（与主界面一致）
BG = "#10141c"
PANEL = "#1b2130"
PANEL_ALT = "#232b3d"
FG = "#e6eaf2"
SUB = "#8b95a8"
ACCENT = "#4f8cff"
GREEN = "#33c48d"
AMBER = "#ffb020"
RED = "#ff6b6b"
PURPLE = "#a78bfa"

STATUS_PENDING = "○"
STATUS_DONE = "✔"
STATUS_OVERDUE = "!"


def cumulative_row_texts(status: CumulativeStatus) -> tuple[str, str]:
    """累计打卡任务在列表两列里的**紧凑**文案 ``(状态, 进度/剩余)``。

    列表列宽有限（状态列约 90px、剩余列约 180px），所以这里刻意用短文案；
    "还差 X 次 / 距截止还有几天"这类完整句子留给下方的提示条与打卡详情面板
    （见 :meth:`TaskPanel._update_hint` 与 ``checkin_view.CumulativeDetail``）。
    """
    if status.state == R_STATE_DONE_EARLY:
        state_text = f"{STATUS_DONE} 提前完成"
    elif status.state == R_STATE_DONE_DEADLINE:
        state_text = f"{STATUS_DONE} 压哨完成"
    elif status.state == R_STATE_MISSED:
        state_text = f"{STATUS_OVERDUE} 逾期未达标"
    elif status.today_checked:
        state_text = f"{STATUS_DONE} 已打卡"
    else:
        state_text = f"{STATUS_PENDING} 未打卡"
    if status.finished:
        return state_text, f"{status.total}/{status.target} · 已达成目标"
    # 剩余列宽 170px，只放得下 ~22 个汉字：用"还差 15 · 剩 60 天"这种紧凑写法，
    # 完整句子（"还差 15 次 · 还剩 60 天 · 截止 12-08"）留给提示条与详情面板。
    left = status.countdown_text()
    left = left.replace("还剩 ", "剩 ").replace("今天是最后一天", "最后一天")
    left = left.replace("已过期 ", "已过 ").replace("无截止日期", "无期限")
    return state_text, f"{status.total}/{status.target} · 还差 {status.remaining} · {left}"


# ================================================================ 可拖拽分隔条
def place_dialog_bottom_right(dialog: tk.Toplevel, min_width: int = 560) -> None:
    """把弹窗放到工作区域的右下角，并保证它完整落在屏幕内。

    踩过的坑：
    * 必须跑两轮 ``update_idletasks()``（第一轮布局、第二轮让 wraplength 换行后的
      高度稳定），否则 ``winfo_reqheight()`` 偏小、底部按钮被裁掉；
    * 只能用 ``update_idletasks()``，用 ``update()`` 会重入事件循环（曾导致
      "application has been destroyed"）；
    * 位置要按**工作区域**算（``winapi.get_work_area``），用整屏高度会被任务栏压住；
    * 内容过高时要夹取到工作区域，否则高 DPI 下（例如 150% 缩放）弹窗下半部分出屏，
      而弹窗是不可调整大小的，用户只能靠标题栏关闭。
    """
    dialog.update_idletasks()
    dialog.update_idletasks()
    width = max(min_width, dialog.winfo_reqwidth())
    height = dialog.winfo_reqheight()
    try:
        from . import winapi

        work_w, work_h = winapi.get_work_area(dialog)
    except Exception:  # noqa: BLE001 - 取不到就退回整屏
        try:
            work_w, work_h = dialog.winfo_screenwidth(), dialog.winfo_screenheight()
        except tk.TclError:
            work_w, work_h = 1920, 1080

    margin = 24
    width = min(width, max(320, work_w - margin * 2))
    height = min(height, max(240, work_h - margin * 2 - 40))
    x = max(0, work_w - width - margin)
    y = max(0, work_h - height - margin - 40)
    dialog.geometry(f"{width}x{height}+{x}+{y}")


def style_panedwindow(widget: tk.Misc) -> None:
    """给 ttk.Panedwindow 一个细的分隔条样式（跨主题颜色差异较大，逐项设置）。"""
    try:
        style = ttk.Style(widget)
        style.configure("Task.TPanedwindow", background=PANEL, bordercolor=PANEL, sashrelief="flat")
        style.configure("Task.Sash", background="#2f3a52", sashthickness=6, gripcount=0,
                        bordercolor=PANEL, lightcolor="#2f3a52", darkcolor="#2f3a52")
        style.layout("Task.Sash", [("Sash.hsash", {"sticky": "nswe"})])
    except tk.TclError:
        log.debug("配置分隔条样式失败", exc_info=True)


def style_dark_combobox(widget: tk.Misc) -> None:
    """让 ttk.Combobox（含它的弹出列表）跟随深色主题。

    与 ``ui.style_dark_combobox`` 同样的处理：不设置 ``*TCombobox*Listbox.*``
    的话，下拉列表在深色界面里是白底白字（看不见选项）。
    这里自带一份而不是从 ``ui`` 导入，是为了保持本模块与主界面解耦
    （``ui.py`` 反过来要导入本模块，互相导入会成环）。
    """
    try:
        style = ttk.Style(widget)
        is_clam = style.theme_use() == "clam"
        style.configure(
            "Dark.TCombobox",
            foreground=FG, fieldbackground=PANEL_ALT, background=PANEL_ALT,
            arrowcolor=FG, bordercolor=PANEL_ALT, lightcolor=PANEL_ALT, darkcolor=PANEL_ALT,
            selectbackground=ACCENT, selectforeground="#ffffff", padding=3, arrowsize=14,
        )
        for state, spec in (
            (("readonly",), {"fieldbackground": PANEL_ALT, "foreground": FG, "background": PANEL_ALT}),
            (("readonly", "focus"), {"fieldbackground": PANEL_ALT, "foreground": FG,
                                     "selectbackground": ACCENT, "selectforeground": "#ffffff"}),
            (("disabled",), {"fieldbackground": PANEL, "foreground": SUB, "arrowcolor": SUB}),
            (("active",), {"background": "#2b3550" if is_clam else PANEL_ALT}),
        ):
            style.map("Dark.TCombobox", **{key: [(state, value)] for key, value in spec.items()})
        widget.option_add("*TCombobox*Listbox.background", PANEL_ALT)
        widget.option_add("*TCombobox*Listbox.foreground", FG)
        widget.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        widget.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
    except tk.TclError:
        log.debug("配置下拉框样式失败", exc_info=True)


# ================================================================ 时:分 选择控件
class TimeField(ttk.Frame):
    """一个 ``HH`` : ``MM`` 的下拉框组，返回 ``datetime.time``。

    为什么不用一个 ``"18:00"`` 的单下拉框：5 分钟粒度就是 288 个选项，
    下拉列表长到没法用；``DateTimePicker`` 里的时/分双下拉是已经被验证过的做法，
    这里复用同一套（含深色 readonly 状态）。
    """

    #: 分钟下拉的粒度（用户没选中的当前分钟会被临时补进候选值）
    MINUTE_STEP = 5

    def __init__(self, master: tk.Misc, font_body: tuple, value: str = DEFAULT_START,
                 style_prefix: str = "Card", on_change=None, **kwargs) -> None:
        super().__init__(master, style=f"{style_prefix}.TFrame", **kwargs)
        self.font_body = font_body
        self.on_change = on_change
        style_dark_combobox(self)
        parsed = parse_hhmm(value, DEFAULT_START)
        self.hour_var = tk.StringVar(value=f"{parsed.hour:02d}")
        self.minute_var = tk.StringVar(value=f"{parsed.minute:02d}")
        self.hour_box = ttk.Combobox(self, width=4, state="readonly", textvariable=self.hour_var,
                                     values=[f"{h:02d}" for h in range(24)], font=font_body,
                                     style="Dark.TCombobox")
        self.hour_box.pack(side="left")
        ttk.Label(self, text=":", style=f"{style_prefix}.TLabel").pack(side="left")
        minutes = [f"{m:02d}" for m in range(0, 60, self.MINUTE_STEP)]
        if self.minute_var.get() not in minutes:
            minutes.append(self.minute_var.get())
            minutes.sort()
        self.minute_box = ttk.Combobox(self, width=4, state="readonly", textvariable=self.minute_var,
                                       values=minutes, font=font_body, style="Dark.TCombobox")
        self.minute_box.pack(side="left")
        for box in (self.hour_box, self.minute_box):
            box.bind("<<ComboboxSelected>>", self._changed)

    def _changed(self, _event: tk.Event | None = None) -> None:
        if callable(self.on_change):
            self.on_change()

    # ------------------------------------------------------------------ 取值
    def get(self) -> time:
        """当前选择的时间。"""
        return parse_hhmm(self.get_text(), DEFAULT_START)

    def get_text(self) -> str:
        """当前选择的 ``"HH:MM"``。"""
        try:
            hour = int(self.hour_var.get())
        except ValueError:
            hour = 18
        try:
            minute = int(self.minute_var.get())
        except ValueError:
            minute = 0
        return format_hhmm(time(max(0, min(23, hour)), max(0, min(59, minute))))

    def set(self, value: time | str | None) -> None:
        """设置时间（``time`` 或 ``"HH:MM"``）。"""
        if value is None:
            return
        parsed = value if isinstance(value, time) else parse_hhmm(str(value), DEFAULT_START)
        self.hour_var.set(f"{parsed.hour:02d}")
        self.minute_var.set(f"{parsed.minute:02d}")
        if self.minute_var.get() not in self.minute_box.cget("values"):
            self.minute_box.configure(values=sorted(list(self.minute_box.cget("values")) + [self.minute_var.get()]))


# ================================================================ 任务完成情况图表
class TaskStatsChart(tk.Frame):
    """最近 7 天任务完成情况堆叠柱状图（按时完成 / 超期完成 / 逾期未完成）。"""

    def __init__(self, master: tk.Misc, store: UsageStore, font_family: str = "Microsoft YaHei UI", **kwargs) -> None:
        super().__init__(master, bg=PANEL, **kwargs)
        self.store = store
        self.font_family = font_family
        self._canvas = None
        self._figure = None
        self._ax = None
        self._placeholder: tk.Label | None = None
        self._summary_var = tk.StringVar(value="")
        self._build()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        try:
            import matplotlib

            matplotlib.use("TkAgg")
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
            from matplotlib.figure import Figure
        except Exception as exc:  # noqa: BLE001
            log.warning("matplotlib 不可用，任务图表降级: %s", exc)
            self.show_message("未安装 matplotlib，无法显示图表。")
            return

        try:
            from .chart import _configure_matplotlib_font  # 复用主图表的字体配置

            _configure_matplotlib_font()
        except Exception:  # noqa: BLE001
            log.debug("配置中文字体失败", exc_info=True)

        self._figure = Figure(figsize=(6.6, 3.4), dpi=100, facecolor=PANEL)
        self._ax = self._figure.add_subplot(111)
        self._style_axes()
        canvas = FigureCanvasTkAgg(self._figure, master=self)
        canvas.draw()
        canvas.get_tk_widget().configure(bg=PANEL, highlightthickness=0)
        canvas.get_tk_widget().pack(fill="both", expand=True, padx=6, pady=(4, 2))
        self._canvas = canvas
        self.bind("<Configure>", self._on_resize)

    def _style_axes(self) -> None:
        """深色坐标系样式。"""
        ax = self._ax
        ax.set_facecolor(PANEL)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color("#2a3143")
        ax.tick_params(colors=SUB, labelsize=9)
        ax.grid(axis="y", color="#2a3143", linestyle="--", linewidth=0.6, alpha=0.7)
        ax.set_axisbelow(True)
        self._figure.subplots_adjust(left=0.075, right=0.99, top=0.86, bottom=0.14)

    def _on_resize(self, event: tk.Event) -> None:
        """让画布跟随控件尺寸。"""
        if self._figure is None or self._canvas is None:
            return
        width = max(240, event.width - 12) / float(self._figure.dpi)
        height = max(140, event.height - 12) / float(self._figure.dpi)
        current_w, current_h = self._figure.get_size_inches()
        if abs(width - current_w) < 0.05 and abs(height - current_h) < 0.05:
            return
        try:
            self._figure.set_size_inches(width, height)
            self._canvas.draw_idle()
        except Exception:  # noqa: BLE001
            log.debug("调整任务图表尺寸失败", exc_info=True)

    def show_message(self, text: str) -> None:
        """用文字占位替代图表。"""
        if self._placeholder is None:
            self._placeholder = tk.Label(
                self, text=text, bg=PANEL, fg=SUB, font=(self.font_family, 10), justify="center"
            )
        self._placeholder.configure(text=text)
        self._placeholder.pack(fill="both", expand=True)

    def hide_message(self) -> None:
        """隐藏占位文字。"""
        if self._placeholder is not None:
            self._placeholder.pack_forget()

    # ------------------------------------------------------------------
    def refresh(self, days: int = 7) -> None:
        """重新查询并绘制。"""
        if self._canvas is None:
            return
        stats = self.store.task_stats_by_day(days)
        overview = self.store.task_overview(days)
        self._summary_var.set(
            f"近 {days} 天：按时完成 {overview['on_time']} 个　·　超期完成 {overview['late']} 个　·　"
            f"逾期未完成 {overview['overdue']} 个　·　待办中 {overview['pending_future']} 个"
        )
        self.hide_message()

        from .utils import weekday_cn

        labels = [f"{s.day[5:]}\n{weekday_cn(s.day)}" for s in stats]
        on_time = [s.on_time for s in stats]
        late = [s.late for s in stats]
        overdue = [s.overdue for s in stats]
        x = list(range(len(labels)))

        ax = self._ax
        ax.clear()
        self._style_axes()
        ax.bar(x, on_time, width=0.5, color=GREEN, label="按时完成", zorder=3)
        ax.bar(x, late, width=0.5, bottom=on_time, color=AMBER, label="超期完成", zorder=3)
        ax.bar(x, overdue, width=0.5,
               bottom=[a + b for a, b in zip(on_time, late)], color=RED, label="逾期未完成", zorder=3)

        ax.set_xticks(x)
        ax.set_xticklabels(labels, color=SUB, fontsize=8)
        total_max = max([a + b + c for a, b, c in zip(on_time, late, overdue)] + [1])
        ax.set_ylim(0, total_max * 1.35)
        yticks = [t for t in ax.get_yticks() if t >= 0 and t <= total_max + 1]
        ax.set_yticks(yticks)
        ax.set_yticklabels([f"{int(t)}" if abs(t - round(t)) < 0.01 else f"{t:.1f}" for t in yticks],
                           color=SUB, fontsize=8)

        # 柱顶标注总数
        for index, (a, b, c) in enumerate(zip(on_time, late, overdue)):
            if a + b + c <= 0:
                continue
            ax.annotate(str(a + b + c), xy=(index, a + b + c), xytext=(0, 3),
                        textcoords="offset points", ha="center", color=FG, fontsize=8)

        ax.set_title("最近 7 天任务完成情况", color=FG, fontsize=10, pad=6)
        legend = ax.legend(loc="upper right", fontsize=8, frameon=False, ncol=3)
        for text in legend.get_texts():
            text.set_color(SUB)
        try:
            self._canvas.draw_idle()
        except Exception:  # noqa: BLE001
            log.debug("刷新任务图表失败", exc_info=True)

    @property
    def summary_var(self) -> tk.StringVar:
        """图表上方的文字摘要（供面板放入布局）。"""
        return self._summary_var


# ================================================================ 任务编辑草稿
@dataclass
class TaskDraft:
    """任务对话框收集出来的内容（保存前的一份草稿，可独立预览）。"""

    title: str
    due_at: datetime | None = None
    priority: int = 1
    task_type: str = TYPE_ONCE
    time_start: str = DEFAULT_START
    time_end: str = DEFAULT_END
    days_of_week: str = ""
    remind_before_minutes: int = DEFAULT_REMIND_BEFORE
    #: 累计打卡任务：总目标次数 / 最终截止日期 / 每日提醒时间
    target_count: int = DEFAULT_TARGET_COUNT
    deadline: date | None = None
    remind_time: str = DEFAULT_REMIND_TIME
    #: 累计打卡任务（可选）：监控对象名 + 时长阈值 → 达标自动打卡
    auto_target: str = ""
    auto_minutes: int = DEFAULT_AUTO_MINUTES

    @property
    def is_recurring(self) -> bool:
        """是否周期任务（每天 / 每周）。"""
        return self.task_type in (TYPE_DAILY, TYPE_WEEKLY)

    @property
    def is_cumulative(self) -> bool:
        """是否累计打卡任务。"""
        return self.task_type == TYPE_CUMULATIVE

    @property
    def type_label(self) -> str:
        return TYPE_LABELS.get(self.task_type, self.task_type)

    def rule(self) -> TaskRule:
        """转成调度规则（用于预览"下一次提醒"，与实际入库后读出来的一致）。"""
        if self.task_type == TYPE_DAILY:
            return TaskRule.daily(self.time_start, self.time_end, self.remind_before_minutes)
        if self.task_type == TYPE_WEEKLY:
            return TaskRule.weekly(self.days_of_week, self.time_start, self.time_end,
                                   self.remind_before_minutes)
        if self.task_type == TYPE_CUMULATIVE:
            return TaskRule.cumulative(self.target_count, self.deadline, self.remind_time,
                                       auto_target=self.auto_target or None,
                                       auto_minutes=self.auto_minutes)
        return TaskRule(TYPE_ONCE)

    def preview_text(self, now: datetime | None = None) -> str:
        """对话框里那行"这句话就是我要保存的东西"的预览。"""
        now = now or datetime.now()
        if self.is_cumulative:
            return self._cumulative_preview(now)
        if not self.is_recurring:
            if self.due_at is None:
                return "单次任务 · 无期限 · 按「截止前 24 小时」提醒"
            return f"单次任务 · {self.due_at:%Y-%m-%d %H:%M} 截止 · 按「截止前 24 小时」提醒"

        rule = self.rule()
        parts = [f"{rule.type_label}任务", rule.schedule_text(),
                 f"开始前 {rule.remind_before_minutes} 分钟提醒"]
        occurrence = rule.next_occurrence(now)
        if occurrence is not None:
            parts.append(f"下次提醒 {occurrence.remind_at(rule.remind_before_minutes):%m-%d %H:%M}")
        text = " · ".join(parts)
        if self.task_type == TYPE_WEEKLY and not self.days_of_week:
            text += "　（还没选星期几）"
        # 今天这次的窗口已经过去了 → 明确说清"从下一次开始"，
        # 免得用户以为一保存就欠了一笔"今天已超期"（用户反馈过这个问题）。
        start = suggested_start_date(self.task_type, self.time_start, self.time_end, now)
        if start and start > now.strftime("%Y-%m-%d"):
            first_day = occurrence.day.strftime("%m-%d") if occurrence is not None else start[5:]
            text += f"　（今天的时间已过，从 {first_day} 开始）"
        return text

    def _cumulative_preview(self, now: datetime) -> str:
        """累计打卡任务的预览：把"什么时候提醒、什么时候截止、还差多少"说清楚。"""
        target = max(1, int(self.target_count or 1))
        parts = [f"累计打卡任务 · 目标 {target} 次"]
        if self.deadline is None:
            return "　".join(parts) + " · 还没选截止日期"
        days = (self.deadline - now.date()).days
        parts.append(f"截止 {self.deadline:%Y-%m-%d}")
        if days < 0:
            parts.append("⚠ 截止日期已经过去了")
        elif days == 0:
            parts.append("今天就是最后一天")
        else:
            parts.append(f"还剩 {days} 天")
        rule = self.rule()
        moment = rule.checkin_reminder_on(now.date())
        next_moment = rule.next_checkin_reminder(now)
        if next_moment is not None:
            when = "今天" if next_moment.date() == now.date() else f"{next_moment:%m-%d}"
            parts.append(f"每天 {format_hhmm(moment.time())} 提醒（下次 {when} "
                         f"{next_moment:%H:%M}）")
        else:
            parts.append(f"每天 {self.remind_time} 提醒")
        if days >= 0:
            parts.append("截止日之前只显示「今日未打卡 / 还差 X 次」，不会算逾期")
        if self.auto_target:
            parts.append(f"监控「{self.auto_target}」今日满 {self.auto_minutes} 分钟自动打卡")
        return " · ".join(parts)


# ================================================================ 任务编辑对话框
class TaskDialog(tk.Toplevel):
    """新增 / 编辑待办任务的对话框（单次 / 每天 / 每周 三种重复周期）。"""

    def __init__(
        self,
        master: tk.Misc,
        font_family: str = "Microsoft YaHei UI",
        task: Task | None = None,
        target_names: list[str] | None = None,
    ) -> None:
        super().__init__(master)
        #: 确认后是一个 :class:`TaskDraft`，取消为 ``None``
        self.result: TaskDraft | None = None
        self.font_family = font_family
        self.font_body = (font_family, 10)
        #: "自动打卡"下拉框的候选：最近监控过的对象名（由调用方从库里取）
        self.target_names = list(target_names or [])
        self.title("编辑待办任务" if task else "添加待办任务")
        self.configure(bg=PANEL)
        self.resizable(False, False)
        self.transient(master)
        try:
            self.attributes("-toolwindow", True)
        except tk.TclError:
            pass
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        rule = task.rule if task else TaskRule(TYPE_ONCE)
        self.title_var = tk.StringVar(value=task.title if task else "")
        self.priority_var = tk.StringVar(value="重要" if (task and task.priority >= 2) else "普通")
        self.type_var = tk.StringVar(value=TYPE_LABELS.get(rule.task_type, TYPE_LABELS[TYPE_ONCE]))
        self.remind_var = tk.StringVar(value=str(rule.remind_before_minutes))
        self.preview_var = tk.StringVar(value="")
        self.day_vars: dict[int, tk.BooleanVar] = {iso: tk.BooleanVar(value=False) for iso in range(1, 8)}
        for iso in rule.days_of_week:
            if iso in self.day_vars:
                self.day_vars[iso].set(True)

        self._build(task)
        self._on_type_change()          # 先按任务类型显示对应分支，再居中
        self._center(master)

        self.bind("<Escape>", lambda _e: self._cancel())
        self.bind("<Return>", lambda _e: self._confirm())
        self.grab_set()
        self.title_entry.focus_set()

    # ------------------------------------------------------------------ 构建
    def _build(self, task: Task | None) -> None:
        body = ttk.Frame(self, style="Card.TFrame", padding=16)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        self.body = body
        rule = task.rule if task else TaskRule(TYPE_ONCE)

        ttk.Label(body, text="任务内容", style="Card.TLabel").grid(row=0, column=0, sticky="w")
        self.title_entry = ttk.Entry(body, textvariable=self.title_var, width=58,
                                     font=(self.font_family, 11))
        self.title_entry.grid(row=1, column=0, sticky="ew", pady=(4, 10))
        self.title_entry.bind("<KeyRelease>", lambda _e: self._update_preview())

        # ---- 重复周期 ----
        type_row = ttk.Frame(body, style="Card.TFrame")
        type_row.grid(row=2, column=0, sticky="w")
        ttk.Label(type_row, text="重复周期：", style="Card.TLabel").pack(side="left")
        style_dark_combobox(type_row)
        self.type_box = ttk.Combobox(
            type_row, width=8, state="readonly", textvariable=self.type_var,
            values=[TYPE_LABELS[t] for t in TASK_TYPES if t in TYPE_LABELS],
            font=self.font_body, style="Dark.TCombobox",
        )
        self.type_box.pack(side="left")
        self.type_box.bind("<<ComboboxSelected>>", lambda _e: self._on_type_change())
        ttk.Label(type_row, text="周期任务按「开始时间」提前提醒，单次任务按「截止前 24 小时」提醒",
                  style="CardHint.TLabel").pack(side="left", padx=(10, 0))

        # ---- 分支一：单次任务（截止时间）----
        self.once_frame = ttk.Frame(body, style="Card.TFrame")
        self.once_frame.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        ttk.Label(self.once_frame, text="截止时间（精确到分钟）", style="Card.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.picker = DateTimePicker(self.once_frame, font_family=self.font_family, style_prefix="Card")
        self.picker.grid(row=1, column=0, sticky="w", pady=(6, 6), padx=(0, 4))
        if task and task.due_at:
            self.picker.set(task.due_at)
        elif task:
            self.picker.set_none()
        quick = ttk.Frame(self.once_frame, style="Card.TFrame")
        quick.grid(row=2, column=0, sticky="w", pady=(0, 4))
        ttk.Label(quick, text="快捷：", style="CardHint.TLabel").pack(side="left")
        for label in ("1 小时后", "3 小时后", "今晚 20:00", "明天此时", "明早 09:00"):
            ttk.Button(quick, text=label, style="Tiny.TButton",
                       command=lambda text=label: self._set_quick(text)).pack(side="left", padx=(0, 4))

        # ---- 分支二：周期任务（时间段 + 星期几 + 提前提醒）----
        self.recur_frame = ttk.Frame(body, style="Card.TFrame")
        self.recur_frame.grid(row=4, column=0, sticky="ew", pady=(12, 0))

        span = ttk.Frame(self.recur_frame, style="Card.TFrame")
        span.grid(row=0, column=0, sticky="w")
        ttk.Label(span, text="时间段：", style="Card.TLabel").pack(side="left")
        self.start_field = TimeField(span, font_body=self.font_body,
                                     value=format_hhmm(rule.time_start), on_change=self._update_preview)
        self.start_field.pack(side="left")
        ttk.Label(span, text=" 到 ", style="Card.TLabel").pack(side="left")
        self.end_field = TimeField(span, font_body=self.font_body,
                                   value=format_hhmm(rule.time_end), on_change=self._update_preview)
        self.end_field.pack(side="left")
        ttk.Label(span, text="（到点前会提前提醒你）", style="CardHint.TLabel").pack(side="left", padx=(8, 0))

        # 星期几：只有"每周"才显示
        self.days_frame = ttk.Frame(self.recur_frame, style="Card.TFrame")
        self.days_frame.grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Label(self.days_frame, text="星期几：", style="Card.TLabel").grid(row=0, column=0, sticky="w")
        days_box = ttk.Frame(self.days_frame, style="Card.TFrame")
        days_box.grid(row=0, column=1, sticky="w")
        for iso in range(1, 8):
            ttk.Checkbutton(days_box, text=WEEKDAY_CN[iso].replace("周", ""), variable=self.day_vars[iso],
                            style="Card.TCheckbutton", command=self._update_preview).pack(
                side="left", padx=(0, 2)
            )
        quick_days = ttk.Frame(self.days_frame, style="Card.TFrame")
        quick_days.grid(row=0, column=2, sticky="w", padx=(10, 0))
        for text, value in (("工作日", (1, 2, 3, 4, 5)), ("周末", (6, 7)),
                            ("全选", (1, 2, 3, 4, 5, 6, 7)), ("清空", ())):
            ttk.Button(quick_days, text=text, style="Tiny.TButton",
                       command=lambda days=value: self._set_days(days)).pack(side="left", padx=(0, 4))

        remind_row = ttk.Frame(self.recur_frame, style="Card.TFrame")
        remind_row.grid(row=2, column=0, sticky="w", pady=(10, 0))
        ttk.Label(remind_row, text="提前提醒：", style="Card.TLabel").pack(side="left")
        style_dark_combobox(remind_row)
        self.remind_box = ttk.Combobox(
            remind_row, width=6, textvariable=self.remind_var, font=self.font_body,
            values=("0", "5", "10", "15", "30", "60", "120"), style="Dark.TCombobox",
        )
        self.remind_box.pack(side="left")
        self.remind_box.bind("<<ComboboxSelected>>", lambda _e: self._update_preview())
        self.remind_box.bind("<KeyRelease>", lambda _e: self._update_preview())
        ttk.Label(remind_row, text=f"分钟（{MIN_REMIND_BEFORE}~{MAX_REMIND_BEFORE}，开始前）",
                  style="CardHint.TLabel").pack(side="left", padx=(6, 0))

        # ---- 分支三：累计打卡任务（目标次数 + 截止日期 + 每日提醒时间）----
        self.cum_frame = ttk.Frame(body, style="Card.TFrame")
        self.cum_frame.grid(row=4, column=0, sticky="ew", pady=(12, 0))

        goal_row = ttk.Frame(self.cum_frame, style="Card.TFrame")
        goal_row.grid(row=0, column=0, sticky="w")
        ttk.Label(goal_row, text="总目标次数：", style="Card.TLabel").pack(side="left")
        self.target_var = tk.StringVar(value=str(rule.target_count or DEFAULT_TARGET_COUNT))
        self.target_entry = ttk.Entry(goal_row, textvariable=self.target_var, width=6,
                                      font=self.font_body)
        self.target_entry.pack(side="left")
        self.target_entry.bind("<KeyRelease>", lambda _e: self._update_preview())
        ttk.Label(goal_row, text="次　（例如 60 次＝年末前跑 60 个两公里）",
                  style="CardHint.TLabel").pack(side="left", padx=(6, 0))

        deadline_row = ttk.Frame(self.cum_frame, style="Card.TFrame")
        deadline_row.grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Label(deadline_row, text="最终截止日期：", style="Card.TLabel").pack(side="left")
        self.deadline_picker = DatePicker(deadline_row, font_family=self.font_family,
                                          style_prefix="Card")
        self.deadline_picker.pack(side="left", padx=(0, 4))
        default_deadline = rule.deadline or (datetime.now().date() + timedelta(days=90))
        self.deadline_picker.set(default_deadline)
        quick_deadline = ttk.Frame(self.cum_frame, style="Card.TFrame")
        quick_deadline.grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Label(quick_deadline, text="快捷：", style="CardHint.TLabel").pack(side="left")
        for label, days_ahead in (("本月底", None), ("3 个月后", 90), ("半年后", 180), ("今年底", "yearend")):
            ttk.Button(quick_deadline, text=label, style="Tiny.TButton",
                       command=lambda d=days_ahead: self._set_deadline_quick(d)).pack(
                side="left", padx=(0, 4))
        ttk.Label(quick_deadline, text="　截止日当天 23:59 之前打卡都算数",
                  style="CardHint.TLabel").pack(side="left")

        remind_row2 = ttk.Frame(self.cum_frame, style="Card.TFrame")
        remind_row2.grid(row=3, column=0, sticky="w", pady=(10, 0))
        ttk.Label(remind_row2, text="每日提醒时间：", style="Card.TLabel").pack(side="left")
        self.remind_time_field = TimeField(remind_row2, font_body=self.font_body,
                                           value=rule.remind_time and format_hhmm(rule.remind_time)
                                           or DEFAULT_REMIND_TIME,
                                           on_change=self._update_preview)
        self.remind_time_field.pack(side="left")
        ttk.Label(remind_row2,
                  text="　每天到点提醒一次「该打卡了」；达标或过了截止日就自动停止提醒",
                  style="CardHint.TLabel").pack(side="left", padx=(8, 0))

        # 自动打卡（可选）：监控对象 + 时长阈值
        auto_row = ttk.Frame(self.cum_frame, style="Card.TFrame")
        auto_row.grid(row=4, column=0, sticky="w", pady=(10, 0))
        ttk.Label(auto_row, text="自动打卡（可选）：", style="Card.TLabel").pack(side="left")
        self.auto_var = tk.StringVar(value=rule.auto_target or "")
        style_dark_combobox(auto_row)
        self.auto_box = ttk.Combobox(auto_row, width=16, textvariable=self.auto_var,
                                     font=self.font_body, style="Dark.TCombobox",
                                     values=self.target_names or ())
        self.auto_box.pack(side="left")
        self.auto_box.bind("<<ComboboxSelected>>", lambda _e: self._update_preview())
        self.auto_box.bind("<KeyRelease>", lambda _e: self._update_preview())
        ttk.Label(auto_row, text="  今日累计满 ", style="CardHint.TLabel").pack(side="left")
        self.auto_minutes_var = tk.StringVar(
            value=str(rule.auto_minutes or DEFAULT_AUTO_MINUTES))
        self.auto_minutes_entry = ttk.Entry(auto_row, textvariable=self.auto_minutes_var,
                                            width=5, font=self.font_body)
        self.auto_minutes_entry.pack(side="left")
        self.auto_minutes_entry.bind("<KeyRelease>", lambda _e: self._update_preview())
        ttk.Label(auto_row, text=" 分钟就自动打一次卡（留空＝只手动打卡）",
                  style="CardHint.TLabel").pack(side="left")

        # ---- 预览 + 优先级 ----
        ttk.Label(body, textvariable=self.preview_var, style="CardHint.TLabel",
                  wraplength=620, justify="left").grid(row=5, column=0, sticky="w", pady=(12, 0))

        prio = ttk.Frame(body, style="Card.TFrame")
        prio.grid(row=6, column=0, sticky="w", pady=(10, 10))
        ttk.Label(prio, text="优先级：", style="Card.TLabel").pack(side="left")
        ttk.Radiobutton(prio, text="普通", value="普通", variable=self.priority_var,
                        style="Card.TRadiobutton").pack(side="left")
        ttk.Radiobutton(prio, text="重要", value="重要", variable=self.priority_var,
                        style="Card.TRadiobutton").pack(side="left", padx=(8, 0))

        ttk.Label(body, text=f"日期选择组件：{backend_name()}", style="CardHint.TLabel").grid(
            row=7, column=0, sticky="w"
        )

        buttons = ttk.Frame(body, style="Card.TFrame")
        buttons.grid(row=8, column=0, sticky="e", pady=(14, 0))
        self.buttons_frame = buttons
        ttk.Button(buttons, text="取消", style="Small.TButton", command=self._cancel).pack(side="right")
        ttk.Button(buttons, text="保存", style="Accent.TButton", command=self._confirm).pack(
            side="right", padx=(0, 8)
        )

    # ------------------------------------------------------------------ 联动
    def _current_type(self) -> str:
        for key, label in TYPE_LABELS.items():
            if label == self.type_var.get():
                return key
        return TYPE_ONCE

    def _on_type_change(self) -> None:
        """切换重复周期时显示/隐藏对应字段（控件实例保持不变，值不会丢）。"""
        task_type = self._current_type()
        if task_type == TYPE_ONCE:
            self.recur_frame.grid_remove()
            self.cum_frame.grid_remove()
            self.once_frame.grid()
        elif task_type == TYPE_CUMULATIVE:
            self.once_frame.grid_remove()
            self.recur_frame.grid_remove()
            self.cum_frame.grid()
        else:
            self.once_frame.grid_remove()
            self.cum_frame.grid_remove()
            self.recur_frame.grid()
            if task_type == TYPE_DAILY:
                self.days_frame.grid_remove()
            else:
                self.days_frame.grid()
                if not any(var.get() for var in self.day_vars.values()):
                    # 首次切到"每周"：默认勾上今天，避免"一天都没选"被拦下
                    self.day_vars[datetime.now().isoweekday()].set(True)
        self._update_preview()

    def _set_deadline_quick(self, days_ahead) -> None:
        """截止日期快捷按钮：本月底 / 3 个月后 / 半年后 / 今年底。"""
        today = datetime.now().date()
        if days_ahead == "yearend":
            target = date(today.year, 12, 31)
        elif days_ahead is None:                       # 本月底
            last_day = calendar.monthrange(today.year, today.month)[1]
            target = date(today.year, today.month, last_day)
        else:
            target = today + timedelta(days=int(days_ahead))
        self.deadline_picker.set_date(target)
        self._update_preview()

    def _set_days(self, days) -> None:
        wanted = set(days)
        for iso, var in self.day_vars.items():
            var.set(iso in wanted)
        self._update_preview()

    def _set_quick(self, label: str) -> None:
        self.picker.set(quick_datetime(label))
        self._update_preview()

    def _update_preview(self) -> None:
        self.preview_var.set(self._draft().preview_text())

    def _parse_remind(self) -> int | None:
        """解析"提前提醒"分钟数（非法返回 ``None``）。"""
        raw = self.remind_var.get().strip()
        if not raw:
            return DEFAULT_REMIND_BEFORE
        try:
            value = int(float(raw))
        except ValueError:
            return None
        if value < MIN_REMIND_BEFORE or value > MAX_REMIND_BEFORE:
            return None
        return value

    def _parse_target(self) -> int | None:
        """解析"总目标次数"（非法返回 ``None``）。"""
        raw = self.target_var.get().strip()
        try:
            value = int(float(raw))
        except ValueError:
            return None
        if value < 1 or value > MAX_TARGET_COUNT:
            return None
        return value

    def _parse_auto_minutes(self) -> int | None:
        """解析"自动打卡"的分钟阈值；留空 = 关闭自动打卡。"""
        if not self.auto_var.get().strip():
            return None                       # 没填监控对象 = 不开这个功能
        raw = self.auto_minutes_var.get().strip()
        if not raw:
            return DEFAULT_AUTO_MINUTES
        try:
            value = int(float(raw))
        except ValueError:
            return -1                         # 用 -1 表示"填了但非法"
        if value < MIN_AUTO_MINUTES or value > MAX_AUTO_MINUTES:
            return -1
        return value

    def _draft(self) -> TaskDraft:
        """把界面上的值收集成草稿（不做阻断式校验）。"""
        remind = self._parse_remind()
        task_type = self._current_type()
        days = (format_iso_weekdays([iso for iso, var in self.day_vars.items() if var.get()])
                if task_type == TYPE_WEEKLY else "")
        target = self._parse_target()
        auto_minutes = self._parse_auto_minutes()
        return TaskDraft(
            title=self.title_var.get().strip(),
            due_at=self.picker.get(),
            priority=2 if self.priority_var.get() == "重要" else 1,
            task_type=task_type,
            time_start=self.start_field.get_text(),
            time_end=self.end_field.get_text(),
            days_of_week=days,
            remind_before_minutes=DEFAULT_REMIND_BEFORE if remind is None else remind,
            target_count=DEFAULT_TARGET_COUNT if target is None else target,
            deadline=self.deadline_picker.get_date(),
            remind_time=self.remind_time_field.get_text(),
            auto_target=self.auto_var.get().strip(),
            auto_minutes=(DEFAULT_AUTO_MINUTES if auto_minutes in (None, -1) else auto_minutes),
        )

    # ------------------------------------------------------------------ 位置 / 结果
    def _center(self, master: tk.Misc) -> None:
        """相对父窗口居中。"""
        self.update_idletasks()
        width, height = self.winfo_reqwidth(), self.winfo_reqheight()
        try:
            x = master.winfo_rootx() + max(0, (master.winfo_width() - width) // 2)
            y = master.winfo_rooty() + max(0, (master.winfo_height() - height) // 3)
        except tk.TclError:
            x = y = 200
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _confirm(self) -> None:
        """校验并关闭对话框。"""
        if not self.title_var.get().strip():
            messagebox.showinfo("提示", "请填写任务内容。", parent=self)
            self.title_entry.focus_set()
            return

        task_type = self._current_type()
        if task_type == TYPE_CUMULATIVE:
            if self._parse_target() is None:
                messagebox.showinfo(
                    "提示", f"「总目标次数」请填写 1~{MAX_TARGET_COUNT} 之间的整数。", parent=self)
                self.target_entry.focus_set()
                return
            deadline = self.deadline_picker.get_date()
            if deadline is None:
                messagebox.showinfo("提示", "请选择「最终截止日期」。", parent=self)
                return
            if deadline < datetime.now().date():
                if not messagebox.askyesno(
                    "确认",
                    f"截止日期（{deadline:%Y-%m-%d}）已经过去了，保存后这条任务会直接显示"
                    "「逾期未达标」。\n\n仍要保存吗？",
                    parent=self,
                ):
                    return
            if self._parse_auto_minutes() == -1:
                messagebox.showinfo(
                    "提示",
                    f"「自动打卡」的分钟数请填写 {MIN_AUTO_MINUTES}~{MAX_AUTO_MINUTES} 之间的整数，"
                    "或者把监控对象留空（＝只手动打卡）。",
                    parent=self,
                )
                self.auto_minutes_entry.focus_set()
                return
        elif task_type != TYPE_ONCE:
            if self._parse_remind() is None:
                messagebox.showinfo(
                    "提示", f"「提前提醒」请填写 {MIN_REMIND_BEFORE}~{MAX_REMIND_BEFORE} 之间的整数（分钟）。",
                    parent=self,
                )
                self.remind_box.focus_set()
                return
            if task_type == TYPE_WEEKLY and not any(var.get() for var in self.day_vars.values()):
                messagebox.showinfo("提示", "「每周」任务至少要选择一天。", parent=self)
                return
            start, end = self.start_field.get(), self.end_field.get()
            if end <= start:
                if not messagebox.askyesno(
                    "确认",
                    f"结束时间（{format_hhmm(end)}）不晚于开始时间（{format_hhmm(start)}）。\n\n"
                    "保存后这一次会按「当天 23:59 结束」处理（即跨零点的时间段目前不支持），是否继续？",
                    parent=self,
                ):
                    return
        else:
            due = self.picker.get()
            if due is not None and due < datetime.now() - timedelta(minutes=1):
                if not messagebox.askyesno("确认", "截止时间已经过去了，仍要保存吗？", parent=self):
                    return

        self.result = self._draft()
        self.destroy()

    def _cancel(self) -> None:
        """取消并关闭。"""
        self.result = None
        self.destroy()


# ================================================================ 补签对话框
class BackfillDialog(tk.Toplevel):
    """补签：给累计打卡任务补一个"过去某一天"的打卡（可写备注）。

    为什么要有它：人总会忘。忘了就断一天，热力图上缺口很难看，也影响"攒次数"的信心。
    但补签必须是**显式**的、要写备注的 —— 否则"打卡"就失去意义了。

    合法范围由数据层把关：只能补【任务创建之后、今天之前】的日子，同一天只能一次。
    """

    WIDTH = 460

    def __init__(self, master: tk.Misc, task: Task, store: UsageStore,
                 font_family: str = "Microsoft YaHei UI") -> None:
        super().__init__(master)
        #: 确认后是 ``(日期, 备注)``，取消为 ``None``
        self.result: tuple[date, str] | None = None
        self.task = task
        self.store = store
        self.font_family = font_family
        self.font_body = (font_family, 10)
        self.title("补签打卡")
        self.configure(bg=PANEL)
        self.resizable(False, False)
        self.transient(master)
        try:
            self.attributes("-toolwindow", True)
        except tk.TclError:
            pass
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        self._build()
        self.bind("<Escape>", lambda _e: self._cancel())
        self.bind("<Return>", lambda _e: self._confirm())
        self._center(master)
        self.grab_set()

    def _build(self) -> None:
        body = ttk.Frame(self, style="Card.TFrame", padding=16)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text=f"补签：{self.task.title}", style="CardTitle.TLabel",
                  font=(self.font_family, 13, "bold")).pack(anchor="w")
        already = self.store.checkin_count(self.task.id)
        target = self.task.rule.target_count or 1
        ttk.Label(body, text=f"当前已打卡 {already}/{target} 次　·　"
                             f"只能补【任务创建之后、今天之前】的日子，每天一次",
                  style="CardHint.TLabel").pack(anchor="w", pady=(2, 10))

        row = ttk.Frame(body, style="Card.TFrame")
        row.pack(anchor="w")
        ttk.Label(row, text="补哪一天：", style="Card.TLabel").pack(side="left")
        self.picker = DatePicker(row, font_family=self.font_family, style_prefix="Card")
        self.picker.pack(side="left")
        # 默认补昨天（最常见的场景）；更早的日子用户自己选
        default_day = date.today() - timedelta(days=1)
        created = self.task.created_at.date() if self.task.created_at else None
        if created is not None and default_day < created:
            default_day = created if created <= date.today() else date.today()
        self.picker.set_date(default_day)

        quick = ttk.Frame(body, style="Card.TFrame")
        quick.pack(anchor="w", pady=(6, 10))
        ttk.Label(quick, text="快捷：", style="CardHint.TLabel").pack(side="left")
        for label, offset in (("昨天", 1), ("前天", 2), ("3 天前", 3), ("一周前", 7)):
            ttk.Button(quick, text=label, style="Tiny.TButton",
                       command=lambda o=offset: self.picker.set_date(date.today() - timedelta(days=o))
                       ).pack(side="left", padx=(0, 4))

        ttk.Label(body, text="备注（会记进打卡历史）", style="Card.TLabel").pack(anchor="w")
        self.note_var = tk.StringVar(value="")
        self.note_entry = ttk.Entry(body, textvariable=self.note_var, width=46,
                                    font=self.font_body)
        self.note_entry.pack(anchor="w", pady=(4, 0))
        ttk.Label(body, text="例如：昨天忘了打卡，补上（备注可留空）",
                  style="CardHint.TLabel").pack(anchor="w", pady=(2, 0))

        buttons = ttk.Frame(body, style="Card.TFrame")
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(buttons, text="取消", style="Small.TButton",
                   command=self._cancel).pack(side="right")
        ttk.Button(buttons, text="确认补签", style="Accent.TButton",
                   command=self._confirm).pack(side="right", padx=(0, 8))
        self.note_entry.focus_set()

    def _center(self, master: tk.Misc) -> None:
        self.update_idletasks()
        width, height = self.winfo_reqwidth(), self.winfo_reqheight()
        try:
            x = master.winfo_rootx() + max(0, (master.winfo_width() - width) // 2)
            y = master.winfo_rooty() + max(0, (master.winfo_height() - height) // 3)
        except tk.TclError:
            x = y = 200
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _confirm(self) -> None:
        day = self.picker.get_date()
        if day is None:
            messagebox.showinfo("提示", "请选择要补签的日期。", parent=self)
            return
        if day > date.today():
            messagebox.showinfo("提示", "不能给未来的日子补签。", parent=self)
            return
        if day == date.today():
            messagebox.showinfo("提示", "今天请直接用「✓ 打卡」，补签只用于过去的日期。", parent=self)
            return
        created = self.task.created_at.date() if self.task.created_at else None
        if created is not None and day < created:
            messagebox.showinfo(
                "提示",
                f"这条任务是 {created:%Y-%m-%d} 创建的，{day:%Y-%m-%d} 那天它还不存在，不能补签。",
                parent=self)
            return
        self.result = (day, self.note_var.get().strip())
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


# ================================================================ 列表分组模型
#: 分组键（顺序即展示顺序）
GROUP_TODAY = "today"
GROUP_WEEK = "week"
GROUP_CHECKIN = "checkin"
GROUP_OTHER = "other"
GROUP_ORDER = (GROUP_TODAY, GROUP_WEEK, GROUP_CHECKIN, GROUP_OTHER)
GROUP_TITLES = {GROUP_TODAY: "今日待办", GROUP_WEEK: "本周待办",
                GROUP_CHECKIN: "累计打卡", GROUP_OTHER: "其他"}

#: 「本周待办」向前看的天数（含今天）
HORIZON_DAYS = 7


@dataclass
class TaskRow:
    """列表里的一行（单次任务 / 周期任务的"今天这一次" / 一条累计打卡任务）。"""

    task: Task
    group: str
    done: bool = False
    overdue: bool = False
    late: bool = False
    status_text: str = ""
    schedule_text: str = ""
    countdown_text: str = ""
    occur_date: date | None = None
    window: Occurrence | None = None
    log: TaskLog | None = None
    order: tuple = ()
    #: 累计打卡任务的进度与状态（其它类型为 ``None``）
    checkin: "CumulativeStatus | None" = None

    @property
    def is_recurring(self) -> bool:
        return self.task.is_recurring

    @property
    def is_cumulative(self) -> bool:
        return self.task.is_cumulative

    @property
    def progress_text(self) -> str:
        """累计打卡任务的进度短语（``45/60（75%）``）；其它类型为空串。"""
        return self.checkin.progress_text() if self.checkin is not None else ""

    @property
    def title_text(self) -> str:
        """列表 #0 列的文字（重要任务带星标）。"""
        if self.task.priority >= 2 and not self.done:
            return f"★ {self.task.title}"
        return self.task.title

    def tags(self) -> tuple[str, ...]:
        """Treeview 行标签（颜色）。"""
        if self.done:
            return ("late",) if self.late else ("done",)
        if self.overdue:
            return ("overdue",)
        if self.task.priority >= 2:
            return ("important",)
        return ()


def periodic_countdown(task: Task, window: Occurrence | None, log_row: TaskLog | None,
                       now: datetime) -> tuple[str, bool, bool]:
    """周期任务这一次的 ``(倒计时文案, 是否已超期, 是否迟到完成)``。

    文案刻意用"已打卡"而不是"已完成"：周期任务的完成**只对今天这一次有效**，
    写成"已完成"容易被读成"这个任务永久完成了"。
    """
    if window is None:
        return "今天不排班", False, False
    if log_row is not None and log_row.completed:
        if log_row.was_late and log_row.completed_at:
            late = fmt_duration(max(0.0, (log_row.completed_at - window.end).total_seconds()),
                                with_seconds=False)
            return f"已打卡（迟到 {late}）", False, True
        return "已打卡", False, bool(log_row.was_late)
    if now < window.start:
        return f"还剩 {fmt_duration((window.start - now).total_seconds(), with_seconds=False)}", False, False
    if now <= window.end:
        remain = fmt_duration((window.end - now).total_seconds(), with_seconds=False)
        return f"进行中，还剩 {remain}", False, False
    return f"已超期 {fmt_duration((now - window.end).total_seconds(), with_seconds=False)}", True, False


def build_task_rows(store: UsageStore, status: str = "pending", now: datetime | None = None,
                    horizon_days: int = HORIZON_DAYS) -> dict[str, list[TaskRow]]:
    """把任务整理成「今日待办 / 本周待办 / 其他」三组。

    刻意做成不依赖 Tk 的纯函数（只读数据库），这样分组口径可以直接单测。

    口径：

    * **单次任务**：按截止时间归属 —— 今天（含已超期）/ 今后 7 天内 / 其他（无期限或更远）。
    * **周期任务**：今天排班的那一次归"今日待办"；今天不排班但 7 天内会排班的归"本周待办"。
      周期任务**永不**写 ``tasks.completed``，完成状态一律看 ``task_logs``。
    * ``status`` 过滤：``pending`` 只给未完成的，``completed`` 只给已完成的，
      ``all`` 两者都给（周期任务在"全部"里会同时出现已完成和待完成的各一次发生）。
    """
    now = now or datetime.now()
    today = now.date()
    last_day = today + timedelta(days=max(1, horizon_days) - 1)
    logs = {row.task_id: row for row in store.task_logs_on(today)}
    groups: dict[str, list[TaskRow]] = {key: [] for key in GROUP_ORDER}

    # ---------------------------------------------------------- 单次任务
    for task in store.list_tasks(status):
        if task.due_at is None:
            group = GROUP_OTHER
        elif task.due_at.date() <= today:
            group = GROUP_TODAY
        elif task.due_at.date() <= last_day:
            group = GROUP_WEEK
        else:
            group = GROUP_OTHER
        overdue = task.is_overdue(now)
        if task.completed:
            status_text = f"{STATUS_DONE} 已完成"
        elif overdue:
            status_text = f"{STATUS_OVERDUE} 已超期"
        else:
            status_text = f"{STATUS_PENDING} 待办"
        groups[group].append(TaskRow(
            task=task, group=group, done=task.completed, overdue=overdue, late=task.was_late(),
            status_text=status_text, schedule_text=task.due_text(),
            countdown_text=task.countdown_text(now),
            order=(task.due_at or datetime.max, task.title),
        ))

    # ---------------------------------------------------------- 周期任务：今天
    recurring = store.recurring_tasks()          # 只查一次，下面两段共用
    for task in recurring:
        if not task.rule.occurs_on(today):
            continue
        log_row = logs.get(task.id)
        done_today = bool(log_row and log_row.completed)
        if status == "pending" and done_today:
            continue
        if status == "completed" and not done_today:
            continue
        window = task.occurrence_on(today)
        text, overdue, late = periodic_countdown(task, window, log_row, now)
        if done_today:
            status_text = f"{STATUS_DONE} 已打卡"
            overdue = False
        elif overdue:
            status_text = f"{STATUS_OVERDUE} 已超期"
        else:
            status_text = f"{STATUS_PENDING} 待完成"
        groups[GROUP_TODAY].append(TaskRow(
            task=task, group=GROUP_TODAY, done=done_today, overdue=overdue, late=late,
            status_text=status_text, schedule_text=task.schedule_text, countdown_text=text,
            occur_date=today, window=window, log=log_row,
            order=(window.start if window else datetime.max, task.title),
        ))

    # ---------------------------------------------------------- 周期任务：本周稍后
    if status != "completed":
        for task in recurring:
            if task.occurrence_on(today) is not None:
                continue                    # 今天排班的已经在上面处理，避免重复
            upcoming = None
            for offset in range(1, max(1, horizon_days)):
                upcoming = task.occurrence_on(today + timedelta(days=offset))
                if upcoming is not None:
                    break
            if upcoming is None:
                continue                    # 7 天内都不排班（正常不会出现）
            weekday = WEEKDAY_CN[upcoming.day.isoweekday()]
            groups[GROUP_WEEK].append(TaskRow(
                task=task, group=GROUP_WEEK, status_text=f"{STATUS_PENDING} 待完成",
                schedule_text=f"{upcoming.day:%m-%d}（{weekday}）{task.rule.schedule_text()}",
                countdown_text=f"还有 {(upcoming.day - today).days} 天",
                occur_date=upcoming.day, window=upcoming,
                order=(datetime.combine(upcoming.day, task.rule.time_start), task.title),
            ))

    # ---------------------------------------------------------- 累计打卡任务
    # 单独一组：「今天做不做」由用户自己决定，所以它不属于"今日待办"；
    # 状态一律走 cumulative_status()，**截止日之前只会是"进行中"**。
    counts = store.checkin_dates_map()           # 一次查询，避免逐条 COUNT/查历史
    for task in store.cumulative_tasks():
        state = cumulative_status(task.rule, counts.get(task.id, []), today)
        # 过滤器：pending = 还没达成目标（含进行中与逾期未达标）；completed = 已达成
        if status == "pending" and state.finished:
            continue
        if status == "completed" and not state.finished:
            continue
        row = TaskRow(
            task=task, group=GROUP_CHECKIN,
            done=state.finished, overdue=state.missed,
            status_text=cumulative_row_texts(state)[0],
            schedule_text=(f"每天 {format_hhmm(task.rule.remind_time or time(18, 0))} 提醒"
                           + (f" · 截止 {task.rule.deadline:%m-%d}"
                              if task.rule.deadline is not None else "")),
            countdown_text=cumulative_row_texts(state)[1],
            checkin=state,
            # 排序：进行中的按"今天还没打卡"优先，然后是截止日近的在前
            order=(0 if state.is_running and not state.today_checked else 1,
                   task.rule.deadline or date.max, task.title),
        )
        groups[GROUP_CHECKIN].append(row)

    for rows in groups.values():
        rows.sort(key=lambda row: row.order)
    return groups


# ================================================================ 任务面板
class TaskPanel(ttk.Frame):
    """待办任务面板：分组列表（今日 / 本周 / 其他）+ 增删改查 + 完成情况图表。"""

    COLUMNS = ("status", "schedule", "countdown")
    #: 首次布局时给下半窗格（图表）预留的最小高度，避免图表被压得看不见标题
    CHART_MIN_HEIGHT = 170

    def __init__(self, master: tk.Misc, store: UsageStore, app=None,
                 font_family: str = "Microsoft YaHei UI", **kwargs) -> None:
        super().__init__(master, style="Card.TFrame", padding=12, **kwargs)
        self.store = store
        self.app = app
        self.font_family = font_family
        self.font_body = (font_family, 10)
        self.font_small = (font_family, 9)
        self.filter_var = tk.StringVar(value="pending")
        self.counts_var = tk.StringVar(value="")
        self.hint_var = tk.StringVar(value="")
        self._task_map: dict[str, Task] = {}
        self._row_map: dict[str, TaskRow] = {}
        self._group_items: dict[str, str] = {}
        self._group_open: dict[str, bool] = {}
        self._refresh_day: date | None = None

        self._build()
        self.refresh()

    # ------------------------------------------------------------------ 布局
    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        # 固定行（标题栏 / 过滤条 / 操作按钮）weight=0；
        # 中间用一只可拖拽的 Panedwindow 承载「任务列表」与「完成情况图表」，
        # 由用户自己决定两者的高度比例 —— 这样无论屏幕多高都不会把图表裁掉。
        for row in (0, 1, 3):
            self.rowconfigure(row, weight=0)
        self.rowconfigure(2, weight=1)      # Panedwindow 占据剩余全部高度

        # ---- 顶部：概览 + 操作按钮 ----
        header = ttk.Frame(self, style="Card.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="✅ 待办任务", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.counts_var, style="CardHint.TLabel").grid(
            row=0, column=1, sticky="w", padx=(12, 0)
        )
        ttk.Button(header, text="➕ 添加任务", style="Accent.TButton", command=self.add_task).grid(
            row=0, column=2, padx=(0, 6)
        )
        ttk.Button(header, text="🔄 刷新", style="Small.TButton", command=self.refresh).grid(row=0, column=3)

        # ---- 过滤条（提示信息并入同一行，省一行高度）----
        toolbar = ttk.Frame(self, style="Card.TFrame")
        toolbar.grid(row=1, column=0, sticky="ew", pady=(8, 6))
        ttk.Label(toolbar, text="显示：", style="Card.TLabel").pack(side="left")
        for text, value in (("未完成", "pending"), ("已完成", "completed"), ("全部", "all")):
            ttk.Radiobutton(
                toolbar, text=text, value=value, variable=self.filter_var,
                style="Card.TRadiobutton", command=self.refresh,
            ).pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="清空已完成", style="Small.TButton",
                   command=self.clear_completed).pack(side="left", padx=(10, 0))
        ttk.Label(toolbar, textvariable=self.hint_var, style="CardHint.TLabel").pack(
            side="right", padx=(10, 0)
        )

        # ---- 任务列表 + 完成情况图表（可拖拽分配高度）----
        style_panedwindow(self)
        self.paned = ttk.Panedwindow(self, orient="vertical", style="Task.TPanedwindow")
        self.paned.grid(row=2, column=0, sticky="nsew")

        list_frame = ttk.Frame(self.paned, style="Card.TFrame")
        table = tk.Frame(list_frame, bg=PANEL_ALT)
        table.pack(fill="both", expand=True)
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)

        # 用 #0 列（tree 列）承载任务内容：这样"今日待办 / 本周待办 / 其他"
        # 是真正可折叠的分组节点（自带三角展开箭头），而不是伪装成普通行的分隔条。
        self.tree = ttk.Treeview(table, columns=self.COLUMNS, show="tree headings",
                                 selectmode="browse", height=3)
        self.tree.heading("#0", text="任务内容")
        self.tree.heading("status", text="状态")
        self.tree.heading("schedule", text="计划 / 截止")
        self.tree.heading("countdown", text="剩余 / 超期")
        self.tree.column("#0", width=340, minwidth=200, anchor="w")
        self.tree.column("status", width=96, anchor="center", stretch=False)
        self.tree.column("schedule", width=240, anchor="center", stretch=False)
        self.tree.column("countdown", width=170, anchor="center", stretch=False)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)

        self.tree.tag_configure("group", foreground=ACCENT, font=(self.font_family, 10, "bold"))
        self.tree.tag_configure("overdue", foreground=RED)
        self.tree.tag_configure("done", foreground=SUB)
        self.tree.tag_configure("late", foreground=AMBER)
        self.tree.tag_configure("important", foreground=PURPLE)
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Button-3>", self._show_context_menu)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._update_hint())
        self.paned.add(list_frame, weight=4)          # 上：任务列表

        # ---- 行内操作按钮 ----
        actions = ttk.Frame(self, style="Card.TFrame")
        actions.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(actions, text="✔ 完成 / 打卡 / 撤销", style="Small.TButton",
                   command=self.toggle_selected).pack(side="left")
        # 累计打卡任务专用：选中打卡任务时才可用（其余情况置灰，避免点了没反应）
        self.checkin_button = ttk.Button(actions, text="✓ 打卡任务打卡", style="Accent.TButton",
                                         command=self.check_in_selected)
        self.checkin_button.pack(side="left", padx=(6, 0))
        self.checkin_button.state(["disabled"])
        ttk.Button(actions, text="✏ 编辑", style="Small.TButton",
                   command=self.edit_selected).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="⏰ 延后 1 小时", style="Small.TButton",
                   command=lambda: self.postpone_selected(60)).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="🗑 删除", style="Danger.TButton",
                   command=self.delete_selected).pack(side="left", padx=(6, 0))
        ttk.Label(actions, text="提示：双击分组折叠，双击任务编辑，右键更多操作",
                  style="CardHint.TLabel").pack(side="right")

        # ---- 图表（放在下半窗格，可拖拽调整高度）----
        self.chart_frame = ttk.Frame(self.paned, style="Card.TFrame")
        self.chart = TaskStatsChart(self.chart_frame, self.store, font_family=self.font_family)
        ttk.Label(self.chart_frame, textvariable=self.chart.summary_var,
                  style="CardHint.TLabel").pack(anchor="w", pady=(6, 0))
        self.chart.pack(fill="both", expand=True, pady=(2, 0))
        self.paned.add(self.chart_frame, weight=6)    # 下：完成情况图表
        self.paned.bind("<Configure>", self._set_initial_sash, add="+")

        # ---- 累计打卡任务详情（进度条 + 打卡热力图）----
        # 与图表**共用**下半窗格：选中打卡任务时把图表换成打卡详情，切回其它任务再换回来。
        # 不用"图表 + 详情"上下叠着放，是因为可用高度只有 ~700px，
        # 再塞 200px 会把任务列表压到只剩两行。
        #
        # 注意这里**延迟导入**：checkin_view 要用本模块的配色常量（ACCENT/PANEL 等），
        # 模块级互相 import 会变成循环导入（实测 ImportError: partially initialized module）。
        # 真正干净的解法是把配色抽到独立的 theme 模块，留待后续重构。
        from .checkin_view import CumulativeDetail      # noqa: PLC0415
        self.detail_frame = ttk.Frame(self.paned, style="Card.TFrame")
        self.detail = CumulativeDetail(
            self.detail_frame, font_family=self.font_family,
            on_check_in=self._detail_check_in, on_undo=self._detail_undo,
            on_backfill=self._detail_backfill,
        )
        self.detail.pack(fill="both", expand=True)
        self._detail_visible = False

        # 右键菜单（周期任务不能"延后"，选中周期任务时这几项会被禁用）
        self.context_menu = tk.Menu(self, tearoff=0, bg=PANEL_ALT, fg=FG,
                                    activebackground=ACCENT, activeforeground="#ffffff",
                                    font=self.font_body, bd=0)
        self.context_menu.add_command(label="标记完成 / 取消完成", command=self.toggle_selected)
        self.context_menu.add_command(label="✓ 打卡 / 撤销今天的打卡", command=self.check_in_selected)
        self.context_menu.add_command(label="📝 补签过去的某一天…", command=self.backfill_selected)
        self.context_menu.add_command(label="编辑任务", command=self.edit_selected)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="延后 1 小时", command=lambda: self.postpone_selected(60))
        self.context_menu.add_command(label="延后到明天此时", command=lambda: self.postpone_selected(60 * 24))
        self.context_menu.add_separator()
        self.context_menu.add_command(label="删除任务", command=self.delete_selected)

    def _set_initial_sash(self, event: tk.Event) -> None:
        """只在首次布局时定一次分隔条位置（之后交给用户拖拽）。

        用 Panedwindow 而不是固定 grid 权重，是因为不同屏幕可用高度差别很大：
        固定比例在 688 高的可视区里会把图表裁掉，而分隔条方案在任何高度下
        都完整可见，用户想多看哪边就拉哪边。

        位置取"列表拿一半"与"给图表留够 CHART_MIN_HEIGHT"两者中更保守的那个：
        * 老版本是 38%，但列表改成"今日 / 本周 / 其他"分组后多了分组标题行，
          38% 只能看到两三条任务，分组反而看不全；
        * 纯粹按 50% 分，在 728 高的屏幕上图表只剩 165px，matplotlib 的标题会被
          挤掉一截（不过这个"挤"是它自己按比例缩放，不会真的裁掉坐标轴）。
        """
        if getattr(self, "_sash_done", False) or event.widget is not self.paned:
            return
        height = self.paned.winfo_height()
        if height < 160:
            return
        self._sash_done = True
        half = int(height * 0.5)
        keep_list = int(height * 0.32)          # 列表至少也要能看见分组标题 + 两行
        position = int(min(half, max(keep_list, height - self.CHART_MIN_HEIGHT)))
        try:
            self.paned.sashpos(0, max(1, min(position, height - 40)))
        except tk.TclError:
            log.debug("设置分隔条位置失败", exc_info=True)

    # ------------------------------------------------------------------ 数据刷新
    def refresh(self) -> None:
        """按当前过滤条件重建分组列表与图表。

        分组节点会保留用户上次的折叠状态（默认全部展开），选中行也会尽量保留。
        """
        status = self.filter_var.get()
        now = datetime.now()
        self._refresh_day = now.date()
        selected_id = self.selected_task()
        selected_id = selected_id.id if selected_id else None
        self._remember_group_state()

        for item in self.tree.get_children():
            self.tree.delete(item)
        self._task_map.clear()
        self._row_map.clear()
        self._group_items.clear()

        groups = build_task_rows(self.store, status, now)
        for key in GROUP_ORDER:
            rows = groups.get(key) or []
            if not rows:
                continue
            done = sum(1 for row in rows if row.done)
            group_id = self.tree.insert(
                "", "end", text=f"{GROUP_TITLES[key]}（{done}/{len(rows)}）",
                values=("", "", ""), tags=("group",), open=self._group_open.get(key, True),
            )
            self._group_items[key] = group_id
            for row in rows:
                item_id = self.tree.insert(
                    group_id, "end", text=row.title_text,
                    values=(row.status_text, row.schedule_text, row.countdown_text),
                    tags=row.tags(),
                )
                self._task_map[item_id] = row.task
                self._row_map[item_id] = row
                if selected_id is not None and row.task.id == selected_id:
                    self.tree.selection_set(item_id)
                    # 只在分组本来就是展开的时候滚动到选中行：
                    # ``see()`` 会**自动展开**祖先节点，无条件调用会让用户刚折叠好的
                    # 分组在每次刷新（每秒都可能发生）后又被弹开。
                    if self.tree.item(group_id, "open"):
                        self.tree.see(item_id)

        counts = self.store.task_counts()
        self.counts_var.set(counts.summary_text())
        self._update_hint()
        try:
            self.chart.refresh(days=7)
        except Exception:  # noqa: BLE001
            log.debug("刷新任务图表失败", exc_info=True)

    def _remember_group_state(self) -> None:
        """在重建列表之前记下各分组的展开状态。"""
        for key, item_id in self._group_items.items():
            try:
                self._group_open[key] = bool(self.tree.item(item_id, "open"))
            except tk.TclError:
                continue

    def _sync_checkin_button(self, row: TaskRow | None) -> None:
        """按当前选中行启用/禁用「打卡」按钮并改文案（避免"点了没反应"）。"""
        button = getattr(self, "checkin_button", None)
        if button is None:
            return
        try:
            if row is None or not row.is_cumulative:
                button.state(["disabled"])
                button.configure(text="✓ 打卡任务打卡")
                return
            state = row.checkin
            if state is not None and state.today_checked:
                button.state(["!disabled"])
                button.configure(text="↩ 撤销今日打卡")
            elif state is not None and (state.finished or state.missed):
                button.state(["disabled"])
                button.configure(text="✓ 打卡任务打卡")
            else:
                button.state(["!disabled"])
                button.configure(text="✓ 今日打卡")
        except tk.TclError:
            log.debug("同步打卡按钮失败", exc_info=True)

    def _sync_detail(self, row: TaskRow | None) -> None:
        """选中的是打卡任务 → 下半窗格显示进度条 + 热力图；否则换回 7 天图表。"""
        want_detail = bool(row is not None and row.is_cumulative)
        if want_detail == getattr(self, "_detail_visible", False):
            if want_detail and row is not None:
                self._fill_detail(row)          # 已经显示着：只更新内容（打卡后要刷新）
            return
        try:
            if want_detail:
                self.paned.forget(self.chart_frame)
                self.paned.add(self.detail_frame, weight=6)
                # 刚 add 进去时 paned 的高度还是旧值（布局没落定），
                # 所以先试一次，再 after_idle 校正一次 —— 否则算出来的
                # "还差多少像素"是错的，最下面那行按钮就留在可视区外了。
                self._ensure_detail_height()
                self.after_idle(self._ensure_detail_height)
            else:
                self.paned.forget(self.detail_frame)
                self.paned.add(self.chart_frame, weight=6)
            self._detail_visible = want_detail
        except tk.TclError:
            log.debug("切换下半窗格失败", exc_info=True)
            return
        if want_detail and row is not None:
            self._fill_detail(row)
        else:
            try:
                self.detail.clear()
            except tk.TclError:
                log.debug("清空打卡详情失败", exc_info=True)

    def _ensure_detail_height(self) -> None:
        """给打卡详情面板腾出它**自己要求**的高度（不够就把分隔条往下推）。

        为什么不写死一个常数：详情面板的高度取决于字体缩放（133% 缩放下实测 201px），
        写死 205 在别的缩放下就会把最下面那行按钮裁掉。这里直接问控件要
        ``winfo_reqheight()``，再留 8px 余量，并且给列表保底 90px。
        """
        try:
            self.update_idletasks()       # 先让布局落定，否则量到的高度是旧值
            needed = max(180, min(300, int(self.detail.winfo_reqheight()) + 8))
            height = self.paned.winfo_height()
            if height < 220:
                return                    # 窗口太矮：两边都放不下，交给用户自己拖
            sash = self.paned.sashpos(0)
            if height - sash >= needed:
                return
            self.paned.sashpos(0, max(90, height - needed))
            log.debug("打卡详情需要 %dpx，已把分隔条推到 %d", needed, self.paned.sashpos(0))
        except tk.TclError:
            log.debug("调整打卡详情高度失败", exc_info=True)

    def _fill_detail(self, row: TaskRow) -> None:
        """把选中打卡任务的进度与历史喂给详情组件。"""
        status = row.checkin
        if status is None:
            status = cumulative_status(row.task.rule, self.store.checkin_dates(row.task.id))
        try:
            self.detail.show_task(row.task, status)
            self.detail.set_checked_days(self.store.checkin_dates(row.task.id))
        except tk.TclError:
            log.debug("刷新打卡详情失败", exc_info=True)

    def _detail_check_in(self, task_id: int) -> tuple[bool, str]:
        """详情面板里的「✓ 打卡」——和列表按钮走同一条路径。"""
        return self._do_check_in(task_id)

    def _detail_backfill(self, task_id: int) -> tuple[bool, str]:
        """详情面板里的「📝 补签…」：选日期 + 写备注，然后交给数据层判定。"""
        task = self.store.get_task(task_id)
        if task is None:
            return False, "任务不存在"
        dialog = BackfillDialog(self, task, self.store, font_family=self.font_family)
        self.wait_window(dialog)
        if dialog.result is None:
            return False, "已取消补签"
        day, note = dialog.result
        ok, message = self.store.check_in(task_id, day, note=note)
        if ok:
            self.refresh()
            self._reschedule_tasks()
            self._notify_app(f"{task.title}：{message}")
        return ok, message

    def backfill_selected(self) -> None:
        """右键菜单里的「📝 补签」：对选中的打卡任务补签。"""
        row = self._require_row()
        if row is None:
            return
        if not row.is_cumulative:
            messagebox.showinfo("提示", f"「{row.task.title}」不是累计打卡任务。", parent=self)
            return
        ok, message = self._detail_backfill(row.task.id)
        if not ok and message != "已取消补签":
            messagebox.showinfo("提示", message, parent=self)

    def _detail_undo(self, task_id: int) -> tuple[bool, str]:
        """详情面板里的「撤销今日打卡」。"""
        today = datetime.now().date()
        if not self.store.undo_check_in(task_id, today):
            return False, "今天没有打卡记录"
        self.refresh()
        self._reschedule_tasks()
        return True, "已撤销今天的打卡"

    def _do_check_in(self, task_id: int) -> tuple[bool, str]:
        """打卡并刷新界面（列表 + 详情 + 调度器）。"""
        ok, message = self.store.check_in(task_id)
        if ok:
            self.refresh()
            self._reschedule_tasks()
        return ok, message

    def _update_hint(self) -> None:
        """提示条：选中行的详细信息（没选中就显示今日总览）。"""
        row = self.selected_row()
        self._sync_checkin_button(row)
        self._sync_detail(row)
        if row is None:
            counts = self.store.task_counts()
            text = f"待办 {counts.pending} 条"
            if counts.overdue:
                text += f"，其中已超期 {counts.overdue} 条"
            self.hint_var.set(text)
            return
        task = row.task
        if task.is_cumulative:
            # 累计打卡任务：状态一律来自 cumulative_status()，
            # 截止日之前**只可能是"进行中"**（用户的核心诉求）
            status = row.checkin or cumulative_status(task.rule, [], datetime.now().date())
            bits = [status.status_text(), status.progress_text(), status.countdown_text()]
            if task.rule.deadline is not None:
                bits.append(f"截止 {task.rule.deadline:%Y-%m-%d}")
            bits.append(f"每天 {format_hhmm(task.rule.remind_time or time(18, 0))} 提醒")
            self.hint_var.set("　·　".join(bits))
            return
        if task.is_recurring:
            bits = [task.schedule_text, f"开始前 {task.rule.remind_before_minutes} 分钟提醒"]
            if row.occur_date is not None:
                state = "已打卡" if row.done else ("已超期" if row.overdue else "待完成")
                bits.append(f"{row.occur_date:%m-%d} {state}")
            self.hint_var.set("　·　".join(bits))
            return
        if task.completed:
            done_at = task.completed_at.strftime("%Y-%m-%d %H:%M") if task.completed_at else "—"
            self.hint_var.set(f"已完成于 {done_at}")
        elif task.due_at:
            remain = task.remaining_seconds()
            text = fmt_duration(abs(remain or 0), with_seconds=False)
            self.hint_var.set(f"{'已超期 ' + text if (remain or 0) < 0 else '还剩 ' + text}　（截止 {task.due_at:%Y-%m-%d %H:%M}）")
        else:
            self.hint_var.set("无期限任务")

    # ------------------------------------------------------------------ 选中项
    def selected_row(self) -> TaskRow | None:
        """当前选中的行（选中的是分组节点或没选中时返回 ``None``）。"""
        selection = self.tree.selection()
        if not selection:
            return None
        return self._row_map.get(selection[0])

    def selected_task(self) -> Task | None:
        """当前选中的任务（没有选中返回 None）。"""
        selection = self.tree.selection()
        if not selection:
            return None
        return self._task_map.get(selection[0])

    def _require_row(self) -> TaskRow | None:
        """取选中行，没有则提示并返回 None。"""
        row = self.selected_row()
        if row is None:
            messagebox.showinfo("提示", "请先在列表中选择一条任务。", parent=self)
            return None
        return row

    def _require_selection(self) -> Task | None:
        """取选中任务，没有则提示并返回 None。"""
        row = self._require_row()
        return row.task if row is not None else None

    # ------------------------------------------------------------------ 增删改
    def add_task(self) -> None:
        """弹出对话框新增任务（单次 / 每天 / 每周 / 累计打卡）。"""
        dialog = TaskDialog(self, font_family=self.font_family)
        self.wait_window(dialog)
        draft = dialog.result
        if draft is None:
            return
        task_id = self.store.add_task(
            draft.title,
            None if (draft.is_recurring or draft.is_cumulative) else draft.due_at,
            draft.priority,
            task_type=draft.task_type, time_start=draft.time_start, time_end=draft.time_end,
            days_of_week=draft.days_of_week, remind_before_minutes=draft.remind_before_minutes,
            target_count=draft.target_count if draft.is_cumulative else None,
            deadline=draft.deadline if draft.is_cumulative else None,
            remind_time=draft.remind_time if draft.is_cumulative else None,
        )
        if task_id is None:
            messagebox.showerror(
                "失败",
                "任务保存失败。「每周」任务至少要选一天；其他原因请查看日志。",
                parent=self,
            )
            return
        if self.filter_var.get() == "completed":
            self.filter_var.set("pending")
        self.refresh()
        self._reschedule_tasks()
        self._notify_app(f"已添加{draft.type_label}任务：{draft.title}")

    def edit_selected(self) -> None:
        """编辑选中任务。"""
        task = self._require_selection()
        if task is None:
            return
        dialog = TaskDialog(self, font_family=self.font_family, task=task)
        self.wait_window(dialog)
        draft = dialog.result
        if draft is None:
            return
        if self.store.update_task(
            task.id, title=draft.title,
            due_at=None if (draft.is_recurring or draft.is_cumulative) else draft.due_at,
            clear_due=(draft.is_recurring or draft.is_cumulative) and task.due_at is not None,
            priority=draft.priority,
            task_type=draft.task_type, time_start=draft.time_start, time_end=draft.time_end,
            days_of_week=draft.days_of_week, remind_before_minutes=draft.remind_before_minutes,
            target_count=draft.target_count if draft.is_cumulative else None,
            deadline=draft.deadline if draft.is_cumulative else None,
            remind_time=draft.remind_time if draft.is_cumulative else None,
        ):
            self.refresh()
            self._reschedule_tasks()
            self._notify_app(f"已更新任务：{draft.title}")

    def _on_double_click(self, event: tk.Event) -> None:
        """双击分组节点 = 折叠 / 展开；双击打卡任务 = 直接打卡；双击其它任务 = 编辑。"""
        item_id = self.tree.identify_row(event.y)
        if not item_id:
            return
        if item_id in self._group_items.values():
            self.tree.item(item_id, open=not self.tree.item(item_id, "open"))
            return
        row = self._row_map.get(item_id)
        if row is not None and row.is_cumulative:
            self.check_in_selected()       # 打卡任务：双击即打卡（和周期任务双击打卡一致）
            return
        if item_id in self._task_map:
            self.edit_selected()

    def check_in_selected(self) -> None:
        """给选中的累计打卡任务打卡（已打卡则撤销当天那次）。"""
        row = self._require_row()
        if row is None:
            return
        task = row.task
        if not task.is_cumulative:
            messagebox.showinfo("提示", f"「{task.title}」不是累计打卡任务。", parent=self)
            return
        today = datetime.now().date()
        if row.checkin is not None and row.checkin.today_checked:
            removed = self.store.undo_check_in(task.id, today)
            ok = bool(removed)
            message = "已撤销今天的打卡" if ok else "撤销失败：今天没有打卡记录"
        else:
            ok, message = self.store.check_in(task.id, today)
        if not ok:
            messagebox.showinfo("提示", message, parent=self)
            return
        self.refresh()
        self._reschedule_tasks()       # 今天已打卡 → 调度器要改到明天再提醒
        self._notify_app(f"{task.title}：{message}")

    def toggle_selected(self) -> None:
        """切换选中任务的状态：单次任务标记完成，周期任务给"今天"打卡，累计任务打卡。"""
        row = self._require_row()
        if row is None:
            return
        task = row.task

        if task.is_cumulative:
            # 累计打卡任务绝不能走 complete_task()：那会写 tasks.completed，
            # 把"打了 45 次卡"变成"这个任务永久完成了"（跟周期任务同一个坑）。
            self.check_in_selected()
            return

        if task.is_recurring:
            today = datetime.now().date()
            window = task.occurrence_on(today)
            if window is None:
                messagebox.showinfo("提示", f"「{task.title}」今天不在这条周期任务的时间表里。", parent=self)
                return
            if row.done:
                self.store.complete_occurrence(task.id, today, completed=False)
                note = "已撤销今天的打卡"
            else:
                self.store.complete_occurrence(task.id, today, completed=True)
                log_row = self.store.get_task_log(task.id, today)
                note = "已打卡完成"
                if log_row is not None and log_row.was_late:
                    note += "（已过结束时间，记为迟到完成）"
            self.refresh()
            self._reschedule_tasks()   # 打卡不改规则，但重排代价极低，保证状态一致
            self._notify_app(f"{task.title}：{note}")
            return

        self.store.complete_task(task.id, not task.completed)
        self.refresh()
        self._reschedule_tasks()      # 打卡/撤销不影响后续提醒，但重排代价极低，保证一致
        if task.completed:
            self._notify_app(f"已重新打开：{task.title}")
        else:
            late_note = ""
            if task.due_at and datetime.now() > task.due_at:
                late_note = "（已超期完成）"
            self._notify_app(f"完成！{task.title}{late_note}")

    def delete_selected(self) -> None:
        """删除选中任务（带确认）。周期任务的打卡历史会一并删除。"""
        task = self._require_selection()
        if task is None:
            return
        extra = "（含全部打卡历史）" if (task.is_recurring or task.is_cumulative) else ""
        if not messagebox.askyesno("确认删除", f"确定要删除任务「{task.title}」吗？{extra}", parent=self):
            return
        self.store.delete_task(task.id)
        self.refresh()
        self._reschedule_tasks()
        self._notify_app("任务已删除")

    def postpone_selected(self, minutes: int = 60) -> None:
        """把截止时间往后推（默认 1 小时）。周期任务与累计打卡任务不支持。"""
        task = self._require_selection()
        if task is None:
            return
        if task.is_cumulative:
            messagebox.showinfo(
                "提示",
                f"「{task.title}」是累计打卡任务，截止日期是整体目标的一部分，不能单独延后。\n\n"
                "需要调整请在「编辑」里改「最终截止日期」。",
                parent=self,
            )
            return
        if task.is_recurring:
            messagebox.showinfo(
                "提示",
                f"「{task.title}」是周期任务，时间由「时间段」决定，不能单独延后。\n\n"
                "需要临时调整请用「编辑」改时间段；这一次已经做完了就直接打卡。",
                parent=self,
            )
            return
        base = task.due_at or datetime.now()
        new_due = base + timedelta(minutes=minutes)
        if self.store.update_task(task.id, due_at=new_due):
            self.refresh()
            self._reschedule_tasks()
            self._notify_app(f"已延后到 {new_due:%m-%d %H:%M}")

    def clear_completed(self) -> None:
        """清空所有已完成的**单次**任务（周期任务的打卡历史属于统计依据，不在这里删）。"""
        done_single = self.store.list_tasks("completed")
        if not done_single:
            messagebox.showinfo("提示", "没有已完成的单次任务。（周期任务的打卡记录会保留，用于图表统计）",
                                parent=self)
            return
        if not messagebox.askyesno(
            "确认",
            f"确定要删除全部 {len(done_single)} 条已完成的单次任务吗？\n\n"
            "周期任务的打卡记录不会被删除（它们是图表统计的依据）。",
            parent=self,
        ):
            return
        removed = self.store.clear_completed_tasks()
        self.refresh()
        self._reschedule_tasks()
        self._notify_app(f"已清理 {removed} 条已完成任务")

    def _show_context_menu(self, event: tk.Event) -> None:
        """右键菜单（按选中的是分组节点 / 单次任务 / 周期任务调整可用项）。"""
        item_id = self.tree.identify_row(event.y)
        if item_id:
            self.tree.selection_set(item_id)
            self._update_hint()
        row = self.selected_row()
        task = row.task if row is not None else None
        if task is not None and task.is_cumulative:
            checked = bool(row is not None and row.checkin is not None
                           and row.checkin.today_checked)
            label = "撤销今天的打卡" if checked else "✓ 今天就打卡"
        elif task is not None and task.is_recurring:
            label = "打卡完成 / 撤销打卡"
        else:
            label = "标记完成 / 取消完成"
        try:
            self.context_menu.entryconfigure(0, label=label)
            # 「补签」只对累计打卡任务可用
            self.context_menu.entryconfigure(
                2, state="normal" if (task is not None and task.is_cumulative) else "disabled")
            postpone_state = "disabled" if (task is None or task.is_recurring
                                            or task.is_cumulative) else "normal"
            self.context_menu.entryconfigure(4, state=postpone_state)
            self.context_menu.entryconfigure(5, state=postpone_state)
        except tk.TclError:
            log.debug("刷新右键菜单状态失败", exc_info=True)
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()

    def _notify_app(self, message: str) -> None:
        """把提示信息交给主界面底部提示条（没有 app 就忽略）。"""
        if self.app is not None and hasattr(self.app, "show_toast_message"):
            try:
                self.app.show_toast_message(message)
            except Exception:  # noqa: BLE001
                log.debug("提示条刷新失败", exc_info=True)

    def _reschedule_tasks(self) -> None:
        """任务增删改后重排周期调度器。

        不做这一步的后果：调度器下一次唤醒最长在 30 分钟后，
        期间新增的周期任务可能整段错过"提前提醒 + 补发"窗口，当天就不提醒了。
        """
        if self.app is None:
            return
        callback = getattr(self.app, "reschedule_recurring", None)
        if not callable(callback):
            return
        try:
            callback("tasks-changed")
        except Exception:  # noqa: BLE001
            log.debug("重排周期任务调度失败", exc_info=True)

    # ------------------------------------------------------------------ 周期刷新
    def tick(self) -> None:
        """主界面每秒调用：只更新会随时间变化的列，避免频繁重建列表。

        跨天时必须整体重建 —— "今天"换了，分组和"今天该做"的口径全都变了。
        """
        now = datetime.now()
        if self._refresh_day is not None and now.date() != self._refresh_day:
            self.refresh()
            return
        for item_id, row in self._row_map.items():
            try:
                if row.is_cumulative:
                    # 累计打卡任务的倒计时只有"跨天"才变，而且它的进度/剩余天数
                    # 不能被单次任务那套 countdown_text() 覆盖（会把"还剩 60 天"
                    # 写成"无期限"，实测踩到过）。跨天时 tick 上面已经整表 refresh 了。
                    continue
                if row.is_recurring:
                    text, overdue, _late = periodic_countdown(row.task, row.window, row.log, now)
                    self.tree.set(item_id, "countdown", text)
                    if not row.done and overdue != row.overdue:
                        # 窗口结束的瞬间：待完成 → 已超期（状态与颜色都要跟着变）
                        row.overdue = overdue
                        row.status_text = f"{STATUS_OVERDUE} 已超期" if overdue else f"{STATUS_PENDING} 待完成"
                        self.tree.set(item_id, "status", row.status_text)
                        self.tree.item(item_id, tags=row.tags())
                else:
                    self.tree.set(item_id, "countdown", row.task.countdown_text(now))
                    overdue = row.task.is_overdue(now)
                    if overdue != row.overdue:
                        row.overdue = overdue
                        if not row.done:
                            row.status_text = f"{STATUS_OVERDUE} 已超期" if overdue else f"{STATUS_PENDING} 待办"
                            self.tree.set(item_id, "status", row.status_text)
                        self.tree.item(item_id, tags=row.tags())
            except tk.TclError:
                continue


# ================================================================ 开机提醒弹窗
class StartupReminderDialog(tk.Toplevel):
    """开机 / 首次运行时的待办提醒弹窗。

    内容严格包含：任务名称、设定的截止时间、以及随机抽取的鼓励语。
    """

    MAX_ROWS = 6
    WIDTH = 560

    def __init__(
        self,
        master: tk.Misc,
        tasks: list[Task],
        font_family: str = "Microsoft YaHei UI",
        on_open_tasks=None,
    ) -> None:
        super().__init__(master)
        self.tasks = tasks
        self.font_family = font_family
        self.on_open_tasks = on_open_tasks
        self.encouragement = phrases.pick()
        self._rows: list[dict] = []

        self.title("TimeGuard 待办提醒")
        self.configure(bg=PANEL)
        self.resizable(False, False)
        try:
            self.attributes("-topmost", True)
            self.attributes("-toolwindow", True)
        except tk.TclError:
            pass
        self.protocol("WM_DELETE_WINDOW", self.close)

        self._build()
        self._place()
        try:
            self.focus_force()
        except tk.TclError:
            pass
        if master is not None:
            try:
                master.after(900, self._bring_to_front)
            except tk.TclError:
                pass

    # ------------------------------------------------------------------
    def _build(self) -> None:
        outer = ttk.Frame(self, style="Card.TFrame", padding=(20, 16))
        outer.pack(fill="both", expand=True)

        overdue = sum(1 for task in self.tasks if task.is_overdue())
        heading = f"今天有 {len(self.tasks)} 条待办任务"
        if overdue:
            heading += f"（{overdue} 条已超期）"

        ttk.Label(outer, text=f"☀ {heading}", style="CardTitle.TLabel",
                  font=(self.font_family, 14, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text=f"以下是未完成的任务，别忘了它们。日期：{datetime.now():%Y-%m-%d}",
            style="CardHint.TLabel",
        ).pack(anchor="w", pady=(2, 10))

        # ---- 任务卡片列表 ----
        list_frame = tk.Frame(outer, bg=PANEL_ALT)
        list_frame.pack(fill="x")
        for index, task in enumerate(self.tasks[: self.MAX_ROWS]):
            self._rows.append(self._build_row(list_frame, task))
        if len(self.tasks) > self.MAX_ROWS:
            tk.Label(
                list_frame, text=f"…… 还有 {len(self.tasks) - self.MAX_ROWS} 条，点击下方按钮查看全部",
                bg=PANEL_ALT, fg=SUB, font=self._font_small(), anchor="w",
            ).pack(fill="x", padx=12, pady=(0, 8))

        # ---- 鼓励语 ----
        quote = tk.Frame(outer, bg="#20293b")
        quote.pack(fill="x", pady=(12, 0))
        tk.Label(quote, text="💪 鼓励一下", bg="#20293b", fg=ACCENT,
                 font=(self.font_family, 10, "bold"), anchor="w").pack(fill="x", padx=12, pady=(8, 0))
        self.quote_label = tk.Label(
            quote, text=self.encouragement, bg="#20293b", fg=FG, wraplength=self.WIDTH - 80,
            justify="left", anchor="w", font=(self.font_family, 11),
        )
        self.quote_label.pack(fill="x", padx=12, pady=(4, 10))

        # ---- 按钮 ----
        buttons = ttk.Frame(outer, style="Card.TFrame")
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(buttons, text="知道了，开始干活", style="Accent.TButton",
                   command=self.close).pack(side="right")
        if self.on_open_tasks is not None:
            ttk.Button(buttons, text="查看待办列表", style="Small.TButton",
                       command=self._open_tasks).pack(side="right", padx=(0, 8))
        ttk.Button(buttons, text="换一句鼓励", style="Small.TButton",
                   command=self._shuffle_quote).pack(side="left")

    def _font_small(self) -> tuple:
        return (self.font_family, 9)

    def _build_row(self, parent: tk.Misc, task: Task) -> dict:
        """构造一行任务卡片：任务名 + 截止时间 + 剩余/超期。"""
        now = datetime.now()
        overdue = task.is_overdue(now)
        row = tk.Frame(parent, bg=PANEL_ALT)
        row.pack(fill="x", padx=10, pady=(8, 0))

        mark = tk.Label(row, text="!" if overdue else "○", bg=PANEL_ALT,
                        fg=RED if overdue else ACCENT, font=(self.font_family, 12, "bold"), width=2)
        mark.pack(side="left", anchor="n")

        text_box = tk.Frame(row, bg=PANEL_ALT)
        text_box.pack(side="left", fill="x", expand=True)
        tk.Label(text_box, text=task.title, bg=PANEL_ALT, fg=FG, anchor="w", justify="left",
                 wraplength=self.WIDTH - 160, font=(self.font_family, 11, "bold")).pack(fill="x")
        tk.Label(
            text_box,
            text=f"截止时间：{task.due_text()}　·　{task.countdown_text(now)}",
            bg=PANEL_ALT, fg=RED if overdue else SUB, anchor="w", font=self._font_small(),
        ).pack(fill="x")

        return {"task": task, "row": row, "countdown": None}

    def _shuffle_quote(self) -> None:
        """换一句鼓励语（点击按钮时）。"""
        self.encouragement = phrases.pick(exclude=self.encouragement)
        try:
            self.quote_label.configure(text=self.encouragement)
        except tk.TclError:
            log.debug("刷新鼓励语失败", exc_info=True)

    def _place(self) -> None:
        """放到屏幕右下角，避免挡住用户正在看的窗口。"""
        place_dialog_bottom_right(self, self.WIDTH)

    def _bring_to_front(self) -> None:
        """周期性把自己顶到最前，避免被其他窗口盖住。"""
        try:
            self.attributes("-topmost", True)
            self.lift()
        except tk.TclError:
            return

    def _open_tasks(self) -> None:
        """打开主界面的待办列表。"""
        if callable(self.on_open_tasks):
            try:
                self.on_open_tasks()
            except Exception:  # noqa: BLE001
                log.debug("打开待办列表失败", exc_info=True)
        self.close()

    def close(self) -> None:
        """关闭弹窗。"""
        try:
            self.destroy()
        except tk.TclError:
            pass


def build_reminder_text(tasks: list[Task]) -> str:
    """生成纯文本版提醒内容（用于系统通知 / 托盘气泡）。"""
    if not tasks:
        return ""
    lines = [f"你有 {len(tasks)} 条待办任务："]
    for task in tasks[:3]:
        due = task.due_text()
        lines.append(f"· {task.title}（截止 {due}）")
    if len(tasks) > 3:
        lines.append(f"…… 还有 {len(tasks) - 3} 条")
    lines.append(phrases.pick())
    return "\n".join(lines)


# ================================================================ 周期任务提醒弹窗
class RecurringReminderDialog(tk.Toplevel):
    """周期性任务的"开始前提醒"弹窗。

    避坑 #5：同一时刻到点的多个任务**合并到一个窗口**，列表展示
    "即将开始：1. 跑步 2. 背单词"，而不是弹出两个窗口。
    """

    MAX_ROWS = 8
    WIDTH = 580

    def __init__(self, master: tk.Misc, batch, font_family: str = "Microsoft YaHei UI",
                 on_open_tasks=None, on_check_in=None) -> None:
        super().__init__(master)
        self.batch = batch
        self.font_family = font_family
        self.on_open_tasks = on_open_tasks
        #: 回调 ``(task_id) -> (是否成功, 给用户看的一句话)``；累计打卡任务的「打卡」按钮用它
        self.on_check_in = on_check_in
        self.encouragement = phrases.pick()
        #: task_id → 该行的按钮与说明文字，打卡成功后就地更新（不重开窗口）
        self._checkin_rows: dict[int, dict] = {}

        only_checkin = bool(batch.items) and all(i.is_cumulative for i in batch.items)
        self.title("TimeGuard 打卡提醒" if only_checkin else "TimeGuard 周期任务提醒")
        self.configure(bg=PANEL)
        self.resizable(False, False)
        try:
            self.attributes("-topmost", True)
            self.attributes("-toolwindow", True)
        except tk.TclError:
            pass
        self.protocol("WM_DELETE_WINDOW", self.close)

        self._build()
        self._place()
        try:
            self.focus_force()
        except tk.TclError:
            pass
        if master is not None:
            try:
                master.after(900, self._bring_to_front)
            except tk.TclError:
                pass

    # ------------------------------------------------------------------
    def _font_small(self) -> tuple:
        return (self.font_family, 9)

    def _build(self) -> None:
        outer = ttk.Frame(self, style="Card.TFrame", padding=(20, 16))
        outer.pack(fill="both", expand=True)

        items = list(self.batch.items)
        heading = f"☀ {self.batch.headline()}"
        ttk.Label(outer, text=heading, style="CardTitle.TLabel",
                  font=(self.font_family, 14, "bold")).pack(anchor="w")
        subtitle = f"现在 {datetime.now():%H:%M}　·　共 {len(items)} 项"
        if self.batch.has_late:
            subtitle += "　·　含迟到补发（错过时段的提醒）"
        ttk.Label(outer, text=subtitle, style="CardHint.TLabel").pack(anchor="w", pady=(2, 10))

        # ---- 任务列表（合并展示） ----
        list_frame = tk.Frame(outer, bg=PANEL_ALT)
        list_frame.pack(fill="x")
        for index, item in enumerate(items[: self.MAX_ROWS], 1):
            self._build_row(list_frame, index, item)
        if len(items) > self.MAX_ROWS:
            tk.Label(list_frame,
                     text=f"…… 还有 {len(items) - self.MAX_ROWS} 项，点击下方按钮查看全部",
                     bg=PANEL_ALT, fg=SUB, font=self._font_small(), anchor="w").pack(
                fill="x", padx=12, pady=(0, 8))

        # ---- 鼓励语 ----
        quote = tk.Frame(outer, bg="#20293b")
        quote.pack(fill="x", pady=(12, 0))
        tk.Label(quote, text="💪 鼓励一下", bg="#20293b", fg=ACCENT,
                 font=(self.font_family, 10, "bold"), anchor="w").pack(fill="x", padx=12, pady=(8, 0))
        self.quote_label = tk.Label(
            quote, text=self.encouragement, bg="#20293b", fg=FG, wraplength=self.WIDTH - 80,
            justify="left", anchor="w", font=(self.font_family, 11))
        self.quote_label.pack(fill="x", padx=12, pady=(4, 10))

        # ---- 按钮 ----
        buttons = ttk.Frame(outer, style="Card.TFrame")
        buttons.pack(fill="x", pady=(14, 0))
        primary = "知道了" if all(i.is_cumulative for i in items) else "知道了，这就开始"
        ttk.Button(buttons, text=primary, style="Accent.TButton",
                   command=self.close).pack(side="right")
        if self.on_open_tasks is not None:
            ttk.Button(buttons, text="查看待办列表", style="Small.TButton",
                       command=self._open_tasks).pack(side="right", padx=(0, 8))
        ttk.Button(buttons, text="换一句鼓励", style="Small.TButton",
                   command=self._shuffle_quote).pack(side="left")

    def _build_row(self, parent: tk.Misc, index: int, item) -> None:
        """一行：序号 + 任务名 + 说明（打卡任务额外给一个「打卡」按钮）。"""
        row = tk.Frame(parent, bg=PANEL_ALT)
        row.pack(fill="x", padx=10, pady=(8, 0))
        colour = AMBER if item.is_late else ACCENT

        tk.Label(row, text="!" if item.is_late else "○", bg=PANEL_ALT, fg=colour,
                 font=(self.font_family, 12, "bold"), width=2).pack(side="left", anchor="n")

        # 打卡按钮先占右侧位置，文字区再填充剩余空间（顺序反了会被挤出去）
        button = None
        if item.is_cumulative and callable(self.on_check_in):
            button = ttk.Button(row, text="✓ 打卡", style="Accent.TButton",
                                command=lambda it=item: self._do_check_in(it))
            button.pack(side="right", padx=(8, 0), pady=(2, 0))

        text_box = tk.Frame(row, bg=PANEL_ALT)
        text_box.pack(side="left", fill="x", expand=True)
        prefix = f"{index}. " if self.batch.count > 1 else ""
        tk.Label(text_box, text=f"{prefix}{item.title}", bg=PANEL_ALT, fg=FG, anchor="w",
                 justify="left", wraplength=self.WIDTH - 220,
                 font=(self.font_family, 11, "bold")).pack(fill="x")
        detail = tk.Label(text_box, text=item.detail_text, bg=PANEL_ALT, fg=colour,
                          anchor="w", justify="left", wraplength=self.WIDTH - 220,
                          font=self._font_small())
        detail.pack(fill="x")
        if button is not None:
            self._checkin_rows[item.task.id] = {"button": button, "detail": detail, "item": item}

    def _do_check_in(self, item) -> None:
        """就地点一次「打卡」：成功就把按钮换成结果，不再骚扰。"""
        ok, message = False, "打卡失败"
        try:
            ok, message = self.on_check_in(item.task.id)
        except Exception:  # noqa: BLE001 - 界面异常不能让弹窗崩掉
            log.exception("打卡回调异常")
        row = self._checkin_rows.get(item.task.id)
        if row is None:
            return
        try:
            if ok:
                row["detail"].configure(text=message, fg=GREEN)
                row["button"].configure(text="✓ 已打卡", state="disabled")
            else:
                row["detail"].configure(text=message, fg=AMBER)
        except tk.TclError:
            log.debug("刷新打卡行失败", exc_info=True)

    def _shuffle_quote(self) -> None:
        self.encouragement = phrases.pick(exclude=self.encouragement)
        try:
            self.quote_label.configure(text=self.encouragement)
        except tk.TclError:
            log.debug("刷新鼓励语失败", exc_info=True)

    def _place(self) -> None:
        """放到屏幕右下角（含工作区域夹取，高 DPI 下也不会出屏）。"""
        place_dialog_bottom_right(self, self.WIDTH)

    def _bring_to_front(self) -> None:
        try:
            self.attributes("-topmost", True)
            self.lift()
        except tk.TclError:
            return

    def _open_tasks(self) -> None:
        if callable(self.on_open_tasks):
            try:
                self.on_open_tasks()
            except Exception:  # noqa: BLE001
                log.debug("打开待办列表失败", exc_info=True)
        self.close()

    def close(self) -> None:
        try:
            self.destroy()
        except tk.TclError:
            pass


def build_recurring_text(batch) -> str:
    """周期任务提醒的纯文本版（用于系统通知 / 托盘气泡）。"""
    if batch is None or not batch.items:
        return ""
    lines = [batch.headline()]
    lines.extend(f"· {line}" for line in batch.summary_lines()[:3])
    if batch.count > 3:
        lines.append(f"…… 还有 {batch.count - 3} 项")
    lines.append(phrases.pick())
    return "\n".join(lines)


# ================================================================ 开机统一提醒弹窗
class StartupCheckDialog(tk.Toplevel):
    """开机时**唯一**的提醒窗口：把"错过的周期任务"和"未完成的待办"合并展示。

    为什么需要它（避坑 #1）：如果周期任务的迟到补发和"开机待办弹窗"各自弹一个窗，
    开机瞬间会连弹两个，打扰翻倍且互相遮挡。这里合并成**一次**打扰：

    * 上半部分（如有时）：⏰ 错过的周期任务（迟到补发），橙色标记；
    * 下半部分（如有时）：☀ 未完成的待办任务（单次任务，含已超期）；
    * 两批各自最多显示若干条，其余折叠成一行提示。
    """

    MAX_RECURRING = 5
    MAX_TASKS = 6
    WIDTH = 600
    #: 弹出后多少秒没被关闭就自动关闭（用户要求：5 秒，并且窗口里要有明显的倒计时）
    AUTO_CLOSE_SECONDS = 5

    def __init__(self, master: tk.Misc, tasks: list[Task], batch=None,
                 font_family: str = "Microsoft YaHei UI", on_open_tasks=None) -> None:
        super().__init__(master)
        self.tasks = list(tasks)
        self.batch = batch
        self.font_family = font_family
        self.on_open_tasks = on_open_tasks
        self.encouragement = phrases.pick()
        self._closed = False
        self._countdown_id: str | None = None
        self._remaining = self.AUTO_CLOSE_SECONDS

        self.title("TimeGuard 开机提醒")
        self.configure(bg=PANEL)
        self.resizable(False, False)
        try:
            self.attributes("-topmost", True)
            self.attributes("-toolwindow", True)
        except tk.TclError:
            pass
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda _e: self.close())
        # 谁销毁了窗口都要停掉倒计时，否则会留下孤儿 after 回调，
        # Tk 会在窗口没了之后去调用一个已删除的命令，往 stderr 刷
        # "invalid command name ..._tick_countdown"。
        self.bind("<Destroy>", self._on_destroy)

        self._build()
        self._place()
        try:
            self.focus_force()
        except tk.TclError:
            pass
        if master is not None:
            try:
                master.after(900, self._bring_to_front)
            except tk.TclError:
                pass
        self._tick_countdown()

    # ------------------------------------------------------------------
    def _font_small(self) -> tuple:
        return (self.font_family, 9)

    @property
    def recurring_items(self) -> list:
        return list(self.batch.items) if self.batch is not None else []

    def _build(self) -> None:
        outer = ttk.Frame(self, style="Card.TFrame", padding=(20, 16))
        outer.pack(fill="both", expand=True)

        # ---- 标题行：左边标题，右边一个**大号**关闭按钮 ----
        # 为什么自己放一个：系统标题栏那个叉号又小、又紧贴屏幕边缘（弹窗是贴右下角放的），
        # 用户反馈"点了没反应"。自己放一个大按钮，位置可控、也看得见。
        header = ttk.Frame(outer, style="Card.TFrame")
        header.pack(fill="x")
        title_box = ttk.Frame(header, style="Card.TFrame")
        title_box.pack(side="left", fill="x", expand=True)
        self._build_heading(title_box)
        self.close_button = tk.Button(
            header, text="✕", command=self.close, bd=0, relief="flat",
            bg="#7a2b2b", fg="#ffe4e4", activebackground="#a33a3a", activeforeground="#ffffff",
            font=(self.font_family, 16, "bold"), width=3, height=1, cursor="hand2",
            padx=6, pady=2, takefocus=True,
        )
        self.close_button.pack(side="right", anchor="n", padx=(12, 0))
        self.close_button.bind("<Enter>", lambda _e: self.close_button.configure(bg="#a33a3a"))
        self.close_button.bind("<Leave>", lambda _e: self.close_button.configure(bg="#7a2b2b"))

        # ---- 上半：错过的周期任务 ----
        if self.recurring_items:
            self._build_section_title(outer, "⏰ 错过的周期任务（补发提醒）", AMBER)
            frame = tk.Frame(outer, bg=PANEL_ALT)
            frame.pack(fill="x")
            for index, item in enumerate(self.recurring_items[: self.MAX_RECURRING], 1):
                self._build_recurring_row(frame, index, item)
            hidden = len(self.recurring_items) - self.MAX_RECURRING
            if hidden > 0:
                tk.Label(frame, text=f"…… 还有 {hidden} 项", bg=PANEL_ALT, fg=SUB,
                         font=self._font_small(), anchor="w").pack(fill="x", padx=12, pady=(0, 8))

        # ---- 下半：未完成的待办 ----
        if self.tasks:
            gap = 14 if self.recurring_items else 0
            self._build_section_title(outer, "☀ 未完成的待办任务", ACCENT, pady=(gap, 0))
            frame = tk.Frame(outer, bg=PANEL_ALT)
            frame.pack(fill="x")
            for task in self.tasks[: self.MAX_TASKS]:
                self._build_task_row(frame, task)
            hidden = len(self.tasks) - self.MAX_TASKS
            if hidden > 0:
                tk.Label(frame, text=f"…… 还有 {hidden} 条，点击下方按钮查看全部",
                         bg=PANEL_ALT, fg=SUB, font=self._font_small(), anchor="w").pack(
                    fill="x", padx=12, pady=(0, 8))

        # ---- 鼓励语 ----
        quote = tk.Frame(outer, bg="#20293b")
        quote.pack(fill="x", pady=(12, 0))
        tk.Label(quote, text="💪 鼓励一下", bg="#20293b", fg=ACCENT,
                 font=(self.font_family, 10, "bold"), anchor="w").pack(fill="x", padx=12, pady=(8, 0))
        self.quote_label = tk.Label(
            quote, text=self.encouragement, bg="#20293b", fg=FG, wraplength=self.WIDTH - 80,
            justify="left", anchor="w", font=(self.font_family, 11))
        self.quote_label.pack(fill="x", padx=12, pady=(4, 10))

        # ---- 倒计时（显眼一点，让用户知道它会自己关）----
        self.countdown_var = tk.StringVar(value="")
        ttk.Label(outer, textvariable=self.countdown_var,
                  style="CardHint.TLabel", font=(self.font_family, 10, "bold"),
                  foreground=AMBER).pack(anchor="w", pady=(12, 0))

        # ---- 按钮 ----
        buttons = ttk.Frame(outer, style="Card.TFrame")
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="知道了，开始干活", style="Accent.TButton",
                   command=self.close).pack(side="right")
        if self.on_open_tasks is not None:
            ttk.Button(buttons, text="查看待办列表", style="Small.TButton",
                       command=self._open_tasks).pack(side="right", padx=(0, 8))
        ttk.Button(buttons, text="换一句鼓励", style="Small.TButton",
                   command=self._shuffle_quote).pack(side="left")

    def _build_heading(self, outer: tk.Misc) -> None:
        """标题：把两部分的数量都写清楚。"""
        missed = len(self.recurring_items)
        pending = len(self.tasks)
        parts = []
        if missed:
            parts.append(f"{missed} 项周期任务已错过时间")
        if pending:
            overdue = sum(1 for task in self.tasks if task.is_overdue())
            text = f"{pending} 条待办未完成"
            if overdue:
                text += f"（{overdue} 条已超期）"
            parts.append(text)
        heading = "、".join(parts) if parts else "今天没有需要处理的事情"

        ttk.Label(outer, text=f"☀ {heading}", style="CardTitle.TLabel",
                  font=(self.font_family, 14, "bold")).pack(anchor="w")
        ttk.Label(outer, text=f"开机检查　·　{datetime.now():%Y-%m-%d %H:%M}",
                  style="CardHint.TLabel").pack(anchor="w", pady=(2, 10))

    def _build_section_title(self, outer: tk.Misc, text: str, colour: str, pady=(0, 4)) -> None:
        tk.Label(outer, text=text, bg=PANEL, fg=colour, anchor="w",
                 font=(self.font_family, 11, "bold")).pack(fill="x", pady=pady)

    def _build_recurring_row(self, parent: tk.Misc, index: int, item) -> None:
        row = tk.Frame(parent, bg=PANEL_ALT)
        row.pack(fill="x", padx=10, pady=(8, 0))
        tk.Label(row, text="!", bg=PANEL_ALT, fg=AMBER, width=2,
                 font=(self.font_family, 12, "bold")).pack(side="left", anchor="n")
        box = tk.Frame(row, bg=PANEL_ALT)
        box.pack(side="left", fill="x", expand=True)
        tk.Label(box, text=f"{index}. {item.title}", bg=PANEL_ALT, fg=FG, anchor="w",
                 justify="left", wraplength=self.WIDTH - 170,
                 font=(self.font_family, 11, "bold")).pack(fill="x")
        tk.Label(box, text=item.detail_text, bg=PANEL_ALT, fg=AMBER, anchor="w",
                 justify="left", wraplength=self.WIDTH - 170,
                 font=self._font_small()).pack(fill="x")

    def _build_task_row(self, parent: tk.Misc, task: Task) -> None:
        now = datetime.now()
        overdue = task.is_overdue(now)
        row = tk.Frame(parent, bg=PANEL_ALT)
        row.pack(fill="x", padx=10, pady=(8, 0))
        tk.Label(row, text="!" if overdue else "○", bg=PANEL_ALT, fg=RED if overdue else ACCENT,
                 width=2, font=(self.font_family, 12, "bold")).pack(side="left", anchor="n")
        box = tk.Frame(row, bg=PANEL_ALT)
        box.pack(side="left", fill="x", expand=True)
        tk.Label(box, text=task.title, bg=PANEL_ALT, fg=FG, anchor="w", justify="left",
                 wraplength=self.WIDTH - 160,
                 font=(self.font_family, 11, "bold")).pack(fill="x")
        tk.Label(box, text=f"截止时间：{task.due_text()}　·　{task.countdown_text(now)}",
                 bg=PANEL_ALT, fg=RED if overdue else SUB, anchor="w",
                 font=self._font_small()).pack(fill="x")

    def _shuffle_quote(self) -> None:
        self.encouragement = phrases.pick(exclude=self.encouragement)
        try:
            self.quote_label.configure(text=self.encouragement)
        except tk.TclError:
            log.debug("刷新鼓励语失败", exc_info=True)

    def _place(self) -> None:
        """放到屏幕右下角（含工作区域夹取，高 DPI 下也不会出屏）。"""
        place_dialog_bottom_right(self, self.WIDTH)

    def _bring_to_front(self) -> None:
        try:
            self.attributes("-topmost", True)
            self.lift()
        except tk.TclError:
            return

    def _open_tasks(self) -> None:
        if callable(self.on_open_tasks):
            try:
                self.on_open_tasks()
            except Exception:  # noqa: BLE001
                log.debug("打开待办列表失败", exc_info=True)
        self.close()

    # ------------------------------------------------------------------ 倒计时
    def _tick_countdown(self) -> None:
        """每秒刷新「还剩几秒自动关闭」，到点自动关闭。

        用户要求：弹出后若干秒没被关闭就自己关掉，并且窗口里要有**明显**的倒计时
        （否则内容突然消失会让人以为是程序出问题了）。
        """
        if self._closed:
            return
        if self._remaining <= 0:
            try:
                self.countdown_var.set("正在自动关闭…")
            except tk.TclError:
                pass
            self.close(auto=True)
            return
        try:
            self.countdown_var.set(
                f"⏳ {self._remaining} 秒后自动关闭　（点「✕」或下面按钮可立即关闭）"
            )
        except tk.TclError:
            return
        self._remaining -= 1
        try:
            self._countdown_id = self.after(1000, self._tick_countdown)
        except tk.TclError:
            self._countdown_id = None

    def _on_destroy(self, event: tk.Event | None = None) -> None:
        """窗口被销毁时收尾（停掉倒计时）。``<Destroy>`` 也会被子控件触发，所以要看一眼。"""
        if event is not None and getattr(event, "widget", None) is not self:
            return
        self._closed = True
        if self._countdown_id is not None:
            try:
                self.after_cancel(self._countdown_id)
            except tk.TclError:
                pass
            self._countdown_id = None

    def close(self, auto: bool = False) -> None:
        """关闭弹窗（``auto=True`` 表示是倒计时到点自动关的）。"""
        if auto:
            log.info("开机提醒弹出后 %d 秒内没有被关闭，已自动关闭", self.AUTO_CLOSE_SECONDS)
        self._on_destroy()
        try:
            self.destroy()
        except tk.TclError:
            pass

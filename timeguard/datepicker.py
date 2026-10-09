"""日期 + 时间选择控件（精确到分钟）。

优先使用第三方库 ``tkcalendar``（日历式选择，体验最好）；
如果环境里没装（或安装失败），自动降级为「年 / 月 / 日」下拉框版本，
保证程序在任何环境下都能用。

对外只暴露一个类：:class:`DateTimePicker`，用法与普通控件一致::

    picker = DateTimePicker(parent, font_family="Microsoft YaHei UI")
    picker.set(datetime.now() + timedelta(hours=2))
    value = picker.get()          # -> datetime | None（None 表示“无期限”）
    picker.set_none()             # 清空为“无期限”
"""

from __future__ import annotations

import calendar
import logging
import tkinter as tk
from datetime import date, datetime, timedelta
from tkinter import ttk

log = logging.getLogger(__name__)

#: 尝试导入 tkcalendar（可选依赖）
try:  # pragma: no cover - 取决于环境
    from tkcalendar import DateEntry  # type: ignore

    HAS_TKCALENDAR = True
except Exception:  # noqa: BLE001
    DateEntry = None  # type: ignore[assignment]
    HAS_TKCALENDAR = False

#: 快捷选项：文案 -> 距现在的时长
QUICK_CHOICES: list[tuple[str, timedelta | None]] = [
    ("1 小时后", timedelta(hours=1)),
    ("3 小时后", timedelta(hours=3)),
    ("今晚 20:00", None),
    ("明天此时", timedelta(days=1)),
    ("明早 09:00", None),
    ("本周末", None),
]


def quick_datetime(label: str, now: datetime | None = None) -> datetime:
    """把快捷选项文案换算成具体时间。"""
    now = now or datetime.now()
    base = now.replace(second=0, microsecond=0)
    for text, delta in QUICK_CHOICES:
        if text != label:
            continue
        if delta is not None:
            return base + delta
        if label == "今晚 20:00":
            target = base.replace(hour=20, minute=0)
            return target if target > base else target + timedelta(days=1)
        if label == "明早 09:00":
            return (base + timedelta(days=1)).replace(hour=9, minute=0)
        if label == "本周末":
            days_to_sunday = (6 - base.weekday()) % 7 or 7
            return (base + timedelta(days=days_to_sunday)).replace(hour=20, minute=0)
    return base + timedelta(hours=1)


#: 深色主题下的字段底色（与 ui.py 的 PANEL_ALT 一致）
FIELD_BG = "#232b3d"
FIELD_FG = "#e6eaf2"
FIELD_BG_DISABLED = "#1b2130"
FIELD_FG_DISABLED = "#8b95a8"
ACCENT_SELECT = "#4f8cff"

#: 专门给日历控件用的 ttk 样式名（必须是独立样式，理由见 _style_calendar_entry）
CALENDAR_STYLE = "Cal.TEntry"
#: 时/分下拉框专用样式（同样避免 readonly 状态下的浅底白字）
TIME_STYLE = "Cal.TCombobox"


def _style_time_combobox(widget: tk.Misc) -> None:
    """给“时 / 分”下拉框套深色样式（readonly 状态下也保持深底浅字）。"""
    try:
        style = ttk.Style(widget)
        is_clam = style.theme_use() == "clam"
        style.configure(
            TIME_STYLE,
            foreground=FIELD_FG,
            fieldbackground=FIELD_BG,
            background=FIELD_BG,
            arrowcolor=FIELD_FG,
            bordercolor="#2b3550" if is_clam else FIELD_BG,
            lightcolor="#2b3550" if is_clam else FIELD_BG,
            darkcolor="#2b3550" if is_clam else FIELD_BG,
            selectbackground=ACCENT_SELECT,
            selectforeground="#ffffff",
            padding=3,
        )
        for state, spec in (
            (("readonly",), {"fieldbackground": FIELD_BG, "foreground": FIELD_FG, "background": FIELD_BG}),
            (("readonly", "focus"), {"fieldbackground": FIELD_BG, "foreground": FIELD_FG,
                                     "selectbackground": ACCENT_SELECT, "selectforeground": "#ffffff"}),
            (("disabled",), {"fieldbackground": FIELD_BG_DISABLED, "foreground": FIELD_FG_DISABLED,
                             "arrowcolor": FIELD_FG_DISABLED}),
            (("active",), {"background": "#2b3550" if is_clam else FIELD_BG}),
        ):
            try:
                style.map(TIME_STYLE, **{key: [(state, value)] for key, value in spec.items()})
            except tk.TclError:
                log.debug("映射时间下拉框样式失败: %s", state, exc_info=True)
    except tk.TclError:
        log.debug("配置时间下拉框样式失败", exc_info=True)


def _style_calendar_entry(widget: tk.Misc) -> None:
    """给 tkcalendar 的 ``DateEntry`` 套上深色样式。

    为什么必须单独处理：``DateEntry._setup_style()`` 会把 **ttk.Combobox 的样式配置与
    状态映射**整套复制到 ``DateEntry`` 上，而 clam 主题的 Combobox 在 ``readonly``
    状态下的 fieldbackground 被硬编码成浅色 ``#dcdad5`` —— 结果就是“浅底 + 白字”，
    日期几乎看不见（用户反馈的问题）。

    这里定义一个独立的 ``Cal.TEntry`` 样式，显式指定各状态的颜色并重新映射，
    既修好可读性，也不会影响界面里其他输入框 / 下拉框。
    """
    try:
        style = ttk.Style(widget)
        is_clam = style.theme_use() == "clam"
        # readonly 是 DateEntry 的常态；disabled 用于“无期限”状态
        style.configure(
            CALENDAR_STYLE,
            foreground=FIELD_FG,
            fieldbackground=FIELD_BG,
            background=FIELD_BG,
            insertcolor=FIELD_FG,
            bordercolor="#2b3550" if is_clam else FIELD_BG,
            lightcolor="#2b3550" if is_clam else FIELD_BG,
            darkcolor="#2b3550" if is_clam else FIELD_BG,
            arrowcolor=FIELD_FG,
            padding=4,
        )
        for state, spec in (
            (("readonly",), {"fieldbackground": FIELD_BG, "foreground": FIELD_FG,
                             "background": FIELD_BG, "arrowcolor": FIELD_FG}),
            (("readonly", "focus"), {"fieldbackground": FIELD_BG, "foreground": FIELD_FG,
                                     "background": FIELD_BG}),
            (("disabled",), {"fieldbackground": FIELD_BG_DISABLED, "foreground": FIELD_FG_DISABLED,
                             "background": FIELD_BG_DISABLED, "arrowcolor": FIELD_FG_DISABLED}),
            (("active",), {"background": "#2b3550" if is_clam else FIELD_BG}),
            (("pressed",), {"background": "#2b3550" if is_clam else FIELD_BG}),
        ):
            try:
                style.map(CALENDAR_STYLE, **{key: [(state, value)] for key, value in spec.items()})
            except tk.TclError:
                log.debug("映射日历样式状态失败: %s", state, exc_info=True)
    except tk.TclError:
        log.debug("配置日历控件样式失败", exc_info=True)


class DateTimePicker(ttk.Frame):
    """日期 + 时间（时/分）选择控件，可清空为“无期限”。"""

    def __init__(
        self,
        master: tk.Misc,
        font_family: str = "Microsoft YaHei UI",
        font_body: tuple | None = None,
        bg: str = "#1b2130",
        style_prefix: str = "Card",
        **kwargs,
    ) -> None:
        super().__init__(master, style=f"{style_prefix}.TFrame", **kwargs)
        self.font_family = font_family
        self.font_body = font_body or (font_family, 10)
        self.bg = bg
        self.style_prefix = style_prefix
        self._value: datetime | None = datetime.now().replace(second=0, microsecond=0) + timedelta(hours=1)
        self._suppress = False          # 防止内部联动回调互相触发
        self._none_mode = False         # 是否处于“无期限”状态
        self.date_entry = None
        self._fallback_boxes = None

        self._build()

    # ------------------------------------------------------------------ 构建
    def _build(self) -> None:
        today = datetime.now()

        # ---- 日期部分 ----
        if HAS_TKCALENDAR:
            # 先备好深色样式，再创建控件（tkcalendar 内部会复制 TCombobox 的样式映射）
            _style_calendar_entry(self)
            self.date_entry = DateEntry(
                self,
                width=13,
                date_pattern="yyyy-mm-dd",     # 统一格式，避免本地化偏差
                firstweekday="monday",
                showweeknumbers=False,
                font=self.font_body,
                background="#2b3550",
                foreground=FIELD_FG,           # 下拉日历的日期文字
                normalbackground="#232b3d",
                weekendbackground="#232b3d",
                othermonthbackground="#1b2130",
                othermonthforeground="#6b7488",
                headersbackground="#2b3550",
                headersforeground="#e6eaf2",
                selectbackground="#4f8cff",
                selectforeground="#ffffff",
                bordercolor="#2b3550",
                darken_on_click=False,         # 下拉日历也跟随深色主题
                locale="zh_CN" if _has_chinese_locale() else "en_US",
                style=CALENDAR_STYLE,          # 关键：用独立样式避免浅底白字
            )
            self.date_entry.delete(0, "end")
            self.date_entry.insert(0, today.strftime("%Y-%m-%d"))
            # tkcalendar 对 width 的处理不总是生效，这里再显式撑一次，保证
            # “2026-10-06” 完整可见（否则窄成一个黑框，很影响观感）
            try:
                self.date_entry.configure(width=12)
                self.date_entry.update_idletasks()
            except tk.TclError:
                log.debug("设置日历控件宽度失败", exc_info=True)
            # tkcalendar 内部会在 <Map> 等事件里重新套用样式映射，这里再兜一次底
            self.date_entry.bind("<Map>", lambda _e: _style_calendar_entry(self), add="+")
            # 从日历里选完日期后，把时间部分同步过去（tkcalendar 会清空 StringVar）
            self.date_entry.bind("<<DateEntrySelected>>", self._on_calendar_selected)
            self.date_entry.pack(side="left", padx=(0, 6))
        else:
            self._build_fallback_date(today)

        # ---- 时间部分：时 : 分 ----
        _style_time_combobox(self)
        time_box = ttk.Frame(self, style=f"{self.style_prefix}.TFrame")
        time_box.pack(side="left")
        self.time_box = time_box          # 供"只要日期"的子类隐藏（见 DatePicker）
        self.hour_var = tk.StringVar(value=f"{self._value.hour:02d}")
        self.minute_var = tk.StringVar(value=f"{self._value.minute:02d}")
        self.hour_box = ttk.Combobox(
            time_box, width=4, textvariable=self.hour_var, state="readonly",
            values=[f"{h:02d}" for h in range(24)], font=self.font_body, style=TIME_STYLE,
        )
        self.hour_box.pack(side="left")
        ttk.Label(time_box, text=":", style=f"{self.style_prefix}.TLabel").pack(side="left")
        self.minute_box = ttk.Combobox(
            time_box, width=4, textvariable=self.minute_var, state="readonly",
            values=[f"{m:02d}" for m in (0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55)],
            font=self.font_body, style=TIME_STYLE,
        )
        self.minute_box.pack(side="left")
        # 若当前分钟不在 5 的倍数里，补进去，避免显示不一致
        for box, var in ((self.hour_box, self.hour_var), (self.minute_box, self.minute_var)):
            box.bind("<<ComboboxSelected>>", self._on_time_changed)
            if var.get() not in box.cget("values"):
                box.configure(values=list(box.cget("values")) + [var.get()])

        # ---- 清空按钮 ----
        self.clear_button = ttk.Button(
            self, text="无期限", width=8, style="Small.TButton", command=self.set_none,
        )
        self.clear_button.pack(side="left", padx=(8, 0))

    def _build_fallback_date(self, today: datetime) -> None:
        """没有 tkcalendar 时的降级方案：年 / 月 / 日 三个下拉框。"""
        _style_time_combobox(self)      # 复用同一套深色下拉框样式
        fallback = ttk.Frame(self, style=f"{self.style_prefix}.TFrame")
        fallback.pack(side="left", padx=(0, 6))
        self.year_var = tk.StringVar(value=str(today.year))
        self.month_var = tk.StringVar(value=f"{today.month:02d}")
        self.day_var = tk.StringVar(value=f"{today.day:02d}")

        self.year_box = ttk.Combobox(
            fallback, width=5, textvariable=self.year_var, state="readonly",
            values=[str(y) for y in range(today.year, today.year + 6)], font=self.font_body,
            style=TIME_STYLE,
        )
        self.year_box.pack(side="left")
        ttk.Label(fallback, text="年", style=f"{self.style_prefix}.TLabel").pack(side="left")
        self.month_box = ttk.Combobox(
            fallback, width=3, textvariable=self.month_var, state="readonly",
            values=[f"{m:02d}" for m in range(1, 13)], font=self.font_body, style=TIME_STYLE,
        )
        self.month_box.pack(side="left")
        ttk.Label(fallback, text="月", style=f"{self.style_prefix}.TLabel").pack(side="left")
        self.day_box = ttk.Combobox(
            fallback, width=3, textvariable=self.day_var, state="readonly",
            values=[f"{d:02d}" for d in range(1, 32)], font=self.font_body, style=TIME_STYLE,
        )
        self.day_box.pack(side="left")
        ttk.Label(fallback, text="日", style=f"{self.style_prefix}.TLabel").pack(side="left")

        for box in (self.year_box, self.month_box, self.day_box):
            box.bind("<<ComboboxSelected>>", self._on_fallback_changed)
        self._fallback_boxes = fallback

    # ------------------------------------------------------------------ 读数/写数
    def get(self) -> datetime | None:
        """读取当前选择；返回 None 表示“无期限”。"""
        if self._value is None:
            return None
        try:
            hour = int(self.hour_var.get())
            minute = int(self.minute_var.get())
        except (TypeError, ValueError):
            hour, minute = 23, 59
        base = self._read_date()
        if base is None:
            return None
        return base.replace(hour=max(0, min(23, hour)), minute=max(0, min(59, minute)), second=0, microsecond=0)

    def set(self, value: datetime | None) -> None:
        """设置当前值；传 None 等于 :meth:`set_none`。"""
        if value is None:
            self.set_none()
            return
        self._value = value.replace(second=0, microsecond=0)
        self._suppress = True
        try:
            self._write_date(self._value)
            self.hour_var.set(f"{self._value.hour:02d}")
            self.minute_var.set(f"{self._value.minute:02d}")
            self._ensure_time_options()
        finally:
            self._suppress = False

    def set_none(self) -> None:
        """清空为“无期限”（日期与时间控件一起置灰）。"""
        self._value = None
        self._none_mode = True
        self._suppress = True
        try:
            # 先变灰再改文本：否则 _write_date 会把 state 又设回 normal（曾出现的状态 bug）
            if HAS_TKCALENDAR and self.date_entry is not None:
                self.date_entry.configure(state="disabled")
            elif self._fallback_boxes is not None:
                for box in (self.year_box, self.month_box, self.day_box):
                    box.configure(state="disabled")
            self.hour_box.configure(state="disabled")
            self.minute_box.configure(state="disabled")
            if HAS_TKCALENDAR and self.date_entry is not None:
                self.date_entry.delete(0, "end")
                self.date_entry.insert(0, "无期限")
                self.date_entry.configure(state="disabled")
            self.clear_button.configure(text="设定期限", command=self._restore_default)
        finally:
            self._suppress = False

    def _restore_default(self) -> None:
        """从“无期限”恢复成默认值（1 小时后），并还原控件状态。"""
        self._none_mode = False
        self.hour_box.configure(state="readonly")
        self.minute_box.configure(state="readonly")
        if HAS_TKCALENDAR and self.date_entry is not None:
            self.date_entry.configure(state="normal")
        elif self._fallback_boxes is not None:
            for box in (self.year_box, self.month_box, self.day_box):
                box.configure(state="readonly")
        self.clear_button.configure(text="无期限")
        self.clear_button.configure(command=self.set_none)
        self.set(datetime.now().replace(second=0, microsecond=0) + timedelta(hours=1))

    @property
    def is_none(self) -> bool:
        """是否处于“无期限”状态。"""
        return self._value is None

    # ------------------------------------------------------------------ 内部
    def _ensure_time_options(self) -> None:
        """保证当前时分在下拉列表中（否则显示会空白）。"""
        for box, var in ((self.hour_box, self.hour_var), (self.minute_box, self.minute_var)):
            values = list(box.cget("values"))
            if var.get() not in values:
                values.append(var.get())
                box.configure(values=values)

    def _read_date(self) -> datetime | None:
        """读取日期部分（降级模式下会做合法性裁剪，例如 2 月 30 日）。"""
        if HAS_TKCALENDAR and self.date_entry is not None:
            text = self.date_entry.get().strip()
            if not text or text == "无期限":
                return None
            for fmt in ("%Y-%m-%d", "%m/%d/%y", "%m/%d/%Y"):
                try:
                    return datetime.strptime(text, fmt)
                except ValueError:
                    continue
            return datetime.now()
        try:
            year = int(self.year_var.get())
            month = int(self.month_var.get())
            day = int(self.day_var.get())
        except (TypeError, ValueError, AttributeError):
            return datetime.now()
        day = min(day, calendar.monthrange(year, month)[1])
        return datetime(year, month, day)

    def _write_date(self, value: datetime) -> None:
        """把日期写入对应控件（“无期限”状态下不改变置灰状态）。"""
        if HAS_TKCALENDAR and self.date_entry is not None:
            self.date_entry.configure(state="normal")
            self.date_entry.delete(0, "end")
            self.date_entry.insert(0, value.strftime("%Y-%m-%d"))
        elif self._fallback_boxes is not None:
            for box in (self.year_box, self.month_box, self.day_box):
                box.configure(state="readonly")
            self.year_var.set(str(value.year))
            self.month_var.set(f"{value.month:02d}")
            self.day_var.set(f"{value.day:02d}")

    def _on_calendar_selected(self, _event: tk.Event) -> None:
        """日历里选了日期：同步内部值（时分沿用当前下拉框的值）。"""
        if self._suppress:
            return
        text = (self.date_entry.get() or "").strip() if self.date_entry is not None else ""
        try:
            picked = datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            return
        self._suppress = True
        try:
            if not self.hour_var.get():
                self.hour_var.set(f"{(self._value or datetime.now()).hour:02d}")
            if not self.minute_var.get():
                self.minute_var.set(f"{(self._value or datetime.now()).minute:02d}")
            self._ensure_time_options()
            self.hour_box.configure(state="readonly")
            self.minute_box.configure(state="readonly")
            self.clear_button.configure(text="无期限", command=self.set_none)
            self._value = picked.replace(
                hour=int(self.hour_var.get()), minute=int(self.minute_var.get()),
                second=0, microsecond=0,
            )
        finally:
            self._suppress = False

    def _on_fallback_changed(self, _event: tk.Event) -> None:
        """降级模式下日期变化。"""
        if self._suppress:
            return
        value = self.get()
        if value is not None:
            self._value = value

    def _on_time_changed(self, _event: tk.Event) -> None:
        """时间变化：保持内部值同步。"""
        if self._suppress:
            return
        value = self.get()
        if value is not None:
            self._value = value


def has_chinese_locale() -> bool:
    """tkcalendar 是否能用中文日历（它依赖 babel 提供 locale）。"""
    try:  # pragma: no cover - 依赖环境
        import babel  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


#: 兼容旧名字
_has_chinese_locale = has_chinese_locale


def backend_name() -> str:
    """当前使用的日期选择实现（界面提示用）。"""
    return "tkcalendar 日历" if HAS_TKCALENDAR else "内置下拉选择器（未安装 tkcalendar）"


class DatePicker(DateTimePicker):
    """**只选日期**的变体（累计打卡任务的"最终截止日期"用它）。

    直接复用已经验证过的 :class:`DateTimePicker`（含 tkcalendar 降级方案），
    只把时/分下拉与"无期限"按钮藏起来 —— 截止日期精确到天，
    给它显示"时:分"会让人以为要卡点打卡，反而增加困惑。

    对外只暴露 :meth:`get_date` / :meth:`set_date`（值统一取当天 00:00）。
    """

    def __init__(self, master: tk.Misc, **kwargs) -> None:
        super().__init__(master, **kwargs)
        for widget in (getattr(self, "time_box", None), getattr(self, "clear_button", None)):
            if widget is not None:
                try:
                    widget.pack_forget()
                except tk.TclError:      # pragma: no cover - 控件已销毁
                    pass

    def get_date(self) -> date | None:
        """当前选择的日期（控件异常时返回 None）。"""
        value = self.get()
        return value.date() if value is not None else None

    def set(self, value: datetime | date | None) -> None:      # type: ignore[override]
        """设置日期（时间部分固定为 00:00）。

        覆盖父类是为了容忍 ``date``：父类的 ``set`` 会调用 ``value.replace(second=0)``，
        而 ``date`` 没有 ``second`` 参数，直接传进来会 TypeError（第一次接线时就踩到了）。
        """
        if value is None:
            self.set_none()
            return
        if isinstance(value, datetime):
            value = value.date()
        super().set(datetime(value.year, value.month, value.day))

    def set_date(self, value: date | datetime | None) -> None:
        """设置日期（``set`` 的语义化别名）。"""
        self.set(value)

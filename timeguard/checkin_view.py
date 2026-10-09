"""累计打卡任务的三个界面组件：进度条 / 打卡热力图 / 任务详情面板。

这三个组件解决的是同一个场景里的三件事：

1. :class:`ProgressBar` —— **条内要居中写文字**（``已打卡 45/60 次（75%）``）。
   为什么自绘而不用 ``ttk.Progressbar``：它的 ``text`` 只在部分主题、且只在确定模式下
   贴着左边显示，深色 clam 主题里基本看不见，也没法让填充色随进度变化（达标要变绿）。
   自绘 ``tk.Canvas`` 想怎么画就怎么画，还不引入任何第三方依赖。
2. :class:`CheckinHeatmap` —— GitHub 贡献图那种"绿色格子"热力图，回答的是
   **"我到底哪几天打了卡、有没有断"**。光给一个"已打卡 45 次"看不出断没断，
   而热力图一眼就能看出来。同样必须自绘：126 个格子的位置 / 分档配色 /
   悬停提示、以及"未来的格子干脆不画"，都不是现成控件能表达的。
3. :class:`CumulativeDetail` —— 把上面两个组件 + 文案 + 按钮组成"累计打卡任务详情"面板，
   放在任务列表下方。

职责边界（很重要，改代码前先看一眼）
------------------------------------
* 本模块**只做展示与转发**：进度数字由调用方算好的 :class:`~timeguard.recurrence.CumulativeStatus`
  给进来；打卡历史由调用方通过 :meth:`CumulativeDetail.set_checked_days` 喂进来
  （``Task`` 本身不带历史）；打卡 / 撤销动作通过 ``on_check_in`` / ``on_undo``
  回调交回调用方。所以本模块**不 import 数据库**，可以脱离 ``UsageStore`` 单独跑。
* **状态文案一律用** :meth:`CumulativeStatus.status_text` /
  :meth:`CumulativeStatus.countdown_text`，本模块**不自己拼状态词** ——
  用户的核心诉求是"截止日之前绝不能出现『逾期』字样"，两处各拼一套文案
  迟早会出现"列表说进行中、详情说逾期"的自相矛盾。
* 所有对外方法在窗口已销毁后**必须吞掉** ``tk.TclError``（项目里其它组件的既定做法，
  只记 ``log.debug(..., exc_info=True)``）：主界面会在切页 / 关窗时继续调这些方法。
"""

from __future__ import annotations

import logging
import tkinter as tk
from datetime import date, datetime, timedelta
from tkinter import ttk

# 颜色统一从 tasks.py 取，不在这里再抄一份：抄一份就会慢慢漂移成两套深色主题
from .tasks import ACCENT, AMBER, FG, GREEN, PANEL, PANEL_ALT, RED, SUB

log = logging.getLogger(__name__)

#: 本模块自用的 ttk 样式名，带 ``Checkin`` 前缀，避免动到主界面的 Card.* 样式
STYLE_CARD = "CheckinCard.TFrame"


def _ensure_styles(widget: tk.Misc) -> None:
    """配置本模块自用的 ttk 样式（幂等，可重复调用）。

    为什么自带样式而不是直接用主界面的 ``Card.TFrame``：本模块要能**脱离主窗口
    单独跑起来**（截图脚本、界面冒烟测试都是裸 Tk root），借用别人的样式名在裸
    root 下会退化成系统默认的浅色控件 —— 深色面板里冒出一块浅灰。颜色取值与
    ``ui.py`` 一致，嵌进主界面时看不出差别。

    注意：**按钮没有走 ttk**（用的是 tk.Button，见 :meth:`CumulativeDetail._build`）。
    实测踩坑：Windows 默认的 vista 主题下，ttk.Button 忽略 ``background`` /
    ``foreground`` 这类颜色选项（按钮由系统画），于是浅色的 ``foreground`` 被画在
    浅灰底上，字直接看不见；而强制 ``theme_use("clam")`` 是改全局主题，
    不该由一个组件偷偷决定。tk.Button 老老实实认颜色，任何主题下都对。
    """
    try:
        ttk.Style(widget).configure(STYLE_CARD, background=PANEL)
    except tk.TclError:
        log.debug("配置累计打卡面板样式失败（窗口可能已销毁）", exc_info=True)


# ================================================================ 日期工具
def _as_date(value) -> date | None:
    """把 ``date`` / ``datetime`` / ``"2026-10-09"`` 统一成 ``date``，认不出来返回 ``None``。

    为什么要容忍字符串：``UsageStore`` 那边查出来的打卡日期有可能直接是 ISO 字符串，
    这里多写几行，调用方就不用为了喂数据再手动转换一遍（转错了还会静默丢数据）。
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):            # datetime 是 date 的子类，必须先判
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value).strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _coerce_days(values) -> set[date]:
    """把任意可迭代的日期集合规范化成 ``set[date]``（认不出来的项直接丢掉）。"""
    days: set[date] = set()
    if values is None:
        return days
    if isinstance(values, (str, date, datetime)):    # 只给了一天，也当集合处理
        day = _as_date(values)
        return {day} if day is not None else days
    try:
        items = list(values)
    except TypeError:
        log.debug("打卡日期不是可迭代对象，按空集合处理", exc_info=True)
        return days
    for item in items:
        day = _as_date(item)
        if day is not None:
            days.add(day)
    return days


# ================================================================ 进度条
class ProgressBar(tk.Canvas):
    """自绘的深色进度条：条内居中显示文字，达标（比例 >= 1）后填充变绿。

    ``ttk.Progressbar`` 做不到"条内居中文字 + 达标变色"（见模块 docstring），
    所以这里用 Canvas 画三层：底色轨道 → 填充块 → 居中文字。
    """

    def __init__(self, master: tk.Misc, width: int = 420, height: int = 22, bg: str = PANEL,
                 font_family: str = "Microsoft YaHei UI", **kwargs) -> None:
        super().__init__(master, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0, **kwargs)
        self._width = max(1, int(width))
        self._height = max(1, int(height))
        self._font = (font_family, 9)
        self._value = 0.0
        self._total = 0.0
        self._text = ""
        self._fill_color = ACCENT
        self.redraw()

    # ------------------------------------------------------------------ 对外
    def set_progress(self, value: float, total: float, text: str = "") -> None:
        """设置进度：``value/total`` 决定填充比例，``text`` 为空时自动写 ``45/60（75%）``。

        ``total <= 0`` 按 0 处理（数据库里出现 0 不该让界面除零崩掉）。
        自动文案带上百分比（而不是只有 ``45/60``）：光看条长估不准还差多少，
        而"75%"是可以直接跟目标对齐的数字。
        """
        try:
            self._value = float(value)
            self._total = float(total)
        except (TypeError, ValueError):
            self._value = self._total = 0.0
        self._text = str(text) if text else (
            f"{int(self._value)}/{int(self._total)}（{int(round(self._ratio() * 100))}%）")
        self.redraw()

    def set_colors(self, fill: str, **_) -> None:
        """换掉"进行中"的填充色。

        达标后仍然强制用绿色：绿色在这里表达的是"已达成目标"这个语义，
        不该被调用方的配色覆盖掉（否则"完成"和"进行到一半"看起来会一样）。
        """
        self._fill_color = fill or ACCENT
        self.redraw()

    @property
    def text(self) -> str:
        """当前条内文字（供外部 / 测试读取）。"""
        return self._text

    @property
    def ratio(self) -> float:
        """当前填充比例（0.0 ~ 1.0）。"""
        return self._ratio()

    # ------------------------------------------------------------------ 绘制
    def _ratio(self) -> float:
        if self._total <= 0:
            return 0.0
        return max(0.0, min(1.0, self._value / self._total))

    def _rounded(self, x0: float, y0: float, x1: float, y1: float, fill: str) -> None:
        """圆角矩形 = 两个半圆 + 中间矩形。

        ``height`` 很小时（低于 8px）半圆会把短条画成"两头尖的橄榄"，
        反而看不清进度，这种矮条就直接用方角矩形（需求里也允许简单矩形）。
        """
        height = y1 - y0
        if height >= 8 and (x1 - x0) >= height:
            radius = height / 2
            self.create_oval(x0, y0, x0 + height, y1, fill=fill, outline=fill)
            self.create_oval(x1 - height, y0, x1, y1, fill=fill, outline=fill)
            self.create_rectangle(x0 + radius, y0, x1 - radius, y1, fill=fill, outline=fill)
        else:
            self.create_rectangle(x0, y0, x1, y1, fill=fill, outline=fill)

    def redraw(self) -> None:
        """整条重画（先 ``delete("all")``，所以可以安全地反复调用）。"""
        try:
            self.delete("all")
            width, height = self._width, self._height
            if width <= 1 or height <= 1:
                return
            ratio = self._ratio()
            self._rounded(0, 0, width, height, PANEL_ALT)          # 底色轨道
            if ratio > 0:
                color = GREEN if ratio >= 1 else self._fill_color
                # 比例极小时也留一小段可见的填充（最短 = 一个圆角头），否则像是没画
                self._rounded(0, 0, max(float(height), width * ratio), height, color)
            self.create_text(width / 2, height / 2, text=self._text, fill=FG,
                             font=self._font, anchor="center")
        except tk.TclError:
            log.debug("重画进度条失败（窗口可能已销毁）", exc_info=True)


# ================================================================ 打卡热力图
class CheckinHeatmap(tk.Frame):
    """打卡热力图：每列一周、周一到周日共 7 行，画最近 ``weeks`` 周。

    布局约定（和 GitHub 贡献图一样）：**最后一列一定是本周**，所以"今天"永远落在
    右下角，一眼就能看出"这周打了几次、前面哪几周断了"。

    两个刻意的取舍：

    * 今天之后的格子**完全不画**（不是画成灰底）—— 灰底会被读成"未来漏打卡了"，
      用户对"还没到的日子算我欠账"很反感。
    * 悬停提示画在画布**顶部那一行**里（默认显示日期范围），不弹 ``Toplevel`` Tooltip：
      详情面板整体才 ~200px 高，弹出式提示容易越界、被主窗口遮住，还可能因为
      ``after`` / 焦点问题抛异常；就地写一行字最省事也最不容易出错。
    """

    #: 左侧"一/三/五"文字列宽
    LABEL_W = 22
    #: 右侧留白：最后一列的月份标签是"从列首往右写"的，不留这点宽度会被画布裁掉
    RIGHT_PAD = 14
    #: 顶部那一行：默认显示日期范围，鼠标悬停时改写成那一天的情况
    HEADER_H = 16
    #: 底部月份刻度行高
    MONTH_H = 13
    #: 打卡稠密度分 3 档（浅 → 深，第三档就是主色绿），按"当周打卡次数"取档
    LEVEL_COLORS = ("#1f6f52", "#2a9e73", GREEN)

    def __init__(self, master: tk.Misc, font_family: str = "Microsoft YaHei UI",
                 weeks: int = 18, cell: int = 12, gap: int = 3, **kwargs) -> None:
        super().__init__(master, bg=PANEL, **kwargs)
        self.font_family = font_family
        self.font_small = (font_family, 8)
        self.weeks = max(1, int(weeks))
        self.cell = max(4, int(cell))
        self.gap = max(0, int(gap))
        self._days: set[date] = set()
        self._today = date.today()
        self._deadline: date | None = None
        self._header: int | None = None
        self._range_text = ""
        self.canvas = tk.Canvas(self, width=self.total_width(), height=self.total_height(),
                                bg=PANEL, highlightthickness=0, bd=0)
        self.canvas.pack(anchor="w")
        self.redraw()

    # ------------------------------------------------------------------ 尺寸
    def total_width(self) -> int:
        """画布宽度（左边标签列 + weeks 列格子 + 右边留白）。"""
        return self.LABEL_W + self.weeks * self.cell + (self.weeks - 1) * self.gap + self.RIGHT_PAD

    def total_height(self) -> int:
        """画布高度（顶部提示行 + 7 行格子 + 底部月份行）。"""
        return self.HEADER_H + 7 * self.cell + 6 * self.gap + self.MONTH_H

    def _col_x(self, column: int) -> float:
        """第 ``column`` 列（0 = 最旧的一周）的左边界 x。"""
        return self.LABEL_W + column * (self.cell + self.gap)

    def _cell_y(self, row: int) -> float:
        """第 ``row`` 行（0 = 周一）的上边界 y。"""
        return self.HEADER_H + row * (self.cell + self.gap)

    # ------------------------------------------------------------------ 对外
    def set_data(self, checked_days, today: date | None = None, deadline: date | None = None) -> None:
        """喂数据并重画（可反复调用，会先清空再画）。

        :param checked_days: 已打卡的自然日（``date`` 可迭代集合，也容忍 ``datetime`` / ISO 字符串）
        :param today: 把哪天当成"今天"（默认取真实今天；测试里可以固定住）
        :param deadline: 最终截止日；**已经过去且当天没打卡**时会用红色描边标出来
        """
        self._days = _coerce_days(checked_days)
        self._today = _as_date(today) or date.today()
        self._deadline = _as_date(deadline)
        self.redraw()

    def redraw(self) -> None:
        """整块重画（先清空画布，因此 set_data 重入安全）。"""
        try:
            canvas = self.canvas
            canvas.delete("all")
            self._header = None
            cell = self.cell
            # 本列周一的日期：最后一列必须是"本周"，往前推 weeks-1 周
            first_monday = self._today - timedelta(days=self._today.isoweekday() - 1,
                                                   weeks=self.weeks - 1)
            self._range_text = (f"近 {self.weeks} 周　{first_monday:%m-%d} ~ {self._today:%m-%d}")
            self._header = canvas.create_text(self.LABEL_W, 2, anchor="nw", text=self._range_text,
                                              fill=SUB, font=self.font_small)

            # 左侧只标 一/三/五：7 行全标太挤，这三行已经够定位了
            for row, text in ((0, "一"), (2, "三"), (4, "五")):
                canvas.create_text(self.LABEL_W - 6, self._cell_y(row) + cell / 2, text=text,
                                   fill=SUB, font=self.font_small, anchor="e")

            # 当周打卡次数 → 颜色档位（1 次浅、2~3 次中、4 次以上用主色绿）
            weekly: dict[date, int] = {}
            for day in self._days:
                if first_monday <= day <= self._today:
                    monday = day - timedelta(days=day.isoweekday() - 1)
                    weekly[monday] = weekly.get(monday, 0) + 1

            self._draw_months(canvas, first_monday)
            for column in range(self.weeks):
                monday = first_monday + timedelta(weeks=column)
                for row in range(7):
                    day = monday + timedelta(days=row)
                    if day > self._today:
                        continue        # 未来的日子不画：画了就像在说"这天你漏了"
                    self._draw_cell(canvas, day, column, row, weekly.get(monday, 0))
        except tk.TclError:
            log.debug("重画热力图失败（窗口可能已销毁）", exc_info=True)

    def _draw_months(self, canvas: tk.Canvas, first_monday: date) -> None:
        """底部月份刻度：只在"月份变化"的那一列写"9月"，且两段标签至少隔开 2 列。"""
        prev_month: int | None = None
        last_label_column = -9          # 保证第一列一定标得出来
        y = self._cell_y(6) + self.cell + 2
        for column in range(self.weeks):
            monday = first_monday + timedelta(weeks=column)
            if prev_month is not None and monday.month == prev_month:
                continue
            if column - last_label_column < 2:
                continue
            canvas.create_text(self._col_x(column), y, text=f"{monday.month}月",
                               fill=SUB, font=self.font_small, anchor="nw")
            prev_month = monday.month
            last_label_column = column

    def _draw_cell(self, canvas: tk.Canvas, day: date, column: int, row: int, week_count: int) -> None:
        """画一个格子，并绑上悬停提示。"""
        cell = self.cell
        x0 = self._col_x(column)
        y0 = self._cell_y(row)
        checked = day in self._days
        if checked:
            fill = self.LEVEL_COLORS[min(2, max(0, week_count - 1))]
            outline, width = fill, 1
        else:
            fill = PANEL_ALT
            # 截止日**已经过去**且这天没打卡 → 红框提醒（一个字都不写，避免"逾期"字样）。
            # 刻意不标"今天就是截止日"的情况：截止日当天仍可打卡、状态还是"进行中"，
            # 标红会把"最后一天"读成"已经欠账了"，与用户的核心诉求相冲突。
            missed_deadline = self._deadline == day and day < self._today
            outline, width = (RED, 2) if missed_deadline else (PANEL_ALT, 1)
        tag = f"cell-{day.isoformat()}"
        canvas.create_rectangle(x0, y0, x0 + cell, y0 + cell, fill=fill,
                                outline=outline, width=width, tags=tag)
        canvas.tag_bind(tag, "<Enter>", lambda _event, d=day, done=checked: self._show_hover(d, done))
        canvas.tag_bind(tag, "<Leave>", lambda _event: self._hide_hover())

    # ------------------------------------------------------------------ 悬停
    def _show_hover(self, day: date, checked: bool) -> None:
        """顶部那行改写成 ``2026-10-09 已打卡 / 未打卡``。"""
        if self._header is None:
            return
        try:
            canvas = self.canvas
            if not canvas.winfo_exists():
                return
            canvas.itemconfigure(self._header, text=f"{day:%Y-%m-%d} {'已打卡' if checked else '未打卡'}")
        except tk.TclError:
            log.debug("更新热力图悬停提示失败（窗口可能已销毁）", exc_info=True)

    def _hide_hover(self) -> None:
        """鼠标移开后恢复成日期范围。"""
        if self._header is None:
            return
        try:
            canvas = self.canvas
            if not canvas.winfo_exists():
                return
            canvas.itemconfigure(self._header, text=self._range_text)
        except tk.TclError:
            log.debug("恢复热力图提示行失败（窗口可能已销毁）", exc_info=True)


# ================================================================ 累计打卡任务详情
class CumulativeDetail(ttk.Frame):
    """累计打卡任务的详情面板：标题 + 状态行 + 进度条 + 热力图 + 打卡按钮。

    放在任务列表下方，回答"这条任务还差几次、哪几天打了卡、现在该不该打卡"。

    数据来源分三路（这样它不依赖数据库，能单独跑起来截图 / 单测）：

    * 进度与状态：:meth:`show_task` 的 ``status``（调用方用
      :func:`~timeguard.recurrence.cumulative_status` 算好）；
    * 打卡历史：:meth:`set_checked_days`（``Task`` 本身不带历史）；
    * 打卡动作：``on_check_in`` / ``on_undo`` 回调（返回"是否成功 + 一句话"）。

    ``show_task`` 每次调用都会复位按钮与提示，所以"打卡成功后按钮置灰"不会残留到
    下一条任务上。
    """

    #: 面板自然高度约 200px（133% 缩放的 Windows 上实测 201px）——
    #: 主窗口可用高度只有 ~704px，这里不能长太高
    HEIGHT_HINT = 200
    #: 「撤销今日打卡」按钮的底色（比 PANEL_ALT 亮一点，跟主界面的次要按钮一致）
    UNDO_BG = "#2f3a52"

    def __init__(self, master: tk.Misc, font_family: str = "Microsoft YaHei UI",
                 on_check_in=None, on_undo=None, on_backfill=None, **kwargs) -> None:
        kwargs.setdefault("style", STYLE_CARD)
        kwargs.setdefault("padding", (10, 3))
        super().__init__(master, **kwargs)
        _ensure_styles(self)
        self.font_family = font_family
        self.font_body = (font_family, 10)
        self.font_small = (font_family, 9)
        #: ``on_check_in(task_id) -> (bool, str)``：是否成功 + 给用户看的一句话
        self.on_check_in = on_check_in
        #: ``on_undo(task_id) -> (bool, str)``
        self.on_undo = on_undo
        #: ``on_backfill(task_id) -> (bool, str)``：打开"补签"对话框（真正的取日期在调用方）
        self.on_backfill = on_backfill
        #: 当前展示的任务（``timeguard.database.Task``）
        self.task = None
        #: 当前展示的状态（``timeguard.recurrence.CumulativeStatus``）
        self.status = None
        self._checked_days: set[date] = set()
        self._place: tuple[str, dict] | None = None       # clear() 之前记下的布局参数
        self._build()

    # ------------------------------------------------------------------ 布局
    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.title_var = tk.StringVar(value="")
        self.plan_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="")
        self.hint_var = tk.StringVar(value="")

        tk.Label(self, textvariable=self.title_var, bg=PANEL, fg=ACCENT, anchor="w",
                 font=(self.font_family, 11, "bold")).grid(row=0, column=0, sticky="ew")
        tk.Label(self, textvariable=self.plan_var, bg=PANEL, fg=SUB, anchor="e",
                 font=self.font_small).grid(row=0, column=1, sticky="e")
        tk.Label(self, textvariable=self.status_var, bg=PANEL, fg=FG, anchor="w",
                 font=self.font_small).grid(row=1, column=0, columnspan=2, sticky="ew")

        self.progress = ProgressBar(self, width=420, height=16, bg=PANEL, font_family=self.font_family)
        self.progress.grid(row=2, column=0, columnspan=2, sticky="w", pady=(2, 2))

        # 18 周 × 8px 格子：热力图整体 ~97px 高，塞得进 HEIGHT_HINT 的高度预算
        self.heatmap = CheckinHeatmap(self, font_family=self.font_family, weeks=18, cell=8, gap=2)
        self.heatmap.grid(row=3, column=0, columnspan=2, sticky="w")

        self.hint_label = tk.Label(self, textvariable=self.hint_var, bg=PANEL, fg=SUB, anchor="w",
                                   font=self.font_small)
        self.hint_label.grid(row=4, column=0, sticky="ew", pady=(2, 0))

        buttons = tk.Frame(self, bg=PANEL)
        buttons.grid(row=4, column=1, sticky="e", pady=(2, 0))
        # 用 tk.Button 而不是 ttk.Button：ttk 按钮的颜色在非 clam 主题下会被系统画法忽略，
        # 浅色文字落在浅灰底上＝看不见（见 _ensure_styles 的说明）
        self.check_button = tk.Button(buttons, text="✓ 打卡", command=self._check_in,
                                      bg=ACCENT, fg="#ffffff", activebackground="#6ba0ff",
                                      activeforeground="#ffffff", disabledforeground=SUB,
                                      relief="flat", bd=0, highlightthickness=0, cursor="hand2",
                                      font=(self.font_family, 9, "bold"), padx=12, pady=3)
        self.check_button.pack(side="left", padx=(0, 6))
        self.undo_button = tk.Button(buttons, text="撤销今日打卡", command=self._undo,
                                     bg=self.UNDO_BG, fg=FG, activebackground="#3a4763",
                                     activeforeground="#ffffff", disabledforeground=SUB,
                                     relief="flat", bd=0, highlightthickness=0, cursor="hand2",
                                     font=(self.font_family, 9), padx=10, pady=3)
        self.undo_button.pack(side="left", padx=(0, 6))
        # 补签：只能补"任务创建之后、今天之前"的日子（约束在数据层，见 UsageStore.check_in）
        self.backfill_button = tk.Button(buttons, text="📝 补签…", command=self._backfill,
                                         bg=self.UNDO_BG, fg=FG, activebackground="#3a4763",
                                         activeforeground="#ffffff", disabledforeground=SUB,
                                         relief="flat", bd=0, highlightthickness=0, cursor="hand2",
                                         font=(self.font_family, 9), padx=10, pady=3)
        self.backfill_button.pack(side="left")

    # ------------------------------------------------------------------ 对外
    def show_task(self, task, status) -> None:
        """展示一条累计打卡任务的详情（每次调用都复位按钮 / 提示，是"切任务"的入口）。"""
        try:
            self._show_task(task, status)
        except tk.TclError:
            log.debug("刷新累计打卡面板失败（窗口可能已销毁）", exc_info=True)

    def set_checked_days(self, days) -> None:
        """喂入这条任务的打卡日期集合，转交给热力图。

        在 :meth:`show_task` **之前或之后**调用都行：之前调用先存着（``show_task``
        会一起画出来），之后调用会立刻重画热力图。
        """
        self._checked_days = _coerce_days(days)
        self._refresh_heatmap()

    def clear(self) -> None:
        """切到非累计打卡任务时清空并隐藏面板。

        会先记住当前的布局参数（``pack`` / ``grid``），这样之后再次
        :meth:`show_task` 能原样唤醒，不需要调用方重新 ``pack`` 一次。
        """
        try:
            self._clear()
        except tk.TclError:
            log.debug("清空累计打卡面板失败（窗口可能已销毁）", exc_info=True)

    # ------------------------------------------------------------------ 内部：内容
    def _show_task(self, task, status) -> None:
        """:meth:`show_task` 的实际实现（把 ``tk.TclError`` 留给外面统一吞）。"""
        self.task = task
        self.status = status
        self._restore_geometry()
        self.title_var.set(self._title_text(task))
        self.plan_var.set(self._plan_text(task))
        self.status_var.set(f"{status.status_text()}　·　{status.countdown_text()}")
        self.progress.set_progress(status.total, status.target, status.progress_text())
        self._refresh_heatmap()
        self._reset_buttons()

    def _clear(self) -> None:
        """:meth:`clear` 的实际实现。"""
        self.task = None
        self.status = None
        self._checked_days = set()
        for var in (self.title_var, self.plan_var, self.status_var, self.hint_var):
            var.set("")
        try:
            self.heatmap.set_data(set())
        except tk.TclError:
            log.debug("清空热力图失败（窗口可能已销毁）", exc_info=True)
        self._hide()

    @staticmethod
    def _title_text(task) -> str:
        """标题（重要任务带 ★，跟任务列表的写法保持一致）。"""
        title = str(getattr(task, "title", "") or "")
        try:
            important = int(getattr(task, "priority", 1) or 1) >= 2
        except (TypeError, ValueError):
            important = False
        return f"★ {title}" if important else title

    @staticmethod
    def _plan_text(task) -> str:
        """任务的一句话说明（累计打卡任务用它自带的 ``checkin_schedule_text()``）。

        开了"监控时长达标自动打卡"的话，把那条规则也接在后面 —— 任务列表的列宽放不下，
        但详情面板这一行有地方，用户需要在这里确认"到底监控哪个对象、多少分钟"。
        """
        rule = getattr(task, "rule", None)
        build = getattr(rule, "checkin_schedule_text", None)
        if not callable(build):
            return ""
        try:
            text = str(build())
            auto = getattr(rule, "auto_checkin_text", None)
            if callable(auto):
                extra = str(auto())
                if extra:
                    text = f"{text}　·　{extra}"
            return text
        except Exception:  # noqa: BLE001 - 说明文字拿不到不该影响面板显示
            log.debug("生成任务说明失败", exc_info=True)
            return ""

    def _refresh_heatmap(self) -> None:
        """按当前 ``status`` 的"今天"和截止日刷新热力图。

        "今天"取 ``status.today`` 而不是真实今天：状态是按某一天算出来的，
        热力图必须用同一天，否则"今天已打卡"和热力图上今天的格子会对不上。
        """
        status = self.status
        today = _as_date(getattr(status, "today", None)) or date.today()
        deadline = _as_date(getattr(status, "deadline", None))
        try:
            self.heatmap.set_data(self._checked_days, today=today, deadline=deadline)
        except tk.TclError:
            log.debug("刷新打卡热力图失败（窗口可能已销毁）", exc_info=True)

    # ------------------------------------------------------------------ 内部：按钮
    def _set_enabled(self, button: tk.Button, enabled: bool, enabled_bg: str) -> None:
        """切按钮的可用状态**并同步配色**（tk.Button 的禁用态不会自己变暗）。

        ``state`` 仍然老老实实设成 ``"normal"`` / ``"disabled"``：调用方（以及验收
        脚本）会直接读 ``cget("state")`` 来判断按钮是不是灰的。
        """
        try:
            if enabled:
                button.configure(state="normal", bg=enabled_bg, fg="#ffffff", cursor="hand2")
            else:
                button.configure(state="disabled", bg=PANEL_ALT, fg=SUB, cursor="arrow")
        except tk.TclError:
            log.debug("切换按钮状态失败（窗口可能已销毁）", exc_info=True)

    def _reset_buttons(self) -> None:
        """按当前 status 复位按钮与提示行。"""
        status = self.status
        finished = bool(status is not None and status.finished)
        missed = bool(status is not None and status.missed)
        checked = bool(status is not None and status.today_checked)
        self._set_enabled(self.check_button, not (finished or missed), ACCENT)
        self._set_enabled(self.undo_button, checked and not finished, self.UNDO_BG)
        if finished:
            # 已完成 / 已逾期都禁用「✓ 打卡」，并用一句话说明原因
            self._set_hint("已达成目标，不再提醒", GREEN)
        elif missed:
            self._set_hint("已过截止日期", AMBER)
        else:
            self._set_hint("", SUB)

    def _set_hint(self, text: str, color: str = SUB) -> None:
        """写提示行（成功绿 / 失败琥珀）。"""
        self.hint_var.set(text)
        try:
            self.hint_label.configure(fg=color)
        except tk.TclError:
            log.debug("更新提示行失败（窗口可能已销毁）", exc_info=True)

    def _check_in(self) -> None:
        """「✓ 打卡」：转发给调用方注入的回调。"""
        self._run_action(self.on_check_in, self.check_button)

    def _undo(self) -> None:
        """「撤销今日打卡」：转发给调用方注入的回调。"""
        self._run_action(self.on_undo, self.undo_button)

    def _backfill(self) -> None:
        """「📝 补签…」：交给调用方弹对话框选日期。

        补签的合法性（不能补未来、不能补任务创建之前、同一天只能一次）由
        :meth:`~timeguard.database.UsageStore.check_in` 判定，这里只负责转发与显示结果。
        """
        self._run_action(self.on_backfill, self.backfill_button)

    def _run_action(self, callback, source: tk.Button) -> None:
        """调回调并把返回的那句话显示出来；成功后按"保守方向"拨按钮。

        按钮的**最终**状态由调用方下一次 ``show_task``（重新算过 status 之后）决定：
        这里只是立刻反馈一下，免得用户在两秒内连点两次。
        """
        task = self.task
        if task is None:
            return
        if not callable(callback):
            self._set_hint("这个面板没有接上操作回调", AMBER)
            return
        try:
            ok, message = callback(task.id)
        except Exception:  # noqa: BLE001 - 回调是调用方的代码，出错也不该把界面打崩
            log.debug("累计打卡操作失败", exc_info=True)
            self._set_hint("操作失败，请稍后再试", AMBER)
            return
        ok = bool(ok)
        self._set_hint(str(message or ("操作完成" if ok else "操作没成功")), GREEN if ok else AMBER)
        if not ok:
            return
        if source is self.check_button:
            self._set_enabled(self.check_button, False, ACCENT)
            # 刚打完卡，撤销是合法操作
            self._set_enabled(self.undo_button, True, self.UNDO_BG)
        elif source is self.backfill_button:
            # 补签不改"今天"的状态：两个按钮维持原样，等下一次 show_task 复位
            pass
        else:
            self._set_enabled(self.undo_button, False, self.UNDO_BG)
            status = self.status
            if not (status is not None and (status.finished or status.missed)):
                self._set_enabled(self.check_button, True, ACCENT)

    # ------------------------------------------------------------------ 内部：显示 / 隐藏
    def _hide(self) -> None:
        """隐藏自己。``pack_forget`` 会丢掉布局参数，所以先记下来给 ``show_task`` 用。"""
        try:
            manager = self.winfo_manager()
            if manager == "pack":
                info = {key: value for key, value in self.pack_info().items() if key != "in"}
                self._place = ("pack", info)
                self.pack_forget()
            elif manager == "grid":
                self._place = ("grid", {})
                self.grid_remove()
            else:
                self._place = None
        except tk.TclError:
            log.debug("隐藏累计打卡面板失败（窗口可能已销毁）", exc_info=True)

    def _restore_geometry(self) -> None:
        """把 ``clear()`` 藏起来的面板放回原位（没被藏过就什么都不做）。"""
        place = self._place
        self._place = None
        if place is None:
            return
        kind, info = place
        try:
            if kind == "pack":
                self.pack(**info)
            elif kind == "grid":
                self.grid()
        except tk.TclError:
            log.debug("重新显示累计打卡面板失败（窗口可能已销毁）", exc_info=True)

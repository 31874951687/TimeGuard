"""主界面：深色主题的 tkinter 窗口（实时状态 / 统计图表 / 设置面板）+ 系统托盘。

界面结构::

    ┌──────────────────────────────────────────────┐
    │ TimeGuard 时间管家            [隐藏到托盘]   │
    ├──────────────────────────────────────────────┤
    │ 今日已用 1小时12分 / 上限 2小时    剩余48分   │
    │ ▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░░░░░  60%                  │
    │ 当前正在监控：哔哩哔哩（chrome.exe）          │
    ├──────────────────────────────────────────────┤
    │ [统计图表] [设置]                             │
    │ ┌── 7 天柱状图 ──┐  ┌── 设置面板 ──┐          │
    └──────────────────────────────────────────────┘

线程约定：所有 tkinter 操作都在主线程执行；监控引擎通过 ``queue`` 和
``after()`` 把数据投递到主线程，托盘回调同样通过队列回到主线程。
"""

from __future__ import annotations

import logging
import queue
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

from . import notifier as notifier_mod
from . import paths, phrases, winapi
from .chart import WeekChart, pick_cjk_font
from .config import Config
from .database import UsageStore
from .engine import Snapshot, UsageEngine
from .notifier import Notifier, PopupManager
from .recurrence import auto_checkin_ready
from .scheduler import TaskScheduler
from .settings_page import SettingsPage
from .tasks import (
    RecurringReminderDialog,
    StartupCheckDialog,
    TaskPanel,
    style_panedwindow,
)
from .utils import fmt_duration, match_target_seconds, today_str

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- 颜色与字号
BG = "#10141c"          # 窗口底色
PANEL = "#1b2130"       # 卡片底色
PANEL_ALT = "#232b3d"   # 列表行底色
FG = "#e6eaf2"          # 主文字
SUB = "#8b95a8"         # 次要文字
ACCENT = "#4f8cff"      # 主题蓝
GREEN = "#33c48d"
AMBER = "#ffb020"
RED = "#ff6b6b"


def style_dark_combobox(widget: tk.Misc) -> None:
    """让 ttk.Combobox（含它的弹出列表）跟随深色主题。

    注意：``fieldbackground`` 只在 clam 主题下生效；``*TCombobox*Listbox`` 这几个
    选项名控制弹出列表的底色/字色，不设置的话在深色界面里会白底白字看不见
    （日历控件与“预设方案”下拉框都踩过这个坑）。
    """
    try:
        style = ttk.Style(widget)
        is_clam = style.theme_use() == "clam"
        style.configure(
            "Dark.TCombobox",
            foreground=FG, fieldbackground=PANEL_ALT, background=PANEL_ALT,
            arrowcolor=FG, bordercolor=PANEL_ALT, lightcolor=PANEL_ALT, darkcolor=PANEL_ALT,
            selectbackground=ACCENT, selectforeground="#ffffff",
            padding=3, arrowsize=14,
        )
        for state, spec in (
            (("readonly",), {"fieldbackground": PANEL_ALT, "foreground": FG, "background": PANEL_ALT}),
            (("readonly", "focus"), {"fieldbackground": PANEL_ALT, "foreground": FG,
                                     "selectbackground": ACCENT, "selectforeground": "#ffffff"}),
            (("disabled",), {"fieldbackground": PANEL, "foreground": SUB, "arrowcolor": SUB}),
            (("active",), {"background": "#2b3550" if is_clam else PANEL_ALT}),
        ):
            style.map("Dark.TCombobox", **{key: [(state, value)] for key, value in spec.items()})
        # 下拉弹出列表（内部是一个 tk Listbox）
        widget.option_add("*TCombobox*Listbox.background", PANEL_ALT)
        widget.option_add("*TCombobox*Listbox.foreground", FG)
        widget.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        widget.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        widget.option_add("*TCombobox*Listbox.font", "TkDefaultFont")
    except tk.TclError:
        log.debug("配置下拉框样式失败", exc_info=True)


def _mix(c1: str, c2: str, t: float) -> str:
    """按比例混合两个 #RRGGBB 颜色。"""
    t = max(0.0, min(1.0, t))
    a = tuple(int(c1[i : i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(c2[i : i + 2], 16) for i in (1, 3, 5))
    return "#" + "".join(f"{int(a[i] + (b[i] - a[i]) * t):02x}" for i in range(3))


class UsageBar(tk.Canvas):
    """细长的进度条（自绘，支持随百分比变色与超限高亮）。"""

    def __init__(self, master: tk.Misc, width: int = 520, height: int = 14, **kwargs) -> None:
        super().__init__(
            master, width=width, height=height, bg=PANEL, highlightthickness=0, bd=0, **kwargs
        )
        self._ratio = 0.0
        self._width = width
        self._height = height

    def set_ratio(self, ratio: float) -> None:
        """设置进度（0~1+），并重绘。"""
        ratio = max(0.0, min(1.5, float(ratio)))
        if abs(ratio - self._ratio) < 0.001:
            return
        self._ratio = ratio
        self.redraw()

    def redraw(self) -> None:
        """重绘进度条。"""
        self.delete("all")
        w, h = self._width, self._height
        radius = h / 2
        # 轨道
        self.create_oval(0, 0, h, h, fill=PANEL_ALT, outline=PANEL_ALT)
        self.create_oval(w - h, 0, w, h, fill=PANEL_ALT, outline=PANEL_ALT)
        self.create_rectangle(radius, 0, w - radius, h, fill=PANEL_ALT, outline=PANEL_ALT)

        ratio = min(1.0, self._ratio)
        if ratio <= 0:
            return
        color = GREEN if self._ratio < 0.7 else (AMBER if self._ratio < 1.0 else RED)
        fill_w = max(h, w * ratio)
        self.create_oval(0, 0, h, h, fill=color, outline=color)
        self.create_oval(fill_w - h, 0, fill_w, h, fill=color, outline=color)
        self.create_rectangle(radius, 0, fill_w - radius, h, fill=color, outline=color)

    def configure_width(self, width: int) -> None:
        """窗口缩放时调整宽度。"""
        self._width = max(120, int(width))
        self.configure(width=self._width)
        self.redraw()



# ---------------------------------------------------------------- 关闭方式选择
class CloseChoiceDialog(tk.Toplevel):
    """点标题栏叉号时问一句：**退出** 还是 **继续在后台运行**？

    为什么要问：这两种意图在界面上只差"点了一下叉号"，但后果完全不同 ——

    * 以前叉号 = 藏进托盘：用户以为关掉了，监控还在跑（**界面在说谎**）；
    * 改成叉号 = 一律退出：习惯"点叉号收进托盘"的用户会不小心关掉监控。

    所以把选择权交回给用户，并且把两个按钮的后果写在按钮旁边（而不是让人猜）。

    :ivar choice: ``"exit"`` 退出程序 / ``"tray"`` 收进托盘 / ``None`` 取消
    """

    WIDTH = 470

    def __init__(self, master: tk.Misc, font_family: str = "Microsoft YaHei UI") -> None:
        super().__init__(master)
        self.choice: str | None = None
        self.font_family = font_family
        self.font_body = (font_family, 10)
        self.font_small = (font_family, 9)
        self.title("关闭 TimeGuard")
        self.configure(bg=PANEL)
        self.resizable(False, False)
        self.transient(master)
        try:
            self.attributes("-toolwindow", True)
        except tk.TclError:
            pass
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda _e: self._cancel())

        body = ttk.Frame(self, style="Card.TFrame", padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="你想怎么关？", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(body, text="两种方式都不会丢数据，区别只是关掉之后还统计不统计。",
                  style="CardHint.TLabel").pack(anchor="w", pady=(4, 12))

        grid = ttk.Frame(body, style="Card.TFrame")
        grid.pack(fill="x")
        self.btn_exit = ttk.Button(grid, text="🛑 退出程序", style="Danger.TButton", width=16,
                                   command=lambda: self._choose("exit"))
        self.btn_exit.grid(row=0, column=0, sticky="w")
        ttk.Label(grid, text="停止统计游戏与网页使用时长，程序完全关闭",
                  style="CardHint.TLabel", wraplength=300, justify="left").grid(
            row=0, column=1, sticky="w", padx=(12, 0)
        )
        self.btn_tray = ttk.Button(grid, text="🗕 后台继续运行", style="Accent.TButton", width=16,
                                   command=lambda: self._choose("tray"))
        self.btn_tray.grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Label(grid, text="窗口收进系统托盘，继续统计；点托盘图标随时能打开",
                  style="CardHint.TLabel", wraplength=300, justify="left").grid(
            row=1, column=1, sticky="w", padx=(12, 0), pady=(10, 0)
        )

        buttons = ttk.Frame(body, style="Card.TFrame")
        buttons.pack(fill="x", pady=(16, 0))
        ttk.Button(buttons, text="取消（不关闭）", style="Small.TButton", command=self._cancel).pack(side="right")

        self._center(master)
        try:
            self.grab_set()
        except tk.TclError:
            pass
        self.btn_tray.focus_set()

    def _center(self, master: tk.Misc) -> None:
        """相对父窗口居中（复用主窗口的位置，不跳到屏幕角落）。

        注意 ``winfo_geometry()`` 在窗口映射之前读到的仍是旧值（Tk 把 geometry
        请求排队到映射时才生效），所以调用方要断言位置的话得等一次 ``update()``。
        """
        self.update_idletasks()
        width = max(self.WIDTH, self.winfo_reqwidth())
        height = self.winfo_reqheight()
        try:
            x = master.winfo_rootx() + max(0, (master.winfo_width() - width) // 2)
            y = master.winfo_rooty() + max(0, (master.winfo_height() - height) // 3)
        except tk.TclError:
            x = y = 200
        self.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")

    def _choose(self, action: str) -> None:
        """记下选择并关闭对话框。"""
        self.choice = action
        self.destroy()

    def _cancel(self) -> None:
        self.choice = None
        self.destroy()

    def show(self) -> str | None:
        """显示对话框并等待用户选择（取消返回 ``None``）。"""
        self.wait_window(self)
        return self.choice


# ---------------------------------------------------------------- 主窗口
class MainWindow:
    """程序主窗口：负责界面刷新、托盘、把监控引擎与 UI 连接起来。"""

    REFRESH_MS = 1000          # 界面刷新周期（毫秒）
    CHART_REFRESH_MS = 60000   # 图表自动刷新周期
    TRAY_REFRESH_MS = 30000    # 托盘悬浮提示刷新周期

    def __init__(self, config: Config, store: UsageStore, engine: UsageEngine, notifier: Notifier,
                 manual_task_check: bool = False) -> None:
        self.config = config
        self.store = store
        self.engine = engine
        self.notifier = notifier
        #: 由 ``--check-tasks`` 手动触发检查时为 True（此时不做默认的启动检查，避免重复）
        self._manual_task_check = manual_task_check

        self.root = tk.Tk()
        winapi.set_dpi_awareness()
        self.scale = winapi.scale_factor()
        self.font_family = pick_cjk_font()
        self.font_body = (self.font_family, 10)
        self.font_small = (self.font_family, 9)
        self.font_title = (self.font_family, 16, "bold")
        self.font_big = (self.font_family, 16, "bold")

        self.snapshot: Snapshot | None = None
        self._dirty = False
        self._tray_icon = None
        self._tray_thread = None
        self._tray_actions: "queue.Queue[str]" = queue.Queue()
        self._hidden = False
        self._last_chart_refresh = 0.0
        self._toast_after_id: str | None = None

        self._setup_window()
        self._setup_style()
        self._build_ui()

        self.popup_manager = PopupManager(self.root, font_family=self.font_family)
        self.engine.attach_popup_manager(self.popup_manager)
        self.engine.on_limit_exceeded = self._on_limit_exceeded
        self.engine.on_state_change = self._on_engine_state

        # 周期任务提醒调度器：算出"下一次该响的准确时刻"，只排一个定时器（不轮询）
        self.task_scheduler = TaskScheduler(
            self.root, self.store, self._on_recurring_batch, self.config.settings,
        )
        self._startup_check_done = False
        self._shutting_down = False

        self._setup_tray()
        self._bind_events()

        # 先启动调度器（它会自己排好下一次唤醒），再做开机统一检查。
        # 顺序很重要：统一检查要用"还没派发过"的状态去收集错过的周期任务。
        self.root.after(1400, self.start_task_scheduler)

        # 启动后按设置决定是否直接进托盘
        if self.config.settings.start_minimized:
            self.root.after(300, self.hide_to_tray)

        # 开机统一检查（界面出现后 1.7 秒，排在调度器启动之后）：
        # 把"错过的周期任务"与"未完成的待办"合并成**一个**弹窗，只打扰一次。
        # 注意：这里**总是**要跑 —— 它同时负责把调度器"延后派发"的错过提醒落地；
        # 是否展示未完成待办清单由 task_check_on_start 决定（见 startup_check）。
        # 例外：--check-tasks 已经手动跑过一次检查，这里就不再重复弹窗。
        if not self._manual_task_check:
            self.root.after(1700, self.startup_check)

        self.root.after(self.REFRESH_MS, self._tick_ui)
        self.root.after(400, self.refresh_chart)
        log.info("主界面初始化完成（DPI 缩放 %.2f，字体 %s）", self.scale, self.font_family)

    # ------------------------------------------------------------------ 窗口
    def _setup_window(self) -> None:
        """按「设置的窗口尺寸」+「屏幕可用工作区」计算实际窗口大小。

        用户可在设置页选择窗口大小（预设 + 自定义宽高），这里负责夹取到可用范围内，
        小屏笔记本（例如 1366x768，工作区仅 728 高）上不会被任务栏裁掉。
        """
        self.root.title(f"TimeGuard 时间管家 · 今日 {today_str()}")
        self.root.configure(bg=BG)
        self.apply_window_size(self.config.settings.window_width,
                               self.config.settings.window_height,
                               center=True)
        self._set_window_icon()

    def apply_window_size(self, width: int, height: int, center: bool = True) -> tuple[int, int]:
        """应用窗口尺寸（会被屏幕可用区域自动收缩），返回最终生效的 (宽, 高)。"""
        scale = min(self.scale, 1.5)
        work_w, work_h = winapi.get_work_area(self.root)
        final_w = int(max(720, min(int(width * scale), work_w - 24)))
        final_h = int(max(480, min(int(height * scale), work_h - 24)))
        min_w = min(int(880 * scale), work_w - 24)
        min_h = min(int(600 * scale), work_h - 24)

        self.root.minsize(max(640, min_w), max(460, min_h))
        if center:
            x = max(0, (work_w - final_w) // 2)
            y = max(0, (work_h - final_h) // 3)
        else:
            x, y = self.root.winfo_x(), self.root.winfo_y()
        self.root.geometry(f"{final_w}x{final_h}+{x}+{y}")
        log.info("窗口尺寸 %dx%d（设置值 %dx%d，工作区 %dx%d，DPI %.2f）",
                 final_w, final_h, width, height, work_w, work_h, self.scale)
        return final_w, final_h

    def _set_stats_sash(self, event: tk.Event) -> None:
        """统计页首次布局时把分隔条放在 45%（之后用户可自由拖拽）。"""
        if not getattr(self, "_stats_sash_pending", False) or event.widget is not self.stats_paned:
            return
        height = self.stats_paned.winfo_height()
        if height < 200:
            return
        self._stats_sash_pending = False
        try:
            self.stats_paned.sashpos(0, int(height * 0.45))
        except tk.TclError:
            log.debug("设置统计页分隔条失败", exc_info=True)

    def _set_window_icon(self) -> None:
        """设置窗口图标（resources/icon.ico 存在时生效）。"""
        icon = paths.icon_file()
        if icon.exists():
            try:
                self.root.iconbitmap(default=str(icon))
            except tk.TclError:
                log.debug("加载窗口图标失败", exc_info=True)

    def _setup_style(self) -> None:
        """配置 ttk 深色主题（基于 clam，可自定义颜色）。"""
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        try:
            self.root.tk.call("tk", "scaling", max(1.0, self.scale) * 1.2)
        except tk.TclError:
            pass

        style.configure(".", background=BG, foreground=FG, font=self.font_body)
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG, font=self.font_body)
        style.configure("Card.TFrame", background=PANEL)
        style.configure("Card.TLabel", background=PANEL, foreground=FG, font=self.font_body)
        style.configure("CardHint.TLabel", background=PANEL, foreground=SUB, font=self.font_small)
        style.configure("CardTitle.TLabel", background=PANEL, foreground=ACCENT,
                        font=(self.font_family, 11, "bold"))
        style.configure("Header.TFrame", background=BG)
        style.configure("Header.TLabel", background=BG, foreground=FG, font=self.font_title)
        style.configure("HeaderSub.TLabel", background=BG, foreground=SUB, font=self.font_small)
        style.configure("Big.TLabel", background=PANEL, foreground=FG, font=self.font_big)
        style.configure("BigOver.TLabel", background=PANEL, foreground=RED, font=self.font_big)
        style.configure("Stat.TLabel", background=PANEL, foreground=SUB, font=self.font_small)
        style.configure("StatValue.TLabel", background=PANEL, foreground=FG,
                        font=(self.font_family, 12, "bold"))
        style.configure("Row.TLabel", background=PANEL_ALT, foreground=FG, font=self.font_body)
        style.configure("RowSub.TLabel", background=PANEL_ALT, foreground=SUB, font=self.font_small)

        # 按钮
        style.configure("TButton", background=PANEL_ALT, foreground=FG, borderwidth=0,
                        focusthickness=0, padding=(10, 6), font=self.font_body)
        style.map("TButton",
                  background=[("active", _mix(PANEL_ALT, "#ffffff", 0.12)), ("disabled", PANEL)],
                  foreground=[("disabled", SUB)])
        style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                        font=(self.font_family, 10, "bold"))
        style.map("Accent.TButton", background=[("active", _mix(ACCENT, "#ffffff", 0.15))])
        style.configure("Danger.TButton", background="#7a2b2b", foreground="#ffdede")
        style.map("Danger.TButton", background=[("active", _mix("#7a2b2b", "#ffffff", 0.15))])
        style.configure("Small.TButton", padding=(8, 5), font=self.font_small)
        style.configure("Tiny.TButton", padding=(5, 2), font=(self.font_family, 8))
        style.configure("TButton", relief="flat")

        # 输入控件
        style.configure("TEntry", fieldbackground=PANEL_ALT, foreground=FG, insertcolor=FG,
                        bordercolor=PANEL_ALT, lightcolor=PANEL_ALT, darkcolor=PANEL_ALT, padding=4)
        style.configure("TSpinbox", fieldbackground=PANEL_ALT, foreground=FG, arrowcolor=FG,
                        bordercolor=PANEL_ALT, insertcolor=FG, padding=3)
        style.configure("Card.TSpinbox", fieldbackground=PANEL_ALT, foreground=FG, arrowcolor=FG,
                        bordercolor=PANEL_ALT, lightcolor=PANEL_ALT, darkcolor=PANEL_ALT,
                        insertcolor=FG, padding=3)
        style.configure("TCheckbutton", background=PANEL, foreground=FG, font=self.font_body)
        style.map("TCheckbutton", background=[("active", PANEL)],
                  indicatorcolor=[("selected", ACCENT), ("!selected", PANEL_ALT)])
        style.configure("Card.TCheckbutton", background=PANEL, foreground=FG, font=self.font_body)
        style.map("Card.TCheckbutton", background=[("active", PANEL)],
                  indicatorcolor=[("selected", ACCENT), ("!selected", PANEL_ALT)])
        style.configure("Card.TRadiobutton", background=PANEL, foreground=FG, font=self.font_body)
        style.map("Card.TRadiobutton", background=[("active", PANEL)],
                  indicatorcolor=[("selected", ACCENT), ("!selected", PANEL_ALT)])
        style.configure("TSeparator", background=PANEL_ALT)

        # 滚动条
        style.configure("TScrollbar", background=PANEL_ALT, troughcolor=PANEL, bordercolor=PANEL,
                        arrowcolor=SUB, borderwidth=0, relief="flat")
        style.map("TScrollbar", background=[("active", _mix(PANEL_ALT, "#ffffff", 0.15))])
        style.configure("Card.Vertical.TScrollbar", background=PANEL_ALT, troughcolor=PANEL_ALT,
                        bordercolor=PANEL_ALT, arrowcolor=SUB, borderwidth=0)
        style.map("Card.Vertical.TScrollbar",
                  background=[("active", _mix(PANEL_ALT, "#ffffff", 0.15))])

        # 选项卡
        style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 6, 0, 0))
        style.configure("TNotebook.Tab", background=PANEL, foreground=SUB, padding=(18, 8),
                        font=(self.font_family, 10, "bold"), borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", PANEL_ALT), ("active", _mix(PANEL, "#ffffff", 0.08))],
                  foreground=[("selected", FG)])

    def _build_ui(self) -> None:
        """搭建主界面控件树。"""
        outer = ttk.Frame(self.root, style="TFrame", padding=(16, 10, 16, 8))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        # ---------- 顶部标题栏 ----------
        header = ttk.Frame(outer, style="Header.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="⏳ TimeGuard 时间管家", style="Header.TLabel").grid(row=0, column=0, sticky="w")
        self.label_status = ttk.Label(header, text="正在初始化…", style="HeaderSub.TLabel")
        self.label_status.grid(row=0, column=1, sticky="w", padx=(14, 0))
        self.btn_pause = ttk.Button(header, text="⏸ 暂停计时", style="Small.TButton", command=self.toggle_pause)
        self.btn_pause.grid(row=0, column=2, padx=(0, 6))
        ttk.Button(header, text="🗕 最小化到托盘", style="Small.TButton",
                   command=lambda: self.hide_to_tray(manual=True)).grid(
            row=0, column=3, padx=(0, 6)
        )
        ttk.Button(header, text="✖ 退出", style="Small.TButton", command=self.quit_app).grid(row=0, column=4)

        # ---------- 今日概览卡片 ----------
        card = ttk.Frame(outer, style="Card.TFrame", padding=(14, 10))
        card.grid(row=1, column=0, sticky="ew", pady=(8, 8))
        card.columnconfigure(0, weight=1)

        top = ttk.Frame(card, style="Card.TFrame")
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)

        left_box = ttk.Frame(top, style="Card.TFrame")
        left_box.grid(row=0, column=0, sticky="w")
        ttk.Label(left_box, text="今日已用 / 每日上限", style="Stat.TLabel").pack(anchor="w")
        used_row = ttk.Frame(left_box, style="Card.TFrame")
        used_row.pack(anchor="w")
        self.label_used = ttk.Label(used_row, text="0秒", style="Big.TLabel")
        self.label_used.pack(side="left")
        self.label_limit = ttk.Label(used_row, text=" / 2小时", style="StatValue.TLabel")
        self.label_limit.pack(side="left", padx=(6, 0), pady=(6, 0))

        mid_box = ttk.Frame(top, style="Card.TFrame")
        mid_box.grid(row=0, column=1, sticky="w", padx=(30, 0))
        ttk.Label(mid_box, text="距离上限剩余", style="Stat.TLabel").pack(anchor="w")
        self.label_remain = ttk.Label(mid_box, text="2小时", style="StatValue.TLabel")
        self.label_remain.pack(anchor="w")

        right_box = ttk.Frame(top, style="Card.TFrame")
        right_box.grid(row=0, column=2, sticky="e")
        ttk.Label(right_box, text="下次提醒 / 今日提醒次数", style="Stat.TLabel").pack(anchor="e")
        next_row = ttk.Frame(right_box, style="Card.TFrame")
        next_row.pack(anchor="e")
        self.label_next = ttk.Label(next_row, text="未超时", style="StatValue.TLabel")
        self.label_next.pack(side="left")
        self.label_popups = ttk.Label(next_row, text="　·　0 次", style="StatValue.TLabel")
        self.label_popups.pack(side="left")

        self.bar = UsageBar(card, width=900)
        self.bar.grid(row=1, column=0, sticky="ew", pady=(10, 4))
        card.bind("<Configure>", self._on_card_resize)

        # 当前状态 + 前台窗口合并成一行显示，节省纵向空间（待办页要用）
        status_row = ttk.Frame(card, style="Card.TFrame")
        status_row.grid(row=2, column=0, sticky="ew")
        status_row.columnconfigure(0, weight=1)
        self.label_current = ttk.Label(status_row, text="当前未检测到被监控的程序", style="Card.TLabel")
        self.label_current.grid(row=0, column=0, sticky="w")
        self.label_foreground = ttk.Label(status_row, text="", style="CardHint.TLabel")
        self.label_foreground.grid(row=0, column=1, sticky="e", padx=(16, 0))

        # ---------- 选项卡 ----------
        notebook = ttk.Notebook(outer)
        notebook.grid(row=2, column=0, sticky="nsew")
        self.notebook = notebook

        # 统计页：时长图表 + 今日明细表，用可拖拽分隔条分配高度（表格能拉大）
        stats_page = ttk.Frame(notebook, style="Card.TFrame", padding=(0, 6, 0, 0))
        stats_page.columnconfigure(0, weight=1)
        stats_page.rowconfigure(0, weight=1)
        style_panedwindow(stats_page)

        stats_paned = ttk.Panedwindow(stats_page, orient="vertical", style="Task.TPanedwindow")
        stats_paned.grid(row=0, column=0, sticky="nsew")

        chart_holder = ttk.Frame(stats_paned, style="Card.TFrame")
        self.chart = WeekChart(chart_holder, self.store, font_family=self.font_family)
        self.chart.pack(fill="both", expand=True)
        stats_paned.add(chart_holder, weight=4)

        summary = ttk.Frame(stats_paned, style="Card.TFrame")
        ttk.Label(summary, text="📊 今日各对象明细", style="CardTitle.TLabel").pack(anchor="w", pady=(6, 4))
        self.tree = ttk.Treeview(summary, columns=("target", "kind", "time"), show="headings", height=6)
        self.tree.heading("target", text="监控对象")
        self.tree.heading("kind", text="类型")
        self.tree.heading("time", text="今日时长")
        self.tree.column("target", width=340, anchor="w")
        self.tree.column("kind", width=120, anchor="center")
        self.tree.column("time", width=160, anchor="e")
        self._style_tree()
        self.tree.pack(fill="both", expand=True)
        stats_paned.add(summary, weight=5)
        self.stats_paned = stats_paned
        self._stats_sash_pending = True
        stats_paned.bind("<Configure>", self._set_stats_sash, add="+")
        notebook.add(stats_page, text="📊 数据统计")

        # 待办任务页（列表与图表用可拖拽分隔条分配高度，任何屏幕都不会被裁）
        tasks_page = ttk.Frame(notebook, style="Card.TFrame")
        tasks_page.columnconfigure(0, weight=1)
        tasks_page.rowconfigure(0, weight=1)
        self.task_panel = TaskPanel(tasks_page, self.store, app=self, font_family=self.font_family)
        self.task_panel.grid(row=0, column=0, sticky="nsew")
        notebook.add(tasks_page, text="✅ 待办任务")
        self.notebook_index_tasks = 1

        # 设置页：数据驱动（左列表 + 右编辑器），不再用"画布塞满控件"的滚动方案。
        # 旧方案一次滚动要重绘上百个控件（实测 128~155ms/帧），快速滑动必有残影。
        settings_page = ttk.Frame(notebook, style="TFrame")
        settings_page.columnconfigure(0, weight=1)
        settings_page.rowconfigure(0, weight=1)
        self.settings_panel = SettingsPage(settings_page, self)
        self.settings_panel.grid(row=0, column=0, sticky="nsew")
        # 向后兼容：旧代码/工具里用过 settings_scroll
        self.settings_scroll = self.settings_panel
        notebook.add(settings_page, text="⚙ 设置")
        self.notebook_index_settings = 2

        # ---------- 底部提示条 ----------
        self.label_toast = tk.Label(
            outer, text="", bg=BG, fg=GREEN, font=self.font_small, anchor="w", justify="left"
        )
        self.label_toast.grid(row=3, column=0, sticky="ew", pady=(8, 0))

    def _style_tree(self) -> None:
        """Treeview 深色样式（行高随 DPI 放大，表格更易读）。"""
        style = ttk.Style(self.root)
        style.configure("Treeview", background=PANEL_ALT, fieldbackground=PANEL_ALT, foreground=FG,
                        rowheight=int(30 * min(self.scale, 1.4)), borderwidth=0, font=self.font_body)
        style.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", "#ffffff")])
        style.configure("Treeview.Heading", background=PANEL, foreground=SUB, relief="flat",
                        font=(self.font_family, 9, "bold"), padding=(4, 6))
        style.map("Treeview.Heading", background=[("active", PANEL_ALT)])
        # ttk 的 Heading 样式不总是接受 padding，再用 option 兜一层
        try:
            self.root.option_add("*Treeview.Heading.padding", 6)
        except tk.TclError:
            log.debug("设置表头内边距失败", exc_info=True)

    def _on_card_resize(self, event: tk.Event) -> None:
        """概览卡片宽度变化时同步进度条宽度。"""
        try:
            self.bar.configure_width(event.width - 40)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ 事件
    def _bind_events(self) -> None:
        # 叉号 = 真退出（并提示"软件已退出运行"）；想后台运行请点「最小化到托盘」
        self.root.protocol("WM_DELETE_WINDOW", self.request_close)
        self.root.bind("<Unmap>", self._on_unmap)

    def _on_unmap(self, event: tk.Event) -> None:
        """窗口被最小化时自动隐藏到托盘（不占任务栏）。

        任务栏的最小化按钮同样是用户的主动操作，窗口"消失"这件事需要解释，
        所以这里也算 manual —— 否则用户会以为程序不见了。
        """
        if event.widget is self.root and self.root.state() == "iconic":
            self.root.after(200, lambda: self.hide_to_tray(manual=True))

    # ------------------------------------------------------------------ 刷新
    def _on_engine_state(self, snapshot: Snapshot) -> None:
        """引擎回调（运行在监控线程）——只保存快照，界面由主线程刷新。"""
        self.snapshot = snapshot

    def _auto_check_in_pass(self) -> None:
        """「监控时长达标自动打卡」：拿引擎快照里的今日时长，够阈值就自动打一次卡。

        为什么要**每秒**都能跑：引擎每秒产出一个快照，自动打卡必须跟着实时时长走；
        但真正做事的只有"已经开了自动打卡、今天还没打卡、还没达标/过期"的任务，
        绝大多数时候这里立刻就返回了。

        三条例外（与手动打卡同一套口径，见 ``recurrence.auto_checkin_ready``）：

        * 今天已经打过卡 → 跳过（同一天只算一次）；
        * 已经攒够目标 / 已经过了截止日 → 跳过；
        * 快照不是今天的 → 整轮跳过（手上可能还捏着昨天的数据，绝不能据此写库）。
        """
        snapshot = self.snapshot
        if snapshot is None or not getattr(snapshot, "target_seconds", None):
            return
        today = datetime.now().date()
        if getattr(snapshot, "day", "") and snapshot.day != today.strftime("%Y-%m-%d"):
            return
        try:
            tasks = self.store.cumulative_tasks()
        except Exception:  # noqa: BLE001 - 数据层异常不该打断界面刷新
            log.debug("读取累计打卡任务失败", exc_info=True)
            return
        if not tasks:
            return
        try:
            counts = self.store.checkin_counts()
            checked = self.store.checkin_ids_on(today)
        except Exception:  # noqa: BLE001
            log.debug("读取打卡记录失败", exc_info=True)
            return
        fired = 0
        for task in tasks:
            rule = task.rule
            if not rule.auto_enabled:
                continue
            if task.id in checked:
                continue
            target = max(1, int(rule.target_count or 1))
            if int(counts.get(task.id, 0)) >= target or rule.is_expired(today):
                continue
            found = match_target_seconds(snapshot.target_seconds, rule.auto_target or "")
            if found is None:
                continue
            name, seconds = found
            ready, why = auto_checkin_ready(rule, seconds, checked_today=False, today=today)
            if not ready:
                continue
            minutes = max(1, int(round(seconds / 60)))
            ok, message = self.store.check_in(
                task.id, today, note=f"自动打卡：{name} 今日已用 {minutes} 分钟", source="auto")
            if not ok:
                log.debug("自动打卡未写入（%s）：%s", task.title, message)
                continue
            fired += 1
            log.info("累计打卡：任务「%s」自动打卡成功（%s；%s）", task.title, why, message)
            # 注意用 MainWindow 自己的提示条接口（面板上的 _notify_app 是 TaskPanel 的方法）
            self.show_toast_message(f"已自动打卡：{task.title}（{name} 今日 {minutes} 分钟）")
            try:
                self.notifier.notify("TimeGuard 自动打卡", f"{task.title}\n{message}", timeout=10)
            except Exception:  # noqa: BLE001 - 通知失败不影响打卡本身
                log.debug("自动打卡通知发送失败", exc_info=True)
        if fired:
            # 打卡改变了"下一次提醒"（今天不再提醒）与列表状态，必须刷新
            try:
                self.task_panel.refresh()
            except Exception:  # noqa: BLE001
                log.debug("自动打卡后刷新列表失败", exc_info=True)
            self.reschedule_recurring(reason="auto-check-in")

    def _tick_ui(self) -> None:
        """主线程定时刷新：状态、明细表、待办倒计时、弹窗队列、托盘动作。"""
        if self._shutting_down:
            return
        try:
            self._refresh_status()
            self._refresh_tree()
            # 待办倒计时每秒刷新（只在待办页可见时做，省 CPU）
            if self._tasks_tab_visible():
                self.task_panel.tick()
            self._auto_check_in_pass()
            self.popup_manager.pump()
            self._drain_tray_actions()
        except Exception:  # noqa: BLE001 - 界面刷新异常不应导致程序退出
            log.exception("刷新界面失败")
        finally:
            # 正在退出就不再续约，否则 Tk 销毁后会报
            # "invalid command name ..._tick_ui"（after 回调成了孤儿）。
            # 这里记住 id，退出时显式取消，连"最后一个已排队的回调"也不留。
            if not self._shutting_down:
                self._tick_after_id = self.root.after(self.REFRESH_MS, self._tick_ui)

    def _tasks_tab_visible(self) -> bool:
        """「待办任务」标签页当前是否可见。"""
        try:
            return self.notebook.index("current") == self.notebook_index_tasks
        except tk.TclError:
            return False

    def _refresh_status(self) -> None:
        snapshot = self.snapshot
        if snapshot is None:
            return
        settings = self.config.settings

        self.label_used.configure(
            text=fmt_duration(snapshot.total_seconds, with_seconds=False),
            style="BigOver.TLabel" if snapshot.over_limit else "Big.TLabel",
        )
        self.label_limit.configure(text=f" / {fmt_duration(snapshot.limit_seconds, with_seconds=False)}")
        remaining = snapshot.limit_seconds - snapshot.total_seconds
        self.label_remain.configure(
            text=("已超出 " + fmt_duration(-remaining, with_seconds=False)) if remaining < 0
            else fmt_duration(remaining, with_seconds=False),
            style="StatValue.TLabel" if remaining >= 0 else "StatValue.TLabel",
        )
        self.bar.set_ratio(snapshot.ratio)
        self.label_popups.configure(text=f"　·　{snapshot.popup_count} 次")

        if snapshot.over_limit:
            if snapshot.next_popup_in > 0:
                self.label_next.configure(text=f"{fmt_duration(snapshot.next_popup_in, with_seconds=False)}后")
            else:
                self.label_next.configure(text="即将提醒")
        else:
            self.label_next.configure(text="未超时")

        # 当前状态文字
        if snapshot.monitoring and snapshot.current is not None:
            current = snapshot.current
            self.label_current.configure(
                text=f"▶ 正在计时：{current.name}（{current.kind_cn}）　"
                     f"本次连续 {fmt_duration(snapshot.session_seconds, with_seconds=False)}"
            )
        elif self.engine.paused:
            self.label_current.configure(text="⏸ 计时已暂停（窗口仍在前台，但不再累计）")
        else:
            self.label_current.configure(text="⏳ 当前未检测到被监控的程序 / 网页")

        fg_text = f"前台窗口：{snapshot.foreground_exe or '—'}"
        if snapshot.foreground_title:
            fg_text += f"　|　{snapshot.foreground_title}"
        if snapshot.fullscreen:
            fg_text += "　|　全屏中"
        self.label_foreground.configure(text=fg_text)
        # 顶部状态栏
        mode_cn = "合并计时" if settings.count_mode == "total" else "分别计时"
        self.label_status.configure(
            text=f"{'● 监控中' if snapshot.monitoring else '○ 已暂停'}　·　"
                 f"上限 {settings.daily_limit_minutes} 分钟　·　提醒间隔 {settings.reminder_interval_minutes} 分钟"
                 f"　·　{mode_cn}　·　通知后端 {self.notifier.backend}"
        )
        self.btn_pause.configure(text="▶ 继续计时" if self.engine.paused else "⏸ 暂停计时")

    def _refresh_tree(self) -> None:
        """刷新“今日各对象明细”表格（仅在统计页可见时执行，省 CPU）。"""
        try:
            if self.notebook.index("current") != 0:
                return
        except tk.TclError:
            return
        stats = self.store.today_targets()
        existing = self.tree.get_children()
        signature = tuple((s.name, round(s.seconds, 1)) for s in stats)
        if getattr(self, "_tree_signature", None) == signature:
            return
        self._tree_signature = signature
        for item in existing:
            self.tree.delete(item)
        for stat in stats:
            self.tree.insert(
                "", "end",
                values=(stat.name, "游戏/软件" if stat.kind == "game" else "网页",
                        fmt_duration(stat.seconds, with_seconds=False)),
            )

    def refresh_chart(self) -> None:
        """重绘 7 天柱状图（时长图表 + 任务完成情况图表）。"""
        try:
            self.chart.refresh(days=7, limit_seconds=self.config.settings.daily_limit_seconds)
        except Exception:  # noqa: BLE001
            log.exception("刷新时长图表失败")
        try:
            self.task_panel.chart.refresh(days=7)
        except Exception:  # noqa: BLE001
            log.debug("刷新任务图表失败", exc_info=True)

    # ------------------------------------------------------------------ 待办任务
    def show_tasks_tab(self) -> None:
        """切换到「待办任务」标签页并刷新列表。"""
        try:
            self.notebook.select(self.notebook_index_tasks)
            self.task_panel.refresh()
        except Exception:  # noqa: BLE001
            log.debug("切换待办标签页失败", exc_info=True)

    def startup_check(self) -> None:
        """开机统一检查：**一次**打扰，合并"错过的周期任务"与"未完成的待办"。

        避坑 #1 的落地：
        * 先向调度器**只取不派发**地收集"此刻到点/错过的周期任务"（含开机迟到补发）；
        * 再取未完成的单次任务（含已超期）；
        * 两者合并到 **一个** ``StartupCheckDialog``，而不是各弹一个窗；
        * 弹窗之后再正式派发（写入 ``task_logs.remind_at``），因此关机重开也不会重复补发。

        **重要**：周期任务的补发派发**不依赖** ``task_check_on_start`` ——
        那个开关只控制"要不要弹未完成待办清单"。否则用户一关掉它，
        当天的周期提醒就会既不补发也不标记，直接丢失。
        它也**不受** ``enable_popup`` 影响：关掉弹窗时仍会派发（写标记 + 发系统通知），
        否则"错过的提醒"会变成永远排到明天的死循环。

        只跑一次（``_startup_check_done``），手动触发请用 :meth:`check_tasks_on_start`。
        """
        if self._startup_check_done:
            return
        self._startup_check_done = True
        settings = self.config.settings

        # ---- 1) 错过的周期任务（只收集，不派发）----
        batch = None
        if settings.recurring_reminder_enabled:
            try:
                batch = self.task_scheduler.collect_due()
            except Exception:  # noqa: BLE001
                log.exception("收集错过的周期任务失败")
                batch = None

        # ---- 2) 未完成的单次任务（受"开机检查待办"开关控制）----
        tasks: list = []
        if settings.task_check_on_start:
            try:
                tasks = self.store.pending_tasks()
            except Exception:  # noqa: BLE001
                log.exception("读取待办任务失败")
                tasks = []

        if batch is None and not tasks:
            log.info("开机检查：没有错过的周期任务，也没有未完成的待办")
            # 仍然要重排一次，确保调度器按最新状态工作
            self.reschedule_recurring("startup-empty")
            return

        overdue = sum(1 for task in tasks if task.is_overdue())
        log.info("开机统一检查：错过周期任务 %d 项，未完成待办 %d 条（%d 条已超期）",
                 batch.count if batch else 0, len(tasks), overdue)

        # ---- 3) 系统通知 ----
        # 周期任务：弹窗被关掉时必须发通知，否则"错过的提醒"用户完全感知不到
        if batch is not None and settings.task_reminder_toast:
            try:
                self.notifier.notify("TimeGuard 错过的周期任务", batch.notify_text(),
                                     timeout=15, force=True)
            except Exception:  # noqa: BLE001
                log.debug("发送错过周期任务通知失败", exc_info=True)
        if tasks and settings.task_reminder_toast:
            self._notify_urgent_tasks()

        # ---- 4) 合并成一个弹窗 ----
        # 有周期补发时必须弹（那正是"错过提醒"的兜底），未完成待办清单才受 enable_popup 控制。
        # 另外：这个弹窗**每天只在首次启动软件时**显示一次 —— 同一天反复启动不该反复被打扰
        # （错过的周期任务仍然会在下面第 5 步被派发/记标记，不会因为不弹窗就丢提醒）。
        today = today_str()
        shown_today = str(getattr(settings, "startup_dialog_date", "") or "")[:10] == today
        want_dialog = batch is not None or settings.enable_popup
        if want_dialog and not shown_today:
            try:
                StartupCheckDialog(self.root, tasks, batch, font_family=self.font_family,
                                   on_open_tasks=self.show_tasks_tab)
            except Exception:  # noqa: BLE001
                log.exception("弹出开机提醒窗口失败")
                # 弹窗失败也要派发，避免下次开机重复补发
            else:
                log.info("开机提醒已合并弹出（周期 %d 项 + 待办 %d 条）；今天不再重复弹",
                         batch.count if batch else 0, len(tasks))
                settings.startup_dialog_date = today
                try:
                    self.config.save()
                except Exception:  # noqa: BLE001
                    log.debug("记录开机提醒日期失败", exc_info=True)
        elif want_dialog:
            log.info("开机提醒今天（%s）已经显示过，本次跳过弹窗（每天只在首次启动时弹）", today)
        else:
            log.info("开机检查：弹窗已关闭，改为静默派发（周期 %d 项）",
                     batch.count if batch else 0)

        # ---- 5) 正式派发 + 记录 ----
        if batch is not None:
            try:
                # 内容已经并进上面的「开机提醒」弹窗，这里只标记"已提醒"、不再回调，
                # 否则会多弹一个周期任务窗口 —— 那又变成两次打扰了。
                self.task_scheduler.dispatch(batch, silent=True)
            except Exception:  # noqa: BLE001
                log.exception("派发错过的周期任务失败")
        if tasks:
            try:
                self.store.mark_tasks_reminded([task.id for task in tasks])
            except Exception:  # noqa: BLE001
                log.debug("记录提醒次数失败", exc_info=True)
        try:
            self._update_tray_tooltip()
            self.task_panel.refresh()
        except Exception:  # noqa: BLE001
            log.debug("刷新界面失败", exc_info=True)

        # 弹窗之后重排一次调度器（此时错过的已经标记过，会直接排到下一次）
        self.reschedule_recurring("startup-done")

    def check_tasks_on_start(self, include_recurring: bool = True) -> None:
        """待办检查（托盘菜单 / 设置页「待办提醒预览」手动调用）。

        :param include_recurring: 是否连"错过的周期任务"一起展示（默认是）。
            开机时走 :meth:`startup_check`；这里是手动触发路径，允许直接调用。
        """
        settings = self.config.settings
        batch = None
        if include_recurring and settings.recurring_reminder_enabled:
            try:
                batch = self.task_scheduler.collect_due()
            except Exception:  # noqa: BLE001
                log.debug("收集周期任务失败", exc_info=True)
                batch = None

        try:
            tasks = self.store.pending_tasks()
        except Exception:  # noqa: BLE001
            log.exception("读取待办任务失败")
            return

        if batch is None and not tasks:
            self.show_toast_message("当前没有待办任务，也没有错过的周期任务")
            return

        if settings.task_reminder_toast:
            if batch is not None:
                try:
                    self.notifier.notify("TimeGuard 错过的周期任务", batch.notify_text(),
                                         timeout=15, force=True)
                except Exception:  # noqa: BLE001
                    log.debug("发送周期任务通知失败", exc_info=True)
            if tasks:
                self._notify_urgent_tasks()

        try:
            StartupCheckDialog(self.root, tasks, batch, font_family=self.font_family,
                               on_open_tasks=self.show_tasks_tab)
        except Exception:  # noqa: BLE001
            log.exception("弹出提醒窗口失败")
            return

        if batch is not None:
            # 静默派发：内容已经在上面那个弹窗里列过了，
            # 非静默会再弹一个周期任务窗 + 再发一条通知（那就是两次打扰）
            self.task_scheduler.dispatch(batch, silent=True)
        if tasks:
            try:
                self.store.mark_tasks_reminded([task.id for task in tasks])
            except Exception:  # noqa: BLE001
                log.debug("记录提醒次数失败", exc_info=True)
        self._update_tray_tooltip()

    def _notify_urgent_tasks(self) -> None:
        """给“临近截止”的任务发一条系统通知（默认提前 24 小时，间隔 6 小时）。

        超期任务会标注“已超期 X”；通知只发一次，包含最多 3 条任务摘要，
        发完后写入 ``notified_at``，由数据库负责间隔判断。
        """
        settings = self.config.settings
        try:
            urgent = self.store.tasks_due_for_notification(
                settings.task_notify_within_hours, settings.task_notify_interval_hours
            )
        except Exception:  # noqa: BLE001
            log.exception("筛选待通知任务失败")
            return
        if not urgent:
            log.info("系统通知：没有临近截止的任务（提前 %.0f 小时内），跳过",
                     settings.task_notify_within_hours)
            return

        now = datetime.now()
        shown = [task for task in urgent if task.due_at is not None][:3]
        lines = [f"你有 {len(urgent)} 条任务临近截止："]
        for task in shown:
            remain = (task.due_at - now).total_seconds()  # type: ignore[operator]
            if remain < 0:
                lines.append(f"· {task.title}（已超期 {fmt_duration(-remain, with_seconds=False)}）")
            else:
                lines.append(f"· {task.title}（还剩 {fmt_duration(remain, with_seconds=False)}）")
        if len(urgent) > len(shown):
            lines.append(f"…… 还有 {len(urgent) - len(shown)} 条")
        lines.append(phrases.pick())

        title = "TimeGuard 待办提醒"
        try:
            ok = self.notifier.notify(title, "\n".join(lines), timeout=15, force=True)
        except Exception:  # noqa: BLE001
            log.debug("发送待办系统通知失败", exc_info=True)
            return
        if ok:
            # 只标记“真正出现在通知里”的任务：否则第 4 条以后的任务会被白白静默 6 小时
            self.store.mark_tasks_notified([task.id for task in shown],
                                           settings.task_notify_within_hours)
            log.info("系统通知：已提醒 %d 条临近截止任务（提前 %.0f 小时 / 间隔 %.0f 小时）%s",
                     len(shown), settings.task_notify_within_hours,
                     settings.task_notify_interval_hours,
                     f"，另有 {len(urgent) - len(shown)} 条下次再提醒" if len(urgent) > len(shown) else "")

    def show_task_reminder_again(self) -> None:
        """手动再查看一次待办提醒（托盘菜单 / 设置页按钮调用）。"""
        self.check_tasks_on_start()

    # ------------------------------------------------------------------ 周期任务提醒
    def start_task_scheduler(self) -> None:
        """启动周期任务调度器（界面出现后调用一次）。

        用 ``deferred`` 只排程、不派发：错过的周期任务留给
        :meth:`startup_check` 与未完成待办合并成一个弹窗。
        """
        try:
            self.task_scheduler.start(deferred=True)
            log.info("周期任务调度器已启动（延后派发，等开机统一检查）：%s",
                     self.task_scheduler.status_text())
        except Exception:  # noqa: BLE001
            log.exception("启动周期任务调度器失败")

    def reschedule_recurring(self, reason: str = "tasks-changed") -> None:
        """任务被新增/修改/删除后重排调度（幂等，可放心多次调用）。"""
        scheduler = getattr(self, "task_scheduler", None)
        if scheduler is None:
            return
        try:
            scheduler.reschedule(reason=reason)
        except Exception:  # noqa: BLE001
            log.debug("重排周期任务调度失败", exc_info=True)

    def _on_recurring_batch(self, batch) -> None:
        """调度器派发一批周期任务提醒（同一时刻的多个任务已合并成一批）。"""
        settings = self.config.settings
        log.info("周期任务提醒：%d 项（%s）", batch.count, "、".join(batch.titles))

        # ---- 系统通知：与软件弹窗并存（这是"到点提醒"，不是重复的辅助弹窗）----
        if settings.task_reminder_toast:
            try:
                self.notifier.notify("TimeGuard 任务即将开始", batch.notify_text(),
                                     timeout=15, force=True)
            except Exception:  # noqa: BLE001
                log.debug("发送周期任务系统通知失败", exc_info=True)

        # ---- 软件弹窗：合并成一个窗口列出全部到点任务 ----
        if not settings.enable_popup:
            self.show_toast_message(batch.headline(), AMBER)
            return
        try:
            RecurringReminderDialog(self.root, batch, font_family=self.font_family,
                                    on_open_tasks=self.show_tasks_tab,
                                    on_check_in=self.check_in_task)
        except Exception:  # noqa: BLE001
            log.exception("弹出周期任务提醒窗口失败")
            return

        try:
            self._update_tray_tooltip()
            self.task_panel.refresh()
        except Exception:  # noqa: BLE001
            log.debug("刷新待办列表失败", exc_info=True)

    def check_in_task(self, task_id: int) -> tuple[bool, str]:
        """给累计打卡任务打一次卡（弹窗按钮与待办列表按钮共用这一条路径）。

        返回 ``(是否成功, 给用户看的一句话)`` —— 数据层已经把各种失败原因
        （不是打卡任务 / 今天已经打过 / 已过截止日）写成中文，这里原样透传。
        """
        try:
            ok, message = self.store.check_in(int(task_id))
        except Exception as exc:  # noqa: BLE001
            log.exception("打卡失败（任务 #%s）", task_id)
            return False, f"打卡失败：{exc}"
        if ok:
            log.info("累计打卡：任务 #%s 打卡成功（%s）", task_id, message)
        try:
            self.task_panel.refresh()
            self._update_tray_tooltip()
        except Exception:  # noqa: BLE001
            log.debug("打卡后刷新界面失败", exc_info=True)
        # 打卡会改变"下一次提醒"（今天不再提醒），所以必须重排调度器
        self.reschedule_recurring(reason="check-in")
        return ok, message

    def undo_check_in_task(self, task_id: int, day=None) -> tuple[bool, str]:
        """撤销某一天的打卡（点错了用）。"""
        try:
            removed = self.store.undo_check_in(int(task_id), day)
        except Exception as exc:  # noqa: BLE001
            log.exception("撤销打卡失败（任务 #%s）", task_id)
            return False, f"撤销失败：{exc}"
        if not removed:
            return False, "那一天没有打卡记录"
        self.task_panel.refresh()
        self.reschedule_recurring(reason="undo-check-in")
        return True, "已撤销该次打卡"

    def test_recurring_reminder(self) -> None:
        """设置页「测试周期提醒」按钮：立刻派发当前到点的那批（没有则提示）。

        注意：这**会**写入 ``task_logs.remind_at``，也就是把"这一次提醒"消费掉
        （真提醒此后不会再弹）。这是有意的 —— 否则测试按钮就得再造一条假数据。
        按钮文案已写明这一点。
        """
        if not bool(getattr(self.config.settings, "recurring_reminder_enabled", True)):
            self.show_toast_message("周期任务提醒已关闭，先开启再测试", AMBER)
            return
        batch = None
        try:
            batch = self.task_scheduler.fire_now()
        except Exception:  # noqa: BLE001
            log.exception("测试周期提醒失败")
        if batch is None:
            self.show_toast_message("当前没有到点的周期任务提醒", AMBER)
            return
        self.show_toast_message(f"已发出 {batch.count} 项周期任务提醒（本次提醒已标记为已提醒）")

    # ------------------------------------------------------------------ 引擎交互
    def _on_limit_exceeded(self, target, used: float, title: str, message: str, detail: str) -> None:
        """引擎通知“超时提醒”（运行在监控线程）——投递到主线程弹窗。"""
        cooldown = float(self.config.settings.popup_cooldown_seconds)
        self.popup_manager.request(title, message, detail, cooldown=cooldown)

    def toggle_pause(self) -> None:
        """暂停 / 继续计时。"""
        paused = self.engine.toggle_pause()
        self.show_toast_message("已暂停计时" if paused else "已继续计时")

    def test_reminder(self) -> None:
        """手动触发一次提醒，便于确认通知与弹窗效果。"""
        s = self.config.settings
        title, message, detail = notifier_mod.reminder_text(
            "测试程序", 5400, s.daily_limit_seconds, "这是一条测试提醒"
        )
        if s.enable_toast:
            self.notifier.notify(title, message, force=True)
        if s.enable_popup:
            self.popup_manager.skip()
            self.popup_manager.request(title, message, detail, cooldown=0)
        self.show_toast_message("已发送测试提醒")

    def reset_today(self) -> None:
        """清零今日统计。"""
        if not messagebox.askyesno("确认", f"确定要清零 [{today_str()}] 的使用记录吗？", parent=self.root):
            return
        self.engine.reset_today()
        self._tree_signature = None
        self.refresh_chart()
        self.show_toast_message("今日统计已清零")

    def export_csv(self) -> None:
        """导出最近 30 天数据到 CSV。"""
        target = paths.data_dir() / f"TimeGuard导出_{today_str()}.csv"
        try:
            rows = self.store.export_csv(target, days=30)
        except OSError as exc:
            messagebox.showerror("导出失败", f"无法写入文件：{exc}", parent=self.root)
            return
        messagebox.showinfo("导出完成", f"已导出 {rows} 条记录到：\n{target}", parent=self.root)
        self.show_toast_message(f"已导出到 {target.name}")

    def open_data_dir(self) -> None:
        """在资源管理器中打开数据目录。"""
        import subprocess

        directory = paths.data_dir()
        try:
            subprocess.Popen(["explorer", str(directory)])
        except OSError:
            messagebox.showinfo("数据目录", str(directory), parent=self.root)

    def show_toast_message(self, text: str, color: str = GREEN) -> None:
        """在底部提示条显示一条短消息（3 秒后自动清除）。"""
        self.label_toast.configure(text=f"✔ {text}", fg=color)
        if self._toast_after_id:
            try:
                self.root.after_cancel(self._toast_after_id)
            except tk.TclError:
                pass
        self._toast_after_id = self.root.after(3000, lambda: self.label_toast.configure(text=""))

    def set_dirty(self, dirty: bool) -> None:
        """标记设置是否有未保存修改。"""
        self._dirty = dirty
        suffix = " *（设置有未保存修改）" if dirty else ""
        self.root.title(f"TimeGuard 时间管家 · 今日 {today_str()}{suffix}")

    def reload_settings_ui(self) -> None:
        """设置被外部改动后，让设置页重新从配置同步显示。"""
        self.settings_panel.reload_from_settings()
        self.set_dirty(False)

    # ------------------------------------------------------------------ 托盘
    def _setup_tray(self) -> None:
        """创建系统托盘图标（pystray 不可用时静默降级）。"""
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception as exc:  # noqa: BLE001
            log.warning("系统托盘不可用（缺少 pystray/Pillow）: %s", exc)
            self.show_toast_message("未安装 pystray/Pillow，托盘功能不可用", AMBER)
            return

        image = self._load_tray_image(Image, ImageDraw)
        menu = pystray.Menu(
            pystray.MenuItem("显示主窗口", lambda: self._tray_actions.put("show"), default=True),
            pystray.MenuItem("暂停 / 继续计时", lambda: self._tray_actions.put("pause")),
            pystray.MenuItem("立刻提醒一次", lambda: self._tray_actions.put("test")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("查看待办任务", lambda: self._tray_actions.put("tasks")),
            pystray.MenuItem("待办提醒", lambda: self._tray_actions.put("task_remind")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出程序", lambda: self._tray_actions.put("quit")),
        )
        self._tray_icon = pystray.Icon("TimeGuard", image, "TimeGuard 时间管家", menu)
        self._tray_thread = None
        try:
            self._tray_icon.run_detached()
            log.info("系统托盘图标已启动")
        except Exception as exc:  # noqa: BLE001
            log.warning("启动托盘失败: %s", exc)
            self._tray_icon = None

    def _load_tray_image(self, Image, ImageDraw):
        """加载托盘图标：优先用 resources 里的图标，否则运行时绘制一个。"""
        for candidate in (paths.icon_file(), paths.png_icon_file()):
            if candidate.exists():
                try:
                    return Image.open(candidate)
                except Exception:  # noqa: BLE001
                    log.debug("读取托盘图标失败: %s", candidate, exc_info=True)
        # 运行时绘制：蓝色圆角方块 + 白色沙漏/时钟指针
        size = 64
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle([4, 4, size - 4, size - 4], radius=16, fill=(79, 140, 255, 255))
        draw.ellipse([16, 16, size - 16, size - 16], outline=(255, 255, 255, 255), width=5)
        draw.line([size // 2, size // 2, size // 2, 22], fill=(255, 255, 255, 255), width=5)
        draw.line([size // 2, size // 2, size // 2 + 12, size // 2 + 6], fill=(255, 255, 255, 255), width=5)
        return image

    def _drain_tray_actions(self) -> None:
        """在主线程处理托盘菜单动作。"""
        while True:
            try:
                action = self._tray_actions.get_nowait()
            except queue.Empty:
                return
            if action == "show":
                self.show_window()
            elif action == "pause":
                self.toggle_pause()
            elif action == "test":
                self.test_reminder()
            elif action == "tasks":
                self.show_window()
                self.show_tasks_tab()
            elif action == "task_remind":
                self.show_task_reminder_again()
            elif action == "quit":
                self.quit_app()

    def _update_tray_tooltip(self) -> None:
        """刷新托盘悬浮提示（含今日时长与待办数量）。"""
        if self._tray_icon is None:
            return
        snapshot = self.snapshot
        used = fmt_duration(snapshot.total_seconds, with_seconds=False) if snapshot else "0秒"
        state = "监控中" if (snapshot and snapshot.monitoring) else "已暂停"
        try:
            counts = self.store.task_counts()
            task_info = f"　·　待办 {counts.pending}"
            if counts.overdue:
                task_info += f"（超期 {counts.overdue}）"
        except Exception:  # noqa: BLE001
            task_info = ""
        try:
            self._tray_icon.title = f"TimeGuard · 今日已用 {used}（{state}）{task_info}"
        except Exception:  # noqa: BLE001
            pass

    def hide_to_tray(self, manual: bool = False) -> None:
        """隐藏窗口到系统托盘（不占任务栏）。

        :param manual: 是否是用户**主动**点了「最小化到托盘」（或任务栏最小化按钮）。
            只有这种情况才发"已在后台运行"的通知 —— 启动时就最小化
            （``start_minimized``）或开机自启不该在用户没操作时弹东西。
        """
        if self._hidden:
            return
        if self._tray_icon is None:
            # 没有托盘图标就藏窗口 = 用户再也找不回界面（只能去任务管理器结束进程）。
            # 这种情况（缺 pystray/Pillow，或托盘启动失败）宁可保持窗口可见。
            self.show_toast_message("系统托盘不可用，窗口保持显示", AMBER)
            return
        self._hidden = True
        try:
            # 先移出屏幕再取消边框，避免任务栏残留图标
            self.root.geometry("+10000+10000")
            self.root.update_idletasks()
            self.root.overrideredirect(True)
            self.root.withdraw()
        except tk.TclError:
            pass
        if manual and not bool(getattr(self.config.settings, "tray_notice_shown", False)):
            # 只提示一次：之后用户再收起窗口就不弹了（提示文案里把这件事讲清楚，
            # 免得用户以为程序不见了）。托盘图标的悬浮提示仍然一直在。
            self.config.settings.tray_notice_shown = True
            try:
                self.config.save()
            except Exception:  # noqa: BLE001
                log.debug("记录托盘提示状态失败", exc_info=True)
            self.notifier.notify(
                "TimeGuard 已在后台运行",
                "已最小化到系统托盘，继续为你统计游戏与网页使用时长。\n"
                "以后最小化到托盘不再自动提示（点托盘图标可随时打开窗口）。",
                timeout=8, force=True,
            )

    def show_window(self) -> None:
        """从托盘恢复主窗口。

        ``hide_to_tray`` 为了不残留任务栏图标，会把窗口先移到 ``+10000+10000``
        （屏幕外）再取消边框、withdraw。所以恢复时必须**把窗口挪回屏幕内**，
        这一步失败的话用户点托盘图标将什么都看不到 —— 因此这里不能再静默吞异常。
        """
        self._hidden = False
        try:
            self.root.overrideredirect(False)
            self.root.deiconify()
            self.root.state("normal")
            # 关键：等窗口真正映射完成再改 geometry。
            # 刚从 withdraw/屏幕外恢复时，紧跟 deiconify 发的 geometry 请求会被丢掉，
            # 窗口就一直停在 +10000（用户点托盘图标什么都看不到）。
            self.root.update_idletasks()
        except tk.TclError:
            log.warning("取消窗口隐藏状态失败", exc_info=True)
        try:
            self._setup_window()   # 重新居中并恢复尺寸
        except tk.TclError:
            log.warning("恢复窗口尺寸失败", exc_info=True)
        self.ensure_on_screen()
        try:
            self.root.lift()
            self.root.focus_force()
            self.root.attributes("-topmost", True)
            self.root.after(400, lambda: self.root.attributes("-topmost", False))
        except tk.TclError:
            log.debug("恢复窗口前置失败", exc_info=True)

    def ensure_on_screen(self) -> None:
        """确保窗口落在屏幕可见区域内；不在就重新居中。

        为什么需要兜底：``hide_to_tray`` 会把窗口挪到 ``+10000+10000``，
        而这一步"挪回来"依赖 Tk 的 geometry 请求真正生效 —— 实测在
        "先移出屏幕 → ``overrideredirect(True)`` → ``withdraw()``"之后，
        恢复时的 geometry 有概率不生效，窗口就一直停在屏幕外（用户什么都看不到）。
        这里用**可见性检测**兜底，比猜某个调用顺序更可靠。
        """
        try:
            self.root.update_idletasks()
            work_w, work_h = winapi.get_work_area(self.root)
            x, y = self.root.winfo_x(), self.root.winfo_y()
        except tk.TclError:
            return
        if 0 <= x <= work_w - 80 and 0 <= y <= work_h - 80:
            return
        log.warning("主窗口停在屏幕外 (%d, %d)，重新居中", x, y)
        try:
            self.apply_window_size(self.config.settings.window_width,
                                   self.config.settings.window_height, center=True)
            self.root.update_idletasks()
        except tk.TclError:
            log.warning("重新居中窗口失败", exc_info=True)

    def request_close(self) -> None:
        """标题栏叉号：**让用户自己选**是退出，还是收进托盘继续后台运行。

        为什么不像很多托盘软件那样"叉号 = 最小化"：那样用户以为关掉了、
        其实还在后台统计，属于**界面在说谎**；而"叉号 = 一律退出"又会把习惯
        用叉号收进托盘的用户不小心关掉监控。所以这里问一次，并把两个选项的
        后果直接写在按钮旁边。
        """
        try:
            action = CloseChoiceDialog(self.root, font_family=self.font_family).show()
        except Exception:  # noqa: BLE001 - 弹不出来也不能卡住关闭流程
            log.exception("弹出关闭方式选择框失败，按『退出』处理")
            action = "exit"
        if action == "exit":
            self.quit_app(confirm=False)
        elif action == "tray":
            self.hide_to_tray(manual=True)
        else:
            log.info("用户取消了关闭操作")

    # ------------------------------------------------------------------ 退出
    def quit_app(self, confirm: bool = True) -> None:
        """退出程序：落库、停引擎、关托盘、销毁窗口。

        :param confirm: 是否先弹确认框。叉号那条路径已经问过一次了，
            这里传 ``False``，避免连问两次。
        """
        # 在托盘里退出时没有可见窗口，先把窗口显示出来，否则对话框没有父窗口会跑到后面
        hidden = self._hidden
        if confirm:
            if hidden:
                self.show_window()
            confirmed = messagebox.askyesno(
                "确认退出",
                "确定要退出 TimeGuard 吗？\n退出后将不再统计使用时长。",
                parent=self.root,
            )
            if not confirmed:
                if hidden:
                    self.hide_to_tray()
                return

        log.info("开始退出程序")
        self._shutting_down = True      # 先停掉所有 after 循环（含界面刷新）
        self._cancel_periodic_callbacks()
        # 通知要在拆掉托盘与窗口**之前**发：它是"我确实退出了"的唯一凭证
        self._notify_exit()
        try:
            self.engine.stop()
        except Exception:  # noqa: BLE001
            log.exception("停止引擎失败")
        try:
            self.task_scheduler.stop()
        except Exception:  # noqa: BLE001
            log.debug("停止任务调度器失败", exc_info=True)
        self._stop_tray()
        try:
            self.store.close()
        except Exception:  # noqa: BLE001
            pass
        # 让系统通知有机会真正弹出来再销毁窗口：plyer 是在自己的线程里调用
        # Shell_NotifyIcon 的，进程立刻结束的话气泡可能来不及出现。
        try:
            self.root.after(600, self._destroy_root)
            return
        except tk.TclError:
            pass
        self._destroy_root()

    def _cancel_periodic_callbacks(self) -> None:
        """取消所有已排队的周期回调（退出时调用）。

        为什么必须显式取消：`after` 注册在具体控件上，控件被销毁后定时器仍然存在，
        Tk 会去调用一个已经不存在的命令，于是往 stderr 刷
        "invalid command name ..._tick_ui" —— 只是噪音，但看着像出错了。
        """
        for attr in ("_tick_after_id", "_chart_after_id", "_tray_after_id"):
            after_id = getattr(self, attr, None)
            if not after_id:
                continue
            try:
                self.root.after_cancel(after_id)
            except tk.TclError:
                pass
            setattr(self, attr, None)

    def _destroy_root(self) -> None:
        """结束 Tk 主循环（退出流程的最后一步）。"""
        try:
            self.root.quit()
        except tk.TclError:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _notify_exit(self) -> None:
        """退出时发一条系统通知。

        用**系统通知**而不是界面里的提示条：窗口马上就要销毁了，
        界面内的提示条根本来不及被看见。
        """
        try:
            self.notifier.notify(
                "TimeGuard 软件已退出运行",
                "已停止统计游戏与网页使用时长；下次启动（或开机自启）会继续。",
                timeout=6, force=True,
            )
        except Exception:  # noqa: BLE001
            log.debug("发送退出通知失败", exc_info=True)

    def _stop_tray(self) -> None:
        """关闭托盘图标。"""
        icon = self._tray_icon
        self._tray_icon = None
        if icon is None:
            return
        try:
            icon.visible = False
            icon.stop()
        except Exception:  # noqa: BLE001
            log.debug("关闭托盘失败", exc_info=True)

    # ------------------------------------------------------------------ 主循环
    def run(self) -> None:
        """进入 tkinter 主循环，并启动周期任务。"""
        self._tray_after_id = self.root.after(self.TRAY_REFRESH_MS, self._tray_tick)
        self._chart_after_id = self.root.after(self.CHART_REFRESH_MS, self._chart_tick)
        try:
            self.root.mainloop()
        finally:
            # 主循环退出（含被 destroy / 异常打断）时标记停机，
            # 让所有 after 循环停止续约，并把已排队的那次也取消掉，
            # 避免出现孤儿回调报错
            self._shutting_down = True
            self._cancel_periodic_callbacks()
            self._stop_tray()

    def _tray_tick(self) -> None:
        """周期刷新托盘提示 + 清理过期明细。"""
        if self._shutting_down:
            return
        self._update_tray_tooltip()
        self._tray_after_id = self.root.after(self.TRAY_REFRESH_MS, self._tray_tick)

    def _chart_tick(self) -> None:
        """周期刷新图表与清理历史数据。"""
        if self._shutting_down:
            return
        try:
            if self.notebook.index("current") == 0:
                self.refresh_chart()
            self.store.purge_old()
        except Exception:  # noqa: BLE001
            log.debug("周期任务异常", exc_info=True)
        self._chart_after_id = self.root.after(self.CHART_REFRESH_MS, self._chart_tick)


# ---------------------------------------------------------------- 可滚动容器
class VerticalScrolledFrame(ttk.Frame):
    """带垂直滚动条的容器（设置页内容较多时需要）。

    通过 ``interior`` 属性访问真正的内部 Frame，把控件放进去即可。

    性能提示：设置页控件很多，一次 ``yview_scroll`` 的重绘代价实测约 20ms
    （≈50fps）。若每个滚轮事件都立刻滚动，快速上下滑动时事件会堆积、画布来不及
    重绘，屏幕上就会出现**文字残影**。因此这里把连续滚轮事件累积起来，
    **一帧只滚动一次**（见 :meth:`_flush_scroll`）。
    """

    #: 用 after_idle 合并滚轮事件：一轮事件循环里到达的所有滚轮消息，
    #: 会在**渲染这一帧之前**合并成一次滚动，因此既减少重绘、又不会延迟画面。
    #: （曾经用 after(10ms) 延迟滚动，实测每两步就有一步画面完全没更新 —— 那就是残影。）
    SCROLL_BATCH_MS = 0

    def __init__(self, master: tk.Misc, bg: str = BG, **kwargs) -> None:
        super().__init__(master, style="TFrame", **kwargs)
        canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        self.interior = ttk.Frame(canvas, style="TFrame")
        window = canvas.create_window((0, 0), window=self.interior, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def _on_configure(_event: tk.Event) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_resize(event: tk.Event) -> None:
            canvas.itemconfigure(window, width=event.width)

        self.interior.bind("<Configure>", _on_configure)
        canvas.bind("<Configure>", _on_canvas_resize)

        # 鼠标滚轮支持（Windows）：只绑定到画布，避免在列表里滚动时连带整页滚动。
        # 事件累积到 _pending_units，由 _flush_scroll 合并执行（防残影）。
        self._scroll_after_id: str | None = None
        self._pending_units = 0

        def _on_mousewheel(event: tk.Event) -> None:
            # Windows 的 delta 是 ±120 的倍数；部分鼠标/触控板会更细
            delta = int(getattr(event, "delta", 0) or 0)
            units = int(-delta / 120)
            if units == 0:
                units = -1 if delta > 0 else 1
            self.queue_scroll(units)

        canvas.bind("<MouseWheel>", _on_mousewheel)
        self._canvas = canvas

    # ------------------------------------------------------------------ 滚动
    def queue_scroll(self, units: int) -> None:
        """累积滚动量，并安排"本轮事件处理完后立刻"执行一次合并滚动。

        用 ``after_idle`` 而不是 ``after(ms)``：idle 回调在本轮所有待处理事件
        （包括连续到达的滚轮消息）处理完之后、**绘制这一帧之前**运行，
        于是既不延迟画面（无残影），又把整批滚轮合并成一次重绘。
        """
        self._pending_units += int(units)
        if self._scroll_after_id is None:
            try:
                self._scroll_after_id = self.after_idle(self._flush_scroll)
            except tk.TclError:
                self._flush_scroll()

    def _flush_scroll(self) -> None:
        """真正执行累积的滚动量（一次重绘完成整批滚动）。"""
        self._scroll_after_id = None
        units, self._pending_units = self._pending_units, 0
        if not units:
            return
        try:
            # xview_scroll 在 Tk 9 被移除，yview_scroll 两种版本都有
            self._canvas.yview_scroll(units, "units")
        except tk.TclError:
            pass

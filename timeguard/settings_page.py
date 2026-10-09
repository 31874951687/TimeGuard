"""数据驱动的设置页（方案 2 终极形态：表格自绘 + 就地编辑 + 详情面板）。

为什么是现在这个形态
--------------------
旧设置页把上百个 ttk 控件塞进 ``Canvas + create_window``，滚动时整块重绘：
实测**一次滚动重绘 128~155ms（≈8fps）**，快速上下滑动必然出现文字残影。
换滚动机制（``place`` 平移）后仍是 123ms/帧 —— 说明瓶颈是
**"每帧要重绘上百个控件"本身**，不是滚动方式。

因此这里彻底不往滚动区里放控件：

* **表格自绘**：全部设置（分组 / 名称 / 当前值 / 说明）都是 ``ttk.Treeview`` 的
  文字行，由原生控件渲染。滚动 = 画文字，没有子控件要重绘。
* **就地编辑**：点某项的值，在该单元格上浮出一个 ``ttk.Entry`` / ``Spinbox``，
  回车提交、Esc 取消、失焦提交。**同一时刻最多只有一个编辑控件**。
* **详情面板**：右侧显示选中项的说明与按钮，列表类（进程名 / 关键字）与
  窗口大小这类"需要多个控件"的设置，编辑器放在右侧 —— 仍然只有个位数控件。

对后续扩展的意义
----------------
新增设置只需在 :data:`SettingsPage._build_schema` 里加一行
（``kind`` 决定用什么控件、是否支持就地编辑），表格、分组、搜索、
就地编辑、保存、恢复默认全部自动生效。
"""

from __future__ import annotations

import copy
import logging
import tkinter as tk
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from tkinter import messagebox, ttk

from . import autostart, winapi
from .config import Config
from .paths import APP_NAME
from .settings_preview import (
    PRESET_FULLSCREEN,
    WindowSizePreview,
    match_preset,
    preset_names,
)
from .utils import today_str

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

#: 就地编辑支持的类型
INLINE_KINDS = ("int", "float", "text", "choice", "bool")

#: 就地编辑时布尔项的两个选项（与表格里「✔ 已开启 / ✘ 已关闭」同一套说法）
BOOL_CHOICES = ("开启", "关闭")

#: 三态布尔在表格里的显示
BOOL_TEXT = {True: "✔ 已开启", False: "✘ 已关闭"}


class Row(Protocol):
    """一行编辑器的统一接口。"""

    frame: tk.Misc

    def get(self) -> Any: ...

    def set(self, value: Any) -> None: ...


@dataclass
class Setting:
    """一条设置的元数据：表格里显示什么、用什么控件编辑、取值范围。"""

    key: str
    label: str
    group: str
    kind: str                      # bool / int / float / text / choice / radio / list / window / action
    hint: str = ""
    low: float = 0.0               # 数值下界
    high: float = 0.0              # 数值上界
    step: float = 1.0              # 数值步进
    suffix: str = ""               # 数值单位
    choices: tuple[str, ...] = ()  # choice / radio 的取值
    extra: str = ""                # 补充说明（表格"说明"列显示 hint，这里放更细的注释）
    keywords: str = ""             # 供搜索

    @property
    def editable_inline(self) -> bool:
        """能否在表格单元格上就地编辑。"""
        return self.kind in INLINE_KINDS


# ================================================================ 通用编辑器
def _value_label(parent: tk.Misc, text: str) -> ttk.Label:
    return ttk.Label(parent, text=text, style="Card.TLabel")


def make_bool_row(parent: tk.Misc, get: Callable[[], bool], set_: Callable[[bool], None],
                  font: tuple) -> Row:
    """复选框行。"""
    var = tk.BooleanVar(value=bool(get()))

    class _Row:
        frame = ttk.Frame(parent, style="Card.TFrame")

        def get(self) -> bool:
            return bool(var.get())

        def set(self, value: bool) -> None:
            var.set(bool(value))

    _Row.frame.pack(fill="x")
    ttk.Checkbutton(_Row.frame, text="启用", variable=var, style="Card.TCheckbutton").pack(anchor="w")
    return _Row()


def make_number_row(parent: tk.Misc, get: Callable[[], float], set_: Callable[[float], None],
                    low: float, high: float, step: float, suffix: str, as_int: bool,
                    font: tuple) -> Row:
    """数值行（Spinbox + 单位后缀）。"""
    var = tk.DoubleVar(value=float(get()))

    class _Row:
        frame = ttk.Frame(parent, style="Card.TFrame")

        def get(self) -> float:
            try:
                value = float(var.get())
            except (tk.TclError, ValueError):
                return float(low)
            value = max(low, min(high, value))
            return float(int(round(value))) if as_int else round(value, 2)

        def set(self, value: float) -> None:
            var.set(int(value) if as_int else float(value))

    _Row.frame.pack(fill="x")
    ttk.Spinbox(_Row.frame, from_=low, to=high, increment=step, width=7, textvariable=var,
                font=font, style="Card.TSpinbox").pack(side="left")
    if suffix:
        _value_label(_Row.frame, suffix).pack(side="left", padx=(6, 0))
    return _Row()


def make_text_row(parent: tk.Misc, get: Callable[[], str], set_: Callable[[str], None],
                  font: tuple, width: int = 34) -> Row:
    """单行文本输入。"""
    var = tk.StringVar(value=str(get()))

    class _Row:
        frame = ttk.Frame(parent, style="Card.TFrame")

        def get(self) -> str:
            return var.get().strip()

        def set(self, value: str) -> None:
            var.set(str(value))

    _Row.frame.pack(fill="x")
    ttk.Entry(_Row.frame, textvariable=var, width=width, font=font).pack(side="left", fill="x", expand=True)
    return _Row()


def make_choice_row(parent: tk.Misc, get: Callable[[], str], set_: Callable[[str], None],
                    values: list[str], font: tuple) -> Row:
    """下拉选择行。"""
    var = tk.StringVar(value=str(get()))

    class _Row:
        frame = ttk.Frame(parent, style="Card.TFrame")

        def get(self) -> str:
            return var.get()

        def set(self, value: str) -> None:
            var.set(str(value))

    _Row.frame.pack(fill="x")
    combo = ttk.Combobox(_Row.frame, values=values, textvariable=var, state="readonly",
                         width=22, font=font, style="Dark.TCombobox")
    combo.pack(side="left")
    return _Row()


def make_radio_row(parent: tk.Misc, get: Callable[[], str], set_: Callable[[str], None],
                   options: list[tuple[str, str]], font: tuple) -> Row:
    """单选行（value, 文案）。"""
    var = tk.StringVar(value=str(get()))

    class _Row:
        frame = ttk.Frame(parent, style="Card.TFrame")

        def get(self) -> str:
            return var.get()

        def set(self, value: str) -> None:
            var.set(str(value))

    _Row.frame.pack(fill="x")
    for value, text in options:
        ttk.Radiobutton(_Row.frame, text=text, value=value, variable=var,
                        style="Card.TRadiobutton").pack(side="left", padx=(0, 10))
    return _Row()


def make_list_row(parent: tk.Misc, get: Callable[[], list[str]], set_: Callable[[list[str]], None],
                  font: tuple, font_small: tuple, height: int = 6, placeholder: str = "") -> Row:
    """列表编辑行：内置可滚动列表 + 输入框 + 添加/删除。"""
    items: list[str] = list(get())

    class _Row:
        frame = ttk.Frame(parent, style="Card.TFrame")

        def get(self) -> list[str]:
            return list(items)

        def set(self, value: list[str]) -> None:
            items[:] = [str(v).strip() for v in (value or []) if str(v).strip()]
            _fill()

    def _fill() -> None:
        box.delete(0, "end")
        for value in items:
            box.insert("end", value)
        count_var.set(f"共 {len(items)} 项")

    row = _Row()
    holder = tk.Frame(row.frame, bg=PANEL_ALT)
    holder.pack(fill="both", expand=True)
    box = tk.Listbox(holder, height=height, activestyle="none", bg=PANEL_ALT, fg=FG,
                     selectbackground=ACCENT, selectforeground="#ffffff",
                     highlightthickness=0, bd=0, font=font)
    bar = ttk.Scrollbar(holder, orient="vertical", command=box.yview,
                        style="Card.Vertical.TScrollbar")
    box.configure(yscrollcommand=bar.set)
    box.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
    bar.pack(side="right", fill="y", pady=6)

    entry_var = tk.StringVar()
    count_var = tk.StringVar()
    tools = ttk.Frame(row.frame, style="Card.TFrame")
    tools.pack(fill="x", pady=(6, 0))
    entry = ttk.Entry(tools, textvariable=entry_var, font=font)
    entry.pack(side="left", fill="x", expand=True, padx=(0, 6))

    def add(_event=None) -> None:
        value = entry_var.get().strip()
        if not value:
            return
        if any(v.lower() == value.lower() for v in items):
            return
        items.append(value)
        entry_var.set("")
        _fill()

    def remove() -> None:
        selection = list(box.curselection())
        if not selection:
            return
        for index in reversed(selection):
            value = box.get(index)
            items[:] = [v for v in items if v.lower() != value.strip().lower()]
        _fill()

    entry.bind("<Return>", add)
    ttk.Button(tools, text="添加", style="Small.TButton", command=add).pack(side="left")
    ttk.Button(tools, text="删除选中", style="Small.TButton", command=remove).pack(side="left", padx=(6, 0))

    ttk.Label(row.frame, textvariable=count_var, style="CardHint.TLabel").pack(anchor="w", pady=(4, 0))
    if placeholder:
        ttk.Label(row.frame, text=placeholder, style="CardHint.TLabel",
                  wraplength=420, justify="left").pack(anchor="w")
    _fill()
    return row


def make_action_row(parent: tk.Misc, buttons: list[tuple[str, Callable[[], None]]],
                    font_small: tuple) -> Row:
    """按钮组行。"""

    class _Row:
        frame = ttk.Frame(parent, style="Card.TFrame")

        def get(self) -> None:
            return None

        def set(self, value: Any) -> None:
            return None

    row = _Row()
    row.frame.pack(fill="x")
    for index, (text, command) in enumerate(buttons):
        style = "Accent.TButton" if index == 0 else "Small.TButton"
        ttk.Button(row.frame, text=text, style=style, command=command).pack(side="left", padx=(0, 6))
    return row


# ================================================================ 设置页


class SettingsPage(ttk.Frame):
    """设置页：左侧表格（自绘）+ 右侧详情/编辑器。

    对外接口与旧 ``SettingsPanel`` 保持一致（``collect`` / ``save`` /
    ``restore_defaults`` / ``var_autostart`` / ``var_win_preset`` /
    ``_set_size`` 等），``ui.py`` 只需换一个类名即可。
    """

    TABLE_HEIGHT = 14             # 表格默认行数（按可用高度自动调整）

    def __init__(self, master: tk.Misc, app) -> None:
        super().__init__(master, style="Card.TFrame", padding=(12, 10))
        self.app = app
        self.config: Config = app.config
        self.settings = self.config.settings
        self.font_family = app.font_family
        self.font_body = app.font_body
        self.font_small = app.font_small

        # 草稿值：所有改动先落在 _draft，点「保存设置」才写入配置
        self._draft: dict[str, Any] = {}
        self._schema: list[Setting] = []
        self._rows: dict[str, Row] = {}          # 右侧面板当前渲染的编辑器
        self._inline: tk.Widget | None = None    # 当前就地编辑控件
        self._inline_key: str | None = None
        self._suppress_detail = False

        # 兼容旧界面的属性名
        self.var_autostart = tk.BooleanVar(
            value=autostart.is_enabled() or self.settings.autostart_enabled)
        self.var_autostart_status = tk.StringVar(value=autostart.status_text())
        self.var_win_w = tk.IntVar(value=int(self.settings.window_width))
        self.var_win_h = tk.IntVar(value=int(self.settings.window_height))
        self.var_win_preset = tk.StringVar(
            value=match_preset(self.settings.window_width, self.settings.window_height))
        self.var_win_info = tk.StringVar(value="")

        self._build_schema()
        self._reset_draft()
        self._build()
        self._refresh_table()
        self.select_first()

    # ================================================================== 表结构
    def _build_schema(self) -> None:
        """所有设置项的声明（新增设置只要在这里加一行）。"""

        def num(key: str, label: str, group: str, low: float, high: float, step: float,
                suffix: str, as_int: bool, hint: str = "") -> Setting:
            return Setting(key, label, group, "int" if as_int else "float", hint=hint,
                           low=low, high=high, step=step, suffix=suffix)

        def flag(key: str, label: str, group: str, hint: str = "") -> Setting:
            return Setting(key, label, group, "bool", hint=hint)

        self._schema = [
            # ---- 目标与提醒 ----
            num("daily_limit_minutes", "每日时长上限（分钟）", "目标与提醒", 1, 1440, 5, "分钟", True,
                "所有监控对象合计，到点后触发提醒"),
            num("reminder_interval_minutes", "超时后重复提醒间隔（分钟）", "目标与提醒", 1, 240, 5,
                "分钟", True, "达到上限后每隔这么久再提醒一次"),
            num("monitor_interval_seconds", "前台采样间隔（秒）", "目标与提醒", 0.2, 10.0, 0.2, "秒",
                False, "越小越精确，CPU 占用略升"),
            Setting("count_mode", "计时口径", "目标与提醒", "radio",
                    hint="total=所有对象合并计时；each=每个对象单独计时",
                    choices=("total", "each")),
            flag("remind_immediately_on_exceed", "刚超过上限就立即提醒一次", "目标与提醒"),
            flag("enable_toast", "发送 Windows 右下角系统通知", "目标与提醒"),
            flag("enable_popup", "弹出置顶强制提醒窗口", "目标与提醒"),
            flag("enable_sound", "弹窗时播放提示音", "目标与提醒"),
            flag("suppress_popup_fullscreen", "全屏游戏时暂缓弹窗（退出全屏补发）", "目标与提醒"),

            # ---- 待办任务 ----
            flag("task_check_on_start", "开机 / 首次运行检查未完成任务并弹窗", "待办任务"),
            flag("task_reminder_toast", "临近截止时另发系统通知", "待办任务"),
            num("task_notify_within_hours", "截止不足多少小时才发通知", "待办任务", 0.5, 168.0, 0.5,
                "小时", False, "只对**单次任务**生效；周期任务用下面的提前提醒"),
            num("task_notify_interval_hours", "同一任务两次通知的最小间隔", "待办任务", 0.5, 72.0, 0.5,
                "小时", False, "避免同一条任务反复打扰"),

            # ---- 周期性任务提醒 ----
            flag("recurring_reminder_enabled", "启用周期任务提醒（开始前 N 分钟）", "周期任务提醒",
                 hint="每条每日/每周任务自己设置提前多少分钟，默认 15 分钟"),
            flag("recurring_late_catchup", "开机时补发错过的周期任务提醒", "周期任务提醒",
                 hint="例如 17:50 关机、18:30 才开机，仍会在开机后补一次"),
            num("recurring_catchup_minutes", "补发窗口（错过开始后多少分钟内补发）", "周期任务提醒",
                1, 240, 1, "分钟", True, "超过这个时间就不再补发，避免开机后弹一堆过期提醒"),
            num("scheduler_max_sleep_minutes", "调度器单次最长睡眠", "周期任务提醒", 1, 240, 1,
                "分钟", True, "越小越能抵抗休眠/改时钟造成的漂移，默认 30 分钟"),

            # ---- 监控对象 ----
            Setting("game_processes", "监控的游戏 / 软件进程名", "监控对象", "list",
                    hint="填任务管理器『详细信息』里的名称，例如 LimbusCompany.exe",
                    extra="提示：游戏由启动器拉起也能识别（会自动向上找父进程）。"),
            Setting("title_keywords", "浏览器窗口标题关键字", "监控对象", "list",
                    hint="标题栏里出现的词即可，例如 哔哩哔哩 / 抖音 / YouTube",
                    extra="只在浏览器进程处于前台时按标题匹配。"),
            flag("match_path_contains", "进程匹配时同时比对完整安装路径", "监控对象"),

            # ---- 界面与启动 ----
            Setting("window_size", "窗口大小", "界面与启动", "window",
                    hint="选预设或自定义宽高，超出屏幕可用区域会自动收缩",
                    extra="下方预览按真实比例显示，橙色表示会被收缩。"),
            flag("start_minimized", "启动后直接最小化到系统托盘", "界面与启动"),
            Setting("autostart_enabled", "开机自动启动 TimeGuard", "界面与启动", "bool",
                    hint="写入注册表 Run 键，不需要管理员权限"),

            # ---- 数据与维护 ----
            Setting("__actions__", "常用操作", "数据与维护", "action",
                    hint="测试提醒 / 清零统计 / 导出 CSV / 打开数据目录"),            Setting("__about__", "数据位置与版本", "数据与维护", "action",
                    hint=f"设置与记录保存在 %APPDATA%\\{APP_NAME}"),
        ]
        for item in self._schema:
            item.keywords = (f"{item.label} {item.group} {item.key} {item.hint} "
                             f"{item.extra}").lower()

    def _reset_draft(self, only_missing: bool = False) -> None:
        """用配置里的当前值重建草稿（列表/字典类深拷贝，避免直接改到配置）。

        :param only_missing: 只补上草稿里还没有的项，**保留用户尚未保存的修改**。
            切换设置项时用 True —— 否则每点一下别的设置都会把刚改的值悄悄还原。
        """
        draft: dict[str, Any] = self._draft if only_missing else {}
        for setting in self._schema:
            if only_missing and setting.key in draft:
                continue
            if setting.kind == "window":
                if not (only_missing and "window_width" in draft):
                    draft["window_width"] = int(self.settings.window_width)
                    draft["window_height"] = int(self.settings.window_height)
            elif setting.kind == "action":
                continue
            elif setting.kind == "bool" and setting.key == "autostart_enabled":
                draft[setting.key] = bool(autostart.is_enabled() or self.settings.autostart_enabled)
            else:
                draft[setting.key] = copy.deepcopy(getattr(self.settings, setting.key, None))
        self._draft = draft

    def _status_text(self, setting: Setting) -> str:
        """表格「说明」列：布尔项直接把当前值写成一句话。"""
        if setting.kind in ("bool", "action", "list", "window"):
            return setting.hint or ("设定后立即生效" if setting.kind == "bool" else "")
        return setting.hint

    # ================================================================== 布局
    def _build(self) -> None:
        self.columnconfigure(0, weight=3)
        self.columnconfigure(1, weight=2)
        self.rowconfigure(1, weight=1)

        # 顶部：标题 + 筛选 + 保存
        head = ttk.Frame(self, style="Card.TFrame")
        head.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        head.columnconfigure(4, weight=1)
        ttk.Label(head, text="⚙ 设置", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(head, text="筛选", style="CardHint.TLabel").grid(row=0, column=1, sticky="e",
                                                                  padx=(16, 4))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_a: self._refresh_table())
        ttk.Entry(head, textvariable=self.search_var, width=16,
                  font=self.font_small).grid(row=0, column=2, sticky="w")
        ttk.Button(head, text="✕", width=3, style="Small.TButton",
                   command=lambda: self.search_var.set("")).grid(row=0, column=3, padx=(4, 0))
        ttk.Button(head, text="💾 保存设置", style="Accent.TButton", command=self.save).grid(
            row=0, column=5, padx=(8, 0))
        ttk.Button(head, text="恢复默认", style="Small.TButton",
                   command=self.restore_defaults).grid(row=0, column=6, padx=(6, 0))

        # 左：设置表格（全部内容都是文字行，滚动 = 画文字）
        left = ttk.Frame(self, style="Card.TFrame")
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 10))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(left, columns=("group", "name", "value", "desc"),
                                 show="headings", selectmode="browse", height=self.TABLE_HEIGHT)
        for column, title, width, anchor in (
            ("group", "分组", 78, "w"),
            ("name", "设置项", 196, "w"),
            ("value", "当前值", 122, "w"),
            ("desc", "说明", 310, "w"),
        ):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, anchor=anchor,
                             stretch=(column in ("name", "desc")))
        self.tree.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview,
                            style="Card.Vertical.TScrollbar")
        bar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._show_detail())
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Button-1>", self._on_click_value, add="+")
        self.tree.bind("<space>", lambda _e: self._toggle_focused())
        self.tree.bind("<Return>", self._on_double_click)

        # 右：选中项详情 + 编辑器 + 按钮
        right = ttk.Frame(self, style="Card.TFrame")
        right.grid(row=1, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=1)
        self.title_var = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.title_var, style="CardTitle.TLabel").grid(
            row=0, column=0, sticky="w")
        ttk.Label(right, textvariable=getattr(self, "_hint_var", tk.StringVar()), style="CardHint.TLabel",
                  wraplength=340, justify="left").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.hint_var = tk.StringVar()
        self.hint_label = ttk.Label(right, textvariable=self.hint_var, style="CardHint.TLabel",
                                    wraplength=340, justify="left")
        self.hint_label.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.editor_holder = ttk.Frame(right, style="Card.TFrame")
        self.editor_holder.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        self.action_holder = ttk.Frame(right, style="Card.TFrame")
        self.action_holder.grid(row=3, column=0, sticky="ew", pady=(8, 0))

        # 底部提示（窗口不够高时优先隐藏）
        self.footer = ttk.Label(
            self, style="CardHint.TLabel", justify="left",
            text="操作：点某项的「当前值」直接改（回车确认 / Esc 取消）；"
                 "复选框项按空格切换；带 ▶ 的设置请在右侧编辑；改完点「保存设置」生效。",
        )
        self.footer.grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))

        self.tree.tag_configure("group", foreground=ACCENT)
        self.tree.tag_configure("action", foreground=SUB)
        self.bind("<Configure>", self._fit_content, add="+")

    def _fit_content(self, event: tk.Event) -> None:
        """按可用高度调整表格行数、必要时精简底部提示。"""
        if event.widget is not self:
            return
        height = self.winfo_height()
        try:
            self.footer.grid_remove() if height < 420 else self.footer.grid()
            if height >= 640:
                rows = 15
            elif height >= 560:
                rows = 13
            elif height >= 480:
                rows = 10
            else:
                rows = 7
            if int(self.tree.cget("height")) != rows:
                self.tree.configure(height=rows)
        except tk.TclError:
            pass

    # ================================================================== 表格
    def _refresh_table(self) -> None:
        """按搜索词重画表格，尽量保持当前选中项。"""
        needle = self.search_var.get().strip().lower()
        selected = self.current_key()
        self._destroy_inline()
        for item in self.tree.get_children():
            self.tree.delete(item)

        group: str | None = None
        for setting in self._schema:
            if needle and needle not in setting.keywords:
                continue
            if setting.group != group:
                group = setting.group
                self.tree.insert("", "end", iid=f"__grp__{group}", tags=("group",),
                                 values=(group, "", "", ""))
            self.tree.insert("", "end", iid=setting.key, values=(
                setting.group,
                ("▶ " if setting.kind in ("list", "window", "radio", "action") else "") + setting.label,
                self._display_value(setting),
                self._status_text(setting),
            ), tags=("action",) if setting.kind == "action" else ())
        if selected and self.tree.exists(selected):
            self.tree.selection_set(selected)
            self.tree.see(selected)
        else:
            self.select_first()

    def _display_value(self, setting: Setting) -> str:
        """「当前值」列的文字。"""
        if setting.kind == "action":
            return "点击下方按钮"
        if setting.kind == "window":
            return f"{int(self._draft.get('window_width', 0))} × {int(self._draft.get('window_height', 0))}"
        if setting.kind == "bool":
            value = bool(self._draft.get(setting.key))
            if setting.key == "autostart_enabled":
                return "✔ 已开启" if value else "✘ 未开启"
            return BOOL_TEXT[value]
        if setting.kind == "list":
            items = self._draft.get(setting.key) or []
            return f"{len(items)} 项（右侧编辑）"
        if setting.kind == "radio":
            labels = {"total": "合并计时", "each": "分别计时"}
            raw = self._draft.get(setting.key)
            return labels.get(str(raw), str(raw))
        if setting.kind == "choice":
            return str(self._draft.get(setting.key, ""))
        value = self._draft.get(setting.key)
        if isinstance(value, float):
            return f"{value:g}{setting.suffix}"
        return f"{value}{setting.suffix}" if setting.suffix else str(value)

    def current_key(self) -> str | None:
        """当前选中的设置项 key。"""
        selection = self.tree.selection()
        if not selection:
            return None
        key = selection[0]
        return None if key.startswith("__grp__") else key

    def _current_setting(self) -> Setting | None:
        key = self.current_key()
        for setting in self._schema:
            if setting.key == key:
                return setting
        return None

    def select_first(self) -> None:
        """默认选中第一个设置项。"""
        for item in self.tree.get_children():
            if not item.startswith("__grp__"):
                self.tree.selection_set(item)
                self.tree.see(item)
                self._show_detail()
                return

    def focus_key(self, key: str) -> None:
        """选中并滚动到某一项。"""
        if self.tree.exists(key):
            self.tree.selection_set(key)
            self.tree.see(key)
            self._show_detail()

    # ================================================================== 详情 / 编辑
    def _show_detail(self) -> None:
        """渲染右侧详情面板（只保留一个编辑器）。"""
        if self._suppress_detail:
            return
        self._commit_row_editors()
        # 补上草稿里缺失的项，但**不能用配置覆盖已有草稿**：
        # 否则每点一下别的设置项，刚改还没保存的值就会被悄悄还原回旧值。
        self._reset_draft(only_missing=True)
        self._destroy_inline()
        for child in self.editor_holder.winfo_children():
            child.destroy()
        for child in self.action_holder.winfo_children():
            child.destroy()
        self._rows.clear()
        self._action_buttons = []

        setting = self._current_setting()
        if setting is None:
            self.title_var.set("")
            self.hint_var.set("")
            return
        self.title_var.set(setting.label)
        self.hint_var.set("\n".join(t for t in (setting.hint, setting.extra) if t))

        if setting.kind == "action":
            buttons = self._action_buttons_for(setting.key)
            self._action_buttons = buttons
            for index, (text, command) in enumerate(buttons):
                style = "Accent.TButton" if index == 0 else "Small.TButton"
                ttk.Button(self.action_holder, text=text, style=style,
                           command=command).pack(anchor="w", pady=2)
            return

        try:
            self._build_detail_editor(setting)
        except Exception:  # noqa: BLE001
            log.exception("渲染设置项 %s 失败", setting.key)
            ttk.Label(self.editor_holder, text="（该设置项渲染失败，详见日志）",
                      style="CardHint.TLabel").pack(anchor="w")

    def _action_buttons_for(self, key: str) -> list[tuple[str, Callable[[], None]]]:
        """「常用操作」/「数据位置」两项的按钮。"""
        if key == "__actions__":
            return [
                ("🔔 测试使用时长提醒", self.app.test_reminder),
                ("⏰ 测试周期提醒（会消费本次）", self.app.test_recurring_reminder),
                ("🧹 清零今日统计", self.app.reset_today),
                ("📤 导出 CSV", self.app.export_csv),
                ("📁 打开数据目录", self.app.open_data_dir),
                ("☀ 待办提醒预览", self.app.show_task_reminder_again),
            ]
        from . import __version__, paths

        self.hint_var.set(
            f"版本：TimeGuard {__version__}\n"
            f"数据目录：{paths.data_dir()}\n"
            f"数据库：{paths.database_file().name}　日志：{paths.log_file().name}\n"
            f"今天：{today_str()}\n"
            "卸载：删除程序目录；如需清空记录，再删除上面的数据目录。"
        )
        return [("📂 打开数据目录", self.app.open_data_dir)]

    # ---- 右侧编辑器 ----
    def _build_detail_editor(self, setting: Setting) -> None:
        """为需要多个控件的设置构建右侧编辑器。"""
        if setting.kind in ("int", "float"):
            def get() -> float:
                return float(self._draft.get(setting.key, setting.low))

            def set_(value: float) -> None:
                self._draft[setting.key] = value
                self._after_change(setting)

            self._rows[setting.key] = make_number_row(
                self.editor_holder, get, set_, setting.low, setting.high, setting.step,
                setting.suffix, setting.kind == "int", self.font_body)
            return

        if setting.kind == "bool":
            def get_bool() -> bool:
                return bool(self._draft.get(setting.key))

            def set_bool(value: bool) -> None:
                self._draft[setting.key] = bool(value)
                self._after_change(setting)

            self._rows[setting.key] = make_bool_row(self.editor_holder, get_bool, set_bool,
                                                    self.font_body)
            return

        if setting.kind in ("text", "choice"):
            values = list(setting.choices)

            def get_text() -> str:
                return str(self._draft.get(setting.key, ""))

            def set_text(value: str) -> None:
                self._draft[setting.key] = str(value)
                self._after_change(setting)

            if values:
                self._rows[setting.key] = make_choice_row(self.editor_holder, get_text, set_text,
                                                          values, self.font_body)
            else:
                self._rows[setting.key] = make_text_row(self.editor_holder, get_text, set_text,
                                                        self.font_body)
            return

        if setting.kind == "radio":
            labels = [("total", "合并计时"), ("each", "分别计时")]

            def get_radio() -> str:
                return str(self._draft.get(setting.key, "total"))

            def set_radio(value: str) -> None:
                self._draft[setting.key] = str(value)
                self._after_change(setting)

            self._rows[setting.key] = make_radio_row(self.editor_holder, get_radio, set_radio,
                                                     labels, self.font_body)
            return

        if setting.kind == "list":
            def get_list() -> list[str]:
                return list(self._draft.get(setting.key) or [])

            def set_list(value: list[str]) -> None:
                self._draft[setting.key] = [str(v).strip() for v in (value or []) if str(v).strip()]
                self._after_change(setting)

            self._rows[setting.key] = make_list_row(
                self.editor_holder, get_list, set_list, self.font_body, self.font_small,
                height=7, placeholder=setting.extra)
            return

        if setting.kind == "window":
            self._rows[setting.key] = self._make_window_editor(self.editor_holder)
            return

        if setting.kind == "bool" and setting.key == "autostart_enabled":
            self._rows[setting.key] = self._make_autostart_editor(self.editor_holder)
            return

    # ---- 就地编辑 ----
    def _on_click_value(self, event: tk.Event) -> None:
        """在「当前值」单元格上单击（第二次）= 就地编辑。

        为什么要"第二次"：第一次点击只负责选中（和其它列一样），
        第二次点同一行的「当前值」才进编辑 —— 和资源管理器改名、Excel 编辑的手感一致。

        命中判断**直接用单元格自己的 bbox**，而不是 ``identify_column`` 的列号：
        列号在不同 ttk 版本 / 是否显示隐藏树列时会差一位（实测「当前值」是 #2 而不是
        #3，按 #3 判断会"点说明列也弹编辑框"）；用 bbox 判断还能保证
        "触发区"和"编辑浮层覆盖区"永远是同一块 —— 也就是数字和它后面的单位
        一起算，不会只认数字那一段。

        顺带让底部提示"点某项的「当前值」直接改"变成真话 —— 以前其实要双击。
        """
        row_id = self.tree.identify_row(event.y)
        if not row_id or row_id.startswith("__grp__"):
            return
        box = self.tree.bbox(row_id, "value")
        if not box or not (box[0] <= event.x <= box[0] + box[2]):
            return
        # 控件级绑定先于类绑定执行，所以这里读到的仍是"点击之前"的选中项
        if row_id not in self.tree.selection():
            return
        setting = next((item for item in self._schema if item.key == row_id), None)
        if setting is None or not setting.editable_inline:
            return
        # 交给空闲回调再浮出编辑框：先让这次点击（尤其是焦点转移）处理完，
        # 否则编辑框刚 focus_set 就被树控件的点击抢走焦点，触发 FocusOut 直接提交掉。
        self.after_idle(lambda: self._start_inline(setting, event.x, event.y))

    def _on_double_click(self, event: tk.Event) -> str | None:
        """双击单元格 = 就地编辑（布尔项弹「开启 / 关闭」，不再无声翻转）。

        想快速翻转布尔项用空格键（见 :meth:`_toggle_focused`），
        或者直接点右侧面板里的复选框。
        """
        setting = self._current_setting()
        if setting is None:
            return None
        if setting.editable_inline:
            self._start_inline(setting, getattr(event, "x", 0), getattr(event, "y", 0))
            return "break"
        # list / window / radio / action：把焦点交给右侧面板
        if setting.key in self._rows:
            try:
                self._rows[setting.key].frame.focus_set()
            except tk.TclError:
                pass
        return None

    def _toggle_focused(self) -> str | None:
        """空格键：切换布尔项。"""
        setting = self._current_setting()
        if setting is not None and setting.kind == "bool":
            self._toggle_value(setting)
            return "break"
        return None

    def _toggle_value(self, setting: Setting) -> None:
        """切换布尔项并刷新表格。"""
        if setting.key == "autostart_enabled":
            want = not bool(self._draft.get(setting.key))
            ok = autostart.apply(want)
            self._draft[setting.key] = bool(ok)
            self.var_autostart.set(bool(ok))
            self.var_autostart_status.set(autostart.status_text())
            if want and not ok:
                messagebox.showwarning(
                    "未能设置开机自启",
                    "写入注册表失败，可能被安全软件拦截。\n"
                    "可手动把 TimeGuard 的快捷方式放进：Win+R → shell:startup",
                    parent=self.app.root)
        else:
            self._draft[setting.key] = not bool(self._draft.get(setting.key))
        self._mark_dirty()
        self._update_row(setting)
        # 同 _commit_inline：先让右侧控件跟上新值，避免旧值被回写覆盖
        self._refresh_row_editors()
        self._show_detail()

    def _start_inline(self, setting: Setting, x: int = 0, y: int = 0) -> None:
        """在「当前值」单元格上浮出一个编辑控件。

        浮层是**一整个容器**（编辑控件 + 单位后缀），不是孤零零一个输入框 ——
        编辑"120分钟"时如果只放一个 Spinbox，单位会被盖掉，看着像这个设置没有单位；
        布尔项同理，直接弹「开启 / 关闭」两个选项，而不是无声地翻一下。
        """
        self._destroy_inline()
        column = "value"
        row_id = setting.key
        if not self.tree.exists(row_id):
            return
        if not (x or y):
            box = self.tree.bbox(row_id, column)
        else:
            box = self.tree.bbox(row_id, column)
        if not box:                      # 该行不在可视区域
            self.tree.see(row_id)
            self.tree.update_idletasks()
            box = self.tree.bbox(row_id, column)
        if not box:
            return
        bx, by, bw, bh = box

        current = self._draft.get(setting.key)
        overlay = tk.Frame(self.tree, bg=PANEL_ALT, highlightthickness=0, bd=0)
        if setting.kind in ("int", "float"):
            # 整数项不要显示成 "120.0"（那是 Spinbox 用 DoubleVar 的锅）
            var: tk.Variable = tk.StringVar(value=self._draft_number_text(setting, current))
            editor = ttk.Spinbox(overlay, from_=setting.low, to=setting.high,
                                 increment=setting.step, textvariable=var, width=8,
                                 font=self.font_body, style="Card.TSpinbox")
        elif setting.kind == "bool":
            var = tk.StringVar(value=BOOL_CHOICES[0] if current else BOOL_CHOICES[1])
            editor = ttk.Combobox(overlay, values=list(BOOL_CHOICES), textvariable=var,
                                  state="readonly", width=6, font=self.font_body,
                                  style="Dark.TCombobox")
        elif setting.kind == "choice":
            var = tk.StringVar(value=str(current))
            editor = ttk.Combobox(overlay, values=list(setting.choices), textvariable=var,
                                  state="readonly", font=self.font_body, style="Dark.TCombobox")
        else:
            var = tk.StringVar(value=str(current))
            editor = ttk.Entry(overlay, textvariable=var, font=self.font_body)
        # 编辑控件左对齐、单位跟在后面：整个浮层都盖在「当前值」单元格上，
        # 所以"点数字"和"点单位"是同一个触发区，不会只认数字那一段。
        editor.pack(side="left", fill="both", expand=setting.kind in ("text", "choice"))
        if setting.suffix and setting.kind in ("int", "float"):
            ttk.Label(overlay, text=setting.suffix, style="Card.TLabel").pack(side="left", padx=(4, 6))

        overlay.place(x=bx, y=by, width=max(bw, 110), height=bh + 2)
        self._inline_overlay = overlay
        editor.focus_set()
        self._inline = editor
        self._inline_key = setting.key
        self._inline_var = var

        def commit(_event=None) -> None:
            self._commit_inline(setting, var)

        def cancel(_event=None) -> str:
            self._destroy_inline()
            return "break"

        editor.bind("<Return>", commit)
        editor.bind("<FocusOut>", commit)
        editor.bind("<Escape>", cancel)
        if setting.kind == "bool":
            # 下拉里选一下就提交（选完焦点可能还在下拉上，不等 FocusOut）
            editor.bind("<<ComboboxSelected>>", commit)
        overlay.bind("<Escape>", cancel)

    def _draft_number_text(self, setting: Setting, current: Any) -> str:
        """数值项在编辑框里的初始文本：整数不显示小数点。"""
        try:
            value = float(current)
        except (TypeError, ValueError):
            value = float(setting.low)
        return str(int(round(value))) if setting.kind == "int" else f"{value:g}"

    def _commit_inline(self, setting: Setting, var: tk.Variable) -> None:
        """提交就地编辑的值。"""
        if self._inline is None:
            return
        raw = var.get()
        if setting.kind == "bool":
            # 布尔项走既有的切换路径：autostart 需要真写注册表并处理失败提示，
            # 不能像普通值那样只改草稿。
            want = str(raw).strip() in ("开启", "已开启", "True", "1", "yes", "on")
            self._destroy_inline()
            if want != bool(self._draft.get(setting.key)):
                self._toggle_value(setting)
            return
        try:
            if setting.kind == "int":
                value: Any = int(round(float(raw)))
                if setting.low or setting.high:
                    value = int(max(setting.low, min(setting.high, value)))
            elif setting.kind == "float":
                value = round(float(raw), 2)
                if setting.low or setting.high:
                    value = max(float(setting.low), min(float(setting.high), value))
            else:
                value = str(raw).strip()
        except (TypeError, ValueError):
            self._destroy_inline()
            self.app.show_toast_message("输入的不是有效数值，已忽略", AMBER)
            return

        self._draft[setting.key] = value
        self._destroy_inline()
        self._mark_dirty()
        self._update_row(setting)
        # 关键：右侧面板里的编辑器仍显示旧值；若直接 _show_detail()，
        # 它会在 _commit_row_editors() 里把旧值回写、覆盖刚才的就地编辑。
        # 所以先按新草稿重建面板控件，再渲染详情。
        self._refresh_row_editors()
        self._show_detail()

    def _refresh_row_editors(self) -> None:
        """按当前草稿重建右侧面板的编辑器控件。

        就地编辑 / 布尔切换改了某个值之后必须调用：否则右侧面板里对应字段的控件
        仍持有旧值，下次切行时 ``_commit_row_editors`` 会把旧值写回，覆盖用户修改。
        """
        if not self._rows:
            return
        self._rows.clear()
        for child in self.editor_holder.winfo_children():
            child.destroy()
        setting = self._current_setting()
        if setting is None or setting.kind == "action":
            return
        try:
            self._build_detail_editor(setting)
        except Exception:  # noqa: BLE001
            log.debug("重建编辑器失败", exc_info=True)

    def _commit_row_editors(self) -> None:
        """把右侧面板里编辑器的当前值收回草稿（切行时调用）。"""
        for key, row in self._rows.items():
            for setting in self._schema:
                if setting.key != key:
                    continue
                if setting.kind == "window":
                    break
                try:
                    value = row.get()
                except Exception:  # noqa: BLE001
                    break
                if setting.kind == "bool":
                    self._draft[key] = bool(value)
                elif setting.kind == "int":
                    self._draft[key] = int(value)
                elif setting.kind == "float":
                    self._draft[key] = float(value)
                elif setting.kind == "list":
                    self._draft[key] = list(value)
                elif value is not None:
                    self._draft[key] = value
                break

    def _destroy_inline(self) -> None:
        """销毁就地编辑控件（连同它的浮层容器）。"""
        widget, self._inline = self._inline, None
        overlay, self._inline_overlay = getattr(self, "_inline_overlay", None), None
        self._inline_key = None
        for target in (widget, overlay):
            if target is None:
                continue
            try:
                target.place_forget()
                target.destroy()
            except tk.TclError:
                pass

    def _update_row(self, setting: Setting) -> None:
        """只刷新某一行的「当前值」列。"""
        if not self.tree.exists(setting.key):
            return
        self.tree.set(setting.key, "value", self._display_value(setting))

    def _after_change(self, setting: Setting) -> None:
        """编辑器改动后的统一处理：刷新表格 + 标记未保存。"""
        self._update_row(setting)
        self._mark_dirty()

    # ---- 兼容旧接口的特例编辑器 ----
    def _make_autostart_editor(self, parent: tk.Misc) -> Row:
        class _Row:
            frame = ttk.Frame(parent, style="Card.TFrame")

            def get(self) -> bool:
                return bool(self_var.get())

            def set(self, value: bool) -> None:
                self_var.set(bool(value))

        self_var = self.var_autostart
        row = _Row()
        row.frame.pack(fill="x")
        ttk.Checkbutton(row.frame, text="启用开机自启", variable=self_var,
                        style="Card.TCheckbutton",
                        command=self._on_autostart_toggle).pack(anchor="w")
        ttk.Label(row.frame, textvariable=self.var_autostart_status, style="CardHint.TLabel",
                  wraplength=340, justify="left").pack(anchor="w", pady=(4, 0))
        ttk.Button(row.frame, text="立即写入 / 移除注册表", style="Small.TButton",
                   command=self._on_autostart_toggle).pack(anchor="w", pady=(6, 0))
        return row

    def _make_window_editor(self, parent: tk.Misc) -> Row:
        class _Row:
            frame = ttk.Frame(parent, style="Card.TFrame")

            def get(self) -> tuple[int, int]:
                return (int(self.w_var.get()), int(self.h_var.get()))

            def set(self, value: Any) -> None:
                width, height = value
                self.w_var.set(int(width))
                self.h_var.set(int(height))

        row = _Row()
        row.frame.pack(fill="x")

        preset_row = ttk.Frame(row.frame, style="Card.TFrame")
        preset_row.pack(fill="x")
        ttk.Label(preset_row, text="预设：", style="Card.TLabel").pack(side="left")
        combo = ttk.Combobox(preset_row, values=preset_names(), textvariable=self.var_win_preset,
                             state="readonly", width=20, font=self.font_small,
                             style="Dark.TCombobox")
        combo.pack(side="left")
        combo.bind("<<ComboboxSelected>>", lambda _e: self._on_preset_selected())

        self.w_var = self.var_win_w
        self.h_var = self.var_win_h
        size_row = ttk.Frame(row.frame, style="Card.TFrame")
        size_row.pack(fill="x", pady=(6, 0))
        ttk.Label(size_row, text="自定义：", style="Card.TLabel").pack(side="left")
        ttk.Spinbox(size_row, from_=720, to=3840, increment=20, width=6, textvariable=self.w_var,
                    style="Card.TSpinbox", command=self._on_size_changed).pack(side="left")
        ttk.Label(size_row, text="×", style="Card.TLabel").pack(side="left", padx=3)
        ttk.Spinbox(size_row, from_=480, to=2160, increment=20, width=6, textvariable=self.h_var,
                    style="Card.TSpinbox", command=self._on_size_changed).pack(side="left")
        ttk.Label(size_row, text="px", style="Card.TLabel").pack(side="left", padx=(4, 0))

        ttk.Button(row.frame, text="立即应用窗口大小", style="Small.TButton",
                   command=self.apply_window_now).pack(anchor="w", pady=(8, 0))
        try:
            self.preview = WindowSizePreview(row.frame, self.font_family,
                                             on_size_change=self._on_preview_result,
                                             width=320, height=140)
            self.preview.pack(anchor="w", pady=(8, 0))
            self.preview.set_size(int(self.w_var.get()), int(self.h_var.get()))
        except Exception:  # noqa: BLE001
            log.debug("窗口预览不可用", exc_info=True)
        ttk.Label(row.frame, textvariable=self.var_win_info, style="CardHint.TLabel",
                  wraplength=340, justify="left").pack(anchor="w", pady=(4, 0))
        return row

    # ================================================================== 收集 / 保存
    def collect(self) -> dict:
        """把草稿收拢成 ``update_settings`` 需要的 dict。"""
        self._commit_row_editors()
        values: dict[str, Any] = {}
        for setting in self._schema:
            if setting.kind == "action":
                continue
            if setting.kind == "window":
                values["window_width"] = self._current_window_size()[0]
                values["window_height"] = self._current_window_size()[1]
                continue
            value = self._draft.get(setting.key)
            if setting.kind == "bool":
                values[setting.key] = bool(value)
            elif setting.kind == "int":
                values[setting.key] = int(value)
            elif setting.kind == "float":
                values[setting.key] = float(value)
            elif setting.kind in ("text", "choice", "radio", "list"):
                values[setting.key] = value
        return values

    def _current_window_size(self) -> tuple[int, int]:
        try:
            return (int(self.var_win_w.get()), int(self.var_win_h.get()))
        except (tk.TclError, ValueError):
            return (int(self.settings.window_width), int(self.settings.window_height))

    def save(self) -> None:
        """保存：写回配置 + 立即生效 + 刷新表格。"""
        self._commit_row_editors()
        self._destroy_inline()
        values = self.collect()
        self.settings.game_processes = list(self._draft.get("game_processes") or [])
        self.settings.title_keywords = list(self._draft.get("title_keywords") or [])
        self.app.engine.update_settings(**values)
        final = autostart.reconcile(values.get("autostart_enabled", False))
        self.settings.autostart_enabled = final
        self.var_autostart.set(final)
        self.var_autostart_status.set(autostart.status_text())
        self.config.save()
        self.app.set_dirty(False)
        self.app.refresh_chart()
        self._refresh_table()
        # 关键：设置改完后必须重排周期任务调度器。
        # 否则用户在设置页把"周期任务提醒"关掉再打开，调度器本会话内就再也不会醒
        # （关闭时定时器链已断，没有别的地方会重新武装它）。
        self._reschedule_scheduler()
        self.app.show_toast_message("设置已保存并立即生效")
        log.info("设置已保存: 上限=%s分钟 间隔=%s分钟 进程=%d个 关键字=%d个 窗口=%sx%s 自启=%s",
                 values.get("daily_limit_minutes"), values.get("reminder_interval_minutes"),
                 len(self.settings.game_processes), len(self.settings.title_keywords),
                 values.get("window_width"), values.get("window_height"), final)

    def _reschedule_scheduler(self) -> None:
        """通知主界面重排周期任务调度器（设置变化后必须调用）。"""
        callback = getattr(self.app, "reschedule_recurring", None)
        if not callable(callback):
            return
        try:
            callback("settings-saved")
        except Exception:  # noqa: BLE001
            log.debug("重排周期任务调度失败", exc_info=True)

    def restore_defaults(self) -> None:
        """恢复默认设置。"""
        if not messagebox.askyesno("确认", "确定要恢复所有默认设置吗？", parent=self.app.root):
            return
        self.config.reset_defaults()
        self.settings = self.config.settings
        self._reset_draft()
        self._build_schema()
        self._sync_window_widgets()
        self._refresh_table()
        self._show_detail()
        self._reschedule_scheduler()
        self.app.reload_settings_ui()
        self.app.show_toast_message("已恢复默认设置")

    def reload_from_settings(self) -> None:
        """外部（恢复默认 / 配置重载）改动后刷新界面。"""
        self.settings = self.config.settings
        self._reset_draft()
        self.var_autostart.set(autostart.is_enabled() or self.settings.autostart_enabled)
        self.var_autostart_status.set(autostart.status_text())
        self._build_schema()
        self._sync_window_widgets()
        self._refresh_table()
        self._show_detail()

    # ================================================================== 窗口大小
    @staticmethod
    def _match_preset(width: int, height: int) -> str:
        return match_preset(width, height)

    def _on_preset_selected(self) -> None:
        name = self.var_win_preset.get()
        if name == PRESET_FULLSCREEN:
            work_w, work_h = winapi.get_work_area(self.app.root)
            self.var_win_w.set(int(work_w - 40))
            self.var_win_h.set(int(work_h - 40))
        else:
            from .settings_preview import WINDOW_PRESETS

            size = WINDOW_PRESETS.get(name)
            if size:
                self.var_win_w.set(int(size[0]))
                self.var_win_h.set(int(size[1]))
        self._on_size_changed()

    def _on_size_changed(self) -> None:
        self._draft["window_width"] = int(self.var_win_w.get())
        self._draft["window_height"] = int(self.var_win_h.get())
        preview = getattr(self, "preview", None)
        if preview is not None:
            try:
                preview.set_size(int(self.var_win_w.get()), int(self.var_win_h.get()))
            except (tk.TclError, ValueError):
                pass
        for setting in self._schema:
            if setting.kind == "window":
                self._update_row(setting)
        self._mark_dirty()

    def _on_preview_result(self, final_w: int, final_h: int, clamped: bool) -> None:
        work_w, work_h = winapi.get_work_area(self.app.root)
        text = f"实际生效：{final_w} × {final_h} px（屏幕可用区域 {work_w} × {work_h}）"
        if clamped:
            text += "　·　设置值超出可用区域，已自动收缩"
        self.var_win_info.set(text)

    def apply_window_now(self) -> None:
        """按当前尺寸立刻调整主窗口。"""
        width, height = self._current_window_size()
        final_w, final_h = self.app.apply_window_size(width, height, center=True)
        if (final_w, final_h) != (width, height):
            self.app.show_toast_message("窗口已按屏幕可用区域收缩", AMBER)
        else:
            self.app.show_toast_message(f"窗口已调整为 {final_w} × {final_h}")
        try:
            self.settings.window_width, self.settings.window_height = final_w, final_h
            self.config.save()
            for setting in self._schema:
                if setting.kind == "window":
                    self._update_row(setting)
        except Exception:  # noqa: BLE001
            log.debug("保存窗口尺寸失败", exc_info=True)

    def _set_size(self, width: int, height: int, preset: str | None = None) -> None:
        """设置窗口宽高（供预设/自定义演示与外部调用）。"""
        self.var_win_w.set(int(width))
        self.var_win_h.set(int(height))
        self.var_win_preset.set(preset if preset is not None else match_preset(width, height))
        self._on_size_changed()

    def _refresh_preview(self) -> None:
        """重绘预览。"""
        preview = getattr(self, "preview", None)
        if preview is None:
            return
        try:
            preview.set_size(int(self.var_win_w.get()), int(self.var_win_h.get()))
        except (tk.TclError, ValueError):
            log.debug("预览重绘失败", exc_info=True)

    def _sync_window_widgets(self) -> None:
        """把配置里的窗口尺寸同步到控件（不触发"未保存修改"）。"""
        self.var_win_w.set(int(self.settings.window_width))
        self.var_win_h.set(int(self.settings.window_height))
        self.var_win_preset.set(match_preset(self.settings.window_width,
                                             self.settings.window_height))
        self._draft["window_width"] = int(self.settings.window_width)
        self._draft["window_height"] = int(self.settings.window_height)
        self._refresh_preview()

    def _on_autostart_toggle(self) -> None:
        """写注册表并刷新状态。"""
        want = bool(self.var_autostart.get())
        ok = autostart.apply(want)
        self.var_autostart.set(ok)
        self.var_autostart_status.set(autostart.status_text())
        self._draft["autostart_enabled"] = bool(ok)
        if want and not ok:
            messagebox.showwarning(
                "未能设置开机自启",
                "写入注册表失败，可能被安全软件拦截。\n"
                "可手动把 TimeGuard 的快捷方式放进：Win+R → shell:startup",
                parent=self.app.root)
        self._mark_dirty()
        for setting in self._schema:
            if setting.key == "autostart_enabled":
                self._update_row(setting)

    def _mark_dirty(self) -> None:
        self.app.set_dirty(True)

    # ================================================================== 外部调用
    def refresh(self) -> None:
        """轻量刷新（保存后 / 外部改动后）。"""
        self._refresh_table()
        self._show_detail()

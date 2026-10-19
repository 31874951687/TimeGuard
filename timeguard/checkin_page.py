"""「🔥 打卡进度」独立页：累计打卡任务的进度条 + 打卡热力图。

为什么单独开一页（用户要求）
----------------------------
热力图和任务清单是**两种不同节奏**的东西：

* 待办清单要的是一眼扫完"今天还有什么没做"，行高越小越好，越多越好；
* 打卡热力图要的是"看得出坚持了多久、哪几天断了"，格子越大越清楚。

挤在同一个下半屏里，两边都变小：清单被压到两三行、热力图缩成 8px 的小方块。
所以这里拆开：清单归「待办任务」，进度与热力图归「打卡进度」。

页面结构
--------
左边是累计打卡任务列表（点一下切换），右边是大号进度条 + 26 周热力图 + 操作按钮。
没有打卡任务时给一句引导，而不是留一片空白。
"""

from __future__ import annotations

import logging
import tkinter as tk
from datetime import date, datetime
from tkinter import ttk

from . import phrases
from .checkin_view import CumulativeDetail
from .database import Task, UsageStore
from .recurrence import cumulative_status
from .tasks import FG, GREEN, PANEL, RED, SUB, cumulative_row_texts

log = logging.getLogger(__name__)

#: 独立页给热力图更大的格子（26 周 × 13px，整块约 360×105）
HEATMAP_WEEKS = 26
HEATMAP_CELL = 13
HEATMAP_GAP = 3


class CheckinPage(ttk.Frame):
    """累计打卡任务的独立页：左边选任务，右边看进度与热力图。"""

    def __init__(self, master: tk.Misc, store: UsageStore, font_family: str = "Microsoft YaHei UI",
                 on_check_in=None, on_undo=None, on_backfill=None, **kwargs) -> None:
        super().__init__(master, style="Card.TFrame", padding=12, **kwargs)
        self.store = store
        self.font_family = font_family
        self.font_body = (font_family, 10)
        self.font_small = (font_family, 9)
        self.on_check_in = on_check_in
        self.on_undo = on_undo
        self.on_backfill = on_backfill
        #: task_id → 行数据（``Task`` + ``CumulativeStatus``），刷新时重建
        self._rows: dict[int, tuple[Task, object]] = {}
        self._selected_id: int | None = None
        #: 上次刷新时的日期：只有跨天才需要在 tick 里重画（否则每秒重画热力图会卡）
        self._day = date.today()
        self._build()

    # ------------------------------------------------------------------ 布局
    def _build(self) -> None:
        self.columnconfigure(0, weight=0, minsize=300)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(1, weight=1)

        head = ttk.Frame(self, style="Card.TFrame")
        head.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Label(head, text="🔥 打卡进度", style="CardTitle.TLabel",
                  font=(self.font_family, 13, "bold")).pack(side="left")
        self.summary_var = tk.StringVar(value="")
        ttk.Label(head, textvariable=self.summary_var, style="CardHint.TLabel").pack(
            side="left", padx=(12, 0))
        ttk.Button(head, text="刷新", style="Small.TButton", command=self.refresh).pack(side="right")

        # ---- 左：任务列表 ----
        left = ttk.Frame(self, style="Card.TFrame")
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 10))
        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)
        ttk.Label(left, text="累计打卡任务", style="Card.TLabel").grid(row=0, column=0, sticky="w")

        columns = ("status", "progress")
        self.tree = ttk.Treeview(left, columns=columns, show="tree headings", height=6)
        self.tree.heading("#0", text="任务内容")
        self.tree.heading("status", text="状态")
        self.tree.heading("progress", text="进度")
        self.tree.column("#0", width=196, anchor="w")
        self.tree.column("status", width=96, anchor="center", stretch=False)
        self.tree.column("progress", width=124, anchor="center", stretch=False)
        # 行配色与待办页保持一致：完成灰、逾期红、进行中默认色
        self.tree.tag_configure("done", foreground=SUB)
        self.tree.tag_configure("overdue", foreground=RED)
        self.tree.tag_configure("running", foreground=FG)
        self.tree.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._on_select())
        self.tree.bind("<Double-1>", lambda _e: self._act(self.on_check_in))

        self.empty_var = tk.StringVar(
            value="还没有累计打卡任务。\n去「待办任务 → 添加任务」，重复周期选「累计打卡」。")
        self.empty_label = tk.Label(left, textvariable=self.empty_var, bg=PANEL, fg=SUB,
                                    justify="left", anchor="w", font=self.font_small)

        # ---- 右：详情（进度条 + 热力图 + 按钮）----
        right = ttk.Frame(self, style="Card.TFrame")
        right.grid(row=1, column=1, sticky="nsew")
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        self.detail = CumulativeDetail(
            right, font_family=self.font_family,
            on_check_in=self._act_check_in, on_undo=self._act_undo,
            on_backfill=self._act_backfill,
            weeks=HEATMAP_WEEKS, cell=HEATMAP_CELL, gap=HEATMAP_GAP,
        )
        self.detail.grid(row=0, column=0, sticky="nsew")

        self.quote_var = tk.StringVar(value=phrases.pick())
        self.quote_label = tk.Label(right, textvariable=self.quote_var, bg=PANEL, fg=SUB,
                                    anchor="w", font=self.font_small, wraplength=520,
                                    justify="left")
        self.quote_label.grid(row=1, column=0, sticky="ew", pady=(6, 0))

    # ------------------------------------------------------------------ 数据
    def refresh(self) -> None:
        """重新读库并重画（打卡 / 补签 / 增删任务之后都要调）。"""
        try:
            tasks = self.store.cumulative_tasks()
        except Exception:  # noqa: BLE001 - 读库失败不该把界面拖垮
            log.debug("读取累计打卡任务失败", exc_info=True)
            tasks = []
        try:
            dates_map = self.store.checkin_dates_map()
        except Exception:  # noqa: BLE001
            log.debug("读取打卡历史失败", exc_info=True)
            dates_map = {}
        now = datetime.now()
        today = now.date()

        self._rows.clear()
        for item in self.tree.get_children():
            self.tree.delete(item)
        for task in tasks:
            status = cumulative_status(task.rule, dates_map.get(task.id, []), today)
            self._rows[task.id] = (task, status)
            state_text, _progress_text = cumulative_row_texts(status)
            # 左栏的"进度"列只放 45/60（列宽有限）：
            # "还差 15 · 剩 60 天" 这类细节在右侧详情里，全写进来会被截断成省略号。
            self.tree.insert("", "end", iid=str(task.id), text=task.title,
                             values=(state_text, f"{status.total}/{status.target}"))

        running = [s for _t, s in self._rows.values() if s.is_running]
        done = [s for _t, s in self._rows.values() if s.finished]
        self.summary_var.set(
            f"共 {len(self._rows)} 条 · 进行中 {len(running)} · 已达成 {len(done)}"
            + (f" · 逾期 {sum(1 for s in self._rows.values() if s[1].missed)}"
               if any(s.missed for _t, s in self._rows.values()) else ""))

        if not self._rows:
            self.tree.grid_remove()
            self.empty_label.grid(row=1, column=0, columnspan=2, sticky="nw")
            self.detail.clear()
            self.quote_var.set("")
            return
        self.empty_label.grid_remove()
        self.tree.grid()

        # 保持原来选中的那条；没有了就选第一条"进行中"的，再没有就选第一条
        wanted = self._selected_id if self._selected_id in self._rows else None
        if wanted is None:
            wanted = next((tid for tid, (_t, s) in self._rows.items() if s.is_running),
                          next(iter(self._rows), None))
        if wanted is not None:
            try:
                self.tree.selection_set(str(wanted))
                self.tree.see(str(wanted))
            except tk.TclError:
                log.debug("恢复打卡页选中项失败", exc_info=True)
            self._show(wanted)

    def _on_select(self) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        try:
            task_id = int(selection[0])
        except ValueError:
            return
        self._show(task_id)

    def _show(self, task_id: int) -> None:
        """把某条任务的状态与历史喂给详情面板。"""
        row = self._rows.get(task_id)
        if row is None:
            return
        task, status = row
        self._selected_id = task_id
        try:
            days = self.store.checkin_dates(task_id)
        except Exception:  # noqa: BLE001
            log.debug("读取打卡日期失败", exc_info=True)
            days = []
        try:
            self.detail.show_task(task, status)
            self.detail.set_checked_days(days)
        except tk.TclError:
            log.debug("刷新打卡详情失败", exc_info=True)

    # ------------------------------------------------------------------ 操作
    def _act(self, callback) -> None:
        """对当前选中的任务执行某个动作（回调由主界面注入）。"""
        if self._selected_id is None or not callable(callback):
            return
        try:
            ok, message = callback(self._selected_id)
        except Exception:  # noqa: BLE001
            log.debug("打卡页操作失败", exc_info=True)
            return
        self.detail.set_hint(str(message or ""), GREEN if ok else SUB)

    def _act_check_in(self, task_id: int):
        return self._call(self.on_check_in, task_id)

    def _act_undo(self, task_id: int):
        return self._call(self.on_undo, task_id)

    def _act_backfill(self, task_id: int):
        return self._call(self.on_backfill, task_id)

    @staticmethod
    def _call(callback, task_id: int):
        if not callable(callback):
            return False, "这个页面没有接上操作回调"
        return callback(task_id)

    def select_task(self, task_id: int) -> bool:
        """外部（例如待办页的"查看打卡进度"按钮）跳过来时指定要看的任务。"""
        if task_id not in self._rows:
            self.refresh()
        if task_id not in self._rows:
            return False
        try:
            self.tree.selection_set(str(task_id))
            self.tree.see(str(task_id))
        except tk.TclError:
            return False
        self._show(task_id)
        return True

    def tick(self) -> None:
        """主界面每秒调一次：只在**跨天**时重画（进度、热力图都按天变）。"""
        today = date.today()
        if getattr(self, "_day", None) != today:
            self._day = today
            self.refresh()

"""「今日各对象明细」折叠条：默认收起，展开就在计时卡片里看谁在用时。

为什么从这里搬过来（v1.7，用户要求）
------------------------------------
这块明细原来放在「📊 数据统计」页下半屏，和 7 天趋势图抢高度。但它的语义是
**"今天的计时花在谁身上了"** —— 和上面的"今日已用 / 上限 / 进度条"是同一件事的
概要 vs 明细，天然属于计时区；而统计页该讲的是**跨天趋势**。

所以现在：

* 计时卡片底部一条折叠条：``▸ 今日各对象明细（3 个 · 合计 1h20m）``，默认收起；
* 点开 → 在卡片里就地展开紧凑列表（对象 / 时长 / 占比条），再点收起；
* 统计页下半改成「今日对象占比环图」（见 :mod:`timeguard.donut`）。

收起时它只占一行（约 22px），所以待办列表不会因此变矮多少。
"""

from __future__ import annotations

import logging
import tkinter as tk
from tkinter import ttk

from .donut import PALETTE
from .tasks import FG, PANEL, PANEL_ALT, SUB
from .utils import fmt_duration

log = logging.getLogger(__name__)

#: 最多列多少个对象（再多折叠成"还有 N 个"）
MAX_ROWS = 8
#: 占比条的轨道高度
BAR_HEIGHT = 8


class TodayDetail(ttk.Frame):
    """计时卡片里的"今日各对象明细"折叠面板。

    ``on_toggle`` 会在展开/收起后回调（主界面据此重算卡片高度，避免列表被挤）。
    """

    def __init__(self, master: tk.Misc, font_family: str = "Microsoft YaHei UI",
                 on_toggle=None, **kwargs) -> None:
        kwargs.setdefault("style", "Card.TFrame")
        super().__init__(master, **kwargs)
        self.font_family = font_family
        self.font_body = (font_family, 10)
        self.font_small = (font_family, 9)
        self.on_toggle = on_toggle
        self._expanded = False
        self._stats: list = []          # 最近一次喂进来的 TargetStat 列表
        self._rows: list[tk.Widget] = []
        self._build()
        self.set_expanded(False)

    # ------------------------------------------------------------------ 布局
    def _build(self) -> None:
        self.columnconfigure(0, weight=1)

        # 折叠条本身：用 Label 而不是 Button —— 按钮在深色卡片上会多一圈边框，
        # 而这条更像"一行可点的说明文字"。
        self.header = tk.Label(
            self, text="▸ 今日各对象明细", bg=PANEL, fg=SUB, anchor="w",
            font=self.font_small, cursor="hand2", padx=0, pady=2)
        self.header.grid(row=0, column=0, sticky="ew")
        self.header.bind("<Button-1>", lambda _e: self.toggle())

        # 展开区（默认不 grid）
        self.body = tk.Frame(self, bg=PANEL)
        self.body.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        self.body.columnconfigure(0, weight=1)
        self.body.grid_remove()

        self.empty = tk.Label(self.body, text="今天还没有监控数据", bg=PANEL, fg=SUB,
                              anchor="w", font=self.font_small)
        self.empty.grid(row=0, column=0, sticky="w")

    # ------------------------------------------------------------------ 对外
    @property
    def expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool) -> None:
        """展开 / 收起（幂等；会在结束时回调 ``on_toggle``）。"""
        expanded = bool(expanded)
        if expanded:
            self.body.grid()
        else:
            self.body.grid_remove()
        self._expanded = expanded
        self._render_header()
        if callable(self.on_toggle):
            try:
                self.on_toggle(expanded)
            except Exception:  # noqa: BLE001 - 布局回调出错不该把界面带崩
                log.debug("明细展开回调失败", exc_info=True)

    def toggle(self) -> None:
        self.set_expanded(not self._expanded)

    def refresh(self, stats: list | None = None) -> None:
        """喂入 ``store.today_targets()`` 的结果并重画（收起时只更新标题行）。"""
        if stats is not None:
            self._stats = list(stats)
        self._render_header()
        if self._expanded:
            self._render_rows()

    # ------------------------------------------------------------------ 内部
    def _render_header(self) -> None:
        """标题行：箭头 + 条目数 + 合计时长。"""
        stats = self._stats
        arrow = "▾" if self._expanded else "▸"
        if not stats:
            text = f"{arrow} 今日各对象明细（今天还没有监控数据）"
        else:
            total = sum(float(getattr(s, "seconds", 0.0) or 0.0) for s in stats)
            text = (f"{arrow} 今日各对象明细（{len(stats)} 个对象 · "
                    f"合计 {fmt_duration(total, with_seconds=False)}）")
        try:
            self.header.configure(text=text)
        except tk.TclError:
            log.debug("更新明细标题失败", exc_info=True)

    def _render_rows(self) -> None:
        for widget in self._rows:
            widget.destroy()
        self._rows.clear()
        stats = self._stats
        if not stats:
            self.empty.grid()
            return
        self.empty.grid_remove()

        total = sum(float(getattr(s, "seconds", 0.0) or 0.0) for s in stats) or 1.0
        shown = stats[:MAX_ROWS]
        for index, item in enumerate(shown):
            seconds = float(getattr(item, "seconds", 0.0) or 0.0)
            ratio = max(0.0, min(1.0, seconds / total))
            row = tk.Frame(self.body, bg=PANEL)
            row.grid(row=index, column=0, sticky="ew", pady=1)
            row.columnconfigure(1, weight=1)

            name = str(getattr(item, "name", "") or "—")
            label = tk.Label(row, text=name, bg=PANEL, fg=FG, anchor="w",
                             font=self.font_small, width=18)
            label.grid(row=0, column=0, sticky="w")
            label.configure(text=_ellipsis(name, 18))

            # 占比轨道 + 填充（用 place(relwidth=...) 最省事，不需要算像素）
            # 颜色按对象轮换，与统计页环图的图例一一对应，扫一眼就能对上
            track = tk.Frame(row, bg=PANEL_ALT, height=BAR_HEIGHT)
            track.grid(row=0, column=1, sticky="ew", padx=(8, 8))
            track.grid_propagate(False)
            if ratio > 0:
                fill = tk.Frame(track, bg=PALETTE[index % len(PALETTE)])
                fill.place(relx=0.0, rely=0.0, relwidth=ratio, relheight=1.0)

            value = tk.Label(row, text=f"{fmt_duration(seconds, with_seconds=False)}"
                                       f"　{ratio * 100:.0f}%",
                             bg=PANEL, fg=SUB, anchor="e", font=self.font_small, width=12)
            value.grid(row=0, column=2, sticky="e")
            self._rows.append(row)

        if len(stats) > MAX_ROWS:
            more = tk.Label(self.body, text=f"　还有 {len(stats) - MAX_ROWS} 个对象…",
                            bg=PANEL, fg=SUB, anchor="w", font=self.font_small)
            more.grid(row=len(shown), column=0, sticky="w")
            self._rows.append(more)


def _ellipsis(text: str, limit: int) -> str:
    """按字符数截断（中英文混排时按显示宽度大致对齐，够用就行）。"""
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"

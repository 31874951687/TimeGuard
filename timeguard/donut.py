"""「今日对象占比」环形图：统计页下半屏（v1.7 起取代原来的明细表）。

明细搬去计时卡片之后，统计页下半空了出来。放一张**占比图**比放空或放大柱状图
更贴合"统计"这个标签：一眼看出今天的时间被哪几个对象分掉了，以及有没有某个
对象偷偷占了大头。

纯 Canvas 自绘（和项目里其它图一样），不引第三方绘图库：

* 环：按占比画弧（``create_arc``，留 2° 间隙，视觉上像分段）；
* 中心：今日合计时长 + 「今日」；
* 右侧：图例（颜色点 / 名称 / 时长 / 百分比），最多 6 条，按从多到少；
* 没有数据时居中显示一句提示，而不是留一个空环。
"""

from __future__ import annotations

import logging
import tkinter as tk
from tkinter import ttk

from .tasks import ACCENT, AMBER, FG, GREEN, PANEL, RED, SUB
from .utils import fmt_duration

log = logging.getLogger(__name__)

#: 配色循环（深色底上都能看清）
PALETTE = (ACCENT, GREEN, AMBER, "#c084fc", "#22d3ee", "#f472b6", RED, "#94a3b8")
#: 图例最多几条（再多就合并成"其他"）
MAX_LEGEND = 6


class ObjectDonut(tk.Canvas):
    """今日各对象时长占比环图（含图例）。"""

    def __init__(self, master: tk.Misc, font_family: str = "Microsoft YaHei UI",
                 width: int = 860, height: int = 190, **kwargs) -> None:
        kwargs.setdefault("bg", PANEL)
        kwargs.setdefault("highlightthickness", 0)
        super().__init__(master, width=width, height=height, **kwargs)
        self.font_family = font_family
        self.font_body = (font_family, 10)
        self.font_small = (font_family, 9)
        self.font_big = (font_family, 14, "bold")
        self._items: list[tuple[str, float]] = []
        self.empty_text = "今天还没有监控数据"
        self.bind("<Configure>", lambda _e: self.redraw())

    # ------------------------------------------------------------------ 对外
    def set_data(self, stats: list) -> None:
        """喂入 ``store.today_targets()`` 的结果（按 ``seconds`` 从多到少）。"""
        items: list[tuple[str, float]] = []
        for item in stats or ():
            try:
                seconds = float(getattr(item, "seconds", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            if seconds > 0:
                items.append((str(getattr(item, "name", "") or "—"), seconds))
        items.sort(key=lambda pair: pair[1], reverse=True)
        self._items = items
        self.redraw()

    @property
    def items(self) -> list[tuple[str, float]]:
        return list(self._items)

    def total(self) -> float:
        return sum(seconds for _name, seconds in self._items)

    # ------------------------------------------------------------------ 绘制
    def redraw(self) -> None:
        try:
            self.delete("all")
            width = max(320, self.winfo_width())
            height = max(120, self.winfo_height())
            if not self._items:
                self.create_text(width / 2, height / 2, text=self.empty_text,
                                 fill=SUB, font=self.font_body)
                return

            # 环（左） + 图例（右）
            ring_size = min(height - 16, 150)
            cx = 20 + ring_size / 2 + 6
            cy = height / 2
            total = self.total() or 1.0

            legend_items = self._items[:MAX_LEGEND]
            rest = self._items[MAX_LEGEND:]
            if rest:
                legend_items = legend_items[: MAX_LEGEND - 1] + [
                    ("其他 %d 项" % len(rest), sum(s for _n, s in rest))
                ]

            start = 90.0
            for index, (_name, seconds) in enumerate(legend_items):
                extent = -360.0 * (seconds / total)
                color = PALETTE[index % len(PALETTE)]
                # 留一点缝，看起来像分段的环
                gap = 1.5 if abs(extent) > 8 else 0.0
                if abs(extent) + gap >= 1:
                    self.create_arc(
                        cx - ring_size / 2, cy - ring_size / 2,
                        cx + ring_size / 2, cy + ring_size / 2,
                        start=start - gap, extent=extent + gap,
                        style="arc", outline=color, width=max(10, int(ring_size * 0.13)))
                start += extent

            # 环心：今日合计
            self.create_text(cx, cy - 8, text=fmt_duration(total, with_seconds=False),
                             fill=FG, font=self.font_big)
            self.create_text(cx, cy + 12, text="今日合计", fill=SUB, font=self.font_small)

            # 图例
            left = cx + ring_size / 2 + 28
            top = max(10, cy - len(legend_items) * 12)
            for index, (name, seconds) in enumerate(legend_items):
                y = top + index * 24
                color = PALETTE[index % len(PALETTE)]
                self.create_rectangle(left, y + 2, left + 10, y + 12,
                                      fill=color, outline=color)
                self.create_text(left + 18, y + 7, text=_ellipsis(name, 16), anchor="w",
                                 fill=FG, font=self.font_small)
                share = seconds / total * 100
                self.create_text(left + 210, y + 7,
                                 text=f"{fmt_duration(seconds, with_seconds=False)}　{share:.0f}%",
                                 anchor="w", fill=SUB, font=self.font_small)
        except tk.TclError:
            log.debug("绘制对象占比环图失败（窗口可能已销毁）", exc_info=True)


class DonutPanel(ttk.Frame):
    """统计页下半屏：标题 + 环图 + 一句话汇总。"""

    def __init__(self, master: tk.Misc, font_family: str = "Microsoft YaHei UI",
                 **kwargs) -> None:
        kwargs.setdefault("style", "Card.TFrame")
        super().__init__(master, **kwargs)
        self.font_family = font_family
        self.font_small = (font_family, 9)
        ttk.Label(self, text="🍩 今日对象占比", style="CardTitle.TLabel").pack(
            anchor="w", pady=(6, 2))
        self.donut = ObjectDonut(self, font_family=font_family, width=840, height=180)
        self.donut.pack(fill="both", expand=True)
        self.summary_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.summary_var, style="CardHint.TLabel").pack(
            anchor="w", pady=(2, 2))

    def refresh(self, stats: list) -> None:
        """重画环图并写一句"最大头是谁"。"""
        self.donut.set_data(stats)
        items = self.donut.items
        if not items:
            self.summary_var.set("今天还没有监控数据 —— 被监控的程序一旦在前台就会出现在这里")
            return
        total = self.donut.total() or 1.0
        name, seconds = items[0]
        self.summary_var.set(
            f"共 {len(items)} 个对象，合计 {fmt_duration(total, with_seconds=False)}；"
            f"占得最多的是「{name}」（{seconds / total * 100:.0f}%）")


def _ellipsis(text: str, limit: int) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"

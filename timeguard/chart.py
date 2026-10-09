"""matplotlib 图表：过去 7 天使用时长柱状图（嵌入 tkinter）。

* 没有任何数据时显示占位提示；
* 自动选择支持中文的字体，避免中文变成方块；
* 图表配色与主界面深色主题保持一致。
"""

from __future__ import annotations

import logging
import tkinter as tk
from tkinter import font as tkfont

from .utils import fmt_duration_short, last_n_days, weekday_cn

log = logging.getLogger(__name__)

# 深色主题配色（与 ui.py 保持一致）
BG = "#151922"
PANEL = "#1b2130"
FG = "#e6eaf2"
SUB = "#8b95a8"
BAR = "#4f8cff"
BAR_OVER = "#ff6b6b"
GRID = "#2a3143"

#: 优先使用的中文字体
CJK_CANDIDATES = [
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "微软雅黑",
    "SimHei",
    "Noto Sans CJK SC",
    "Source Han Sans SC",
    "PingFang SC",
    "sans-serif",
]


def pick_cjk_font() -> str:
    """挑选一个系统中真实存在的中文字体。"""
    try:
        available = {name.lower() for name in tkfont.families()}
    except tk.TclError:
        available = set()
    for name in CJK_CANDIDATES:
        if name.lower() in available:
            return name
    return "TkDefaultFont"


def _configure_matplotlib_font() -> None:
    """把中文字体注入 matplotlib，避免图表中文乱码。"""
    try:
        import matplotlib
        from matplotlib import font_manager

        matplotlib.rcParams["axes.unicode_minus"] = False
        installed = {f.name.lower() for f in font_manager.fontManager.ttflist}
        for name in CJK_CANDIDATES:
            if name.lower() in installed:
                matplotlib.rcParams["font.sans-serif"] = [name]
                matplotlib.rcParams["font.family"] = "sans-serif"
                return
    except Exception:  # noqa: BLE001
        log.debug("配置 matplotlib 中文字体失败", exc_info=True)


class WeekChart(tk.Frame):
    """7 天使用时长柱状图控件。

    对外接口：
    * :meth:`refresh` —— 用最新数据重绘；
    * :meth:`set_placeholder` —— 显示占位文字（无数据时）。
    """

    def __init__(self, master: tk.Misc, store, font_family: str = "Microsoft YaHei UI", **kwargs) -> None:
        super().__init__(master, bg=PANEL, **kwargs)
        self.store = store
        self.font_family = font_family
        self._canvas = None
        self._figure = None
        self._placeholder: tk.Label | None = None
        self._build()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        _configure_matplotlib_font()
        try:
            import matplotlib

            matplotlib.use("TkAgg")
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
            from matplotlib.figure import Figure
        except Exception as exc:  # noqa: BLE001 - 没装 matplotlib 时降级为文字提示
            log.warning("matplotlib 不可用，图表功能降级: %s", exc)
            self.show_message("未安装 matplotlib，无法显示图表。\n请执行：pip install matplotlib")
            return

        self._figure = Figure(figsize=(6.0, 3.0), dpi=100, facecolor=PANEL)
        self._ax = self._figure.add_subplot(111)
        self._style_axes()
        canvas = FigureCanvasTkAgg(self._figure, master=self)
        canvas.draw()
        canvas.get_tk_widget().configure(bg=PANEL, highlightthickness=0)
        canvas.get_tk_widget().pack(fill="both", expand=True, padx=6, pady=6)
        self._canvas = canvas
        # 让图表尺寸跟随控件实际大小（窗口拉伸 / 高 DPI 缩放都不会裁掉坐标轴）
        self.bind("<Configure>", self._on_resize)

    def _style_axes(self) -> None:
        """统一图表样式（深色背景、无上/右边框、留足坐标轴边距）。"""
        ax = self._ax
        ax.set_facecolor(PANEL)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(GRID)
        ax.tick_params(colors=SUB, labelsize=9)
        ax.grid(axis="y", color=GRID, linestyle="--", linewidth=0.6, alpha=0.7)
        ax.set_axisbelow(True)
        # 用固定边距代替 tight_layout：紧凑控件里 tight_layout 容易报
        # “margins cannot be made large enough” 警告并裁掉坐标轴文字
        self._figure.subplots_adjust(left=0.11, right=0.985, top=0.82, bottom=0.19)

    def _on_resize(self, event: tk.Event) -> None:
        """控件尺寸变化时同步 matplotlib 画布尺寸。"""
        if self._figure is None or self._canvas is None:
            return
        width = max(240, event.width - 12) / float(self._figure.dpi)
        height = max(160, event.height - 12) / float(self._figure.dpi)
        current_w, current_h = self._figure.get_size_inches()
        if abs(width - current_w) < 0.05 and abs(height - current_h) < 0.05:
            return
        try:
            self._figure.set_size_inches(width, height)
            self._canvas.draw_idle()
        except Exception:  # noqa: BLE001
            log.debug("调整图表尺寸失败", exc_info=True)

    # ------------------------------------------------------------------
    def show_message(self, text: str) -> None:
        """用一个居中的文字标签替代图表（例如缺少依赖）。"""
        if self._placeholder is not None:
            self._placeholder.configure(text=text)
            return
        self._placeholder = tk.Label(
            self, text=text, bg=PANEL, fg=SUB, font=(self.font_family, 11), justify="center"
        )
        self._placeholder.pack(fill="both", expand=True)

    def hide_message(self) -> None:
        """隐藏占位文字。"""
        if self._placeholder is not None:
            self._placeholder.pack_forget()

    # ------------------------------------------------------------------
    def refresh(self, days: int = 7, limit_seconds: float | None = None) -> None:
        """重新查询数据库并绘制柱状图。

        :param days: 天数（默认 7）
        :param limit_seconds: 每日上限（秒），用于把超限的柱子标红并画参考线
        """
        if self._canvas is None:
            return
        stats = self.store.recent_days(days)
        if not stats:
            self.show_message("暂无数据")
            return
        self.hide_message()

        labels = [f"{s.day[5:]}\n{weekday_cn(s.day)}" for s in stats]
        values = [s.seconds for s in stats]
        today = stats[-1].day if stats else ""

        ax = self._ax
        ax.clear()
        self._style_axes()

        colors = []
        for s in stats:
            if limit_seconds and s.seconds >= limit_seconds:
                colors.append(BAR_OVER)
            elif s.day == today:
                colors.append("#7fb0ff")
            else:
                colors.append(BAR)

        x = range(len(labels))
        bars = ax.bar(x, values, color=colors, width=0.55, zorder=3)
        ax.set_xticks(list(x))
        ax.set_xticklabels(labels, color=SUB, fontsize=9)

        # Y 轴用“分钟”更直观
        max_value = max(values) if values else 0
        y_max = max(max_value, (limit_seconds or 0)) * 1.25
        ax.set_ylim(0, max(y_max, 60))
        ax.set_yticks([t for t in ax.get_yticks() if t >= 0])
        ax.set_yticklabels(
            [fmt_duration_short(t) for t in ax.get_yticks()], color=SUB, fontsize=9
        )

        # 每日上限参考线（标注放右上角空白处，避免压住柱顶数值）
        if limit_seconds:
            ax.axhline(limit_seconds, color="#ff9f43", linestyle="--", linewidth=1.0, zorder=4)
            ax.annotate(
                f"每日上限 {fmt_duration_short(limit_seconds)}",
                xy=(len(labels) - 0.62, limit_seconds), xytext=(0, 4), textcoords="offset points",
                color="#ff9f43", fontsize=8, ha="right", va="bottom",
                bbox=dict(boxstyle="round,pad=0.18", facecolor=PANEL, edgecolor="none", alpha=0.85),
            )

        # 柱顶数值
        for rect, value in zip(bars, values):
            if value <= 0:
                continue
            ax.annotate(
                fmt_duration_short(value),
                xy=(rect.get_x() + rect.get_width() / 2, value),
                xytext=(0, 3), textcoords="offset points",
                ha="center", color=FG, fontsize=8,
            )

        ax.set_title("最近 7 天使用时长", color=FG, fontsize=11, pad=8)
        try:
            self._canvas.draw_idle()
        except Exception:  # noqa: BLE001
            log.debug("刷新图表失败", exc_info=True)

    def destroy(self) -> None:  # pragma: no cover - tkinter 生命周期
        """销毁控件时关闭 matplotlib 图形，释放内存。"""
        try:
            if self._figure is not None:
                import matplotlib.pyplot as plt

                plt.close(self._figure)
        except Exception:  # noqa: BLE001
            pass
        super().destroy()


def seven_day_labels(days: int = 7) -> list[str]:
    """返回 ``["07-01 周一", ...]`` 形式的日期标签（供其他界面复用）。"""
    return [f"{d[5:]} {weekday_cn(d)}" for d in last_n_days(days)]

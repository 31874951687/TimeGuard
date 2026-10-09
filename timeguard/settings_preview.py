"""窗口尺寸预设与「实时预览」控件。

从 ``ui.py`` 抽出来单独成模块，方便设置页（``settings_page``）复用，
也避免 ui.py 与 settings_page 互相 import 造成循环依赖。
"""

from __future__ import annotations

import logging
import tkinter as tk

from . import winapi

log = logging.getLogger(__name__)

# ---- 配色（与主界面一致）----
BG = "#10141c"
PANEL = "#1b2130"
FG = "#e6eaf2"
SUB = "#8b95a8"
ACCENT = "#4f8cff"
AMBER = "#ffb020"

#: 窗口尺寸预设：名称 -> (宽, 高)
WINDOW_PRESETS: dict[str, tuple[int, int]] = {
    "紧凑（1020 × 700）": (1020, 700),
    "标准（1020 × 800）": (1020, 800),
    "大（1180 × 900）": (1180, 900),
    "特大（1320 × 1000）": (1320, 1000),
}

#: 「占满屏幕可用区域」与「自定义」不是固定尺寸，单列出来做判断
PRESET_FULLSCREEN = "占满屏幕可用区域"
PRESET_CUSTOM = "自定义"


def preset_names() -> list[str]:
    """预设下拉里的全部选项（含占满屏幕 / 自定义）。"""
    return [*WINDOW_PRESETS.keys(), PRESET_FULLSCREEN, PRESET_CUSTOM]


def match_preset(width: int, height: int) -> str:
    """按当前宽高反查预设名称。"""
    for name, size in WINDOW_PRESETS.items():
        if size == (int(width), int(height)):
            return name
    return PRESET_CUSTOM


def _mix(c1: str, c2: str, t: float) -> str:
    """按比例混合两个 #RRGGBB 颜色。"""
    t = max(0.0, min(1.0, t))
    a = tuple(int(c1[i : i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(c2[i : i + 2], 16) for i in (1, 3, 5))
    return "#" + "".join(f"{int(a[i] + (b[i] - a[i]) * t):02x}" for i in range(3))


class WindowSizePreview(tk.Canvas):
    """窗口尺寸预览：按真实比例画出「屏幕可用区域」和「主窗口」两个矩形。

    会标出实际生效的像素尺寸；若设置值超出可用区域，用橙色提示"已自动收缩"。
    """

    def __init__(self, master: tk.Misc, font_family: str, on_size_change=None,
                 width: int = 420, height: int = 210, **kwargs) -> None:
        super().__init__(master, width=width, height=height, bg=PANEL, highlightthickness=0,
                         bd=0, **kwargs)
        self.font_family = font_family
        self.on_size_change = on_size_change
        self._width = width
        self._height = height
        self._want = (1020, 800)

    def set_size(self, width: int, height: int) -> None:
        """设置要预览的目标尺寸并重绘。"""
        self._want = (max(640, int(width)), max(460, int(height)))
        self.redraw()

    def redraw(self) -> None:
        """按真实比例重绘预览。"""
        self.delete("all")
        work_w, work_h = winapi.get_work_area(self)
        pad = 10
        area_w = max(60, self._width - pad * 2)
        area_h = max(40, self._height - pad * 2 - 18)
        ratio = min(area_w / work_w, area_h / work_h)
        screen_w, screen_h = work_w * ratio, work_h * ratio
        ox = pad + (area_w - screen_w) / 2
        oy = pad + (area_h - screen_h) / 2

        # 屏幕可用区域
        self.create_rectangle(ox, oy, ox + screen_w, oy + screen_h,
                              outline=SUB, fill="#161b26", width=1)
        self.create_text(ox + 6, oy + 10, anchor="w", fill=SUB, font=(self.font_family, 8),
                         text=f"屏幕可用区域 {work_w}×{work_h}")

        # 实际生效的窗口（会按可用区域收缩）
        want_w, want_h = self._want
        final_w = min(want_w, work_w - 24)
        final_h = min(want_h, work_h - 24)
        clamped = (final_w, final_h) != (want_w, want_h)
        win_w, win_h = final_w * ratio, final_h * ratio
        wx = ox + (screen_w - win_w) / 2
        wy = oy + (screen_h - win_h) / 2
        color = AMBER if clamped else ACCENT
        self.create_rectangle(wx, wy, wx + win_w, wy + win_h,
                              outline=color, fill=_mix(color, BG, 0.82), width=2)
        label = f"主窗口 {final_w}×{final_h}"
        if clamped:
            label += "（已按屏幕收缩）"
        self.create_text(wx + win_w / 2, wy + win_h / 2, fill=FG,
                         font=(self.font_family, 9, "bold"), text=label)
        self.create_text(ox + 6, oy + screen_h - 2, anchor="sw", fill=SUB,
                         font=(self.font_family, 8), text="窗口会居中显示，不会被任务栏遮挡")
        if callable(self.on_size_change):
            try:
                self.on_size_change(final_w, final_h, clamped)
            except Exception:  # noqa: BLE001
                log.debug("预览回调异常", exc_info=True)

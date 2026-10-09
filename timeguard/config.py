"""用户设置：数据模型 + JSON 持久化。

所有可调参数集中在这里，界面（设置面板）只负责改动 :class:`AppSettings`，
监控引擎每个循环都会读取最新值，因此修改设置**无需重启**即可生效。
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, ClassVar, get_type_hints

from . import paths
from .utils import read_json, write_json_atomic

log = logging.getLogger(__name__)

#: 默认识别的浏览器进程名（包含国产浏览器常见进程名）
DEFAULT_BROWSERS: list[str] = [
    "chrome.exe",       # Google Chrome
    "msedge.exe",       # Microsoft Edge
    "firefox.exe",      # Firefox
    "brave.exe",        # Brave
    "opera.exe",
    "vivaldi.exe",
    "360se.exe",        # 360 安全浏览器
    "360chrome.exe",    # 360 极速浏览器
    "sogouexplorer.exe",  # 搜狗浏览器
    "qqbrowser.exe",    # QQ 浏览器
    "maxthon.exe",      # 傲游浏览器
    "liebao.exe",       # 猎豹浏览器
]

#: 默认监控的游戏 / 软件进程名（示例，用户可在界面自行增删）
DEFAULT_GAMES: list[str] = [
    "game.exe",
    "steam.exe",
    "League of Legends.exe",
    "LimbusCompany.exe",   # 林布斯公司（Limbus Company）
    "GenshinImpact.exe",
    "Yuanshen.exe",
    "StarRail.exe",
    "dota2.exe",
    "cs2.exe",
    "Minecraft.Windows.exe",
    "原神.exe",
]

#: 默认的网页标题关键字
DEFAULT_KEYWORDS: list[str] = [
    "哔哩哔哩",
    "哔哩哔哩 (゜-゜)つロ 干杯~",
    "bilibili",
    "抖音",
    "douyin",
    "YouTube",
    "youtube.com",
    "微博",
    "weibo",
    "小红书",
    "xiaohongshu",
    "知乎",
    "斗鱼",
    "虎牙",
]


@dataclass
class AppSettings:
    """全部用户设置（可直接 JSON 序列化）。"""

    # ---- 目标与提醒 ----
    daily_limit_minutes: int = 120          # 每日总时长上限（分钟），默认 2 小时
    reminder_interval_minutes: int = 30     # 超时后重复提醒的间隔（分钟）
    remind_immediately_on_exceed: bool = True  # 刚超过上限时是否立刻提醒一次
    count_mode: str = "total"               # total=所有监控对象合并计时；each=每个对象单独计时
    monitor_interval_seconds: float = 1.0   # 前台窗口采样间隔（秒）
    pause_seconds: float = 60.0             # 检测到锁屏/休眠/长时间无变化时的丢弃阈值

    # ---- 监控对象 ----
    game_processes: list[str] = field(default_factory=lambda: list(DEFAULT_GAMES))
    browser_processes: list[str] = field(default_factory=lambda: list(DEFAULT_BROWSERS))
    title_keywords: list[str] = field(default_factory=lambda: list(DEFAULT_KEYWORDS))
    match_path_contains: bool = True        # 除进程名外，是否再匹配完整映像路径

    # ---- 行为开关 ----
    enable_toast: bool = True               # 是否发送 Windows 右下角通知
    enable_popup: bool = True               # 是否弹出强制提醒窗口
    enable_sound: bool = True               # 弹窗时播放提示音
    suppress_popup_fullscreen: bool = True  # 全屏游戏时暂缓弹窗（退出全屏后补发）
    start_minimized: bool = False           # 启动后直接最小化到托盘
    auto_start_monitor: bool = True         # 启动后立即开始计时

    # ---- 待办任务 ----
    task_check_on_start: bool = True        # 开机 / 首次运行时检查未完成任务并弹窗提醒
    task_reminder_toast: bool = True        # 临近截止时是否发 Windows 右下角通知
    task_notify_within_hours: float = 24.0  # 截止时间不足这么多小时才发 Windows 通知
    task_notify_interval_hours: float = 6.0  # 同一任务两次 Windows 通知的最小间隔（小时）
    autostart_enabled: bool = False         # 是否写入注册表开机自启（由设置页开关控制）

    #: 「开机提醒」弹窗最近一次显示的日期（``YYYY-MM-DD``）。
    #: 弹窗**每天只在首次启动软件时**显示一次，同一天再启动就不弹了
    #: （错过的周期任务照样会在后台派发，只是不再重复打扰）。
    startup_dialog_date: str = ""

    #: 「最小化到托盘」的通知是否已经提示过。只提示一次，之后不再自动弹
    #: （托盘图标随时可以打开窗口，提示本身也把这句话说明白了）。
    tray_notice_shown: bool = False

    # ---- 周期性任务的提醒调度 ----
    recurring_reminder_enabled: bool = True   # 是否启用"开始前 N 分钟"提醒
    recurring_late_catchup: bool = True       # 开机时补发今天错过的周期任务提醒
    recurring_catchup_minutes: int = 5        # 补发窗口：错过开始时间后多久内仍补发一次
    scheduler_max_sleep_minutes: int = 30     # 定时器单次最长睡眠（防止休眠/改时钟导致漂移）

    # ---- 界面 ----
    window_width: int = 1020                # 主窗口宽度（像素，实际会按屏幕可用区域收缩）
    window_height: int = 800                # 主窗口高度（像素）

    # ---- 内部状态（不建议手工修改） ----
    popup_cooldown_seconds: int = 60        # 两次强制弹窗之间的最短间隔，防止弹窗轰炸

    # ------------------------------------------------------------------
    #: 数值字段的取值范围（防止 config.json 被手改成离谱值导致调度器/界面异常）
    INT_RANGES: ClassVar[dict[str, tuple[int, int]]] = {
        "daily_limit_minutes": (1, 1440),
        "reminder_interval_minutes": (1, 240),
        "task_notify_within_hours": (0.5, 168.0),   # 兼容旧版 float 存法
        "task_notify_interval_hours": (0.5, 72.0),
        "popup_cooldown_seconds": (0, 3600),
        "window_width": (640, 7680),
        "window_height": (480, 4320),
        "recurring_catchup_minutes": (1, 240),
        "scheduler_max_sleep_minutes": (1, 240),
    }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppSettings":
        """从 dict 构造，忽略未知键、按**声明类型**强制转换（保证向后兼容）。

        为什么不能只看值的运行时类型：JSON 里 ``"recurring_catchup_minutes": "五分钟"``
        或 ``true`` 都会被原样存进来，后面 ``int()`` 一抛异常，调度器就断链停摆
        （整个设计不轮询，异常后没有兜底）。所以这里按字段声明类型转换 + 丢坏值。
        """
        valid = {f.name: f for f in fields(cls)}
        # 注意：本模块用了 `from __future__ import annotations`，所以 field.type 是
        # **字符串**（'int' / 'bool' / 'list[str]'），不能直接 `is int` 比较。
        # 统一解析成真实类型对象，否则所有强制转换都会静默失效（踩过这个坑）。
        try:
            hints = get_type_hints(cls)
        except Exception:  # noqa: BLE001 - 解析失败就退回默认，至少不崩
            hints = {}
        kwargs: dict[str, Any] = {}
        for key, value in (data or {}).items():
            if key not in valid:
                continue
            target = hints.get(key, valid[key].type)
            try:
                if target is bool:
                    if isinstance(value, str):
                        text = value.strip().lower()
                        if text in ("true", "1", "yes", "on", "是"):
                            kwargs[key] = True
                        elif text in ("false", "0", "no", "off", "否"):
                            kwargs[key] = False
                        else:
                            continue
                    elif isinstance(value, (bool, int, float)):
                        kwargs[key] = bool(value)
                    continue
                if target is int:
                    number = int(float(value))       # 容忍 "5" / 5.0 / True
                elif target is float:
                    number = float(value)
                elif target is str:
                    kwargs[key] = str(value)
                    continue
                elif str(target).startswith("list"):
                    if not isinstance(value, list):
                        continue
                    kwargs[key] = [str(v).strip() for v in value if str(v).strip()]
                    continue
                else:
                    continue
            except (TypeError, ValueError):
                log.warning("配置项 %s 的值 %r 无法转换为 %s，已忽略", key, value, target)
                continue
            span = cls.INT_RANGES.get(key)
            if span is not None:
                low, high = span
                clamped = max(low, min(high, number))
                if clamped != number:
                    log.warning("配置项 %s 的值 %r 超出范围 %s，已夹取为 %r",
                                key, number, span, clamped)
                number = clamped
            kwargs[key] = int(number) if target is int else float(number)
        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的 dict。"""
        return asdict(self)

    # ---- 便捷换算 ----
    @property
    def daily_limit_seconds(self) -> float:
        """每日上限（秒）。"""
        return max(1, int(self.daily_limit_minutes)) * 60.0

    @property
    def reminder_interval_seconds(self) -> float:
        """重复提醒间隔（秒）。"""
        return max(1, int(self.reminder_interval_minutes)) * 60.0

    # ---- 列表增删查重（界面用） ----
    def add_game(self, name: str) -> bool:
        return _add_unique(self.game_processes, name)

    def remove_game(self, name: str) -> bool:
        return _remove(self.game_processes, name)

    def add_keyword(self, name: str) -> bool:
        return _add_unique(self.title_keywords, name)

    def remove_keyword(self, name: str) -> bool:
        return _remove(self.title_keywords, name)


def _add_unique(items: list[str], value: str) -> bool:
    value = (value or "").strip()
    if not value:
        return False
    if any(v.lower() == value.lower() for v in items):
        return False
    items.append(value)
    return True


def _remove(items: list[str], value: str) -> bool:
    before = len(items)
    items[:] = [v for v in items if v.lower() != (value or "").strip().lower()]
    return len(items) != before


class Config:
    """配置文件的读写封装（线程安全由调用方保证，写操作使用原子替换）。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else paths.config_file()
        self.settings = AppSettings()
        self.load()

    def load(self) -> AppSettings:
        """从磁盘加载；首次运行会写入一份默认配置便于用户查看/编辑。"""
        raw = read_json(self.path, default=None)
        if raw is None:
            self.settings = AppSettings()
            self.save()
        else:
            self.settings = AppSettings.from_dict(raw)
        return self.settings

    def save(self) -> None:
        """保存到磁盘（原子写入）。"""
        try:
            write_json_atomic(self.path, self.settings.to_dict())
        except OSError as exc:  # 磁盘只读等情况不应让程序崩溃
            log.warning("保存配置失败: %s", exc)

    def update(self, **kwargs: Any) -> None:
        """批量更新字段并保存。"""
        for key, value in kwargs.items():
            if hasattr(self.settings, key):
                setattr(self.settings, key, value)
        self.save()

    def reset_defaults(self) -> AppSettings:
        """恢复默认设置并保存。"""
        self.settings = AppSettings()
        self.save()
        return self.settings

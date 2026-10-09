"""监控与计时引擎（运行在后台线程，绝不阻塞 tkinter 主线程）。

计时规则
--------
* 每 ``monitor_interval`` 秒采样一次 **前台窗口**；
* 只有当“前台窗口属于被监控对象”且“窗口未最小化”且“未锁屏”且
  “距上次采样时间差正常（排除休眠/待机）”时，这段时间才被累计；
* 因此切到别的软件、最小化窗口、锁屏、休眠都会自动暂停计时；
* 计时增量每 5 秒落库一次，程序被强杀最多丢几秒数据。

线程模型
--------
引擎线程只做“采样 → 判断 → 累加 → 落库 → 触发提醒”；
所有界面操作（弹窗、刷新 UI）通过回调 / 队列交回主线程处理。
"""

from __future__ import annotations

import fnmatch
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from . import winapi
from .config import AppSettings, Config
from .database import UsageStore
from .notifier import Notifier, PopupManager, reminder_text
from .utils import fmt_duration, today_str

log = logging.getLogger(__name__)

FLUSH_INTERVAL = 5.0        # 计时增量落库间隔（秒）
SLEEP_GAP_THRESHOLD = 8.0   # 两次采样间隔超过该值视为休眠/挂起，丢弃该时间段
PARENT_LOOKUP_DEPTH = 4     # 向父进程回溯的层数（启动器 -> 游戏本体）


@dataclass
class Target:
    """一次命中：当前正在被计时的监控对象。"""

    kind: str           # "game" | "web"
    name: str           # 展示名，例如 "哔哩哔哩" / "game.exe"
    exe: str = ""       # 命中时的进程名
    title: str = ""     # 命中时的窗口标题（已截断）
    family: str = "process"  # 图表归并用的类别名

    @property
    def kind_cn(self) -> str:
        return "游戏/软件" if self.kind == "game" else "网页"


@dataclass
class Snapshot:
    """供界面线程读取的运行时状态快照。"""

    day: str = ""
    total_seconds: float = 0.0                       # 今日累计（监控对象合计）
    current: Target | None = None
    session_seconds: float = 0.0                     # 当前这一次连续使用时长
    target_seconds: dict[str, float] = field(default_factory=dict)   # 今日各对象时长
    limit_seconds: float = 0.0
    remaining_seconds: float = 0.0                   # 距离上限还剩多少（负数表示已超）
    over_limit: bool = False
    monitoring: bool = False
    next_popup_in: float = 0.0                       # 距离下次提醒还有多久
    popup_count: int = 0
    fullscreen: bool = False
    foreground_exe: str = ""
    foreground_title: str = ""
    matched: bool = False

    @property
    def ratio(self) -> float:
        """已用时长 / 上限（0~1+）。"""
        if self.limit_seconds <= 0:
            return 0.0
        return self.total_seconds / self.limit_seconds


class UsageEngine:
    """使用时长监控引擎。"""

    def __init__(
        self,
        config: Config,
        store: UsageStore,
        notifier: Notifier,
        on_limit_exceeded: Callable[[Target, float, str, str, str], None] | None = None,
        on_state_change: Callable[[Snapshot], None] | None = None,
        on_day_rollover: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.notifier = notifier
        self.on_limit_exceeded = on_limit_exceeded
        self.on_state_change = on_state_change
        self.on_day_rollover = on_day_rollover

        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()  # set 表示暂停计时（只看不记）

        self._day: str = today_str()
        self._total: float = self.store.total_seconds(self._day)
        self._target_seconds: dict[str, float] = {t.name: t.seconds for t in self.store.today_targets(self._day)}

        self._current: Target | None = None
        self._session_start: float = 0.0
        # 初始化时就给出时间基准：即使还没调用 start()，第一次 tick 也不会
        # 因为 delta = now - 0 被视为“休眠恢复”而丢掉整段时间
        self._last_tick: float = time.monotonic()
        self._pending: dict[str, float] = {}       # 尚未落库的增量 {对象: 秒数}
        self._pending_kind: dict[str, str] = {}    # 落库时需要的对象类型 {对象: kind}
        self._last_flush: float = 0.0
        self._last_popup: float = 0.0
        self._over_limit_seen: bool = False   # 今天是否已经越过上限（用于“首次超限立即提醒”）
        self._last_fg: winapi.ForegroundInfo | None = None
        self._resolve_cache: dict[int, tuple[float, Target | None]] = {}
        self._popup_manager: PopupManager | None = None
        self._error_count = 0

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        """启动监控线程（幂等）。"""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._last_tick = time.monotonic()
            self._last_flush = self._last_tick
            self._thread = threading.Thread(target=self._run, name="TimeGuardEngine", daemon=True)
            self._thread.start()
            log.info("监控引擎已启动")

    def stop(self, timeout: float = 3.0) -> None:
        """停止监控线程并落库剩余增量。"""
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
        self.flush()
        log.info("监控引擎已停止")

    @property
    def running(self) -> bool:
        """监控线程是否存活。"""
        return bool(self._thread and self._thread.is_alive() and not self._stop_event.is_set())

    @property
    def paused(self) -> bool:
        """是否处于暂停计时状态。"""
        return self._pause_event.is_set()

    def toggle_pause(self, paused: bool | None = None) -> bool:
        """暂停 / 继续计时；返回暂停后的状态。"""
        if paused is None:
            paused = not self._pause_event.is_set()
        if paused:
            self._pause_event.set()
            self.flush()          # 暂停时立刻把已计时数据落库
            self._current = None
            self._session_start = 0.0
        else:
            self._pause_event.clear()
            self._last_tick = time.monotonic()
        log.info("计时%s", "已暂停" if paused else "已恢复")
        return paused

    def attach_popup_manager(self, manager: PopupManager) -> None:
        """绑定弹窗管理器（由界面创建后注入）。"""
        self._popup_manager = manager

    # ------------------------------------------------------------------ 主循环
    def _run(self) -> None:
        while not self._stop_event.is_set():
            settings = self.config.settings
            interval = max(0.2, float(settings.monitor_interval_seconds))
            started = time.monotonic()
            try:
                self._tick(settings)
                self._error_count = 0
            except Exception:  # noqa: BLE001 - 监控循环必须永不退出
                self._error_count += 1
                log.exception("监控循环异常（第 %d 次）", self._error_count)
                time.sleep(min(5.0, 0.5 * self._error_count))
            elapsed = time.monotonic() - started
            self._stop_event.wait(max(0.05, interval - elapsed))

    # ------------------------------------------------------------------ 单次采样
    def tick(self, settings: AppSettings | None = None, info: winapi.ForegroundInfo | None = None) -> Snapshot:
        """执行一次采样（``info`` 为空时真实读取前台窗口）。

        把 ``now`` / ``settings`` / ``info`` 设计成可注入，是为了让核心计时逻辑
        能够被单元测试稳定覆盖（见 tests/test_core.py）。
        """
        settings = settings or self.config.settings
        now = time.monotonic()
        delta = now - self._last_tick
        self._last_tick = now

        # ---- 1. 跨天处理 ----
        today = today_str()
        if today != self._day:
            self.flush()
            old_day = self._day
            self._day = today
            self._total = 0.0
            self._target_seconds.clear()
            self._current = None
            self._last_popup = 0.0
            self._over_limit_seen = False
            if self._popup_manager:
                self._popup_manager.reset_daily()
            if callable(self.on_day_rollover):
                try:
                    self.on_day_rollover(old_day)
                except Exception:  # noqa: BLE001
                    log.debug("跨天回调异常", exc_info=True)
            log.info("已跨天，统计重置为 %s", today)

        # ---- 2. 采样前台窗口 ----
        if info is None:
            info = winapi.sample_foreground(need_path=settings.match_path_contains)
        self._last_fg = info
        target = self._resolve(info, settings)

        # ---- 3. 判断这段时间是否计入 ----
        counting = (
            target is not None
            and not self._pause_event.is_set()
            and 0 < delta <= SLEEP_GAP_THRESHOLD
        )
        counted = delta if counting else 0.0

        if counted > 0:
            self._accumulate(target, counted, now)
        else:
            # 目标消失时结束当前会话
            if self._current is not None and (target is None or target.name != self._current.name):
                self.flush()
                self._current = None
                self._session_start = 0.0
            if target is not None and self._current is None:
                self._current = target
                self._session_start = now
                log.info("开始计时: %s (%s)", target.name, target.exe)

        # ---- 4. 定期落库 ----
        if now - self._last_flush >= FLUSH_INTERVAL:
            self.flush()

        # ---- 5. 提醒判断 ----
        self._check_limits(target, settings, info)

        # ---- 6. 通知界面 ----
        snapshot = self.snapshot()
        if callable(self.on_state_change):
            try:
                self.on_state_change(snapshot)
            except Exception:  # noqa: BLE001
                log.debug("界面回调异常", exc_info=True)
        return snapshot

    #: 内部循环调用的名字（保持私有语义）
    _tick = tick

    def _accumulate(self, target: Target, seconds: float, now: float) -> None:
        """累加时长到内存与数据库待写缓冲。"""
        if self._current is None or self._current.name != target.name:
            # 目标切换：先把上一段落库
            self.flush()
            self._current = target
            self._session_start = now
            log.info("开始计时: %s (%s)", target.name, target.exe)

        with self._lock:
            self._total += seconds
            self._target_seconds[target.name] = self._target_seconds.get(target.name, 0.0) + seconds
            self._pending[target.name] = self._pending.get(target.name, 0.0) + seconds
            self._pending_kind[target.name] = target.kind  # 记录类型，落库时用

    # ------------------------------------------------------------------ 落库
    def flush(self) -> None:
        """把未落库的计时增量写入 SQLite。"""
        with self._lock:
            pending = self._pending
            kinds = self._pending_kind
            self._pending = {}
            self._pending_kind = {}
            day = self._day
        for name, seconds in pending.items():
            if seconds > 0:
                self.store.add_seconds(name, seconds, kind=kinds.get(name, "process"), day=day)
        self._last_flush = time.monotonic()

    # ------------------------------------------------------------------ 匹配
    def _resolve(self, info: winapi.ForegroundInfo, settings: AppSettings) -> Target | None:
        """把前台窗口解析成监控对象；不匹配则返回 None。"""
        if not info.exe or info.locked or info.minimized:
            # 锁屏 / 最小化 / 没有进程：无论缓存如何都不计时
            return None

        now = time.monotonic()
        cached = self._resolve_cache.get(info.pid)
        if cached and now - cached[0] < 60.0:
            target = cached[1]
            if target is None:
                return None
            if target.exe.lower() != info.exe.lower():
                # 同一 PID 换了可执行文件（罕见），缓存作废
                self._resolve_cache.pop(info.pid, None)
            else:
                # 缓存命中同一个进程时，网页对象仍要用当前标题复核关键字
                # （例如 Chrome 把标签页从 B 站切到别的网站，就应停止计时）
                if target.kind == "web":
                    keyword = self._match_keyword(info.title, settings)
                    if not keyword:
                        return None
                    if keyword != target.name:
                        return Target(
                            kind="web", name=keyword, exe=info.exe,
                            title=self._short_title(info.title), family=f"{keyword}（网页）",
                        )
                return target

        target = self._resolve_uncached(info, settings)
        self._resolve_cache[info.pid] = (now, target)
        if len(self._resolve_cache) > 256:
            self._resolve_cache.clear()
        return target

    def _resolve_uncached(self, info: winapi.ForegroundInfo, settings: AppSettings) -> Target | None:
        """不做缓存的对象识别（锁屏 / 最小化 / 无进程一律不计时）。"""
        if info.locked or info.minimized or not info.exe:
            return None

        # 1) 优先判断游戏 / 软件进程（含父进程，处理启动器场景）
        for exe, path in self._process_chain(info):
            if self._match_process(exe, path, settings.game_processes, settings):
                return Target(kind="game", name=exe, exe=exe, title=self._short_title(info.title), family=exe)

        # 2) 再判断浏览器网页（进程名是浏览器 + 标题命中关键字）
        for exe, path in self._process_chain(info):
            if self._match_process(exe, path, settings.browser_processes, settings) or exe.lower() in (
                "chrome.exe", "msedge.exe", "firefox.exe",
            ):
                keyword = self._match_keyword(info.title, settings)
                if keyword:
                    return Target(
                        kind="web", name=keyword, exe=exe,
                        title=self._short_title(info.title), family=f"{keyword}（网页）",
                    )
        return None

    @staticmethod
    def _process_chain(info: winapi.ForegroundInfo) -> list[tuple[str, str]]:
        """返回 [(进程名, 路径), ...]，第一项是窗口进程本身，后面是其父进程。"""
        chain: list[tuple[str, str]] = [(info.exe, info.path)]
        try:
            import psutil

            proc = psutil.Process(info.pid)
            for _ in range(PARENT_LOOKUP_DEPTH):
                parent = proc.parent()
                if parent is None:
                    break
                try:
                    parent_name = parent.name()
                    parent_path = ""
                    try:
                        parent_path = parent.exe()
                    except Exception:  # noqa: BLE001
                        parent_path = ""
                except Exception:  # noqa: BLE001
                    break
                if not parent_name or any(parent_name.lower() == n.lower() for n, _ in chain):
                    break
                chain.append((parent_name, parent_path))
                proc = parent
        except Exception:  # noqa: BLE001 - 无权限等，退化为仅窗口进程
            pass
        return chain

    @staticmethod
    def _match_process(exe: str, path: str, names: list[str], settings: AppSettings) -> bool:
        """进程是否命中白名单。

        匹配规则（都忽略大小写）：
        1. 用户填的条目就是可执行文件名时，要求 **完全相同**；
        2. 用户填的是完整路径（含反斜杠）时，与运行中的完整映像路径比较；
        3. 允许用 ``*.exe`` / ``前缀*`` 形式做模糊匹配。
        """
        if not exe and not path:
            return False
        exe_l = (exe or "").lower()
        path_l = (path or "").lower()
        for raw in names:
            item = (raw or "").strip().lower()
            if not item:
                continue
            item_base = os.path.basename(item)
            # 1) 进程名完全一致（最常见的情况，必须精确，避免误伤同类进程）
            if item_base and item_base == exe_l:
                return True
            # 2) 完整路径匹配
            if settings.match_path_contains and path_l and (
                path_l == item or path_l.endswith(os.sep + item) or item in path_l
            ):
                return True
            # 3) 通配符匹配（例如 "*game*.exe"）
            if "*" in item_base or "?" in item_base:
                if fnmatch.fnmatch(exe_l, item_base) or (path_l and fnmatch.fnmatch(path_l, item)):
                    return True
        return False

    @staticmethod
    def _match_keyword(title: str, settings: AppSettings) -> str | None:
        """窗口标题是否命中关键字，返回命中的关键字。"""
        if not title:
            return None
        title_l = title.lower()
        for raw in settings.title_keywords:
            keyword = (raw or "").strip()
            if keyword and keyword.lower() in title_l:
                return keyword
        return None

    @staticmethod
    def _short_title(title: str, limit: int = 70) -> str:
        """截断过长的标题，避免界面错乱。"""
        title = (title or "").strip()
        return title if len(title) <= limit else title[: limit - 1] + "…"

    # ------------------------------------------------------------------ 提醒
    def _check_limits(self, target: Target | None, settings: AppSettings, info: winapi.ForegroundInfo) -> None:
        """判断是否达到提醒条件并触发通知 / 弹窗。"""
        if settings.count_mode == "each":
            used = self._target_seconds.get(target.name, 0.0) if target else 0.0
            subject = target.name if target else "监控对象"
        else:
            used = self._total
            subject = self._describe_subject(target)
        limit = settings.daily_limit_seconds
        now = time.monotonic()

        # 未超限 / 正在暂停：不提醒
        if used < limit or self._pause_event.is_set():
            if used < limit:
                self._over_limit_seen = False
            return

        # 今天首次越过上限：根据用户设置决定是否立刻提醒
        first_time = not self._over_limit_seen
        if first_time:
            self._over_limit_seen = True
            if not settings.remind_immediately_on_exceed:
                self._last_popup = now  # 从此刻开始按间隔计时，跳过“立刻提醒”
                return

        due = self._last_popup <= 0.0            # 今天还没提醒过：立刻提醒
        if not due and now - self._last_popup >= settings.reminder_interval_seconds:
            due = True
        if not due:
            return

        # 全屏游戏时暂缓弹窗，退出全屏后补发（避免打断游戏/导致游戏卡顿）
        if settings.suppress_popup_fullscreen and info.fullscreen and self._popup_manager is not None:
            if not self._popup_manager.has_deferred:
                title, message, detail = reminder_text(subject, used, limit, "检测到全屏程序，已暂缓弹窗")
                self._popup_manager.defer(title, message, detail)
                log.info("全屏中，暂缓弹窗提醒")
            return
        if self._popup_manager is not None and self._popup_manager.has_deferred and not info.fullscreen:
            self._popup_manager.flush_deferred()

        title, message, detail = reminder_text(
            subject,
            used,
            limit,
            f"今日提醒第 {self._popup_manager.popup_count_today + 1 if self._popup_manager else 1} 次",
        )
        self._last_popup = now

        if settings.enable_toast:
            self.notifier.notify(title, message, timeout=12, force=True)
        if settings.enable_popup and callable(self.on_limit_exceeded):
            try:
                self.on_limit_exceeded(target or Target(kind="game", name=subject), used, title, message, detail)
            except Exception:  # noqa: BLE001
                log.debug("弹窗回调异常", exc_info=True)

    def _describe_subject(self, target: Target | None) -> str:
        """总计时模式下，提醒文案里的“XX软件/网页”。"""
        if target is not None:
            return target.name
        if self._target_seconds:
            return max(self._target_seconds.items(), key=lambda kv: kv[1])[0]
        return "监控对象"

    # ------------------------------------------------------------------ 状态快照
    def snapshot(self) -> Snapshot:
        """打包当前状态（线程安全，供界面读取）。"""
        settings = self.config.settings
        with self._lock:
            total = self._total
            target_seconds = dict(self._target_seconds)
            current = self._current
            session_seconds = (time.monotonic() - self._session_start) if (current and self._session_start) else 0.0
        limit = settings.daily_limit_seconds
        info = self._last_fg or winapi.ForegroundInfo()
        next_popup_in = 0.0
        if total >= limit and self._last_popup > 0:
            next_popup_in = max(0.0, settings.reminder_interval_seconds - (time.monotonic() - self._last_popup))
        return Snapshot(
            day=self._day,
            total_seconds=total,
            current=current,
            session_seconds=session_seconds,
            target_seconds=target_seconds,
            limit_seconds=limit,
            remaining_seconds=limit - total,
            over_limit=total >= limit,
            monitoring=self.running and not self.paused,
            next_popup_in=next_popup_in,
            popup_count=self._popup_manager.popup_count_today if self._popup_manager else 0,
            fullscreen=info.fullscreen,
            foreground_exe=info.exe,
            foreground_title=self._short_title(info.title, 50),
            matched=current is not None,
        )

    # ------------------------------------------------------------------ 界面辅助
    def reload_from_db(self) -> None:
        """从数据库重新读取今日数据（例如点击“清零”之后调用）。"""
        with self._lock:
            self._pending.clear()
            self._total = self.store.total_seconds(self._day)
            self._target_seconds = {t.name: t.seconds for t in self.store.today_targets(self._day)}

    def update_settings(self, **kwargs) -> None:
        """更新设置并保存（界面调用）。"""
        self.config.update(**kwargs)
        # 设置变化后允许立刻重新评估提醒
        self._last_popup = 0.0
        log.info("设置已更新: %s", ", ".join(kwargs.keys()))

    def reset_today(self) -> None:
        """清零今日统计。"""
        with self._lock:
            self._pending.clear()
            self._day = today_str()
        self.store.reset_day(self._day)
        self.reload_from_db()
        self._last_popup = 0.0
        if self._popup_manager:
            self._popup_manager.reset_daily()
        log.info("今日统计已清零")

    def describe_total(self) -> str:
        """今日总时长的中文描述（托盘气泡 / 界面标题用）。"""
        return fmt_duration(self._total, with_seconds=False)

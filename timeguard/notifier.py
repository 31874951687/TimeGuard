"""提醒通道：

* :class:`Notifier` —— Windows 右下角系统通知（优先 plyer，其次 win10toast，
  最后退化为 PowerShell 气泡脚本），并提供提示音。
* :class:`ReminderPopup` —— 强制提醒窗口（tkinter Toplevel，持续置顶）。
* :class:`PopupManager` —— 弹窗调度器：节流、冷却、全屏暂缓、主线程弹出。
"""

from __future__ import annotations

import ctypes
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

from .utils import RateLimiter, fmt_duration

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- 通知后端探测
try:  # pragma: no cover
    from plyer import notification as _plyer_notification  # type: ignore

    HAS_PLYER = True
except Exception:  # noqa: BLE001
    _plyer_notification = None
    HAS_PLYER = False

try:  # pragma: no cover
    from win10toast import ToastNotifier  # type: ignore

    HAS_WIN10TOAST = True
except Exception:  # noqa: BLE001
    ToastNotifier = None  # type: ignore[assignment]
    HAS_WIN10TOAST = False


def _play_sound(kind: str = "alarm") -> None:
    """播放提示音（使用 Windows 内置音效，不需要音频文件）。"""
    try:
        import winsound

        if kind == "alarm":
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        else:
            winsound.MessageBeep(winsound.MB_OK)
    except Exception:  # noqa: BLE001
        pass


def _env_flag(name: str) -> bool:
    """读一个"真值"环境变量（``1/true/yes/on`` 都算开）。"""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


class Notifier:
    """系统通知发送器（线程安全，内部限流与自动降级）。"""

    APP_NAME = "TimeGuard 时间管家"

    #: 测试模式开关：设成 1/true/on 时 ``notify`` 只记录、不真的发系统通知。
    #:
    #: 为什么需要它：开发与回归脚本（尤其是 ``tools/e2e_*.py`` 与 ``ui_smoke.py``）
    #: 会真的往 Windows 通知中心塞一堆气泡 —— 用户看到的就是"这软件老在弹东西"，
    #: 而通知中心里的历史还得手动清。测试时默认打开这个开关，
    #: 需要验证"通知真的弹出来"时再显式关掉。
    SUPPRESS_ENV = "TIMEGUARD_NO_TOAST"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._toaster = None
        self._backend = "none"
        self._detect_backend()
        self._limiter = RateLimiter(30.0)  # 系统通知最短间隔，避免通知中心刷屏
        #: 所有"请求发送过"的通知（含被测试模式抑制的），便于脚本断言内容
        self.sent: list[tuple[str, str]] = []
        self.suppress = _env_flag(self.SUPPRESS_ENV)

    def _detect_backend(self) -> None:
        if HAS_PLYER:
            self._backend = "plyer"
        elif HAS_WIN10TOAST:
            self._backend = "win10toast"
            try:
                self._toaster = ToastNotifier()
            except Exception:  # noqa: BLE001
                self._backend = "powershell"
        else:
            self._backend = "powershell"
        log.info("通知后端: %s", self._backend)

    @property
    def backend(self) -> str:
        """当前使用的通知后端名称。"""
        return self._backend

    # ------------------------------------------------------------------
    def notify(self, title: str, message: str, timeout: int = 10, force: bool = False) -> bool:
        """发送一条系统通知；返回是否成功。

        被限流或被测试模式抑制时返回 ``True``（"请求已受理"），
        但真实通知不会出现 —— 两种情况都记在 :attr:`sent` 里，便于断言。
        """
        self.sent.append((str(title), str(message)))
        if self.suppress:
            log.debug("测试模式（%s=1）：不发送系统通知「%s」", self.SUPPRESS_ENV, title)
            return True
        if not force and not self._limiter.allow("toast"):
            log.debug("系统通知被限流，跳过")
            return False
        with self._lock:
            try:
                if self._backend == "plyer":
                    _plyer_notification.notify(
                        title=title, message=message, app_name=self.APP_NAME, timeout=timeout
                    )
                    return True
                if self._backend == "win10toast" and self._toaster is not None:
                    self._toaster.show_toast(title, message, duration=timeout, threaded=True)
                    return True
                return self._powershell_toast(title, message)
            except Exception as exc:  # noqa: BLE001 - 通知失败绝不能影响监控
                log.warning("发送系统通知失败(%s): %s", self._backend, exc)
                return False

    @staticmethod
    def _powershell_toast(title: str, message: str) -> bool:
        """不依赖第三方库的气泡通知（后台调用 PowerShell 的 NotifyIcon）。"""
        if sys.platform != "win32":
            return False
        safe_title = title.replace("'", "''")
        safe_msg = message.replace("'", "''")
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$n=New-Object System.Windows.Forms.NotifyIcon;"
            "$n.Icon=[System.Drawing.SystemIcons]::Warning;$n.Visible=$true;"
            f"$n.ShowBalloonTip(10000,'{safe_title}','{safe_msg}',"
            "[System.Windows.Forms.ToolTipIcon]::Warning);"
            "Start-Sleep -Seconds 6;$n.Dispose()"
        )
        try:
            subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
                creationflags=0x08000000,  # CREATE_NO_WINDOW
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("PowerShell 通知失败: %s", exc)
            return False

    @staticmethod
    def beep(kind: str = "alarm") -> None:
        """播放提示音。"""
        _play_sound(kind)


# ---------------------------------------------------------------- 强制弹窗

POPUP_BG = "#1b1f2a"
POPUP_FG = "#f2f4f8"
POPUP_ACCENT = "#ff6b6b"
POPUP_SUB = "#9aa4b8"


class ReminderPopup:
    """右下角强制提醒窗口：持续置顶、不可忽略，直到用户点击按钮。

    布局采用“固定画布 + 自适应内边距”：先按屏幕与内容算好尺寸，再让内部
    内容填满，避免按钮被裁剪。
    """

    WIDTH = 470
    MIN_HEIGHT = 250
    MAX_HEIGHT = 430
    AUTO_CLOSE_AFTER = 90.0  # 秒：无人理会时自动关闭，避免残留窗口

    def __init__(
        self,
        master: tk.Misc,
        title: str,
        message: str,
        detail: str = "",
        font_family: str = "Microsoft YaHei UI",
        sticky: bool = True,
        sound: bool = True,
        on_close=None,
    ) -> None:
        self.master = master
        self.font_family = font_family
        self.on_close = on_close
        self._closed = False
        self._sticky = sticky

        self.win = tk.Toplevel(master)
        self.win.withdraw()
        self.win.title("TimeGuard 提醒")
        self.win.configure(bg=POPUP_BG)
        self.win.resizable(False, False)
        self.win.attributes("-topmost", True)
        try:
            self.win.attributes("-toolwindow", True)  # 不在任务栏占位
        except tk.TclError:
            pass
        self.win.protocol("WM_DELETE_WINDOW", self.close)

        self._build(title, message, detail)
        self._place_bottom_right()
        self.win.deiconify()
        self.win.lift()
        try:
            self.win.focus_force()
        except tk.TclError:
            pass

        if sticky:
            self._sticky_loop()
        if sound:
            _play_sound("alarm")
        self.win.after(int(self.AUTO_CLOSE_AFTER * 1000), self.close)

    # ------------------------------------------------------------------
    def _build(self, title: str, message: str, detail: str) -> None:
        self.win.pack_propagate(False)   # 尺寸由 geometry 统一决定，内容只负责填满
        outer = tk.Frame(self.win, bg=POPUP_BG, padx=20, pady=16)
        outer.pack(fill="both", expand=True)

        tk.Label(
            outer, text=title, bg=POPUP_BG, fg=POPUP_ACCENT,
            font=(self.font_family, 14, "bold"), anchor="w", justify="left",
        ).pack(fill="x")

        # 先创建底部按钮条与详情行（用 side="bottom" 占位），保证它们不会被挤出窗口
        bar = tk.Frame(outer, bg=POPUP_BG)
        bar.pack(fill="x", side="bottom")
        try:
            style = ttk.Style(self.win)
            style.configure("Popup.TButton", font=(self.font_family, 10, "bold"), padding=(14, 7))
        except tk.TclError:
            pass
        btn = ttk.Button(bar, text="我知道了，先休息一下", style="Popup.TButton", command=self.close)
        btn.pack(side="right")
        try:
            btn.focus_set()
        except tk.TclError:
            pass
        self._bar = bar

        self._detail_label = None
        if detail:
            self._detail_label = tk.Label(
                outer, text=detail, bg=POPUP_BG, fg=POPUP_SUB, wraplength=self.WIDTH - 60,
                font=(self.font_family, 9), anchor="w", justify="left",
            )
            self._detail_label.pack(fill="x", side="bottom", pady=(0, 4))

        # 正文最后加入：仅占据剩余空间，不会顶掉底栏
        self._message_label = tk.Label(
            outer, text=message, bg=POPUP_BG, fg=POPUP_FG, wraplength=self.WIDTH - 60,
            font=(self.font_family, 11), anchor="nw", justify="left",
        )
        self._message_label.pack(fill="both", expand=True, pady=(12, 8))

    def _place_bottom_right(self) -> None:
        """定位到屏幕右下角（托盘通知区域上方），高度按内容自适应。"""
        try:
            self.win.update_idletasks()
        except tk.TclError:
            pass
        width = max(self.WIDTH, self.win.winfo_reqwidth())
        height = max(self.MIN_HEIGHT, min(self.MAX_HEIGHT, self._required_height()))
        try:
            screen_w = self.win.winfo_screenwidth()
            screen_h = self.win.winfo_screenheight()
        except tk.TclError:
            screen_w, screen_h = 1920, 1080
        x = max(0, screen_w - width - 24)
        y = max(0, screen_h - height - 80)
        self.win.geometry(f"{width}x{height}+{x}+{y}")

    def _required_height(self) -> int:
        """按各部分真实请求尺寸相加，得出不裁剪内容所需的最小高度。"""
        try:
            self.win.update_idletasks()
            pad = 16 * 2 + 6
            needed = (
                self._message_label.winfo_reqheight()
                + self._bar.winfo_reqheight()
                + pad
                + 20                                   # 标题行
            )
            if self._detail_label is not None:
                needed += self._detail_label.winfo_reqheight() + 4
            return int(needed)
        except (tk.TclError, AttributeError):
            return self.MIN_HEIGHT

    def _sticky_loop(self) -> None:
        """周期性把自己重新置顶，防止被游戏窗口压下去。"""
        if self._closed:
            return
        try:
            self.win.attributes("-topmost", True)
            self.win.lift()
        except tk.TclError:
            return
        self.win.after(2000, self._sticky_loop)

    def close(self) -> None:
        """关闭弹窗（幂等，可安全重复调用）。"""
        if self._closed:
            return
        self._closed = True
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        if callable(self.on_close):
            try:
                self.on_close()
            except Exception:  # noqa: BLE001
                log.debug("弹窗关闭回调异常", exc_info=True)


class PopupManager:
    """弹窗调度器：节流 / 冷却 / 全屏暂缓 / 在主线程弹出。

    监控线程通过 :meth:`request` 投递请求，界面主线程定时调用 :meth:`pump`
    真正弹窗 —— 这样既能跨线程安全，也不会阻塞监控循环。
    """

    def __init__(self, master: tk.Misc, font_family: str = "Microsoft YaHei UI") -> None:
        self.master = master
        self.font_family = font_family
        self.pending: "queue.Queue[tuple[str, str, str]]" = queue.Queue()
        self._active: ReminderPopup | None = None
        self._deferred: tuple[str, str, str] | None = None
        self._limiter = RateLimiter(60.0)
        self.last_popup_at: float = 0.0
        self.popup_count_today = 0

    # ------------------------------------------------------------------
    @property
    def active(self) -> bool:
        """当前是否有弹窗正在显示。"""
        popup = self._active
        return popup is not None and not popup._closed  # noqa: SLF001

    def request(self, title: str, message: str, detail: str = "", cooldown: float | None = None) -> bool:
        """请求弹窗（线程安全）。返回是否被接受（被冷却丢弃则为 False）。"""
        if cooldown is not None:
            self._limiter.interval = max(0.0, float(cooldown))
        if not self._limiter.allow("popup"):
            log.debug("弹窗被冷却限流，跳过（剩余 %.0fs）", self._limiter.remaining("popup"))
            return False
        self.pending.put((title, message, detail))
        return True

    def defer(self, title: str, message: str, detail: str = "") -> None:
        """暂缓一条提醒（例如全屏游戏时），稍后由 :meth:`flush_deferred` 补发。"""
        self._deferred = (title, message, detail)

    @property
    def has_deferred(self) -> bool:
        """是否有被暂缓的提醒。"""
        return self._deferred is not None

    def flush_deferred(self) -> None:
        """补发被暂缓的提醒（调用方需保证当前不在全屏状态）。"""
        item = self._deferred
        if not item:
            return
        self._deferred = None
        self._limiter.reset("popup")
        self.request(*item, cooldown=0.0)

    def skip(self) -> None:
        """重置冷却，允许稍后立即再提醒。"""
        self._limiter.reset("popup")

    def reset_daily(self) -> None:
        """跨天时重置计数。"""
        self.popup_count_today = 0
        self._deferred = None
        self._limiter.reset()

    # ------------------------------------------------------------------
    def pump(self) -> None:
        """由主线程定时调用：把队列中的请求真正弹出来。"""
        if self.active:
            return
        try:
            title, message, detail = self.pending.get_nowait()
        except queue.Empty:
            return
        self._active = ReminderPopup(
            self.master,
            title=title,
            message=message,
            detail=detail,
            font_family=self.font_family,
            sticky=True,
            on_close=self._on_closed,
        )
        self.last_popup_at = time.monotonic()
        self.popup_count_today += 1

    def _on_closed(self) -> None:
        self._active = None
        self.skip()  # 用户已确认，解除冷却，让后续提醒能及时出现


# ---------------------------------------------------------------- 辅助函数
def flash_window(master: tk.Misc) -> None:
    """让窗口在任务栏闪烁提示（Windows 专属，失败静默）。"""
    try:
        if sys.platform == "win32":
            hwnd = ctypes.windll.user32.GetAncestor(master.winfo_id(), 2)  # GA_ROOT
            ctypes.windll.user32.FlashWindow(hwnd or master.winfo_id(), True)
        master.attributes("-topmost", True)
        master.after(1200, lambda: master.attributes("-topmost", False))
    except Exception:  # noqa: BLE001
        pass


def reminder_text(
    target: str, used_seconds: float, limit_seconds: float, extra: str = ""
) -> tuple[str, str, str]:
    """生成提醒文案，返回 ``(标题, 正文, 详情)``。"""
    used_txt = fmt_duration(used_seconds, with_seconds=False)
    limit_txt = fmt_duration(limit_seconds, with_seconds=False)
    title = "注意！使用时长已超出上限"
    message = f"你已累计使用【{target}】{used_txt}，已达到今日上限（{limit_txt}），建议立即休息。"
    detail = f"今日已用 {used_txt} ／ 上限 {limit_txt}"
    if extra:
        detail = f"{detail}　·　{extra}"
    return title, message, detail

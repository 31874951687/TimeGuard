"""端到端验证：叉号退出 vs 最小化到托盘，提示语各说各的真话。

背景（用户反馈）：以前只要窗口第一次藏进托盘，就弹一句"TimeGuard 已在后台运行"；
而标题栏的**叉号其实也是藏进托盘**——用户以为关掉了，监控却还在跑，界面在说谎。

现在约定：

* 「🗕 最小化到托盘」/ 任务栏最小化 → **继续后台运行** + 提示"已在后台运行"；
* 标题栏**叉号** → 确认后**真正退出** + 提示"软件已退出运行"；
* 开机自启 / 启动即最小化 → **不提示**（用户没做任何操作，不该被打扰）。

通知全部用系统通知（窗口马上要销毁，界面内的提示条来不及被看见）。

用法::

    python tools/e2e_tray_close.py
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["TIMEGUARD_DATA_DIR"] = tempfile.mkdtemp(prefix="tg_e2e_tray_")
os.environ.setdefault("TIMEGUARD_NO_TOAST", "1")  # 测试不发真实系统通知（别往通知中心塞气泡）

from timeguard import winapi  # noqa: E402
from timeguard.config import Config  # noqa: E402
from timeguard.database import UsageStore  # noqa: E402
from timeguard.engine import UsageEngine  # noqa: E402
from timeguard.notifier import Notifier  # noqa: E402
from timeguard.ui import MainWindow  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []
STATE = {"done": False}
WATCHDOG_MS = 90000
OUT = ROOT / "tools" / "_shots"

_u32 = ctypes.WinDLL("user32", use_last_error=True)
_u32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
_u32.GetWindowRect.restype = wt.BOOL
_u32.SetForegroundWindow.argtypes = [wt.HWND]
_u32.SetForegroundWindow.restype = wt.BOOL


def save_window_shot(widget, name: str) -> None:
    """把某个 Toplevel 按屏幕坐标整窗截图存成 PNG。

    为什么不用 ``PrintWindow(PW_RENDERFULLCONTENT)``（阶段四截图用的就是它）：
    对**这个**对话框它只画出标题栏、客户区一片空白（实测同一时刻用屏幕截图
    能拍到完整内容），属于 DWM 合成层的行为差异，不值得为一张开发截图去猜。
    屏幕截图前先 ``SetForegroundWindow``，避免被别的窗口压住拍错。
    """
    try:
        from PIL import ImageGrab

        hwnd = ctypes.windll.user32.GetAncestor(widget.winfo_id(), 2)
        try:
            _u32.SetForegroundWindow(hwnd)
        except Exception:  # noqa: BLE001
            pass
        widget.update_idletasks()
        widget.update()
        rect = wt.RECT()
        _u32.GetWindowRect(hwnd, ctypes.byref(rect))
        image = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom))
        OUT.mkdir(parents=True, exist_ok=True)
        image.save(OUT / f"{name}.png")
        # 全是同一个颜色 = 拍空了，早点发现比事后看图早
        colors = image.convert("RGB").getcolors(maxcolors=4096)
        if colors is not None and len(colors) <= 2:
            print(f"  截图可疑（只有 {len(colors)} 种颜色，可能拍空了）: {OUT / (name + '.png')}")
            return
        print(f"  截图已保存: {OUT / (name + '.png')}（{image.width}x{image.height}）")
    except Exception as exc:  # noqa: BLE001
        print(f"  截图失败（忽略）: {exc}")


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  [通过] {label}{('　' + detail) if detail else ''}")
        PASS.append(label)
    else:
        print(f"  [失败] {label}{('　' + detail) if detail else ''}")
        FAIL.append(label)


def main() -> int:
    winapi.set_dpi_awareness()
    config = Config()
    settings = config.settings
    settings.start_minimized = False
    settings.enable_popup = False
    settings.task_check_on_start = False
    settings.recurring_reminder_enabled = False

    store = UsageStore()
    notifier = Notifier()
    engine = UsageEngine(config, store, notifier)
    window = MainWindow(config, store, engine, notifier)

    # ---- 把系统通知换成"记账"的假实现：既不打扰用户，也能断言发了什么 ----
    sent: list[tuple[str, str]] = []
    window.notifier.notify = lambda title, message="", timeout=10, force=False: (  # type: ignore[assignment]
        sent.append((str(title), str(message))) or True
    )

    # ---- 关闭方式对话框换成可编排的替身：脚本不能真的停下来等人点 ----
    import timeguard.ui as ui_mod
    real_choice_dialog = ui_mod.CloseChoiceDialog

    def _stub_dialog(action):                          # noqa: ANN001, ANN202
        """返回一个假的「关闭方式选择框」，``show()`` 直接给答案。"""

        class Stub:
            def __init__(self, master=None, **kw) -> None:   # noqa: ANN001, ANN003
                pass

            def show(self):                            # noqa: ANN201
                return action

        return Stub

    import traceback as _tb

    def on_tk_error(exc_type, exc_value, exc_tb) -> None:     # noqa: ANN001
        print("!! Tk 回调里发生异常，脚本中止 !!")
        _tb.print_exception(exc_type, exc_value, exc_tb)
        FAIL.append(f"Tk 回调异常: {exc_value!r}")
        finish(1)

    window.root.report_callback_exception = on_tk_error          # type: ignore[assignment]

    def last_title() -> str:
        return sent[-1][0] if sent else ""

    def run() -> None:
        for _ in range(20):
            window.root.update()

        def watchdog() -> None:
            if STATE["done"]:
                return
            print(f"!! 超过 {WATCHDOG_MS / 1000:.0f} 秒仍未跑完，判定为卡死 !!")
            FAIL.append("流程超时")
            finish(1)

        window.root.after(WATCHDOG_MS, watchdog)

        print("=== 端到端：叉号 / 最小化到托盘 的提示语 ===")

        # ---- 0) 叉号必须绑到"真退出"的处理函数上 ----
        protocol = str(window.root.protocol("WM_DELETE_WINDOW"))
        check("叉号已绑定到 request_close（不再是藏进托盘）",
              "request_close" in protocol, protocol)

        # 托盘图标要先就绪，否则后面 hide_to_tray 会走"托盘不可用"的分支
        waited = 0.0
        while window._tray_icon is None and waited < 6.0:      # noqa: SLF001
            window.root.update()
            time.sleep(0.05)
            waited += 0.05
        check("系统托盘图标已就绪", window._tray_icon is not None,      # noqa: SLF001
              f"等待 {waited:.1f}s")

        # ---- 1) 主动点「最小化到托盘」→ 只提示一次，并说明以后不再提示 ----
        config.settings.tray_notice_shown = False      # 确定性的起点
        config.save()
        sent.clear()
        window.hide_to_tray(manual=True)
        for _ in range(4):
            window.root.update()
        check("点「最小化到托盘」会提示『已在后台运行』",
              len(sent) == 1 and "已在后台运行" in last_title() and window._hidden,  # noqa: SLF001
              f"{sent}")
        check("提示里说明了『以后不再自动提示』",
              bool(sent) and "不再自动提示" in sent[0][1],
              sent[0][1].replace("\n", " ") if sent else "")
        check("提示状态被持久化（只提示一次，不是每次会话一次）",
              bool(config.settings.tray_notice_shown))

        # 再来一次**不该**再弹（用户要求：只提示一次）
        window.show_window()
        for _ in range(4):
            window.root.update()
        sent.clear()
        window.hide_to_tray(manual=True)
        for _ in range(4):
            window.root.update()
        check("之后重复收进托盘不再弹提示",
              sent == [] and window._hidden, f"{sent} / hidden={window._hidden}")  # noqa: SLF001

        # ---- 1.5) 从托盘恢复后，窗口必须真的回到屏幕内 ----
        # （踩过的坑：hide_to_tray 把窗口挪到 +10000 之后，恢复时那次 geometry
        #   请求会被 Tk 丢掉，窗口一直停在屏幕外 —— 用户点托盘图标什么都看不到）
        window.show_window()
        for _ in range(6):
            window.root.update()
        work_w, work_h = winapi.get_work_area(window.root)
        x, y = window.root.winfo_x(), window.root.winfo_y()
        check("从托盘恢复后主窗口回到屏幕内（不是停在屏幕外）",
              0 <= x <= work_w - 80 and 0 <= y <= work_h - 80,
              f"位置 ({x}, {y})，工作区 {work_w}x{work_h}")

        # 再来两轮，确认不是偶然
        off_screen = 0
        for _ in range(3):
            window.hide_to_tray(manual=True)
            for _ in range(4):
                window.root.update()
            window.show_window()
            for _ in range(6):
                window.root.update()
            if window.root.winfo_x() > work_w - 80:
                off_screen += 1
        check("连续三次收起/恢复都不会把窗口留在屏幕外", off_screen == 0, f"{off_screen}/3")

        # ---- 2) 启动即最小化（非用户操作）→ 不提示 ----
        window.show_window()
        for _ in range(4):
            window.root.update()
        sent.clear()
        window.hide_to_tray()                     # manual 默认 False
        for _ in range(4):
            window.root.update()
        check("启动即最小化 / 程序自身收起时**不打扰**用户",
              sent == [] and window._hidden, f"{sent}")   # noqa: SLF001

        # ---- 2.5) 托盘不可用时绝不能把窗口藏起来（藏了就找不回来了）----
        window.show_window()
        for _ in range(4):
            window.root.update()
        saved_icon = window._tray_icon                     # noqa: SLF001
        window._tray_icon = None                           # noqa: SLF001 - 模拟缺 pystray
        sent.clear()
        window.hide_to_tray(manual=True)
        for _ in range(4):
            window.root.update()
        check("托盘不可用时拒绝隐藏窗口（否则用户再也找不回界面）",
              not window._hidden and sent == [],           # noqa: SLF001
              f"hidden={window._hidden} sent={sent}")      # noqa: SLF001
        window._tray_icon = saved_icon                     # noqa: SLF001

        # ---- 3) 叉号弹出的是"二选一"对话框，而且两个选项都说清了后果 ----
        # 截图必须等窗口真正完成映射与合成（刚创建就抓会得到一张空白图）
        from timeguard.ui import CloseChoiceDialog
        probe = CloseChoiceDialog(window.root, font_family=window.font_family)
        exit_text = str(probe.btn_exit.cget("text"))
        tray_text = str(probe.btn_tray.cget("text"))
        check("关闭选项框同时给出『退出』与『后台运行』两个按钮",
              "退出" in exit_text and "后台" in tray_text, f"{exit_text} / {tray_text}")
        body = probe.winfo_children()[0]
        hint_text = " ".join(
            str(child.cget("text"))
            for frame in body.winfo_children()
            for child in frame.winfo_children()
            if child.winfo_class() == "TLabel"
        )
        check("每个选项都写明了后果（不能让人猜）",
              "停止统计" in hint_text and "托盘" in hint_text, hint_text)
        check("默认高亮的是较安全的『后台运行』（退出用警示色）",
              str(probe.btn_tray.cget("style")).startswith("Accent")
              and str(probe.btn_exit.cget("style")).startswith("Danger"),
              f"{probe.btn_tray.cget('style')} / {probe.btn_exit.cget('style')}")
        check("对话框宽度足够容纳两行说明",
              probe.winfo_reqwidth() >= 400, f"{probe.winfo_reqwidth()}px")
        probe.lift()

        def after_shot() -> None:
            check("对话框两个按钮都真的可见（宽 %d 高 %d）" % (probe.winfo_width(), probe.winfo_height()),
                  probe.btn_exit.winfo_ismapped() == 1 and probe.btn_tray.winfo_ismapped() == 1
                  and probe.winfo_width() > 300,
                  f"{probe.winfo_width()}x{probe.winfo_height()}")
            # 位置要等窗口真正映射之后再读（映射前 winfo_geometry() 仍是旧值）
            pad_x, pad_y = probe.winfo_rootx(), probe.winfo_rooty()
            check("关闭选项框落在屏幕内且居中于主窗口（不是跑到左上角）",
                  0 <= pad_x <= work_w - 50 and 0 <= pad_y <= work_h - 50 and pad_x > 100,
                  f"({pad_x}, {pad_y})，工作区 {work_w}x{work_h}")
            probe._choose("tray")                      # noqa: SLF001
            check("选『后台运行』→ 返回值正确且窗口关闭",
                  probe.choice == "tray" and probe.winfo_exists() == 0, str(probe.choice))

            probe2 = CloseChoiceDialog(window.root, font_family=window.font_family)
            probe2._cancel()                           # noqa: SLF001
            check("按取消 → 返回 None（不关闭任何东西）",
                  probe2.choice is None and probe2.winfo_exists() == 0, str(probe2.choice))
            window.root.after(60, step_cancel)

        def step_cancel() -> None:
            # ---- 4) 叉号 + 取消 → 什么都不该发生 ----
            window.show_window()
            for _ in range(4):
                window.root.update()
            sent.clear()
            ui_mod.CloseChoiceDialog = _stub_dialog(None)
            window.request_close()
            for _ in range(4):
                window.root.update()
            check("叉号选『取消』→ 不退出、不隐藏、不发通知",
                  not window._hidden and not window._shutting_down and sent == [],  # noqa: SLF001
                  f"hidden={window._hidden} shutting={window._shutting_down} sent={sent}")  # noqa: SLF001
            check("取消后窗口与引擎仍然正常",
                  window.root.winfo_exists() == 1
                  and not engine._stop_event.is_set())      # noqa: SLF001
            window.root.after(60, step_tray_choice)

        def step_tray_choice() -> None:
            # ---- 5) 叉号 + 选『后台运行』→ 藏进托盘、继续统计并提示 ----
            sent.clear()
            ui_mod.CloseChoiceDialog = _stub_dialog("tray")
            # 把"提示过一次"的标记清掉，专门验证叉号走托盘这条路也会提示一次
            config.settings.tray_notice_shown = False
            config.save()
            sent.clear()
            window.request_close()
            for _ in range(4):
                window.root.update()
            check("叉号选『后台运行』→ 窗口收进托盘并提示『已在后台运行』",
                  window._hidden and len(sent) == 1 and "已在后台运行" in last_title(),  # noqa: SLF001
                  f"hidden={window._hidden} sent={sent}")    # noqa: SLF001
            check("叉号选『后台运行』→ 引擎继续统计（没有退出）",
                  not engine._stop_event.is_set()            # noqa: SLF001
                  and not window._shutting_down)             # noqa: SLF001
            window.root.after(60, step_exit_choice)

        def step_exit_choice() -> None:
            # ---- 6) 叉号 + 选『退出』→ 真退出 + 提示"软件已退出运行" ----
            window.show_window()
            for _ in range(4):
                window.root.update()
            sent.clear()
            ui_mod.CloseChoiceDialog = _stub_dialog("exit")
            window.request_close()
            check("叉号选『退出』→ 提示『软件已退出运行』",
                  len(sent) == 1 and "已退出运行" in last_title(), f"{sent}")
            check("叉号选『退出』→ 引擎已停止", engine._stop_event.is_set())   # noqa: SLF001
            check("叉号选『退出』→ 托盘图标已移除", window._tray_icon is None)  # noqa: SLF001
            check("叉号选『退出』→ 进入退出流程（不再刷新界面）", window._shutting_down is True)  # noqa: SLF001
            ui_mod.CloseChoiceDialog = real_choice_dialog
            finish(0)

        # 等 500ms 让它画完，再截图 + 继续后面的断言
        def shoot() -> None:
            # 抓图前必须让 Tk 真的把控件画进窗口 DC：只等时间是没用的，
            # ``update()`` 会同步处理待办的重绘，否则 PrintWindow 拿到一张空白图。
            probe.update_idletasks()
            probe.update()
            probe.update()
            save_window_shot(probe, "21_close_choice")
            window.root.after(60, after_shot)

        window.root.after(500, shoot)

    def finish(code: int) -> None:
        if STATE["done"]:
            return
        STATE["done"] = True
        print("-" * 60)
        print(f"结果: {'全部通过' if not FAIL else '有失败项'}（通过 {len(PASS)} / 失败 {len(FAIL)}）")
        for item in FAIL:
            print(f"  失败: {item}")
        raise SystemExit(code)

    window.root.after(1200, run)
    window.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

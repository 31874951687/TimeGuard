"""端到端验证：设置页「当前值」的就地编辑（数字 + 单位 / 开启·关闭）。

背景（用户反馈）：表格里显示的是「120分钟」，但双击浮出的编辑框只显示 `120.0`
—— 单位被盖掉、整数还多了个小数点，看着像这个设置没有单位；布尔项更直接，
双击就无声地翻一下，看不到"开启/关闭"这两个字。

现在：

* 就地编辑是一个**浮层容器**（编辑控件 + 单位后缀），整个单元格都是触发区，
  数字和单位一起显示（整数不再显示成 `120.0`）；
* 布尔项弹的是「开启 / 关闭」两个选项（和表格里「✔ 已开启 / ✘ 已关闭」同一套说法），
  想快速翻转仍然可以按空格；
* 校验：非法输入被拒绝、上下界被夹取、切换布尔项走的是既有路径（autostart 要写注册表）。

用法::

    python tools/e2e_settings_inline.py
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["TIMEGUARD_DATA_DIR"] = tempfile.mkdtemp(prefix="tg_e2e_inline_")
os.environ.setdefault("TIMEGUARD_NO_TOAST", "1")  # 测试不发真实系统通知

from timeguard import winapi  # noqa: E402
from timeguard.config import Config  # noqa: E402
from timeguard.database import UsageStore  # noqa: E402
from timeguard.engine import UsageEngine  # noqa: E402
from timeguard.notifier import Notifier  # noqa: E402
from timeguard.ui import MainWindow  # noqa: E402

OUT = ROOT / "tools" / "_shots"
PASS: list[str] = []
FAIL: list[str] = []
STATE = {"done": False}
WATCHDOG_MS = 120000

_u32 = ctypes.WinDLL("user32", use_last_error=True)
_u32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]


class _RECT(wt.RECT):
    pass


def save_shot(widget, name: str) -> None:
    """整窗截图（含标题栏），用屏幕抓图而不是 PrintWindow（后者对某些窗口客户区是空白）。"""
    try:
        from PIL import ImageGrab

        hwnd = ctypes.windll.user32.GetAncestor(widget.winfo_id(), 2)
        widget.update_idletasks()
        widget.update()
        rect = _RECT()
        _u32.GetWindowRect(hwnd, ctypes.byref(rect))
        image = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom))
        OUT.mkdir(parents=True, exist_ok=True)
        image.save(OUT / f"{name}.png")
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
    cfg = Config()
    cfg.settings.enable_popup = False
    cfg.settings.task_check_on_start = False
    cfg.settings.recurring_reminder_enabled = False
    store = UsageStore()
    notifier = Notifier()
    eng = UsageEngine(cfg, store, notifier)
    win = MainWindow(cfg, store, eng, notifier)

    import traceback as _tb

    def on_tk_error(exc_type, exc_value, exc_tb) -> None:  # noqa: ANN001
        print("!! Tk 回调里发生异常，脚本中止 !!")
        _tb.print_exception(exc_type, exc_value, exc_tb)
        FAIL.append(f"Tk 回调异常: {exc_value!r}")
        finish(1)

    win.root.report_callback_exception = on_tk_error  # type: ignore[assignment]

    page = win.settings_panel

    def setting_of(key: str):
        return next(s for s in page._schema if s.key == key)  # noqa: SLF001

    def pump(n: int = 8) -> None:
        for _ in range(n):
            win.root.update()

    def open_inline(key: str):
        page.focus_key(key)
        pump(6)
        setting = setting_of(key)
        page._start_inline(setting, 0, 0)  # noqa: SLF001
        pump(6)
        return setting

    def run() -> None:
        def watchdog() -> None:
            if STATE["done"]:
                return
            print(f"!! 超过 {WATCHDOG_MS / 1000:.0f} 秒仍未跑完，判定为卡死 !!")
            FAIL.append("流程超时")
            finish(1)

        win.root.after(WATCHDOG_MS, watchdog)

        print("=== 端到端：设置页就地编辑 ===")
        win.notebook.select(win.notebook_index_settings)
        pump(14)

        # ---- 1) 数值项：编辑框里要有数字，也要有单位 ----
        setting = open_inline("daily_limit_minutes")
        inline = page._inline          # noqa: SLF001
        overlay = page._inline_overlay  # noqa: SLF001
        box = page.tree.bbox("daily_limit_minutes", "value")
        check("数值项的就地编辑器已浮出", inline is not None and overlay is not None)
        if inline is not None and overlay is not None:
            check("整数项不再显示成 120.0（就是 120）",
                  str(inline.get()) == "120", f"{inline.get()!r}")
            labels = [str(w.cget("text")) for w in overlay.winfo_children()
                      if w.winfo_class() == "TLabel"]
            check("编辑区里保留了单位「分钟」", "分钟" in labels, f"{labels}")
            check("浮层覆盖整个「当前值」单元格（数字和单位同一个触发区）",
                  box is not None and overlay.winfo_width() >= box[2],
                  f"浮层宽 {overlay.winfo_width()} / 单元格宽 {box[2] if box else '?'}")
            check("表格里本来显示的就是「120分钟」",
                  page._display_value(setting) == "120分钟",  # noqa: SLF001
                  page._display_value(setting))  # noqa: SLF001
        save_shot(win.root, "22_inline_number")

        # 提交一个正常值
        if inline is not None:
            page._inline_var.set("90")      # noqa: SLF001
            page._commit_inline(setting, page._inline_var)  # noqa: SLF001
            pump(6)
            check("就地编辑提交后草稿与表格都更新",
                  page._draft["daily_limit_minutes"] == 90          # noqa: SLF001
                  and page._display_value(setting) == "90分钟",      # noqa: SLF001
                  f"{page._draft['daily_limit_minutes']} / {page._display_value(setting)}")  # noqa: SLF001

        # ---- 2) 非法输入被拒绝、超界被夹取 ----
        setting = open_inline("daily_limit_minutes")
        page._inline_var.set("不是数字")     # noqa: SLF001
        page._commit_inline(setting, page._inline_var)  # noqa: SLF001
        pump(6)
        check("非法输入被拒绝（保留原值）",
              page._draft["daily_limit_minutes"] == 90,  # noqa: SLF001
              str(page._draft["daily_limit_minutes"]))   # noqa: SLF001

        setting = open_inline("daily_limit_minutes")
        page._inline_var.set("99999")        # noqa: SLF001
        page._commit_inline(setting, page._inline_var)  # noqa: SLF001
        pump(6)
        check("超上界的值被夹取",
              page._draft["daily_limit_minutes"] == setting.high,  # noqa: SLF001
              f"{page._draft['daily_limit_minutes']}（上界 {setting.high}）")  # noqa: SLF001

        # ---- 3) 布尔项：弹「开启 / 关闭」而不是无声翻转 ----
        setting = open_inline("enable_toast")
        inline = page._inline            # noqa: SLF001
        overlay = page._inline_overlay   # noqa: SLF001
        before = bool(page._draft["enable_toast"])   # noqa: SLF001
        check("布尔项的就地编辑器是「开启 / 关闭」下拉",
              inline is not None and inline.winfo_class() == "TCombobox"
              and tuple(inline.cget("values")) == ("开启", "关闭"),
              f"{getattr(inline, 'winfo_class', lambda: '?')()} "
              f"{tuple(inline.cget('values')) if inline is not None else '?'}")
        check("下拉里显示的当前值与表格一致",
              inline is not None and str(inline.get()) == ("开启" if before else "关闭"),
              f"{inline.get() if inline is not None else '?'} / 表格 "
              f"{page._display_value(setting)}")   # noqa: SLF001
        save_shot(win.root, "23_inline_bool")

        if inline is not None:
            page._inline_var.set("关闭" if before else "开启")   # noqa: SLF001
            page._commit_inline(setting, page._inline_var)       # noqa: SLF001
            pump(8)
            check("选「关闭」后草稿真的翻转了",
                  bool(page._draft["enable_toast"]) != before,   # noqa: SLF001
                  f"{before} -> {page._draft['enable_toast']}")  # noqa: SLF001
            check("表格里的说法跟着变（已开启 / 已关闭）",
                  ("已关闭" in page._display_value(setting)) == before,  # noqa: SLF001
                  page._display_value(setting))  # noqa: SLF001

        # 选"同一个值"不该产生未保存修改（避免误标脏）
        page.refresh_from_draft() if hasattr(page, "refresh_from_draft") else None
        setting = open_inline("enable_toast")
        if page._inline is not None:  # noqa: SLF001
            same = "开启" if bool(page._draft["enable_toast"]) else "关闭"  # noqa: SLF001
            page._inline_var.set(same)                                # noqa: SLF001
            page._commit_inline(setting, page._inline_var)            # noqa: SLF001
            pump(6)
            check("选择与当前值相同的选项不会改坏数据",
                  ("已开启" in page._display_value(setting)) == bool(page._draft[setting.key]),  # noqa: SLF001
                  page._display_value(setting))  # noqa: SLF001

        # ---- 4) 空格键仍然能快速翻转布尔项 ----
        page.focus_key("enable_toast")
        pump(6)
        before = bool(page._draft["enable_toast"])  # noqa: SLF001
        page._toggle_focused()                      # noqa: SLF001
        pump(6)
        check("空格键快速翻转布尔项仍然可用",
              bool(page._draft["enable_toast"]) != before, f"{before} -> {page._draft['enable_toast']}")  # noqa: SLF001

        # ---- 5) 其余类型没被改坏 ----
        setting = open_inline("count_mode")
        check("选择型（计时口径）仍用下拉且值正确",
              page._inline is not None and str(page._inline.get()) == str(page._draft["count_mode"]),  # noqa: SLF001
              f"{page._inline.get() if page._inline else '?'}")  # noqa: SLF001
        page._destroy_inline()  # noqa: SLF001
        pump(4)

        # 列表 / 窗口尺寸这类需要多个控件的设置：双击不该浮出就地编辑器
        from types import SimpleNamespace

        # ---- 6) 单击触发：第一次点选中、第二次点「当前值」进编辑 ----
        page._destroy_inline()                          # noqa: SLF001
        page.focus_key("monitor_interval_seconds")
        pump(8)
        box = page.tree.bbox("daily_limit_minutes", "value")
        if box:
            click = SimpleNamespace(x=box[0] + 4, y=box[1] + 4)
            page._on_click_value(click)                 # noqa: SLF001 - 该行还没选中
            pump(6)
            check("第一次点「当前值」只选中，不弹编辑框", page._inline is None)  # noqa: SLF001
            page.focus_key("daily_limit_minutes")
            pump(8)
            box = page.tree.bbox("daily_limit_minutes", "value")
            click = SimpleNamespace(x=box[0] + 4, y=box[1] + 4)
            page._on_click_value(click)                 # noqa: SLF001 - 已选中
            pump(8)
            check("已选中后再点一次「当前值」就进编辑（底部提示不再骗人）",
                  page._inline is not None)             # noqa: SLF001
            # 点在单位那一侧的像素位置同样要能触发（同一个触发区）
            page._destroy_inline()                      # noqa: SLF001
            page.focus_key("daily_limit_minutes")
            pump(6)
            box = page.tree.bbox("daily_limit_minutes", "value")
            page._on_click_value(SimpleNamespace(x=box[0] + box[2] - 6, y=box[1] + 4))  # noqa: SLF001
            pump(8)
            check("点在单元格右端（单位那一侧）也能触发编辑",
                  page._inline is not None,             # noqa: SLF001
                  f"x={box[0] + box[2] - 6}（单元格 {box[0]}..{box[0] + box[2]}）")
            page._destroy_inline()                      # noqa: SLF001
            pump(4)
            # 点在「说明」列不该触发
            page.focus_key("daily_limit_minutes")
            pump(6)
            name_box = page.tree.bbox("daily_limit_minutes", "desc")
            if name_box:
                page._on_click_value(SimpleNamespace(x=name_box[0] + 4, y=name_box[1] + 4))  # noqa: SLF001
                pump(6)
                check("点在「说明」列不会弹编辑框", page._inline is None)  # noqa: SLF001

        for key in ("game_processes", "window_size"):
            setting = setting_of(key)
            page.focus_key(key)
            pump(6)
            page._on_double_click(SimpleNamespace(x=0, y=0))  # noqa: SLF001
            pump(4)
            check(f"「{key}」不可就地编辑，交给右侧面板",
                  not setting.editable_inline and page._inline is None,  # noqa: SLF001
                  f"editable_inline={setting.editable_inline} inline={page._inline}")  # noqa: SLF001
        finish(0)

    def finish(code: int) -> None:
        if STATE["done"]:
            return
        STATE["done"] = True
        print("-" * 60)
        print(f"结果: {'全部通过' if not FAIL else '有失败项'}（通过 {len(PASS)} / 失败 {len(FAIL)}）")
        for item in FAIL:
            print(f"  失败: {item}")
        try:
            eng.stop()
            win._stop_tray()  # noqa: SLF001
            store.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            win.root.quit()
            win.root.destroy()
        except Exception:  # noqa: BLE001
            pass
        raise SystemExit(code)

    win.root.after(1200, run)
    win.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

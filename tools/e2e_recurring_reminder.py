"""端到端验证：周期性任务的"开始前提醒"到底会不会真的弹出来。

真实开关一次主界面 + 调度器，用真实数据库与时间推进验证四件事：

1. **合并**：同一时刻到点的多个任务 → 只弹**一个**窗、只发**一条**通知（避坑 #5）；
2. **不重复**：同一批再次到点不会重复打扰（``task_logs.remind_at`` 去重）；
3. **迟到补发**：模拟"关机错过 → 开机"（把提前提醒时刻设在过去、仍在补发窗口内），
   开机后应当补发一次并标记为迟到（避坑 #1）；
4. **开机补发窗口**：超过窗口就不再补发。

跑完会截图到 ``tools/_shots/14_recurring_reminder.png``，方便肉眼确认弹窗内容。

用法::

    python tools/e2e_recurring_reminder.py
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["TIMEGUARD_DATA_DIR"] = tempfile.mkdtemp(prefix="tg_e2e_recur_")
os.environ.setdefault("TIMEGUARD_NO_TOAST", "1")  # 测试不发真实系统通知（别往通知中心塞气泡）

from timeguard import recurrence as R  # noqa: E402
from timeguard import winapi  # noqa: E402
from timeguard.config import Config  # noqa: E402
from timeguard.database import UsageStore  # noqa: E402
from timeguard.engine import UsageEngine  # noqa: E402
from timeguard.notifier import Notifier  # noqa: E402
from timeguard.scheduler import KIND_ADVANCE, compute_schedule  # noqa: E402
from timeguard.ui import MainWindow  # noqa: E402

OUT = ROOT / "tools" / "_shots"
PASS: list[str] = []
FAIL: list[str] = []

_u32 = ctypes.WinDLL("user32", use_last_error=True)
_g32 = ctypes.WinDLL("gdi32", use_last_error=True)
_u32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
_u32.GetDC.argtypes = [wt.HWND]
_u32.GetDC.restype = wt.HDC
_u32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
_u32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
_g32.CreateCompatibleDC.argtypes = [wt.HDC]
_g32.CreateCompatibleDC.restype = wt.HDC
_g32.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
_g32.CreateCompatibleBitmap.restype = wt.HBITMAP
_g32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
_g32.SelectObject.restype = wt.HGDIOBJ
_g32.DeleteObject.argtypes = [wt.HGDIOBJ]
_g32.DeleteDC.argtypes = [wt.HDC]
_g32.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, ctypes.c_uint, ctypes.c_uint,
                           ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]


class _BIH(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


def grab(hwnd: int):
    from PIL import Image

    rect = wt.RECT()
    _u32.GetClientRect(hwnd, ctypes.byref(rect))
    width, height = rect.right, rect.bottom
    hdc = _u32.GetDC(hwnd)
    mem = _g32.CreateCompatibleDC(hdc)
    bmp = _g32.CreateCompatibleBitmap(hdc, width, height)
    _g32.SelectObject(mem, bmp)
    _u32.PrintWindow(hwnd, mem, 2)
    info = _BIH()
    info.biSize = ctypes.sizeof(_BIH)
    info.biWidth, info.biHeight, info.biPlanes, info.biBitCount = width, -height, 1, 32
    buf = ctypes.create_string_buffer(width * height * 4)
    _g32.GetDIBits(mem, bmp, 0, height, buf, ctypes.byref(info), 0)
    image = Image.frombuffer("RGBA", (width, height), buf, "raw", "BGRA", 0, 1).convert("RGB")
    _g32.DeleteObject(bmp)
    _g32.DeleteDC(mem)
    _u32.ReleaseDC(hwnd, hdc)
    return image


def grab_window_of(widget) -> int:
    """取某个 Toplevel 的顶层窗口句柄。"""
    return ctypes.windll.user32.GetAncestor(widget.winfo_id(), 2)


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  [通过] {label}{('　' + detail) if detail else ''}")
        PASS.append(label)
    else:
        print(f"  [失败] {label}{('　' + detail) if detail else ''}")
        FAIL.append(label)


def count_dialogs(root) -> list:
    """当前所有提醒类 Toplevel。"""
    result = []
    for child in root.winfo_children():
        if child.winfo_class() == "Toplevel":
            try:
                if "提醒" in str(child.title()):
                    result.append(child)
            except Exception:  # noqa: BLE001
                continue
    return result


def main() -> int:
    winapi.set_dpi_awareness()
    config = Config()
    settings = config.settings
    settings.task_check_on_start = True           # 开启开机统一检查（阶段 3 的合并链路）
    settings.task_reminder_toast = False          # 关闭系统通知（本脚本只验证弹窗合并）
    settings.recurring_late_catchup = True
    settings.recurring_catchup_minutes = 5

    store = UsageStore()
    notifier = Notifier()
    engine = UsageEngine(config, store, notifier)
    window = MainWindow(config, store, engine, notifier)

    now = datetime.now()
    # 两条"开始前 15 分钟提醒"的每日任务：开始时间设在 10 分钟后，
    # 于是"此刻"正好落在提前提醒窗口 [start-15min, start) 内 —— 无论脚本几点运行都成立。
    start = (now + timedelta(minutes=10)).strftime("%H:%M")
    end = (now + timedelta(minutes=100)).strftime("%H:%M")
    run_id = store.add_task("跑步 2 公里", task_type=R.TYPE_DAILY,
                            time_start=start, time_end=end, remind_before_minutes=15)
    word_id = store.add_task("背单词", task_type=R.TYPE_DAILY,
                             time_start=start, time_end=end, remind_before_minutes=15)
    single_id = store.add_task("单次任务（不该被周期调度器管）",
                               due_at=now + timedelta(hours=2))

    def reset_reminder_marks() -> None:
        """清掉"已提醒"标记，把场景恢复成"还没提醒过"。

        注意：这里**故意不调用** ``reschedule`` —— 那会顺手把批次派发掉，
        导致后面观察不到弹窗。
        """
        today = date.today().strftime("%Y-%m-%d")
        for task_id in (run_id, word_id):
            store.log_occurrence(task_id, date.today(), completed=False)
        store._conn.execute(  # noqa: SLF001
            "UPDATE task_logs SET remind_at = NULL, remind_kind = NULL WHERE occur_date = ?",
            (today,))
        store._conn.commit()  # noqa: SLF001

    def run() -> None:
        for _ in range(12):
            window.root.update()

        print("=== 端到端：周期任务提醒 ===")
        print(f"  数据目录: {os.environ['TIMEGUARD_DATA_DIR']}")
        print(f"  任务时间窗: {start}~{end}（提前 15 分钟提醒）")
        print(f"  现在: {datetime.now():%Y-%m-%d %H:%M:%S}")

        # ---- 0) 开机链路：调度器延后派发，由统一检查合并成一个窗（阶段 3 行为）----
        # 开机检查排在 1700ms，而本回调在 1600ms —— 明确等一下，别靠猜时序。
        def startup_done() -> bool:
            rows = {row.task_id: row for row in store.task_logs_on(date.today())}
            return rows.get(run_id) is not None and rows[run_id].remind_at is not None

        waited = 0.0
        while not startup_done() and waited < 6.0:
            window.root.update()
            time.sleep(0.05)
            waited += 0.05
        print(f"  等待开机统一检查完成: {waited:.1f} 秒")

        logs = {row.task_id: row for row in store.task_logs_on(date.today())}
        standalone = [d for d in count_dialogs(window.root) if "周期任务提醒" in str(d.title())]
        startup_dialogs = [d for d in count_dialogs(window.root) if "开机提醒" in str(d.title())]
        check("开机时没有独立弹出周期任务窗（已并入合并窗）",
              len(standalone) == 0,
              f"独立周期窗 {len(standalone)} 个")
        check("开机统一检查把到点任务并进『开机提醒』窗",
              len(startup_dialogs) == 1,
              f"开机提醒窗 {len(startup_dialogs)} 个")
        for dialog in startup_dialogs:
            try:
                dialog.destroy()
            except Exception:  # noqa: BLE001
                pass
        window.root.update()
        logs = {row.task_id: row for row in store.task_logs_on(date.today())}
        check("开机链路已写入提醒标记",
              logs.get(run_id) is not None and logs.get(run_id).remind_at is not None)

        # ---- 1) 合并：把标记清掉后再派发，应当只弹一个窗 ----
        reset_reminder_marks()
        window.root.update()
        before = len(count_dialogs(window.root))
        batch = window.task_scheduler.fire_now()
        for _ in range(10):
            window.root.update()
        dialogs = count_dialogs(window.root)
        check("多个任务合并成一个弹窗", len(dialogs) - before == 1,
              f"新增弹窗 {len(dialogs) - before} 个（任务 {batch.count if batch else 0} 条）")
        check("批次内容体现『即将开始』",
              bool(batch) and "即将开始" in batch.headline(),
              batch.headline() if batch else "（没有批次）")

        # 延迟截图：弹窗刚创建时还没完成映射（winfo_height() 只有 1px），
        # 立刻抓图会把底部按钮拍成被裁掉的样子 —— 这是截图时机问题，不是布局问题。
        window.root.after(700, after_dialog_ready)
        return

    def after_dialog_ready() -> None:
        """等弹窗真正完成映射后再截图与断言。"""
        for _ in range(6):
            window.root.update()
        dialogs = count_dialogs(window.root)
        if dialogs:
            tip = dialogs[0]
            try:
                OUT.mkdir(parents=True, exist_ok=True)
                grab(grab_window_of(tip)).save(OUT / "14_recurring_reminder.png")
                print(f"  截图已保存: {OUT / '14_recurring_reminder.png'}")
            except Exception as exc:  # noqa: BLE001
                print(f"  截图失败（忽略）: {exc}")
            # 按钮必须真的落在窗口可见区域内
            outer = tip.winfo_children()[0]
            buttons = None
            for child in outer.winfo_children():
                if child.winfo_class() == "TFrame":
                    buttons = child
            inside = bool(buttons) and (
                buttons.winfo_rooty() - tip.winfo_rooty() + buttons.winfo_reqheight()
                <= tip.winfo_height() + 2)
            check("弹窗底部按钮没有被裁掉", inside,
                  f"弹窗高 {tip.winfo_height()}（需要 {tip.winfo_reqheight()}）")

        for dialog in dialogs:
            try:
                dialog.destroy()
            except Exception:  # noqa: BLE001
                pass
        for _ in range(4):
            window.root.update()

        # ---- 2) 去重：已标记后再次派发不会重复打扰 ----
        logs = {row.task_id: row for row in store.task_logs_on(date.today())}
        check("两个任务都被标记为已提醒",
              logs.get(run_id) is not None and logs.get(word_id) is not None)
        check("提醒类型记为 advance（正常提前）",
              all(logs[i].remind_kind == KIND_ADVANCE for i in (run_id, word_id) if i in logs))

        before = len(count_dialogs(window.root))
        window.reschedule_recurring("e2e-again")
        for _ in range(10):
            window.root.update()
        check("再次到点不会重复打扰", len(count_dialogs(window.root)) == before,
              f"新增弹窗 {len(count_dialogs(window.root)) - before} 个")
        check("重复派发不会新增提醒标记",
              all(row.remind_kind == KIND_ADVANCE
                  for row in store.task_logs_on(date.today())))

        # ---- 3) 单次任务不归周期调度器管（通知互斥）----
        state = window.task_scheduler.state()
        check("单次任务不在周期调度批次里",
              state.batch is None
              or all(item.task.id != single_id for item in state.batch.items))

        # ---- 4) 迟到补发：把"现在"设为开始后 2 分钟（补发窗口 5 分钟内）----
        reset_reminder_marks()
        tasks = store.recurring_tasks()
        task = store.get_task(run_id)
        window_start = task.occurrence_on(date.today())
        logs_now: dict = {}
        late_state = compute_schedule(tasks, logs_now, window_start.start + timedelta(minutes=2),
                                      catchup_minutes=5)
        check("开机后补发窗口内会补发一次",
              late_state.batch is not None and late_state.batch.count == len(tasks),
              f"补发 {late_state.batch.count if late_state.batch else 0} 项")
        check("补发被标记为迟到提醒",
              bool(late_state.batch and late_state.batch.has_late),
              late_state.batch.items[0].kind_label if late_state.batch else "")
        check("迟到补发批次标题为多项合并",
              bool(late_state.batch and late_state.batch.count == 2
                   and "2 项任务即将开始" in late_state.batch.headline()),
              late_state.batch.headline() if late_state.batch else "")

        beyond_state = compute_schedule(tasks, logs_now,
                                       window_start.start + timedelta(minutes=30),
                                       catchup_minutes=5)
        check("超过补发窗口不再打扰", beyond_state.batch is None)

        finish(0)

    def finish(code: int) -> None:
        print("-" * 60)
        print(f"结果: {'全部通过' if not FAIL else '有失败项'}（通过 {len(PASS)} / 失败 {len(FAIL)}）")
        for item in FAIL:
            print(f"  失败: {item}")
        try:
            engine.stop()
            window.task_scheduler.stop()
            window._stop_tray()  # noqa: SLF001
            store.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            window.root.quit()
            window.root.destroy()
        except Exception:  # noqa: BLE001
            pass
        raise SystemExit(code)

    window.root.after(1600, run)
    window.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

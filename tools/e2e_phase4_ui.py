"""端到端验证（阶段四）：周期任务的界面 —— 对话框与分组列表。

前面三个阶段验证的是"数据 / 调度 / 补发"，这一阶段验证**用户真正点得到的东西**：

1. **对话框能建出三种任务**：单次 / 每天 / 每周 —— 全部通过真实控件（下拉框、
   时:分 选择器、星期复选框）填写，再走真实的 ``TaskDialog._confirm()`` 与
   ``TaskPanel.add_task()``，最后落到真实数据库里核对字段；
2. **校验拦得住**：每周不选星期几 → 拒绝保存且窗口不关闭；
3. **分组列表**：今日待办 / 本周待办 / 其他三个可折叠分组，且周期任务的
   "今天排班 / 本周稍后"不会重复出现；
4. **打卡语义**：周期任务打卡只写 ``task_logs``，**绝不写** ``tasks.completed``；
5. **周期任务不能被"延后"**：延后入口只对单次任务生效。

跑完截图到 ``tools/_shots/16~20_*.png``。

用法::

    python tools/e2e_phase4_ui.py
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["TIMEGUARD_DATA_DIR"] = tempfile.mkdtemp(prefix="tg_e2e_phase4_")
os.environ.setdefault("TIMEGUARD_NO_TOAST", "1")  # 测试不发真实系统通知（别往通知中心塞气泡）

from timeguard import recurrence as R  # noqa: E402
from timeguard import tasks as tasks_mod  # noqa: E402
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
#: 整个流程的兜底超时（正常大约 10 秒跑完）
WATCHDOG_MS = 120000

_u32 = ctypes.WinDLL("user32", use_last_error=True)
_g32 = ctypes.WinDLL("gdi32", use_last_error=True)
_u32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
_u32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
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
    """抓取顶层窗口的**完整**外观（含标题栏）。

    注意：``PrintWindow(PW_RENDERFULLCONTENT)`` 会把非客户区（标题栏）也画进
    位图，所以位图必须按 ``GetWindowRect`` 的尺寸分配；用 ``GetClientRect``
    会导致底部被裁掉约 30px（第一版截图里"保存"按钮被切了一半就是这个原因）。
    """
    from PIL import Image

    rect = wt.RECT()
    _u32.GetWindowRect(hwnd, ctypes.byref(rect))
    width, height = rect.right - rect.left, rect.bottom - rect.top
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
    return ctypes.windll.user32.GetAncestor(widget.winfo_id(), 2)


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
    settings.enable_popup = False                 # 关掉时长弹窗，别抢镜
    settings.enable_toast = False
    settings.task_check_on_start = False          # 关掉开机检查（本脚本自己驱动界面）
    settings.task_reminder_toast = False
    # 本脚本只验证"界面写进库的东西对不对"；提醒的触发/合并/补发由
    # tools/e2e_recurring_reminder.py 专门验证。这里关掉提醒派发，
    # 是为了让脚本无论几点运行都不会被弹窗打断截图（否则 18:00~20:00 之间跑就会弹）。
    settings.recurring_reminder_enabled = False

    store = UsageStore()
    notifier = Notifier()
    engine = UsageEngine(config, store, notifier)
    window = MainWindow(config, store, engine, notifier)
    panel = window.task_panel

    # ---- 把 messagebox 换成"记账"的假实现：既验证弹了提示，又不会把脚本卡住 ----
    notices: list[tuple[str, str]] = []

    def fake_info(title, message, **kw):        # noqa: ANN001, ANN003
        notices.append((str(title), str(message)))
        return "ok"

    def fake_askyesno(title, message, **kw):    # noqa: ANN001, ANN003
        notices.append((str(title), str(message)))
        return True

    tasks_mod.messagebox.showinfo = fake_info       # type: ignore[assignment]
    tasks_mod.messagebox.askyesno = fake_askyesno   # type: ignore[assignment]

    # ---- 监视"界面 → 调度器"的重排调用（审计发现的 S2：改完不重排）----
    reschedule_calls: list[str] = []
    real_reschedule = window.reschedule_recurring

    def spy_reschedule(reason: str = "tasks-changed") -> None:
        reschedule_calls.append(str(reason))
        real_reschedule(reason)

    window.reschedule_recurring = spy_reschedule     # type: ignore[assignment]

    # ---- Tk 回调里的异常必须让脚本**响亮地失败**，而不是静默挂在事件循环里 ----
    # （踩过的坑：after 回调里抛 NameError 被 Tk 吞掉，脚本一直"运行中"却没有输出）
    import traceback as _tb

    def on_tk_error(exc_type, exc_value, exc_tb) -> None:     # noqa: ANN001
        print("!! Tk 回调里发生异常，脚本中止 !!")
        _tb.print_exception(exc_type, exc_value, exc_tb)
        FAIL.append(f"Tk 回调异常: {exc_value!r}")
        finish(1)

    window.root.report_callback_exception = on_tk_error         # type: ignore[assignment]

    # ---- 用"自动填写并确认"的对话框替身驱动真实的 TaskPanel.add_task() ----
    real_dialog = tasks_mod.TaskDialog

    def auto_dialog(fill):                      # noqa: ANN001, ANN202
        """返回一个 TaskDialog 子类：创建后自动按 ``fill`` 填表并确认。"""

        class Auto(real_dialog):                # type: ignore[valid-type,misc]
            def __init__(self, master, **kw):   # noqa: ANN001, ANN003
                super().__init__(master, **kw)
                self.after(20, lambda: (fill(self), self._confirm()))

        return Auto

    def label_of(task_type: str) -> str:
        return R.TYPE_LABELS[task_type]

    # ================================================================ 场景
    def run() -> None:
        for _ in range(20):
            window.root.update()

        print("=== 端到端：阶段四（周期任务界面） ===")
        print(f"  数据目录: {os.environ['TIMEGUARD_DATA_DIR']}")
        print(f"  现在: {datetime.now():%Y-%m-%d %H:%M:%S}（周{'一二三四五六日'[datetime.now().isoweekday() - 1]}）")

        def watchdog() -> None:
            if STATE["done"]:
                return
            print(f"!! 超过 {WATCHDOG_MS / 1000:.0f} 秒仍未跑完，判定为卡死 !!")
            FAIL.append("流程超时（可能有回调链断掉）")
            finish(1)

        window.root.after(WATCHDOG_MS, watchdog)

        window.notebook.select(window.notebook_index_tasks)
        for _ in range(10):
            window.root.update()
        check("待办任务面板就绪", panel.winfo_exists() == 1)

        # ---- 0) 对话框：三种重复周期的截图 + 布局/预览校验 ----
        shot_types = [("once", "16_task_dialog_once"), ("daily", "17_task_dialog_daily"),
                      ("weekly", "18_task_dialog_weekly")]
        pending_shots = list(shot_types)

        def layout_ok(dialog, task_type: str) -> tuple[bool, str]:
            """对话框的可见性检查（比截图更硬：控件必须真的被映射且在窗口内）。"""
            dialog.update_idletasks()
            dialog.update()
            mapped = {
                "once": dialog.once_frame.winfo_ismapped(),
                "recur": dialog.recur_frame.winfo_ismapped(),
                "days": dialog.days_frame.winfo_ismapped(),
                "start": dialog.start_field.winfo_ismapped(),
                "remind": dialog.remind_box.winfo_ismapped(),
                "buttons": dialog.buttons_frame.winfo_ismapped(),
            }
            height_fits = dialog.winfo_height() + 2 >= dialog.winfo_reqheight()
            buttons, _ = dialog.buttons_frame.winfo_children()[:1], None
            button = buttons[0] if buttons else None
            inside = True
            if button is not None:
                inside = (button.winfo_rooty() - dialog.winfo_rooty() + button.winfo_reqheight()
                          <= dialog.winfo_height() + 2)
            detail = (f"mapped={mapped} 高 {dialog.winfo_height()}/{dialog.winfo_reqheight()}"
                      f" 按钮在内={inside}")
            if task_type == "once":
                good = mapped["once"] and not mapped["recur"] and mapped["buttons"] and inside
            elif task_type == "daily":
                good = (mapped["recur"] and mapped["start"] and mapped["remind"]
                        and not mapped["days"] and inside)
            else:
                good = mapped["recur"] and mapped["days"] and mapped["start"] and inside
            return bool(good and height_fits), detail

        def shot_next() -> None:
            if not pending_shots:
                after_shots()
                return
            task_type, name = pending_shots.pop(0)
            dialog = real_dialog(window.root, font_family=window.font_family)
            dialog.title_var.set(f"界面截图：{label_of(task_type)}任务")
            dialog.type_var.set(label_of(task_type))
            dialog._on_type_change()            # noqa: SLF001
            if task_type == "weekly":
                dialog._set_days((1, 3, 5))     # noqa: SLF001
            elif task_type == "daily":
                dialog.start_field.set("06:00")
                dialog.end_field.set("08:00")
                dialog.remind_var.set("15")
            # 预览必须跟着类型变
            text = dialog.preview_var.get()
            check(f"「{label_of(task_type)}」预览文案正确",
                  (label_of(task_type) + "任务") in text or ("单次任务" in text),
                  text)
            dialog.lift()

            def capture() -> None:
                good, detail = layout_ok(dialog, task_type)
                check(f"「{label_of(task_type)}」对话框布局完整（无缺失/裁切）", good, detail)
                OUT.mkdir(parents=True, exist_ok=True)
                try:
                    grab(grab_window_of(dialog)).save(OUT / f"{name}.png")
                    print(f"  截图已保存: {OUT / (name + '.png')}")
                except Exception as exc:            # noqa: BLE001
                    print(f"  截图失败（忽略）: {exc}")
                dialog.destroy()
                window.root.after(80, shot_next)

            window.root.after(500, capture)     # 等窗口真正完成映射与合成再抓图

        def after_shots() -> None:
            run_validation()

        # ---- 1) 校验：每周不选星期几 → 拒绝保存 ----
        def run_validation() -> None:
            dialog = real_dialog(window.root, font_family=window.font_family)
            dialog.title_var.set("每周但不选星期")
            dialog.type_var.set(label_of("weekly"))
            dialog._on_type_change()            # noqa: SLF001 - 会自动勾上今天
            auto_checked = [iso for iso, var in dialog.day_vars.items() if var.get()]
            check("切到「每周」会自动勾上今天（不会一天都没选）",
                  auto_checked == [datetime.now().isoweekday()], f"勾选 {auto_checked}")
            dialog._set_days(())                # noqa: SLF001 - 清空
            before = len(notices)
            dialog._confirm()                   # noqa: SLF001
            check("每周不选星期几被拦下（不保存、窗口不关）",
                  dialog.result is None and dialog.winfo_exists() == 1
                  and len(notices) > before and "至少要选择一天" in notices[-1][1],
                  notices[-1][1].replace("\n", " ") if notices else "（没有提示）")

            # 提前提醒填非法值 → 也要拦下
            dialog.type_var.set(label_of("daily"))
            dialog._on_type_change()            # noqa: SLF001
            dialog.title_var.set("提前提醒非法")
            dialog.remind_var.set("很快")
            before = len(notices)
            dialog._confirm()                   # noqa: SLF001
            check("提前提醒填非数字被拦下",
                  dialog.result is None and len(notices) > before
                  and "整数" in notices[-1][1],
                  notices[-1][1].replace("\n", " ") if notices else "")

            # 结束时间早于开始时间 → 询问后允许（按到 23:59 处理）
            dialog.remind_var.set("15")
            dialog.start_field.set("22:00")
            dialog.end_field.set("06:00")
            before = len(notices)
            dialog._confirm()                   # noqa: SLF001
            check("逆序时间段会先问一句再保存",
                  dialog.result is not None and len(notices) > before
                  and "不晚于开始时间" in notices[-1][1])
            dialog.destroy()
            window.root.after(60, step_add_tasks)

        # ---- 2) 走真实 add_task 链路建三种任务 ----
        created: dict[str, int] = {}

        def step_add_tasks() -> None:
            today = datetime.now().isoweekday()
            later = ((today + 1) % 7) + 1                # 今天之后的下一个星期（保证 7 天内）
            # 今天排班的每日任务：窗口设在 10 分钟后（截图里就能看到"还剩 …"）
            start = (datetime.now() + timedelta(minutes=10)).strftime("%H:%M")
            end = (datetime.now() + timedelta(minutes=70)).strftime("%H:%M")
            print(f"  每日任务时间窗: {start}~{end}（提前 5 分钟提醒）")
            print(f"  今天是周{today}，另一个星期取周{later}")

            def add(fill) -> None:                       # noqa: ANN001
                tasks_mod.TaskDialog = auto_dialog(fill)  # type: ignore[assignment]
                panel.add_task()

            add(lambda dlg: (dlg.title_var.set("每天早上跑步"),
                             dlg.type_var.set(label_of("daily")),
                             dlg._on_type_change(),          # noqa: SLF001
                             dlg.start_field.set(start), dlg.end_field.set(end),
                             dlg.remind_var.set("5")))
            # 每周：今天 + 另一个星期 各一次。
            # 时间段用和每日任务同一套"相对现在"的值 —— 不能写死 18:00~20:00：
            # v1.4 起"今天的时间窗已经过去"的周期任务会从明天才开始生效，
            # 晚上跑这个脚本时那条任务就不算今天了，断言会莫名其妙地挂。
            add(lambda dlg: (dlg.title_var.set("每周复盘"),
                             dlg.type_var.set(label_of("weekly")),
                             dlg._on_type_change(),          # noqa: SLF001
                             dlg._set_days((today, later)),  # noqa: SLF001
                             dlg.start_field.set(start), dlg.end_field.set(end),
                             dlg.remind_var.set("30")))
            # 今天不排班的每周任务 → 只能落在「本周待办」
            add(lambda dlg: (dlg.title_var.set("每周健身"),
                             dlg.type_var.set(label_of("weekly")),
                             dlg._on_type_change(),          # noqa: SLF001
                             dlg._set_days((later,)),        # noqa: SLF001
                             dlg.start_field.set("07:00"), dlg.end_field.set("08:00"),
                             dlg.remind_var.set("10")))
            add(lambda dlg: (dlg.title_var.set("单次：交周报"),
                             dlg.type_var.set(label_of("once")),
                             dlg._on_type_change(),          # noqa: SLF001
                             dlg.picker.set(datetime.now() + timedelta(hours=2))))
            add(lambda dlg: (dlg.title_var.set("单次：三天后交材料"),
                             dlg.type_var.set(label_of("once")),
                             dlg._on_type_change(),          # noqa: SLF001
                             dlg.picker.set(datetime.now() + timedelta(days=3))))
            add(lambda dlg: (dlg.title_var.set("单次：无期限的杂事"),
                             dlg.type_var.set(label_of("once")),
                             dlg._on_type_change(),          # noqa: SLF001
                             dlg.picker.set_none()))
            tasks_mod.TaskDialog = real_dialog           # type: ignore[assignment]

            for task in store.list_tasks("all") + store.recurring_tasks():
                created[task.title] = task.id
            check("六种任务都建出来了", len(created) == 6, f"{sorted(created)}")
            check("每次增删改都重排了调度器（审计项 S2）",
                  reschedule_calls.count("tasks-changed") == 6,
                  f"重排 {reschedule_calls}")
            window.root.after(80, step_verify_db)

        # ---- 3) 数据库字段核对 ----
        def step_verify_db() -> None:
            daily = store.get_task(created["每天早上跑步"])
            weekly = store.get_task(created["每周复盘"])
            once = store.get_task(created["单次：交周报"])
            check("每天任务的库字段正确",
                  daily is not None and daily.rule.task_type == R.TYPE_DAILY
                  and daily.due_at is None and daily.rule.remind_before_minutes == 5,
                  f"type={daily.rule.task_type} start={daily.rule.time_start} "
                  f"due={daily.due_at} before={daily.rule.remind_before_minutes}")
            today = datetime.now().isoweekday()
            later = ((today + 1) % 7) + 1
            check("每周任务的星期几正确落库",
                  weekly is not None and weekly.rule.days_of_week == tuple(sorted((today, later))),
                  f"days={weekly.rule.days_of_week}（期望 {tuple(sorted((today, later)))}）")
            check("每周任务的提前提醒正确落库",
                  weekly is not None and weekly.rule.remind_before_minutes == 30,
                  f"before={weekly.rule.remind_before_minutes}")
            check("单次任务仍走 due_at、没有周期字段",
                  once is not None and once.is_recurring is False and once.due_at is not None
                  and once.rule.days_of_week == (),
                  f"due={once.due_at} days={once.rule.days_of_week}")
            window.root.after(80, step_grouping)

        # ---- 4) 分组列表 ----
        def read_tree() -> dict[str, list[str]]:
            """读成 ``{分组标题: [行文字, ...]}``。"""
            result: dict[str, list[str]] = {}
            for group_id in panel.tree.get_children(""):
                title = panel.tree.item(group_id, "text")
                children = [panel.tree.item(child, "text") for child in panel.tree.get_children(group_id)]
                result[title] = children
            return result

        def group_rows(tree: dict[str, list[str]], key: str) -> list[str]:
            """按分组键取行文字（分组不存在时返回空列表，避免 StopIteration）。"""
            name = tasks_mod.GROUP_TITLES[key]
            return next((children for title, children in tree.items() if title.startswith(name)), [])

        def select_by_title(group_key: str, title: str) -> str | None:
            """在某个分组里按标题选中一行，返回 item id。"""
            group_id = panel._group_items.get(group_key)             # noqa: SLF001
            if group_id is None:
                return None
            for child in panel.tree.get_children(group_id):
                if panel.tree.item(child, "text") == title:
                    panel.tree.selection_set(child)
                    return child
            return None

        def step_grouping() -> None:
            panel.filter_var.set("pending")
            panel.refresh()
            for _ in range(6):
                window.root.update()
            tree = read_tree()
            titles = list(tree)
            check("列表按 今日 / 本周 / 其他 分组",
                  all(any(t.startswith(tasks_mod.GROUP_TITLES[name]) for t in titles)
                      for name in (tasks_mod.GROUP_TODAY, tasks_mod.GROUP_WEEK)),
                  f"分组 {titles}")

            today_rows = group_rows(tree, tasks_mod.GROUP_TODAY)
            week_rows = group_rows(tree, tasks_mod.GROUP_WEEK)
            check("今天排班的每日任务 + 单次任务都在『今日待办』",
                  "每天早上跑步" in today_rows and "单次：交周报" in today_rows,
                  f"今日 {today_rows}")
            check("今天排班的每周任务也在『今日待办』（不重复进本周）",
                  "每周复盘" in today_rows and "每周复盘" not in week_rows,
                  f"今日 {today_rows} / 本周 {week_rows}")
            check("今天不排班的每周任务落在『本周待办』",
                  "每周健身" in week_rows and "每周健身" not in today_rows,
                  f"本周 {week_rows}")
            check("三天后的单次任务落在『本周待办』",
                  "单次：三天后交材料" in week_rows, f"本周 {week_rows}")
            other_rows = group_rows(tree, tasks_mod.GROUP_OTHER)
            check("无期限的单次任务落在『其他』",
                  "单次：无期限的杂事" in other_rows, f"其他 {other_rows}")

            # ---- 窗口结束的瞬间：状态与颜色必须翻转（tick 的分支）----
            item = select_by_title(tasks_mod.GROUP_TODAY, "每天早上跑步")
            row = panel.selected_row()
            if item is not None and row is not None:
                now = datetime.now()
                row.window = tasks_mod.Occurrence(now.date(), now - timedelta(hours=2),
                                                  now - timedelta(hours=1))
                row.done, row.overdue = False, False
                panel.tick()
                for _ in range(2):
                    window.root.update()
                check("时间窗结束后 tick 会把状态翻成『已超期』并变红",
                      panel.tree.set(item, "status").endswith("已超期")
                      and "overdue" in panel.tree.item(item, "tags"),
                      f"{panel.tree.set(item, 'status')} / {panel.tree.item(item, 'tags')} / "
                      f"{panel.tree.set(item, 'countdown')}")
            panel.refresh()
            for _ in range(4):
                window.root.update()

            # 分组标题带 (已完成/总数)
            group_title = next((t for t in titles
                                if t.startswith(tasks_mod.GROUP_TITLES[tasks_mod.GROUP_TODAY])), "")
            check("分组标题显示 已完成/总数", "（0/3）" in group_title, group_title)

            # 折叠状态要在刷新后保留
            group_id = panel._group_items[tasks_mod.GROUP_TODAY]      # noqa: SLF001
            panel.tree.item(group_id, open=False)
            panel.refresh()
            for _ in range(4):
                window.root.update()
            group_id = panel._group_items[tasks_mod.GROUP_TODAY]      # noqa: SLF001
            check("折叠状态在刷新后保留", not panel.tree.item(group_id, "open"))
            panel.tree.item(group_id, open=True)
            panel.refresh()
            for _ in range(4):
                window.root.update()

            OUT.mkdir(parents=True, exist_ok=True)
            # 分栏不能把任何一边压扁：列表要看得见分组标题 + 两行，图表要能画出标题
            list_h = panel.paned.sashpos(0)
            chart_h = panel.paned.winfo_height() - list_h
            check("默认分栏同时留够列表与图表的高度",
                  list_h >= 100 and chart_h >= 150,
                  f"列表 {list_h}px / 图表 {chart_h}px（窗格 {panel.paned.winfo_height()}px）")
            try:
                grab(grab_window_of(window.root)).save(OUT / "19_task_panel_grouped.png")
                print(f"  截图已保存: {OUT / '19_task_panel_grouped.png'}")
            except Exception as exc:            # noqa: BLE001
                print(f"  截图失败（忽略）: {exc}")
            window.root.after(120, step_checkin)

        # ---- 5) 打卡语义 ----
        def step_checkin() -> None:
            daily_id = created["每天早上跑步"]
            item = select_by_title(tasks_mod.GROUP_TODAY, "每天早上跑步")
            check("能在列表里选中每日任务", item is not None)

            if item is not None:
                panel.toggle_selected()
                for _ in range(6):
                    window.root.update()
                log_row = store.get_task_log(daily_id, date.today())
                check("打卡写入 task_logs",
                      log_row is not None and log_row.completed is True,
                      f"log={log_row}")
                check("打卡**没有**写 tasks.completed（关键不变式）",
                      store.get_task(daily_id).completed is False)
                check("打卡后该行从『今日待办（未完成）』消失",
                      "每天早上跑步" not in group_rows(read_tree(), tasks_mod.GROUP_TODAY),
                      f"今日 {group_rows(read_tree(), tasks_mod.GROUP_TODAY)}")

                # 在"已完成"视图里应该能看到它
                panel.filter_var.set("completed")
                panel.refresh()
                for _ in range(4):
                    window.root.update()
                completed_rows = [row for rows in read_tree().values() for row in rows]
                check("切换到『已完成』能看到刚才的打卡",
                      "每天早上跑步" in completed_rows, f"{completed_rows}")
                # 打卡状态的截图（标题写"已打卡"，避免被读成永久完成）
                select_by_title(tasks_mod.GROUP_TODAY, "每天早上跑步")
                panel._update_hint()            # noqa: SLF001
                for _ in range(3):
                    window.root.update()
                OUT.mkdir(parents=True, exist_ok=True)
                try:
                    grab(grab_window_of(window.root)).save(OUT / "20_task_panel_checked_in.png")
                    print(f"  截图已保存: {OUT / '20_task_panel_checked_in.png'}")
                except Exception as exc:        # noqa: BLE001
                    print(f"  截图失败（忽略）: {exc}")

                # 撤销打卡
                select_by_title(tasks_mod.GROUP_TODAY, "每天早上跑步")
                panel.toggle_selected()
                for _ in range(4):
                    window.root.update()
                log_row = store.get_task_log(daily_id, date.today())
                check("撤销打卡把 completed 置回 False",
                      log_row is not None and log_row.completed is False)
                panel.filter_var.set("pending")
                panel.refresh()
                for _ in range(4):
                    window.root.update()

            window.root.after(120, step_postpone)

        # ---- 6) 周期任务不能被延后 ----
        def step_postpone() -> None:
            weekly = store.get_task(created["每周复盘"])
            before_start = weekly.rule.time_start
            select_by_title(tasks_mod.GROUP_TODAY, "每周复盘")
            before = len(notices)
            panel.postpone_selected(60)
            after = store.get_task(created["每周复盘"])
            check("周期任务点『延后』会被拒绝并给出说明",
                  len(notices) > before and "不能单独延后" in notices[-1][1],
                  notices[-1][1].replace("\n", " ") if notices else "")
            check("周期任务的时间窗没有被改掉", after.rule.time_start == before_start)

            select_by_title(tasks_mod.GROUP_TODAY, "单次：交周报")
            single = store.get_task(created["单次：交周报"])
            panel.postpone_selected(60)
            moved = store.get_task(created["单次：交周报"])
            check("单次任务『延后』仍然有效",
                  moved.due_at is not None and single.due_at is not None
                  and abs((moved.due_at - single.due_at).total_seconds() - 3600) < 1,
                  f"{single.due_at} → {moved.due_at}")

            panel.refresh()
            for _ in range(6):
                window.root.update()
            window.root.after(120, step_dialog_edit)

        # ---- 7) 编辑已有周期任务：对话框要把库里的值回填出来 ----
        def step_dialog_edit() -> None:
            weekly = store.get_task(created["每周复盘"])
            dialog = real_dialog(window.root, font_family=window.font_family, task=weekly)
            check("编辑周期任务时类型正确回填", dialog.type_var.get() == label_of("weekly"),
                  dialog.type_var.get())
            picked = tuple(sorted(iso for iso, var in dialog.day_vars.items() if var.get()))
            check("编辑周期任务时星期几正确回填", picked == weekly.rule.days_of_week,
                  f"{picked} vs {weekly.rule.days_of_week}")
            check("编辑周期任务时时间段正确回填",
                  dialog.start_field.get_text() == R.format_hhmm(weekly.rule.time_start)
                  and dialog.end_field.get_text() == R.format_hhmm(weekly.rule.time_end),
                  f"{dialog.start_field.get_text()}~{dialog.end_field.get_text()}")
            check("编辑周期任务时提前提醒正确回填", dialog.remind_var.get() == "30",
                  dialog.remind_var.get())
            # 直接保存（不改任何东西）→ 库里的规则不能变
            dialog._confirm()                   # noqa: SLF001
            draft = dialog.result
            check("未改动保存后草稿与原规则一致",
                  draft is not None and draft.task_type == R.TYPE_WEEKLY
                  and draft.days_of_week == R.format_iso_weekdays(weekly.rule.days_of_week)
                  and draft.remind_before_minutes == 30,
                  f"{draft}")
            dialog.destroy()

            # ---- 8) 编辑时切换类型：每周 → 每天，周期字段要跟着清 ----
            weekly2 = store.get_task(created["每周健身"])
            item = select_by_title(tasks_mod.GROUP_WEEK, "每周健身")
            check("能在『本周待办』里选中每周任务准备编辑", item is not None)
            tasks_mod.TaskDialog = auto_dialog(          # type: ignore[assignment]
                lambda dlg: (dlg.type_var.set(label_of("daily")), dlg._on_type_change()))
            panel.edit_selected()
            tasks_mod.TaskDialog = real_dialog           # type: ignore[assignment]
            switched = store.get_task(weekly2.id)
            check("每周 → 每天 后星期几被清空（不留『已改成每天却还写着星期』的脏数据）",
                  switched.rule.task_type == R.TYPE_DAILY and switched.rule.days_of_week == (),
                  f"type={switched.rule.task_type} days={switched.rule.days_of_week}")
            check("每周 → 每天 后开始/结束时间保留为新时长",
                  switched.rule.time_start.hour == 7 and switched.rule.time_end.hour == 8,
                  f"{switched.rule.time_start}~{switched.rule.time_end}")
            panel.refresh()
            for _ in range(4):
                window.root.update()
            window.root.after(80, finish_all)

        def finish_all() -> None:
            panel.filter_var.set("all")
            panel.refresh()
            counts = store.task_counts()
            check("今日统计自洽（total = pending + completed）",
                  counts.total == counts.pending + counts.completed,
                  f"total={counts.total} pending={counts.pending} completed={counts.completed}")
            check("今日待办计数包含了尚未打卡的周期任务",
                  counts.pending >= 3, f"pending={counts.pending}")
            check("打卡 / 撤销 / 延后都重排了调度器",
                  reschedule_calls.count("tasks-changed") >= 7, f"重排 {reschedule_calls}")
            window.root.after(100, lambda: finish(0))

        window.root.after(60, shot_next)

    def finish(code: int) -> None:
        if STATE["done"]:
            return
        STATE["done"] = True
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

    window.root.after(1200, run)
    window.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

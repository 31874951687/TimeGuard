"""GUI 冒烟测试（开发用，不属于交付内容）。

启动真实主窗口 -> 模拟引擎计时 -> 截图 -> 自动退出。
需要交互式桌面会话（有真实显示器），否则跳过截图。

用法：python tools/ui_smoke.py [截图输出目录]
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
import tempfile
import time
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 必须在导入 timeguard 之前指定临时数据目录，避免污染真实记录
DEMO_DIR = Path(tempfile.mkdtemp(prefix="tg_smoke_"))
os.environ["TIMEGUARD_DATA_DIR"] = str(DEMO_DIR)
os.environ.setdefault("TIMEGUARD_NO_TOAST", "1")  # 测试不发真实系统通知（别往通知中心塞气泡）
os.environ.setdefault("MPLBACKEND", "TkAgg")

from timeguard import winapi  # noqa: E402
from timeguard.config import Config  # noqa: E402
from timeguard.database import UsageStore  # noqa: E402
from timeguard.engine import UsageEngine  # noqa: E402
from timeguard.notifier import Notifier  # noqa: E402
from timeguard.ui import MainWindow  # noqa: E402
from timeguard.utils import last_n_days  # noqa: E402

OUT_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "tools" / "_shots"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def read_image(path: Path):
    """读图并立即释放文件句柄。

    注意：``Image.open`` 是惰性的，不 copy+close 的话文件一直被占用，
    后续再往同一个路径写就会报 WinError 32（踩过这个坑）。
    """
    from PIL import Image

    with Image.open(path) as handle:
        return handle.copy()


def seed_demo_data(store: UsageStore) -> None:
    """写入最近 6 天的演示数据，让图表有内容可看。"""
    days = last_n_days(7)[:-1]
    plan = [
        (["game.exe", "哔哩哔哩"], [4200, 3900]),
        (["Steam.exe", "YouTube"], [6300, 2400]),
        (["game.exe"], [9300]),
        (["抖音"], [5100]),
        (["game.exe", "哔哩哔哩", "知乎"], [7200, 3300, 900]),
        (["cs2.exe"], [2700]),
    ]
    for day, (names, seconds) in zip(days, plan):
        for name, secs in zip(names, seconds):
            store.add_seconds(name, secs, kind="web" if name in ("哔哩哔哩", "YouTube", "抖音", "知乎") else "game", day=day)
    # 今天：一点点数据 + 让引擎继续累加
    store.add_seconds("game.exe", 900, kind="game")
    store.add_seconds("哔哩哔哩", 600, kind="web")


def capture_window(hwnd: int, path: Path) -> bool:
    """用 GDI 抓取指定窗口的客户区，保存为 PNG（Pillow 只在保存时用一下）。"""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    user32.GetDC.argtypes = [wt.HWND]
    user32.GetDC.restype = wt.HDC
    user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
    user32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
    gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
    gdi32.CreateCompatibleDC.restype = wt.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wt.HBITMAP
    gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
    gdi32.SelectObject.restype = wt.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wt.HDC]
    gdi32.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, ctypes.c_uint, ctypes.c_uint,
                                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                    ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                    ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]

    rect = wt.RECT()
    # 用 GetWindowRect 而不是 GetClientRect：PrintWindow(PW_RENDERFULLCONTENT)
    # 会把标题栏也画进位图，按客户区分配尺寸会导致底部约 30px 被裁掉
    # （截图里"保存/开始干活"这类底部按钮被切一半就是这个原因）。
    user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        print(f"GetWindowRect 失败 hwnd={hwnd:#x} err={ctypes.get_last_error()}")
        return False
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        print(f"窗口尺寸异常 hwnd={hwnd:#x} {width}x{height} visible={user32.IsWindowVisible(hwnd)} "
              f"iconic={user32.IsIconic(hwnd)} title={buf.value!r} class={cls.value!r}")
        return False

    hdc_window = user32.GetDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_window)
    hbitmap = gdi32.CreateCompatibleBitmap(hdc_window, width, height)
    gdi32.SelectObject(hdc_mem, hbitmap)

    # PW_RENDERFULLCONTENT = 2，能抓到 DWM 合成后的内容
    ok = user32.PrintWindow(hwnd, hdc_mem, 2)
    if not ok:
        ok = user32.PrintWindow(hwnd, hdc_mem, 0)
    print(f"PrintWindow -> {ok}，尺寸 {width}x{height}")

    header = BITMAPINFOHEADER()
    header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    header.biWidth = width
    header.biHeight = -height        # 负数表示自上而下
    header.biPlanes = 1
    header.biBitCount = 32
    header.biCompression = 0         # BI_RGB

    buffer = ctypes.create_string_buffer(width * height * 4)
    gdi32.GetDIBits(hdc_mem, hbitmap, 0, height, buffer, ctypes.byref(header), 0)

    from PIL import Image

    image = Image.frombuffer("RGBA", (width, height), buffer, "raw", "BGRA", 0, 1)
    image.convert("RGB").save(path)
    print(f"截图已保存: {path}")

    gdi32.DeleteObject(hbitmap)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(hwnd, hdc_mem)
    return True


def seed_demo_tasks(store) -> None:
    """写入演示待办任务：超期 / 今天 / 明天 / 已完成（覆盖各种状态展示）。"""
    from datetime import datetime, timedelta

    now = datetime.now()
    store.add_task("把 TimeGuard 的周报写完并发给主管", now - timedelta(hours=3), priority=2)
    store.add_task("晚饭后散步 30 分钟", now + timedelta(hours=1))
    store.add_task("预约下周三的体检", now + timedelta(days=1, hours=2))
    store.add_task("复习英语单词 50 个", now + timedelta(days=2), priority=2)
    store.add_task("整理下载文件夹", None)          # 无期限
    done_id = store.add_task("交电费", now - timedelta(days=1))
    store.complete_task(done_id, True)
    done2 = store.add_task("给家里打电话", now - timedelta(hours=6))
    store.complete_task(done2, True)

    # 造一点历史数据，让“任务完成情况”图表有柱子（覆盖最近 5 天）
    from timeguard.database import TS_FMT

    demo_plan = [
        (2, 1, 0, "读书", "读 20 页专业书", "复习昨天的笔记"),
        (1, 0, 1, "整理", "整理项目文档", "清理浏览器标签"),
        (3, 2, 1, "学习", "做完一节网课", "写一段代码练习", "整理错题"),
        (0, 1, 2, "家务", "洗衣服", "给绿植浇水", "倒垃圾"),
        (1, 1, 0, "健康", "做 20 个深蹲", "拉伸 10 分钟"),
    ]
    for offset, plan in enumerate(demo_plan, start=1):
        on_time, late, overdue = plan[0], plan[1], plan[2]
        tags = plan[3:]
        day = now - timedelta(days=offset)
        created = day.strftime(TS_FMT)
        index = 0
        for _ in range(on_time):
            store._conn.execute(  # noqa: SLF001
                "INSERT INTO tasks(title, due_at, created_at, completed, completed_at) VALUES(?,?,?,1,?)",
                (f"{tags[index % len(tags)]}（按时）", (day + timedelta(hours=6)).strftime(TS_FMT),
                 created, (day + timedelta(hours=3)).strftime(TS_FMT)))
            index += 1
        for _ in range(late):
            store._conn.execute(  # noqa: SLF001
                "INSERT INTO tasks(title, due_at, created_at, completed, completed_at) VALUES(?,?,?,1,?)",
                (f"{tags[index % len(tags)]}（超期完成）", (day + timedelta(hours=2)).strftime(TS_FMT),
                 created, (day + timedelta(hours=9)).strftime(TS_FMT)))
            index += 1
        for _ in range(overdue):
            store._conn.execute(  # noqa: SLF001
                "INSERT INTO tasks(title, due_at, created_at, completed, completed_at) VALUES(?,?,?,0,NULL)",
                (f"{tags[index % len(tags)]}（未完成）", (day + timedelta(hours=2)).strftime(TS_FMT), created))
            index += 1
    store._conn.commit()  # noqa: SLF001


def main() -> int:
    winapi.set_dpi_awareness()
    config = Config()
    config.settings.daily_limit_minutes = 120
    config.settings.enable_toast = False
    config.settings.enable_popup = True
    config.settings.task_check_on_start = False      # 冒烟测试里手动触发，避免时序干扰
    store = UsageStore()
    seed_demo_data(store)
    seed_demo_tasks(store)

    notifier = Notifier()
    engine = UsageEngine(config, store, notifier)
    window = MainWindow(config, store, engine, notifier)

    # 让界面上的“正在计时”也动起来：伪造一个命中目标（不在默认名单里，避免依赖真实前台程序）
    config.settings.game_processes.append("demo-game.exe")

    def fake_tick() -> None:
        """每 500ms 手工触发一次计时，模拟命中“demo-game.exe”。"""
        engine._last_tick -= 0.5  # noqa: SLF001
        engine.tick(config.settings, winapi.ForegroundInfo(
            hwnd=1, pid=1234, exe="demo-game.exe", title="【演示】某游戏 客户端", fullscreen=False))
        window.root.after(500, fake_tick)

    window.root.after(500, fake_tick)
    engine.start()   # 真实监控当前前台窗口（DeepSeek Harness 不属于监控对象）

    def grab(name: str) -> None:
        """截图当前主窗口。"""
        hwnd = ctypes.windll.user32.GetAncestor(window.root.winfo_id(), 2)  # GA_ROOT
        capture_window(hwnd, OUT_DIR / f"{name}.png")

    def grab_main_when_ready(tries: int = 40) -> None:
        """等引擎的第一份快照到了再截主界面。

        踩过的坑：固定 1.5 秒截图时快照还没送到界面线程，截出来"今日已用/距离上限"
        是空的、图表也没画 —— 看着像程序坏了，其实只是截早了。
        """
        snapshot = window.snapshot
        if (snapshot is None or snapshot.total_seconds <= 0) and tries > 0:
            window.root.after(250, lambda: grab_main_when_ready(tries - 1))
            return
        grab("01_main")

    def tab_stats() -> None:
        window.notebook.select(0)
        window.refresh_chart()
        window.root.after(1500, lambda: grab("02_stats"))

    def tab_tasks() -> None:
        window.show_tasks_tab()
        window.root.after(1600, lambda: grab("03_tasks"))

    def tab_settings() -> None:
        """设置页：抓总览图，再定位到「窗口大小」项抓预览控件。"""
        window.notebook.select(2)

        def shot() -> None:
            grab("04_settings")
            page = window.settings_panel
            page.focus_key("window_size")           # 选中窗口大小项 -> 右侧渲染预览
            window.root.update()
            window.root.after(500, lambda: grab("10_settings_window_size"))
            preview = getattr(page, "preview", None)
            if preview is not None:
                window.root.after(900, lambda: capture_widget(preview, "11_window_preview"))

        window.root.after(1200, shot)

    def capture_widget(widget, name: str) -> None:
        """单独截取某个控件的区域（用主窗口截图裁剪）。"""
        import tkinter as _tk
        assert isinstance(widget, _tk.Widget)
        full = OUT_DIR / "_tmp_full.png"
        grab("_tmp_full")
        try:
            image = read_image(full)
            x = widget.winfo_rootx() - window.root.winfo_rootx()
            y = widget.winfo_rooty() - window.root.winfo_rooty()
            # 主窗口截图含标题栏，需要额外下移约 26px
            box = (max(0, x), max(0, y + 26), x + widget.winfo_width(), y + widget.winfo_height() + 26)
            image.crop(box).save(OUT_DIR / f"{name}.png")
            print(f"截图已保存: {OUT_DIR / (name + '.png')}")
            full.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001
            print(f"裁剪控件截图失败: {exc}")

    def task_dialog() -> None:
        """打开「添加任务」对话框（验证日期时间选择器）。"""
        window.notebook.select(1)
        from timeguard.tasks import TaskDialog
        dialog = TaskDialog(window.root, font_family=window.font_family)
        window.root.after(1500, lambda: grab_dialog(dialog, "05_task_dialog"))
        window.root.after(2200, dialog.destroy)

    def grab_dialog(dialog, name: str) -> None:
        top = ctypes.windll.user32.GetAncestor(dialog.winfo_id(), 2)
        capture_window(top, OUT_DIR / f"{name}.png")

    def popup_demo() -> None:
        window.popup_manager.skip()
        window.test_reminder()
        window.root.after(1500, lambda: grab("06_main_with_popup"))
        window.root.after(2000, grab_popup)

    def grab_popup() -> None:
        """单独抓取置顶的提醒弹窗（它是独立的 Toplevel 窗口）。"""
        popup = window.popup_manager._active  # noqa: SLF001
        if popup is None:
            print("弹窗未创建（可能被限流）")
            return
        top = ctypes.windll.user32.GetAncestor(popup.win.winfo_id(), 2)
        popup.win.attributes("-topmost", True)
        popup.win.update_idletasks()
        capture_window(top, OUT_DIR / "07_usage_popup.png")

    def task_reminder_demo() -> None:
        """触发开机待办提醒弹窗（核心新增功能）。"""
        if window.popup_manager.active:
            window.popup_manager._active.close()  # noqa: SLF001 - 先关掉使用时长弹窗
            window.root.after(400, task_reminder_demo)
            return
        # 演示数据里可能只有周期任务，而"未完成待办"只看单次任务；
        # 一条都没有时 check_tasks_on_start 只会弹提示条、不开窗 —— 这里先补一条，
        # 否则这一步永远截不到图（踩过：截图静默留成了上一次的旧图）。
        if not window.store.pending_tasks():
            from datetime import datetime, timedelta

            window.store.add_task("演示：查看今日待办", datetime.now() + timedelta(hours=2))
            window.task_panel.refresh()
        window.check_tasks_on_start()

        def grab_it() -> None:
            for widget in window.root.winfo_children():
                if widget.winfo_class() == "Toplevel" and isinstance(widget, tk.Toplevel):
                    title = widget.title()
                    # 阶段三之后这个窗叫「开机提醒」（合并了"错过的周期任务 + 未完成待办"），
                    # 所以这里两个关键词都要认，否则截图会静默变成上一次的旧图。
                    if "开机提醒" in title or "待办" in title:
                        top = ctypes.windll.user32.GetAncestor(widget.winfo_id(), 2)
                        widget.attributes("-topmost", True)
                        widget.update_idletasks()
                        if capture_window(top, OUT_DIR / "08_task_reminder.png"):
                            print(f"已截取提醒弹窗：{title!r}")
                        widget.destroy()
                        return
            print("未找到待办提醒窗口")

        window.root.after(1800, grab_it)

    def window_size_demo() -> None:
        """验证“窗口大小”设置：改成自定义尺寸 -> 立即应用 -> 截图为证。"""
        panel = window.settings_panel
        panel._set_size(1180, 700, preset="自定义")   # noqa: SLF001
        window.root.after(300, panel.apply_window_now)
        window.root.after(1200, lambda: grab("12_window_custom_1180x700"))
        # 再恢复标准尺寸
        window.root.after(1600, lambda: panel._set_size(1020, 800, preset="标准（1020 × 800）"))  # noqa: SLF001
        window.root.after(1900, panel.apply_window_now)

    def scroll_check() -> None:
        """冒烟检查：设置页快速滚动是否还有残影。

        历史背景：旧设置页把上百个控件塞进 Canvas + create_window，一次滚动重绘
        实测 128~155ms/帧，快速滑动必有文字残影（换滚动机制也治不好）。
        现在设置页是"原生 Treeview 列表 + 单行编辑器"，这里按真实帧率
        （一帧一批滚轮消息）连滚，用**抓图比对**判断画面是否每帧都真的更新了。

        两个会让这项检查**误报**的前置条件，必须先排除：
        * 有提醒弹窗还开着（它是置顶 + grab 的），主窗口可能被挡住甚至被系统
          短暂 unmap —— 抓图会失败，而抓图失败会被误读成"画面没更新"；
        * 主窗口不可见。
        所以这里先关掉遗留弹窗、确认主窗口可见，抓不到图就明确跳过而不是报警。
        """
        for child in window.root.winfo_children():
            if child.winfo_class() == "Toplevel":
                try:
                    child.destroy()
                except Exception:  # noqa: BLE001
                    pass
        window.show_window()
        for _ in range(10):
            window.root.update()
        probe = OUT_DIR / "_probe_window.png"
        if not capture_window(ctypes.windll.user32.GetAncestor(window.root.winfo_id(), 2), probe):
            print("滚动残影检查: 主窗口当前不可抓图，跳过（不是残影）")
            return
        probe.unlink(missing_ok=True)

        window.notebook.select(window.notebook_index_settings)
        for _ in range(10):
            window.root.update()

        page = window.settings_panel
        tree = page.tree

        def crop_tree(image):
            x = tree.winfo_rootx() - window.root.winfo_rootx()
            y = tree.winfo_rooty() - window.root.winfo_rooty()
            return image.crop((max(0, x), max(0, y + 26),
                               x + tree.winfo_width(), y + tree.winfo_height() + 26))

        def grab_window():
            full = OUT_DIR / "_tmp_scroll.png"
            grab("_tmp_scroll")
            return read_image(full)

        def diff_ratio(a, b) -> float:
            from PIL import ImageChops

            if a.size != b.size:
                return 1.0
            hist = ImageChops.difference(a, b).convert("L").histogram()
            return sum(hist[16:]) / max(1, a.width * a.height)

        frames = 10
        per_frame = 15
        base = crop_tree(grab_window())
        stale = 0
        start = time.perf_counter()
        for frame in range(frames):
            delta = -120 if frame % 2 == 0 else 120
            for _ in range(per_frame):
                tree.event_generate("<MouseWheel>", delta=delta,
                                    x=tree.winfo_width() // 2, y=tree.winfo_height() // 2)
            window.root.update()
            now = crop_tree(grab_window())
            if diff_ratio(base, now) < 0.002:
                stale += 1
            base = now
        elapsed = (time.perf_counter() - start) * 1000

        page.focus_key("daily_limit_minutes")
        window.root.update()
        print(f"滚动残影检查: {frames} 帧 × {per_frame} 条滚轮消息 -> 画面未更新 {stale}/{frames} 帧，"
              f"耗时 {elapsed:.0f} ms")
        if stale:
            print("  [警告] 存在画面未更新的帧 = 快速滑动会有残影")

    def finish() -> None:
        print("=== 运行时快照 ===")
        snapshot = engine.snapshot()
        print(f"今日累计: {snapshot.total_seconds:.1f}s  上限: {snapshot.limit_seconds:.0f}s  "
              f"超限: {snapshot.over_limit}  提醒次数: {snapshot.popup_count}")
        print(f"各对象: { {k: round(v, 1) for k, v in snapshot.target_seconds.items()} }")
        counts = store.task_counts()
        print(f"待办任务: 共 {counts.total} 条 / 待办 {counts.pending} / 已完成 {counts.completed} / "
              f"超期 {counts.overdue} / 今天到期 {counts.due_today}")
        print(f"近7天任务分桶: {[(s.day[5:], s.on_time, s.late, s.overdue) for s in store.task_stats_by_day(7)]}")
        print(f"托盘图标: {'已启动' if window._tray_icon else '不可用'}")  # noqa: SLF001
        print(f"数据目录: {DEMO_DIR}")
        print(f"日志文件: {DEMO_DIR / 'timeguard.log'}")
        # 顺便验证各控件都存在
        page = window.settings_panel
        setting_rows = [i for i in page.tree.get_children() if not i.startswith("__grp__")]
        group_rows = [i for i in page.tree.get_children() if i.startswith("__grp__")]
        print("控件自检:", all([
            window.bar.winfo_exists(), window.chart.winfo_exists(),
            window.tree.winfo_exists(),
            page.tree.winfo_exists(),
            len(setting_rows) >= 15,
            page.tree.cget("columns") == ("group", "name", "value", "desc"),
            window.task_panel.tree.winfo_exists(), window.task_panel.chart.winfo_exists(),
            len(window.task_panel.tree.get_children()) > 0,
            window.stats_paned.winfo_exists(),
        ]))
        print("任务面板行数:", len(window.task_panel.tree.get_children()))
        page.focus_key("window_size")
        window.root.update()
        print(f"设置表格: {len(setting_rows)} 个设置项 / {len(group_rows)} 个分组")
        print("窗口尺寸预设:", page.var_win_preset.get(),
              "| 目标:", (page.var_win_w.get(), page.var_win_h.get()))
        print("预览说明:", page.var_win_info.get())
        print("统计页分隔条位置:", window.stats_paned.sashpos(0),
              "| 待办页分隔条位置:", window.task_panel.paned.sashpos(0))
        engine.stop()
        window._stop_tray()  # noqa: SLF001
        store.close()
        window.root.quit()
        window.root.destroy()

    # 依次执行：主界面 -> 统计图 -> 待办页 -> 设置页 -> 添加任务对话框 -> 弹窗 -> 待办提醒
    # 注意：滚动残影检查必须排在"待办提醒"**之后并留足间隔**，
    # 否则它是和那个置顶弹窗同时跑，抓图会被挡住（误报成残影，踩过一次）。
    window.root.after(1200, grab_main_when_ready)
    window.root.after(2500, tab_stats)
    window.root.after(5000, tab_tasks)
    window.root.after(7500, tab_settings)
    window.root.after(10000, task_dialog)
    window.root.after(13500, popup_demo)
    window.root.after(17000, task_reminder_demo)
    window.root.after(19500, scroll_check)
    window.root.after(21500, window_size_demo)
    window.root.after(24000, finish)

    print("开始 GUI 冒烟测试...")
    window.run()
    print("GUI 冒烟测试结束")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

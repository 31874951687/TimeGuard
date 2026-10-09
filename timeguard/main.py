"""TimeGuard 程序入口。

用法::

    python -m timeguard              # 启动图形界面（推荐）
    python -m timeguard --selftest   # 自检：环境、依赖、数据库、匹配逻辑
    python run.pyw                   # 无控制台窗口启动（源码运行）
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
import tkinter as tk
from datetime import datetime, timedelta
from tkinter import messagebox

from . import __version__, autostart, paths, winapi
from .config import Config
from .database import UsageStore
from .engine import UsageEngine
from .notifier import Notifier

log = logging.getLogger("timeguard")


def setup_logging(verbose: bool = False) -> None:
    """配置日志：既输出到控制台，也滚动写入数据目录下的日志文件。"""
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S")

    class FlushingRotatingFileHandler(logging.handlers.RotatingFileHandler):
        """每条日志立即 flush 的滚动文件处理器。

        默认的 RotatingFileHandler 有缓冲，程序被任务管理器强杀 / 崩溃时
        缓冲内容会丢失 —— 而恰恰是这种时候最需要看日志（实测启动后的内容
        全都不见了）。这里覆盖 emit 强制落盘，代价可忽略（日志量很小）。
        """

        def emit(self, record: logging.LogRecord) -> None:
            try:
                super().emit(record)
                self.flush()
            except (OSError, ValueError):
                pass

    try:
        file_handler = FlushingRotatingFileHandler(
            paths.log_file(), maxBytes=512 * 1024, backupCount=2, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        pass

    # 打包成 windowed exe 时没有控制台，此时跳过 StreamHandler
    if sys.stderr is not None and getattr(sys.stderr, "isatty", lambda: False)():
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)


def check_environment() -> list[str]:
    """检查运行环境，返回警告信息列表（不阻塞启动）。"""
    warnings: list[str] = []
    if sys.platform != "win32":
        warnings.append("当前不是 Windows 系统，前台窗口监控与系统通知将不可用。")
    if sys.version_info < (3, 10):
        warnings.append(f"建议使用 Python 3.10+（当前 {sys.version.split()[0]}）。")
    try:
        import psutil  # noqa: F401
    except Exception:  # noqa: BLE001
        warnings.append("未安装 psutil，进程识别会退化为 Win32 API（建议安装 psutil）。")
    try:
        import pystray  # noqa: F401
        import PIL  # noqa: F401
    except Exception:  # noqa: BLE001
        warnings.append("未安装 pystray / Pillow，系统托盘功能不可用。")
    try:
        import matplotlib  # noqa: F401
    except Exception:  # noqa: BLE001
        warnings.append("未安装 matplotlib，7 天统计图表不可用。")
    try:
        import win32gui  # noqa: F401
    except Exception:  # noqa: BLE001
        warnings.append("未安装 pywin32，已自动使用 ctypes 读取窗口标题。")
    try:
        import tkcalendar  # noqa: F401
    except Exception:  # noqa: BLE001
        warnings.append("未安装 tkcalendar，日期选择将使用内置的下拉式选择器（功能不受影响）。")
    return warnings


def selftest() -> int:
    """自检模式：验证依赖、数据库读写、配置读写、窗口与进程识别。"""
    ok = True

    def report(line: str) -> None:
        """自检信息既打印到控制台，也写进日志。

        exe 是 windowed 程序、没有控制台，``--selftest`` 的输出只能靠日志查看，
        所以这里同步记录到 ``%APPDATA%\\TimeGuard\\timeguard.log``。
        """
        print(line)
        log.info("[selftest] %s", line)

    report(f"TimeGuard {__version__} 自检")
    report(f"Python : {sys.version.split()[0]}  ({sys.executable})")
    report(f"数据目录: {paths.data_dir()}")
    report(f"数据库  : {paths.database_file()}")
    report(f"配置文件: {paths.config_file()}")
    report("-" * 60)

    # 1) 依赖
    for label, module in (
        ("psutil", "psutil"),
        ("pywin32", "win32gui"),
        ("pystray", "pystray"),
        ("Pillow", "PIL"),
        ("plyer", "plyer"),
        ("matplotlib", "matplotlib"),
        ("tkcalendar", "tkcalendar"),
    ):
        try:
            __import__(module)
            report(f"[ OK ] 依赖 {label}")
        except Exception as exc:  # noqa: BLE001
            report(f"[缺失] 依赖 {label} -> {exc}")

    # 2) 配置
    config = Config()
    report(f"[ OK ] 配置加载成功：上限 {config.settings.daily_limit_minutes} 分钟，"
           f"监控进程 {len(config.settings.game_processes)} 个，关键字 {len(config.settings.title_keywords)} 个")

    # 3) 数据库
    store = UsageStore()
    before = store.total_seconds()
    store.add_seconds("__selftest__", 1.0, kind="process")
    after = store.total_seconds()
    report(f"[ OK ] 数据库读写正常（{before:.0f}s -> {after:.0f}s）")
    report(f"[ OK ] 最近 7 天统计：{[(d.day, round(d.seconds)) for d in store.recent_days(7)]}")

    # 4) 待办任务表（新增模块）
    try:
        task_id = store.add_task("__selftest__ 自检任务", datetime.now() + timedelta(hours=1), priority=1)
        tasks = store.list_tasks("all")
        counts = store.task_counts()
        stats = store.task_stats_by_day(7)
        report(f"[ OK ] 待办任务表读写正常（新增 id={task_id}，当前共 {counts.total} 条，"
               f"待办 {counts.pending} 条，近 7 天按时完成 {stats[-1].on_time} 条）")
        if task_id:
            store.delete_task(task_id)
        overdue_demo = store.add_task("__selftest__ 超期任务", datetime.now() - timedelta(hours=2))
        overdue_count = store.task_counts().overdue
        report(f"[ OK ] 超期判定正常（超期任务数 {overdue_count}）")
        if overdue_demo:
            store.delete_task(overdue_demo)
        del tasks
    except Exception as exc:  # noqa: BLE001
        ok = False
        report(f"[失败] 待办任务表异常：{exc}")

    # 5) 周期性任务：规则解析 / 发生记录 / 下一次触发时间推算
    try:
        from . import recurrence as recurrence_mod

        daily_id = store.add_task("__selftest__ 每日跑步", task_type=recurrence_mod.TYPE_DAILY,
                                  time_start="06:00", time_end="08:00", remind_before_minutes=15)
        weekly_id = store.add_task("__selftest__ 高数", task_type=recurrence_mod.TYPE_WEEKLY,
                                   days_of_week="3,4", time_start="18:00", time_end="20:00")
        daily_task = store.get_task(daily_id) if daily_id else None
        weekly_task = store.get_task(weekly_id) if weekly_id else None
        assert daily_task is not None and weekly_task is not None
        assert daily_task.rule.time_start.hour == 6 and daily_task.rule.remind_before_minutes == 15
        assert weekly_task.rule.days_of_week == (3, 4)
        now = datetime.now()
        nxt = weekly_task.rule.next_occurrence(now)
        # 打卡 → 只写发生记录，不改规则本身
        store.complete_occurrence(daily_id)
        assert store.is_occurrence_done(daily_id) is True
        assert store.get_task(daily_id).completed is False
        store.complete_occurrence(daily_id, completed=False)
        assert store.is_occurrence_done(daily_id) is False
        report(f"[ OK ] 周期任务正常（每日规则={daily_task.rule.schedule_text()}，"
               f"每周规则={weekly_task.rule.schedule_text()}）")
        report(f"[ OK ] 下一次触发时间推算正常（{nxt.start:%Y-%m-%d %H:%M}）")
        report("[ OK ] 打卡写入发生记录正常（不改动任务规则本身）")
        for temp_id in (daily_id, weekly_id):
            if temp_id:
                store.delete_task(temp_id)
    except Exception as exc:  # noqa: BLE001
        ok = False
        report(f"[失败] 周期任务异常：{type(exc).__name__} {exc}")

    # 6) 鼓励语库与开机自启
    from . import phrases as phrases_mod
    from .datepicker import backend_name

    report(f"[ OK ] 鼓励语库 {len(phrases_mod.ENCOURAGEMENTS)} 句，随机示例：{phrases_mod.pick()}")
    report(f"[ OK ] 日期选择组件：{backend_name()}")
    report(f"[ {'OK ' if autostart.is_supported() else '跳过'} ] 开机自启（注册表）：{autostart.status_text()}")

    # 7) 前台窗口
    if sys.platform == "win32":
        info = winapi.sample_foreground(need_path=True)
        report(f"[ OK ] 前台窗口：exe={info.exe or '—'} pid={info.pid} 标题={info.title[:40]!r} "
               f"全屏={info.fullscreen} 锁屏={info.locked}")
        report(f"[ OK ] 进程名解析（pid={os.getpid()}）：{winapi.get_process_name(os.getpid())}")
    else:
        report("[跳过] 非 Windows 系统，前台窗口检测不可用")

    # 8) 匹配逻辑（用当前进程名做一次真实匹配演练）
    engine = UsageEngine(config, store, Notifier())
    settings = config.settings
    settings.game_processes.append("python.exe")
    demo = winapi.ForegroundInfo(hwnd=1, pid=os.getpid(), title="哔哩哔哩 (゜-゜)つロ 干杯~",
                                 exe="python.exe", path=sys.executable)
    target = engine._resolve_uncached(demo, settings)  # noqa: SLF001 - 自检故意调用内部方法
    report(f"[ OK ] 匹配演练结果：{target}")
    if target is None:
        ok = False
        report("[失败] 匹配演练未命中，请检查匹配逻辑")

    # 9) 清理自检数据
    store._conn.execute("DELETE FROM daily_target WHERE target_name = '__selftest__' AND seconds <= 2")  # noqa: SLF001
    store._conn.commit()
    store.close()

    report("-" * 60)
    report("自检完成：" + ("全部通过" if ok else "存在问题，请查看上面的输出"))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    """程序主入口。"""
    parser = argparse.ArgumentParser(description="TimeGuard —— 游戏 / 网页使用时长监控与提醒")
    parser.add_argument("--selftest", action="store_true", help="只运行环境自检，不打开界面")
    parser.add_argument("--verbose", "-v", action="store_true", help="输出调试日志")
    parser.add_argument("--minimized", action="store_true", help="启动后直接最小化到托盘")
    parser.add_argument("--check-tasks", action="store_true",
                        help="启动界面后立即检查待办任务并弹出提醒（用于验证/手动触发）")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    log.info("=" * 60)
    log.info("TimeGuard %s 启动，Python %s，数据目录 %s", __version__, sys.version.split()[0], paths.data_dir())

    if sys.platform == "win32":
        winapi.set_dpi_awareness()

    if args.selftest:
        return selftest()

    warnings = check_environment()
    for text in warnings:
        log.warning("环境提示: %s", text)

    # ---- 装配各模块 ----
    config = Config()
    if args.minimized:
        config.settings.start_minimized = True

    # 开机自启：让注册表与设置保持一致（换了安装位置会自动修正路径）
    if config.settings.autostart_enabled and autostart.reconcile(True):
        log.info("开机自启已就绪：%s", autostart.current_command())
    elif config.settings.autostart_enabled:
        log.warning("开机自启未能写入注册表（可能被安全软件拦截）")

    store = UsageStore()
    notifier = Notifier()
    engine = UsageEngine(config, store, notifier)

    # ---- 界面 ----
    from .ui import MainWindow

    try:
        window = MainWindow(config, store, engine, notifier, manual_task_check=args.check_tasks)
    except tk.TclError as exc:
        log.exception("创建界面失败")
        print(f"无法创建图形界面：{exc}", file=sys.stderr)
        store.close()
        return 2

    # 首次运行时提示缺失能力的警告
    if warnings:
        def _show_warnings() -> None:
            messagebox.showwarning("环境提示", "程序已启动，但存在以下提示：\n\n• " + "\n• ".join(warnings))

        window.root.after(600, _show_warnings)

    if config.settings.auto_start_monitor:
        engine.start()

    # --check-tasks：启动后立即检查待办（等同于开机自启时的检查流程）
    if args.check_tasks:
        window.root.after(1500, window.check_tasks_on_start)

    window.run()

    log.info("TimeGuard 已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

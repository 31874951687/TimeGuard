"""TimeGuard 核心逻辑单元测试（不依赖真实前台窗口，全部可离线运行）。

运行方式::

    python -m pytest tests -q        # 有 pytest 时
    python tests/test_core.py        # 无 pytest 也能跑（自带简易执行器）

隔离策略：每个用例都拿到一个全新的空目录（``tmp_path``），
配置、数据库都建在该目录下，因此用例之间不会互相污染。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# ---- 必须在导入 timeguard 之前指向一个临时数据目录，避免污染真实记录 ----
os.environ["TIMEGUARD_DATA_DIR"] = tempfile.mkdtemp(prefix="timeguard_test_")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from timeguard import winapi  # noqa: E402
from timeguard.utils import use_utf8_console  # noqa: E402
from timeguard.config import Config  # noqa: E402
from timeguard.database import UsageStore  # noqa: E402
from timeguard.engine import UsageEngine  # noqa: E402
from timeguard.notifier import PopupManager, reminder_text  # noqa: E402
from timeguard.utils import (  # noqa: E402
    RateLimiter,
    fmt_duration,
    fmt_duration_short,
    last_n_days,
    read_json,
    write_json_atomic,
)


def _fresh_dir() -> Path:
    """申请一个全新的空目录（每个用例一份）。"""
    return Path(tempfile.mkdtemp(prefix="tg_case_"))


class _DummyNotifier:
    """替身通知器：单元测试里不真的发通知。"""

    backend = "dummy"

    def notify(self, *args, **kwargs) -> bool:
        return True

    def beep(self, *args, **kwargs) -> None:
        return None


# ---------------------------------------------------------------- 工具函数
def test_fmt_duration() -> None:
    assert fmt_duration(0) == "0秒"
    assert fmt_duration(45) == "45秒"
    assert fmt_duration(90) == "1分30秒"
    assert fmt_duration(3600) == "1小时00分00秒"
    assert fmt_duration(3600, with_seconds=False) == "1小时00分"
    assert fmt_duration(7200, with_seconds=False) == "2小时00分"
    assert fmt_duration_short(65) == "1m"
    assert fmt_duration_short(3700) == "1h01m"


def test_last_n_days() -> None:
    from datetime import date

    days = last_n_days(7)
    assert len(days) == 7
    assert days == sorted(days)
    assert days[-1] == date.today().isoformat()


def test_json_roundtrip_and_corruption() -> None:
    base = _fresh_dir()
    path = base / "sub" / "demo.json"
    write_json_atomic(path, {"中文": [1, 2, 3]})
    assert read_json(path)["中文"] == [1, 2, 3]

    path.write_text("{ 这不是合法 JSON", encoding="utf-8")
    assert read_json(path, default={"fallback": True}) == {"fallback": True}


def test_rate_limiter() -> None:
    limiter = RateLimiter(60)
    assert limiter.allow("k") is True
    assert limiter.allow("k") is False
    assert limiter.remaining("k") > 0
    limiter.reset("k")
    assert limiter.allow("k") is True


# ---------------------------------------------------------------- 配置
def test_config_defaults_and_persistence() -> None:
    path = _fresh_dir() / "config.json"
    config = Config(path)
    assert config.settings.daily_limit_minutes == 120
    assert config.settings.daily_limit_seconds == 7200
    assert config.settings.reminder_interval_minutes == 30

    config.settings.daily_limit_minutes = 90
    config.settings.add_game("MyGame.exe")
    config.settings.add_keyword("B站")
    config.save()

    reloaded = Config(path)
    assert reloaded.settings.daily_limit_minutes == 90
    assert "MyGame.exe" in reloaded.settings.game_processes
    assert "B站" in reloaded.settings.title_keywords

    assert reloaded.settings.add_game("mygame.exe") is False   # 重复添加被拒绝
    assert reloaded.settings.remove_keyword("B站") is True
    assert "B站" not in reloaded.settings.title_keywords


def test_config_tolerates_bad_json() -> None:
    path = _fresh_dir() / "broken.json"
    path.write_text("{{{ 坏文件", encoding="utf-8")
    config = Config(path)
    assert config.settings.daily_limit_minutes == 120  # 回退默认值而不是崩溃


def test_config_ignores_unknown_and_bad_types() -> None:
    path = _fresh_dir() / "weird.json"
    write_json_atomic(path, {
        "daily_limit_minutes": 45,
        "unknown_key": "忽略我",
        "game_processes": ["a.exe", "", "  b.exe  "],
        "enable_toast": False,
    })
    settings = Config(path).settings
    assert settings.daily_limit_minutes == 45
    assert settings.game_processes == ["a.exe", "b.exe"]
    assert settings.enable_toast is False


# ---------------------------------------------------------------- 数据库
def test_store_accumulate_and_query() -> None:
    store = UsageStore(_fresh_dir() / "usage.db")
    store.add_seconds("game.exe", 30, kind="game")
    store.add_seconds("game.exe", 30.5, kind="game")
    store.add_seconds("哔哩哔哩", 15, kind="web")

    assert round(store.total_seconds(), 1) == 75.5
    targets = store.today_targets()
    assert targets[0].name == "game.exe"
    assert round(targets[0].seconds, 1) == 60.5
    assert targets[1].kind == "web"

    recent = store.recent_days(7)
    assert len(recent) == 7
    assert round(recent[-1].seconds, 1) == 75.5

    hourly = store.hourly_today()
    assert abs(sum(hourly.values()) - 75.5) < 0.001

    by_target = store.recent_days_by_target(7)
    assert "game.exe" in by_target

    csv_path = _fresh_dir() / "export.csv"
    assert store.export_csv(csv_path, days=7) >= 2
    csv_text = csv_path.read_text(encoding="utf-8-sig")
    assert csv_text.startswith("【使用时长】")       # 第一段：使用时长明细
    assert "【任务定义】" in csv_text                 # 第二段：任务（单次 + 周期规则）
    assert "【周期任务打卡记录】" in csv_text          # 第三段：周期任务按天打卡
    store.add_task("导出测试任务", None)
    store.export_csv(csv_path, days=7)
    assert "导出测试任务" in csv_path.read_text(encoding="utf-8-sig")

    store.reset_day()
    assert store.total_seconds() == 0.0
    store.close()


def test_store_ignores_nonpositive_seconds() -> None:
    store = UsageStore(_fresh_dir() / "usage.db")
    store.add_seconds("game.exe", 0)
    store.add_seconds("game.exe", -5)
    assert store.total_seconds() == 0.0
    store.close()


# ---------------------------------------------------------------- 匹配逻辑
def _make_engine(settings_mutator=None):
    """构造一个隔离的引擎实例（独立配置 + 独立数据库）。"""
    base = _fresh_dir()
    config = Config(base / "cfg.json")
    store = UsageStore(base / "eng.db")
    if settings_mutator is not None:
        settings_mutator(config.settings)
    engine = UsageEngine(config, store, _DummyNotifier())  # type: ignore[arg-type]
    return engine, config, store


def test_match_game_process() -> None:
    engine, config, store = _make_engine(lambda s: s.game_processes.append("timeguard-test.exe"))
    settings = config.settings

    info = winapi.ForegroundInfo(hwnd=1, pid=4242, exe="timeguard-test.exe", title="某游戏")
    target = engine._resolve_uncached(info, settings)  # noqa: SLF001
    assert target is not None and target.kind == "game" and target.name == "timeguard-test.exe"

    # 大小写不敏感
    info2 = winapi.ForegroundInfo(hwnd=1, pid=4242, exe="TIMEGUARD-TEST.EXE", title="某游戏")
    assert engine._resolve_uncached(info2, settings) is not None  # noqa: SLF001

    # 只是名字接近但不同 -> 不匹配（避免误伤）
    similar = winapi.ForegroundInfo(hwnd=1, pid=4243, exe="timeguard-test-helper.exe", title="别的程序")
    assert engine._resolve_uncached(similar, settings) is None  # noqa: SLF001
    store.close()


def test_match_browser_keyword() -> None:
    engine, config, store = _make_engine()
    settings = config.settings

    hit = winapi.ForegroundInfo(hwnd=1, pid=99, exe="chrome.exe",
                                title="【4K】测试视频_哔哩哔哩_bilibili")
    target = engine._resolve_uncached(hit, settings)  # noqa: SLF001
    assert target is not None and target.kind == "web"
    assert target.name in settings.title_keywords

    # 浏览器但标题不命中关键字 -> 不计时
    miss = winapi.ForegroundInfo(hwnd=1, pid=99, exe="chrome.exe", title="公司内部管理系统")
    assert engine._resolve_uncached(miss, settings) is None  # noqa: SLF001

    # 非浏览器进程即使标题命中也不算网页
    other = winapi.ForegroundInfo(hwnd=1, pid=98, exe="notepad.exe", title="哔哩哔哩 笔记")
    assert engine._resolve_uncached(other, settings) is None  # noqa: SLF001
    store.close()


def test_ignores_minimized_locked_and_empty() -> None:
    engine, config, store = _make_engine(lambda s: s.game_processes.append("demo.exe"))
    settings = config.settings

    assert engine._resolve_uncached(winapi.ForegroundInfo(hwnd=0, exe=""), settings) is None  # noqa: SLF001
    assert engine._resolve_uncached(  # noqa: SLF001
        winapi.ForegroundInfo(hwnd=1, pid=1, exe="demo.exe", minimized=True), settings
    ) is None
    assert engine._resolve_uncached(  # noqa: SLF001
        winapi.ForegroundInfo(hwnd=1, pid=1, exe="demo.exe", locked=True), settings
    ) is None
    assert engine._resolve(  # noqa: SLF001 - 带缓存的路径同样要拒绝最小化窗口
        winapi.ForegroundInfo(hwnd=1, pid=1, exe="demo.exe", minimized=True), settings
    ) is None
    store.close()


def test_short_title_truncates() -> None:
    engine, config, store = _make_engine()
    short = engine._short_title("标题" * 100)  # noqa: SLF001
    assert len(short) <= 70 and short.endswith("…")
    store.close()


# ---------------------------------------------------------------- 计时行为
def _simulate(engine, settings, info, ticks: int, delta: float) -> None:
    """模拟 ``ticks`` 次采样，每次间隔 ``delta`` 秒。

    通过把引擎内部的时间基准往前推 ``delta`` 秒来伪造“真实经过了 delta 秒”，
    这样计时逻辑（休眠丢弃、暂停、目标切换）都能被确定性地验证。
    """
    for _ in range(ticks):
        engine._last_tick -= delta  # noqa: SLF001
        engine.tick(settings, info)


def assert_seconds(actual: float, expected: float, tolerance: float = 0.3) -> None:
    """带容差的时长断言：避免真实时钟带来的毫秒级浮动导致偶发失败。"""
    assert abs(actual - expected) <= tolerance, f"期望约 {expected}s，实际 {actual:.3f}s"


def test_counting_accumulates_only_for_targets() -> None:
    engine, config, store = _make_engine(lambda s: (s.game_processes.append("demo.exe"),
                                                    setattr(s, "daily_limit_minutes", 600)))
    settings = config.settings
    hit = winapi.ForegroundInfo(hwnd=1, pid=7, exe="demo.exe", title="Demo Game")

    _simulate(engine, settings, hit, ticks=5, delta=1.0)
    engine.flush()
    assert_seconds(engine.snapshot().total_seconds, 5.0)
    assert_seconds(store.total_seconds(), 5.0)

    # 切到无关程序：不再累计
    miss = winapi.ForegroundInfo(hwnd=2, pid=8, exe="explorer.exe", title="文件夹")
    _simulate(engine, settings, miss, ticks=5, delta=1.0)
    engine.flush()
    assert_seconds(engine.snapshot().total_seconds, 5.0)

    # 休眠 / 挂起造成的超大间隔应被丢弃
    _simulate(engine, settings, hit, ticks=3, delta=999.0)
    engine.flush()
    assert_seconds(engine.snapshot().total_seconds, 5.0)

    # 最小化或锁屏时暂停累计
    minimized = winapi.ForegroundInfo(hwnd=1, pid=7, exe="demo.exe", title="Demo", minimized=True)
    _simulate(engine, settings, minimized, ticks=4, delta=1.0)
    engine.flush()
    assert_seconds(engine.snapshot().total_seconds, 5.0)
    store.close()


def test_pause_stops_counting() -> None:
    engine, config, store = _make_engine(lambda s: (s.game_processes.append("demo.exe"),
                                                    setattr(s, "daily_limit_minutes", 600)))
    settings = config.settings
    hit = winapi.ForegroundInfo(hwnd=1, pid=7, exe="demo.exe", title="Demo")

    _simulate(engine, settings, hit, ticks=3, delta=1.0)
    engine.toggle_pause(True)
    _simulate(engine, settings, hit, ticks=5, delta=1.0)
    engine.flush()
    assert_seconds(engine.snapshot().total_seconds, 3.0)
    assert engine.paused is True

    engine.toggle_pause(False)
    _simulate(engine, settings, hit, ticks=2, delta=1.0)
    engine.flush()
    assert_seconds(engine.snapshot().total_seconds, 5.0)
    store.close()


def test_per_target_timing_mode() -> None:
    def _setup(s):
        s.game_processes.append("demo.exe")
        s.title_keywords = ["哔哩哔哩"]
        s.daily_limit_minutes = 600

    engine, config, store = _make_engine(_setup)
    settings = config.settings
    game = winapi.ForegroundInfo(hwnd=1, pid=7, exe="demo.exe", title="Demo")
    web = winapi.ForegroundInfo(hwnd=2, pid=8, exe="chrome.exe", title="视频_哔哩哔哩")

    _simulate(engine, settings, game, ticks=4, delta=1.0)
    _simulate(engine, settings, web, ticks=6, delta=1.0)
    engine.flush()

    snapshot = engine.snapshot()
    assert_seconds(snapshot.total_seconds, 10.0)
    assert_seconds(snapshot.target_seconds["demo.exe"], 4.0)
    assert_seconds(snapshot.target_seconds["哔哩哔哩"], 6.0)
    store.close()


def test_limit_triggers_callback_once_per_interval() -> None:
    def _setup(s):
        s.game_processes.append("demo.exe")
        s.daily_limit_minutes = 1        # 上限 60 秒
        s.reminder_interval_minutes = 30
        s.enable_toast = False

    engine, config, store = _make_engine(_setup)
    settings = config.settings
    fired: list[tuple] = []
    engine.on_limit_exceeded = lambda *args: fired.append(args)
    hit = winapi.ForegroundInfo(hwnd=1, pid=7, exe="demo.exe", title="Demo")

    _simulate(engine, settings, hit, ticks=59, delta=1.0)
    assert fired == []                      # 还没到上限

    _simulate(engine, settings, hit, ticks=2, delta=1.0)
    assert len(fired) == 1                  # 首次超限立即提醒一次

    _simulate(engine, settings, hit, ticks=30, delta=1.0)
    assert len(fired) == 1                  # 30 分钟间隔内不重复打扰
    assert engine.snapshot().over_limit is True
    assert engine.snapshot().limit_seconds == 60
    store.close()


def test_no_timing_for_untracked_foreground() -> None:
    engine, config, store = _make_engine(lambda s: setattr(s, "daily_limit_minutes", 600))
    settings = config.settings
    miss = winapi.ForegroundInfo(hwnd=5, pid=11, exe="winword.exe", title="文档.docx")

    _simulate(engine, settings, miss, ticks=10, delta=1.0)
    engine.flush()
    assert engine.snapshot().total_seconds == 0.0
    assert store.total_seconds() == 0.0
    store.close()


def test_reset_today_clears_records() -> None:
    engine, config, store = _make_engine(lambda s: (s.game_processes.append("demo.exe"),
                                                    setattr(s, "daily_limit_minutes", 600)))
    settings = config.settings
    hit = winapi.ForegroundInfo(hwnd=1, pid=7, exe="demo.exe", title="Demo")
    _simulate(engine, settings, hit, ticks=6, delta=1.0)
    engine.flush()
    assert store.total_seconds() > 0

    engine.reset_today()
    engine.flush()
    assert engine.snapshot().total_seconds == 0.0
    assert store.total_seconds() == 0.0
    store.close()


# ---------------------------------------------------------------- 提醒
def test_reminder_text() -> None:
    title, message, detail = reminder_text("哔哩哔哩", 7200, 7200)
    assert "哔哩哔哩" in message
    assert "2小时" in message
    assert "今日已用" in detail
    assert title.startswith("注意")


def test_popup_rate_limit() -> None:
    """弹窗在冷却时间内应被丢弃（用假的 tkinter master，不真的开窗口）。"""

    class _FakeMaster:
        pass

    manager = PopupManager(_FakeMaster())  # type: ignore[arg-type]
    assert manager.request("t", "m", "d", cooldown=60) is True
    assert manager.request("t", "m", "d", cooldown=60) is False   # 冷却中
    manager.skip()
    assert manager.request("t", "m", "d", cooldown=60) is True


def test_popup_queue_pump_and_defer() -> None:
    """验证 pump 会弹出队列里的提醒，并且已有弹窗时不会叠加。"""
    import timeguard.notifier as notifier_mod

    created: list[dict] = []

    class _FakePopup:
        def __init__(self, master, **kwargs):
            created.append(kwargs)
            self._closed = False

        def close(self):
            self._closed = True

    class _FakeMaster:
        pass

    original = notifier_mod.ReminderPopup
    notifier_mod.ReminderPopup = _FakePopup  # type: ignore[misc]
    try:
        manager = PopupManager(_FakeMaster())  # type: ignore[arg-type]

        # 全屏暂缓 -> 稍后补发
        manager.defer("暂缓标题", "暂缓正文", "详情")
        assert manager.has_deferred is True
        manager.flush_deferred()
        assert manager.has_deferred is False
        assert manager.pending.qsize() == 1
        assert manager.popup_count_today == 0      # 队列里还没真正弹

        manager.pump()
        assert len(created) == 1
        assert created[0]["message"] == "暂缓正文"
        assert manager.active is True
        assert manager.popup_count_today == 1

        manager.pump()                             # 已有弹窗，不叠加
        assert len(created) == 1

        created[0]["on_close"]()                   # 模拟用户点击“我知道了”
        assert manager.active is False
    finally:
        notifier_mod.ReminderPopup = original  # type: ignore[misc]


# ---------------------------------------------------------------- 简易执行器
def _main() -> int:
    """无 pytest 时的极简测试执行器（每个用例独立空目录）。"""
    use_utf8_console()          # 英文系统下打印中文不再抛 UnicodeEncodeError
    import traceback

    tests = [
        (name, func)
        for name, func in sorted(globals().items())
        if name.startswith("test_") and callable(func)
    ]
    passed, failed = 0, 0
    for name, func in tests:
        try:
            func()
            print(f"[PASS] {name}")
            passed += 1
        except Exception:  # noqa: BLE001
            print(f"[FAIL] {name}")
            traceback.print_exc()
            failed += 1
    print("-" * 60)
    print(f"通过 {passed} 个，失败 {failed} 个")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())

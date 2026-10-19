"""v1.7 布局改版端到端验收：顶栏精简 / 计时口径胶囊 / 明细内联 / 对象占比环图。

用户的原始要求（原话）：

1. "标题下方的信息过多，例如用户不需要通过标题下方的区域来了解监控的时间和
   提醒间隔……合并计时应该放在监控计时旁边，其他在我第一张图的功能都要按照
   我上述的思想去改变位"；
2. "数据统计部分有些内容可以分开放，例如今日各对象明细就可以放在监控计时的详情中"。

选定的方案是 A（顶栏极简 + 明细内联折叠）+ 合并计时可点切换 + 统计页补环图，
所以这里逐条验收：

* 顶栏只留 "● 监控中 / ○ 已暂停"，不再出现上限 / 提醒间隔 / 计时模式 / 通知后端；
* 计时卡片里出现可点的"合并计时 ⇄ 分别计时"胶囊，点一下配置真的变；
* 计时卡片底部有"今日各对象明细"折叠条：默认收起 → 点开有行 → 再点收起；
* 统计页下半是对象占比环图（按占比画弧 + 图例 + 汇总句）；
* 通知后端搬到了设置页（读得出真实值）。

用法：``python tools/e2e_layout_v17.py``（自带 watchdog，超过 180 秒自动判失败）。
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback
from datetime import datetime, timedelta
from pathlib import Path

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _HERE)

DATA_DIR = Path(tempfile.mkdtemp(prefix="timeguard_v17_"))
os.environ["TIMEGUARD_DATA_DIR"] = str(DATA_DIR)
os.environ["TIMEGUARD_NO_TOAST"] = "1"

from PIL import ImageGrab  # noqa: E402

from timeguard import winapi  # noqa: E402
from timeguard.config import Config  # noqa: E402
from timeguard.database import UsageStore  # noqa: E402
from timeguard.engine import Snapshot, UsageEngine  # noqa: E402
from timeguard.notifier import Notifier  # noqa: E402
from timeguard.utils import use_utf8_console  # noqa: E402

OUT_DIR = Path(_HERE) / "tools" / "_shots"
OUT_DIR.mkdir(parents=True, exist_ok=True)

PASSED = 0
FAILED = 0
STATE = {"done": False}


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  [通过] {name}" + (f"　{detail}" if detail else ""))
    else:
        FAILED += 1
        print(f"  [失败] {name}" + (f"　{detail}" if detail else ""))


def pump(root, seconds: float = 0.4) -> None:
    """跑几帧事件循环（不要用 sleep 死等：Tk 的布局/重绘要靠 update 推进）。"""
    end = datetime.now() + timedelta(seconds=seconds)
    while datetime.now() < end:
        root.update()
        root.after(10)


def shot(root, name: str) -> Path:
    """整窗截图（GetWindowRect + ImageGrab，和项目其它 e2e 一致）。"""
    import ctypes

    root.update_idletasks()
    hwnd = ctypes.windll.user32.GetAncestor(root.winfo_id(), 2)
    rect = ctypes.wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
    path = OUT_DIR / f"{name}.png"
    ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom)).save(path)
    print(f"  截图已保存: {path}（{rect.right - rect.left}x{rect.bottom - rect.top}）")
    return path


def seed_usage(store: UsageStore) -> list[tuple[str, float]]:
    """造几条今天的监控记录（顶栏/环图/明细都需要真实数据）。"""
    rows = [("哔哩哔哩动画", "web", 42 * 60), ("高数 - 学习", "web", 31 * 60),
            ("Minecraft", "game", 18 * 60), ("微信", "process", 4 * 60)]
    for name, kind, seconds in rows:
        store.add_seconds(name, float(seconds), kind=kind)
    return [(name, float(seconds)) for name, _kind, seconds in rows]


def main() -> int:
    use_utf8_console()
    from timeguard.ui import MainWindow

    winapi.set_dpi_awareness()
    cfg = Config()
    cfg.settings.enable_popup = False
    cfg.settings.count_mode = "total"
    cfg.save()
    store = UsageStore()
    notifier = Notifier()
    engine = UsageEngine(cfg, store, notifier)
    seed_usage(store)

    window = MainWindow(cfg, store, engine, notifier)

    def watchdog() -> None:
        if STATE["done"]:
            return
        STATE["done"] = True
        print("!! 超过 180 秒仍未跑完，判定为卡死 !!")
        print("-" * 60)
        print(f"结果: 有失败项（通过 {PASSED} / 失败 {FAILED + 1}）")
        try:
            window._stop_tray()   # noqa: SLF001
            window.root.destroy()
        finally:
            os._exit(1)

    window.root.after(180_000, watchdog)

    def fake_snapshot(monitoring: bool = True, total: float = 95 * 60) -> Snapshot:
        """造一个"看起来像真的"快照：上限要跟配置一致，别让卡片和图表各说一套。"""
        return Snapshot(day=datetime.now().strftime("%Y-%m-%d"), monitoring=monitoring,
                        total_seconds=total,
                        limit_seconds=float(cfg.settings.daily_limit_seconds),
                        target_seconds={name: sec for name, sec in
                                        [("哔哩哔哩动画", 42 * 60), ("高数 - 学习", 31 * 60)]})

    def run() -> None:
        try:
            window.show_window()
            window.snapshot = fake_snapshot()
            window._refresh_status()      # noqa: SLF001
            window._refresh_tree()        # noqa: SLF001
            pump(window.root, 0.6)

            # ------------------------------------------------ 1) 顶栏精简
            print("-- 1) 顶栏只留实时状态 --")
            header = window.label_status.cget("text")
            check("顶栏状态只有「● 监控中」", header == "● 监控中", header)
            for banned in ("上限", "提醒间隔", "合并计时", "分别计时", "通知后端", "plyer"):
                check(f"顶栏不再出现「{banned}」", banned not in header, header)
            check("顶栏还在标题右侧（没被挪走）",
                  window.label_status.winfo_ismapped() == 1
                  and window.label_status.winfo_rootx() > window.root.winfo_rootx(),
                  f"x={window.label_status.winfo_rootx()}")
            window.snapshot = fake_snapshot(monitoring=False)
            window._refresh_status()      # noqa: SLF001
            pump(window.root, 0.2)
            check("暂停后状态变成「○ 已暂停」",
                  window.label_status.cget("text") == "○ 已暂停",
                  window.label_status.cget("text"))
            window.snapshot = fake_snapshot()
            window._refresh_status()      # noqa: SLF001

            # ------------------------------------------------ 2) 计时口径胶囊
            print("-- 2) 合并计时胶囊（就在计时旁边，点了即切换）--")
            pill = window.btn_count_mode
            check("计时卡片里有口径胶囊", pill.winfo_exists() == 1 and pill.winfo_ismapped() == 1)
            check("胶囊初始显示「合并计时」（配置是 total）",
                  pill.cget("text") == "合并计时", pill.cget("text"))
            # 胶囊必须在"今日已用 / 上限"那一行的容器里（不是顶栏、不是别处）
            same_row = pill.master is window.label_used.master
            check("胶囊和「今日已用」在同一行（真的贴着计时）", same_row,
                  f"{pill.master} vs {window.label_used.master}")
            window.toggle_count_mode()
            pump(window.root, 0.3)
            check("点一下切成「分别计时」并落库",
                  pill.cget("text") == "分别计时" and cfg.settings.count_mode == "each",
                  f"{pill.cget('text')} / {cfg.settings.count_mode}")
            window.toggle_count_mode()
            pump(window.root, 0.3)
            check("再点一下切回「合并计时」",
                  pill.cget("text") == "合并计时" and cfg.settings.count_mode == "total",
                  f"{pill.cget('text')} / {cfg.settings.count_mode}")

            # ------------------------------------------------ 3) 明细折叠条
            print("-- 3) 今日各对象明细：从统计页搬进计时卡片（默认收起）--")
            detail = window.today_detail
            check("折叠条在计时卡片里", detail.winfo_exists() == 1 and detail.winfo_ismapped() == 1)
            check("默认是收起的", detail.expanded is False)
            head = detail.header.cget("text")
            check("标题里写明对象数与合计时长",
                  "4 个对象" in head and "合计" in head, head)
            check("收起时不显示任何明细行", not detail._rows)  # noqa: SLF001
            shot(window.root, "31_main_v17_collapsed")

            detail.toggle()                     # 点开
            pump(window.root, 0.4)
            check("点一下能展开", detail.expanded is True)
            check("展开后有 4 行明细", len(detail._rows) == 4,  # noqa: SLF001
                  str(len(detail._rows)))  # noqa: SLF001
            check("箭头变成 ▾", detail.header.cget("text").startswith("▾"),
                  detail.header.cget("text"))
            shot(window.root, "32_main_v17_expanded")
            detail.toggle()                     # 收起
            pump(window.root, 0.3)
            check("再点一下收起", detail.expanded is False)

            # 统计页里不再有明细表
            check("统计页的明细表已移除（window.tree 不存在）",
                  not hasattr(window, "tree"))

            # ------------------------------------------------ 4) 对象占比环图
            print("-- 4) 统计页下半：今日对象占比环图 --")
            window.notebook.select(0)
            pump(window.root, 0.5)
            donut = window.donut_panel.donut
            items = donut.items
            check("环图读到 4 个对象", len(items) == 4, str(items))
            check("按从多到少排序",
                  [name for name, _s in items][0] == "哔哩哔哩动画", str(items[:1]))
            check("环图画出了弧（每个对象一段）", len(donut.find_all()) >= 4,
                  f"{len(donut.find_all())} 个图元")
            summary = window.donut_panel.summary_var.get()
            check("汇总句指出占比最多的对象",
                  "哔哩哔哩动画" in summary and "共 4 个对象" in summary, summary)
            shot(window.root, "33_stats_v17_donut")

            # ------------------------------------------------ 5) 通知后端搬家
            print("-- 5) 通知后端从顶栏搬到设置页 --")
            window.notebook.select(window.notebook_index_settings)
            pump(window.root, 0.5)
            page = window.settings_panel
            check("设置页有「当前通知后端」这一行", page.tree.exists("__notifier__"))
            backend = page._notifier_backend()  # noqa: SLF001
            check("能读出真实后端名", bool(backend) and backend != "未知", backend)
            page.focus_key("__notifier__")
            pump(window.root, 0.3)
            check("详情里写明后端名", backend in page.hint_var.get(), page.hint_var.get()[:40])

            finish(0)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            finish(1)

    def finish(code: int) -> None:
        if STATE["done"]:
            return
        STATE["done"] = True
        print("-" * 60)
        if FAILED:
            print(f"结果: 有失败项（通过 {PASSED} / 失败 {FAILED}）")
        else:
            print(f"结果: 全部通过（通过 {PASSED} / 失败 {FAILED}）")
        try:
            engine.stop()
            window.task_scheduler.stop()
            window._stop_tray()          # noqa: SLF001
            store.close()
            window.root.destroy()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        raise SystemExit(code)

    window.root.after(400, run)
    window.root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

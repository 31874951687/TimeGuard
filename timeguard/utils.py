"""通用小工具：时间格式化、安全 JSON 读写、限流器。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------- 监控对象匹配
def match_target_seconds(target_seconds: dict, needle: str) -> tuple[str, float] | None:
    """在"今日各监控对象时长"里找匹配项，返回 ``(名字, 秒数)``；没命中返回 ``None``。

    匹配规则（都忽略大小写、忽略首尾空格），刻意做得**可预期**：

    1. 名字与关键词完全相同 → 命中；
    2. 名字里包含关键词（``"高数"`` 命中 ``"高数 - 学习"``）→ 命中；
    3. 关键词里包含名字（用户把关键词写长了一点，例如填了
       ``"哔哩哔哩动画"`` 而监控对象叫 ``"哔哩哔哩"``）→ 命中。

    命中多个对象时**把时长加起来**：引擎每一秒只会记到一个对象上，所以这里相加
    不会重复计算，反而能把"高数"主题下的几个关键字对象合起来算总时长 ——
    这正是用户想要的（学了就是学了，别因为关键字写法不同漏掉）。名字取时长最大的
    那个，便于在打卡备注里写清是谁触发的。
    """
    key = (needle or "").strip().lower()
    if not key or not target_seconds:
        return None
    total = 0.0
    best_name = ""
    best_seconds = -1.0
    for name, seconds in target_seconds.items():
        name_l = (name or "").strip().lower()
        if not name_l or not (name_l == key or key in name_l or name_l in key):
            continue
        try:
            value = float(seconds or 0.0)
        except (TypeError, ValueError):
            continue
        total += value
        if value > best_seconds:
            best_seconds, best_name = value, name
    return (best_name, total) if best_name else None


# ---------------------------------------------------------------- 控制台编码
def use_utf8_console() -> None:
    """把标准输出/错误切到 UTF-8，避免中文在非中文 Windows 上把程序打崩。

    真实踩坑：GitHub Actions 的 Windows runner 是 en-US，stdout 走管道时
    Python 用 cp1252 编码，`print("通过 140 个…")` 直接抛 UnicodeEncodeError，
    于是**测试全过、退出码却是 1**（CI 红过一次）。
    普通用户把系统换成英文后跑 `--selftest` 也会遇到同样的事。

    ``errors="replace"`` 保证即使终端真的显示不了中文，也只是变成问号，不会中断程序。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass                       # 某些重定向场景不允许重配置，忽略即可


# ---------------------------------------------------------------- 时间相关

def today_str() -> str:
    """今天的日期字符串 ``YYYY-MM-DD``（本地时区）。"""
    return date.today().isoformat()


def now_str() -> str:
    """当前时间戳 ``YYYY-MM-DD HH:MM:SS``（用于写入数据库）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def last_n_days(n: int) -> list[str]:
    """最近 n 天（含今天）的日期字符串列表，升序。"""
    today = date.today()
    return [(today - timedelta(days=n - 1 - i)).isoformat() for i in range(n)]


def weekday_cn(date_str: str) -> str:
    """把 ``YYYY-MM-DD`` 转成 ``周一`` 这样的中文星期。"""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return ""
    return "周" + "一二三四五六日"[d.weekday()]


def fmt_duration(seconds: float, with_seconds: bool = True) -> str:
    """把秒数格式化成 ``1小时02分`` / ``05分30秒`` / ``6天22小时`` 这样的中文时长。

    超过 24 小时会切换成“天 + 小时”，避免出现 ``166小时00分`` 这种难读的数字
    （待办任务超期几天时很常见）。
    """
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        if hours:
            return f"{days}天{hours}小时"
        return f"{days}天" if not minutes else f"{days}天{minutes}分"
    if hours:
        return f"{hours}小时{minutes:02d}分" if not with_seconds else f"{hours}小时{minutes:02d}分{secs:02d}秒"
    if minutes:
        return f"{minutes}分{secs:02d}秒" if with_seconds else f"{minutes}分钟"
    return f"{secs}秒"


def fmt_duration_short(seconds: float) -> str:
    """紧凑格式，用于图表坐标轴：``1h02m`` / ``12m`` / ``40s``。"""
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


# ---------------------------------------------------------------- 文件相关

def ensure_dir(path: Path) -> Path:
    """确保目录存在并返回该目录。"""
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path, default: Any = None) -> Any:
    """读取 JSON；文件不存在或损坏时返回 default。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json_atomic(path: Path, data: Any) -> None:
    """原子写入 JSON，避免写入中断导致配置文件损坏。"""
    ensure_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class RateLimiter:
    """简易限流器：同一 key 在 interval 秒内只放行一次。"""

    def __init__(self, interval: float) -> None:
        self.interval = float(interval)
        self._last: dict[str, float] = {}

    def allow(self, key: str) -> bool:
        """是否允许本次触发（允许则刷新时间戳）。"""
        now = time.monotonic()
        last = self._last.get(key, 0.0)
        if now - last >= self.interval:
            self._last[key] = now
            return True
        return False

    def mark(self, key: str) -> None:
        """仅记录时间戳，不判断。"""
        self._last[key] = time.monotonic()

    def remaining(self, key: str) -> float:
        """距离下次允许还剩多少秒。"""
        return max(0.0, self.interval - (time.monotonic() - self._last.get(key, 0.0)))

    def reset(self, key: str | None = None) -> None:
        """清空限流记录（可用于“立即再提醒一次”）。"""
        if key is None:
            self._last.clear()
        else:
            self._last.pop(key, None)

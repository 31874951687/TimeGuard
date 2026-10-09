"""通用小工具：时间格式化、安全 JSON 读写、限流器。"""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

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

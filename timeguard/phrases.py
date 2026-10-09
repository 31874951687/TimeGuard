"""内置鼓励语库（完全离线，不依赖任何网络 API）。

* :data:`ENCOURAGEMENTS` —— 18 句中文鼓励语（``random.choice`` 抽取）；
* :func:`pick` —— 随机取一句，可通过 ``exclude`` 避免与上次重复；
* :func:`pick_many` —— 一次取多句不重复的（用于展示型弹窗）。
"""

from __future__ import annotations

import random

#: 内置鼓励语（18 句，覆盖行动 / 专注 / 心态 / 休息四个方向）
ENCOURAGEMENTS: list[str] = [
    "先做五分钟，行动会带着你走完全程。",
    "你不需要很厉害才能开始，但你需要开始才会很厉害。",
    "完成比完美更重要，先把这件事做完。",
    "今天的你，只要比昨天前进一小步就足够了。",
    "把大任务切成小块，一口一口吃下去。",
    "专注 25 分钟，然后奖励自己 5 分钟休息。",
    "现在开始，最好的时机永远是此刻。",
    "拖延的焦虑，比做事本身更累；动手就轻松了。",
    "别和自己较劲，先做起来，状态是干出来的。",
    "你已经比想象中更接近终点了，再坚持一会儿。",
    "一次只做一件事，把注意力放回当下这一件。",
    "允许自己慢，但不允许自己停。",
    "把手机放远一点，世界会安静很多。",
    "难的事情值得慢慢做，不着急。",
    "休息不是浪费时间，是为了走得更远。",
    "写下待办的那一刻，你就已经赢了一半。",
    "今天推进一步，明天就少一点负担。",
    "相信自己：你过去完成过很多难事，这次也一样。",
]


def pick(exclude: str | None = None) -> str:
    """随机抽一句鼓励语。

    :param exclude: 若抽到与它相同，则再抽一次（避免连续两次一样）
    """
    if not ENCOURAGEMENTS:
        return ""
    choice = random.choice(ENCOURAGEMENTS)
    if exclude and len(ENCOURAGEMENTS) > 1 and choice == exclude:
        choice = random.choice([item for item in ENCOURAGEMENTS if item != exclude])
    return choice


def pick_many(count: int = 1) -> list[str]:
    """随机抽 ``count`` 句不重复的鼓励语。"""
    count = max(1, min(int(count), len(ENCOURAGEMENTS)))
    return random.sample(ENCOURAGEMENTS, count)

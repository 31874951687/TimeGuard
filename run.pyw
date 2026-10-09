"""无控制台窗口启动 TimeGuard（双击本文件即可，扩展名 .pyw）。"""

from __future__ import annotations

import sys
from pathlib import Path

# 保证以源码方式双击运行时也能找到 timeguard 包
sys.path.insert(0, str(Path(__file__).resolve().parent))

from timeguard.main import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

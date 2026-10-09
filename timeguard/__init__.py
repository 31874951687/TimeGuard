"""TimeGuard —— Windows 使用时长监控 + 待办任务管理工具。

包结构::

    timeguard/
    ├── paths.py      # 统一路径（数据目录 / 资源目录）
    ├── utils.py      # 时长格式化、原子 JSON 读写、限流器
    ├── config.py     # 用户设置（JSON 持久化）
    ├── database.py   # SQLite：使用记录 + 待办任务
    ├── winapi.py     # Windows 系统 API（前台窗口 / 标题 / 进程 / DPI / 全屏）
    ├── notifier.py   # 系统右下角通知 + 使用时长强制弹窗
    ├── engine.py     # 使用时长监控与计时引擎（后台线程）
    ├── chart.py      # matplotlib 7 天使用时长柱状图（嵌入 tkinter）
    ├── autostart.py  # 开机自启（winreg 注册表）
    ├── phrases.py    # 内置离线鼓励语库（18 句）
    ├── recurrence.py # 周期性任务的时间规则（纯函数：星期解析 / 时间窗 / 下一次发生）
    ├── scheduler.py  # 周期任务调度器（算准时刻不轮询 + 同一刻合并成一批）
    ├── datepicker.py # 日期时间选择控件（tkcalendar + 降级实现）
    ├── tasks.py      # 待办面板（今日/本周/其他分组 + 打卡）/ 任务对话框（单次·每天·每周）/ 图表 / 弹窗
    ├── settings_preview.py # 窗口尺寸预设 + 实时预览控件
    ├── settings_page.py    # 数据驱动设置页（表格自绘 + 就地编辑）
    ├── ui.py         # 主界面 + 关闭方式二选一 + 系统托盘
    └── main.py       # 程序入口
"""

__version__ = "1.5.0"
__all__ = ["__version__"]

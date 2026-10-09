# 相似的开源项目（2026-10 实测数据）

写这个项目时顺手调研了一圈"别人是怎么做的"。下面所有数据都是
**直接查 GitHub API 得到的**（不是凭印象写的），统计时间：**2026-10-08**。

先说结论，按"和 TimeGuard 有多像"排序：

| 排名 | 项目 | 像在哪 | 不像在哪 |
| --- | --- | --- | --- |
| 🥇 | [ActivityWatch](https://github.com/ActivityWatch/activitywatch) | 同一件事的"标准答案"：Python、跨平台、自动记录前台窗口、本地存储、隐私优先 | **只统计不限制**：没有每日上限、没有强制弹窗、没有待办 |
| 🥈 | [ScreenTimeTracker](https://github.com/majianchuan/ScreenTimeTracker) | Windows 桌面、记录前台应用与屏幕时间、隐私优先 | C#/WPF；只统计，不做限制与提醒 |
| 🥉 | [Focusd](https://github.com/0xarchit/Focusd) | **Windows + 每日上限**，还统计浏览器标签页 | Go；没有待办/周期任务模块，也没有"置顶强制弹窗"这套交互 |
| 4 | [Focuser](https://github.com/aadeshrao123/Focuser) | 应用 + 网站**限时/拦截**（自称 Cold Turkey 的开源替代），跨平台 | Rust + 浏览器扩展，走"拦截"而不是"弹窗劝退"；没有待办 |
| 5 | [Super Productivity](https://github.com/super-productivity/super-productivity) | **待办 + 时间盒 + 时间跟踪**三合一，2.2 万星 | Electron 前端；不监控其它进程，不强制打断 |
| 6 | [stretchly](https://github.com/hovancik/stretchly) / [Workrave](https://github.com/rcaelers/workrave) / [Safe Eyes](https://github.com/slgobinath/safeeyes) | "到点强制打断你"的弹窗体验，做了十几年 | 按**固定周期**提醒休息，不看你到底用了多久、也不管是哪个游戏 |

## 一、屏幕时间统计（和本项目的"时长监控"最接近）

| 项目 | 星 | 语言 | 最后更新 | 许可 | 一句话 |
| --- | ---: | --- | --- | --- | --- |
| [ActivityWatch](https://github.com/ActivityWatch/activitywatch) | 19,126 | Python | 2026-10-08 | MPL-2.0 | 跨平台自动时间追踪的事实标准，本地存储、可扩展 watcher |
| ↳ [aw-watcher-window](https://github.com/ActivityWatch/aw-watcher-window) | 130 | Python | 2026-10-07 | MPL-2.0 | 里面那个"看前台窗口"的采集器（Windows 也能用） |
| [Watcher](https://github.com/Waishnav/Watcher) | 216 | Python | 2025-05-21 | MIT | 极简屏幕时间统计（Linux），代码量小、适合对照阅读 |
| [ScreenTimeTracker](https://github.com/majianchuan/ScreenTimeTracker) | 145 | C# | 2026-08-19 | GPL-3.0 | Windows 桌面应用与屏幕时间统计，界面现代 |
| [Hindsight](https://github.com/Tomotsugu-dev/Hindsight) | 136 | Rust | 2026-10-08 | — | 本地优先的桌面活动追踪 + 本地 AI 日报 |
| [selfspy](https://github.com/selfspy/selfspy) | 2,495 | Python | 2019-03-06 | GPL-3.0 | 老牌"记录你干的一切"（键盘/鼠标/窗口），已多年不更新，但思路值得看 |
| [iTime](https://github.com/panshunda9/iTime) | 0 | — | 2026-07-29 | 无 | 中文项目，记录前台活动（含"AI 替人工作的时间"），很新 |
| [TimeLens](https://github.com/PythonSmall-Q/TimeLens) | 7 | TypeScript | 2026-10-05 | — | Tauri 写的屏幕时间记录 + 桌面小组件 |

## 二、限制 / 拦截（和本项目的"每日上限 + 强制弹窗"最接近）

| 项目 | 星 | 语言 | 最后更新 | 许可 | 一句话 |
| --- | ---: | --- | --- | --- | --- |
| [Focusd](https://github.com/0xarchit/Focusd) | 43 | Go | 2026-08-03 | MIT | **Windows 屏幕时间 + 每日上限 + 浏览器标签页**，无云无账号 —— 本项目的最近亲 |
| [Focuser](https://github.com/aadeshrao123/Focuser) | 47 | Rust | 2026-10-06 | MIT | 应用/网站限时 + 拦截 + 番茄钟，跨平台，Cold Turkey 的开源替代 |
| [LeechBlock NG](https://github.com/proginosko/LeechBlockNG) | 1,074 | JavaScript | 2026-09-28 | — | 浏览器扩展层面的网站限时/封锁（经典之作） |
| [distract-me-not](https://github.com/AXeL-dev/distract-me-not) | 169 | JavaScript | 2025-05-30 | — | 轻量网站拦截器 |
| [Koncentro](https://github.com/kun-codes/Koncentro) | 187 | Python | 2026-06-14 | GPL-3.0 | **Python**：番茄钟 + 任务管理 + 网站拦截，三合一，和本项目的组合方式很像 |
| [Curbox](https://github.com/curbox-app/curbox-android) | 1,385 | Kotlin | 2026-10-01 | — | 安卓端的应用/网站拦截（移动端思路） |

## 三、强制休息提醒（和本项目的"到点弹窗打断你"最接近）

| 项目 | 星 | 语言 | 最后更新 | 许可 | 一句话 |
| --- | ---: | --- | --- | --- | --- |
| [stretchly](https://github.com/hovancik/stretchly) | 6,584 | JavaScript | 2026-10-08 | BSD-2 | 跨平台休息提醒，弹窗体验打磨得很细 |
| [Workrave](https://github.com/rcaelers/workrave) | 1,830 | C++ | 2026-10-08 | GPL-3.0 | 二十年老牌 RSI 防护：微休息 + 强制休息 + 活动量统计 |
| [Safe Eyes](https://github.com/slgobinath/safeeyes) | 1,765 | Python | 2026-09-29 | GPL-3.0 | Python + GTK，20-20-20 护眼提醒 |
| [breaktimer-app](https://github.com/tom-james-watson/breaktimer-app) | 1,588 | TypeScript | 2026-10-01 | — | 现代感很强的休息提醒（Tauri） |
| [OpenCareEyes](https://github.com/Odyphus/OpenCareEyes) | 103 | Python | 2026-08-07 | Apache-2.0 | 中文：Windows 护眼 + 20-20-20 提醒 + 专注模式，可免安装 |
| [StretchBreak](https://github.com/pieterdd/StretchBreak) | 20 | Rust | 2026-04-19 | — | 小体量休息提醒 |
| [Stand Up Buddy](https://github.com/CureJe/sedentary-reminder) | 99 | C# | 2026-09-22 | — | 久坐提醒（Windows/macOS） |

## 四、待办 + 周期任务（和本项目的"待办任务"模块最接近）

| 项目 | 星 | 语言 | 最后更新 | 许可 | 一句话 |
| --- | ---: | --- | --- | --- | --- |
| [Super Productivity](https://github.com/super-productivity/super-productivity) | 22,632 | TypeScript | 2026-10-07 | MIT | 待办 + 时间盒 + 时间跟踪 + Jira/GitLab 集成，功能最全 |
| [Taskwarrior](https://github.com/GothenburgBitFactory/taskwarrior) | 6,106 | C++ | 2026-10-08 | — | 命令行任务管理的标杆：`recur` 周期规则、`due`、提醒 |
| [Task Coach](https://github.com/taskcoach/taskcoach) | 40 | Python | 2026-10-08 | — | **Python**：周期任务、截止日期、提醒、子任务、计时 —— 待办模块的直接前辈 |
| [pomotroid](https://github.com/Splode/pomotroid) | 5,534 | Rust | 2026-09-08 | — | 好看的番茄钟 |
| [pomatez](https://github.com/zidoro/pomatez) | 4,909 | TypeScript | 2026-05-19 | — | 番茄钟 + 任务清单 |
| [FocusTimer](https://github.com/focustimerhq/FocusTimer) | 2,268 | Vala | 2026-09-20 | — | GNOME 上的番茄钟 |

## 五、这个项目的定位（和上面相比的差异）

调研下来，**"监控其它进程的使用时长 + 到上限强制弹窗 + 内置周期任务提醒"这三件事同时做的开源项目很少**：

* 统计类的（ActivityWatch、ScreenTimeTracker、Watcher）**只记录、不干预**；
* 限制类的（Focuser、LeechBlock、Curbox）走的是**"拦住不让你进"**，而不是"到点了弹窗劝你"；
* 休息提醒类的（stretchly、Workrave、Safe Eyes）**按固定周期**打断你，不在乎你实际用了多久；
* 待办类的（Super Productivity、Taskwarrior、Task Coach）不监控别的程序。

TimeGuard 的组合是：**"只在前台活跃时计时"+"按每日上限劝退"+"先弹窗、再上通知、可延后"+"同一套界面里带周期任务打卡"**，
再加上"全屏游戏时暂缓、退出全屏补发""锁屏/休眠自动暂停"这些 Windows 场景细节。
如果你要参考别人的实现，建议：

* 学**统计**：读 ActivityWatch 的 watcher 架构与 `aw-watcher-window`；
* 学**限制**：读 Focusd（Windows + 上限）与 Focuser（跨平台拦截）；
* 学**打断体验**：读 stretchly 与 Workrave 的弹窗/推迟/宽限设计；
* 学**周期任务**：读 Task Coach 的周期规则与提醒，或者 Taskwarrior 的 `recur` 语义。

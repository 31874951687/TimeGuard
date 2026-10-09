# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（TimeGuard）。

用法::

    pyinstaller TimeGuard.spec --noconfirm

产物：dist/TimeGuard/TimeGuard.exe（目录模式，启动快）
如需单文件 exe，把下面的 EXE(...) 与 COLLECT(...) 换成 onefile 写法，
或直接使用 build.bat 中的 --onefile 参数。
"""

import os
import sys
from pathlib import Path

# ---- 项目根目录 ----
# SPECPATH 由 PyInstaller 注入，正常情况下就是 spec 文件所在目录；
# 但实测在个别情况下（从其他 cwd 调用 / 缓存目录干扰）它可能不是项目根，
# 那样会导致整个 timeguard 包收集不到，打出一个「启动即 ModuleNotFoundError」的 exe。
# 因此这里做多路兜底并逐个校验（必须同时存在 run.pyw 与 timeguard 包才算数）。
def _find_root() -> Path:
    candidates = [Path(SPECPATH), Path(SPECPATH).parent, Path.cwd()]  # noqa: F821
    for candidate in candidates:
        try:
            if (candidate / "run.pyw").exists() and (candidate / "timeguard" / "__init__.py").exists():
                return candidate.resolve()
        except OSError:
            continue
    raise SystemExit(f"[TimeGuard.spec] 找不到项目根目录（已尝试: {[str(c) for c in candidates]}）")


ROOT = _find_root()

# 把项目根塞进 sys.path，保证 collect_submodules 能发现 timeguard 包
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

# ---- 需要一起打包的静态资源（托盘/窗口图标）----
datas = []
resources = ROOT / "resources"
if resources.is_dir():
    datas.append((str(resources), "resources"))

# ---- 本程序包：把源码作为数据文件复制进 _internal/timeguard/ ----
# 背景（踩过的坑）：PyInstaller 的模块图在本机解析不到这个包 ——
#   importlib.find_spec('timeguard') 能找到 ...\timeguard\timeguard\__init__.py，
#   hiddenimports 里的 timeguard.* 全部报 "not found"，
#   a.pure 里一个 timeguard 子模块都没有（同环境下换个包名就能正常收集），
# 结果打出来的 exe 双击就弹 "No module named 'timeguard.main'"。
#
# 解决办法：不再依赖分析器，把 .py 源码直接复制到 _internal/timeguard/。
# 运行时 PyInstaller 已把 _internal 放进了 sys.path（实测确认），
# 所以 import timeguard.main 必定成功。
app_sources_dir = ROOT / "timeguard"
app_files = sorted(p for p in app_sources_dir.glob("*.py"))
if len(app_files) < 5:
    raise SystemExit(f"[TimeGuard.spec] 在 {app_sources_dir} 里找不到程序源码：{app_files}")

datas.extend([(str(path), "timeguard") for path in app_files])
print(f"[TimeGuard.spec] ROOT={ROOT}", flush=True)
print(f"[TimeGuard.spec] 以数据文件形式打入 {len(app_files)} 个源码文件："
      f"{[p.name for p in app_files[:4]]} ...", flush=True)


def _app_imported_modules() -> list[str]:
    """扫描本包源码，列出它 Import 的**所有**模块，交给 PyInstaller 显式冻结。

    为什么必须这么做：`timeguard` 是以**数据文件**形式打包的，PyInstaller 没有分析
    它的源码，因此它用到的一切（连 ``sqlite3`` 这种顶层标准库模块）都不会被自动
    收集 —— 实测 exe 启动报过：
        ModuleNotFoundError: No module named 'logging.handlers'
        ModuleNotFoundError: No module named 'sqlite3'

    这里用 AST 静态扫描本包的 import 语句，把结果全部加进 hiddenimports。
    这样做是安全的：这些模块本来就要用；相对导入（level>0）与本包自身会被跳过，
    避免又把 timeguard 交给那个不可靠的分析器去解析。
    """
    import ast
    import importlib.util

    imported: set[str] = set()
    for path in app_files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError) as exc:
            print(f"[TimeGuard.spec] 解析 {path.name} 失败（跳过）：{exc}", flush=True)
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                # level > 0 是相对导入（from .xxx），不需要外部模块
                if node.level == 0 and node.module:
                    imported.add(node.module)

    needed = []
    skipped: list[str] = []
    for name in sorted(imported):
        top = name.split(".")[0]
        if top in ("timeguard", "__future__"):        # 本包自身 / 编译期指令，跳过
            continue
        # 只声明"当前环境真的装了"的模块：本包有若干 try/except 的可选依赖
        # （如 win10toast），若硬写进 hiddenimports 会让打包直接失败
        try:
            importlib.util.find_spec(name)
        except (ImportError, ValueError, ModuleNotFoundError):
            skipped.append(name)
            continue
        needed.append(name)
    if skipped:
        print(f"[TimeGuard.spec] 跳过未安装的可选模块：{skipped}", flush=True)
    return needed


_app_modules = _app_imported_modules()
print(f"[TimeGuard.spec] 需显式冻结的模块（{len(_app_modules)}）：{_app_modules}", flush=True)

# ---- 隐式依赖：动态导入 / 冻结后不自动可用的模块，全部显式声明 ----
hiddenimports = [
    *_app_modules,
    "plyer.platforms.win.notification",
    "plyer.platforms.win.libs.balloontip",
    "pystray._win32",
    "win32gui",
    "win32api",
    "win32con",
    "win32process",
    "psutil",
    "PIL.ImageDraw",
    "PIL.ImageFont",
    "matplotlib.backends.backend_tkagg",
    # 待办任务的日历式日期选择器（缺失时程序会降级，但打包时最好带上）
    "tkcalendar",
    "tkcalendar.dateentry",
    "tkcalendar.calendar_",
    "babel",
    "babel.dates",
    "babel.numbers",
]

# ---- 排除体积大又用不到的东西，减小打包体积 ----
excludes = [
    "PyQt5", "PyQt6", "PySide2", "PySide6",
    "notebook", "jupyter", "IPython", "pytest",
    "scipy", "pandas", "python_docx", "pptx", "openpyxl",
    "matplotlib.tests", "matplotlib.backends.backend_webagg",
]

a = Analysis(  # noqa: F821
    [str(ROOT / "run.pyw")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)  # noqa: F821

# ---- 打包前兜底检查 ----
# 注意：这里刻意【不】断言 a.pure 里有 timeguard —— 本机 PyInstaller 的模块图就是找不到
# 这个包（见上文说明），我们改用 datas 把源码复制进 _internal/timeguard/。
# 所以只校验 datas 里确实带上了源码，避免再次静默产出坏包。
_packed_sources = [src for src, dest in datas if dest == "timeguard" and str(src).endswith(".py")]
print(f"[TimeGuard.spec] datas 中的 timeguard 源文件：{len(_packed_sources)} 个", flush=True)
if len(_packed_sources) < 5:
    raise SystemExit(f"[TimeGuard.spec] 打包中止：timeguard 源码没进 datas（{_packed_sources}）")

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TimeGuard",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 不显示黑色控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(resources / "icon.ico") if (resources / "icon.ico").exists() else None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="TimeGuard",
)

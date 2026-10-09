# ============================================================
#  TimeGuard 安装辅助脚本（由 installer.bat 调用）
#
#  为什么用 PowerShell：cmd 批处理无法可靠地写出中文文件名 / 中文目录名
#  （受控制台代码页限制），PowerShell 全程使用 Unicode，最稳妥。
#
#  本脚本只做两件事：
#    1. 把程序文件复制到「桌面\TimeGuard 时间管家」
#    2. 创建 桌面 / 开始菜单 / 开机启动 三个快捷方式
#
#  参数：
#    -Source      解压出来的程序文件所在目录（含 TimeGuard.exe）
#    -Desktop     桌面目录（默认自动获取）
#    -NoShortcuts 只复制文件，不建快捷方式
# ============================================================
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Source,
    [string]$Desktop = [Environment]::GetFolderPath('Desktop'),
    [switch]$NoShortcuts
)

$ErrorActionPreference = 'Stop'
$AppFolderName = 'TimeGuard 时间管家'
$LnkName = 'TimeGuard 时间管家.lnk'

function Write-Step { param([string]$Text) Write-Host "  $Text" }

# ---------------------------------------------------------------- 复制程序
if (-not (Test-Path (Join-Path $Source 'TimeGuard.exe'))) {
    throw "程序文件缺失：$Source\TimeGuard.exe"
}

$dest = Join-Path $Desktop $AppFolderName
Write-Step "安装位置 : $dest"
if (-not (Test-Path $dest)) { New-Item -ItemType Directory -Path $dest -Force | Out-Null }
Copy-Item (Join-Path $Source '*') $dest -Recurse -Force
$exe = Join-Path $dest 'TimeGuard.exe'
if (-not (Test-Path $exe)) { throw "复制后仍未找到 $exe" }
Write-Step "程序文件 : 已复制 ($([math]::Round(((Get-ChildItem $dest -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)) MB)"

# 载荷里的说明文件是 ASCII 名，安装后改成中文名（中文路径只有 PowerShell 处理才安全）
$readmePacked = Join-Path $dest 'README-CHS.txt'
if (Test-Path $readmePacked) {
    try {
        Move-Item $readmePacked (Join-Path $dest '使用说明.txt') -Force
        Write-Step '使用说明 : 使用说明.txt'
    } catch {
        Write-Step "使用说明 : 保留 README-CHS.txt（$($_.Exception.Message)）"
    }
}

# 标记文件（ASCII 编码）：批处理只拿它当“安装是否成功”的标志来启动程序。
# 路径里的中文写进 ASCII 文件会变问号，但只影响显示，不影响启动判断。
[IO.File]::WriteAllText((Join-Path $env:TEMP 'TimeGuard_install_path.txt'), "$exe", [Text.Encoding]::ASCII)

if ($NoShortcuts) {
    Write-Step '快捷方式 : 已跳过（-NoShortcuts）'
    return
}

# ---------------------------------------------------------------- 创建快捷方式
function New-Lnk {
    param([string]$LinkPath, [string]$Description)
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($LinkPath)
    $lnk.TargetPath = $exe
    $lnk.WorkingDirectory = $dest
    $lnk.IconLocation = "$exe,0"
    $lnk.Description = $Description
    $lnk.Save()
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($shell)
}

New-Lnk -LinkPath (Join-Path $Desktop $LnkName) -Description 'TimeGuard 时间管家 - 游戏/网页使用时长监控与强制提醒'
Write-Step "桌面     : $LnkName"

$programs = [Environment]::GetFolderPath('Programs')
if ($programs) {
    New-Lnk -LinkPath (Join-Path $programs 'TimeGuard.lnk') -Description 'TimeGuard 时间管家'
    Write-Step '开始菜单 : TimeGuard.lnk'
}

$startup = [Environment]::GetFolderPath('Startup')
if ($startup -and (Test-Path $startup)) {
    New-Lnk -LinkPath (Join-Path $startup 'TimeGuard.lnk') -Description 'TimeGuard 开机自启'
    Write-Step '开机启动 : 已启用（不想要就删除「启动」文件夹里的 TimeGuard 快捷方式）'
}

Write-Step '完成'

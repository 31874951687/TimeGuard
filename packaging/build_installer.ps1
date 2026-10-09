# ============================================================
#  TimeGuard 安装包构建脚本（7-Zip 自解压方案，已在 Windows 10 + 7-Zip 26 实测通过）
#
#  产物：
#    dist\TimeGuard-Setup.exe      单文件自解压安装程序（双击 -> 解压 -> 运行安装脚本）
#    dist\TimeGuard_portable.zip   便携版压缩包（解压即用）
#    桌面\TimeGuard 时间管家.lnk    快捷方式
#    桌面\TimeGuard-Setup.exe      安装包副本
#    桌面\TimeGuard 时间管家\       已解压可直接运行的绿色版目录
#
#  用法：powershell -NoProfile -ExecutionPolicy Bypass -File packaging\build_installer.ps1
#        参数 -DesktopOnly  只重建桌面快捷方式 / 绿色版目录，不重新打包
# ============================================================
[CmdletBinding()]
param(
    [switch]$DesktopOnly,
    [string]$Desktop = [Environment]::GetFolderPath('Desktop')
)

$ErrorActionPreference = 'Stop'
$root      = Split-Path -Parent $PSScriptRoot           # 项目根目录
$dist      = Join-Path $root 'dist'
$appDir    = Join-Path $dist 'TimeGuard'
$appExe    = Join-Path $appDir 'TimeGuard.exe'
$packDir   = Join-Path $root 'build\pack'
$setupName = 'TimeGuard-Setup.exe'
$portableZipName = 'TimeGuard_portable.zip'
$appFolderName = 'TimeGuard 时间管家'
$lnkName = 'TimeGuard 时间管家.lnk'
$readmeName = '使用说明.txt'
$readmePackedName = 'README-CHS.txt'   # 安装包内用 ASCII 文件名，纯 ASCII 的 installer.bat 才敢直接引用

$sevenZip = @(
    "$env:ProgramFiles\7-Zip\7z.exe",
    "${env:ProgramFiles(x86)}\7-Zip\7z.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

function New-Shortcut {
    param(
        [Parameter(Mandatory)][string]$LinkPath,
        [Parameter(Mandatory)][string]$TargetPath,
        [string]$WorkingDirectory = (Split-Path -Parent $TargetPath),
        [string]$IconPath = $TargetPath,
        [string]$Description = ''
    )
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($LinkPath)
    $lnk.TargetPath = $TargetPath
    $lnk.WorkingDirectory = $WorkingDirectory
    $lnk.IconLocation = "$IconPath,0"
    if ($Description) { $lnk.Description = $Description }
    $lnk.Save()
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($shell)
}

Write-Host "项目目录 : $root"
Write-Host "桌面目录 : $Desktop"
Write-Host "7-Zip    : $(if ($sevenZip) { $sevenZip } else { '未找到' })"
Write-Host ''

if (-not (Test-Path $appExe)) {
    throw "找不到 $appExe。请先打包：pyinstaller TimeGuard.spec --noconfirm"
}
$hasSevenZip = [bool]$sevenZip

# ---------------------------------------------------------------- 0. 打包产物必须真能跑
# 为什么要这一步：曾经因为 run.pyw 的一行注释把 sys.path.insert 吞掉，
# PyInstaller 打出一个缺包的 exe —— 构建全部"成功"，双击却弹
# "No module named 'timeguard.main'"。所以部署前必须先 smoke 一下。
Write-Host '[0/4] 验证已打包的 exe 能否正常运行（--selftest）...'
$probeDir = Join-Path $env:TEMP ("TimeGuard_probe_" + [guid]::NewGuid().ToString('N').Substring(0, 6))
New-Item -ItemType Directory -Path $probeDir -Force | Out-Null
# 用临时环境变量把数据目录指到探针目录（PowerShell 5.1 没有 Start-Process -Environment）
$savedDataDir = $env:TIMEGUARD_DATA_DIR
$env:TIMEGUARD_DATA_DIR = $probeDir
$probe = $null
$finished = $false
try {
    $probe = Start-Process -FilePath $appExe -ArgumentList '--selftest' -PassThru
    $finished = $probe.WaitForExit(90000)
} finally {
    if ($savedDataDir) { $env:TIMEGUARD_DATA_DIR = $savedDataDir }
    else { Remove-Item Env:TIMEGUARD_DATA_DIR -ErrorAction SilentlyContinue }
}
if (-not $finished) {
    try { $probe.Kill() } catch { }
    throw "打包产物无法正常退出（--selftest 超时）——exe 可能是坏的，已中止部署"
}
if ($probe.ExitCode -ne 0) {
    $logPath = Join-Path $probeDir 'timeguard.log'
    $tail = if (Test-Path $logPath) { (Get-Content $logPath -Encoding UTF8 -Tail 8) -join "`n" } else { '（没有产生日志）' }
    throw "打包产物自检失败（退出码 $($probe.ExitCode)）。日志尾部：`n$tail"
}
Write-Host "      [OK] exe 自检通过（退出码 0），数据目录：$probeDir"
Remove-Item $probeDir -Recurse -Force -ErrorAction SilentlyContinue

# ---------------------------------------------------------------- 1. 桌面绿色版目录 + 快捷方式
Write-Host '[1/4] 部署桌面绿色版目录 + 快捷方式...'
$portableDir = Join-Path $Desktop $appFolderName
if (Test-Path $portableDir) { Remove-Item -Recurse -Force $portableDir }
New-Item -ItemType Directory -Path $portableDir -Force | Out-Null
Copy-Item (Join-Path $appDir '*') $portableDir -Recurse -Force
Write-Host "      已复制程序文件到：$portableDir"

$lnkPath = Join-Path $Desktop $lnkName
New-Shortcut -LinkPath $lnkPath -TargetPath (Join-Path $portableDir 'TimeGuard.exe') `
    -WorkingDirectory $portableDir -IconPath (Join-Path $portableDir 'TimeGuard.exe') `
    -Description 'TimeGuard 时间管家 - 游戏/网页使用时长监控与强制提醒'
Write-Host "      桌面快捷方式：$lnkPath"

if ($DesktopOnly) {
    Write-Host ''
    Write-Host '仅重建桌面内容完成。'
    return
}

# ---------------------------------------------------------------- 2. 组装自解压载荷
Write-Host ''
Write-Host '[2/4] 组装安装包载荷...'
if (Test-Path $packDir) { Remove-Item -Recurse -Force $packDir }
New-Item -ItemType Directory -Path $packDir -Force | Out-Null
Copy-Item (Join-Path $appDir '*') $packDir -Recurse -Force
Write-Host '      载荷内容：TimeGuard.exe + _internal + installer.bat + create_shortcuts.ps1 + README-CHS.txt'

# installer.bat 必须是 ASCII + CRLF：cmd.exe 按控制台代码页解析批处理文件，
# 带中文的 UTF-8 批处理会被读成乱码命令（已在实机复现该故障）；
# 中文目录名 / 快捷方式名的工作全部交给 create_shortcuts.ps1（Unicode 安全）。
$installerSrc = Join-Path $PSScriptRoot 'installer.bat'
$installerText = [IO.File]::ReadAllText($installerSrc, [Text.Encoding]::UTF8)
$installerText = $installerText -replace "`r`n", "`n" -replace "`n", "`r`n"
$badChars = @($installerText.ToCharArray() | Where-Object { [int]$_ -gt 127 })
if ($badChars.Count -gt 0) { throw "installer.bat 含有 $($badChars.Count) 个非 ASCII 字符，会导致 cmd 解析失败" }
[IO.File]::WriteAllText((Join-Path $packDir 'installer.bat'), $installerText, [Text.Encoding]::ASCII)

# 中文说明文件（UTF-8，记事本 / 写字板都能正确显示）
# 安装包内用 ASCII 文件名，installer.bat 复制到安装目录后再随程序一起保留
$readmeSrc = Join-Path $PSScriptRoot 'readme-chs.txt'
if (Test-Path $readmeSrc) {
    Copy-Item $readmeSrc (Join-Path $packDir $readmePackedName) -Force
    Copy-Item $readmeSrc (Join-Path $portableDir $readmeName) -Force
} else {
    Write-Host '      [提示] 未找到 packaging\readme-chs.txt，安装包内将不含中文说明'
}

# 安装辅助脚本（UTF-8 with BOM，PowerShell 需要 BOM 才能正确读中文）
$helperSrc = Join-Path $PSScriptRoot 'create_shortcuts.ps1'
if (-not (Test-Path $helperSrc)) { throw "缺少安装辅助脚本：$helperSrc" }
$helperText = [IO.File]::ReadAllText($helperSrc, [Text.Encoding]::UTF8)
$helperText = $helperText -replace "`r`n", "`n" -replace "`n", "`r`n"
[IO.File]::WriteAllText((Join-Path $packDir 'create_shortcuts.ps1'), $helperText, (New-Object Text.UTF8Encoding($true)))

# ---------------------------------------------------------------- 3. 生成单文件自解压安装程序
Write-Host ''
Write-Host '[3/4] 生成单文件自解压安装程序...'
$setupPath = $null
if (-not $hasSevenZip) {
    Write-Host '      [跳过] 未安装 7-Zip，无法生成自解压 exe（便携版 zip 仍会生成）'
} else {
    $sfxModule = Join-Path (Split-Path -Parent $sevenZip) '7z.sfx'
    if (-not (Test-Path $sfxModule)) { throw "找不到 7-Zip 的自解压模块：$sfxModule" }

    $payload = Join-Path $root 'build\payload.7z'
    if (Test-Path $payload) { Remove-Item -Force $payload }
    Push-Location $packDir
    try {
        & $sevenZip a -t7z -mx=5 $payload '*' | Out-Null
    } finally {
        Pop-Location
    }
    Write-Host "      payload.7z  $([math]::Round((Get-Item $payload).Length / 1MB, 1)) MB"

    # 自解压配置：中文标题 + 解压确认 + 解压后自动运行安装脚本
    $sfxConfig = Join-Path $root 'build\sfx_config.txt'
    $configText = @(
        ';!@Install@!UTF-8!',
        'Title="TimeGuard 时间管家 安装程序"',
        'BeginPrompt="即将安装 TimeGuard 时间管家。\n程序会解压到桌面并创建快捷方式，是否继续？"',
        'Progress="yes"',
        'RunProgram="installer.bat"',
        ';!@InstallEnd@!'
    ) -join "`r`n"
    [IO.File]::WriteAllText($sfxConfig, $configText, (New-Object Text.UTF8Encoding($false)))

    $setupPath = Join-Path $dist $setupName
    if (Test-Path $setupPath) { Remove-Item -Force $setupPath }

    # 自解压 exe = 7z.sfx + 配置 + 7z 数据（二进制拼接，实测可用）
    $out = [IO.File]::Create($setupPath)
    try {
        foreach ($part in @($sfxModule, $sfxConfig, $payload)) {
            $bytes = [IO.File]::ReadAllBytes($part)
            $out.Write($bytes, 0, $bytes.Length)
        }
    } finally {
        $out.Close()
    }
    Write-Host "      [OK] $setupPath  $([math]::Round((Get-Item $setupPath).Length / 1MB, 1)) MB"
}

# ---------------------------------------------------------------- 4. 便携版 zip + 复制到桌面
Write-Host ''
Write-Host '[4/4] 生成便携版 zip 并复制到桌面...'
$portableZip = Join-Path $dist $portableZipName
if (Test-Path $portableZip) { Remove-Item -Force $portableZip }
Push-Location $appDir
try {
    if ($hasSevenZip) {
        & $sevenZip a -tzip -mx=5 $portableZip '*' | Out-Null
    } else {
        Compress-Archive -Path '*' -DestinationPath $portableZip -Force
    }
} finally {
    Pop-Location
}
Write-Host "      [OK] $portableZip  $([math]::Round((Get-Item $portableZip).Length / 1MB, 1)) MB"

foreach ($item in @($setupPath, $portableZip)) {
    if ($item -and (Test-Path $item)) {
        $target = Join-Path $Desktop (Split-Path -Leaf $item)
        Copy-Item $item $target -Force
        Write-Host "      [OK] 已复制到桌面：$target"
    }
}

Write-Host ''
Write-Host '================= 完成 ================='
Write-Host "桌面快捷方式 : $lnkPath"
Write-Host "桌面绿色版   : $portableDir"
if ($setupPath) { Write-Host "桌面安装包   : $(Join-Path $Desktop $setupName)" }
Write-Host "桌面便携 zip : $(Join-Path $Desktop $portableZipName)"
Write-Host '========================================'

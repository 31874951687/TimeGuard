<#
清理 Windows 通知中心里由本程序留下的通知。

为什么要做这件事：开发与回归脚本（改造前）会真的调用系统通知，通知中心里就攒下
一堆气泡，看着像程序在乱弹。现在测试默认走 TIMEGUARD_NO_TOAST=1（只记录不真发），
这个脚本负责把**历史遗留**的那批也尽量清掉。

已知限制（实测）：plyer 在 Windows 上走的是 Shell_NotifyIcon 的**旧式气泡**，
它们不在 WinRT 的 Toast 历史里 —— ToastNotificationManager.History.Clear(aumid)
对它们无效（试过 13 个候选 AUMID：Python / python.exe / exe 完整路径 / 显示名 …，
通知中心里那几条依旧在）。所以：

* 属于 WinRT toast 的通知，这个脚本能清掉；
* 属于旧式气泡的，只能用通知中心自己的「清除所有通知」按钮（或等它过期）。
  真要从根上不再产生，靠的是测试模式（见 README「测试时不要把 Windows 通知中心塞满」）。

脚本最后会打开通知中心截一张图。注意**等 2.5 秒再截**：刚打开时窗口还在播入场
动画，1.2 秒截到的可能是一张没有内容的空面板 —— 会误判成"已经清干净了"（踩过）。

用法：
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\clear_toasts.ps1
#>

$ErrorActionPreference = 'Stop'

[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
$history = [Windows.UI.Notifications.ToastNotificationManager]::History

# 非打包程序的通知 AUMID 可能是 exe 路径，也可能是显示名，所以挨个试一遍
$desktopExe = Join-Path ([Environment]::GetFolderPath('Desktop')) 'TimeGuard 时间管家\TimeGuard.exe'
$candidates = @(
    'Python',
    'python.exe',
    (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python314\python.exe'),
    'TimeGuard',
    'TimeGuard.exe',
    'TimeGuard 时间管家',
    $desktopExe,
    (Join-Path $PSScriptRoot '..\dist\TimeGuard\TimeGuard.exe'),
    (Join-Path $PSScriptRoot '..\dist\TimeGuard\timeguard.exe'),
    (Join-Path ([Environment]::GetFolderPath('Desktop')) 'TimeGuard 时间管家\timeguard.exe'),
    '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\TimeGuard.exe'
) | Where-Object { $_ } | Select-Object -Unique

$cleared = 0
foreach ($id in $candidates) {
    try {
        $history.Clear($id)
        Write-Host "  已清理: $id"
        $cleared++
    } catch {
        Write-Host "  跳过 ($id): $($_.Exception.Message)"
    }
}
Write-Host "共处理 $cleared 个 AUMID（旧式气泡清不掉，属已知限制）"

# 打开通知中心截一张图，人工确认（等动画播完再截）
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
Start-Process 'ms-actioncenter:'
Start-Sleep -Milliseconds 2500
$bounds = [Windows.Forms.SystemInformation]::VirtualScreen
$bmp = New-Object Drawing.Bitmap $bounds.Width, $bounds.Height
$gfx = [Drawing.Graphics]::FromImage($bmp)
$gfx.CopyFromScreen($bounds.Left, $bounds.Top, 0, 0, $bmp.Size)
$out = Join-Path $PSScriptRoot '_shots\_action_center.png'
New-Item -ItemType Directory -Force -Path (Split-Path $out) | Out-Null
$bmp.Save($out, [Drawing.Imaging.ImageFormat]::Png)
$gfx.Dispose()
$bmp.Dispose()
[Windows.Forms.SendKeys]::SendWait('{ESC}')
Write-Host "通知中心截图: $out"



@echo off
rem ============================================================
rem  TimeGuard installer  (launched automatically by TimeGuard-Setup.exe)
rem
rem  This batch file deliberately stays pure ASCII + CRLF: cmd.exe parses
rem  batch files using the console code page, so non-ASCII text here gets
rem  mis-read on some systems (verified failure mode). All Unicode work
rem  (Chinese folder / shortcut names) is delegated to create_shortcuts.ps1.
rem ============================================================
setlocal
title TimeGuard Setup

rem %~dp0 always ends with a backslash. Strip it for passing as an argument
rem (a path ending in \" escapes the closing quote when handed to powershell.exe)
set "SRC=%~dp0"
set "SRC=%SRC:~0,-1%"
set "PS1=%SRC%\create_shortcuts.ps1"
set "MARK=%TEMP%\TimeGuard_install_path.txt"

echo.
echo  ==========================================================
echo    TimeGuard  -  Setup
echo    game / web usage monitor with forced break reminders
echo  ==========================================================
echo.

rem ---- sanity check ----
if not exist "%SRC%\TimeGuard.exe" (
    echo  [ERROR] TimeGuard.exe was not found next to this installer.
    echo          The archive may be incomplete: %SRC%
    goto :fail
)
if not exist "%PS1%" (
    echo  [ERROR] create_shortcuts.ps1 is missing from the archive.
    goto :fail
)

rem ---- 1) install files + shortcuts (PowerShell handles Unicode) ----
echo  [1/5] Installing program files and shortcuts...
if exist "%MARK%" del "%MARK%" >nul 2>nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -Source "%SRC%"
if errorlevel 1 (
    echo  [ERROR] Installation script failed. See the messages above.
    goto :fail
)

rem ---- 2) confirm the install (marker is written by the PowerShell step) ----
set "DEST="
if exist "%MARK%" for /f "usebackq delims=" %%P in ("%MARK%") do set "DEST=%%P"
if not defined DEST (
    echo  [ERROR] The install marker was not written - installation failed.
    goto :fail
)

rem ---- 3) done ----
rem NOTE: do NOT delete %SRC% here. cmd.exe keeps reading this batch file from
rem disk, so removing its own folder aborts the script mid-run (verified:
rem "The system cannot find the path specified." plus exit code 1).
rem The extraction folder lives in %TEMP% and Windows cleans it up itself.
echo  [2/5] Done.
echo.
echo  ==========================================================
echo    Installed to your Desktop folder: TimeGuard
echo    Data folder : %%APPDATA%%\TimeGuard  (settings and history)
echo.
echo    First run: open the Settings tab, replace the sample game
echo    process names with your own, then click "Save settings".
echo  ==========================================================
echo.

rem ---- 4) ask whether to launch ----
set "RUN="
set /p RUN=Launch TimeGuard now? [Y/N]:
if /i "%RUN%"=="Y" start "" "%DEST%"
if /i "%RUN%"=="YES" start "" "%DEST%"
endlocal
exit /b 0

:fail
echo.
echo  Installation did not finish. Please report the messages above.
echo.
pause
endlocal
exit /b 1

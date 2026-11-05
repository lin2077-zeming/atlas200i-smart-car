@echo off
REM ===========================================================================
REM  Start Car Console.cmd -- double-click entry point for car_console.ps1
REM
REM  Windows blocks .ps1 by default (execution policy), so double-clicking the
REM  .ps1 fails or flashes past. This wrapper uses -ExecutionPolicy Bypass for
REM  THIS CALL ONLY -- it does not change any system-wide setting.
REM
REM  What it does: find the board -> start the web console on it over SSH ->
REM  wait until it answers -> open your browser at http://<board>:8090/
REM
REM  Usage (arguments are passed through):
REM      Start Car Console.cmd
REM      Start Car Console.cmd -Stop               (stop the console on board)
REM      Start Car Console.cmd -Sweep              (scan subnet if not found)
REM      Start Car Console.cmd -IP 192.168.31.128  (use a specific IP)
REM      Start Car Console.cmd -Asr                (preload speech recognition)
REM
REM  NOTE: keep this file pure ASCII. cmd.exe reads .cmd files using the OEM
REM  codepage, so non-ASCII comments get mangled and executed as commands.
REM ===========================================================================
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0car_console.ps1" %*
set RC=%ERRORLEVEL%
echo.
if %RC%==0 (
    echo [DONE] Console is running. Browser should have opened.
) else (
    echo [FAILED] See the messages above.
)
pause
exit /b %RC%
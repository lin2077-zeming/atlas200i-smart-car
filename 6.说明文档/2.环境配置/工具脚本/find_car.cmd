@echo off
REM ===========================================================================
REM  find_car.cmd -- double-click entry point for find_car.ps1
REM
REM  Windows blocks .ps1 by default (execution policy), so double-clicking
REM  find_car.ps1 fails or flashes past. This wrapper uses -ExecutionPolicy
REM  Bypass for THIS CALL ONLY -- it does not change any system-wide setting.
REM
REM  Usage (arguments are passed through):
REM      find_car.cmd
REM      find_car.cmd -Subnet 192.168.31
REM      find_car.cmd -Sweep
REM
REM  NOTE: keep this file pure ASCII. cmd.exe reads .cmd files using the OEM
REM  codepage, so non-ASCII comments get mangled and executed as commands.
REM ===========================================================================
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0find_car.ps1" %*
set RC=%ERRORLEVEL%
echo.
if %RC%==0 (
    echo [DONE] Car found. Use the ssh command above.
) else (
    echo [NOT FOUND] See the troubleshooting tips above.
)
pause
exit /b %RC%

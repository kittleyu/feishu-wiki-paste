@echo off
cd /d "%~dp0"
rem Use base managed python.exe (VISIBLE window) instead of pythonw,
rem so any crash/error is shown directly and 0xc0000142 pythonw issue is avoided.
set "PY=C:\Users\daixiaoyu\.workbuddy\binaries\python\versions\3.13.12\python.exe"
set "GIT=C:\Users\daixiaoyu\.workbuddy\vendor\PortableGit\cmd\git.exe"

rem Kill any stale feishu_bot processes from previous runs (targeted by command line)
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' AND CommandLine LIKE '%feishu_bot%'\" | ForEach-Object { $_.Terminate() }" >nul 2>&1
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe' AND CommandLine LIKE '%feishu_bot%'\" | ForEach-Object { $_.Terminate() }" >nul 2>&1

rem Pull latest code (silent)
"%GIT%" pull origin main >nul 2>&1

if "%1"=="detached" (
  "%PY%" -u feishu_bot.py
  exit
)
rem Launch detached so this launcher window closes; bot window stays visible
start "" cmd.exe /c "%~dp0start_bot.bat detached"
exit

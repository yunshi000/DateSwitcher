@echo off
rem 以管理员身份启动 DateSwitcher（会弹出 UAC 确认框）
cd /d "%~dp0"
where pythonw >nul 2>nul && (set "PYW=pythonw.exe") || (set "PYW=python.exe")
powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath $env:PYW -ArgumentList '\"%~dp0dateswitcher.py\"'"

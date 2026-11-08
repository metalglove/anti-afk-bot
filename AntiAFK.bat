@echo off
rem Starts the Anti-AFK app as administrator, so the game accepts its input.
cd /d "%~dp0"
net session >nul 2>&1 || (powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0'" & exit /b)
start "" pythonw app.py

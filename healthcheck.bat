@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m bot.healthcheck
) else (
  docker compose exec bot python -m bot.healthcheck
)
pause

@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Subscription bot

if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo Создан файл .env. Заполните в нём BOT_TOKEN и ADMIN_IDS, сохраните и запустите run.bat снова.
  echo A .env file was created. Fill in BOT_TOKEN and ADMIN_IDS, save it and run run.bat again.
  notepad ".env"
  pause
  exit /b 1
)

set "PY=python"
py -3.12 --version >nul 2>nul && set "PY=py -3.12"
%PY% --version >nul 2>nul || goto nopython

if not exist ".venv\Scripts\python.exe" (
  echo Создаю виртуальное окружение / Creating virtual environment...
  %PY% -m venv .venv || goto nopython
)

fc /b requirements.txt ".venv\installed.txt" >nul 2>nul
if errorlevel 1 (
  echo Устанавливаю зависимости / Installing dependencies...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip -q
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt -q
  if errorlevel 1 goto pipfail
  copy /y requirements.txt ".venv\installed.txt" >nul
)

:loop
echo.
echo [%date% %time%] Запуск бота / Starting the bot...
".venv\Scripts\python.exe" -m bot.main
set "CODE=%errorlevel%"
if "%CODE%"=="2" goto badconfig
echo [%date% %time%] Бот остановился, код %CODE%. Перезапуск через 15 секунд. Ctrl+C - выход.
echo The bot stopped, code %CODE%. Restarting in 15 seconds. Press Ctrl+C to quit.
timeout /t 15 /nobreak >nul
goto loop

:badconfig
echo Ошибка в настройках .env - смотрите сообщение выше. / Fix .env - see the message above.
pause
exit /b 2

:pipfail
echo Не удалось установить зависимости. Проверьте интернет и запустите снова.
echo Could not install dependencies. Check the internet connection and try again.
pause
exit /b 1

:nopython
echo Python 3.12 не найден. Установите его с https://www.python.org/downloads/ (галочка "Add python.exe to PATH").
echo Python 3.12 was not found. Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH").
pause
exit /b 1

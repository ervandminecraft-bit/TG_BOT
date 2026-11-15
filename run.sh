#!/usr/bin/env bash
# Start the bot without Docker (macOS / Linux). Restarts it automatically if it stops.
cd "$(dirname "$0")" || exit 1

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Создан файл .env. Заполните BOT_TOKEN и ADMIN_IDS и запустите ./run.sh снова."
  echo "A .env file was created. Fill in BOT_TOKEN and ADMIN_IDS and run ./run.sh again."
  exit 1
fi

PY=""
for candidate in python3.12 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then PY="$candidate"; break; fi
done
if [ -z "$PY" ]; then
  echo "Python 3.12 не найден / not found: https://www.python.org/downloads/"
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "Создаю виртуальное окружение / Creating virtual environment..."
  "$PY" -m venv .venv || exit 1
fi

if ! cmp -s requirements.txt .venv/installed.txt; then
  echo "Устанавливаю зависимости / Installing dependencies..."
  .venv/bin/python -m pip install --upgrade pip -q
  .venv/bin/python -m pip install -r requirements.txt -q || { echo "pip install failed (internet?)"; exit 1; }
  cp requirements.txt .venv/installed.txt
fi

# On macOS keep the computer awake while the bot runs (does not help when the lid is closed).
KEEP_AWAKE=""
if [ "$(uname)" = "Darwin" ] && command -v caffeinate >/dev/null 2>&1; then
  KEEP_AWAKE="caffeinate -is"
fi

while true; do
  echo "[$(date)] Запуск бота / Starting the bot..."
  $KEEP_AWAKE .venv/bin/python -m bot.main
  code=$?
  if [ "$code" = "2" ]; then
    echo "Ошибка в .env — смотрите сообщение выше / Fix .env — see the message above."
    exit 2
  fi
  echo "[$(date)] Бот остановился (код $code). Перезапуск через 15 с. Ctrl+C — выход."
  sleep 15
done

"""Health check: python -m bot.healthcheck

Exit code 0 = the bot is alive (the scheduler wrote a heartbeat in the last 5 minutes).
Used by Docker HEALTHCHECK and by the healthcheck.bat / healthcheck.sh scripts.
"""
from __future__ import annotations

import sys
from datetime import datetime

from bot.config import settings
from bot.utils.timeutil import utcnow

MAX_AGE_SECONDS = 300


def main() -> int:
    path = settings.heartbeat_file
    if not path.exists():
        print("NOT RUNNING: no heartbeat file yet / нет файла heartbeat (бот не запускался?)")
        return 1
    try:
        last = datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
    except ValueError:
        print("UNKNOWN: heartbeat file is corrupted / файл heartbeat повреждён")
        return 1
    age = (utcnow() - last).total_seconds()
    if age > MAX_AGE_SECONDS:
        print(f"NOT RUNNING: last heartbeat {int(age // 60)} min ago / последний сигнал {int(age // 60)} мин назад")
        return 1
    print(f"OK: bot is running, last heartbeat {int(age)} s ago / бот работает, сигнал {int(age)} с назад")
    return 0


if __name__ == "__main__":
    sys.exit(main())

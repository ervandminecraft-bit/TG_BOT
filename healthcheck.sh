#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1
if [ -x .venv/bin/python ]; then
  .venv/bin/python -m bot.healthcheck
else
  docker compose exec bot python -m bot.healthcheck
fi

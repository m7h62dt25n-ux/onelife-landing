#!/bin/bash
# Ручной/cron-запуск бота-редактора. (launchd запускает Python напрямую — см. plist.)
# Бот сам подгружает .env, поэтому source здесь не обязателен.
cd "$(dirname "$0")" || exit 1
exec ./venv/bin/python editor_bot.py

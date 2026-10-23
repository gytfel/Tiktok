#!/usr/bin/env bash
# Запуск TikTok Studio на macOS и Linux: ./start.sh
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "Python 3.10+ не найден. Установите его (macOS: brew install python, Ubuntu: sudo apt install python3 python3-venv)."
  exit 1
fi

# Окружение, зависимости и запуск готовит launcher.py.
exec "$PY" launcher.py "$@"

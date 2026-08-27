#!/bin/sh
# Launch LUMEN, creating the virtualenv on first run.
set -e
cd "$(dirname "$0")"

PY="${PYTHON:-python3.12}"
if ! command -v "$PY" >/dev/null 2>&1; then
    PY=python3
fi

if [ ! -d .venv ]; then
    echo "Creating virtualenv with $PY ..."
    "$PY" -m venv .venv
    ./.venv/bin/pip install --quiet --upgrade pip
    ./.venv/bin/pip install --quiet -r requirements.txt
fi

exec ./.venv/bin/python main.py "$@"

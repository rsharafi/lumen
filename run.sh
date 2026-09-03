#!/bin/sh
# Launch LUMEN, creating the virtualenv on first run.
set -e
cd "$(dirname "$0")"

# cmu-graphics 2.x needs Python 3.11 or newer, and the Python that ships with
# macOS is 3.9 - so `python3` is the wrong answer on a fresh Mac. Take the
# first suitable interpreter on PATH, or say plainly what is missing.
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    for candidate in python3.13 python3.12 python3.11 python3; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c \
            'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)' \
            2>/dev/null; then
            PY="$candidate"
            break
        fi
    done
fi
if [ -z "$PY" ]; then
    echo "LUMEN needs Python 3.11 or newer (cmu-graphics 2.x requires it)." >&2
    echo "  macOS:  brew install python@3.12" >&2
    echo "  then:   ./run.sh          (or PYTHON=/path/to/python ./run.sh)" >&2
    exit 1
fi

if [ ! -d .venv ]; then
    echo "Creating virtualenv with $PY ..."
    "$PY" -m venv .venv
    ./.venv/bin/pip install --quiet --upgrade pip
    ./.venv/bin/pip install --quiet -r requirements.txt
fi

PASS=""
for arg in "$@"; do
    case "$arg" in
        --fullscreen) export LUMEN_FULLSCREEN=1 ;;
        --help|-h)
            cat <<'USAGE'
LUMEN

  ./run.sh                 play it
  ./run.sh --fullscreen    start covering the display

  Anything else is passed straight through to the game.

  There used to be four combinations here - two hosts crossed with three
  renderers. There is one of each now: our own loop, drawing through
  OpenGL. In game, the DISPLAY row on the title screen changes resolution
  and VISUALS switches between the lit pipeline and the original look.
USAGE
            exit 0 ;;
        *) PASS="$PASS $arg" ;;
    esac
done

exec ./.venv/bin/python native.py $PASS

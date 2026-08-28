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

# Two independent choices: which loop hosts the game, and what draws it.
# Defaults are unchanged - cmu-graphics hosting, SDL's renderer drawing.
ENTRY=main.py
PASS=""
for arg in "$@"; do
    case "$arg" in
        --native)  ENTRY=native.py ;;
        --cmu)     ENTRY=main.py ;;
        --gl)      ENTRY=native.py; export LUMEN_RENDERER=gl ;;
        --sdl)     export LUMEN_RENDERER=gpu ;;
        --cpu)     ENTRY=main.py; export LUMEN_RENDERER=cpu ;;
        --help|-h)
            cat <<'USAGE'
LUMEN

  ./run.sh                 cmu-graphics hosting it, SDL drawing  (default)
  ./run.sh --native        our own loop, SDL drawing
  ./run.sh --gl            our own loop, OpenGL drawing (shaders, HDR)
  ./run.sh --cpu           cmu-graphics hosting *and* drawing (the original)

  --cmu / --native pick the host, --cpu / --sdl / --gl pick the renderer.
  Anything else is passed through. The same choices are available directly:

      LUMEN_RENDERER=gl python native.py

  In game, the DISPLAY row on the title screen changes resolution and
  VISUALS switches between the lit pipeline and the original look. Those
  are settings; the host and renderer are chosen at launch, because the
  renderer binds at import.
USAGE
            exit 0 ;;
        *) PASS="$PASS $arg" ;;
    esac
done

echo "LUMEN: $ENTRY, renderer=${LUMEN_RENDERER:-gpu}" >&2
exec ./.venv/bin/python "$ENTRY" $PASS

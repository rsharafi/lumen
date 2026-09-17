#!/bin/sh
# Build the Windows .exe without a Windows machine.
#
#     packaging/build_windows.sh
#
# PyInstaller cannot cross-compile: the executable it writes is made out of
# the Python that runs it, so a Windows build needs Windows Python. This runs
# one under Wine, in a container, which is the nearest thing to a Windows
# machine a Mac can hold - and what comes out is an ordinary native .exe.
#
# Docker has to be running, and on an Apple silicon Mac it has to have Rosetta
# turned on for x86 images; the alternative emulator cannot run Wine at all.
# The first build pulls a few hundred megabytes of image and wheels and takes
# some minutes; later ones reuse both.
#
# The build happens on a copy inside the container rather than in the mounted
# repository. Wine and a bind mount disagree about creating directories partway
# through a path, and the copy is faster besides.
set -e
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
IMAGE=tobix/pywine:3.12

mkdir -p dist
docker run --rm --platform linux/amd64 -v "$ROOT":/work -w /work "$IMAGE" sh -c '
set -e
export WINEDEBUG=-all
mkdir -p /src
tar -C /work --exclude=./.venv --exclude=./.git --exclude=./build \
    --exclude=./dist --exclude=./.sound_cache --exclude=./docs \
    --exclude=__pycache__ -cf - . | tar -C /src -xf -
cd /src
# numpy is pinned for the build, and only because of Wine: numpy 2.3 calls
# `crealf` in the C runtime, which Wine did not implement until 10.1, and the
# image that runs at all on Apple silicon is on 9.17. The pinned version is
# the one that ends up inside the .exe; on a real Windows machine either runs
# the game the same.
wine python -m pip install --quiet --disable-pip-version-check \
    "numpy==2.2.1" -r requirements.txt pyinstaller
wine python packaging/build.py
cp /src/dist/LUMEN.exe /work/dist/LUMEN.exe
'
echo
ls -lh dist/LUMEN.exe

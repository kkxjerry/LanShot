#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -P "$(dirname "$0")" && pwd)
cd "$SCRIPT_DIR"

if command -v python3 >/dev/null 2>&1 && python3 -c 'import tkinter' >/dev/null 2>&1; then
    PYTHON=python3
elif [ -x /usr/bin/python3 ] && /usr/bin/python3 -c 'import tkinter' >/dev/null 2>&1; then
    PYTHON=/usr/bin/python3
else
    echo "No Tk-enabled Python 3 was found. Use diagnostics.py status from the command line."
    exit 1
fi

exec "$PYTHON" diagnostics.py "$@" gui

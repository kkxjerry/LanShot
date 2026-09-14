#!/bin/zsh
set -euo pipefail
SCRIPT_DIR="${0:A:h}"
source "$SCRIPT_DIR/runtime_python.zsh"
PYTHON="$(lanshot_python)"
"$PYTHON" "$SCRIPT_DIR/mode_controller.py" stop

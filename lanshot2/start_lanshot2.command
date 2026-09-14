#!/bin/zsh
set -euo pipefail
SCRIPT_DIR="${0:A:h}"
source "$SCRIPT_DIR/../unified/runtime_python.zsh"
PYTHON="$(lanshot_python)"
exec "$PYTHON" "$SCRIPT_DIR/../unified/mode_controller.py" voice

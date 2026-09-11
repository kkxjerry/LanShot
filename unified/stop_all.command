#!/bin/zsh
set -euo pipefail
SCRIPT_DIR="${0:A:h}"
python3 "$SCRIPT_DIR/mode_controller.py" stop

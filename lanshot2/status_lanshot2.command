#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
PROJECT_DIR="${SCRIPT_DIR:h}"
SETTINGS="${LANSHOT_SETTINGS:-$HOME/Library/Application Support/LanShotP1R2Live/settings.json}"

"$SCRIPT_DIR/audio_service.py" status
if [[ -f "$SETTINGS" ]]; then
  python3 "$PROJECT_DIR/screenshot-sender/diagnostics.py" --settings "$SETTINGS" status
fi

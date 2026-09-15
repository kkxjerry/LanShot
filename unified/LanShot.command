#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
source "$SCRIPT_DIR/runtime_python.zsh"
PYTHON="$(lanshot_python)"
choice=$(/usr/bin/osascript <<'APPLESCRIPT'
tell application "System Events"
    activate
    set selectedMode to button returned of (display dialog "选择 LanShot 运行模式" with title "LanShot" buttons {"全部停止", "面试模式", "截屏模式"} default button "截屏模式")
    return selectedMode
end tell
APPLESCRIPT
) || exit 0

case "$choice" in
  "截屏模式") mode="screenshot" ;;
  "面试模式") mode="voice" ;;
  *) mode="stop" ;;
esac

"$PYTHON" "$SCRIPT_DIR/mode_controller.py" "$mode"
echo
read -k 1 "?按任意键关闭窗口..."
echo

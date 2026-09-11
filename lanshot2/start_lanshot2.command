#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
PROJECT_DIR="${SCRIPT_DIR:h}"
SETTINGS="${LANSHOT_SETTINGS:-$HOME/Library/Application Support/LanShotP1R2Live/settings.json}"

if [[ ! -f "$SETTINGS" ]]; then
  echo "找不到 LanShot 设置：$SETTINGS"
  exit 1
fi
if [[ ! -x "$SCRIPT_DIR/LanShot2AudioCapture.app/Contents/MacOS/native_audio_capture" ]]; then
  "$SCRIPT_DIR/build_audio.command"
fi

set +e
python3 "$PROJECT_DIR/screenshot-sender/manage_services.py" start \
  --settings "$SETTINGS" \
  --expected-profile default \
  --open-display \
  --reset-budget
manager_status=$?
set -e
if (( manager_status == 1 )); then
  echo "LanShot 截图服务启动失败，LanShot2 未启动音频。"
  exit 1
fi

if ! "$SCRIPT_DIR/audio_service.py" start; then
  echo "双路音频启动失败，正在回滚已经启动的 LanShot 服务。"
  python3 "$PROJECT_DIR/screenshot-sender/manage_services.py" stop --settings "$SETTINGS" || true
  exit 1
fi
echo "LanShot2 已启动：截图能力保持原样，系统音频与麦克风正在分别实时转写。"

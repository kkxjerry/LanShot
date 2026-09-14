#!/bin/zsh
set -euo pipefail

ROOT="${0:A:h}"
CONFIG_DIR="$HOME/Library/Application Support/LanShot"

pause_on_error() {
  local code=$?
  print
  read -k 1 "?安装失败，按任意键关闭窗口..."
  print
  exit "$code"
}
trap pause_on_error ZERR

major_version="$(/usr/bin/sw_vers -productVersion | /usr/bin/cut -d. -f1)"
if (( major_version < 13 )); then
  print -u2 -- "LanShot 需要 macOS 13 或更高版本。"
  false
fi

if ! /usr/bin/xcrun --find swiftc >/dev/null 2>&1; then
  print -u2 -- "缺少 Xcode Command Line Tools。请先运行 xcode-select --install。"
  false
fi

source "$ROOT/unified/runtime_python.zsh"
PYTHON="$(lanshot_python)"
/bin/mkdir -p "$CONFIG_DIR"
print -r -- "$PYTHON" > "$CONFIG_DIR/python_path"
/bin/chmod 600 "$CONFIG_DIR/python_path"

if ! /usr/bin/security find-generic-password \
  -s com.lanshot.bailian -a DASHSCOPE_API_KEY -w >/dev/null 2>&1; then
  print -n -- "请输入阿里云百炼 API Key："
  read -s api_key
  print
  if [[ -z "$api_key" ]]; then
    print -u2 -- "API Key 不能为空。"
    false
  fi
  /usr/bin/security add-generic-password -U \
    -s com.lanshot.bailian -a DASHSCOPE_API_KEY -w "$api_key" >/dev/null
  unset api_key
fi

print -- "正在编译悬浮窗..."
/bin/sh "$ROOT/capture-exclusion-demo/build.sh"

identity="$(/usr/bin/security find-identity -v -p codesigning 2>/dev/null \
  | /usr/bin/awk '/"Apple Development:/ { print $2; exit }')"
if [[ -n "$identity" ]]; then
  print -- "正在使用本机 Apple Development 证书编译音频程序..."
  LANSHOT_CODESIGN_IDENTITY="$identity" "$ROOT/lanshot2/build_audio.command"
else
  print -- "本机没有 Apple Development 证书，将使用一次性本地签名。"
  print -- "以后重新编译音频程序时，macOS 可能要求重新授权录屏。"
  LANSHOT_CODESIGN_IDENTITY="-" "$ROOT/lanshot2/build_audio.command"
fi

/bin/chmod +x "$ROOT"/*.command "$ROOT"/unified/*.command "$ROOT"/lanshot2/*.command

print -- "正在启动语音模式到待机状态，不会录音..."
"$PYTHON" "$ROOT/unified/mode_controller.py" voice

print
print -- "安装完成。"
print -- "1. 菜单栏出现 LanShot 麦克风图标后，点击“开始采集”。"
print -- "2. 首次使用时，允许 LanShot Voice Capture 的麦克风、录屏与系统录音权限。"
print -- "3. 若 F23 不可用，在“隐私与安全性 -> 输入监控”中允许当前 Python 或终端。"
print -- "4. 日常启动请双击 unified/LanShot.command 或 unified/voice_mode.command。"
print
read -k 1 "?按任意键关闭窗口..."
print

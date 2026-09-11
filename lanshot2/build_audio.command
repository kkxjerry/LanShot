#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
APP="$SCRIPT_DIR/LanShot2AudioCapture.app"
EXECUTABLE="$APP/Contents/MacOS/native_audio_capture"
IDENTITY="${LANSHOT_CODESIGN_IDENTITY:-$(security find-identity -v -p codesigning 2>/dev/null | awk '/"Apple Development:/ { print $2; exit }')}"

if [[ -z "$IDENTITY" ]]; then
  echo "缺少 Apple Development 代码签名证书，拒绝生成会反复丢失TCC权限的临时签名。" >&2
  exit 1
fi

mkdir -p "$APP/Contents/MacOS"
cp "$SCRIPT_DIR/Info.plist" "$APP/Contents/Info.plist"

xcrun swiftc \
  -parse-as-library \
  "$SCRIPT_DIR/native_audio_capture.swift" \
  -o "$EXECUTABLE" \
  -framework AVFoundation \
  -framework CoreMedia \
  -framework Foundation \
  -framework ScreenCaptureKit \
  -framework Security

codesign --force --sign "$IDENTITY" --timestamp=none \
  --entitlements "$SCRIPT_DIR/audio.entitlements" \
  "$APP"
echo "$EXECUTABLE"

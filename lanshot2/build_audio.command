#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
APP="$SCRIPT_DIR/LanShot2AudioCapture.app"
EXECUTABLE="$APP/Contents/MacOS/native_audio_capture"

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
  -framework Security \
  -framework Speech

codesign --force --sign - \
  --requirements '=designated => identifier "com.lanshot2.audio-capture"' \
  --entitlements "$SCRIPT_DIR/audio.entitlements" \
  "$APP"
echo "$EXECUTABLE"

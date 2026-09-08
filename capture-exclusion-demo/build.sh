#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BUILD_DIR="$SCRIPT_DIR/build"
mkdir -p "$BUILD_DIR"
TEMP=$(mktemp -d "$BUILD_DIR/.native-build.XXXXXX")
trap 'rm -rf "$TEMP"' EXIT HUP INT TERM
APP="$TEMP/CaptureExclusionDemo.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
xcrun swiftc -parse-as-library -O -framework AppKit "$SCRIPT_DIR/CaptureExclusionDemo.swift" -o "$APP/Contents/MacOS/CaptureExclusionDemo"
cp "$SCRIPT_DIR/Info.plist" "$APP/Contents/Info.plist"
codesign --force --sign - "$APP"
FINAL="$BUILD_DIR/CaptureExclusionDemo.app"
if [ -e "$FINAL" ]; then
    BACKUP="$FINAL.previous-$(date +%s)"
    [ ! -e "$BACKUP" ] || { echo "Backup path already exists; build not installed."; exit 1; }
    mv "$FINAL" "$BACKUP"
fi
mv "$APP" "$FINAL"
printf '%s\n' "$FINAL"

#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -P "$(dirname "$0")" && pwd)
cd "$SCRIPT_DIR"
xcrun clang -O2 -fobjc-arc native_ocr.m \
  -framework Foundation \
  -framework CoreGraphics \
  -framework ImageIO \
  -framework Vision \
  -o native_ocr

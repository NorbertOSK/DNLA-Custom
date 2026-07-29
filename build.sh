#!/bin/sh
# Build the "DNLA Custom" macOS app from the dlnacast sources.
# All build tooling is installed inside ./.venv — the system Python is untouched.
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
.venv/bin/pip install --quiet --upgrade pip pyinstaller pyobjc-framework-Cocoa

if [ ! -f assets/icon.icns ]; then
    .venv/bin/python make_icon.py
fi

.venv/bin/pyinstaller --noconfirm --clean --onefile --windowed \
    --icon assets/icon.icns \
    --name "DNLA Custom" dnla_custom_gui.py

echo
echo "Build complete:"
echo "  Binary     : dist/DNLA Custom"
echo "  App bundle : dist/DNLA Custom.app"

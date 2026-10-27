#!/bin/bash
# Builds EasySwitch Relay for macOS.
#
#     bash macos/build.sh
#
# Needs Python 3.10-3.14 on THIS Mac only. The built-in /usr/bin/python3 is
# 3.9 and too old; get one from https://www.python.org/downloads/macos/ or
# `brew install python@3.12`. The result runs without Python.
#
# Output (ARCH is arm64 for Apple Silicon, x86_64 for Intel):
#     dist/EasySwitchRelay-macos-ARCH/        the app plus install/uninstall scripts
#     dist/EasySwitchRelay-macos-ARCH.zip     the same, zipped
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY=""
for c in python3.13 python3.12 python3.14 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1; then
        v="$("$c" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
        if [[ "$v" =~ ^3\.(1[0-4])$ ]]; then PY="$c"; break; fi
    fi
done
if [[ -z "$PY" ]]; then
    echo "Python 3.10-3.14 not found. Install it from https://www.python.org/downloads/macos/" >&2
    echo "or with 'brew install python@3.12' (only needed for building), then run this again." >&2
    exit 1
fi
echo "Using Python: $("$PY" -c 'import sys; print(sys.version)')"

# Fresh, isolated build environment every time.
rm -rf .build-venv
"$PY" -m venv .build-venv
VPY="$ROOT/.build-venv/bin/python"

# Hash-locked, binary-only install: every file must match a sha256 in
# requirements-build.txt, and no package may run build code during install.
"$VPY" -m pip install --disable-pip-version-check --require-hashes --only-binary ':all:' \
    -r requirements-build.txt

"$VPY" -m PyInstaller --noconfirm --clean --distpath build/dist --workpath build/work \
    easyswitch-relay.spec

APP_BUILT="build/dist/EasySwitch Relay.app"
# Re-sign the whole bundle ad hoc so macOS sees one consistent app identity
# (that's what the Input Monitoring permission is attached to).
codesign --force --deep --sign - "$APP_BUILT"
codesign --verify --deep --strict "$APP_BUILT"

ARCH="$(uname -m)"
OUT="dist/EasySwitchRelay-macos-$ARCH"
rm -rf "$OUT" "$OUT.zip"
mkdir -p "$OUT"
ditto "$APP_BUILT" "$OUT/EasySwitch Relay.app"
cp macos/install.sh macos/uninstall.sh README.md "$OUT/"
chmod +x "$OUT/install.sh" "$OUT/uninstall.sh"

ditto -c -k --keepParent "$OUT" "$OUT.zip"
HASH="$(shasum -a 256 "$OUT.zip" | cut -d' ' -f1)"
echo "$HASH  $(basename "$OUT.zip")" > "$OUT.zip.sha256"

echo
echo "Built:  $ROOT/$OUT.zip"
echo "SHA256: $HASH"
echo
echo "Install on this Mac:  bash \"$ROOT/$OUT/install.sh\""

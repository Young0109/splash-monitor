#!/usr/bin/env bash
# Build "Splash Monitor.app" with PyInstaller (macOS, Apple Silicon).
#
# IMPORTANT — Tk version:
#   Use a Python backed by Tcl/Tk >= 8.6.8 (python.org installer, Homebrew
#   python-tk, or a uv-managed CPython with tcl-tk). Apple's system
#   /usr/bin/python3 ships a legacy Tk 8.5 whose Canvas rendering is broken
#   on recent macOS: the window opens but all canvas-drawn content (the
#   four cards) is blank. The script below refuses such an environment.
#
# Usage:
#   PY=/usr/local/bin/python3.15 ./build_app.sh      # explicit interpreter
#   ./build_app.sh                                    # default PY (python3.12)
#
# Output: dist/Splash Monitor.app
set -euo pipefail
cd "$(dirname "$0")"

PY="${PY:-python3.12}"
NAME="Splash Monitor"
BUNDLE_ID="com.local.splash-monitor"
VENV="${VENV:-$PWD/.build-venv}"

if [ ! -f assets/AppIcon.icns ]; then
  echo "==> Generating app icon"
  python3 tools/make_icon.py --out assets/AppIcon.icns --save-master assets/icon_master.png
fi

if [ ! -d "$VENV" ]; then
  echo "==> Creating build venv with $PY"
  "$PY" -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip pyinstaller
fi

echo "==> Checking bundled Tcl/Tk version"
"$VENV/bin/python" - <<'EOF'
import sys, _tkinter
v = _tkinter.TK_VERSION
print("    Tcl/Tk:", _tkinter.TCL_VERSION, "/", v)
parts = [int(x) for x in v.split(".")[:2]]
if parts < [8, 6]:
    sys.exit("ERROR: Tk %s is too old (Canvas renders blank on modern macOS).\n"
             "Re-run with PY=<python backed by Tk 8.6.8+/9.x>, e.g. the "
             "python.org installer build." % v)
EOF

echo "==> Building"
# NOTE: PyInstaller resolves --icon relative to --specpath, so pass an absolute path.
"$VENV/bin/pyinstaller" --noconfirm --clean --windowed \
  --name "$NAME" \
  --icon "$PWD/assets/AppIcon.icns" \
  --osx-bundle-identifier "$BUNDLE_ID" \
  --distpath dist --workpath build --specpath build \
  splash_monitor.py

# Sanity: bundle must carry its own Tcl/Tk, not fall back to the system one.
if ! ls "dist/$NAME.app/Contents/Frameworks/" | grep -qiE '^tcl'; then
  echo "ERROR: no bundled Tcl framework found — check the build environment." >&2
  exit 1
fi

echo "==> Done: dist/$NAME.app"

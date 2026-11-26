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

echo "==> Checking bundled Tcl/Tk version (minimum 8.6.8)"
"$VENV/bin/python" - <<'EOF'
import re, sys, _tkinter
# _tkinter.TK_VERSION only reports major.minor ("8.6", "9.0"); the patch
# level must come from the Tk interpreter itself ("info patchlevel").
patch = None
try:
    import tkinter
    _r = tkinter.Tk()
    patch = _r.tk.call("info", "patchlevel")
    _r.destroy()
except Exception:
    pass
ref = patch or _tkinter.TK_VERSION
nums = [int(x) for x in re.findall(r"\d+", ref)[:3]]
print("    Tcl/Tk:", _tkinter.TCL_VERSION, "/", _tkinter.TK_VERSION,
      "patchlevel:", patch if patch else "unavailable")
if len(nums) >= 3:                                  # full major/minor/patch check
    if nums[:3] < [8, 6, 8]:
        sys.exit("ERROR: Tk %s is below the minimum 8.6.8 "
                 "(older Tk Canvas rendering is blank on modern macOS; "
                 "Tk 9.x is fully supported). Re-run with "
                 "PY=<python backed by Tk 8.6.8+/9.x>, e.g. the python.org "
                 "installer build." % ref)
elif nums < [8, 6]:
    sys.exit("ERROR: Tk %s is too old (minimum 8.6.8; Tk 9.x supported)." % ref)
else:
    print("    WARNING: patch level unavailable; assuming >= 8.6.8")
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

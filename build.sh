#!/bin/bash
# Build Meeting Notes.app
#
#   ./build.sh            build into dist/
#   ./build.sh --install  build, then copy into /Applications
#
set -euo pipefail
cd "$(dirname "$0")"
PY=.venv/bin/python
say() { printf "\033[1m==>\033[0m %s\n" "$1"; }

if [ ! -x "$PY" ]; then
  say "Creating the virtualenv"
  BASE_PY=$(command -v python3.11 || command -v python3)
  "$BASE_PY" -m venv .venv
fi

say "Installing build dependencies"
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q -r requirements.txt py2app

say "Building the window (React)"
command -v npm >/dev/null || { echo "npm not found. Install it: brew install node"; exit 1; }
npm ci --prefix ui --no-audit --no-fund >/dev/null
npm run build --prefix ui >/dev/null

say "Building the recorder (Swift)"
mkdir -p build
swiftc -O -o build/mn-recorder recorder/main.swift \
  -framework CoreAudio -framework AVFoundation 2>build/swiftc.log \
  || { echo "The recorder did not compile:"; cat build/swiftc.log; exit 1; }

say "Drawing the icons"
"$PY" assets/make_icon.py --icns >/dev/null

say "Building the app"
rm -rf dist build/bdist* build/lib*
LOG=build/py2app.log
"$PY" setup.py py2app >"$LOG" 2>&1 || {
  echo "Build failed. Last lines of $LOG:"; tail -30 "$LOG"; exit 1; }

APP="dist/Meeting Notes.app"
[ -d "$APP" ] || { echo "No app produced; see $LOG"; exit 1; }

# Ad-hoc signature, so macOS keeps the app's permissions between launches
# instead of asking about Outlook and notifications every single time.
# Next to the app's own executable: started by the app, it is the app that
# macOS asks about the microphone and the system audio, not the helper.
cp build/mn-recorder "$APP/Contents/MacOS/mn-recorder"

say "Signing (ad-hoc)"
# An unsigned app loses its Keychain and Outlook permissions, so a failure here
# stops the build instead of producing an app that half works.
codesign --force --sign - "$APP/Contents/MacOS/mn-recorder" || { echo "codesign failed"; exit 1; }
codesign --force --deep --sign - "$APP" || { echo "codesign failed"; exit 1; }
codesign --verify --deep --strict "$APP" || { echo "the signature does not verify"; exit 1; }

say "Built: $APP  ($(du -sh "$APP" | cut -f1))"

if [ "${1:-}" = "--install" ]; then
  say "Installing to /Applications"
  rm -rf "/Applications/Meeting Notes.app"
  cp -R "$APP" /Applications/
  say "Open it from Launchpad, or: open '/Applications/Meeting Notes.app'"
else
  say "Try it: open '$APP'"
  say "Install it: ./build.sh --install"
fi

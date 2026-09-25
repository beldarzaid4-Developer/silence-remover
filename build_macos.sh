#!/usr/bin/env bash
# build_macos.sh
#
# Builds DeadAirRemover.app as a native Intel (x86_64) macOS application,
# with ffmpeg/ffprobe bundled inside so the end user needs nothing else
# installed.
#
# Run this ON A MAC (Intel, or Apple Silicon with Rosetta 2 installed - the
# script forces an x86_64 build either way via `arch -x86_64`).
#
# Usage:
#   chmod +x build_macos.sh
#   ./build_macos.sh
#
# Optional: if you already have Intel-native ffmpeg/ffprobe binaries you'd
# rather use (e.g. your own build, or one from a different provider), just
# place them at ./ffmpeg-bin/ffmpeg and ./ffmpeg-bin/ffprobe (chmod +x both)
# BEFORE running this script, and it will skip the download step.

set -euo pipefail
cd "$(dirname "$0")"

echo "=== Dead-Air Remover: macOS Intel build ==="

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "ERROR: this script must be run on macOS." >&2
  exit 1
fi

# --- 0. Make sure Rosetta 2 is available if we're on Apple Silicon -------- #
HOST_ARCH="$(uname -m)"
if [[ "$HOST_ARCH" == "arm64" ]]; then
  if ! arch -x86_64 /usr/bin/true 2>/dev/null; then
    echo "This is an Apple Silicon Mac and Rosetta 2 isn't installed yet."
    echo "Installing Rosetta 2 (one-time, requires admin password)..."
    softwareupdate --install-rosetta --agree-to-license
  fi
  echo "Host is Apple Silicon - forcing an Intel (x86_64) build via Rosetta."
else
  echo "Host is Intel (x86_64) - building natively."
fi
RUN_X64=(arch -x86_64)

# --- 1. Fetch static Intel ffmpeg/ffprobe if not already provided --------- #
mkdir -p ffmpeg-bin
if [[ -x "ffmpeg-bin/ffmpeg" && -x "ffmpeg-bin/ffprobe" ]]; then
  echo "Using existing ffmpeg-bin/ffmpeg + ffprobe (already present)."
else
  echo "Downloading static Intel (x86_64) ffmpeg + ffprobe from evermeet.cx..."
  TMP="$(mktemp -d)"
  curl -fL "https://evermeet.cx/ffmpeg/getrelease/ffmpeg/zip" -o "$TMP/ffmpeg.zip"
  curl -fL "https://evermeet.cx/ffmpeg/getrelease/ffprobe/zip" -o "$TMP/ffprobe.zip"
  unzip -o "$TMP/ffmpeg.zip" -d "$TMP" >/dev/null
  unzip -o "$TMP/ffprobe.zip" -d "$TMP" >/dev/null
  mv "$TMP/ffmpeg" ffmpeg-bin/ffmpeg
  mv "$TMP/ffprobe" ffmpeg-bin/ffprobe
  chmod +x ffmpeg-bin/ffmpeg ffmpeg-bin/ffprobe
  rm -rf "$TMP"

  # Sanity-check the download actually is x86_64 (evermeet.cx has always
  # published Intel-only builds, but verify rather than assume).
  FILE_INFO="$(file ffmpeg-bin/ffmpeg)"
  echo "  -> $FILE_INFO"
  if ! echo "$FILE_INFO" | grep -q "x86_64"; then
    echo "WARNING: downloaded ffmpeg does not report as x86_64. If the"
    echo "evermeet.cx build changed, download an Intel static build"
    echo "manually (e.g. https://www.osxexperts.net) and place it at"
    echo "ffmpeg-bin/ffmpeg / ffmpeg-bin/ffprobe, then re-run this script."
    exit 1
  fi
fi

# --- 2. Python env + PyInstaller ------------------------------------------ #
echo "Setting up an x86_64 Python virtualenv..."
"${RUN_X64[@]}" python3 -m venv .venv-x64
source .venv-x64/bin/activate
"${RUN_X64[@]}" python3 -m pip install --upgrade pip pyinstaller

# --- 3. Build --------------------------------------------------------------#
echo "Building DeadAirRemover.app (x86_64)..."
rm -rf build dist
"${RUN_X64[@]}" python3 -m PyInstaller silence_remover.spec --noconfirm

APP="dist/DeadAirRemover.app"
if [[ ! -d "$APP" ]]; then
  echo "ERROR: build did not produce $APP" >&2
  exit 1
fi

# --- 4. Ad-hoc code sign (reduces, doesn't eliminate, Gatekeeper friction) #
echo "Ad-hoc signing the app bundle..."
codesign --force --deep -s - "$APP" || echo "  (codesign failed/non-fatal, continuing)"

# --- 5. Verify architecture ------------------------------------------------#
BIN_ARCH="$(file "$APP/Contents/MacOS/DeadAirRemover")"
echo "Built binary: $BIN_ARCH"
if ! echo "$BIN_ARCH" | grep -q "x86_64"; then
  echo "ERROR: built app is not x86_64!" >&2
  exit 1
fi

# --- 6. Real end-to-end self-test, using the .app's OWN bundled ffmpeg ---- #
echo ""
echo "=== Running end-to-end self-test inside the packaged app ==="
"${RUN_X64[@]}" "$APP/Contents/MacOS/DeadAirRemover" --selftest
SELFTEST_STATUS=$?

if [[ $SELFTEST_STATUS -ne 0 ]]; then
  echo "SELF-TEST FAILED (exit $SELFTEST_STATUS)." >&2
  exit 1
fi

echo ""
echo "=== BUILD + SELF-TEST PASSED ==="
echo "App bundle: $APP"
echo ""
echo "To launch it normally for the first time (it's ad-hoc signed, not"
echo "notarized, so Gatekeeper will block a plain double-click):"
echo "  1. In Finder, right-click (or Control-click) DeadAirRemover.app"
echo "  2. Choose 'Open'"
echo "  3. Click 'Open' again in the confirmation dialog"
echo "  (only needed once - after that it opens normally)"

#!/usr/bin/env bash
# Wraps a macOS clarp-slint binary in Clarp.app (Info.plist, the icon from
# src-tauri/icons/icon.icns), signs it ad hoc and zips it for download:
#   desktop-rs/tools/macos-bundle.sh BINARY VERSION ZIP
# Runs on macOS only (plutil, codesign, ditto); CI calls it.
set -euo pipefail
binary=$1 version=$2 zip=$3
root=$(cd "$(dirname "$0")/../.." && pwd)
work=$(mktemp -d "${RUNNER_TEMP:-/var/tmp}/clarp-bundle.XXXXXX")
trap 'rm -rf "$work"' EXIT
app="$work/Clarp.app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
cp "$binary" "$app/Contents/MacOS/clarp-slint"
chmod 755 "$app/Contents/MacOS/clarp-slint"

# The Tauri shell's macOS icon, the same Clarp mark.
cp "$root/src-tauri/icons/icon.icns" "$app/Contents/Resources/Clarp.icns"

cat > "$app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key><string>en</string>
    <key>CFBundleExecutable</key><string>clarp-slint</string>
    <key>CFBundleIdentifier</key><string>com.maxteabag.Clarp</string>
    <key>CFBundleName</key><string>Clarp</string>
    <key>CFBundleDisplayName</key><string>Clarp</string>
    <key>CFBundleIconFile</key><string>Clarp</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
    <key>CFBundleShortVersionString</key><string>0.1.0</string>
    <key>CFBundleVersion</key><string>${version}</string>
    <key>LSMinimumSystemVersion</key><string>11.0</string>
    <key>LSApplicationCategoryType</key><string>public.app-category.productivity</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>NSSupportsAutomaticGraphicsSwitching</key><true/>
    <key>NSMicrophoneUsageDescription</key><string>Clarp records your voice when you dictate a message.</string>
</dict>
</plist>
PLIST
plutil -lint "$app/Contents/Info.plist"

codesign --force --sign - --timestamp=none "$app"
codesign --verify --strict --verbose=2 "$app"
mkdir -p "$(dirname "$zip")"
rm -f "$zip"
ditto -c -k --sequesterRsrc --keepParent "$app" "$zip"
echo "bundled $zip"

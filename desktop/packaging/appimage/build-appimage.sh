#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_DIR="${CLARP_DESKTOP_SOURCE_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
DESKTOP_DIR="$(cd "$DESKTOP_DIR" && pwd)"
BUILD_DIR="${CLARP_APPIMAGE_BUILD_DIR:-$DESKTOP_DIR/build/appimage}"
APP_DIR="$BUILD_DIR/AppDir"

for tool in cmake ninja linuxdeploy linuxdeploy-plugin-qt; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    printf 'missing required AppImage tool: %s\n' "$tool" >&2
    exit 2
  fi
done

QMAKE6="${QMAKE:-$(command -v qmake6 || command -v qmake || true)}"
if [[ -z "$QMAKE6" ]]; then
  printf 'missing required AppImage tool: qmake6\n' >&2
  exit 2
fi
if [[ "$($QMAKE6 -query QT_VERSION)" != 6.* ]]; then
  printf 'AppImage packaging requires qmake from Qt 6\n' >&2
  exit 2
fi

cmake -E remove_directory "$BUILD_DIR/cmake"
cmake -E remove_directory "$APP_DIR"

cmake_args=(
  -S "$DESKTOP_DIR"
  -B "$BUILD_DIR/cmake"
  -G Ninja
  -DCMAKE_BUILD_TYPE=Release
  -DCMAKE_INSTALL_PREFIX=/usr
  -DBUILD_TESTING=OFF
  -DCLARP_WARNINGS_AS_ERRORS=ON
)
if [[ -n "${CLARP_RELEASE_VERSION:-}" ]]; then
  cmake_args+=("-DCLARP_DESKTOP_VERSION=$CLARP_RELEASE_VERSION")
fi
cmake "${cmake_args[@]}"
cmake --build "$BUILD_DIR/cmake" --parallel
DESTDIR="$APP_DIR" cmake --install "$BUILD_DIR/cmake"

for QT_LICENSE in \
  /usr/share/licenses/qt6-base/LGPL-3.0-only.txt \
  /usr/share/licenses/freedesktop-sdk/gnupg/COPYING.LGPL3; do
  if [[ -f "$QT_LICENSE" ]]; then
    cmake -E make_directory "$APP_DIR/usr/share/licenses/qt6"
    cmake -E copy_if_different "$QT_LICENSE" \
      "$APP_DIR/usr/share/licenses/qt6/LGPL-3.0-only.txt"
    break
  fi
done

export QML_SOURCES_PATHS="$DESKTOP_DIR/qml"
APPIMAGE_OUTPUT="${CLARP_APPIMAGE_OUTPUT:-Clarp-native-x86_64.AppImage}"
export LDAI_OUTPUT="$APPIMAGE_OUTPUT"
if [[ -n "${CLARP_RELEASE_VERSION:-}" ]]; then
  export LINUXDEPLOY_OUTPUT_VERSION="$CLARP_RELEASE_VERSION"
fi
# AppStream metadata is validated by the quality gate and CI. Keeping the
# packager offline makes release builds reproducible and avoids a DNS failure
# being mistaken for invalid local metadata.
export LDAI_NO_APPSTREAM=1
if [[ -n "${CLARP_APPIMAGE_RUNTIME_FILE:-}" ]]; then
  export LDAI_RUNTIME_FILE="$CLARP_APPIMAGE_RUNTIME_FILE"
fi
export QMAKE="$QMAKE6"
export EXTRA_PLATFORM_PLUGINS="libqwayland.so;libqoffscreen.so"
# linuxdeploy bundles an older binutils whose strip cannot read the RELR
# sections emitted by current Arch/Fedora toolchains. The copied libraries are
# already stripped; skipping this second pass is both safe and required.
export NO_STRIP=1
linuxdeploy \
  --appdir "$APP_DIR" \
  --executable "$APP_DIR/usr/bin/clarp-desktop" \
  --desktop-file "$APP_DIR/usr/share/applications/com.maxteabag.Clarp.desktop" \
  --icon-file "$APP_DIR/usr/share/icons/hicolor/scalable/apps/com.maxteabag.Clarp.svg" \
  --plugin qt \
  --output appimage

# appimagetool writes zsync metadata in its working directory even when the
# AppImage output is absolute. Keep both release files beside each other.
GENERATED_ZSYNC="$PWD/$(basename "$APPIMAGE_OUTPUT").zsync"
if [[ -f "$GENERATED_ZSYNC" && ! -f "$APPIMAGE_OUTPUT.zsync" ]]; then
  mv "$GENERATED_ZSYNC" "$APPIMAGE_OUTPUT.zsync"
fi

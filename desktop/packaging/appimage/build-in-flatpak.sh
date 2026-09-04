#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"
OUTPUT_DIR="${CLARP_APPIMAGE_OUTPUT_DIR:-$ROOT_DIR/dist}"
TOOL_DIR="${CLARP_APPIMAGE_TOOL_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/clarp/appimage-tools}"
VERSION="${CLARP_RELEASE_VERSION:-}"

if [[ -z "$VERSION" ]]; then
  VERSION="$(sed -n 's/^version = "\([0-9][0-9.]*\)"$/\1/p' "$ROOT_DIR/pyproject.toml" | head -n 1)"
fi
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  printf 'invalid Clarp release version: %s\n' "$VERSION" >&2
  exit 2
fi
if [[ "$(uname -m)" != x86_64 ]]; then
  printf 'the AppImage release lane currently supports x86_64 only\n' >&2
  exit 2
fi

for tool in curl flatpak sha256sum zsyncmake; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    printf 'missing required release tool: %s\n' "$tool" >&2
    exit 2
  fi
done

mkdir -p "$OUTPUT_DIR" "$TOOL_DIR"

fetch_pinned() {
  local name="$1"
  local url="$2"
  local expected="$3"
  local destination="$TOOL_DIR/$name"
  local temporary="$destination.download"

  if [[ -f "$destination" ]] &&
     [[ "$(sha256sum "$destination" | cut -d' ' -f1)" == "$expected" ]]; then
    return
  fi
  curl --fail --location --retry 3 --output "$temporary" "$url"
  if [[ "$(sha256sum "$temporary" | cut -d' ' -f1)" != "$expected" ]]; then
    rm -f "$temporary"
    printf 'checksum mismatch for %s\n' "$name" >&2
    exit 1
  fi
  mv "$temporary" "$destination"
}

fetch_pinned \
  linuxdeploy \
  https://github.com/linuxdeploy/linuxdeploy/releases/download/1-alpha-20251107-1/linuxdeploy-x86_64.AppImage \
  c20cd71e3a4e3b80c3483cef793cda3f4e990aca14014d23c544ca3ce1270b4d
fetch_pinned \
  linuxdeploy-plugin-qt \
  https://github.com/linuxdeploy/linuxdeploy-plugin-qt/releases/download/1-alpha-20250213-1/linuxdeploy-plugin-qt-x86_64.AppImage \
  15106be885c1c48a021198e7e1e9a48ce9d02a86dd0a1848f00bdbf3c1c92724
fetch_pinned \
  runtime-x86_64 \
  https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64 \
  2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d
chmod 0755 "$TOOL_DIR/linuxdeploy" "$TOOL_DIR/linuxdeploy-plugin-qt"

flatpak remote-add --user --if-not-exists \
  flathub https://dl.flathub.org/repo/flathub.flatpakrepo
flatpak install --user --noninteractive -y flathub org.kde.Sdk//6.11

IMAGE_NAME="Clarp-${VERSION}-x86_64.AppImage"
UPDATE_OWNER="${CLARP_UPDATE_OWNER:-Maxteabag}"
UPDATE_REPOSITORY="${CLARP_UPDATE_REPOSITORY:-clarp}"

flatpak run --user \
  --filesystem="$ROOT_DIR" \
  --filesystem="$OUTPUT_DIR" \
  --filesystem="$TOOL_DIR" \
  --env=APPIMAGE_EXTRACT_AND_RUN=1 \
  --env=CLARP_APPIMAGE_OUTPUT="$OUTPUT_DIR/$IMAGE_NAME" \
  --env=CLARP_APPIMAGE_RUNTIME_FILE="$TOOL_DIR/runtime-x86_64" \
  --env=CLARP_RELEASE_VERSION="$VERSION" \
  --env=CLARP_SOURCE_ROOT="$ROOT_DIR" \
  --env=CLARP_TOOLS_DIR="$TOOL_DIR" \
  --env=LDAI_UPDATE_INFORMATION="gh-releases-zsync|$UPDATE_OWNER|$UPDATE_REPOSITORY|latest|Clarp-*-x86_64.AppImage.zsync" \
  --command=bash \
  org.kde.Sdk//6.11 \
  -lc 'export PATH="$CLARP_TOOLS_DIR:$PATH"; cd "$CLARP_SOURCE_ROOT/desktop"; exec packaging/appimage/build-appimage.sh'

if [[ ! -x "$OUTPUT_DIR/$IMAGE_NAME" ]]; then
  printf 'AppImage output is missing\n' >&2
  exit 1
fi
if [[ ! -f "$OUTPUT_DIR/$IMAGE_NAME.zsync" ]]; then
  (
    cd "$OUTPUT_DIR"
    zsyncmake -u "$IMAGE_NAME" -o "$IMAGE_NAME.zsync" "$IMAGE_NAME"
  )
fi
(
  cd "$OUTPUT_DIR"
  sha256sum "$IMAGE_NAME" > "$IMAGE_NAME.sha256"
)

APPIMAGE_EXTRACT_AND_RUN=1 \
QT_QPA_PLATFORM=offscreen \
QT_QUICK_BACKEND=software \
CLARP_SCREENSHOT_PATH="$OUTPUT_DIR/appimage-smoke.png" \
  "$OUTPUT_DIR/$IMAGE_NAME"
test -s "$OUTPUT_DIR/appimage-smoke.png"

#!/usr/bin/env bash
# GitHub-hosted Ubuntu setup only. Three apt attempts (two retries), then a
# pinned GitHub release fallback: <=10m05s of timeout/kill/backoff allowances,
# within the workflow step's 12-minute bound. Never change the test sandbox.
set -euo pipefail

if command -v ffmpeg >/dev/null 2>&1 && ffmpeg -version >/dev/null 2>&1; then
  echo 'ffmpeg already available; skipping installation'
  exit 0
fi

for attempt in 1 2 3; do
  echo "ffmpeg apt attempt $attempt/3"
  # Root owns the timeout, so it can terminate apt and its children. Update,
  # optional interrupted-install repair and install share ONE attempt deadline.
  if sudo timeout --signal=TERM --kill-after=15s 120s bash -s -- "$attempt" <<'APT'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
if (($1 > 1)); then dpkg --configure -a; fi
opts=(-o Acquire::Retries=0 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30
      -o DPkg::Lock::Timeout=30 -o APT::Update::Error-Mode=any)
apt-get "${opts[@]}" update
apt-get "${opts[@]}" install -y --no-install-recommends ffmpeg
APT
  then
    if command -v ffmpeg >/dev/null 2>&1 && ffmpeg -version >/dev/null 2>&1; then
      echo "ffmpeg available after attempt $attempt"
      exit 0
    fi
    echo 'apt returned success but ffmpeg is still unusable' >&2
  else
    rc=$?
    echo "ffmpeg apt attempt $attempt failed (exit $rc)" >&2
  fi
  if ((attempt < 3)); then sleep 2; fi
done

# Month-end release (upstream retains it for two years) + exact archive hash,
# never the moving "latest" asset or an expiring daily build. The
# upstream release's checksums.sha256 and GitHub asset digest agree on this pin.
release=autobuild-2026-10-31-12-59
bundle=ffmpeg-N-128227-g4811625b5f-linux64-gpl
digest=67929be39bbb696f41d3295686597a439aeac902f61d82be660ccc1453c1fdfb
url="https://github.com/BtbN/FFmpeg-Builds/releases/download/$release/$bundle.tar.xz"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
echo "Ubuntu apt unavailable; using pinned GitHub ffmpeg $release"
timeout --signal=TERM --kill-after=15s 180s bash -s -- "$url" "$digest" "$bundle" "$tmp" <<'STATIC'
set -euo pipefail
url=$1 digest=$2 bundle=$3 tmp=$4
curl --fail --location --silent --show-error --retry 1 --retry-delay 2 \
  --connect-timeout 15 --max-time 120 --output "$tmp/ffmpeg.tar.xz" "$url"
printf '%s  %s\n' "$digest" "$tmp/ffmpeg.tar.xz" | sha256sum --check --status
tar -xJf "$tmp/ffmpeg.tar.xz" -C "$tmp" --strip-components=1 --no-same-owner \
  "$bundle/bin/ffmpeg" "$bundle/bin/ffprobe"
# /usr/bin is visible on PATH inside ci_contained.sh. RUNNER_TEMP/GITHUB_PATH
# alone would be hidden/reset by that unchanged sandbox.
sudo install -m 0755 "$tmp/bin/ffmpeg" "$tmp/bin/ffprobe" /usr/bin/
/usr/bin/ffmpeg -version
/usr/bin/ffprobe -version
STATIC
echo 'Pinned ffmpeg and ffprobe installed and verified'

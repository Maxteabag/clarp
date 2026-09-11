#!/bin/sh
# Isolated Codex writer-lock + Clarp source checks. No live auth, no network.
set -eu
ROOT="$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)"
CTX="${CLARP_ISOLATED_CTX:-/var/tmp/clarp-isolated-lock-ctx}"
IMAGE="${CLARP_ISOLATED_IMAGE:-clarp-codex-isolated-lock}"

# Repo .dockerignore excludes tests/, so stage a tiny context.
rm -rf "$CTX"
mkdir -p "$CTX/tests/qa" "$CTX/server/lib"
cp "$ROOT/tests/qa/fake_codex.py" \
   "$ROOT/tests/qa/isolated_codex_lock.py" \
   "$ROOT/tests/qa/Dockerfile.isolated" \
   "$CTX/tests/qa/"
cp "$CTX/tests/qa/Dockerfile.isolated" "$CTX/Dockerfile"
cp "$ROOT/server/lib/codex_app_server.py" \
   "$ROOT/server/lib/agy_runner.py" \
   "$ROOT/server/lib/error_classify.py" \
   "$CTX/server/lib/"

if find "$CTX" -name auth.json -print -quit | grep -q .; then
  echo "refusing: auth.json leaked into isolated context" >&2
  exit 2
fi

docker build -t "$IMAGE" "$CTX"
docker run --rm --network=none "$IMAGE"

#!/usr/bin/env bash
# Source/admission is frozen by compute before this supervisor may run remotely.
set -euo pipefail
exec uv run --frozen --group dev python "$(dirname "$0")/supervisor.py" "$@"

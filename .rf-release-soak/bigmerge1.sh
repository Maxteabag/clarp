#!/usr/bin/env bash
# Source/admission is frozen by compute before this supervisor may run remotely.
set -euo pipefail
exec python3 "$(dirname "$0")/supervisor.py" "$@"

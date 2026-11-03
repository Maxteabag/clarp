#!/usr/bin/env bash
# Print the stable patch-id of <base>..<commit> (REFACTOR.md G4): the merge
# captain compares it before and after a rebase.
# Usage: tools/refactor/patchid.sh <base> <commit>
set -euo pipefail
[ $# -eq 2 ] || { echo "usage: $0 <base> <commit>" >&2; exit 64; }
git diff "$1" "$2" | git patch-id --stable | awk '{print $1}'

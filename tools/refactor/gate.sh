#!/usr/bin/env bash
# The refactor program's G1 gate (REFACTOR.md section 5). Full suites never run on
# the owner's laptop, so this dispatches .github/workflows/tests.yml on a pushed
# branch and judges the run by its JUnit results, never by a wrapper's exit code.
# PASS only when the run tested exactly <sha>, concluded success, and the Python
# and JavaScript results both exist with tests > 0 and no failure or error outside
# tests/quarantine.txt (CI runs the quarantined tests; their failures are tolerated).
# Usage: tools/refactor/gate.sh <branch> [<sha>]
#   branch: refactor/ci/<name>-<sha12>, already pushed and pointing at <sha>
#   (sha defaults to the local branch head). GATE_RUN=<id> judges an existing run.
#   Each judgement downloads into a fresh directory under $GATE_DIR; a failed
#   download fails the gate. tools/refactor/gate_judge.py reads the results.
set -uo pipefail
REPO="${GATE_REPO:-Maxteabag/clarp}"
# Dispatch needs write access: use the repo owner's gh login for this process only.
export GH_TOKEN="${GH_TOKEN:-$(gh auth token -u "${REPO%%/*}" 2>/dev/null)}"
branch="${1:?usage: $0 <branch> [<sha>]}"
sha=$(git rev-parse --verify "${2:-refs/heads/$branch}^{commit}") || exit 2
remote=$(git ls-remote origin "refs/heads/$branch" | awk '{print $1}')
[ "$remote" = "$sha" ] || { echo "gate: FAIL origin/$branch is ${remote:-missing}, not $sha" >&2; exit 2; }
base="${GATE_DIR:-/var/tmp/clarp-refactor-program/logs}"
mkdir -p "$base" || exit 2
poll="${GATE_POLL_S:-30}"
run="${GATE_RUN:-}"
if [ -z "$run" ]; then
  since=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  gh workflow run tests.yml -R "$REPO" --ref "$branch" || exit 2
  for _ in $(seq 1 40); do
    sleep "$((poll < 6 ? poll : 6))"
    run=$(gh run list -R "$REPO" -w tests.yml -b "$branch" -e workflow_dispatch -L 10 \
      --json databaseId,headSha,createdAt \
      --jq "[.[] | select(.headSha == \"$sha\" and .createdAt >= \"$since\")] | last | .databaseId // empty")
    [ -n "$run" ] && break
  done
  [ -n "$run" ] || { echo "gate: FAIL dispatched run for $sha not found" >&2; exit 2; }
fi
echo "gate: $sha run=https://github.com/$REPO/actions/runs/$run"
until [ "$(gh run view "$run" -R "$REPO" --json status --jq .status 2>/dev/null)" = completed ]; do sleep "$poll"; done
read -r conclusion run_sha attempt < <(gh run view "$run" -R "$REPO" --json conclusion,headSha,attempt \
  --jq '"\(.conclusion) \(.headSha) \(.attempt)"')
# A fresh directory per judgement: results from another run or attempt can never be judged.
out=$(mktemp -d "$base/gate-${sha:0:12}-run$run-a${attempt:-0}-XXXXXX") || exit 2
echo "gate: dir=$out"
gh run download "$run" -R "$REPO" -n test-results -D "$out" >/dev/null 2>&1 \
  || { echo "gate: FAIL could not download test-results of run $run" >&2; exit 2; }
gh run view "$run" -R "$REPO" --attempt "${attempt:-1}" --json jobs > "$out/jobs.json" \
  || { echo "gate: FAIL could not read the jobs of run $run" >&2; exit 2; }
git show "$sha:tests/quarantine.txt" > "$out/quarantine.txt" 2>/dev/null || : > "$out/quarantine.txt"
python3 "$(dirname "$0")/gate_judge.py" "$out" "$conclusion" "$run_sha" "$sha"

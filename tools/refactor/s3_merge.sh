#!/usr/bin/env bash
# Merge main into refactor/integration for S3 (docs/architecture/route-registry.md, section 11).
#
#   tools/refactor/s3_merge.sh <main-ref> <integration-ref> <work-branch>
#
# server/server.py is never merged by hand: the result takes main's server.py,
# replays the S3 seam and preparatory server.py diffs (tools/refactor/s3_replay.txt)
# with `git apply --3way`, then regenerates the moves with s3_apply (helpers step,
# then groups), committing each stage so ast_identity can verify it:
#   A  merge commit: main's server.py + replayed diffs, generated files removed
#   B  s3_apply --helpers      verified: ast_identity A B server.py=server.py,lib/routes/helpers.py
#   C  s3_apply --all          verified: ast_identity B C server.py::Handler=<every group mixin>
# then the local gates. It prints the G1 and G2 commands for the merge captain;
# G2 compares main against C and must show zero differences.
#
# Exit codes: 0 done (gates green; run G1/G2 next), 2 usage or dirty tree,
# 3 a merge conflict outside server.py and the generated files, 4 a replayed diff
# does not apply, 5 s3_apply refused, 6 ast_identity failed, 7 a local gate failed.
# The work branch is left as it stopped, for inspection; nothing is pushed.
set -uo pipefail
die() { echo "s3_merge: STOP ($1): $2" >&2; exit "$1"; }
[ $# -eq 3 ] || die 2 "usage: $0 <main-ref> <integration-ref> <work-branch>"
main=$(git rev-parse --verify "$1^{commit}") || die 2 "unknown ref $1"
integration=$(git rev-parse --verify "$2^{commit}") || die 2 "unknown ref $2"
work=$3
root=$(git rev-parse --show-toplevel)
cd "$root" || die 2 "no repository"
status=$(git status --porcelain --untracked-files=all -- server tools/refactor scripts/refactor) \
  || die 2 "git status failed"
[ -z "$status" ] || die 2 "uncommitted or untracked files under server/, tools/refactor/ or scripts/refactor/:
$status"
tracked_status=$(git status --porcelain --untracked-files=no) || die 2 "git status failed"
[ -z "$tracked_status" ] || die 2 "the working tree has uncommitted changes:
$tracked_status"
PY=${S3_PYTHON:-$root/.venv/bin/python}
[ -x "$PY" ] || die 2 "no project python at $PY (uv sync, or set S3_PYTHON)"

git switch -q -c "$work" "$integration" || die 2 "cannot create $work"
echo "s3_merge: merging $main into $integration on $work"
merge_out=$(git merge --no-commit --no-ff "$main" 2>&1)
merge_rc=$?
git rev-parse -q --verify MERGE_HEAD >/dev/null || die 3 "git merge did not start a merge (exit $merge_rc):
$merge_out"
[ "$(git rev-parse MERGE_HEAD)" = "$main" ] || die 3 "MERGE_HEAD is not $main"
conflicts=$(git diff --name-only --diff-filter=U)
if [ "$merge_rc" -ne 0 ] && [ -z "$conflicts" ]; then
  die 3 "git merge failed without conflicts (exit $merge_rc):
$merge_out"
fi
# The recipe and replay list as they stand in the merged tree.
git checkout "$main" -- tools/refactor/s3_moves.toml tools/refactor/s3_replay.txt 2>/dev/null || true
# Generated files: the helpers module and every group mixin in the recipe.
generated=$("$PY" - "$root/tools/refactor/s3_moves.toml" <<'PYEOF'
import sys, tomllib
data = tomllib.loads(open(sys.argv[1]).read())
paths = ["server/" + g["module"] for g in data.get("group", [])]
if data.get("helpers", {}).get("names"):
    paths.insert(0, "server/lib/routes/helpers.py")
print("\n".join(paths))
PYEOF
) || die 2 "cannot read tools/refactor/s3_moves.toml"
helpers_paths=$(printf '%s\n' "$generated" | grep -x 'server/lib/routes/helpers.py' || true)
group_paths=$(printf '%s\n' "$generated" | grep -vx 'server/lib/routes/helpers.py' || true)
mixins=$("$PY" - "$root/tools/refactor/s3_moves.toml" <<'PYEOF'
import sys, tomllib
data = tomllib.loads(open(sys.argv[1]).read())
print(",".join(f"{g['module']}::{g['class']}" for g in data.get("group", [])))
PYEOF
)
replay=$(grep -v '^\s*#' tools/refactor/s3_replay.txt | awk 'NF {print $1}')

for path in $conflicts; do
  case "$path" in
    server/server.py) ;;
    *) printf '%s\n' "$generated" | grep -qx "$path" || die 3 "conflict outside server.py and the generated files: $path" ;;
  esac
done

# A: main's server.py + the replayed S3 diffs; generated files removed.
git checkout "$main" -- server/server.py || die 4 "cannot take main's server.py"
for path in $generated; do git rm -q --cached --ignore-unmatch "$path"; rm -f "$path"; done
for sha in $replay; do
  git diff "$sha^" "$sha" -- server/server.py | git apply --3way --index - \
    || die 4 "the server.py diff of $sha does not apply to main's server.py; port it by hand and update s3_replay.txt"
done
git add server/server.py
git -c core.hooksPath=/dev/null commit -q -m "merge main $(git rev-parse --short "$main") into integration (S3: server.py replayed, moves to regenerate)" \
  || die 4 "cannot commit the merge"
A=$(git rev-parse HEAD)

# B: helpers step.
"$PY" tools/refactor/s3_apply.py --root "$root" --helpers || die 5 "s3_apply --helpers refused"
B=$A
stage_status=$(git status --porcelain --untracked-files=all -- server/server.py $helpers_paths) || die 5 "git status failed"
if [ -n "$stage_status" ]; then
  git add -A -- server/server.py $helpers_paths && git -c core.hooksPath=/dev/null commit -q -m "s3_apply: helpers step" \
    || die 5 "cannot commit the helpers step"
  B=$(git rev-parse HEAD)
  "$PY" scripts/refactor/ast_identity.py "$A" "$B" server.py=server.py,lib/routes/helpers.py \
    || die 6 "ast_identity (top-level) failed for the helpers step"
fi

# C: every group.
"$PY" tools/refactor/s3_apply.py --root "$root" --all || die 5 "s3_apply --all refused"
C=$B
stage_status=$(git status --porcelain --untracked-files=all -- server/server.py $group_paths) || die 5 "git status failed"
if [ -n "$stage_status" ]; then
  git add -A -- server/server.py $group_paths && git -c core.hooksPath=/dev/null commit -q -m "s3_apply: group moves" \
    || die 5 "cannot commit the group moves"
  C=$(git rev-parse HEAD)
  [ -n "$mixins" ] && { "$PY" scripts/refactor/ast_identity.py "$B" "$C" "server.py::Handler=$mixins" \
    || die 6 "ast_identity (method mode) failed for the group moves"; }
fi

stray=$(git status --porcelain --untracked-files=all -- server tools scripts) || die 5 "git status failed"
stray=$(printf '%s\n' "$stray" | grep -v -e ' server/server.py$' || true)
for path in $generated; do stray=$(printf '%s\n' "$stray" | grep -v " $path$" || true); done
[ -z "$(printf '%s' "$stray" | tr -d '[:space:]')" ] || die 5 "s3_apply left changes outside server.py and the generated files:
$stray"
git merge-base --is-ancestor "$main" HEAD || die 3 "the result does not contain $main"

# Local gates: in-process tests only (one file each; no test servers on the laptop).
heavy=/var/tmp/clarp-refactor-program/bin/heavy
for test in tests/integration/test_route_goldens.py tests/unit/test_import_layers.py \
            tests/unit/test_handler_boundary.py tests/integration/test_s3_helpers_identity.py \
            tests/unit/test_s3_apply.py; do
  [ -f "$test" ] || continue
  "$heavy" "$PY" -m pytest -n 0 -q -p no:cacheprovider "$test" >/dev/null 2>&1 || die 7 "local gate failed: $test"
  echo "s3_merge: gate ok: $test"
done

cat <<EOT
s3_merge: done. A=$A B=$B C=$C (work branch $work)
Next (merge captain), on the buildbox (these gates start test servers, never on the laptop):
  Server gates: /var/tmp/clarp-refactor-program/bin/heavy-remote uv run --frozen --group dev python -m pytest -n 0 -q \
                  tests/integration/test_route_registry.py tests/integration/test_route_response_goldens.py
  G1: /var/tmp/clarp-refactor-program/bin/heavy-remote uv run --frozen --group dev python -m pytest -q
  G2 (merge parity, lead condition 2: main against the merged integration, zero differences):
      python3 tests/replay/run.py prepare $main $C --prune
      /var/tmp/clarp-refactor-program/bin/heavy-remote uv run --frozen --group dev python tests/replay/run.py --jobs 8 compare --base $main --head $C
  This does not replace a slice's own G2 against its refactor/integration base.
EOT

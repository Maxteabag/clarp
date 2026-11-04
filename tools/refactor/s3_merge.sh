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
#   C  s3_apply --group <name>, one commit per group in recipe order ("s3_apply: group <name>"),
#      each verified: ast_identity <previous> <this> server.py::Handler=<that group's mixin>
#   D  S9 module splits (tools/refactor/splits/*.toml, owner rf-fix3; S7 entries rf-fix2):
#      the old modules were taken from main in A and the split parts removed; one commit
#      per split ("split_apply: <name>") and per repoint ("split_apply: repoint <name>"),
#      each verified: ast_identity <previous> <this> <old>=<old>,<part>,... (top-level mode)
# then the local gates. It prints the G1 and G2 commands for the merge captain;
# G2 compares main against D and must show zero differences. With no recipe in
# tools/refactor/splits/ stage D does nothing and D is C.
#
# Exit codes: 0 done (gates green; run G1/G2 next), 2 usage or dirty tree,
# 3 a merge conflict outside server.py and the generated files, 4 a replayed diff
# does not apply, 5 s3_apply refused, 6 ast_identity failed, 7 a local gate failed,
# 8 a split refused or a split module was edited on integration (rf-fix3 / rf-fix2),
# 9 ast_identity failed for a split or repoint (rf-fix3 / rf-fix2).
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
git checkout "$main" -- tools/refactor/splits tools/refactor/split_apply.py 2>/dev/null || true
# S9: one line per split or repoint, "split|repoint NAME OLD=OLD,PART,...", in apply order.
split_specs=""
if [ -f tools/refactor/split_apply.py ] && [ -d tools/refactor/splits ]; then
  split_specs=$("$PY" tools/refactor/split_apply.py --root "$root" --list) || die 8 "cannot read tools/refactor/splits/"
fi
split_old=$(printf '%s\n' "$split_specs" | awk 'NF {split($3, m, "="); print "server/" m[1]}' | sort -u)
split_generated=$(printf '%s\n' "$split_specs" | awk '$1 == "split" {split($3, m, "="); n = split(m[2], p, ","); for (i = 2; i <= n; i++) print "server/" p[i]}')
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
# One line per group in recipe order: name, server/<module>, <module>::<class>.
group_specs=$("$PY" - "$root/tools/refactor/s3_moves.toml" <<'PYEOF'
import sys, tomllib
data = tomllib.loads(open(sys.argv[1]).read())
for g in data.get("group", []):
    print(g["name"], "server/" + g["module"], f"{g['module']}::{g['class']}")
PYEOF
) || die 2 "cannot read the groups of tools/refactor/s3_moves.toml"
replay=$(grep -v '^\s*#' tools/refactor/s3_replay.txt | awk 'NF {print $1}')

for path in $conflicts; do
  case "$path" in
    server/server.py) ;;
    *) printf '%s\n' "$generated" $split_old $split_generated | grep -qx "$path" \
         || die 3 "conflict outside server.py and the generated files: $path" ;;
  esac
done

# S9: integration may change a split module only through its recipe. Regenerating the
# split from the merge base's text must give exactly integration's files; anything else
# is an integration-only edit that taking main's text below would silently drop.
if [ -n "$split_specs" ]; then
  base=$(git merge-base "$main" "$integration") || die 8 "no merge base"
  "$PY" - "$root" "$base" "$integration" <<'PYEOF' || die 8 "a split module differs on integration from its recipe applied to the merge base (see above): port the edit to main or into the recipe"
import pathlib, subprocess, sys, tempfile
root, base, integration = sys.argv[1:4]
sys.path.insert(0, f"{root}/tools/refactor")
import split_apply
def show(ref, rel):
    out = subprocess.run(["git", "-C", root, "show", f"{ref}:{rel}"], capture_output=True, text=True)
    return out.stdout if out.returncode == 0 else None
splits, repoints = split_apply.load_recipes(pathlib.Path(root, "tools/refactor/splits"))
bad = []
with tempfile.TemporaryDirectory() as tmp:
    for entry in splits:
        rels = ["server/" + entry["module"]] + ["server/" + p["module"] for p in entry["part"]]
        if show(integration, rels[1]) is None:
            continue   # not applied on integration yet: nothing to lose
        work = pathlib.Path(tmp, entry["name"])
        (work / "server" / entry["module"]).parent.mkdir(parents=True, exist_ok=True)
        (work / "server" / entry["module"]).write_text(show(base, rels[0]) or "")
        expected = split_apply.apply_split(work, entry) if show(base, rels[1]) is None else {}
        for rel in rels:
            want = expected.get(rel, show(base, rel))
            if want != show(integration, rel):
                bad.append(f"{rel} (split {entry['name']})")
    for entry in repoints:
        rel = "server/" + entry["file"]
        work = pathlib.Path(tmp, "repoint-" + entry["name"])
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_text(show(base, rel) or "")
        try:
            repointed = split_apply.apply_repoint(work, entry).get(rel, show(base, rel))
        except split_apply.Refusal:
            repointed = None
        if show(integration, rel) not in (show(base, rel), repointed):
            bad.append(f"{rel} (repoint {entry['name']})")
print("\n".join("s3_merge: integration-only edit in " + b for b in bad), file=sys.stderr)
sys.exit(1 if bad else 0)
PYEOF
fi

# A: main's server.py + the replayed S3 diffs; generated files removed.
git checkout "$main" -- server/server.py || die 4 "cannot take main's server.py"
for path in $generated; do git rm -q --cached --ignore-unmatch "$path"; rm -f "$path"; done
for path in $split_old; do git checkout "$main" -- "$path" || die 8 "cannot take main's $path"; done
for path in $split_generated; do git rm -q --cached --ignore-unmatch "$path"; rm -f "$path"; done
for sha in $replay; do
  git diff "$sha^" "$sha" -- server/server.py | git apply --3way --index - \
    || die 4 "the server.py diff of $sha does not apply to main's server.py; port it by hand and update s3_replay.txt"
done
git add server/server.py $split_old
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

# C: one commit per group, in recipe order, so each move-only commit stays the
# size of its group and is verified on its own.
C=$B
while read -r name path mixin; do
  [ -n "$name" ] || continue
  "$PY" tools/refactor/s3_apply.py --root "$root" --group "$name" </dev/null || die 5 "s3_apply --group $name refused"
  stage_status=$(git status --porcelain --untracked-files=all -- server/server.py "$path") || die 5 "git status failed"
  [ -n "$stage_status" ] || continue
  git add -A -- server/server.py "$path" && git -c core.hooksPath=/dev/null commit -q -m "s3_apply: group $name" \
    || die 5 "cannot commit group $name"
  prev=$C
  C=$(git rev-parse HEAD)
  "$PY" scripts/refactor/ast_identity.py "$prev" "$C" "server.py::Handler=$mixin" </dev/null \
    || die 6 "ast_identity (method mode) failed for group $name"
done <<< "$group_specs"

# D: S9 module splits, one commit per split or repoint, each verified on its own.
D=$C
while read -r kind name move; do
  [ -n "$kind" ] || continue
  old=${move%%=*}
  parts=$(printf '%s' "${move#*=}" | tr ',' ' ')
  "$PY" tools/refactor/split_apply.py --root "$root" "--$kind" "$name" </dev/null || die 8 "split_apply refused $kind $name"
  paths=$(for p in $parts; do printf 'server/%s ' "$p"; done)
  stage_status=$(git status --porcelain --untracked-files=all -- $paths) || die 8 "git status failed"
  [ -n "$stage_status" ] || continue
  label=$([ "$kind" = split ] && echo "$name" || echo "repoint $name")
  git add -A -- $paths && git -c core.hooksPath=/dev/null commit -q -m "split_apply: $label" \
    || die 8 "cannot commit $kind $name"
  prev=$D
  D=$(git rev-parse HEAD)
  "$PY" scripts/refactor/ast_identity.py "$prev" "$D" "$move" </dev/null \
    || die 9 "ast_identity (top-level) failed for $kind $name"
  echo "s3_merge: $kind $name: ast_identity $move ok"
done <<< "$split_specs"

stray=$(git status --porcelain --untracked-files=all -- server tools scripts) || die 5 "git status failed"
stray=$(printf '%s\n' "$stray" | grep -v -e ' server/server.py$' || true)
for path in $generated $split_old $split_generated; do stray=$(printf '%s\n' "$stray" | grep -v " $path$" || true); done
[ -z "$(printf '%s' "$stray" | tr -d '[:space:]')" ] || die 5 "s3_apply left changes outside server.py and the generated files:
$stray"
git merge-base --is-ancestor "$main" HEAD || die 3 "the result does not contain $main"

# Local gates: in-process tests only (one file each; no test servers or subprocesses
# on the laptop; test_s3_apply.py runs git, so it is a buildbox gate below).
heavy=/var/tmp/clarp-refactor-program/bin/heavy
for test in tests/integration/test_route_goldens.py tests/unit/test_import_layers.py \
            tests/unit/test_handler_boundary.py tests/integration/test_s3_helpers_identity.py \
            tests/unit/test_swallowed_excepts.py tests/unit/test_split_apply.py; do
  [ -f "$test" ] || continue
  "$heavy" "$PY" -m pytest -n 0 -q -p no:cacheprovider "$test" >/dev/null 2>&1 || die 7 "local gate failed: $test"
  echo "s3_merge: gate ok: $test"
done

cat <<EOT
s3_merge: done. A=$A B=$B C=$C D=$D (work branch $work)
Next (merge captain), on the buildbox (these gates start test servers, never on the laptop):
  Server gates: /var/tmp/clarp-refactor-program/bin/heavy-remote uv run --frozen --group dev python -m pytest -n 0 -q \
                  tests/integration/test_route_registry.py tests/integration/test_route_response_goldens.py \
                  tests/unit/test_s3_apply.py
  G1: /var/tmp/clarp-refactor-program/bin/heavy-remote uv run --frozen --group dev python -m pytest -q
  G2 (merge parity, lead condition 2: main against the merged integration, zero differences):
      python3 tests/replay/run.py prepare $main $D --prune
      /var/tmp/clarp-refactor-program/bin/heavy-remote uv run --frozen --group dev python tests/replay/run.py --jobs 8 compare --base $main --head $D
  This does not replace a slice's own G2 against its refactor/integration base.
EOT

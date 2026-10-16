#!/usr/bin/env bash
# Run one CI test command on a GitHub-hosted Linux runner with no network (loopback only), no
# credentials, no privilege and only its own processes. Dependencies must already be installed:
# nothing here fetches anything.
#
#   bash scripts/ci_contained.sh <command> [args...]
#
# The runner's passwordless sudo is used ONLY to create the namespaces. The command then runs as the
# runner user with no_new_privs, no supplementary groups and no capabilities, so sudo (or any setuid
# helper) cannot re-elevate. The runner user's home (runner install, _temp, caches, tokens on disk)
# is hidden except the workspace and the venv's interpreter, /tmp and /var/tmp are fresh, the Docker
# sockets are masked, and a self-check proves all of that before the command starts.
set -euo pipefail

for tool in sudo unshare setpriv ip mount; do
  command -v "$tool" >/dev/null || { echo "ci_contained: missing $tool" >&2; exit 2; }
done

work=$(pwd -P)
uid=$(id -u)
gid=$(id -g)
runner_home=$(cd ~ && pwd -P)
python_bin=$(readlink -f "$work/.venv/bin/python")
python_root=$(dirname "$(dirname "$python_bin")")       # the interpreter the venv resolves to
# pyvenv.cfg `home` names the interpreter through uv's minor-version alias symlink
# (cpython-3.12-… -> cpython-3.12.14-…), which must survive the tmpfs over the runner home too.
python_alias=$(dirname "$(sed -n 's/^home *= *//p' "$work/.venv/pyvenv.cfg")")
node_dir=$(dirname "$(readlink -f "$(command -v node)")")
runner_temp=${RUNNER_TEMP:-}
fresh_home=/ci-home                                     # created inside the namespace, empty

inner=$(cat <<'INNER'
set -euo pipefail
work=$1 uid=$2 gid=$3 runner_home=$4 python_root=$5 node_dir=$6 runner_temp=$7 fresh_home=$8 python_alias=$9
shift 9
ip link set lo up
for sock in /var/run/docker.sock /run/docker.sock; do
  if [ -S "$sock" ]; then mount --bind /dev/null "$sock"; fi
done
# Keep the workspace and the interpreter, hide the rest of the runner user's home.
under_home() { case "$1" in "$runner_home"/*) return 0 ;; *) return 1 ;; esac; }
stage=$(mktemp -d /tmp/ci-stage.XXXXXX)
mkdir -p "$stage/work"
mount --rbind "$work" "$stage/work"
# The interpreter (and the alias pyvenv.cfg names) are rebound only when they live in the runner
# home; a system interpreter (/usr) is already visible and is never rebound.
if under_home "$python_root"; then mkdir -p "$stage/python"; mount --rbind "$python_root" "$stage/python"; fi
alias_link=""
if under_home "$python_alias" && [ -L "$python_alias" ]; then alias_link=$(readlink "$python_alias"); fi
mount -t tmpfs -o mode=0755 tmpfs "$runner_home"
mkdir -p "$work"
mount --rbind "$stage/work" "$work"
if under_home "$python_root"; then mkdir -p "$python_root"; mount --rbind "$stage/python" "$python_root"; fi
if [ -n "$alias_link" ]; then mkdir -p "$(dirname "$python_alias")"; ln -s "$alias_link" "$python_alias"; fi
if [ -n "$runner_temp" ] && [ -d "$runner_temp" ]; then mount -t tmpfs tmpfs "$runner_temp"; fi
mount -t tmpfs -o mode=1777 tmpfs /tmp
mount -t tmpfs -o mode=1777 tmpfs /var/tmp
mkdir -p "$fresh_home" && chown "$uid:$gid" "$fresh_home" && chmod 0700 "$fresh_home"
cd "$work"
exec setpriv --reuid="$uid" --regid="$gid" --clear-groups --no-new-privs \
  --inh-caps=-all --ambient-caps=-all --bounding-set=-all -- \
  env -i HOME="$fresh_home" PATH="$work/.venv/bin:$node_dir:/usr/bin:/bin" LANG=C.UTF-8 CI=true \
    VIRTUAL_ENV="$work/.venv" UV_OFFLINE=1 NPM_CONFIG_OFFLINE=true \
  bash -c 'python scripts/ci_containment_selfcheck.py "$@" && shift 3 && exec "$@"' selfcheck \
    "$runner_home" "$runner_temp" "$work" "$@"
INNER
)

exec sudo unshare --net --mount --pid --fork --mount-proc -- bash -c "$inner" ci-contained \
  "$work" "$uid" "$gid" "$runner_home" "$python_root" "$node_dir" "$runner_temp" "$fresh_home" "$python_alias" "$@"

#!/usr/bin/env bash
# Install Clarp onto a fresh machine.
umask 077
# Idempotent — safe to re-run after pulling updates.
#
# Env vars you can pass:
#   UV                  — uv executable. Defaults to `uv` on PATH.
#   CLARP_SKIP_ENV      — test/developer escape hatch; use PYTHON as-is.
#   CLARP_TOOLCHAIN_MODE — managed, existing, or none. Defaults to the recorded
#                          installation choice, then existing on a fresh install.
set -euo pipefail

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

HOST_PLATFORM="$(uname -s)"
PLATFORM="$HOST_PLATFORM"
[[ "${CLARP_PLATFORM_OVERRIDE:-}" != "macos" ]] || PLATFORM="Darwin"
[[ "${CLARP_PLATFORM_OVERRIDE:-}" != "linux" ]] || PLATFORM="Linux"
if [[ "$PLATFORM" == "Darwin" ]]; then
    if [[ "$HOST_PLATFORM" == "Darwin" ]]; then
        MACOS_MAJOR="$(sw_vers -productVersion | cut -d. -f1)"
        if [[ ! "$MACOS_MAJOR" =~ ^[0-9]+$ || "$MACOS_MAJOR" -lt 14 ]]; then
            echo "ERROR: Clarp requires macOS 14 (Sonoma) or newer." >&2
            exit 1
        fi
    fi
    CFG_DIR="${CLARP_CONFIG_DIR:-$HOME/Library/Application Support/Clarp}"
    SHARE="${CLARP_SHARE_DIR:-$HOME/Library/Application Support/Clarp}"
    CACHE_DIR="${CLARP_CACHE_DIR:-$HOME/Library/Caches/Clarp}"
    SERVICE_FILE="$HOME/Library/LaunchAgents/com.maxteabag.clarp.server.plist"
else
    XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
    XDG_DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
    XDG_CACHE_HOME="${XDG_CACHE_HOME:-$HOME/.cache}"
    CFG_DIR="${CLARP_CONFIG_DIR:-$XDG_CONFIG_HOME/clarp}"
    SHARE="${CLARP_SHARE_DIR:-$XDG_DATA_HOME/clarp}"
    CACHE_DIR="${CLARP_CACHE_DIR:-$XDG_CACHE_HOME/clarp}"
    SERVICE_FILE="$XDG_CONFIG_HOME/systemd/user/clarp.service"
fi
RELEASES="$SHARE/releases"
BIN="$HOME/.local/bin"
SKILLS="$HOME/.claude/skills"
PYTHON="${PYTHON:-$(command -v python3 || true)}"
UV="${UV:-$(command -v uv || true)}"
TOOLCHAIN_MODE="${CLARP_TOOLCHAIN_MODE:-}"
if [[ -z "$TOOLCHAIN_MODE" && -f "$CFG_DIR/install.json" && -n "$PYTHON" ]]; then
    TOOLCHAIN_MODE="$("$PYTHON" - "$CFG_DIR/install.json" <<'PY'
import json, pathlib, sys
try:
    value = json.loads(pathlib.Path(sys.argv[1]).read_text()).get("toolchain", "")
except (OSError, json.JSONDecodeError):
    value = ""
print(value if value in {"managed", "existing", "none"} else "")
PY
)"
fi
if [[ -z "$TOOLCHAIN_MODE" && -f "$SHARE/current/TOOLCHAIN_MODE" ]]; then
    _recorded_toolchain="$(sed -n '1p' "$SHARE/current/TOOLCHAIN_MODE")"
    if [[ "$_recorded_toolchain" == "managed" || \
          "$_recorded_toolchain" == "existing" || \
          "$_recorded_toolchain" == "none" ]]; then
        TOOLCHAIN_MODE="$_recorded_toolchain"
    fi
fi
TOOLCHAIN_MODE="${TOOLCHAIN_MODE:-existing}"
export CLARP_SHARE_DIR="$SHARE" CLARP_CONFIG_DIR="$CFG_DIR" \
       CLARP_CACHE_DIR="$CACHE_DIR"

sha256_file() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        echo "ERROR: sha256sum or shasum is required" >&2
        return 1
    fi
}

# Installation is intentionally a clean product boundary: it never discovers
# or mutates predecessor product directories. Importing data from another
# product requires a separate, explicit tool rather than hidden install-time
# filesystem moves.
echo ">> creating directories"
mkdir -p "$SHARE" "$RELEASES" "$BIN" "$(dirname "$SERVICE_FILE")" "$SKILLS" \
         "$CACHE_DIR/audio" "$CFG_DIR"
chmod 700 "$SHARE" "$CACHE_DIR" "$CACHE_DIR/audio" "$CFG_DIR"
for _d in "$CACHE_DIR/audio/hls" "$CACHE_DIR/logs"; do
    [[ ! -d "$_d" ]] || { chmod 700 "$_d"; chmod -R go-rwx "$_d"; }
done
chmod 600 "$SHARE"/state.sqlite* 2>/dev/null || true

if [[ ! -f "$REPO_DIR/static/app/bundle.js" ]]; then
    echo ">> building PWA"
    command -v npm >/dev/null 2>&1 || {
        echo "ERROR: npm is required to build the web UI." >&2
        exit 1
    }
    (cd "$REPO_DIR" && npm ci --ignore-scripts --no-audit --no-fund && npm run build)
fi

echo ">> staging versioned Clarp release"
STAGE="$(mktemp -d "$RELEASES/.install.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
cp "$REPO_DIR/server/server.py" "$STAGE/server.py"
cp -r "$REPO_DIR/server/lib" "$STAGE/lib"
cp -r "$REPO_DIR/static" "$STAGE/static"
mkdir -p "$STAGE/scripts" "$STAGE/bin"
cp "$REPO_DIR/scripts/agent_bg.py" "$STAGE/scripts/agent_bg.py"
cp "$REPO_DIR/scripts/server_update_job.py" "$STAGE/scripts/server_update_job.py"
cp "$REPO_DIR/scripts/transcription_model_job.py" "$STAGE/scripts/transcription_model_job.py"
cp "$REPO_DIR/scripts/portrait_generation_job.py" "$STAGE/scripts/portrait_generation_job.py"
cp "$REPO_DIR/scripts/agent_tasks.py" "$STAGE/scripts/agent_tasks.py"
cp "$REPO_DIR/scripts/agent_artifacts.py" "$STAGE/scripts/agent_artifacts.py"
cp "$REPO_DIR/scripts/github_workflow_artifact.py" "$STAGE/scripts/github_workflow_artifact.py"
cp "$REPO_DIR/scripts/live_console.py" "$STAGE/scripts/live_console.py"
cp "$REPO_DIR/scripts/leader_decision.py" "$STAGE/scripts/leader_decision.py"
cp "$REPO_DIR/scripts/clarp-media-publish.py" "$STAGE/scripts/clarp-media-publish.py"
cp "$REPO_DIR/scripts/install_agent_toolchain.py" "$STAGE/scripts/install_agent_toolchain.py"
cp "$REPO_DIR/bin/clarp-admin.py" "$STAGE/bin/clarp-admin.py"
cp "$REPO_DIR/bin/clarp-tui.py" "$STAGE/bin/clarp-tui.py"
cp -R "$REPO_DIR/plugin" "$STAGE/plugin"
cp -R "$REPO_DIR/systemd" "$STAGE/systemd"
cp "$REPO_DIR/requirements.txt" "$STAGE/requirements.txt"
cp "$REPO_DIR/pyproject.toml" "$STAGE/pyproject.toml"
cp "$REPO_DIR/uv.lock" "$STAGE/uv.lock"
mkdir -p "$STAGE/toolchain"
cp "$REPO_DIR/toolchain/README.md" \
   "$REPO_DIR/toolchain/package.json" \
   "$REPO_DIR/toolchain/toolchain.json" \
   "$STAGE/toolchain/"
# Only the managed toolchain reads the lockfile (its hash below), and a
# missing one must not block an existing/none install (issue #8).
if [[ -f "$REPO_DIR/toolchain/package-lock.json" ]]; then
    cp "$REPO_DIR/toolchain/package-lock.json" "$STAGE/toolchain/"
elif [[ "$TOOLCHAIN_MODE" == "managed" ]]; then
    echo "ERROR: toolchain/package-lock.json is required for the managed toolchain" >&2
    exit 1
fi
cp "$REPO_DIR/LICENSE.md" "$STAGE/LICENSE.md"
cp "$REPO_DIR/COMMERCIAL_LICENSE.md" "$STAGE/COMMERCIAL_LICENSE.md"
SOURCE_REMOTE="${CLARP_SOURCE_REMOTE:-$(git -C "$REPO_DIR" remote get-url origin 2>/dev/null || true)}"
if [[ -z "$SOURCE_REMOTE" ]]; then
    SOURCE_REMOTE="https://github.com/Maxteabag/clarp.git"
fi
case "$SOURCE_REMOTE" in
    http://*@*|https://*@*)
        echo "   WARNING: origin URL contains userinfo; omitting it from release metadata" >&2
        SOURCE_REMOTE=""
        ;;
esac
case "$SOURCE_REMOTE" in
    http://*|https://*)
        SOURCE_REMOTE="${SOURCE_REMOTE%%\?*}"
        SOURCE_REMOTE="${SOURCE_REMOTE%%\#*}"
        ;;
esac
printf '%s\n' "$SOURCE_REMOTE" > "$STAGE/SOURCE_REMOTE"
mkdir -p "$STAGE/docs"
cp "$REPO_DIR/docs/user-values.example.md" "$STAGE/docs/user-values.example.md"
if [[ -d "$REPO_DIR/skills" ]]; then
    cp -R "$REPO_DIR/skills" "$STAGE/skills"
fi
DEPLOYED_VERSION="$(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
if ! git -C "$REPO_DIR" diff --quiet HEAD -- 2>/dev/null; then
    DEPLOYED_VERSION="${DEPLOYED_VERSION}-dirty"
fi
printf '%s\n' "$DEPLOYED_VERSION" > "$STAGE/DEPLOYED_VERSION"
RELEASE_ID="${DEPLOYED_VERSION}-$(date +%Y%m%d%H%M%S)-$$"
printf '%s\n' "$RELEASE_ID" > "$STAGE/DEPLOYED_RELEASE_ID"
RELEASE="$RELEASES/$RELEASE_ID"
mv "$STAGE" "$RELEASE"
trap - EXIT

if [[ "${CLARP_SKIP_ENV:-0}" != "1" ]]; then
    [[ -n "$UV" ]] || {
        echo "ERROR: uv is required. Install uv and rerun setup.sh." >&2
        exit 1
    }
    "$UV" lock --check --project "$REPO_DIR" >/dev/null
    ENV_ID="$(sha256_file "$REPO_DIR/uv.lock")"
    ENV_ID="${ENV_ID:0:20}"
    ENV_DIR="$SHARE/environments/$ENV_ID"
    echo ">> syncing locked Python environment $ENV_ID"
    UV_PROJECT_ENVIRONMENT="$ENV_DIR" "$UV" sync --frozen --no-dev \
        --project "$REPO_DIR"
    PYTHON="$ENV_DIR/bin/python"
elif [[ -z "$PYTHON" ]]; then
    echo "ERROR: PYTHON is required when CLARP_SKIP_ENV=1" >&2
    exit 1
fi

if [[ "$TOOLCHAIN_MODE" == "managed" ]]; then
    TOOLCHAIN_ID="$("$PYTHON" - "$RELEASE/toolchain" \
        "$RELEASE/scripts/install_agent_toolchain.py" <<'PY'
import hashlib, pathlib, sys
root = pathlib.Path(sys.argv[1])
digest = hashlib.sha256()
for name in ("toolchain.json", "package.json", "package-lock.json"):
    digest.update((root / name).read_bytes())
digest.update(pathlib.Path(sys.argv[2]).read_bytes())
print(digest.hexdigest()[:20])
PY
)"
    TOOLCHAIN_DIR="$SHARE/toolchains/$TOOLCHAIN_ID"
    "$PYTHON" "$RELEASE/scripts/install_agent_toolchain.py" \
        --root "$TOOLCHAIN_DIR" --source "$RELEASE/toolchain"
    printf '%s\n' "$TOOLCHAIN_DIR" > "$RELEASE/TOOLCHAIN_DIR"
elif [[ "$TOOLCHAIN_MODE" != "existing" && "$TOOLCHAIN_MODE" != "none" ]]; then
    echo "ERROR: CLARP_TOOLCHAIN_MODE must be managed, existing, or none" >&2
    exit 1
fi
printf '%s\n' "$TOOLCHAIN_MODE" > "$RELEASE/TOOLCHAIN_MODE"

PREVIOUS_CURRENT=""
PREVIOUS_TOOLCHAIN=""
if [[ -L "$SHARE/current" ]]; then
    PREVIOUS_CURRENT="$("$PYTHON" -c \
        'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve())' \
        "$SHARE/current" 2>/dev/null || true)"
fi
if [[ -L "$SHARE/toolchain" ]]; then
    PREVIOUS_TOOLCHAIN="$("$PYTHON" -c \
        'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve())' \
        "$SHARE/toolchain" 2>/dev/null || true)"
fi
ACTIVATED=0
UNIT_BACKUP="$SHARE/.clarp.service.previous"
UNIT_HAD_PREVIOUS=0
EXTERNAL_BACKUP="$SHARE/.external-backup.$$"
EXTERNAL_MANIFEST="$EXTERNAL_BACKUP/manifest"
mkdir -p "$EXTERNAL_BACKUP"
: > "$EXTERNAL_MANIFEST"
backup_external() {
    _path="$1"
    _key="$(wc -l < "$EXTERNAL_MANIFEST" | tr -d ' ')"
    if [[ -e "$_path" || -L "$_path" ]]; then
        "$PYTHON" - "$_path" "$EXTERNAL_BACKUP/$_key" <<'PY'
import os, pathlib, shutil, sys
source, target = map(pathlib.Path, sys.argv[1:])
if source.is_symlink():
    target.symlink_to(os.readlink(source), target_is_directory=source.is_dir())
elif source.is_dir():
    shutil.copytree(source, target, symlinks=True)
else:
    shutil.copy2(source, target)
PY
        printf 'present|%s|%s\n' "$_path" "$_key" >> "$EXTERNAL_MANIFEST"
    else
        printf 'missing|%s|%s\n' "$_path" "$_key" >> "$EXTERNAL_MANIFEST"
    fi
}
restore_external() {
    while IFS='|' read -r _state _path _key; do
        [[ -n "$_path" ]] || continue
        rm -rf "$_path"
        if [[ "$_state" == "present" ]]; then
            mkdir -p "$(dirname "$_path")"
            "$PYTHON" - "$EXTERNAL_BACKUP/$_key" "$_path" <<'PY'
import os, pathlib, shutil, sys
source, target = map(pathlib.Path, sys.argv[1:])
if source.is_symlink():
    target.symlink_to(os.readlink(source), target_is_directory=source.is_dir())
elif source.is_dir():
    shutil.copytree(source, target, symlinks=True)
else:
    shutil.copy2(source, target)
PY
        fi
    done < "$EXTERNAL_MANIFEST"
}
backup_external "$BIN/clarp-admin"
backup_external "$BIN/clarp-agent-tasks"
backup_external "$BIN/clarp-agent-artifacts"
backup_external "$BIN/clarp-media-publish"
backup_external "$BIN/clarp-agent-bg"
backup_external "$BIN/clarp-github-workflow-artifact"
backup_external "$BIN/clarp-message-watch"
backup_external "$HOME/.claude/settings.json"
backup_external "$CFG_DIR/user-values.md"
rm -f "$UNIT_BACKUP"
if [[ -f "$SERVICE_FILE" ]]; then
    cp "$SERVICE_FILE" "$UNIT_BACKUP"
    UNIT_HAD_PREVIOUS=1
fi
rollback_failed_activation() {
    _rc=$?
    trap - EXIT
    if [[ $_rc -ne 0 && $ACTIVATED -eq 1 ]]; then
        echo "!! install failed after activation; restoring previous release" >&2
        printf '%s\n' "activation failed with exit $_rc" > "$RELEASE/INSTALL_FAILED"
        rm -f "$SHARE/.current.rollback"
        if [[ -n "$PREVIOUS_CURRENT" && -d "$PREVIOUS_CURRENT" ]]; then
            ln -s "$PREVIOUS_CURRENT" "$SHARE/.current.rollback"
            "$PYTHON" -c 'import os,sys; os.replace(sys.argv[1],sys.argv[2])' \
                "$SHARE/.current.rollback" "$SHARE/current"
        else
            rm -f "$SHARE/current"
            for _name in server.py lib static scripts skills plugin systemd \
                         docs requirements.txt pyproject.toml uv.lock toolchain \
                         DEPLOYED_VERSION DEPLOYED_RELEASE_ID SOURCE_REMOTE; do
                rm -rf "$SHARE/$_name"
            done
        fi
        rm -f "$SHARE/.toolchain.rollback"
        if [[ -n "$PREVIOUS_TOOLCHAIN" && -d "$PREVIOUS_TOOLCHAIN" ]]; then
            ln -s "$PREVIOUS_TOOLCHAIN" "$SHARE/.toolchain.rollback"
            "$PYTHON" -c 'import os,sys; os.replace(sys.argv[1],sys.argv[2])' \
                "$SHARE/.toolchain.rollback" "$SHARE/toolchain"
        elif [[ "$TOOLCHAIN_MODE" == "managed" ]]; then
            rm -f "$SHARE/toolchain"
        fi
        if [[ $UNIT_HAD_PREVIOUS -eq 1 && -f "$UNIT_BACKUP" ]]; then
            cp "$UNIT_BACKUP" "$SERVICE_FILE"
        else
            rm -f "$SERVICE_FILE"
        fi
        PYTHONPATH="$RELEASE" "$PYTHON" -c \
            'import sys; from lib import service_manager; service_manager.restore_after_failed_install(had_previous=bool(int(sys.argv[1])))' \
            "$UNIT_HAD_PREVIOUS" >/dev/null 2>&1 || true
        restore_external
    fi
    exit $_rc
}
trap rollback_failed_activation EXIT

# Atomic release activation. Compatibility links keep the existing systemd
# unit and helper paths working while making rollback a single symlink switch.
rm -f "$SHARE/.current.next"
ln -s "$RELEASE" "$SHARE/.current.next"
"$PYTHON" -c 'import os,sys; os.replace(sys.argv[1],sys.argv[2])' \
    "$SHARE/.current.next" "$SHARE/current"
ACTIVATED=1
for _name in server.py lib static scripts skills plugin systemd docs \
             requirements.txt pyproject.toml uv.lock toolchain \
             DEPLOYED_VERSION DEPLOYED_RELEASE_ID SOURCE_REMOTE; do
    _target="$SHARE/current/$_name"
    [[ -e "$_target" ]] || continue
    ln -sfn "$_target" "$SHARE/$_name"
done

echo ">> installing bin scripts"
chmod 700 "$SHARE/current/bin/clarp-admin.py" "$SHARE/current/bin/clarp-tui.py"
chmod 700 "$SHARE/current/scripts/agent_tasks.py" \
          "$SHARE/current/scripts/agent_artifacts.py" \
          "$SHARE/current/scripts/clarp-media-publish.py" \
          "$SHARE/current/scripts/agent_bg.py" \
          "$SHARE/current/scripts/github_workflow_artifact.py"
chmod 700 "$SHARE/current/skills/clarp-message-watch/scripts/watch_messages.py"
write_python_wrapper() {
    "$PYTHON" - "$BIN/$1" "$SHARE" "$CFG_DIR" "$CACHE_DIR" \
        "$RELEASE" "$2" <<'PY'
import pathlib, shlex, sys
destination = pathlib.Path(sys.argv[1])
share = pathlib.Path(sys.argv[2])
config = pathlib.Path(sys.argv[3])
cache = pathlib.Path(sys.argv[4])
fallback_release = pathlib.Path(sys.argv[5])
relative = sys.argv[6]
temporary = destination.with_name(f".{destination.name}.next")
temporary.write_text(
    "#!/bin/sh\n"
    "# managed-by-clarp\n"
    f"export CLARP_SHARE_DIR={shlex.quote(str(share))}\n"
    f"export CLARP_CONFIG_DIR={shlex.quote(str(config))}\n"
    f"export CLARP_CACHE_DIR={shlex.quote(str(cache))}\n"
    f"export CLAUDE_PWA_CONFIG={shlex.quote(str(config / 'config.toml'))}\n"
    f"export CLAUDE_PWA_DB={shlex.quote(str(share / 'state.sqlite'))}\n"
    f"active={shlex.quote(str(share / 'current'))}\n"
    f"fallback={shlex.quote(str(fallback_release))}\n"
    f"relative={shlex.quote(relative)}\n"
    'if [ -r "$active/SERVICE_PYTHON" ] && [ -f "$active/$relative" ]; then\n'
    '  code_root=$active\n'
    'else\n'
    '  code_root=$fallback\n'
    'fi\n'
    'export CLARP_CODE_ROOT="$code_root"\n'
    'runtime=$(sed -n \'1p\' "$code_root/SERVICE_PYTHON")\n'
    'script="$code_root/$relative"\n'
    'exec "$runtime" "$script" "$@"\n'
)
temporary.chmod(0o700)
temporary.replace(destination)
PY
}
write_python_wrapper clarp-admin bin/clarp-admin.py
write_python_wrapper clarp-tui bin/clarp-tui.py
write_python_wrapper clarp-agent-tasks scripts/agent_tasks.py
write_python_wrapper clarp-agent-artifacts scripts/agent_artifacts.py
write_python_wrapper clarp-media-publish scripts/clarp-media-publish.py
write_python_wrapper clarp-agent-bg scripts/agent_bg.py
write_python_wrapper clarp-github-workflow-artifact scripts/github_workflow_artifact.py
write_python_wrapper clarp-message-watch skills/clarp-message-watch/scripts/watch_messages.py
mkdir -p "$SHARE/bin"
for _helper in clarp-admin clarp-tui clarp-agent-tasks clarp-agent-artifacts \
        clarp-media-publish clarp-agent-bg clarp-github-workflow-artifact \
        clarp-message-watch; do
    cp "$BIN/$_helper" "$SHARE/bin/$_helper"
done
chmod 700 "$SHARE/bin" "$SHARE/bin"/*

# Helper scripts agents call at runtime (e.g. agent_bg.py to set a visible
# background status). Installed into the share dir so the per-turn instruction
# can reference a stable path regardless of where the repo lives.
mkdir -p "$SHARE/current/scripts"

# HLS remains available as the conservative full-clip delivery, but the default
# chunked-file path does not require ffmpeg. Warn only when this install is
# configured to use HLS without ffmpeg on PATH.
if ! command -v ffmpeg >/dev/null 2>&1; then
    if grep -Eq '^[[:space:]]*delivery[[:space:]]*=[[:space:]]*"hls"' "$CFG_DIR/config.toml" 2>/dev/null; then
        echo "   WARNING: ffmpeg not found on PATH but [audio] delivery = \"hls\""
        echo "            requires it. Install ffmpeg or switch delivery to"
        echo "            \"chunked-file\" in $CFG_DIR/config.toml."
    fi
fi


# ---- config.toml ---------------------------------------------------------
# First install: copy the example so the user has somewhere to edit. Don't
# touch an existing config — that would silently revert their customisations.
if [[ ! -f "$CFG_DIR/config.toml" ]]; then
    echo ">> writing default $CFG_DIR/config.toml from example"
    cp "$REPO_DIR/config.example.toml" "$CFG_DIR/config.toml"
else
    echo ">> $CFG_DIR/config.toml already exists, leaving it alone"
fi
chmod 600 "$CFG_DIR/config.toml"

# ---- service PATH --------------------------------------------------------
# systemd user services inherit only a bare PATH (e.g. /usr/local/bin:/usr/bin).
# The server shells out to the configured agent CLIs, which usually live in a
# user prefix (~/.local/bin) or a version-manager shim (mise/npm) that the bare
# PATH can't see — so /send 500s at runtime. Build the service PATH from where
# those tools actually resolve right now, plus common prefixes for tools a user
# might install *after* this script runs. Unknown/missing dirs in PATH are
# harmless. `clarp` is discovered only for users who explicitly select that
# optional Claude-compatible provider.
#
# Note: we use the dir of the symlink itself (`command -v`), not its readlink
# target — `claude` and the mise `clarp` shim both resolve to a differently
# named file (a versioned binary / `mise`), so the target dir wouldn't contain
# a `claude`/`clarp` entry to find.
echo ">> resolving tool locations for the service PATH"
declare -a _path_dirs=()
_path_dirs+=( "$SHARE/bin" )
if [[ "$TOOLCHAIN_MODE" == "managed" ]]; then
    _path_dirs+=( "$TOOLCHAIN_DIR/bin" )
fi
find_external_tool() {
    "$PYTHON" - "$1" "$SHARE/toolchains" <<'PY'
import os, pathlib, sys
command = sys.argv[1]
managed = pathlib.Path(sys.argv[2]).resolve()
for raw in os.environ.get("PATH", "").split(os.pathsep):
    if not raw:
        continue
    candidate = pathlib.Path(raw).expanduser() / command
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        continue
    try:
        resolved = candidate.resolve()
    except OSError:
        continue
    if resolved == managed or managed in resolved.parents:
        continue
    print(candidate)
    break
PY
}
for _t in clarp claude codex agy node ffmpeg uv git; do
    if [[ "$TOOLCHAIN_MODE" == "managed" ]]; then
        _loc="$(command -v "$_t" 2>/dev/null || true)"
    else
        _loc="$(find_external_tool "$_t")"
    fi
    [[ -n "$_loc" ]] && _path_dirs+=( "$(cd "$(dirname "$_loc")" && pwd)" )
done
_path_dirs+=( /usr/local/bin /usr/bin /bin )

SERVICE_PATH=""
service_dir_is_safe() {
    "$PYTHON" - "$1" "$SHARE/toolchains" <<'PY'
import pathlib, sys
directory = pathlib.Path(sys.argv[1])
managed = pathlib.Path(sys.argv[2]).resolve()
for name in ("claude", "codex", "node"):
    candidate = directory / name
    if not candidate.exists() and not candidate.is_symlink():
        continue
    try:
        resolved = candidate.resolve()
    except OSError:
        continue
    if resolved == managed or managed in resolved.parents:
        raise SystemExit(1)
PY
}
for _d in "${_path_dirs[@]}"; do
    [[ -z "$_d" ]] && continue
    if [[ "$TOOLCHAIN_MODE" != "managed" ]] && ! service_dir_is_safe "$_d"; then
        continue
    fi
    case ":$SERVICE_PATH:" in
        *":$_d:"*) ;;                                   # already present, skip
        *) SERVICE_PATH="${SERVICE_PATH:+$SERVICE_PATH:}$_d" ;;
    esac
done
echo "   service PATH = $SERVICE_PATH"
printf '%s\n' "$PYTHON" > "$RELEASE/SERVICE_PYTHON"
printf '%s\n' "$SERVICE_PATH" > "$RELEASE/SERVICE_PATH"
if [[ "$TOOLCHAIN_MODE" == "managed" ]]; then
    ln -sfn "$TOOLCHAIN_DIR" "$SHARE/toolchain"
elif [[ -L "$SHARE/toolchain" ]]; then
    rm -f "$SHARE/toolchain"
fi

# ---- per-user service ----------------------------------------------------
echo ">> installing $( [[ "$PLATFORM" == "Darwin" ]] && echo launchd || echo systemd ) service (python=$PYTHON)"
PYTHONPATH="$SHARE/current" "$PYTHON" - "$PYTHON" "$SHARE" "$SERVICE_PATH" <<'PY'
import pathlib
import sys
from lib import service_manager

service_manager.write_definition(
    python=pathlib.Path(sys.argv[1]),
    share=pathlib.Path(sys.argv[2]),
    service_path=sys.argv[3],
)
service_manager.install_and_restart()
PY

if [[ "${CLARP_SKIP_HEALTHCHECK:-0}" != "1" ]]; then
    PYTHONPATH="$SHARE/current" "$PYTHON" - \
        "$CFG_DIR/config.toml" "$RELEASE_ID" <<'PY'
import pathlib
import sys
from lib import service_manager

service_manager.wait_until_ready(
    pathlib.Path(sys.argv[1]), expected_release_id=sys.argv[2])
PY
fi

if [[ -f "$CFG_DIR/install.json" ]]; then
    "$BIN/clarp-admin" skills repair-links || \
        echo "   WARNING: managed skill link repair failed; run clarp-admin doctor"
fi

# Clarp writes nothing to ~/.claude/. Hooks are loaded by
# `claude --plugin-dir` (lib.deployment.plugin_dir) and usage comes from
# per-turn accounting (lib.turn_usage), so there is no settings.json to merge
# and nothing to symlink into ~/.claude/hooks.

# AGY has no per-launch plugin flag. A named global lifecycle hook observes
# terminal turns for already-registered conversations; stream-json runs opt out.
PYTHONPATH="$SHARE/current" "$PYTHON" - "$SHARE" "$HOME" <<'PY'
from pathlib import Path
import sys
from lib.agy_hooks import configure_hooks
if not configure_hooks(Path(sys.argv[1]), Path(sys.argv[2])):
    print("   WARNING: Antigravity hook configuration left untouched")
PY

echo
SRV_PORT=$(awk '
    /^\[server\]/ { in_srv=1; next }
    /^\[/         { in_srv=0 }
    in_srv && /^port[[:space:]]*=/ { gsub(/[^0-9]/,"",$0); print; exit }
' "$CFG_DIR/config.toml")
SRV_BIND=$(awk '
    /^\[server\]/ { in_srv=1; next }
    /^\[/         { in_srv=0 }
    in_srv && /^bind_addr[[:space:]]*=/ {
        sub(/^[^=]*=[[:space:]]*/, "", $0); gsub(/"/, "", $0); print; exit
    }
' "$CFG_DIR/config.toml")
echo "Done. Clarp is running at http://${SRV_BIND:-127.0.0.1}:${SRV_PORT:-7682}/"
printf '%s\n' "ok" > "$RELEASE/INSTALL_OK"
ACTIVATED=0
rm -f "$UNIT_BACKUP"
rm -rf "$EXTERNAL_BACKUP"
trap - EXIT
echo
echo "Next steps:"
if [[ "$TOOLCHAIN_MODE" == "managed" ]]; then
    echo "  1. Sign into the managed Claude/Codex CLIs from Computer settings"
    echo "     or with the wrappers reported by 'clarp-admin paths'."
elif [[ "$TOOLCHAIN_MODE" == "existing" ]]; then
    echo "  1. Existing backend CLIs are baked into the service PATH. Re-run"
    echo "     install.sh if one moves."
else
    echo "  1. Agent CLI setup was deferred. Re-run setup.sh with an explicit"
    echo "     managed or existing toolchain choice before dispatching turns."
fi
echo "  2. Open the PWA on your phone. Fresh installs bind to loopback."
echo "     Use 'clarp-admin network use tailscale', 'lan', or 'manual' when"
echo "     you are ready to connect another device."
echo "  3. Reconfigure through either interface; both use the same setup engine:"
echo "       clarp-tui                 # Textual wizard"
echo "       clarp-admin setup         # interactive CLI wizard"
echo "       clarp-admin setup --help  # unattended/AI-friendly examples"
echo "     Then create a one-time pairing QR from the TUI or clarp-admin."

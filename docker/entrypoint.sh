#!/bin/sh
set -eu

umask 077

CLARP_DATA_DIR="${CLARP_DATA_DIR:-/data}"
export CLARP_DATA_DIR
export CLARP_DEPLOYMENT_MODE=container
export CLARP_SHARE_DIR="${CLARP_SHARE_DIR:-/opt/clarp}"
export CLARP_CONFIG_DIR="${CLARP_CONFIG_DIR:-$CLARP_DATA_DIR/clarp}"
export CLAUDE_PWA_CONFIG="${CLAUDE_PWA_CONFIG:-$CLARP_CONFIG_DIR/config.toml}"
export CLAUDE_PWA_DB="${CLAUDE_PWA_DB:-$CLARP_CONFIG_DIR/state.sqlite}"
export CLARP_CACHE_DIR="${CLARP_CACHE_DIR:-/tmp/clarp-cache}"
export CLARP_CLAUDE_HOME="${CLARP_CLAUDE_HOME:-$CLARP_DATA_DIR/claude}"
export CODEX_HOME="${CODEX_HOME:-$CLARP_DATA_DIR/codex}"
export CLARP_INSTALL_STATE="${CLARP_INSTALL_STATE:-$CLARP_CONFIG_DIR/install.json}"
export CLARP_CLAUDE_SKILLS="${CLARP_CLAUDE_SKILLS:-$CLARP_CLAUDE_HOME/skills}"
export CLARP_CODEX_SKILLS="${CLARP_CODEX_SKILLS:-$CODEX_HOME/skills}"
export CLARP_TRANSCRIPTION_MODELS="${CLARP_TRANSCRIPTION_MODELS:-$CLARP_DATA_DIR/models}"
export CLARP_TRANSCRIPTION_REGISTRY="${CLARP_TRANSCRIPTION_REGISTRY:-$CLARP_DATA_DIR/models/transcription-models.json}"
export HF_HOME="${HF_HOME:-$CLARP_DATA_DIR/models/huggingface}"
export CLARP_MEDIA_DIR="${CLARP_MEDIA_DIR:-$CLARP_DATA_DIR/media}"
export CLARP_UPLOADS_DIR="${CLARP_UPLOADS_DIR:-$CLARP_DATA_DIR/uploads}"
export CLARP_WORKSPACE_ROOT="${CLARP_WORKSPACE_ROOT:-$CLARP_DATA_DIR/workspace}"
export GH_CONFIG_DIR="${GH_CONFIG_DIR:-$CLARP_DATA_DIR/git/gh}"
export CLAUDE_PWA_BIND="${CLAUDE_PWA_BIND:-0.0.0.0}"

python3 - <<'PY'
from pathlib import Path
import os
import secrets
import shutil

root = Path(os.environ["CLARP_DATA_DIR"])
if not root.exists():
    raise SystemExit(f"Clarp data volume is missing: {root}")
if not os.access(root, os.W_OK):
    raise SystemExit(f"Clarp data volume is not writable: {root}")

from lib.deployment import LAYOUT
LAYOUT.create_container_directories()

# Restores are staged by `clarp-admin backup restore` and applied only during
# startup, before any server or hook process has opened SQLite.
from lib.instance_backup import apply_pending_restore
apply_pending_restore()

home = Path.home()
links = {
    home / ".claude": LAYOUT.claude_home,
    home / ".codex": LAYOUT.codex_home,
}
for link, target in links.items():
    if link.is_symlink() and link.resolve() == target.resolve():
        continue
    if link.exists() or link.is_symlink():
        raise SystemExit(f"refusing to replace existing container path: {link}")
    link.symlink_to(target, target_is_directory=True)

config = LAYOUT.config_file
if not config.exists():
    source = Path("/opt/clarp/config.example.toml")
    shutil.copy2(source, config)
    text = config.read_text()
    text = text.replace('bind_addr = "127.0.0.1"', 'bind_addr = "0.0.0.0"')
    text = text.replace('auth_token = ""', f'auth_token = "{secrets.token_urlsafe(32)}"')
    config.write_text(text)
    config.chmod(0o600)
    # A headless container has no opportunity to enter a cloud credential in
    # the setup TUI. Start silent and let Computer settings choose a provider.
    from lib.config_writer import set_toml_value
    set_toml_value(config, "tts", "provider", "none")
    set_toml_value(config, "tts", "fallback", "none")

# Initialize the schema and identity without claiming any global personas.
# Docker nodes start empty; users explicitly create or move personas to them.
from lib import db
db.conn()
from lib.server_identity import get_server_info
get_server_info()

# Core Clarp skills come from the immutable image, while their enablement and
# CLI links belong to this server's private data volume.
from lib import managed_skills
for item in managed_skills.status():
    if item.get("pack") == "core":
        managed_skills.set_enabled(str(item["id"]), True)
from lib.personal_skills import repair_links as repair_personal_skill_links
repair_personal_skill_links()
PY

exec "$@"

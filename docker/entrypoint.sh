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

from lib.device_pairing import list_devices, issue
if not list_devices():
    import urllib.parse
    server_info = get_server_info()
    server_name = server_info.get("name", "Clarp Docker")
    server_id = server_info.get("server_id", "")
    port = int(os.environ.get("CLARP_PORT") or os.environ.get("CLAUDE_PWA_PORT", 7682))

    public_url = os.environ.get("CLARP_PUBLIC_URL", "").strip()
    if not public_url:
        host_override = os.environ.get("CLARP_HOST", "").strip()
        if host_override:
            public_url = f"http://{host_override}:{port}"
        else:
            candidates = set()
            import socket
            try:
                for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                    candidates.add(info[4][0])
            except Exception:
                pass
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("1.1.1.1", 80))
                candidates.add(s.getsockname()[0])
                s.close()
            except Exception:
                pass
            try:
                import subprocess
                out = subprocess.run(["ip", "-o", "-4", "addr", "show"], capture_output=True, text=True, timeout=2).stdout
                for line in out.splitlines():
                    parts = line.split()
                    if len(parts) >= 4:
                        candidates.add(parts[3].split("/")[0])
            except Exception:
                pass
            candidates.discard("127.0.0.1")
            ts_ips = [ip for ip in candidates if ip.startswith("100.") and 64 <= int(ip.split(".")[1]) <= 127]
            lan_ips = [ip for ip in candidates if ip.startswith("192.168.") or ip.startswith("10.")]
            best_ip = ts_ips[0] if ts_ips else (lan_ips[0] if lan_ips else "127.0.0.1")
            public_url = f"http://{best_ip}:{port}"

    try:
        record = issue(device_name="iPhone", scope="full", ttl_seconds=3600)
        query = urllib.parse.urlencode({
            "name": server_name,
            "url": public_url,
            "code": record["code"],
            "server_id": server_id,
            "scope": record["scope"],
            "expires_at": record["expires_at"],
        })
        pair_uri = f"clarp://pair?{query}"
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(pair_uri)
        qr.make(fit=True)
        print("\n" + "=" * 62, flush=True)
        print(" Clarp Initial Setup: Scan to pair with iPhone", flush=True)
        print("=" * 62, flush=True)
        qr.print_ascii(invert=True)
        print(f"Pairing URI: {pair_uri}", flush=True)
        print(f"Target URL:  {public_url} (valid for 1 hour)", flush=True)
        print("=" * 62 + "\n", flush=True)
    except Exception as exc:
        print(f"Notice: could not emit bootstrap pairing QR: {exc}", flush=True)
PY

exec "$@"

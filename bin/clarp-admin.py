#!/usr/bin/env python3
"""Clarp setup, updates, managed skills, and transcription models."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
from pathlib import Path
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
import urllib.parse
import time
from contextlib import contextmanager


HOME = Path.home()
REPO = Path(__file__).resolve().parents[1]
SERVER_ROOT = REPO if (REPO / "lib").is_dir() else REPO / "server"
sys.path.insert(0, str(SERVER_ROOT))
from lib import service_manager, xdg  # noqa: E402
from lib.config_writer import set_toml_value, toml_value  # noqa: E402

CONFIG_DIR = Path(os.environ.get("CLARP_CONFIG_DIR", xdg.config_dir(HOME)))
CONFIG_FILE = CONFIG_DIR / "config.toml"
INSTALL_STATE = CONFIG_DIR / "install.json"
SHARE = Path(os.environ.get("CLARP_SHARE_DIR", xdg.data_dir(HOME)))
CLAUDE_SKILLS = Path(os.environ.get("CLARP_CLAUDE_SKILLS", HOME / ".claude/skills"))
CODEX_SKILLS = Path(os.environ.get(
    "CLARP_CODEX_SKILLS",
    Path(os.environ.get("CODEX_HOME", HOME / ".codex")) / "skills",
))
_INSTALL_STATE_LOCK_DEPTH = 0


def run(*args: str, cwd: Path | None = None, check: bool = True,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, check=check, text=True, env=env)


# Where every install ultimately came from. install.sh falls back to this when
# the source has no git metadata (the curl | bash quick start unpacks a
# tarball), and so must the updater, or a quick-start install can never update
# itself (issue #12).
CANONICAL_SOURCE_REMOTE = "https://github.com/Maxteabag/clarp.git"


def git_origin(repo: Path) -> str:
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"], cwd=repo,
        text=True, capture_output=True, check=False)
    return sanitize_remote(result.stdout.strip())


def sanitize_remote(value: str) -> str:
    """Drop embedded credentials, query and fragment from an http(s) remote."""
    value = (value or "").strip()
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme in {"http", "https"}:
        host = parsed.netloc
        if parsed.username is not None:
            host = parsed.hostname or ""
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            if parsed.port is not None:
                host = f"{host}:{parsed.port}"
        value = urllib.parse.urlunsplit(
            (parsed.scheme, host, parsed.path, "", ""))
    return value


def resolve_update_remote(state: dict) -> str:
    """The remote to fetch updates from, never empty.

    install.json records what git reported at setup time, which is nothing for
    a tarball. install.sh always resolves a remote and writes it next to the
    release, so that file is the second source, and the canonical repository
    is the last, exactly as install.sh itself falls back.
    """
    candidates = [str(state.get("source_remote") or "")]
    for path in (SHARE / "current/SOURCE_REMOTE", SHARE / "SOURCE_REMOTE"):
        try:
            candidates.append(path.read_text())
        except OSError:
            continue
    for candidate in candidates:
        remote = sanitize_remote(candidate)
        if remote:
            return remote
    return CANONICAL_SOURCE_REMOTE


def stable_release_tags(tags: list[str]) -> list[str]:
    """Stable channel excludes prereleases, matching server update checks."""
    return [tag for tag in tags if re.fullmatch(r"v\d+\.\d+\.\d+(?:\+[0-9A-Za-z.-]+)?", tag)]


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    path.chmod(0o600)


def prompt(label: str, default: str) -> str:
    answer = input(f"{label} [{default}]: ").strip()
    return answer or default


def confirm(label: str, default: bool = True) -> bool:
    suffix = "Y/n" if default else "y/N"
    answer = input(f"{label} [{suffix}]: ").strip().lower()
    return default if not answer else answer in {"y", "yes"}


def server_connection() -> tuple[str, str]:
    cfg = tomllib.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    server = cfg.get("server", {})
    host = str(server.get("bind_addr", "127.0.0.1"))
    if host in {"0.0.0.0", "::", "localhost"}: host = "127.0.0.1"
    if ":" in host and not host.startswith("["): host = f"[{host}]"
    return f"http://{host}:{int(server.get('port', 7682))}", str(
        server.get("auth_token", "") or "")


def api_request(method: str, path: str, body=None):
    base, token = server_connection()
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    if token: request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read()
    return json.loads(raw) if raw else {}


def load_manifest() -> dict:
    candidates = [SHARE / "current/skills/manifest.json", REPO / "skills/manifest.json"]
    for candidate in candidates:
        if candidate.is_file():
            return json.loads(candidate.read_text())
    raise SystemExit("Clarp skill manifest is missing")


def skill_source(skill_id: str) -> Path:
    base = SHARE / "current/skills"
    if not base.is_dir():
        base = REPO / "skills"
    return base / skill_id


def managed_link(path: Path) -> bool:
    if not path.is_symlink():
        return False
    try:
        return path.resolve().is_relative_to(SHARE.resolve())
    except OSError:
        return False


def link_skill(skill_id: str) -> None:
    source = skill_source(skill_id)
    if not (source / "SKILL.md").is_file():
        raise SystemExit(f"skill has no SKILL.md: {skill_id}")
    for root in (CLAUDE_SKILLS, CODEX_SKILLS):
        root.mkdir(parents=True, exist_ok=True)
        destination = root / skill_id
        if destination.exists() or destination.is_symlink():
            if managed_link(destination) or (destination.is_symlink()
                                               and destination.resolve() == source.resolve()):
                destination.unlink()
            else:
                print(f"WARNING: preserving non-Clarp skill at {destination}")
                continue
        destination.symlink_to(source, target_is_directory=True)
        print(f"linked {destination} -> {source}")


def unlink_skill(skill_id: str) -> None:
    for root in (CLAUDE_SKILLS, CODEX_SKILLS):
        destination = root / skill_id
        if managed_link(destination):
            destination.unlink()
            print(f"removed {destination}")


def selected_skills() -> list[str]:
    return read_json(INSTALL_STATE, {}).get("skills", [])


def installed_command(name: str) -> str:
    managed_root = SHARE / "toolchain"
    try:
        recorded = (SHARE / "current/TOOLCHAIN_DIR").read_text().strip()
        if recorded:
            managed_root = Path(recorded)
    except OSError:
        pass
    managed = managed_root / "bin" / name
    if managed.is_file():
        return str(managed)
    return shutil.which(name) or ""


@contextmanager
def install_state_lock():
    global _INSTALL_STATE_LOCK_DEPTH
    if _INSTALL_STATE_LOCK_DEPTH:
        _INSTALL_STATE_LOCK_DEPTH += 1
        try:
            yield
        finally:
            _INSTALL_STATE_LOCK_DEPTH -= 1
        return
    lock_path = INSTALL_STATE.with_suffix(INSTALL_STATE.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _INSTALL_STATE_LOCK_DEPTH = 1
        try:
            yield
        finally:
            _INSTALL_STATE_LOCK_DEPTH = 0
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def save_selected_skills(skills: list[str]) -> None:
    with install_state_lock():
        state = read_json(INSTALL_STATE, {})
        state["skills"] = sorted(set(skills))
        write_json(INSTALL_STATE, state)


def cmd_skills(args) -> int:
    if args.skills_command in {"import", "source-add", "source-update"}:
        server_root = SHARE
        if not (server_root / "lib").is_dir():
            server_root = REPO / "server"
        sys.path.insert(0, str(server_root))
        from lib import personal_skills
        if args.skills_command == "import":
            for skill_id in personal_skills.import_path(
                    Path(args.path), replace=args.replace):
                print(f"imported: {skill_id}")
        elif args.skills_command == "source-add":
            print(json.dumps(personal_skills.add_git(
                args.url, name=args.name, ref=args.ref), indent=2))
        else:
            print(json.dumps(personal_skills.update_git(args.name), indent=2))
        return 0
    with install_state_lock():
        return _cmd_skills_locked(args)


def _cmd_skills_locked(args) -> int:
    manifest = load_manifest()
    entries = {item["id"]: item for item in manifest["skills"]}
    if args.skills_command == "list":
        active = set(selected_skills())
        for item in manifest["skills"]:
            status = "installed" if item["id"] in active else "available"
            print(f"{item['id']:<30} {item['pack']:<10} {status}")
        return 0
    if args.skills_command == "install":
        unknown = set(args.skill_ids) - entries.keys()
        if unknown:
            raise SystemExit(f"unknown skills: {', '.join(sorted(unknown))}")
        active = selected_skills()
        for skill_id in args.skill_ids:
            link_skill(skill_id)
            if skill_id not in active:
                active.append(skill_id)
        save_selected_skills(active)
        return 0
    if args.skills_command == "remove":
        active = selected_skills()
        for skill_id in args.skill_ids:
            unlink_skill(skill_id)
            active = [item for item in active if item != skill_id]
        save_selected_skills(active)
        return 0
    if args.skills_command == "repair-links":
        active = selected_skills()
        # Core skills are product contracts and cannot be disabled. Enrol core
        # skills introduced after the original install instead of leaving them
        # permanently inactive in an older persisted selection list.
        for item in manifest["skills"]:
            if item.get("pack") == "core" and item["id"] not in active:
                active.append(item["id"])
        state = read_json(INSTALL_STATE, {})
        if not state.get("artifact_skill_split_v1") and "clarp-artifacts" in active:
            active.extend(item["id"] for item in manifest["skills"]
                          if item.get("pack") == "artifacts")
            state["artifact_skill_split_v1"] = True
            state["skills"] = sorted(set(active))
            write_json(INSTALL_STATE, state)
        else:
            save_selected_skills(active)
        if not state.get("artifact_skill_defaults_v2") and "clarp-artifacts" in active:
            previous_active=set(active)
            removed={"clarp-links","clarp-message-drafts","clarp-collections","clarp-live-tasks","clarp-deployments","clarp-workspaces"}
            active=[s for s in active if s not in removed]
            for new_default in ("clarp-directories","clarp-github-actions"):
                if new_default in entries and new_default not in active: active.append(new_default)
            state=read_json(INSTALL_STATE,{}); state["artifact_skill_defaults_v2"]=True; state["skills"]=sorted(set(active)); write_json(INSTALL_STATE,state)
            for skill_id in previous_active-set(active): unlink_skill(skill_id)
        # A rollback may not know skills introduced by the newer release. Keep
        # their selection for a future re-upgrade, but remove links that would
        # otherwise point through `current` at nonexistent sources.
        for skill_id in set(active) - entries.keys():
            unlink_skill(skill_id)
        for skill_id in active:
            # Selection state can contain a core skill introduced by a newer
            # release. Rollback must still repair every skill the older active
            # manifest knows without failing on that forward-only ID.
            if skill_id in entries:
                link_skill(skill_id)
        return 0
    return 2


def import_transcription_modules():
    server_root = SHARE / "current"
    if not (server_root / "lib").is_dir(): server_root = REPO / "server"
    sys.path.insert(0, str(server_root))
    from lib.stt import _installed_model_records  # type: ignore
    from lib.transcription_catalog import (model_by_id, public_catalog,
                                           recommended_model_id)  # type: ignore
    return (_installed_model_records, public_catalog(), recommended_model_id(),
            model_by_id)


def install_model(model_id: str) -> None:
    installed, _catalog, _recommended, lookup = import_transcription_modules()
    if any(item["id"] == model_id for item in installed()):
        print(f"already installed: {model_id}")
        return
    item = lookup(model_id)
    if item is None:
        raise SystemExit(f"unsupported model: {model_id}")
    supported = item.get("platforms", ["linux", "macos"])
    host = service_manager.platform_kind()
    if host not in supported:
        raise SystemExit(f"{model_id} is not supported on {host}")
    print(f"installing {item['name']} ({item['weight']})")
    server_root = SHARE / "current"
    if not (server_root / "lib").is_dir(): server_root = REPO / "server"
    sys.path.insert(0, str(server_root))
    from lib.transcription_models import install
    install(model_id)
    print(f"installed: {model_id}")


def remove_model(model_id: str, *, allow_active: bool = False) -> None:
    installed, _catalog, _recommended, _lookup = import_transcription_modules()
    record = next((item for item in installed() if item["id"] == model_id), None)
    if record is None:
        print(f"not installed: {model_id}")
        return
    server_root = SHARE / "current"
    if not (server_root / "lib").is_dir(): server_root = REPO / "server"
    sys.path.insert(0, str(server_root))
    from lib.transcription_models import remove
    remove(model_id, allow_active=allow_active)
    print(f"removed: {model_id}")


def cmd_transcription(args) -> int:
    if args.transcription_command == "adapters":
        server_root = SHARE / "current"
        if not (server_root / "lib").is_dir(): server_root = REPO / "server"
        sys.path.insert(0, str(server_root))
        from lib import custom_stt_adapters
        action = args.adapter_command
        if action == "list":
            print(json.dumps({"adapters": custom_stt_adapters.inventory()}, indent=2))
            return 0
        if action == "validate":
            manifest = custom_stt_adapters.load_manifest(args.path, portable=True)
            print(json.dumps({"ok": True, "adapter": manifest.provider_row()}, indent=2))
            return 0
        if action == "install":
            manifest = custom_stt_adapters.install(
                args.path, replace=args.replace)
            print(json.dumps({"ok": True, "installed": manifest.id}, indent=2))
            return 0
        if action == "remove":
            cfg = tomllib.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
            active = str((cfg.get("whisper") or {}).get("provider") or "").lower()
            if args.provider.strip().lower() == active:
                raise SystemExit(
                    "cannot remove the active transcription adapter; "
                    "select another model first")
            custom_stt_adapters.remove(args.provider)
            print(json.dumps({"ok": True, "removed": args.provider}, indent=2))
            return 0
        manifest = custom_stt_adapters.get(args.provider)
        if manifest is None:
            raise SystemExit(
                f"custom transcription adapter is not installed: {args.provider}")
        print(json.dumps(custom_stt_adapters.test_adapter(manifest), indent=2))
        return 0
    installed, catalog, recommended, lookup = import_transcription_modules()
    installed_ids = {item["id"] for item in installed()}
    if args.transcription_command == "list":
        for item in catalog:
            state = ("installed" if item["id"] in installed_ids else
                     "download" if item.get("supported", True) else "unsupported")
            marker = " recommended" if item.get("recommended") else ""
            size = item["download_bytes"] / 1_000_000
            print(f"{item['id']:<38} {item['weight']:<11} {size:>6.0f} MB  {state}{marker}")
        return 0
    if args.transcription_command == "install":
        install_model(args.model_id)
        return 0
    if args.transcription_command == "remove":
        remove_model(args.model_id)
        return 0
    if args.transcription_command == "use":
        item = lookup(args.model_id)
        if item is None:
            server_root = SHARE / "current"
            if not (server_root / "lib").is_dir(): server_root = REPO / "server"
            sys.path.insert(0, str(server_root))
            from lib.custom_stt_adapters import get as custom_adapter, models
            provider = args.model_id.split(":", 1)[0] \
                if ":" in args.model_id else ""
            manifest = custom_adapter(provider)
            item = next((row for row in models(manifest)
                         if row["id"] == args.model_id), None) \
                if manifest is not None else None
        if item is None:
            raise SystemExit(f"unsupported model: {args.model_id}")
        if not item.get("custom"):
            install_model(args.model_id)
        set_toml_value(CONFIG_FILE, "whisper", "model", item["model"])
        set_toml_value(CONFIG_FILE, "whisper", "provider", item["provider"])
        set_toml_value(CONFIG_FILE, "whisper", "enabled", True)
        print(f"server default set to {args.model_id}; restart Clarp to apply")
        return 0
    if args.transcription_command == "test":
        cfg = tomllib.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
        server = cfg.get("server", {})
        host = server.get("bind_addr", "127.0.0.1")
        if host in {"0.0.0.0", "::"}: host = "127.0.0.1"
        url = f"http://{host}:{server.get('port', 7682)}/transcription-capabilities"
        request = urllib.request.Request(url)
        if server.get("auth_token"):
            request.add_header("Authorization", f"Bearer {server['auth_token']}")
        with urllib.request.urlopen(request, timeout=5) as response:
            print(json.dumps(json.load(response), indent=2))
        return 0
    if args.transcription_command == "import":
        server_root = SHARE / "current"
        if not (server_root / "lib").is_dir(): server_root = REPO / "server"
        sys.path.insert(0, str(server_root))
        from lib.transcription_models import register
        model_id = f"{args.provider}:{args.model}"
        register(model_id, args.path, runtime_path=args.runtime_path or None)
        print(f"registered: {model_id}")
        return 0
    return 2


def validate_setup_choices(
    backend: str, optional: list[str], toolchain: str = "existing",
) -> None:
    if backend not in {"claude", "codex", "both"}:
        raise SystemExit("backend must be claude, codex, or both")
    known = {item["id"] for item in load_manifest()["skills"]}
    unknown = set(optional) - known
    if unknown:
        raise SystemExit(f"unknown optional skills: {', '.join(sorted(unknown))}")
    if toolchain not in {"managed", "existing", "none"}:
        raise SystemExit("toolchain must be managed, existing, or none")


def external_command(command: str) -> str | None:
    """Find a PATH command that is not owned by Clarp's managed toolchain."""
    managed_root = (SHARE / "toolchains").resolve()
    for raw in os.environ.get("PATH", "").split(os.pathsep):
        if not raw:
            continue
        candidate = Path(raw).expanduser() / command
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved == managed_root or managed_root in resolved.parents:
            continue
        return str(candidate)
    return None


def database_needs_roster() -> bool:
    # Setup owns the configured installation at SHARE. Ambient service/test
    # variables must not make it inspect an unrelated database.
    path = SHARE / "state.sqlite"
    if not path.is_file():
        return True
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as database:
            table = database.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agents'"
            ).fetchone()
            return table is None or database.execute(
                "SELECT 1 FROM agents LIMIT 1").fetchone() is None
    except sqlite3.Error:
        return False


def seed_fresh_roster(backend: str) -> None:
    """Create the built-in personas only when the new database is empty."""
    initial_backend = "codex" if backend == "codex" else "claude"
    server_root = SHARE / "current"
    if not (server_root / "lib").is_dir():
        server_root = REPO / "server"
    script = (
        "import pathlib,sys; "
        "sys.path.insert(0, sys.argv[1]); "
        "from lib.roster_seed import seed_defaults; "
        "seed_defaults(sys.argv[2], cwd=pathlib.Path(sys.argv[3]))"
    )
    run(sys.executable, "-c", script, str(server_root), initial_backend, str(HOME))


def _restore_files(snapshots: dict[Path, bytes | None]) -> None:
    for path, content in snapshots.items():
        if content is None:
            if path.exists(): path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)


def configured_server_value(key: str, fallback):
    """Existing `[server]` setting, so re-running setup on a live install does
    not reset it. Setup used to write the `--bind`/`--port` defaults
    unconditionally, which silently replaced a deliberate tailnet bind_addr with
    loopback and cut the phone off from the server."""
    if not CONFIG_FILE.exists():
        return fallback
    try:
        cfg = tomllib.loads(CONFIG_FILE.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return fallback
    value = cfg.get("server", {}).get(key)
    return fallback if value is None else value


def _execute_setup(backend: str, transcription: str, bind: str | None,
                   port: int | None,
                   optional: list[str], channel: str,
                   toolchain: str = "existing", tts_provider: str = "",
                   tts_fallback: str = "none", network_mode: str = "",
                   public_url: str = "") -> int:
    if (os.environ.get("PYTEST_CURRENT_TEST")
            and os.environ.get("CLARP_ALLOW_TEST_INSTALL") != "1"):
        default_config = xdg.config_dir(HOME).resolve()
        default_share = xdg.data_dir(HOME).resolve()
        if (CONFIG_DIR.resolve() == default_config
                or SHARE.resolve() == default_share):
            raise RuntimeError(
                "refusing to run setup against default user paths under pytest; "
                "isolate CONFIG_DIR and SHARE or set CLARP_ALLOW_TEST_INSTALL=1")
    user_values = CONFIG_DIR / "user-values.md"
    snapshots = {
        path: path.read_bytes() if path.is_file() else None
        for path in (CONFIG_FILE, user_values, INSTALL_STATE)
    }
    previous_skills = set(selected_skills())
    previous_release = ((SHARE / "current").resolve()
                        if (SHARE / "current").exists() else None)
    chosen: list[str] = []
    previous_install_state = snapshots[INSTALL_STATE]
    skills_reconciled = False
    newly_installed_model = ""
    new_config = not CONFIG_FILE.exists()
    fresh_database = database_needs_roster()
    previous_network_port = int(configured_server_value("port", 7682))
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if not CONFIG_FILE.exists():
            shutil.copy2(REPO / "config.example.toml", CONFIG_FILE)
        # User values start from the generic product template. Setup does not
        # discover or import differently named preference files implicitly.
        if (not user_values.exists()
                and (REPO / "docs/user-values.example.md").is_file()):
            shutil.copy2(REPO / "docs/user-values.example.md", user_values)
        if bind is not None:
            set_toml_value(CONFIG_FILE, "server", "bind_addr", bind)
        if port is not None:
            set_toml_value(CONFIG_FILE, "server", "port", port)
        if tts_provider:
            from lib.tts_providers import valid_ids
            valid_tts = valid_ids()
            if tts_provider not in valid_tts:
                raise SystemExit(f"unsupported TTS provider: {tts_provider}")
            if tts_fallback not in valid_tts:
                raise SystemExit(f"unsupported TTS fallback: {tts_fallback}")
            set_toml_value(CONFIG_FILE, "tts", "provider", tts_provider)
            set_toml_value(CONFIG_FILE, "tts", "fallback", tts_fallback)
            if tts_provider != "cartesia":
                set_toml_value(CONFIG_FILE, "audio", "delivery", "chunked-file")
        if new_config or fresh_database:
            set_toml_value(CONFIG_FILE, "server", "default_session", "mike")
        cfg = tomllib.loads(CONFIG_FILE.read_text())
        effective_bind = str(
            cfg.get("server", {}).get("bind_addr", "127.0.0.1"))
        if (not cfg.get("server", {}).get("auth_token")
                and effective_bind not in {"127.0.0.1", "::1"}):
            set_toml_value(
                CONFIG_FILE, "server", "auth_token", secrets.token_urlsafe(32))

        if transcription not in {"apple", "apple-only", "none"}:
            _installed, _catalog, recommended, lookup = import_transcription_modules()
            model_id = recommended if transcription == "recommended" else transcription
            already_installed = any(record["id"] == model_id for record in _installed())
            item = lookup(model_id)
            if item is None:
                raise SystemExit(f"unsupported transcription model: {model_id}")
            if item["provider"] not in {"faster-whisper", "whisper.cpp"}:
                raise SystemExit(
                    "The setup-time server default must be Faster-Whisper or "
                    "whisper.cpp. Choose recommended or a matching provider id; "
                    "choose the platform-recommended provider.")
            install_model(model_id)
            if not already_installed:
                newly_installed_model = model_id
            set_toml_value(CONFIG_FILE, "whisper", "model", item["model"])
            set_toml_value(CONFIG_FILE, "whisper", "provider", item["provider"])
            set_toml_value(CONFIG_FILE, "whisper", "enabled", True)
        else:
            set_toml_value(CONFIG_FILE, "whisper", "enabled", False)

        env = os.environ.copy()
        env["CLARP_TOOLCHAIN_MODE"] = toolchain
        run(str(REPO / "install.sh"), cwd=REPO, env=env)
        if (SHARE / "current/server.py").is_file():
            deadline = time.time() + 20
            while True:
                try:
                    api_request("GET", "/status")
                    break
                except (OSError, urllib.error.URLError):
                    if time.time() >= deadline:
                        raise RuntimeError("Clarp server did not become ready after install")
                    time.sleep(0.25)
        with install_state_lock():
            previous_skills = set(selected_skills())
            previous_install_state = (
                INSTALL_STATE.read_bytes() if INSTALL_STATE.is_file() else None)
            skills_reconciled = True
            manifest = load_manifest()
            core = [item["id"] for item in manifest["skills"] if item["pack"] == "core"]
            artifact_skills = [item["id"] for item in manifest["skills"]
                               if item["pack"] == "artifacts" and item.get("default_enabled", True)]
            chosen = sorted(set(core + artifact_skills + optional))
            for skill_id in sorted(previous_skills - set(chosen)):
                unlink_skill(skill_id)
            for skill_id in chosen: link_skill(skill_id)
            write_json(INSTALL_STATE, {
                "source_repo": str(REPO), "skills": chosen,
                "source_remote": git_origin(REPO) or resolve_update_remote({}),
                "channel": channel, "backend": backend,
                "transcription": transcription, "python": sys.executable,
                "toolchain": toolchain,
                "artifact_skill_split_v1": True,
                "artifact_skill_defaults_v2": True,
            })
            if (SHARE / "current/server.py").is_file():
                seed_fresh_roster(backend)
        # Networking is deliberately last: it mutates external Tailscale state.
        # No later setup step may fail after this transition commits.
        if network_mode:
            cmd_network(argparse.Namespace(
                network_command="use", mode=network_mode,
                url=public_url, previous_port=previous_network_port))
    except BaseException:
        _restore_files({path: content for path, content in snapshots.items()
                        if path != INSTALL_STATE})
        if newly_installed_model:
            try:
                remove_model(newly_installed_model, allow_active=True)
            except Exception as cleanup_error:  # noqa: BLE001 - preserve root failure
                print(f"warning: could not roll back model: {cleanup_error}",
                      file=sys.stderr)
        current_release = ((SHARE / "current").resolve()
                           if (SHARE / "current").exists() else None)
        if current_release != previous_release:
            if previous_release and previous_release.is_dir():
                activate_release(previous_release)
            else:
                cmd_uninstall(argparse.Namespace(purge_data=False))
        if skills_reconciled:
            with install_state_lock():
                _restore_files({INSTALL_STATE: previous_install_state})
                for skill_id in set(chosen) - previous_skills:
                    unlink_skill(skill_id)
                for skill_id in previous_skills:
                    try:
                        link_skill(skill_id)
                    except SystemExit:
                        pass
        raise
    print(setup_complete_message())
    return 0


def pwa_access_url() -> str:
    """The link that provisions a browser with the auth token in one open.

    The PWA reads `?token=` on first visit and stores it; without this link a
    user has to find the token in config.toml by hand, and a browser holding a
    stale token can only recover by opening a fresh one (issue #10).
    """
    cfg = _network_config()
    token = str(cfg.get("server", {}).get("auth_token") or "").strip()
    base = _pairing_public_url() + "/"
    if not token:
        return base
    return base + "?" + urllib.parse.urlencode({"token": token})


def setup_complete_message() -> str:
    return (
        "\nClarp setup complete. Run `clarp-admin doctor` for diagnostics."
        f"\n\nOpen the PWA with this link (it carries the auth token):"
        f"\n  {pwa_access_url()}"
        "\nPrint it again any time with `clarp-admin url` (add --qr for a phone)."
    )


def cmd_url(args) -> int:
    url = pwa_access_url()
    if args.json:
        print(json.dumps({"url": url}, indent=2))
        return 0
    if args.qr:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    print(url)
    return 0


def cmd_setup(args) -> int:
    interactive = not args.non_interactive and sys.stdin.isatty()
    backend = args.backend
    transcription = args.transcription
    toolchain = args.toolchain
    tts_provider = args.tts
    tts_fallback = args.tts_fallback
    network_mode = args.network
    public_url = args.public_url
    bind = args.bind
    port = args.port
    optional = list(args.optional_skill)
    if interactive:
        bind_default = bind or str(
            configured_server_value("bind_addr", "127.0.0.1"))
        port_default = port if port is not None else int(
            configured_server_value("port", 7682))
        print("\nClarp setup wizard\n")
        backend = prompt("Agent backends (claude/codex/both)", backend)
        toolchain = prompt(
            "Agent tools (managed/existing/none)", toolchain or "managed")
        print("Transcription choices: none (no download), "
              "recommended (~488 MB), or a supported model id")
        transcription = prompt(
            "Transcription", transcription or "recommended")
        current = _network_config()
        current_tts = str(
            current.get("tts", {}).get("provider", "cartesia"))
        current_network = str(
            current.get("network", {}).get("mode", "tailscale"))
        print("Voice choices: clarp/cartesia/elevenlabs/deepgram/none")
        tts_provider = prompt("Voice output", tts_provider or current_tts)
        tts_fallback = prompt("Voice fallback", tts_fallback or "none")
        print("Network choices: tailscale/lan/manual/off")
        network_mode = prompt("Phone network", network_mode or current_network)
        if network_mode == "manual":
            public_url = prompt("Public HTTPS URL", public_url)
        bind = prompt("Server bind address", bind_default)
        port = int(prompt("Server port", str(port_default)))
        if confirm("Install optional native iOS skills (calendar, location)?", True):
            optional += ["clarp-calendar", "clarp-location"]
        if confirm("Install optional messaging watcher skill?", False):
            optional += ["clarp-message-watch"]

    if not interactive:
        if not toolchain:
            raise SystemExit("--toolchain is required with --non-interactive")
        if not transcription:
            raise SystemExit("--transcription is required with --non-interactive")
    validate_setup_choices(backend, optional, toolchain)
    if toolchain == "existing":
        required = (["claude"] if backend == "claude" else
                    ["codex"] if backend == "codex" else ["claude", "codex"])
        missing = [command for command in required if not external_command(command)]
        if missing:
            raise SystemExit(
                "selected existing toolchain is missing: " + ", ".join(missing))

    return _execute_setup(
        backend, transcription, bind, port, optional, args.channel, toolchain,
        tts_provider, tts_fallback, network_mode, public_url)


def cmd_doctor(_args) -> int:
    failures = 0
    container_mode = os.environ.get("CLARP_DEPLOYMENT_MODE") == "container"
    try:
        service_python = Path(
            (SHARE / "current/SERVICE_PYTHON").read_text().strip())
    except OSError:
        service_python = Path(sys.executable)
    checks = {
        "locked python": service_python if service_python.is_file() else None,
        "claude or codex": installed_command("claude") or installed_command("codex"),
        "config": CONFIG_FILE if CONFIG_FILE.is_file() else None,
    }
    if not container_mode:
        checks["current release"] = (
            SHARE / "current" if (SHARE / "current").exists() else None)
    for label, value in checks.items():
        ok = bool(value)
        failures += not ok
        print(f"{'OK' if ok else 'FAIL':<5} {label}: {value or 'missing'}")
    if container_mode:
        data = Path(os.environ.get("CLARP_DATA_DIR", "/data"))
        ok = data.is_dir() and os.access(data, os.W_OK)
        failures += not ok
        print(f"{'OK' if ok else 'FAIL':<5} private data volume: {data}")
        for label, command in {
            "git": "git", "GitHub CLI": "gh", "ffmpeg": "ffmpeg",
        }.items():
            value = shutil.which(command)
            failures += not bool(value)
            print(f"{'OK' if value else 'FAIL':<5} {label}: {value or 'missing'}")
        try:
            __import__("faster_whisper")
            whisper_runtime = "python module"
        except ImportError:
            whisper_runtime = ""
        failures += not bool(whisper_runtime)
        print(f"{'OK' if whisper_runtime else 'FAIL':<5} "
              f"Faster-Whisper: {whisper_runtime or 'missing'}")
    else:
        ok = service_manager.is_active()
        failures += not ok
        print(f"{'OK' if ok else 'FAIL':<5} service: {'active' if ok else 'inactive'}")
        runtime_ok = service_manager.is_runtime_active()
        failures += not runtime_ok
        print(f"{'OK' if runtime_ok else 'FAIL':<5} "
              f"agent runtime: {'active' if runtime_ok else 'inactive'}")
    try:
        from lib import config as config_module

        config_module.reset_cache()
        cfg = config_module.load(CONFIG_FILE)
        raw_key = cfg.apns_key_path or os.environ.get("APNS_KEY_PATH", "")
        credential_parts = (raw_key, cfg.apns_key_id, cfg.apns_team_id)
        if not any(credential_parts):
            print("OK    APNs push: not configured (optional)")
        elif not all(credential_parts):
            failures += 1
            print("FAIL  APNs push: incomplete key path, key id, or team id")
        else:
            key_file = Path(cfg.apns_key_file())
            ok = cfg.apns_enabled()
            failures += not ok
            suffix = "" if ok else " (missing or unreadable)"
            print(f"{'OK' if ok else 'FAIL':<5} APNs signing key: {key_file}{suffix}")
    except Exception as exc:  # noqa: BLE001 - doctor reports rather than crashes
        failures += 1
        print(f"FAIL  APNs push: {exc}")
    try:
        server_root = SHARE if (SHARE / "lib").is_dir() else REPO / "server"
        sys.path.insert(0, str(server_root))
        from lib.tts_providers import status as tts_status
        voice = tts_status()
        selected_voice = next(
            row for row in voice["providers"]
            if row["id"] == voice["provider"])
        ok = bool(selected_voice["available"])
        failures += not ok
        print(f"{'OK' if ok else 'FAIL':<5} voice provider: "
              f"{selected_voice['name']}"
              + ("" if ok else " (not configured or installed)"))
    except Exception as exc:  # noqa: BLE001 - doctor reports rather than crashes
        failures += 1
        print(f"FAIL  voice provider: {exc}")
    network = _network_config().get("network", {})
    network_mode = str(network.get("mode", "off"))
    network_ok = True
    if network_mode == "tailscale":
        network_ok = bool(_tailscale_info().get("online"))
    elif network_mode == "lan":
        network_ok = bool(network.get("advertise_lan", False))
    elif network_mode == "manual":
        network_ok = _pairing_public_url().startswith("https://")
    failures += not network_ok
    print(f"{'OK' if network_ok else 'FAIL':<5} phone network: {network_mode}")
    for skill_id in selected_skills():
        for root in (CLAUDE_SKILLS, CODEX_SKILLS):
            path = root / skill_id
            ok = path.is_symlink() and (path / "SKILL.md").is_file()
            failures += not ok
            print(f"{'OK' if ok else 'FAIL':<5} skill link: {path}")
    return 1 if failures else 0


def cmd_paths(_args) -> int:
    from lib.deployment import DeploymentLayout
    from lib.paths import RuntimePaths

    layout = DeploymentLayout.from_environment()
    runtime_paths = RuntimePaths.from_home(HOME)
    print(json.dumps({
        "platform": service_manager.platform_kind(),
        "share": str(layout.share),
        "config": str(layout.config_file),
        "database": str(layout.state_database),
        "cache": str(layout.cache_dir),
        "logs": str(
            HOME / "Library/Logs/Clarp"
            if service_manager.platform_kind() == "macos"
            else layout.cache_dir / "logs"),
        "service": str(service_manager.definition_path(HOME)),
        "runtime_service": str(service_manager.runtime_definition_path(HOME)),
        "runtime_socket": str(runtime_paths.runtime_socket),
        "toolchain": str((layout.share / "toolchain").resolve(strict=False)),
    }, indent=2))
    return 0


def cmd_sessions(_args) -> int:
    from lib import db

    rows = db.conn().execute(
        """SELECT session,agent_id,persona,backend,cwd
             FROM agents WHERE deleted_at IS NULL ORDER BY persona,session"""
    ).fetchall()
    print(json.dumps([dict(row) for row in rows], indent=2))
    return 0


def cmd_backup(args) -> int:
    server_root = SHARE
    if not (server_root / "lib").is_dir():
        server_root = REPO / "server"
    sys.path.insert(0, str(server_root))
    from lib import instance_backup
    path = Path(args.path).expanduser() if getattr(args, "path", None) else None
    if args.backup_command == "create":
        result = instance_backup.create(path)
        print(result)
        return 0
    if args.backup_command == "verify":
        print(json.dumps(instance_backup.verify(path), indent=2))
        return 0
    if args.backup_command == "restore":
        if os.environ.get("CLARP_DEPLOYMENT_MODE") != "container":
            raise SystemExit("restart-applied restore is currently container-only")
        pending = instance_backup.stage_restore(path)
        print(f"restore staged at {pending}; restart this container to apply")
        return 0
    return 2


def cmd_onboard(args) -> int:
    cfg = tomllib.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    server = cfg.get("server", {})
    public_url = (args.url or os.environ.get("CLARP_PUBLIC_URL", "")).strip()
    if not public_url:
        host = str(server.get("bind_addr", "127.0.0.1"))
        if host in {"0.0.0.0", "::"}: host = "127.0.0.1"
        public_url = f"http://{host}:{int(server.get('port', 7682))}"
    server_root = SHARE
    if not (server_root / "lib").is_dir(): server_root = REPO / "server"
    sys.path.insert(0, str(server_root))
    from lib.server_identity import get_server_info
    info = get_server_info()
    if not str(server.get("auth_token") or "").strip():
        raise SystemExit("onboarding requires configured server authentication")
    from lib.device_pairing import issue
    record = issue(device_name="iPhone", scope="full")
    values = urllib.parse.urlencode({
        "name": args.name or info["name"], "url": public_url.rstrip("/"),
        "code": record["code"], "server_id": info["server_id"],
        "scope": record["scope"], "expires_at": record["expires_at"],
    })
    print(f"clarp://pair?{values}")
    return 0


def _pairing_public_url(explicit: str = "") -> str:
    cfg = tomllib.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    server = cfg.get("server", {})
    value = (explicit or str(server.get("public_base_url") or "")).strip()
    if value:
        return value.rstrip("/")
    if str(cfg.get("network", {}).get("mode", "off")) == "lan":
        hostname = socket.gethostname().strip().rstrip(".")
        if hostname:
            if not hostname.endswith(".local"):
                hostname += ".local"
            return f"http://{hostname}:{int(server.get('port', 7682))}"
    host = str(server.get("bind_addr", "127.0.0.1"))
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{int(server.get('port', 7682))}"


def cmd_pair(args) -> int:
    server_root = SHARE
    if not (server_root / "lib").is_dir():
        server_root = REPO / "server"
    sys.path.insert(0, str(server_root))
    from lib.device_pairing import issue, list_devices, revoke
    if args.pair_command == "list":
        print(json.dumps({"devices": list_devices(include_revoked=args.all)}, indent=2))
        return 0
    if args.pair_command == "revoke":
        if not revoke(args.device_id):
            raise SystemExit(f"paired device not found: {args.device_id}")
        print(f"revoked: {args.device_id}")
        return 0

    cfg = tomllib.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    if not str(cfg.get("server", {}).get("auth_token") or "").strip():
        raise SystemExit(
            "pairing requires server authentication; configure networking/auth "
            "in clarp-tui or set [server] auth_token, then restart Clarp")
    public_url = _pairing_public_url(args.url)
    parsed = urllib.parse.urlsplit(public_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SystemExit(
            "pairing requires a valid http:// or https:// Computer URL")
    if parsed.hostname in {"127.0.0.1", "::1", "localhost"} and not args.allow_loopback:
        raise SystemExit(
            "pairing URL is loopback-only; pass --url with the HTTPS/LAN address "
            "the iPhone can reach")
    record = issue(
        device_name=args.name, scope=args.scope, ttl_seconds=args.ttl)
    from lib.server_identity import get_server_info
    info = get_server_info()
    query = urllib.parse.urlencode({
        "name": info["name"], "url": public_url,
        # The exchange response is authoritative for server identity and scope.
        # Keeping the bootstrap URI compact makes a square terminal QR fit.
        "code": record["code"],
    })
    uri = f"clarp://pair?{query}"
    if args.json:
        print(json.dumps({**record, "url": public_url, "uri": uri}, indent=2))
        return 0
    import qrcode
    qr = qrcode.QRCode(border=1)
    qr.add_data(uri)
    qr.make(fit=True)
    qr.print_ascii(invert=True)
    print(uri)
    print(f"Expires in {args.ttl} seconds; one use only.")
    return 0


def _network_config() -> dict:
    try:
        value = tomllib.loads(CONFIG_FILE.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _ensure_network_auth() -> str:
    cfg = _network_config()
    token = str(cfg.get("server", {}).get("auth_token") or "").strip()
    if token:
        return token
    token = secrets.token_urlsafe(32)
    set_toml_value(CONFIG_FILE, "server", "auth_token", token)
    return token


def _tailscale_info() -> dict:
    executable = shutil.which("tailscale")
    if not executable:
        return {"installed": False, "online": False, "dns_name": ""}
    result = subprocess.run(
        [executable, "status", "--json"], text=True,
        capture_output=True, check=False)
    if result.returncode != 0:
        return {"installed": True, "online": False, "dns_name": "",
                "error": result.stderr.strip()}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = {}
    self_node = payload.get("Self", {}) if isinstance(payload, dict) else {}
    dns_name = str(self_node.get("DNSName") or "").strip().rstrip(".")
    return {
        "installed": True,
        "online": bool(self_node.get("Online", dns_name)),
        "dns_name": dns_name,
    }


def _tailscale_cleanup_expected() -> bool:
    marker = CONFIG_FILE.with_name("tailscale-serve.json")
    mode = str(_network_config().get("network", {}).get("mode", "off"))
    return marker.is_file() or mode == "tailscale"


def _tailscale_serve_status() -> dict:
    executable = shutil.which("tailscale")
    if not executable:
        if _tailscale_cleanup_expected():
            raise SystemExit(
                "Tailscale is unavailable, so Clarp cannot verify or remove "
                "its previously managed Serve route")
        return {}
    status = subprocess.run(
        [executable, "serve", "status", "--json"], text=True,
        capture_output=True, check=False)
    if status.returncode != 0:
        if not _tailscale_cleanup_expected():
            return {}
        raise SystemExit(
            "could not inspect Tailscale Serve before cleanup: "
            + (status.stderr.strip() or status.stdout.strip()))
    try:
        return json.loads(status.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise SystemExit("Tailscale Serve returned invalid status JSON") from exc


def _tailscale_serve_marker() -> dict:
    marker = CONFIG_FILE.with_name("tailscale-serve.json")
    try:
        value = json.loads(marker.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _managed_tailscale_serve_routes(
    listener_ports: set[int],
) -> dict[str, int]:
    """Map HTTPS ports to matching Clarp loopback listener ports."""
    payload = _tailscale_serve_status()
    expected = {
        f"http://127.0.0.1:{port}": port for port in listener_ports
    }
    routes: dict[str, int] = {}
    for host_port, web in (payload.get("Web") or {}).items():
        handlers = web.get("Handlers", {}) if isinstance(web, dict) else {}
        root = handlers.get("/", {}) if isinstance(handlers, dict) else {}
        proxy = root.get("Proxy") if isinstance(root, dict) else None
        if proxy in expected:
            parsed = urllib.parse.urlsplit(f"https://{host_port}")
            routes[str(parsed.port or 443)] = expected[proxy]
    return routes


def _managed_tailscale_serve_ports(port: int) -> set[str]:
    return set(_managed_tailscale_serve_routes({port}))


def _assert_tailscale_root_available(
    port: int, *, https_port: str = "443",
    allowed_proxy_ports: set[int] | None = None,
) -> None:
    """Refuse to overwrite another application's root Serve handler."""
    allowed = allowed_proxy_ports or {port}
    expected = {f"http://127.0.0.1:{candidate}" for candidate in allowed}
    for host_port, web in (_tailscale_serve_status().get("Web") or {}).items():
        parsed = urllib.parse.urlsplit(f"https://{host_port}")
        if str(parsed.port or 443) != https_port or not isinstance(web, dict):
            continue
        handlers = web.get("Handlers", {})
        root = handlers.get("/", {}) if isinstance(handlers, dict) else {}
        if root and (not isinstance(root, dict)
                     or root.get("Proxy") not in expected):
            raise SystemExit(
                f"Tailscale Serve HTTPS port {https_port} root is already "
                "managed by another application")


def _set_managed_tailscale_serve(
    port: int, *, https_port: str = "443", enabled: bool,
) -> None:
    executable = shutil.which("tailscale") or "tailscale"
    command = [
        executable, "serve", f"--https={https_port}", "--set-path=/",
    ]
    if enabled:
        command[2:2] = ["--bg"]
        command.append(f"http://127.0.0.1:{port}")
    else:
        command.append("off")
    result = subprocess.run(
        command, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        action = "restore" if enabled else "remove"
        raise SystemExit(
            f"could not {action} Clarp's Tailscale Serve route: "
            + (result.stderr.strip() or result.stdout.strip()))


def _remove_managed_tailscale_serve(
    port: int, *, https_ports: set[str] | None = None,
) -> None:
    """Remove only root Serve handlers that point at this Clarp port."""
    ports = (_managed_tailscale_serve_ports(port)
             if https_ports is None else https_ports)
    for https_port in sorted(ports):
        _set_managed_tailscale_serve(
            port, https_port=https_port, enabled=False)


def cmd_network(args) -> int:
    cfg = _network_config()
    network = cfg.get("network", {})
    server = cfg.get("server", {})
    if args.network_command == "status":
        mode = str(network.get("mode", "off"))
        print(json.dumps({
            "mode": mode,
            "advertise_lan": bool(network.get("advertise_lan", False)),
            "bind_addr": str(server.get("bind_addr", "127.0.0.1")),
            "port": int(server.get("port", 7682)),
            "public_base_url": str(server.get("public_base_url", "")),
            "pairing_url": _pairing_public_url() if mode != "off" else "",
            "auth_configured": bool(server.get("auth_token")),
            "tailscale": _tailscale_info(),
        }, indent=2))
        return 0

    mode = args.mode
    if mode == "manual":
        parsed = urllib.parse.urlsplit(args.url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise SystemExit("manual networking requires a valid https:// URL")
    port = int(server.get("port", 7682))
    previous_port = int(getattr(args, "previous_port", port) or port)
    previous_config = CONFIG_FILE.read_bytes()
    serve_marker = CONFIG_FILE.with_name("tailscale-serve.json")
    previous_marker = (serve_marker.read_bytes()
                       if serve_marker.is_file() else None)
    marker_state = _tailscale_serve_marker()
    marker_port = marker_state.get("listener_port")
    candidate_ports = {previous_port, port}
    if isinstance(marker_port, int) and not isinstance(marker_port, bool):
        candidate_ports.add(marker_port)
    existing_serve_routes = _managed_tailscale_serve_routes(candidate_ports)
    existing_serve_ports = set(existing_serve_routes)
    # Actual Serve state is authoritative for rollback even when an older
    # release left config.mode desynchronized from its live route.
    previous_serve_routes = dict(existing_serve_routes)
    serve_ports_to_remove = (existing_serve_ports
                             if mode != "tailscale" else set())
    created_serve_ports: set[str] = set()
    try:
        if mode != "off":
            _ensure_network_auth()
        if mode == "tailscale":
            info = _tailscale_info()
            if not info.get("installed"):
                raise SystemExit("Tailscale is not installed")
            if not info.get("online") or not info.get("dns_name"):
                raise SystemExit(
                    "Tailscale is not logged in or has no MagicDNS name")
            _assert_tailscale_root_available(
                port, allowed_proxy_ports=candidate_ports)
            if "443" not in existing_serve_ports:
                created_serve_ports.add("443")
            _set_managed_tailscale_serve(port, enabled=True)
            set_toml_value(CONFIG_FILE, "server", "bind_addr", "127.0.0.1")
            set_toml_value(
                CONFIG_FILE, "server", "public_base_url",
                f"https://{info['dns_name']}")
            set_toml_value(CONFIG_FILE, "network", "advertise_lan", False)
        elif mode == "lan":
            set_toml_value(CONFIG_FILE, "server", "bind_addr", "0.0.0.0")
            set_toml_value(CONFIG_FILE, "server", "public_base_url", "")
            set_toml_value(CONFIG_FILE, "network", "advertise_lan", True)
        elif mode == "manual":
            set_toml_value(CONFIG_FILE, "server", "bind_addr", "127.0.0.1")
            set_toml_value(
                CONFIG_FILE, "server", "public_base_url", args.url.rstrip("/"))
            set_toml_value(CONFIG_FILE, "network", "advertise_lan", False)
        else:
            set_toml_value(CONFIG_FILE, "server", "bind_addr", "127.0.0.1")
            set_toml_value(CONFIG_FILE, "server", "public_base_url", "")
            set_toml_value(CONFIG_FILE, "network", "advertise_lan", False)
        set_toml_value(CONFIG_FILE, "network", "mode", mode)
        service_manager.restart()
        if serve_ports_to_remove:
            _remove_managed_tailscale_serve(
                previous_port, https_ports=serve_ports_to_remove)
        if mode == "tailscale":
            write_json(serve_marker, {
                "listener_port": port,
                "https_ports": sorted(existing_serve_ports | {"443"}),
            })
        elif serve_marker.exists():
            serve_marker.unlink()
    except BaseException as original_error:
        CONFIG_FILE.write_bytes(previous_config)
        if previous_marker is None:
            if serve_marker.exists():
                serve_marker.unlink()
        else:
            serve_marker.write_bytes(previous_marker)
        rollback_errors: list[str] = []
        try:
            service_manager.restart()
        except Exception as exc:  # noqa: BLE001 - report with original failure
            rollback_errors.append(f"service restore: {exc}")

        def restore_route(https_port: str, *, enabled: bool, route_port: int) -> None:
            last_error = ""
            for _attempt in range(3):
                try:
                    _set_managed_tailscale_serve(
                        route_port, https_port=https_port, enabled=enabled)
                    return
                except (SystemExit, Exception) as exc:
                    last_error = str(exc)
                    time.sleep(0.25)
            action = "restore" if enabled else "remove"
            rollback_errors.append(
                f"Tailscale route {action} on HTTPS {https_port}: {last_error}")

        for https_port in sorted(created_serve_ports):
            restore_route(https_port, enabled=False, route_port=port)
        for https_port, route_port in sorted(previous_serve_routes.items()):
            restore_route(
                https_port, enabled=True, route_port=route_port)
        if rollback_errors:
            raise RuntimeError(
                f"network change failed ({original_error}); rollback also failed: "
                + "; ".join(rollback_errors)) from original_error
        raise
    print(json.dumps({"ok": True, "mode": mode,
                      "pairing_url": _pairing_public_url()}, indent=2))
    return 0


def cmd_tts(args) -> int:
    server_root = SHARE
    if not (server_root / "lib").is_dir():
        server_root = REPO / "server"
    sys.path.insert(0, str(server_root))
    from lib import tts_providers
    if args.tts_command == "adapters":
        from lib import custom_tts_adapters
        reserved = tts_providers.VALID_IDS
        action = args.adapter_command
        if action == "list":
            print(json.dumps({"adapters": custom_tts_adapters.inventory(
                reserved_ids=reserved)}, indent=2))
            return 0
        if action == "validate":
            manifest = custom_tts_adapters.load_manifest(
                args.path, reserved_ids=reserved, portable=True)
            print(json.dumps({"ok": True, "adapter": manifest.provider_row()}, indent=2))
            return 0
        if action == "install":
            manifest = custom_tts_adapters.install(
                args.path, reserved_ids=reserved, replace=args.replace)
            print(json.dumps({"ok": True, "installed": manifest.id}, indent=2))
            return 0
        if action == "remove":
            active = tts_providers.status()
            normalized = args.provider.strip().lower()
            if normalized in {active["provider"], active["fallback"]}:
                raise SystemExit(
                    "cannot remove the active primary or fallback adapter; "
                    "select a replacement first")
            custom_tts_adapters.remove(args.provider, reserved_ids=reserved)
            print(json.dumps({"ok": True, "removed": args.provider}, indent=2))
            return 0
        manifest = custom_tts_adapters.get(args.provider, reserved_ids=reserved)
        if manifest is None:
            raise SystemExit(f"custom voice adapter is not installed: {args.provider}")
        print(json.dumps(custom_tts_adapters.test_adapter(manifest), indent=2))
        return 0
    if args.tts_command == "status":
        print(json.dumps(tts_providers.status(), indent=2))
        return 0
    if args.tts_command == "configure":
        import getpass
        key = (sys.stdin.readline().strip() if args.stdin else
               getpass.getpass(f"{args.provider} API key: ").strip())
        if not key:
            raise SystemExit("API key cannot be empty")
        section = args.provider
        set_toml_value(CONFIG_FILE, section, "api_key", key)
        if (SHARE / "current").exists():
            service_manager.restart(check=False)
        print(f"configured {args.provider} credentials")
        return 0

    provider = args.provider
    fallback = args.fallback
    allowed = tts_providers.valid_ids()
    if provider not in allowed:
        raise SystemExit(f"unsupported TTS provider: {provider}")
    provider_rows = {
        row["id"]: row for row in tts_providers.status()["providers"]}
    if fallback not in allowed or (
            fallback != "none"
            and not provider_rows.get(fallback, {}).get("can_fallback", False)):
        raise SystemExit(f"unsupported TTS fallback: {fallback}")
    for selected in {provider, fallback} - {"none"}:
        if (provider_rows[selected]["kind"] == "local"
                and not provider_rows[selected]["installed"]):
            raise SystemExit(
                f"{selected} is not installed; "
                f"run clarp-admin tts install {selected}")
        if (provider_rows[selected].get("custom")
                and not provider_rows[selected]["available"]):
            raise SystemExit(
                f"custom TTS adapter is unavailable: {selected}; "
                f"run clarp-admin tts adapters test {selected}")
    set_toml_value(CONFIG_FILE, "tts", "provider", provider)
    set_toml_value(CONFIG_FILE, "tts", "fallback", fallback)
    if args.voice:
        set_toml_value(CONFIG_FILE, "local_tts", "voice", args.voice)
    if provider != "cartesia":
        set_toml_value(CONFIG_FILE, "audio", "delivery", "chunked-file")
    if (SHARE / "current").exists():
        service_manager.restart(check=False)
    print(json.dumps({"ok": True, "provider": provider,
                      "fallback": fallback}, indent=2))
    return 0


def parse_delay(value: str) -> int:
    units = {"s": 1, "m": 60, "h": 3600}
    if len(value) < 2 or value[-1] not in units:
        raise SystemExit("delay must look like 30s, 5m, or 2h")
    return int(value[:-1]) * units[value[-1]]


def cmd_prompt(args) -> int:
    if args.from_session and args.origin:
        raise SystemExit("--origin cannot be combined with --from")
    if args.delay:
        executable = shutil.which("clarp-admin") or str(Path(__file__).resolve())
        command = [executable, "prompt", "--to", args.to, "--text", args.text]
        if args.from_session: command += ["--from", args.from_session]
        if args.origin: command += ["--origin", args.origin]
        if args.server: command += ["--server", args.server]
        ok, error = service_manager.launch_detached(
            command, unit=f"clarp-prompt-{secrets.token_hex(6)}",
            delay_seconds=parse_delay(args.delay))
        if not ok:
            raise SystemExit(error or "could not schedule prompt")
        print(f"scheduled prompt to {args.to} after {args.delay}")
        return 0
    origin = "agent" if args.from_session else (args.origin or "automation")
    payload = {
        "session": args.to, "text": args.text, "force_session": True,
        "synthesize_audio": False, "hands_free": False, "origin": origin,
    }
    if args.from_session: payload["sender"] = args.from_session
    if args.server:
        server_root = SHARE
        if not (server_root / "lib").is_dir(): server_root = REPO / "server"
        sys.path.insert(0, str(server_root))
        from lib.server_peers import send
        result = send(args.server, payload)
    else:
        result = api_request("POST", "/send", payload)
    print(json.dumps(result, indent=2))
    return 0


def cmd_peers(args) -> int:
    server_root = SHARE
    if not (server_root / "lib").is_dir(): server_root = REPO / "server"
    sys.path.insert(0, str(server_root))
    from lib import server_peers
    if args.peers_command == "list":
        print(json.dumps(server_peers.list_public(), indent=2))
    elif args.peers_command == "add":
        print(json.dumps(server_peers.add(args.name, args.url, args.token), indent=2))
    elif args.peers_command == "remove":
        server_peers.remove(args.name)
        print(f"removed peer: {args.name}")
    return 0


def cmd_repos(args) -> int:
    server_root = SHARE
    if not (server_root / "lib").is_dir(): server_root = REPO / "server"
    sys.path.insert(0, str(server_root))
    from lib import workspace_repos
    if args.repos_command == "list":
        result = workspace_repos.list_repositories()
    elif args.repos_command == "clone":
        result = workspace_repos.clone(args.url, name=args.name, ref=args.ref)
    else:
        result = workspace_repos.health(args.name)
    print(json.dumps(result, indent=2))
    return 0


def cmd_location(args) -> int:
    query = urllib.parse.urlencode({"session": args.session})
    current = api_request("GET", f"/location?{query}")
    now_ms = int(time.time() * 1000)
    if current.get("ts") and now_ms - int(current["ts"]) <= args.max_age * 1000:
        print(json.dumps(current, indent=2)); return 0
    started = now_ms
    api_request("POST", "/location/request", {"session": args.session})
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        time.sleep(2)
        current = api_request("GET", f"/location?{query}")
        if current.get("ts") and int(current["ts"]) >= started:
            print(json.dumps(current, indent=2)); return 0
    raise SystemExit("location was not shared before timeout")


def cmd_calendar(args) -> int:
    payload = {
        "session": args.session, "title": args.title,
        "start": args.start, "end": args.end,
        "time_zone": args.time_zone, "location": args.location,
        "notes": args.notes, "url": args.url, "calendar": args.calendar,
        "all_day": args.all_day,
    }
    print(json.dumps(api_request("POST", "/calendar/request", payload), indent=2))
    return 0


def releases() -> list[Path]:
    root = SHARE / "releases"
    return sorted((p for p in root.iterdir()
                   if p.is_dir() and (p / "INSTALL_OK").is_file()),
                  key=lambda p: p.stat().st_mtime, reverse=True) if root.is_dir() else []


def activate_release(release: Path) -> None:
    if not (release / "server.py").is_file():
        raise SystemExit(f"invalid Clarp release: {release}")
    current = SHARE / "current"
    previous = current.resolve() if current.exists() else None
    live_unit = service_manager.definition_path(HOME)
    previous_unit = live_unit.read_bytes() if live_unit.is_file() else None
    try:
        service_python = Path((release / "SERVICE_PYTHON").read_text().strip())
        service_path = (release / "SERVICE_PATH").read_text().strip()
    except OSError as exc:
        raise SystemExit(
            f"release has no service runtime metadata: {release}") from exc
    temporary = SHARE / ".current.next"
    if temporary.is_symlink(): temporary.unlink()
    temporary.symlink_to(release, target_is_directory=True)
    temporary.replace(current)
    try:
        try:
            toolchain_dir = (release / "TOOLCHAIN_DIR").read_text().strip()
        except OSError:
            toolchain_dir = ""
        if toolchain_dir:
            toolchain_link = SHARE / "toolchain"
            replacement = SHARE / ".toolchain.next"
            replacement.unlink(missing_ok=True)
            replacement.symlink_to(toolchain_dir, target_is_directory=True)
            replacement.replace(toolchain_link)
        else:
            toolchain_link = SHARE / "toolchain"
            if toolchain_link.is_symlink():
                toolchain_link.unlink()
        live_unit.parent.mkdir(parents=True, exist_ok=True)
        service_manager.write_definition(
            python=service_python, share=SHARE, service_path=service_path,
            home=HOME)
        cmd_skills(argparse.Namespace(skills_command="repair-links"))
        service_manager.install_and_restart()
    except BaseException:
        if previous and previous.is_dir():
            fallback = SHARE / ".current.rollback"
            if fallback.is_symlink(): fallback.unlink()
            fallback.symlink_to(previous, target_is_directory=True)
            fallback.replace(current)
            try:
                previous_toolchain = (previous / "TOOLCHAIN_DIR").read_text().strip()
            except OSError:
                previous_toolchain = ""
            if previous_toolchain:
                replacement = SHARE / ".toolchain.rollback"
                replacement.unlink(missing_ok=True)
                replacement.symlink_to(
                    previous_toolchain, target_is_directory=True)
                replacement.replace(SHARE / "toolchain")
            elif (SHARE / "toolchain").is_symlink():
                (SHARE / "toolchain").unlink()
            if previous_unit is not None:
                live_unit.write_bytes(previous_unit)
            elif live_unit.exists():
                live_unit.unlink()
            service_manager.restore_after_failed_install(
                had_previous=previous_unit is not None)
            try:
                cmd_skills(argparse.Namespace(skills_command="repair-links"))
            except BaseException:
                # Rollback must preserve the activation failure that triggered
                # it; link repair is best-effort while the prior release is
                # being restored.
                pass
        raise


def cmd_rollback(args) -> int:
    available = releases()
    if not available:
        raise SystemExit("no installed releases")
    current = (SHARE / "current").resolve() if (SHARE / "current").exists() else None
    candidates = [item for item in available if item.resolve() != current]
    target = next((item for item in candidates if item.name == args.version), None) \
        if args.version else (candidates[0] if candidates else None)
    if target is None:
        raise SystemExit("no previous matching release")
    activate_release(target)
    print(f"rolled back to {target.name}")
    return 0


def cmd_update(args) -> int:
    state = read_json(INSTALL_STATE, {})
    source = Path(state.get("source_repo", REPO))
    if not (source / ".git").exists() and not (source / ".git").is_file():
        source = SHARE / "update-source"
        if not (source / ".git").exists():
            remote = resolve_update_remote(state)
            source.parent.mkdir(parents=True, exist_ok=True)
            run("git", "clone", "--filter=blob:none", remote, str(source))
    # Quick-start clones are shallow. Refresh remote branch heads explicitly as
    # well as tags so the no-tag fallback cannot silently reinstall stale main.
    run("git", "fetch", "--tags", "--prune", "origin",
        "+refs/heads/*:refs/remotes/origin/*", cwd=source)
    ref = args.ref
    if not ref and state.get("channel", "stable") == "stable":
        tags = stable_release_tags(subprocess.run(
            ["git", "tag", "--list", "v*", "--sort=-version:refname"],
            cwd=source, text=True, capture_output=True, check=True).stdout.splitlines())
        ref = tags[0] if tags else "origin/main"
    ref = ref or "origin/main"
    with tempfile.TemporaryDirectory(prefix="clarp-update-") as temporary:
        worktree = Path(temporary) / "release"
        run("git", "worktree", "add", "--detach", str(worktree), ref, cwd=source)
        try:
            env = os.environ.copy()
            env["CLARP_TOOLCHAIN_MODE"] = str(
                state.get("toolchain") or "existing")
            run(str(worktree / "install.sh"), cwd=worktree, env=env)
        finally:
            run("git", "worktree", "remove", "--force", str(worktree),
                cwd=source, check=False)
    cmd_skills(argparse.Namespace(skills_command="repair-links"))
    print(f"updated Clarp from {ref}")
    return 0


def cmd_uninstall(args) -> int:
    cfg = _network_config()
    try:
        listener_ports = {int(cfg.get("server", {}).get("port", 7682))}
        marker_port = _tailscale_serve_marker().get("listener_port")
        if isinstance(marker_port, int) and not isinstance(marker_port, bool):
            listener_ports.add(marker_port)
        routes = _managed_tailscale_serve_routes(listener_ports)
        if routes:
            _remove_managed_tailscale_serve(
                next(iter(listener_ports)), https_ports=set(routes))
        marker = CONFIG_FILE.with_name("tailscale-serve.json")
        if marker.exists():
            marker.unlink()
    except (SystemExit, Exception) as exc:
        if not getattr(args, "force_network_cleanup", False):
            raise SystemExit(
                f"could not remove Clarp's network route: {exc}. "
                "Restore Tailscale and retry, or pass --force-network-cleanup "
                "to accept the dangling-route risk") from exc
        print(f"warning: {exc}; continuing forced uninstall", file=sys.stderr)
    service_manager.stop_and_disable()
    for skill_id in selected_skills(): unlink_skill(skill_id)
    # Clarp installs exactly one thing outside its own directories.
    for root, names in ((HOME / ".local/bin", [
            "clarp-admin", "clarp-tui", "clarp-agent-tasks", "clarp-agent-artifacts",
            "clarp-media-publish", "clarp-agent-bg", "clarp-message-watch",
            "clarp-github-workflow-artifact"]),):
        for name in names:
            path = root / name
            managed = path.is_symlink() and path.resolve().is_relative_to(
                SHARE.resolve())
            if path.is_file() and not path.is_symlink():
                try:
                    managed = "# managed-by-clarp" in path.read_text()[:256]
                except OSError:
                    managed = False
            if managed:
                path.unlink()
    unit = service_manager.definition_path(HOME)
    if unit.is_file(): unit.unlink()
    service_manager.reload_definitions()
    for name in ("current", "server.py", "lib", "static", "scripts", "skills",
                 "plugin", "systemd", "docs", "requirements.txt",
                 "pyproject.toml", "uv.lock", "DEPLOYED_VERSION",
                 "DEPLOYED_RELEASE_ID", "SOURCE_REMOTE"):
        path = SHARE / name
        if path.is_symlink(): path.unlink()
    if (SHARE / "releases").is_dir(): shutil.rmtree(SHARE / "releases")
    toolchain_link = SHARE / "toolchain"
    if toolchain_link.is_symlink():
        toolchain_link.unlink()
    for runtime_dir in (SHARE / "environments", SHARE / "toolchains"):
        if runtime_dir.is_dir():
            shutil.rmtree(runtime_dir)
    if (SHARE / "bin").is_dir():
        shutil.rmtree(SHARE / "bin")
    if args.purge_data:
        if SHARE.is_dir(): shutil.rmtree(SHARE)
        if CONFIG_DIR.is_dir(): shutil.rmtree(CONFIG_DIR)
        print("removed Clarp runtime, configuration, and conversation data")
    else:
        print("removed Clarp runtime; preserved configuration and conversation data")
    return 0


def cmd_schedule(args) -> int:
    cmd = args.schedule_command
    if cmd == "list":
        query = f"?session={urllib.parse.quote(args.session)}" if getattr(args, "session", None) else ""
        res = api_request("GET", f"/agent-schedules{query}")
        schedules = res.get("schedules", [])
        if not schedules:
            print("No scheduled jobs found.")
            return 0
        for s in schedules:
            status = "ENABLED" if s.get("enabled") else "DISABLED"
            print(f"• [{status}] {s.get('name')} (ID: {s.get('schedule_id')})")
            print(f"   Session: {s.get('session')} | Cron: {s.get('cron_expression')}")
            print(f"   Prompt:  {s.get('prompt')}")
            if s.get("next_run_at"):
                dt = datetime.fromtimestamp(s["next_run_at"] / 1000.0, tz=timezone.utc)
                print(f"   Next:    {dt.strftime('%Y-%m-%d %H:%M:%S UTC')}")
            print("")
        return 0
    elif cmd == "add":
        payload = {
            "session": args.session,
            "name": args.name,
            "cron_expression": args.cron,
            "prompt": args.prompt,
            "enabled": not args.disabled,
        }
        res = api_request("POST", "/agent-schedules", payload)
        sched = res.get("schedule", {})
        print(f"✓ Created schedule '{sched.get('name')}' ({sched.get('schedule_id')}) for {args.session}")
        return 0
    elif cmd == "toggle":
        if not args.enable and not args.disable:
            raise SystemExit("Must specify --enable or --disable")
        payload = {
            "schedule_id": args.schedule_id,
            "enabled": True if args.enable else False,
        }
        api_request("POST", "/agent-schedules/toggle", payload)
        print(f"✓ Schedule {args.schedule_id} {'enabled' if args.enable else 'disabled'}")
        return 0
    elif cmd == "remove":
        payload = {"schedule_id": args.schedule_id}
        api_request("POST", "/agent-schedules/delete", payload)
        print(f"✓ Schedule {args.schedule_id} removed")
        return 0
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="clarp-admin", description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    setup = sub.add_parser(
        "setup",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Install or reconfigure Clarp through the shared setup engine.\n"
            "Without --non-interactive, this command asks terminal questions.\n"
            "For the graphical terminal wizard, run: clarp-tui"),
        epilog="""examples:
  # Interactive CLI questions
  clarp-admin setup

  # Textual TUI using the same setup engine
  clarp-tui

  # Complete unattended installation for automation or an AI agent
  clarp-admin setup --non-interactive \\
    --backend both \\
    --toolchain managed \\
    --transcription recommended \\
    --tts cartesia --tts-fallback none \\
    --network tailscale \\
    --optional-skill clarp-calendar \\
    --optional-skill clarp-location

First installation from a source checkout should normally use ./setup.sh,
which creates the locked Python environment before invoking this command.
Run ./setup.sh --help to see TUI, interactive CLI, and automation routes.
""")
    setup.add_argument(
        "--non-interactive", action="store_true",
        help="disable all prompts; requires explicit toolchain and transcription")
    setup.add_argument(
        "--backend", choices=("claude", "codex", "both"), default="both",
        help="agent backends to enable (default: both)")
    setup.add_argument(
        "--transcription", default="",
        help="recommended, none, or a supported model id")
    setup.add_argument("--toolchain", choices=("managed", "existing", "none"),
                       default="",
                       help="pinned Clarp tools, PATH tools, or deferred setup")
    setup.add_argument(
        "--tts", choices=("clarp", "cartesia", "elevenlabs", "deepgram",
                          "none"), default="",
        help="primary voice-output provider")
    setup.add_argument(
        "--tts-fallback",
        choices=("clarp", "cartesia", "elevenlabs", "deepgram", "none"),
        default="none", help="explicit voice fallback (default: none)")
    setup.add_argument(
        "--network", choices=("tailscale", "lan", "manual", "off"),
        default="", help="phone networking mode")
    setup.add_argument(
        "--public-url", default="",
        help="existing HTTPS address; required for --network manual")
    setup.add_argument(
        "--bind", help="server bind address; existing value is preserved when omitted")
    setup.add_argument(
        "--port", type=int,
        help="server port; existing value is preserved when omitted")
    setup.add_argument(
        "--optional-skill", action="append", default=[], metavar="SKILL_ID",
        help="optional skill to enable; repeat for multiple skills")
    setup.add_argument(
        "--channel", choices=("stable", "development"), default="stable",
        help="update channel (default: stable)")
    setup.set_defaults(func=cmd_setup)

    skills = sub.add_parser("skills").add_subparsers(dest="skills_command", required=True)
    skills.add_parser("list").set_defaults(func=cmd_skills)
    skills.add_parser("repair-links").set_defaults(func=cmd_skills)
    skills_import = skills.add_parser("import")
    skills_import.add_argument("path")
    skills_import.add_argument("--replace", action="store_true")
    skills_import.set_defaults(func=cmd_skills)
    source_add = skills.add_parser("source-add")
    source_add.add_argument("url")
    source_add.add_argument("--name", default="")
    source_add.add_argument("--ref", default="")
    source_add.set_defaults(func=cmd_skills)
    source_update = skills.add_parser("source-update")
    source_update.add_argument("name")
    source_update.set_defaults(func=cmd_skills)
    for name in ("install", "remove"):
        command = skills.add_parser(name)
        command.add_argument("skill_ids", nargs="+")
        command.set_defaults(func=cmd_skills)

    transcription = sub.add_parser("transcription").add_subparsers(
        dest="transcription_command", required=True)
    transcription.add_parser("list").set_defaults(func=cmd_transcription)
    transcription.add_parser("test").set_defaults(func=cmd_transcription)
    for name in ("install", "remove", "use"):
        command = transcription.add_parser(name)
        command.add_argument("model_id")
        command.set_defaults(func=cmd_transcription)
    import_model = transcription.add_parser("import")
    import_model.add_argument(
        "--provider",
        choices=("faster-whisper", "whisper.cpp"),
        required=True)
    import_model.add_argument("--model", required=True)
    import_model.add_argument("--path", required=True)
    import_model.add_argument("--runtime-path", default="")
    import_model.set_defaults(func=cmd_transcription)
    stt_adapters = transcription.add_parser("adapters").add_subparsers(
        dest="adapter_command", required=True)
    stt_adapters.add_parser("list").set_defaults(func=cmd_transcription)
    stt_validate = stt_adapters.add_parser("validate")
    stt_validate.add_argument("path")
    stt_validate.set_defaults(func=cmd_transcription)
    stt_install = stt_adapters.add_parser("install")
    stt_install.add_argument("path")
    stt_install.add_argument("--replace", action="store_true")
    stt_install.set_defaults(func=cmd_transcription)
    stt_test = stt_adapters.add_parser("test")
    stt_test.add_argument("provider")
    stt_test.set_defaults(func=cmd_transcription)
    stt_remove = stt_adapters.add_parser("remove")
    stt_remove.add_argument("provider")
    stt_remove.set_defaults(func=cmd_transcription)

    sub.add_parser("doctor").set_defaults(func=cmd_doctor)
    url = sub.add_parser(
        "url", help="print the PWA link that carries the auth token")
    url.add_argument("--qr", action="store_true", help="also print a QR code")
    url.add_argument("--json", action="store_true")
    url.set_defaults(func=cmd_url)
    sub.add_parser("paths").set_defaults(func=cmd_paths)
    sub.add_parser("sessions").set_defaults(func=cmd_sessions)
    onboard = sub.add_parser("onboard")
    onboard.add_argument("--url", default="")
    onboard.add_argument("--name", default="")
    onboard.set_defaults(func=cmd_onboard)

    pair = sub.add_parser("pair").add_subparsers(
        dest="pair_command", required=True)
    pair_create = pair.add_parser("create")
    pair_create.add_argument("--url", default="")
    pair_create.add_argument("--name", default="iPhone")
    pair_create.add_argument("--scope", choices=("full", "limited"),
                             default="full")
    pair_create.add_argument("--ttl", type=int, default=600)
    pair_create.add_argument("--allow-loopback", action="store_true")
    pair_create.add_argument("--json", action="store_true")
    pair_create.set_defaults(func=cmd_pair)
    pair_list = pair.add_parser("list")
    pair_list.add_argument("--all", action="store_true")
    pair_list.set_defaults(func=cmd_pair)
    pair_revoke = pair.add_parser("revoke")
    pair_revoke.add_argument("device_id")
    pair_revoke.set_defaults(func=cmd_pair)

    network = sub.add_parser("network").add_subparsers(
        dest="network_command", required=True)
    network.add_parser("status").set_defaults(func=cmd_network)
    network_use = network.add_parser("use")
    network_use.add_argument(
        "mode", choices=("tailscale", "lan", "manual", "off"))
    network_use.add_argument("--url", default="")
    network_use.set_defaults(func=cmd_network)

    tts = sub.add_parser("tts").add_subparsers(
        dest="tts_command", required=True)
    tts.add_parser("status").set_defaults(func=cmd_tts)
    tts_use = tts.add_parser("use")
    tts_use.add_argument("provider")
    tts_use.add_argument("--fallback", default="none")
    tts_use.add_argument("--voice", default="")
    tts_use.set_defaults(func=cmd_tts)
    tts_configure = tts.add_parser("configure")
    tts_configure.add_argument(
        "provider", choices=("cartesia", "elevenlabs", "deepgram"))
    tts_configure.add_argument("--stdin", action="store_true")
    tts_configure.set_defaults(func=cmd_tts)
    adapters = tts.add_parser("adapters").add_subparsers(
        dest="adapter_command", required=True)
    adapters.add_parser("list").set_defaults(func=cmd_tts)
    adapters_validate = adapters.add_parser("validate")
    adapters_validate.add_argument("path")
    adapters_validate.set_defaults(func=cmd_tts)
    adapters_install = adapters.add_parser("install")
    adapters_install.add_argument("path")
    adapters_install.add_argument("--replace", action="store_true")
    adapters_install.set_defaults(func=cmd_tts)
    adapters_test = adapters.add_parser("test")
    adapters_test.add_argument("provider")
    adapters_test.set_defaults(func=cmd_tts)
    adapters_remove = adapters.add_parser("remove")
    adapters_remove.add_argument("provider")
    adapters_remove.set_defaults(func=cmd_tts)
    backup = sub.add_parser("backup").add_subparsers(
        dest="backup_command", required=True)
    backup_create = backup.add_parser("create")
    backup_create.add_argument("path", nargs="?")
    backup_create.set_defaults(func=cmd_backup)
    for name in ("verify", "restore"):
        command = backup.add_parser(name)
        command.add_argument("path")
        command.set_defaults(func=cmd_backup)
    update = sub.add_parser("update")
    update.add_argument("--ref")
    update.set_defaults(func=cmd_update)
    rollback = sub.add_parser("rollback")
    rollback.add_argument("version", nargs="?")
    rollback.set_defaults(func=cmd_rollback)
    uninstall = sub.add_parser("uninstall")
    uninstall.add_argument("--purge-data", action="store_true")
    uninstall.add_argument("--force-network-cleanup", action="store_true")
    uninstall.set_defaults(func=cmd_uninstall)
    prompt_cmd = sub.add_parser("prompt")
    prompt_cmd.add_argument("--to", required=True)
    prompt_cmd.add_argument("--from", dest="from_session")
    prompt_cmd.add_argument("--text", required=True)
    prompt_cmd.add_argument("--delay")
    prompt_cmd.add_argument("--origin", choices=("automation", "watcher"))
    prompt_cmd.add_argument("--server", help="explicit configured peer name")
    prompt_cmd.set_defaults(func=cmd_prompt)
    peers = sub.add_parser("peers").add_subparsers(
        dest="peers_command", required=True)
    peers.add_parser("list").set_defaults(func=cmd_peers)
    peer_add = peers.add_parser("add")
    peer_add.add_argument("name")
    peer_add.add_argument("url")
    peer_add.add_argument("--token", required=True)
    peer_add.set_defaults(func=cmd_peers)
    peer_remove = peers.add_parser("remove")
    peer_remove.add_argument("name")
    peer_remove.set_defaults(func=cmd_peers)
    repos = sub.add_parser("repos").add_subparsers(
        dest="repos_command", required=True)
    repos.add_parser("list").set_defaults(func=cmd_repos)
    repo_clone = repos.add_parser("clone")
    repo_clone.add_argument("url")
    repo_clone.add_argument("--name", default="")
    repo_clone.add_argument("--ref", default="")
    repo_clone.set_defaults(func=cmd_repos)
    repo_health = repos.add_parser("health")
    repo_health.add_argument("name")
    repo_health.set_defaults(func=cmd_repos)
    location = sub.add_parser("location")
    location.add_argument("--session", required=True)
    location.add_argument("--max-age", type=int, default=300)
    location.add_argument("--timeout", type=int, default=35)
    location.set_defaults(func=cmd_location)
    calendar = sub.add_parser("calendar")
    calendar.add_argument("--session", required=True)
    calendar.add_argument("--title", required=True)
    calendar.add_argument("--start", required=True)
    calendar.add_argument("--end", required=True)
    calendar.add_argument("--time-zone", default="")
    calendar.add_argument("--location", default="")
    calendar.add_argument("--notes", default="")
    calendar.add_argument("--url", default="")
    calendar.add_argument("--calendar", default="")
    calendar.add_argument("--all-day", action="store_true")
    calendar.set_defaults(func=cmd_calendar)

    schedule = sub.add_parser("schedule").add_subparsers(
        dest="schedule_command", required=True)
    sched_list = schedule.add_parser("list")
    sched_list.add_argument("--session", "-s", default=None, help="filter by session")
    sched_list.set_defaults(func=cmd_schedule)

    sched_add = schedule.add_parser("add")
    sched_add.add_argument("session", help="agent session name")
    sched_add.add_argument("--name", "-n", required=True, help="name for the scheduled job")
    sched_add.add_argument("--cron", "-c", required=True, help="cron expression (e.g. '0 8:30 * * 1-5' or '@daily')")
    sched_add.add_argument("--prompt", "-p", required=True, help="prompt to dispatch on schedule")
    sched_add.add_argument("--disabled", action="store_true", help="create initially disabled")
    sched_add.set_defaults(func=cmd_schedule)

    sched_toggle = schedule.add_parser("toggle")
    sched_toggle.add_argument("schedule_id", help="schedule ID")
    sched_toggle.add_argument("--enable", action="store_true", help="enable schedule")
    sched_toggle.add_argument("--disable", action="store_true", help="disable schedule")
    sched_toggle.set_defaults(func=cmd_schedule)

    sched_rm = schedule.add_parser("remove")
    sched_rm.add_argument("schedule_id", help="schedule ID to remove")
    sched_rm.set_defaults(func=cmd_schedule)

    return result


def main() -> int:
    args = parser().parse_args()
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())

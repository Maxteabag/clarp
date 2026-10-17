"""A legacy custom TTS adapter whose id is "google" (valid at Host contract 64) against the built-in
Google provider (contract 69): detection, refusal, the catalog shape and the operator resolution.

Everything runs on owned temporary roots. Adapter executables are never run, Google is never called
(its synthesis and credentials are spied), and no real restart, provider or live config is touched.
"""
from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import pathlib
import stat
import sys
import tomllib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "server"))
from lib import config, custom_tts_adapters, google_tts, tts_mode, tts_providers, tts_worker  # noqa: E402

SPEC = importlib.util.spec_from_file_location("collision_admin", REPO / "bin/clarp-admin.py")
admin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(admin)

COLLISION, UNVERIFIABLE = "google_id_collision", "google_id_unverifiable"
CHIRP = "nb-NO-Chirp3-HD-Aoede"
LEGACY = {"schema_version": 1, "id": "google", "name": "Legacy Google", "description": "a 64 adapter",
          "executable": "./adapter", "operations": ["voices", "preview", "synthesize"],
          "audio_format": "audio/mpeg", "default_voice": "legacy-voice", "can_fallback": True}


@pytest.fixture
def root(tmp_path, monkeypatch):
    path = tmp_path / "tts-adapters.d"
    path.mkdir()
    monkeypatch.setattr(custom_tts_adapters, "ROOT", path)
    return path


def _cfg(monkeypatch, **kwargs):
    kwargs.setdefault("tts_provider", "google")
    kwargs.setdefault("tts_fallback", "none")
    cfg = config.Config(**kwargs)
    monkeypatch.setattr(config, "_CACHED", cfg)
    return cfg


def _package(root, folder="legacy-google", manifest=None, executable=b"#!/bin/sh\nexit 1\n"):
    package = root / folder
    package.mkdir(parents=True)
    (package / "adapter").write_bytes(executable)            # present, never executed
    (package / "adapter").chmod(0o755)
    (package / "manifest.json").write_text(
        manifest if isinstance(manifest, str) else json.dumps(manifest or LEGACY))
    return package


def _state(root):
    return custom_tts_adapters.collision_inventory(root)


# ---- ROOT categories (a safety classification; generic discovery is unchanged) -------------

def test_an_absent_root_is_complete_with_no_collision(tmp_path, monkeypatch):
    _cfg(monkeypatch)
    missing = tmp_path / "missing"
    monkeypatch.setattr(custom_tts_adapters, "ROOT", missing)
    inventory = _state(missing)
    assert inventory["root_category"] == "absent" and inventory["complete"] is True
    assert google_tts.collision_code() is None


def test_an_empty_root_is_complete_with_no_collision(root, monkeypatch):
    _cfg(monkeypatch)
    assert _state(root)["complete"] is True
    assert google_tts.collision_code() is None


def test_a_symlinked_root_is_unverifiable_and_never_followed(tmp_path, monkeypatch):
    _cfg(monkeypatch)
    target = tmp_path / "elsewhere"
    _package(target)
    link = tmp_path / "linked-root"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(custom_tts_adapters, "ROOT", link)
    real_open = os.open
    opened = []

    def spy(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(os, "open", spy)
    inventory = _state(link)
    assert inventory["root_category"] == "symlink" and inventory["complete"] is False
    assert not [p for p in opened if str(target) in p]
    assert google_tts.collision_code() == UNVERIFIABLE


def test_a_root_that_is_not_a_directory_is_unverifiable(tmp_path, monkeypatch):
    _cfg(monkeypatch)
    path = tmp_path / "tts-adapters.d"
    path.write_text("not a directory")
    monkeypatch.setattr(custom_tts_adapters, "ROOT", path)
    assert _state(path)["root_category"] == "not_dir"
    assert google_tts.collision_code() == UNVERIFIABLE


def test_an_unreadable_root_is_unverifiable(root, monkeypatch):
    _cfg(monkeypatch)
    root.chmod(0)
    try:
        if os.access(root, os.R_OK):
            pytest.skip("running with privileges that read a 0000 directory")
        assert _state(root)["root_category"] == "unreadable"
        assert google_tts.collision_code() == UNVERIFIABLE
    finally:
        root.chmod(0o700)


# ---- candidate categories ------------------------------------------------------------------

@pytest.mark.parametrize("manifest, expected", [
    ({**LEGACY, "id": "acme"}, None),
    ({**LEGACY, "id": "cartesia"}, None),
    ({k: v for k, v in LEGACY.items() if k != "id"}, None),
    ({**LEGACY, "id": "google"}, COLLISION),
    ({**LEGACY, "id": " Google "}, COLLISION),
    ({**LEGACY, "id": 123}, UNVERIFIABLE),
    ({**LEGACY, "id": ""}, UNVERIFIABLE),
    ("{not json", UNVERIFIABLE),
    ("[1, 2]", UNVERIFIABLE),
], ids=["other-id", "already-reserved-id", "id-absent", "google", "mixed-case-google",
        "non-string-id", "empty-id", "parse-error", "not-an-object"])
def test_each_manifest_is_classified(root, monkeypatch, manifest, expected):
    _cfg(monkeypatch)
    _package(root, manifest=manifest)
    assert google_tts.collision_code() == expected


def test_an_oversize_manifest_is_unverifiable(root, monkeypatch):
    _cfg(monkeypatch)
    _package(root, manifest=json.dumps({**LEGACY, "pad": "x" * custom_tts_adapters.MAX_RESPONSE_BYTES}))
    assert google_tts.collision_code() == UNVERIFIABLE


def test_an_unreadable_manifest_is_unverifiable(root, monkeypatch):
    _cfg(monkeypatch)
    package = _package(root)
    (package / "manifest.json").chmod(0)
    try:
        if os.access(package / "manifest.json", os.R_OK):
            pytest.skip("running with privileges that read a 0000 file")
        assert google_tts.collision_code() == UNVERIFIABLE
    finally:
        (package / "manifest.json").chmod(0o600)


def test_a_symlinked_manifest_or_package_is_unverifiable(root, tmp_path, monkeypatch):
    _cfg(monkeypatch)
    elsewhere = _package(tmp_path / "outside")
    linked = root / "linked"
    linked.mkdir()
    (linked / "manifest.json").symlink_to(elsewhere / "manifest.json")
    assert google_tts.collision_code() == UNVERIFIABLE
    (linked / "manifest.json").unlink()
    linked.rmdir()
    (root / "linked-package").symlink_to(elsewhere, target_is_directory=True)
    assert google_tts.collision_code() == UNVERIFIABLE


def test_a_root_level_json_manifest_is_a_candidate(root, monkeypatch):
    _cfg(monkeypatch)
    (root / "legacy.json").write_text(json.dumps(LEGACY))
    assert google_tts.collision_code() == COLLISION


def test_a_folder_without_a_manifest_is_not_a_candidate(root, monkeypatch):
    _cfg(monkeypatch)
    (root / "notes").mkdir()
    assert google_tts.collision_code() is None


# ---- fingerprints ----------------------------------------------------------------------------

def test_fingerprint_parts_are_length_prefixed():
    digest = custom_tts_adapters._collision_digest
    assert digest([b"ab", b"c"]) != digest([b"a", b"bc"])


def _fingerprint(root):
    [collision] = _state(root)["collisions"]
    return collision["fingerprint"]


def test_the_fingerprint_binds_manifest_and_executable_bytes(root):
    package = _package(root)
    first = _fingerprint(root)
    (package / "adapter").write_bytes(b"#!/bin/sh\nexit 2\n")
    second = _fingerprint(root)
    (package / "manifest.json").write_text(json.dumps({**LEGACY, "name": "Renamed"}))
    third = _fingerprint(root)
    assert len({first, second, third}) == 3


@pytest.mark.parametrize("layout", ["final", "intermediate"])
def test_a_symlinked_executable_is_fingerprinted_by_its_link_never_followed(root, tmp_path, monkeypatch, layout):
    outside = tmp_path / "outside-bin"
    outside.mkdir()
    (outside / "adapter").write_bytes(b"outside bytes")
    executable = "./adapter" if layout == "final" else "./bin/adapter"
    package = _package(root, manifest={**LEGACY, "executable": executable})
    (package / "adapter").unlink()
    if layout == "final":
        (package / "adapter").symlink_to(outside / "adapter")
    else:
        (package / "bin").symlink_to(outside, target_is_directory=True)
    real_open = os.open
    opened = []

    def spy(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(os, "open", spy)
    before = _fingerprint(root)
    (outside / "adapter").write_bytes(b"different outside bytes")
    assert _fingerprint(root) == before                     # the target's bytes never counted
    assert not [p for p in opened if str(outside) in p]


# ---- resolution --------------------------------------------------------------------------

def _resolved(monkeypatch, packages, choice="builtin"):
    return _cfg(monkeypatch, tts_google_resolution={"choice": choice, "packages": packages})


def test_a_resolution_bound_to_the_exact_fingerprints_unblocks_google(root, monkeypatch):
    _package(root)
    _resolved(monkeypatch, [_fingerprint(root)])
    assert google_tts.collision_code() is None


@pytest.mark.parametrize("change", ["content", "added", "choice", "not-a-list"])
def test_a_resolution_no_longer_matching_blocks_again(root, monkeypatch, change):
    _package(root)
    fingerprint = _fingerprint(root)
    if change == "content":
        (root / "legacy-google" / "adapter").write_bytes(b"#!/bin/sh\nexit 3\n")
        _resolved(monkeypatch, [fingerprint])
    elif change == "added":
        _package(root, folder="second-google")
        _resolved(monkeypatch, [fingerprint])
    elif change == "choice":
        _resolved(monkeypatch, [fingerprint], choice="migrate")
    else:
        _resolved(monkeypatch, fingerprint)
    assert google_tts.collision_code() == COLLISION


def test_a_removed_package_makes_a_stale_resolution_harmless(root, monkeypatch):
    package = _package(root)
    _resolved(monkeypatch, [_fingerprint(root)])
    for child in package.iterdir():
        child.unlink()
    package.rmdir()
    assert google_tts.collision_code() is None


def test_an_incomplete_inventory_stays_unverifiable_despite_a_resolution(root, monkeypatch):
    _package(root)
    fingerprint = _fingerprint(root)
    _package(root, folder="broken", manifest="{not json")
    _resolved(monkeypatch, [fingerprint])
    assert google_tts.collision_code() == UNVERIFIABLE


def test_the_resolution_table_is_parsed_from_the_config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('[tts]\nprovider = "google"\n\n[tts.google_resolution]\nchoice = "builtin"\n'
                    'packages = ["aa", "bb"]\n')
    monkeypatch.setenv("CLAUDE_PWA_CONFIG", str(path))
    config.reset_cache()
    cfg = config.load()
    assert cfg.tts_google_resolution == {"choice": "builtin", "packages": ["aa", "bb"]}
    assert "google_resolution" not in cfg.tts_agent_overrides


# ---- status and catalog: fixed codes, required row shape, no legacy-map interpretation ------

@pytest.fixture
def offline_catalogs(monkeypatch):
    """The live voice catalogues are never reached: empty lists, so the bundled ones are used."""
    from lib import cartesia_voices, deepgram_voices, elevenlabs_voices
    monkeypatch.setattr(deepgram_voices, "english_voices", lambda *a, **k: [])
    monkeypatch.setattr(elevenlabs_voices, "english_voices", lambda *a, **k: [])
    monkeypatch.setattr(cartesia_voices, "english_voices", lambda *a, **k: [])

def test_status_reports_google_unavailable_with_the_fixed_code_and_no_path(root, monkeypatch):
    _cfg(monkeypatch)
    _package(root)
    status = tts_providers.status()
    google = next(r for r in status["providers"] if r["id"] == "google")
    assert google["available"] is False and google["error"] == COLLISION
    legacy = next(r for r in status["providers"] if r["id"].startswith("invalid."))
    assert legacy["error"] == tts_providers.LEGACY_GOOGLE_ERROR
    assert str(root) not in json.dumps(status)


def test_catalog_google_rows_keep_the_required_shape_without_legacy_interpretation(root, monkeypatch, offline_catalogs):
    from lib import voice_catalog
    _cfg(monkeypatch)
    _package(root)
    agents = {"mike": {"name": "Mike", "voice_id": json.dumps({"google": CHIRP})},
              "sara": {"name": "Sara", "voice_id": json.dumps({"google": "nb-NO-Chirp3-HD-Kore"})}}
    body = voice_catalog.catalog(agents, "mike")
    google = next(p for p in body["providers"] if p["id"] == "google")
    assert google["available"] is False and google["error"] == COLLISION
    assert google["voices"]
    for row in google["voices"]:
        assert row["current"] is False and isinstance(row["current"], bool)
        assert "taken_by" in row and row["taken_by"] is None
    for provider in body["providers"]:
        for row in provider["voices"]:
            assert isinstance(row["current"], bool) and "taken_by" in row


def test_a_resolved_catalog_computes_rows_as_today(root, monkeypatch, offline_catalogs):
    from lib import voice_catalog
    _package(root)
    _resolved(monkeypatch, [_fingerprint(root)])
    body = voice_catalog.catalog({"mike": {"name": "Mike", "voice_id": json.dumps({"google": CHIRP})}}, "mike")
    google = next(p for p in body["providers"] if p["id"] == "google")
    assert next(r for r in google["voices"] if r["id"] == CHIRP)["current"] is True


# ---- synthesis: refusal before any Google interpretation; explicit fallback only -----------

@pytest.fixture
def no_google(monkeypatch):
    """Google's credentials, synthesis and voice interpretation must not be reached."""
    def forbid(name):
        def boom(*args, **kwargs):
            raise AssertionError(f"{name} reached while google is blocked")
        return boom
    monkeypatch.setattr(google_tts, "credentials", forbid("google_tts.credentials"))
    monkeypatch.setattr(google_tts, "_urllib_transport", forbid("google transport"))
    monkeypatch.setattr(tts_worker, "_google_voice", forbid("tts_worker._google_voice"))


def test_the_route_is_refused_before_any_voice_interpretation_without_a_fallback(root, monkeypatch, no_google):
    cfg = _cfg(monkeypatch)
    _package(root)
    route = tts_mode.route(cfg, {"persona": "Mike", "agent_id": "a"})
    assert tts_worker._google_collision_refusal(cfg, route) == COLLISION


def test_an_explicit_non_google_fallback_speaks_with_the_code_as_its_error(root, monkeypatch, tmp_path, no_google):
    cfg = _cfg(monkeypatch, tts_fallback="cartesia", cartesia_api_key="k",
               cartesia_voices={"Mike": "cart-voice"})
    _package(root)
    route = tts_mode.route(cfg, {"persona": "Mike", "agent_id": "a"})
    assert tts_worker._google_collision_refusal(cfg, route) is None    # the fallback may run
    spoke, events = [], []

    def cartesia(*, text, voice_id, out_path, **kwargs):
        spoke.append(voice_id)
        pathlib.Path(out_path).write_bytes(b"ID3")
        return 3
    monkeypatch.setattr(tts_worker, "cartesia_synthesize", cartesia)
    monkeypatch.setattr(tts_worker, "_emit", lambda *a, **k: events.append((a, k)))
    tts_worker._synthesize(cfg=cfg, row={"text": "hello", "voice_id": json.dumps({"google": CHIRP}),
                                         "session": "s"},
                           agent={"persona": "Mike"}, out_path=tmp_path / "out.mp3", on_chunk=None,
                           trace_id="t", clip_route=route)
    assert spoke == ["cart-voice"]
    fallback = [k for a, k in events if a[1] == "providerFallback"]
    assert fallback and fallback[0]["detail"]["error"] == COLLISION
    assert "reason" not in fallback[0]["detail"]


def test_a_google_fallback_is_refused(root, monkeypatch, tmp_path, no_google):
    cfg = _cfg(monkeypatch, tts_provider="cartesia", tts_fallback="google")
    _package(root)
    route = tts_mode.route(cfg, {"persona": "Mike", "agent_id": "a"})

    def failing_cartesia(**kwargs):
        raise RuntimeError("cartesia down")
    monkeypatch.setattr(tts_worker, "cartesia_synthesize", failing_cartesia)
    with pytest.raises(google_tts.GoogleIdCollision) as refused:
        tts_worker._synthesize(cfg=cfg, row={"text": "hi", "voice_id": "v", "session": "s"},
                               agent={"persona": "Mike"}, out_path=tmp_path / "o.mp3", on_chunk=None,
                               trace_id="t", clip_route=route)
    assert str(refused.value) == COLLISION


def test_synthesize_itself_refuses_before_credentials_or_network(root, monkeypatch, tmp_path, no_google):
    _cfg(monkeypatch)
    _package(root)
    with pytest.raises(google_tts.GoogleIdCollision):
        google_tts.synthesize(text="hi", voice=CHIRP, out_path=tmp_path / "o.mp3",
                              creds=google_tts.Credentials("google_tts", "", "", ""))


def test_the_tts_error_message_names_the_collision_without_a_path(monkeypatch):
    sent = []
    from lib import events as events_module
    monkeypatch.setattr(events_module, "broadcast", lambda stream, event: sent.append(event))
    tts_worker._publish_tts_error(object(), {"session": "s", "agent_id": "a"}, {"persona": "Mike"}, COLLISION)
    tts_worker._publish_tts_error(object(), {"session": "s", "agent_id": "a"}, {"persona": "Mike"}, UNVERIFIABLE)
    text = json.dumps(sent)
    assert "old custom adapter also uses the id google" in text
    assert "cannot be identified" in text


def test_with_no_collision_google_behaves_as_today(root, monkeypatch):
    cfg = _cfg(monkeypatch)
    route = tts_mode.route(cfg, {"persona": "Mike", "agent_id": "a"})
    assert tts_worker._google_collision_refusal(cfg, route) is None
    assert google_tts.collision_code() is None


# ---- operator report and resolution (container-only, under the existing data-root locks) ---

def _args(**kwargs):
    base = {"tts_command": "google-collision", "report": False, "resolve_builtin": False, "dry_run": False}
    return argparse.Namespace(**{**base, **kwargs})


@pytest.fixture
def operator(root, tmp_path, monkeypatch):
    """A container data root with the two lock files; this process holds users SHARED on the fd the
    module treats as the inherited fd 9, as an entrypoint-started maintenance command does."""
    from lib import data_root_locks
    backups = tmp_path / "data" / "clarp" / "backups"
    backups.mkdir(parents=True)
    backups.chmod(0o700)
    owner_path, users_path = backups / data_root_locks.OWNER_LOCK, backups / data_root_locks.USERS_LOCK
    owner_path.touch(mode=0o600)
    users_path.touch(mode=0o600)
    users = os.open(users_path, os.O_WRONLY | os.O_APPEND)
    fcntl.flock(users, fcntl.LOCK_SH)
    monkeypatch.setattr(data_root_locks, "backups_dir", lambda: backups)
    monkeypatch.setattr(data_root_locks, "USERS_FD", users)
    monkeypatch.setattr(data_root_locks, "check_filesystem", lambda path: "ext4")
    monkeypatch.setenv("CLARP_DEPLOYMENT_MODE", "container")
    config_path = tmp_path / "config.toml"
    config_path.write_text('[server]\nport = 7682\n\n[tts]\nprovider = "google"\nfallback = "none"\n\n'
                           '[tts.agents.mike]\nprovider = "google"\ngoogle_voice = "legacy-voice"\n')
    config_path.chmod(0o600)
    monkeypatch.setattr(admin, "CONFIG_FILE", config_path)
    monkeypatch.setenv("CLAUDE_PWA_CONFIG", str(config_path))
    config.reset_cache()
    _package(root)
    yield {"backups": backups, "config": config_path, "owner": owner_path, "users": users_path, "fd": users}
    os.close(users)


def test_the_report_lists_the_collision_and_every_stored_google_use(operator, capsys):
    assert admin.cmd_tts(_args(report=True)) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["state"] == COLLISION
    assert report["inventory"]["collisions"][0]["relpath"] == "legacy-google/manifest.json"
    uses = json.dumps(report["uses"])
    assert '"tts.provider"' in uses and '"tts.agents.mike.provider"' in uses and "legacy-voice" in uses
    assert "[tts.google_resolution]" in report["resolution_table"]


def test_a_dry_run_writes_nothing(operator, capsys):
    before = operator["config"].read_bytes()
    assert admin.cmd_tts(_args(resolve_builtin=True, dry_run=True)) == 0
    assert operator["config"].read_bytes() == before
    assert "exclusivity" in capsys.readouterr().out


def test_resolve_writes_only_its_table_under_both_locks_and_verifies(operator, monkeypatch):
    before = tomllib.loads(operator["config"].read_text())
    assert admin.cmd_tts(_args(resolve_builtin=True)) == 0
    after = tomllib.loads(operator["config"].read_text())
    table = after["tts"].pop("google_resolution")
    assert after == before
    assert table["choice"] == "builtin" and len(table["packages"]) == 1
    assert stat.S_IMODE(operator["config"].stat().st_mode) == 0o600
    assert list(operator["config"].parent.glob("config.toml.google-resolution-backup-*"))
    config.reset_cache()
    assert google_tts.collision_code() is None


def test_resolve_refuses_while_a_host_owns_the_volume(operator):
    owner = os.open(operator["owner"], os.O_WRONLY | os.O_APPEND)
    fcntl.flock(owner, fcntl.LOCK_EX)
    before = operator["config"].read_bytes()
    try:
        assert admin.cmd_tts(_args(resolve_builtin=True)) == 75
    finally:
        os.close(owner)
    assert operator["config"].read_bytes() == before


def test_resolve_refuses_while_another_entrypoint_user_holds_the_volume(operator):
    other = os.open(operator["users"], os.O_WRONLY | os.O_APPEND)
    fcntl.flock(other, fcntl.LOCK_SH)
    before = operator["config"].read_bytes()
    try:
        assert admin.cmd_tts(_args(resolve_builtin=True)) == 76
    finally:
        os.close(other)
    assert operator["config"].read_bytes() == before


def test_resolve_refuses_while_a_restore_is_staged_or_applying(operator):
    (operator["backups"] / "restore.pending.tar.gz").write_bytes(b"x")
    before = operator["config"].read_bytes()
    assert admin.cmd_tts(_args(resolve_builtin=True)) == 76
    assert operator["config"].read_bytes() == before


def test_resolve_refuses_without_the_inherited_users_lock(operator, monkeypatch, tmp_path):
    from lib import data_root_locks
    stray = os.open(tmp_path / "stray", os.O_WRONLY | os.O_CREAT, 0o600)
    monkeypatch.setattr(data_root_locks, "USERS_FD", stray)
    before = operator["config"].read_bytes()
    try:
        assert admin.cmd_tts(_args(resolve_builtin=True)) == 77
    finally:
        os.close(stray)
    assert operator["config"].read_bytes() == before


def test_resolve_refuses_a_config_it_cannot_parse(operator):
    operator["config"].write_text('[tts\nprovider = "google"\n')
    before = operator["config"].read_bytes()
    assert admin.cmd_tts(_args(resolve_builtin=True)) != 0
    assert operator["config"].read_bytes() == before


def test_a_failed_verification_restores_the_original(operator, monkeypatch):
    before = operator["config"].read_bytes()
    real_loads = tomllib.loads
    calls = []

    def flaky(text):
        calls.append(1)
        parsed = real_loads(text)
        if len(calls) == 2:                                   # the post-write verification read
            parsed["server"]["port"] = 1
        return parsed
    monkeypatch.setattr(admin.tomllib, "loads", flaky)
    assert admin.cmd_tts(_args(resolve_builtin=True)) != 0
    assert operator["config"].read_bytes() == before
    assert list(operator["config"].parent.glob("config.toml.google-resolution-backup-*"))


def test_resolve_refuses_in_native_mode(operator, monkeypatch, capsys):
    monkeypatch.setenv("CLARP_DEPLOYMENT_MODE", "native")
    before = operator["config"].read_bytes()
    assert admin.cmd_tts(_args(resolve_builtin=True)) != 0
    assert operator["config"].read_bytes() == before
    assert "--report" in capsys.readouterr().err


def test_selecting_google_from_the_cli_is_refused_while_blocked(operator):
    with pytest.raises(SystemExit) as refused:
        admin.cmd_tts(argparse.Namespace(tts_command="use", provider="google", fallback="none", voice=""))
    assert COLLISION in str(refused.value)


# ---- review precision notes P-1..P-4 (v3 addendum 3) -----------------------------------------

@pytest.mark.parametrize("kind", ["symlink", "not_dir"])
def test_the_report_names_the_filesystem_remedy_for_a_root_resolve_cannot_lift(operator, tmp_path, monkeypatch,
                                                                               capsys, kind):
    link = tmp_path / "odd-root"
    if kind == "symlink":
        link.symlink_to(operator["backups"], target_is_directory=True)
    else:
        link.write_text("")
    monkeypatch.setattr(custom_tts_adapters, "ROOT", link)
    assert admin.cmd_tts(_args(report=True)) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["state"] == UNVERIFIABLE and report["resolution_table"] == ""
    assert "real directory" in report["remedy"] and str(link) not in report["remedy"]


def test_resolve_refuses_an_owner_lock_path_swapped_after_it_was_opened(operator, monkeypatch, tmp_path):
    from lib import data_root_locks
    real_open = os.open

    def swapping(path, *args, **kwargs):
        fd = real_open(path, *args, **kwargs)
        if str(path).endswith(data_root_locks.OWNER_LOCK):
            replacement = tmp_path / "replacement.lock"
            replacement.touch(mode=0o600)
            os.replace(replacement, path)                     # the path now names another inode
        return fd
    monkeypatch.setattr(os, "open", swapping)
    before = operator["config"].read_bytes()
    assert admin.cmd_tts(_args(resolve_builtin=True)) == 77
    assert operator["config"].read_bytes() == before
    assert not list(operator["config"].parent.glob("config.toml.google-resolution-backup-*"))


def test_resolve_refuses_a_symlinked_config_without_following_it(operator, tmp_path):
    real = tmp_path / "real-config.toml"
    real.write_bytes(operator["config"].read_bytes())
    operator["config"].unlink()
    operator["config"].symlink_to(real)
    before = real.read_bytes()
    assert admin.cmd_tts(_args(resolve_builtin=True)) == 78
    assert operator["config"].is_symlink() and real.read_bytes() == before
    assert not list(tmp_path.glob("config.toml.google-resolution-backup-*"))


@pytest.mark.parametrize("definition", [
    'google_resolution.choice = "builtin"\ngoogle_resolution.packages = []\n',
    'google_resolution = { choice = "builtin", packages = [] }\n',
], ids=["dotted_keys", "inline_table"])
def test_resolve_refuses_a_resolution_not_defined_as_its_own_table(operator, definition):
    text = operator["config"].read_text().replace('fallback = "none"\n', 'fallback = "none"\n' + definition, 1)
    operator["config"].write_text(text)
    before = operator["config"].read_bytes()
    assert admin.cmd_tts(_args(resolve_builtin=True)) == 78
    assert operator["config"].read_bytes() == before
    assert not list(operator["config"].parent.glob("config.toml.google-resolution-backup-*"))


def test_resolve_refuses_a_resolution_with_a_sub_table(operator):
    with operator["config"].open("a") as handle:
        handle.write('\n[tts.google_resolution.extra]\nnote = "x"\n')
    before = operator["config"].read_bytes()
    assert admin.cmd_tts(_args(resolve_builtin=True)) == 78
    assert operator["config"].read_bytes() == before


def test_resolve_replaces_a_stale_resolution_table_in_place(operator):
    with operator["config"].open("a") as handle:
        handle.write('\n[tts.google_resolution]\nchoice = "builtin"\npackages = ["stale"]\n')
    assert admin.cmd_tts(_args(resolve_builtin=True)) == 0
    after = tomllib.loads(operator["config"].read_text())
    assert after["tts"]["google_resolution"]["packages"] != ["stale"]
    assert operator["config"].read_text().count("[tts.google_resolution]") == 1

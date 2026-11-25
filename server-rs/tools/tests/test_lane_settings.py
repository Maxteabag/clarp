"""The owner's switches of Codex lanes survive an install, as top-level keys above [server]."""
from pathlib import Path
import re
import subprocess
import sys
import tomllib

TOOLS = Path(__file__).resolve().parents[1]
SCRIPT = (TOOLS / "install-host").read_text()


def run_carry(config_text, tmp_path):
    """install-host's heredoc for top-level keys, run on a config: what it keeps, and what it says."""
    body = re.search(r"carried_top=\"\$\(python3 - \"\$config_file\" <<'PYTHON'\n(.*?)\nPYTHON\n", SCRIPT, re.S)[1]
    config = tmp_path / "config.toml"
    config.write_text(config_text)
    return subprocess.run([sys.executable, "-c", body, str(config)], capture_output=True, text=True, check=True)


def carried_top(config_text, tmp_path):
    """What install-host keeps of an existing config's top-level keys."""
    return run_carry(config_text, tmp_path).stdout


def written(carried):
    """The config file install-host writes, with these carried top-level lines."""
    body = re.search(r"\(umask 077; \{\ncat <<EOF\n(.*?)\nEOF\n", SCRIPT, re.S)[1]
    shell = "cat <<EOF\n" + body + "\nEOF\n"
    env = {"carried_top": carried, "address": "127.0.0.1", "port": "7690", "token": "t", "PATH": "/usr/bin:/bin"}
    return subprocess.run(["bash", "-c", shell], capture_output=True, text=True, check=True, env=env).stdout


def test_an_install_keeps_the_lane_switches(tmp_path):
    config = ("# a comment\ncodex_chat_lane = true\nclaude_chat_lane = false\nmuse_chat_lane = true\n"
              "codex_lane_idle_minutes = 20\ncodex_lane_most = 4\n[server]\nkind = \"clarp-host\"\n"
              "[agents]\nreset_tell = [\"x\"]\n")
    assert tomllib.loads(carried_top(config, tmp_path)) == {
        "codex_chat_lane": True, "claude_chat_lane": False, "muse_chat_lane": True,
        "codex_lane_idle_minutes": 20, "codex_lane_most": 4}


def test_only_top_level_switches_of_their_kind_are_kept(tmp_path):
    config = "codex_chat_lane = \"yes\"\ncodex_lane_most = 4\n[other]\ncodex_lane_idle_minutes = 20\n"
    assert tomllib.loads(carried_top(config, tmp_path)) == {"codex_lane_most": 4}
    assert carried_top("[server]\nkind = \"clarp-host\"\n", tmp_path) == ""
    assert carried_top("not toml [", tmp_path) == ""


def test_each_switch_keeps_its_own_kind_only(tmp_path):
    wrong = ("codex_chat_lane = 5\nclaude_chat_lane = \"on\"\nmuse_chat_lane = 1\n"
             "codex_lane_idle_minutes = true\ncodex_lane_most = false\n")
    said = run_carry(wrong, tmp_path)
    assert said.stdout == ""
    for name in ("codex_chat_lane", "claude_chat_lane", "muse_chat_lane", "codex_lane_idle_minutes",
                 "codex_lane_most"):
        assert f"install-host: {name} in " in said.stderr


def test_the_written_file_has_them_above_the_server_table(tmp_path):
    kept = carried_top("codex_chat_lane = true\n[server]\n", tmp_path).strip()
    config = tomllib.loads(written(kept))
    assert config["codex_chat_lane"] is True
    assert set(config["server"]) == {"kind", "bind_addr", "port", "auth_token"}
    assert "codex_chat_lane" not in tomllib.loads(written(""))


def test_the_shared_memory_budget_is_kept_too(tmp_path):
    config = "lane_memory_mb = 4096\n[server]\n"
    assert tomllib.loads(carried_top(config, tmp_path)) == {"lane_memory_mb": 4096}
    assert "install-host: lane_memory_mb in " in run_carry("lane_memory_mb = true\n", tmp_path).stderr


def test_the_opencode_switch_is_kept_too(tmp_path):
    config = "opencode_chat_lane = true\n[server]\n"
    assert tomllib.loads(carried_top(config, tmp_path)) == {"opencode_chat_lane": True}
    assert "install-host: opencode_chat_lane in " in run_carry("opencode_chat_lane = 1\n", tmp_path).stderr

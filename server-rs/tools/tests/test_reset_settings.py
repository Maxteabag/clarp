"""The banked-reset settings survive an install, and a cut-over that dropped them is reported."""
from importlib.machinery import SourceFileLoader
from pathlib import Path
import re
import subprocess
import sys
import tomllib

TOOLS = Path(__file__).resolve().parents[1]
RESET = """
[agents]
claude_reset_command = ["/home/peter/.local/bin/claude-auto-reset", "--broker", "laptopstudio", "--peer", "laptopstudio"]
codex_reset_command = ["/home/peter/.local/bin/codex-auto-reset"]
codex_reset_max_per_day = 1
reset_tell = ["front-desk-1"]
claude_account_switch_command = ["not", "carried"]
"""


def carried(config_text, tmp_path):
    """What install-host keeps of an existing config: its own heredoc, run on it."""
    script = (TOOLS / "install-host").read_text()
    body = re.search(r"carried_config=\"\$\(python3 - \"\$config_file\" <<'PYTHON'\n(.*?)\nPYTHON\n", script, re.S)[1]
    config = tmp_path / "config.toml"
    config.write_text(config_text)
    return subprocess.run([sys.executable, "-c", body, str(config)], capture_output=True, text=True, check=True).stdout


def test_an_install_keeps_the_reset_settings_and_nothing_else_of_agents(tmp_path):
    kept = tomllib.loads(carried(RESET + "\n[conversation_forks]\nagents = [\"a\"]\n", tmp_path))
    assert kept["agents"] == {
        "claude_reset_command": ["/home/peter/.local/bin/claude-auto-reset", "--broker", "laptopstudio",
                                 "--peer", "laptopstudio"],
        "codex_reset_command": ["/home/peter/.local/bin/codex-auto-reset"],
        "codex_reset_max_per_day": 1,
        "reset_tell": ["front-desk-1"],
    }
    assert kept["conversation_forks"] == {"agents": ["a"]}


def test_a_config_without_them_gets_no_agents_table(tmp_path):
    assert "agents" not in tomllib.loads(carried("[conversation_forks]\nagents = []\n", tmp_path))


def test_compare_settings_reports_reset_settings_the_rust_config_lacks():
    compare = SourceFileLoader("compare_settings", str(TOOLS / "compare-settings")).load_module()
    python_cfg = tomllib.loads(RESET)
    found = compare.reset_findings(python_cfg, {})
    assert [name for _kind, name, _mine, theirs in found] == [
        "[agents] claude_reset_command", "[agents] codex_reset_command", "[agents] codex_reset_max_per_day",
        "[agents] reset_tell"]
    assert all(theirs == "missing" for *_rest, theirs in found)
    assert compare.reset_findings(python_cfg, python_cfg) == []

"""The skill scripts that speak to the Rust Host outside a turn present the tools' token (#795).

clarp-status-fork's rust_host.py presents the token of the agents' tools, never the owner's;
clarp-sub-agent gives its watcher the Host and that token by name alone, with its turn.
"""

import hashlib
import importlib.util
import sys
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[3] / "skills"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


rust_host = load("rust_host_under_test", SKILLS / "clarp-status-fork/scripts/rust_host.py")


def config_in(tmp_path, tools=None):
    folder = tmp_path / "clarp-host"
    folder.mkdir()
    config = folder / "config.toml"
    config.write_text('[server]\nkind = "clarp-host"\nbind_addr = "127.0.0.1"\nport = 7690\n'
                      'auth_token = "the-owners-token"\n')
    if tools is not None:
        (folder / "tools.toml").write_text(tools)
    return config


def test_the_tools_token_is_made_as_the_host_makes_it():
    made = hashlib.sha256(b"clarp-host: the token of an agent's own tools\0owner").hexdigest()
    assert rust_host.tools_token_of("owner") == made
    assert rust_host.tools_token_of("") == ""


def test_the_status_fork_never_presents_the_owners_token(tmp_path):
    config = config_in(tmp_path)
    host = rust_host.RustHost(config, environ={})
    assert host._token == rust_host.tools_token_of("the-owners-token")
    assert host._token != "the-owners-token"


def test_it_takes_its_turns_token_for_its_own_host_then_the_tools_file(tmp_path):
    config = config_in(tmp_path, '[server]\nkind = "clarp-host"\nbind_addr = "127.0.0.1"\n'
                                 'port = 7690\ntool_token = "from-the-file"\n')
    turn = {"CLARP_HOST_ADDR": "127.0.0.1:7690", "CLARP_TOOL_TOKEN": "from-the-turn"}
    assert rust_host.RustHost(config, environ=turn)._token == "from-the-turn"
    # A turn of another Host is not this Host's: the file is.
    other = {"CLARP_HOST_ADDR": "127.0.0.1:7699", "CLARP_TOOL_TOKEN": "elsewhere"}
    assert rust_host.RustHost(config, environ=other)._token == "from-the-file"
    assert rust_host.RustHost(config, environ={})._token == "from-the-file"


def test_a_tools_file_of_another_host_is_passed_over(tmp_path):
    config = config_in(tmp_path, '[server]\nkind = "clarp-host"\nbind_addr = "127.0.0.1"\n'
                                 'port = 7699\ntool_token = "another-hosts"\n')
    assert rust_host.RustHost(config, environ={})._token == rust_host.tools_token_of(
        "the-owners-token")


def test_a_sub_agents_watcher_is_told_the_host_and_tools_token_by_name(monkeypatch, tmp_path):
    monkeypatch.setenv("CLARP_SUB_AGENT_DIR", str(tmp_path))
    sub_agent = load("sub_agent_under_test", SKILLS / "clarp-sub-agents/scripts/sub_agent.py")
    for name, value in {
        "PATH": "/usr/bin", "HOME": "/home/u", "CLARP_HOST_ADDR": "127.0.0.1:7690",
        "CLARP_TOOL_TOKEN": "the-tools-token", "CLARP_PROVIDER_TURN": "1.ab.3.cd",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("CLARP_PROVIDER_TURN_FILE", raising=False)
    watch = sub_agent._passthrough(identity=True)
    assert "--setenv=PATH=/usr/bin" in watch
    assert "--setenv=CLARP_HOST_ADDR" in watch
    assert "--setenv=CLARP_TOOL_TOKEN" in watch
    assert "--setenv=CLARP_PROVIDER_TURN" in watch
    assert not any("the-tools-token" in option or "1.ab.3.cd" in option for option in watch)
    # A worker that runs another program is given the Host and the token, not the turn.
    run = sub_agent._passthrough(identity=False)
    assert "--setenv=CLARP_TOOL_TOKEN" in run
    assert "--setenv=CLARP_PROVIDER_TURN" not in run

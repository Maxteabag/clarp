#!/usr/bin/env python3
"""Record the Python Host's agent creation policy for per-agent MCP selections."""

import importlib.util
import json
import sys
from pathlib import Path

import scratch

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "server-rs/crates/clarp-kernel/tests/fixtures/mcp-selection-vectors.json"


@scratch.case("clarp-mcp-selection-")
def record(root):
    spec = importlib.util.spec_from_file_location("replay_host", REPO / "tests/replay/host.py")
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / "scenario-config.toml").touch()
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / "server"))
    from lib import backends, config, personas
    from lib.policies.agent_spec import AgentSpec, RosterView, SpecError

    (Path.home() / ".claude.json").write_text(json.dumps({
        "mcpServers": {"alpha": {}, "Beta": {}, "two words": {}},
    }))
    known = config.read_global_mcp_servers()
    current = {"backend": "claude", "name": "Existing"}
    roster = RosterView(
        agents={"existing": current},
        existing_agent=lambda _: current,
        global_mcp_servers=known,
    )
    cases = [
        {},
        {"replace_sid": "existing"},
        {"mcp_servers": []},
        {"replace_sid": "existing", "mcp_servers": []},
        {"mcp_servers": ["", " ", chr(0x1C)]},
        {"mcp_servers": ["  Beta ", "alpha", "Beta", "two words", chr(0x1C) + "alpha"]},
        {"mcp_servers": ["beta", "alpha", "other", "beta"]},
        {"mcp_servers": None},
        {"mcp_servers": "alpha"},
        {"mcp_servers": {"alpha": True}},
        {"mcp_servers": ["alpha", 3]},
        {"mcp_servers": [None]},
        {"backend": "codex", "mcp_servers": ["alpha"]},
        {"backend": "codex", "mcp_servers": ["other"]},
        {"backend": "codex", "mcp_servers": ["", " "]},
        {"backend": "codex", "replace_sid": "existing", "mcp_servers": ["alpha"]},
        {"replace_sid": "existing", "mcp_servers": ["alpha"]},
    ]
    results = []
    for asked in cases:
        backend = backends.by_id(asked.get("backend", "claude"))
        name = current["name"] if "replace_sid" in asked else "Synthetic"
        parsed = AgentSpec.parse({"name": name, **asked},
                                 backends=backends, roster=roster, personas=personas)
        if isinstance(parsed, SpecError):
            outcome = {"error": parsed.message, "message": parsed.detail or parsed.message}
            assert parsed.status == 400, (asked, parsed)
        else:
            outcome = {"selection": parsed.mcp_servers}
        results.append({
            "asked": asked,
            "supports_mcp": backend.supports_mcp,
            "label": backend.label,
            "result": outcome,
        })
    return {"catalog": known, "cases": results}


if __name__ == "__main__":
    OUT.write_text(json.dumps(record(), ensure_ascii=True, indent=2) + "\n")

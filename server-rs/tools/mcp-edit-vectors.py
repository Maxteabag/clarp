#!/usr/bin/env python3
"""Record the Python Host's catalog selection for POST /agent-mcp."""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import scratch

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "server-rs/crates/clarp-kernel/tests/fixtures/mcp-edit-vectors.json"


class Request:
    def __init__(self, names):
        self.names = names

    def _read_json(self):
        return {"session": "fixture", "mcp_servers": self.names}

    def _reject_janitor_control(self, data):
        return False

    def _json_ok(self, body):
        return body


@scratch.case("clarp-mcp-edit-")
def record(root):
    spec = importlib.util.spec_from_file_location("replay_host", REPO / "tests/replay/host.py")
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / "scenario-config.toml").touch()
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / "server"))
    from lib import agents, identity, mcp_selection
    from lib.routes.agents import AgentRoutes

    catalog = dict.fromkeys([
        "alpha", "Beta", "7", "True", "False", "None", "['alpha', 7]",
        "{'b': True, 'a': None}", "",
    ], {})
    (Path.home() / ".claude.json").write_text(json.dumps({"mcpServers": catalog}))
    cases = [
        [],
        ["unknown"],
        [" Beta ", "unknown", "alpha", "Beta", " beta "],
        [chr(0x1C) + "alpha", "alpha", "  ", ""],
        [7, "7", True, False, None, "None", 123],
        [["alpha", 7], "['alpha', 7]", {"b": True, "a": None}],
    ]
    outcomes = []
    with patch.object(identity, "lookup", return_value={"agent_id": "fixture"}):
        for names in cases:
            with patch.object(agents, "update_agent") as update:
                answer = AgentRoutes._handle_agent_mcp(Request(names))
            update.assert_called_once()
            configured, stored = mcp_selection.decode(update.call_args.kwargs["mcp_servers"])
            assert configured and stored == answer["mcp_servers"]
            outcomes.append({"requested": names, "chosen": stored})
    return {"catalog": catalog, "cases": outcomes}


if __name__ == "__main__":
    OUT.write_text(json.dumps(record(), ensure_ascii=True, indent=2) + "\n")

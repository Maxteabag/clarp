"""Fixture files must be well-formed and must not describe a server that
does not exist: every embedded /log response validates against
contract/schemas/log.json and every known-type SSE step against
contract/schemas/sse.json. Behaviour is checked by the vitest runner
(tests/contract/fixtures.test.js); this file only guards the inputs."""
from __future__ import annotations

import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from schema_check import validate  # noqa: E402

FIXTURES = REPO / "contract" / "fixtures"
AREAS = {"sync", "delivery", "sse", "audio", "state"}
STEP_KEYS = {"open", "snapshot", "log", "sse", "beginFetch", "endFetch",
             "send", "confirm", "stale", "clip", "replayCheck", "optimistic"}
LOG_MODES = {"tail", "delta", "older"}
EFFECTS = {"fetch_tail", "fetch_delta", "fetch_older", "drop_cache"}

LOG_SCHEMA = json.loads((REPO / "contract/schemas/log.json").read_text())
SSE = json.loads((REPO / "contract/schemas/sse.json").read_text())
SSE_DEFS = SSE["$defs"]
TYPE_TO_DEF = {
    "transcript-updated": "transcript-updated",
    "agent-state": "agent-state",
    "agent-activity": "agent-activity",
    "agent-roster": "agent-roster",
    "agent-focus": "agent-focus",
    "queue-updated": "queue-updated",
    "user-notification": "user-notification",
    "audio": "audio",
    "tts-error": "tts-error",
    "server-version": "server-version",
    "remote-action": "remote-action",
}


def _files() -> list[pathlib.Path]:
    assert FIXTURES.is_dir(), "contract/fixtures is missing"
    return sorted(FIXTURES.glob("*/*.json"))


def test_fixture_files_are_well_formed():
    files = _files()
    assert files, "no fixtures found"
    for path in files:
        body = json.loads(path.read_text())
        assert isinstance(body.get("title"), str) and body["title"], path
        assert body.get("area") in AREAS, f"{path}: bad area"
        if 'clients' in body:
            assert body['clients'] and set(body['clients']) <= {'web', 'ios'}, path
            assert body.get('client_scope_reason'), path
        assert path.parent.name == body["area"], f"{path}: area/dir mismatch"
        assert isinstance(body.get("steps"), list), path
        assert isinstance(body.get("expect"), dict), path
        assert isinstance(body["expect"].get("effects"), list), path
        assert set(body["expect"]["effects"]) <= EFFECTS, path
        for step in body["steps"]:
            assert isinstance(step, dict) and len(step) == 1, f"{path}: {step!r}"
            assert next(iter(step)) in STEP_KEYS, f"{path}: {step!r}"
            if "log" in step:
                assert step["log"].get("mode") in LOG_MODES, path
                validate(step["log"]["response"], LOG_SCHEMA, LOG_SCHEMA)
            if "sse" in step:
                ev = step["sse"]
                assert isinstance(ev.get("type"), str), path
                name = TYPE_TO_DEF.get(ev["type"])
                if name is not None:
                    validate(ev, SSE_DEFS[name], SSE)

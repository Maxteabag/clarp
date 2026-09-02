#!/usr/bin/env python3
"""Run offline orchestrator routing scenarios without the PWA or real models."""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import tempfile
from types import SimpleNamespace


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from lib import agents as agents_db  # noqa: E402
from lib import db  # noqa: E402
from lib.orchestrator import OrchestratorService  # noqa: E402
from lib.tts_engine import FakeTTSEngine  # noqa: E402


class FakeStream:
    def __init__(self):
        self.events = []

    def broadcast(self, event):
        self.events.append(dict(event))


def seed_agents() -> None:
    for session, persona, voice in (
        ("mike", "Mike", "mike-voice"),
        ("antoni", "Antoni", "antoni-voice"),
        ("rachel", "Rachel", "rachel-voice"),
    ):
        agents_db.create_agent(
            persona=persona,
            voice_id=voice,
            cwd="/tmp",
            session=session,
        )


def load_scenarios(paths: list[pathlib.Path]) -> list[dict]:
    scenarios: list[dict] = []
    for path in paths:
        data = json.loads(path.read_text())
        scenarios.extend(data if isinstance(data, list) else [data])
    return scenarios


def run_scenario(scenario: dict, tmp: pathlib.Path) -> tuple[bool, str]:
    db.reset_for_tests(tmp / "state.sqlite")
    seed_agents()
    for pending in scenario.get("pending_utterances", []):
        from lib.db import conn, now_ms
        conn().execute(
            """INSERT INTO orchestrator_pending_utterances (
                   pending_id, trace_id, utterance, requested_session,
                   candidate_session, speak_as_session, reason, created_at,
                   expires_at, status
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
            (
                pending["pending_id"],
                pending.get("trace_id", "pending-trace"),
                pending["utterance"],
                pending.get("requested_session", "mike"),
                pending.get("candidate_session", ""),
                pending.get("speak_as_session", "mike"),
                pending.get("reason", ""),
                now_ms(),
                now_ms() + 120000,
            ),
        )
    tts = FakeTTSEngine(tmp / "audio")
    ctx = SimpleNamespace(
        default_session="mike",
        stream=FakeStream(),
        tts=tts,
        agents_path=tmp / "agents.json",
    )
    ctx.speak_announcement = (
        lambda text, voice_id, session=None: tts.synthesize(
            text, voice_id, session=session
        )
    )
    dispatches = []

    def model_call(_packet, _settings):
        if scenario.get("model_exception"):
            raise RuntimeError(str(scenario["model_exception"]))
        return dict(scenario["model_response"])

    def dispatch(**kwargs):
        dispatches.append(kwargs)
        return SimpleNamespace(session=kwargs["forced_session"], backend="claude")

    outcome = OrchestratorService(ctx, model_call=model_call).handle_send(
        text=scenario["utterance"],
        requested_session=scenario.get("requested_session", "mike"),
        trace_id="scenario",
        hands_free=True,
        synthesize_audio=True,
        dispatch=dispatch,
    )
    expected = scenario["expected"]
    got = {"action": outcome.action if outcome else "", "session": outcome.session if outcome else ""}
    if got != expected:
        return False, f"{scenario['name']}: expected {expected}, got {got}"
    if "dispatched" in scenario:
        dispatched = bool(dispatches)
        if dispatched is not bool(scenario["dispatched"]):
            return False, f"{scenario['name']}: dispatched={dispatched}"
    if "expected_dispatch_text" in scenario:
        text = dispatches[0]["text"] if dispatches else ""
        if text != scenario["expected_dispatch_text"]:
            return False, (
                f"{scenario['name']}: expected dispatch text "
                f"{scenario['expected_dispatch_text']!r}, got {text!r}"
            )
    return True, scenario["name"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "paths",
        nargs="*",
        type=pathlib.Path,
        default=[ROOT / "tests" / "orchestrator" / "scenarios" / "core.json"],
    )
    args = parser.parse_args()
    scenarios = load_scenarios(args.paths)
    ok = 0
    with tempfile.TemporaryDirectory(prefix="orchestrator-scenarios-") as td:
        root = pathlib.Path(td)
        for i, scenario in enumerate(scenarios):
            passed, detail = run_scenario(scenario, root / str(i))
            print(("PASS " if passed else "FAIL ") + detail)
            ok += 1 if passed else 0
    print(f"{ok}/{len(scenarios)} scenarios passed")
    return 0 if ok == len(scenarios) else 1


if __name__ == "__main__":
    raise SystemExit(main())

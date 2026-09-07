#!/usr/bin/env python3
"""Preview or apply a nested Clarp team plan through the authenticated API."""

from __future__ import annotations

import argparse
import json
import tomllib
import urllib.request
from pathlib import Path
from urllib.parse import quote


def api_config() -> tuple[str, dict[str, str]]:
    config_path = Path.home() / ".config/clarp/config.toml"
    server = tomllib.loads(config_path.read_text())["server"]
    host = server.get("bind_addr", "127.0.0.1")
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    return (
        f"http://{host}:{server['port']}",
        {
            "Authorization": f"Bearer {server['auth_token']}",
            "Content-Type": "application/json",
        },
    )


def call(base: str, headers: dict[str, str], method: str, path: str, data=None):
    payload = None if data is None else json.dumps(data).encode()
    request = urllib.request.Request(
        base + path, data=payload, headers=headers, method=method
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def load_plan(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    if not isinstance(data, list):
        raise SystemExit("plan must be a JSON array")
    names: set[str] = set()
    for item in data:
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            raise SystemExit("every team needs a name")
        name = str(item["name"]).strip()
        if name in names:
            raise SystemExit(f"duplicate team name: {name}")
        parent = item.get("parent")
        if parent and parent not in names:
            raise SystemExit(f"parent must precede child: {parent} -> {name}")
        names.add(name)
    return data


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("plan", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--require-all-active", action="store_true")
    args = parser.parse_args()
    plan = load_plan(args.plan)
    base, headers = api_config()
    snapshot = call(base, headers, "GET", "/agents/snapshot")["agents"]
    active = {a["session"]: a for a in snapshot if not a.get("archived_at")}
    teams = call(base, headers, "GET", "/teams")["teams"]
    by_name = {team["name"]: team for team in teams}
    desired_sessions = [
        session for item in plan for session in item.get("members", [])
    ]
    unknown = sorted(set(desired_sessions) - set(active))
    duplicates = sorted(
        session
        for session in set(desired_sessions)
        if desired_sessions.count(session) > 1
    )
    omitted = sorted(set(active) - set(desired_sessions))
    if unknown:
        raise SystemExit(f"unknown or archived sessions: {', '.join(unknown)}")
    if duplicates:
        raise SystemExit(f"sessions assigned more than once: {', '.join(duplicates)}")
    if args.require_all_active and omitted:
        raise SystemExit(f"active sessions omitted: {', '.join(omitted)}")

    preview = []
    for item in plan:
        name = str(item["name"]).strip()
        preview.append(
            {
                "name": name,
                "action": "update" if name in by_name else "create",
                "parent": item.get("parent"),
                "members": item.get("members", []),
            }
        )
    if not args.apply:
        print(json.dumps({"apply": False, "teams": preview, "omitted": omitted}, indent=2))
        return 0

    for item in plan:
        name = str(item["name"]).strip()
        parent = item.get("parent")
        parent_id = by_name[parent]["team_id"] if parent else None
        current = by_name.get(name)
        body = {
            "name": name,
            "color": item.get("color", ""),
            "parent_team_id": parent_id,
        }
        if current:
            body.update({"leader_enabled": False, "communication_enabled": False})
            by_name[name] = call(
                base, headers, "POST", f"/teams/{quote(current['team_id'])}", body
            )["team"]
        else:
            by_name[name] = call(base, headers, "POST", "/teams", body)["team"]

    for item in plan:
        team = by_name[str(item["name"]).strip()]
        team_id = team["team_id"]
        desired = {active[s]["agent_id"] for s in item.get("members", [])}
        current = set(team.get("member_agent_ids") or [])
        for agent_id in sorted(current - desired):
            call(
                base,
                headers,
                "DELETE",
                f"/teams/{quote(team_id)}/members/{quote(agent_id)}",
            )
        for agent_id in sorted(desired - current):
            call(
                base,
                headers,
                "POST",
                f"/teams/{quote(team_id)}/members",
                {"agent_id": agent_id},
            )

    final = call(base, headers, "GET", "/teams")["teams"]
    managed = {str(item["name"]).strip() for item in plan}
    result = [team for team in final if team["name"] in managed]
    print(json.dumps({"apply": True, "teams": result, "omitted": omitted}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

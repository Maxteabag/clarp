#!/usr/bin/env python3
"""Create and update durable Clarp task plans from agent workflows."""

from __future__ import annotations
import json
import os
import pathlib
import sys

share = pathlib.Path(
    os.environ.get("CLARP_SHARE_DIR", pathlib.Path.home() / ".local/share/clarp")
)
sys.path.insert(0, os.environ.get("CLARP_CODE_ROOT", str(share / "current")))
from lib import task_plans  # noqa: E402


def main(argv: list[str]) -> int:
    usage = (
        "usage: agent_tasks.py create SESSION PLAN_ID TITLE JSON_ITEMS | "
        "update RETURNED_PLAN_ID ITEM_ID STATUS [DETAIL] | "
        "finish RETURNED_PLAN_ID [STATUS] | show SESSION | list SESSION | "
        "goal SESSION ALIAS TITLE JSON_ITEMS JSON_GOAL | "
        "act PLAN_ID REVISION ACTION JSON_DATA | step PLAN_ID REVISION ITEM STATUS [DETAIL]"
    )
    if len(argv) < 2:
        print(usage, file=sys.stderr)
        return 2
    try:
        command = argv[1]
        if command == "create" and len(argv) == 7:
            command = "goal"
        if (
            command
            in {
                "checkpoint",
                "replan",
                "pause",
                "resume",
                "cancel",
                "block",
                "supersede",
                "dependency",
                "enroll",
                "add_step",
                "document",
            }
            and len(argv) == 5
        ):
            argv = [argv[0], "act", argv[2], argv[3], command, argv[4]]
            command = "act"
        if command == "goal" and len(argv) == 7:
            result = task_plans.create(
                session=argv[2],
                plan_id=argv[3],
                title=argv[4],
                items=json.loads(argv[5]),
                goal=json.loads(argv[6]),
            )
        elif command == "act" and len(argv) == 6:
            from lib import task_goal_state

            result = task_goal_state.mutate(
                argv[2], revision=int(argv[3]), action=argv[4], data=json.loads(argv[5])
            )
        elif command == "step" and len(argv) in {6, 7}:
            result = task_plans.update_item(
                task_plans.item_key(argv[2], argv[4]),
                argv[5],
                argv[6] if len(argv) == 7 else None,
                revision=int(argv[3]),
            )
        elif command == "complete" and len(argv) == 4:
            result = task_plans.finish(argv[2], revision=int(argv[3]))
        elif command == "list" and len(argv) == 3:
            result = task_plans.list_for_session(argv[2])
        elif command == "read" and len(argv) in {4, 5}:
            result = task_plans.goal_document(
                argv[2], argv[3], int(argv[4]) if len(argv) == 5 else None
            )
        elif command == "get" and len(argv) == 3:
            result = task_plans.get(argv[2])
        elif command == "create" and len(argv) == 6:
            items = json.loads(argv[5])
            result = task_plans.create(
                session=argv[2], plan_id=argv[3], title=argv[4], items=items
            )
        elif command == "update" and len(argv) in {5, 6}:
            result = task_plans.update_item(
                task_plans.item_key(argv[2], argv[3]),
                argv[4],
                argv[5] if len(argv) == 6 else None,
            )
        elif command == "finish" and len(argv) in {3, 4}:
            result = task_plans.finish(
                argv[2], argv[3] if len(argv) == 4 else "completed"
            )
        elif command == "show" and len(argv) == 3:
            result = task_plans.active_for_session(argv[2]) or {}
        else:
            print(usage, file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"agent_tasks: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

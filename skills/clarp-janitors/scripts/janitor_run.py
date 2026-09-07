#!/usr/bin/env python3
"""Small model-facing adapter to the installed, authenticated Janitor CLI."""
import os
import sys


def main() -> None:
    if len(sys.argv) < 3 or sys.argv[1] not in {"context", "review"}:
        raise SystemExit("usage: janitor_run.py context RUN_ID | review RUN_ID [review flags]")
    action = "run-context" if sys.argv[1] == "context" else "review"
    os.execvp("clarp-admin", ["clarp-admin", "janitor", action, *sys.argv[2:]])


if __name__ == "__main__":
    main()

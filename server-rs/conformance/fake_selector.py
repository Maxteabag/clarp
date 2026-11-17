#!/usr/bin/env python3
"""The stand-in for an owner's selector of accounts in the conformance suite.

A Host that runs its owner's selector gives it, on its standard input, the models its waiting
turns run on (``{"models": [...]}``), and reads its answer from its standard output. This one
says that an account serves every one of them, and changes nothing anywhere: the stand-in for
the tool has its own word for when a limit is over (``[qa-limit:N]`` in ``fake_claude.py``).
"""
import json
import sys


def main() -> int:
    try:
        asked = json.loads(sys.stdin.read() or '{}')
    except ValueError:
        asked = {}
    models = asked.get('models') if isinstance(asked, dict) else None
    sys.stdout.write(json.dumps({'available': True, 'models': models if isinstance(models, list) else []}))
    return 0


if __name__ == '__main__':
    sys.exit(main())

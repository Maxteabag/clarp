"""Carry exact Bash invocation identity into optional managed job registration.

PreToolUse updatedInput changes input only; it never makes a permission decision.
The original command is preserved after a shell-quoted, non-secret export.
"""
import json
import re
import shlex

_ID = re.compile(r'[A-Za-z0-9_-]{1,160}\Z')


def tool_input_with_origin(agent: dict, native: str, tool: str, inputs: dict) -> dict | None:
    command = inputs.get('command')
    if inputs.get('run_in_background') is not True or not isinstance(command, str):
        return None
    if not all(isinstance(v, str) and _ID.fullmatch(v)
               for v in (agent.get('agent_id'), agent.get('backend'), native, tool)):
        return None
    origin = json.dumps({'agent_id': agent['agent_id'], 'provider': agent['backend'],
                         'native_session_id': native, 'tool_use_id': tool}, separators=(',', ':'))
    prefix = 'export CLARP_BACKGROUND_ORIGIN=' + shlex.quote(origin) + '\n'
    return {**inputs, 'command': command if command.startswith(prefix) else prefix + command}

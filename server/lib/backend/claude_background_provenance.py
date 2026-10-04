"""Carry exact Bash invocation identity into optional managed job registration.

PreToolUse updatedInput changes input only; it never makes a permission decision.
The original command is preserved after a shell-quoted, non-secret export.
The same hook output adds BACKGROUND_TASK_CONTEXT for the model (Claude Code
2.1.285 applies hookSpecificOutput.additionalContext for PreToolUse).
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


# Claude promises "you will be notified when it completes". Under Clarp each
# turn is one Claude process and its background tasks end with it, so that
# promise does not hold once the turn's reply is sent (Ziggy, 2026-10-04: the
# detached render finished, its watcher had ended with the turn, and nobody was
# told). Advice only: whether a goal wakes its owner depends on this Host's
# recovery policy, which a hook cannot see.
BACKGROUND_TASK_CONTEXT = (
    "Clarp: this background command belongs to the current turn. Clarp runs one "
    "Claude process per turn, and when your reply ends, that process exits and "
    "this task stops with it: you will not be notified when it finishes, and a "
    "watcher loop here stops too. A process you start detached (setsid or nohup) "
    "keeps running, but nothing reports its result back to you. If its result "
    "must reach you after this turn, start it as a registered job and attach a "
    "clarp-goal dependency with its job handle and a deadline before you reply "
    "(clarp-background-jobs skill, \"Completion that must wake you\"). That wake "
    "happens only while goal recovery runs on this Host; if the goal shows "
    "paused or blocked, tell the user you will not be woken."
)


def background_task_context(inputs: dict) -> str | None:
    return BACKGROUND_TASK_CONTEXT if inputs.get('run_in_background') is True else None

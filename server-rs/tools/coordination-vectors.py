#!/usr/bin/env python3
"""Writes down the Python Host's rules of coordination between agents (its PR 482, contract 108).

When one agent sends another a message, the turn it starts is coordination between agents, and every row of that
turn says so (`audience`). The rules are `server/lib/coordination.py` (what is coordination, a row's audience, who
can leave a note for the user), `server/lib/policies/notifications.py` (whether a note pages the user) and
`server/lib/send_request.py` (what a send is: its origin and its sender, decided from what it claims and from who
made it). They are run here as they are, with stand-ins for the agents a name is looked up among and for the
delegations of bookkeeping, and written to `crates/clarp-kernel/tests/fixtures/coordination-vectors.json`.

    <the Python Host's interpreter> server-rs/tools/coordination-vectors.py

It runs in a process of its own whose every home, data, config and cache directory is a new empty directory (the
replay harness's own isolation, its database there too), with no way out of the machine. The same file at every
run. Nothing is logged. A case of the reference that only a Host which steers a message into a running turn can
reach ('mixed') is named `steer_only`, so that a Host that does not steer can leave it out by that name.
"""
from __future__ import annotations

import importlib.util
import itertools
import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch as tool_scratch

REPO = pathlib.Path(__file__).resolve().parents[2]
OUT = REPO / 'server-rs' / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'coordination-vectors.json'

ME, OTHER = 'a-worker', 'a-boss'
# Every way an origin can be said: none, the words of the reference, and words it does not know.
ORIGINS = (None, '', 'user', 'oracle', 'agent', 'automation', 'schedule', 'watcher', 'heartbeat', 'dreaming',
           'note', 'system', 'peer', ' user ', 'User', 'ORACLE', ' agent', 'Agent')
SENDERS = (None, '', ' ', ME, OTHER, f' {OTHER} ', 'a-gone')
KINDS = (None, '', 'note', 'Note', ' note', 'reply', 'tool', 'live')
TRACE_AUDIENCES = (None, '', 'agents', 'mixed', 'user', 'AGENTS', 'agents ')
BACKENDS = (None, '', 'claude', 'codex', 'agy', 'antigravity', 'grok', 'opencode', 'open-code', 'opencode-ai',
            'deepseek', 'deep-seek', 'muse', 'muse-code', 'musecode', 'Claude', ' CODEX ', 'gemini', 'unknown')

# The agents of the stand-in roster: a session, an agent id, a name.
ROSTER = {'worker': ('worker', ME, 'Worker'), 'boss': ('boss', OTHER, 'Boss'),
          'keeper': ('keeper', 'a-keeper', 'Keeper')}
SEND_ORIGINS = (None, '', 'user', 'oracle', 'agent', 'automation', 'schedule', 'watcher', 'heartbeat', 'dreaming',
                'leader_tick', 'janitor', 'note', ' USER ', 'Oracle', 'bogus')
SEND_SENDERS = (None, '', 'boss', OTHER, 'worker', 'keeper', 'nobody')
# Who made the request, as the Host knows it: a turn's own token of an agent (or none), and a peer Host.
CALLERS = ('', OTHER, ME, 'a-keeper', 'a-gone')
TARGETS = ('worker', 'boss')


@tool_scratch.case('clarp-coordination-vectors-')
def run(root) -> dict:
    """Runs in a process of its own: what the Python Host's modules do."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import agents, coordination, goal_ledger, identity, send_request
    from lib.policies import notifications as policy

    # --- what is coordination, and a row's audience ---
    coordination_cases = [
        {'agent_id': ME, 'origin': origin, 'sender_agent_id': sender,
         'coordination': coordination.is_coordination(agent_id=ME, origin=origin, sender_agent_id=sender)}
        for origin, sender in itertools.product(ORIGINS, SENDERS)]
    audiences = [{'kind': kind, 'trace_audience': trace,
                  'audience': coordination.row_audience(kind=kind, trace_audience=trace)}
                 for kind, trace in itertools.product(KINDS, TRACE_AUDIENCES)]

    # --- who can leave a note: the agent's backend as its record has it (the Python Host's set) ---
    records: dict = {}
    agents.get_by_agent_id = lambda agent_id: records.get(agent_id)
    can = []
    for backend in BACKENDS:
        records.clear()
        records[ME] = {'agent_id': ME, 'backend': backend}
        can.append({'backend': backend, 'can_leave_note': coordination.can_leave_note(ME)})
    records.clear()
    can.append({'backend': None, 'no_agent': True, 'can_leave_note': coordination.can_leave_note(ME)})
    can.append({'backend': 'claude', 'agent_id': '', 'can_leave_note': coordination.can_leave_note('')})

    # --- whether a note pages the user ---
    notes = []
    for present, can_chat, muted, leader, quiet in itertools.product((False, True), repeat=5):
        decision = policy.note_decision(policy.AgentCapabilities(
            present=present, can_chat=can_chat, muted=muted, is_team_leader=leader, quiet=quiet))
        told = {'notify': isinstance(decision, policy.Notify), 'reason': decision.reason}
        if isinstance(decision, policy.Notify):
            told.update(kind=decision.kind, push=decision.push, muted=decision.muted)
        notes.append({'present': present, 'can_chat': can_chat, 'muted': muted, 'is_team_leader': leader,
                      'quiet': quiet, 'decision': told})

    # --- what a send is: its origin and its sender, from what it claims and who made it ---
    def lookup(name):
        name = (name or '').strip()
        for session, agent_id, persona in ROSTER.values():
            if name in (session, agent_id):
                return {'session': session, 'agent_id': agent_id, 'persona': persona}
        return None

    def resolve(raw):
        found = lookup(raw)
        return found['agent_id'] if found else ''

    identity.lookup = lookup
    goal_ledger.is_delegate_of = lambda helper, principal: (helper, principal) == ('a-keeper', ME)
    sends = []
    for target, origin, sender, caller, peer in itertools.product(TARGETS, SEND_ORIGINS, SEND_SENDERS, CALLERS,
                                                                   (False, True)):
        data = {'text': 'Hello.', 'session': target}
        if origin is not None:
            data['origin'] = origin
        if sender is not None:
            data['sender'] = sender
        case = {'target': target, 'origin': origin, 'sender': sender, 'caller_agent_id': caller, 'peer': peer}
        try:
            made = send_request.SendRequest.from_payload(
                data, default_session='worker', trace_id_factory=lambda: 'minted', authenticated=True,
                resolve_sender=resolve, caller_agent_id=caller, peer=peer)
        except send_request.SendRequestError as refused:
            case['refused'] = {'status': refused.status, 'error': str(refused)}
        else:
            target_id = lookup(made.session)['agent_id']
            case['made'] = {'origin': made.origin, 'sender_agent_id': made.sender_agent_id,
                            'synthesize_audio': made.synthesize_audio,
                            'coordination': coordination.is_coordination(
                                agent_id=target_id, origin=made.origin, sender_agent_id=made.sender_agent_id)}
        sends.append(case)

    from lib.origins import USER_WORD_ORIGINS
    return {'what': 'Written by server-rs/tools/coordination-vectors.py from server/lib/coordination.py, '
                    'policies/notifications.py and send_request.py. Do not edit by hand.',
            'agent_id': ME, 'user_word_origins': sorted(USER_WORD_ORIGINS),
            'roster': [list(row) for row in ROSTER.values()],
            'delegates': [['a-keeper', ME]],
            'coordination': coordination_cases, 'audience': audiences, 'can_leave_note': can,
            'note_decision': notes, 'sends': sends,
            # The reference's audience values: 'mixed' only comes of a message steered into a running turn.
            'steer_only': [coordination.MIXED]}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run()))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env=tool_scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}), check=False)
    if ran.returncode != 0:
        print(ran.stderr[-3000:], file=sys.stderr)
        return 1
    said = json.loads(ran.stdout.splitlines()[-1])
    lines = ['{']
    names = list(said)
    for at, name in enumerate(names):
        comma = ',' if at < len(names) - 1 else ''
        value = said[name]
        if isinstance(value, list) and value and isinstance(value[0], dict):
            lines.append(f' {json.dumps(name)}: [')
            lines.extend('  ' + json.dumps(case, ensure_ascii=True) + (',' if n < len(value) - 1 else '')
                         for n, case in enumerate(value))
            lines.append(' ]' + comma)
        else:
            lines.append(f' {json.dumps(name)}: {json.dumps(value, ensure_ascii=True)}{comma}')
    lines.append('}')
    OUT.write_text('\n'.join(lines) + '\n', encoding='ascii')
    print(f'{OUT.relative_to(REPO)}: {len(said["coordination"])} coordination, {len(said["audience"])} audience, '
          f'{len(said["can_leave_note"])} backends, {len(said["note_decision"])} note decisions, '
          f'{len(said["sends"])} sends ({sum(1 for c in said["sends"] if "refused" in c)} refused), '
          f'{OUT.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

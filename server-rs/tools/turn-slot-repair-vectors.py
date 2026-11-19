#!/usr/bin/env python3
"""Writes the vectors of who stops or repairs an agent and why (contract 61, the feature `turn_slot_repair`).

The Python Host's Stop (`POST /stop`, `Handler._handle_stop` and `_stop_agent_session`) and its repair of a slot
(`POST /repair-slot`, `Handler._handle_repair_slot` and `TurnDispatchService.repair_slot`) both read who acts and why
(`Handler._control_actor`) and keep it on the agent's state, which an app reads on the state's activity
(`activity._control_provenance`). This drives those functions of the Python Host as they are, on a handler that
answers into a list and a database made new for it:

- who acts: a claim or none, a paired device or the administrator, the token of an agent's turn, of nobody, one whose
  lookup fails;
- why: a reason of every shape, cut where the Host cuts it;
- the Stop of an agent with no turn: its answer, the goals' pause reason, the state it keeps;
- the repair of an agent with nothing to release: its refusals, its answer, and the note it keeps on the state;
- the activity's provenance over the details of every source.

    "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/turn-slot-repair-vectors.py

It runs in a process of its own whose every home, data, config and cache directory is new and empty (the replay
harness's own isolation); no Host is started and nothing leaves the machine. It writes
`crates/clarp-kernel/tests/fixtures/turn-slot-repair-vectors.json`.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[2]
OUT = REPO / 'server-rs' / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'turn-slot-repair-vectors.json'


def run() -> dict:
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    root = pathlib.Path(tempfile.mkdtemp(prefix='clarp-turn-slot-repair-vectors-', dir='/var/tmp'))
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    import server
    from lib import activity, agents, events, provider_background_jobs as turns, turn_identity

    ids = {session: agents.create_agent(persona=session.title(), voice_id='', cwd='/srv', session=session,
                                        backend='claude')
           for session in ('desk', 'omar')}
    tokens = {}
    for session in ('desk', 'omar'):
        token, _ = turns.identity_env(ids[session])
        turns.turn_started(token, agent_id=ids[session], provider='claude', pid=1)
        tokens[session] = token
    never, _ = turns.identity_env(ids['desk'])

    class Ctx:
        runtime_client = None
        default_session = 'desk'
        stream = None

    def handler(data, *, principal='administrator', turn=None):
        made = object.__new__(server.Handler)
        made.headers = {} if turn is None else {'X-Clarp-Turn': turn}
        made._request_principal = principal
        made.server = type("Server", (), {"ctx": Ctx()})()
        made.said = []
        made._read_json = lambda: data
        made._json_ok = lambda payload: made.said.append((200, payload))
        made._json_error = lambda code, message: made.said.append((code, {'error': message}))
        made._json = lambda code, payload: made.said.append((code, payload))
        return made

    def acting(data, **how):
        try:
            return {'actor': list(handler(data, **how)._control_actor(data))}
        except turn_identity.TurnIdentityUnavailable:
            return {'unavailable': True}

    claims = [{}, {'actor': 'user'}, {'actor': 'agent:omar'}, {'actor': '  agent:omar  '}, {'actor': ''},
              {'actor': None}, {'actor': 7}, {'actor': 'x' * 100}, {'actor': 'æ' * 90}]
    actors = []
    for data in claims:
        for principal in ('administrator', 'dev-0a1b2c', ''):
            actors.append({'data': data, 'principal': principal, 'turn': None, **acting(data, principal=principal)})
        for turn in ('desk', 'never', ''):
            token = {'desk': tokens['desk'], 'never': never, '': ''}[turn]
            actors.append({'data': data, 'principal': 'administrator', 'turn': turn,
                           **acting(data, principal='administrator', turn=token)})
    real = turn_identity.caller_agent_id
    turn_identity.caller_agent_id = lambda environ=None: (_ for _ in ()).throw(RuntimeError('database is locked'))
    actors.append({'data': {}, 'principal': 'administrator', 'turn': 'failing',
                   **acting({}, principal='administrator', turn=tokens['desk'])})
    turn_identity.caller_agent_id = real
    # Claims a JSON body can make that are no text: Python writes them as it writes them.
    for data in ({'actor': True}, {'actor': 7.5}, {'actor': ['user']}):
        actors.append({'data': data, 'principal': 'administrator', 'turn': None,
                       **acting(data, principal='administrator')})

    def latest(session):
        row = agents.latest_state(ids[session])
        if row is None:
            return None
        detail = row.get('detail') or {}
        return {'kind': row['kind'], 'detail': detail, 'provenance': activity._control_provenance(detail)}

    repairs = []
    bodies = [None, [], {}, {'session': 'nobody', 'reason': 'x'}, {'session': 'desk'}, {'session': 'desk', 'reason': '   '},
              {'session': 'desk', 'reason': ' it is stuck  '}, {'session': ' desk ', 'reason': 'x'},
              {'session': 'desk', 'reason': 'r' * 250}, {'session': 'desk', 'reason': 'æ' * 210},
              {'session': 'desk', 'reason': 7}, {'session': 'desk', 'reason': 'fixing', 'actor': 'agent:omar'}]
    for data in bodies:
        made = handler(data)
        try:
            made._handle_repair_slot()
        except Exception as broke:  # noqa: BLE001 - told by its kind
            made.said.append(('broke', type(broke).__name__ + ': ' + str(broke)[:200]))
        repairs.append({'data': data, 'said': made.said, 'state': latest('desk')})
    made = handler({'session': 'desk', 'reason': 'by its turn'}, turn=tokens['omar'])
    made._handle_repair_slot()
    repairs.append({'data': {'session': 'desk', 'reason': 'by its turn'}, 'turn': 'omar', 'said': made.said,
                    'state': latest('desk')})
    # The Stop of an agent with no turn: its answer, the reason its goals are paused with, and its state.
    from lib import task_plans
    paused_with = []
    task_plans.pause_recovery_for_agent = lambda agent_id, reason: paused_with.append(reason) or 0
    stops = []
    for data, turn in [({'session': 'desk'}, None), ({'session': 'desk', 'reason': '  wrong branch  '}, None),
                       ({'session': 'desk', 'actor': 'agent:omar'}, None),
                       ({'session': 'desk', 'actor': 'agent:omar', 'reason': 'it loops'}, None),
                       ({'session': 'desk', 'reason': 'r' * 250}, None), ({'session': 'desk', 'reason': 7}, None),
                       ({'session': 'desk', 'reason': 'it loops'}, 'omar'), ({'session': 'desk'}, 'omar'),
                       ({'session': 'desk', 'actor': 'user'}, 'never')]:
        del paused_with[:]
        token = {'omar': tokens['omar'], 'never': never, None: None}[turn]
        made = handler(data, turn=token)
        try:
            made._handle_stop()
        except Exception as broke:  # noqa: BLE001 - told by its kind
            made.said.append(('broke', type(broke).__name__ + ': ' + str(broke)[:200]))
        stops.append({'data': data, 'turn': turn, 'said': made.said, 'goals_paused_with': paused_with[:],
                      'state': latest('desk')})
    details = [{'source': 'user_stop', 'stop_actor': 'agent:omar', 'stop_actor_verified': True, 'stop_reason': 'x',
                'message': 'Turn stopped'},
               {'source': 'user_stop', 'message': 'Turn stopped'},
               {'source': 'user_stop', 'stop_actor': 'user'},
               {'source': 'slot_repair', 'repair_actor': 'user', 'repair_actor_verified': False, 'repair_reason': 'x',
                'released_trace': ''},
               {'source': 'slot_repair', 'repair_actor': 'user'},
               {'source': 'slot_repair'},
               {'source': 'other', 'stop_actor': 'user', 'repair_actor': 'user'},
               {}]
    provenance = [{'detail': detail, 'provenance': activity._control_provenance(detail)} for detail in details]
    return {'actors': actors, 'repairs': repairs, 'stops': stops, 'provenance': provenance,
            'activity_fields': list(events.FIELDS[events.SSEType.AGENT_ACTIVITY])}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run()))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, check=False)
    if ran.returncode != 0:
        print(ran.stderr[-6000:], file=sys.stderr)
        return 1
    said = json.loads(ran.stdout.splitlines()[-1])
    OUT.write_text(json.dumps(said, ensure_ascii=True, indent=1) + '\n')
    print(f'{OUT.relative_to(REPO)}: ' + ', '.join(f'{len(rows)} {part}' for part, rows in said.items()))
    return 0


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
"""Writes the vectors the Rust Host's naming of the turn that published an artifact is held to.

An artifact published by `POST /artifacts` names the turn that published it (`source_trace_id`,
R102): the trace of the turn whose Host-issued token the request presents (`X-Clarp-Turn`), only
when that turn is the artifact's own agent's and is that agent's running turn now (the Python
Host's `routes/artifacts._publishing_trace`, `turn_identity.caller_turn` and `running_trace`).
Anything else is the empty text, and a later revision keeps what the first publication had.
This publishes, for each way a request can stand to a turn and for each kind that is published
through that route (a document, a render spec, a read-only report), and writes what the
artifact says, by name (`the trace`, `none`) since a trace is the reference's own text, to
`crates/clarp-kernel/tests/fixtures/source-trace-vectors.json`.

    python3 server-rs/tools/source-trace-vectors.py

Nothing of a Host is started; the library writes only in a scratch place of its own
(`tools/scratch.py`), which is gone when it ends.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch as tool_scratch  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'source-trace-vectors.json'


def isolate(place: pathlib.Path) -> None:
    """Points the library at a place of its own before it is imported."""
    os.environ.update({
        'CLAUDE_PWA_DB': str(place / 'state.sqlite'), 'CLAUDE_PWA_LOG_DIR': str(place / 'logs'),
        'CLARP_CONFIG_DIR': str(place / 'config'), 'CLARP_SHARE_DIR': str(place / 'share'),
        'CLARP_CACHE_DIR': str(place / 'cache'), 'CLAUDE_PWA_CONFIG': str(place / 'no-such-config.toml'),
        'CLARP_CODE_ROOT': str(REPO / 'server'),
    })
    os.environ.pop('CLARP_PROVIDER_TURN', None)
    sys.path.insert(0, str(REPO / 'server'))


# Each way a request can stand to a turn: how the case is set up, in words the Rust test
# stages the same way.
CASES = [
    ('the running turn', 'its own agent, in its turn that runs'),
    ('no token', 'the request presents none'),
    ('a token that is none', 'a text that is no token of this Host'),
    ('a token of a turn nobody has', "a token of the Host's own form whose turn no agent has"),
    ("another agent's token", 'the token of a turn of another agent, which runs'),
    ('the turn ended', 'its own agent, in a turn that ended (a job it started that publishes later)'),
    ('an older turn', 'its own agent, in a turn that ended while a newer one runs'),
    ('a running turn without a trace', 'its own agent, in its turn that runs, which has no trace'),
]
KINDS = ('document', 'render_spec', 'report')


def generate(place: pathlib.Path) -> int:
    isolate(place)
    from lib import agents as agents_db, artifacts, db, provider_background_jobs as provider_jobs
    from lib import render_spec, turn_identity, turn_lifecycle
    from lib.routes.artifacts import ArtifactRoutes
    db.reset_for_tests(place / 'trace.sqlite')
    minted = iter(f'agent-{n}' for n in range(1, 1000))
    agents_db._new_agent_id = lambda: next(minted)
    spec = json.loads((REPO / 'tests' / 'fixtures' / 'render_spec' / 'build-decision.json').read_text())
    work = place / 'work'
    work.mkdir()

    class Request(ArtifactRoutes):
        def __init__(self, token: str = '') -> None:
            self.headers = {'X-Clarp-Turn': token} if token else {}

    counter = iter(range(1, 10_000))
    sessions: dict[str, str] = {}

    def fresh(name: str) -> str:
        n = next(counter)
        agent = agents_db.create_agent(persona=f'{name} {n}', voice_id='v', cwd=str(work), session=f'{name}-{n}')
        sessions[agent] = f'{name}-{n}'
        return agent

    def token_of(agent: str, trace: str) -> str:
        token = provider_jobs.new_turn_token()
        provider_jobs.turn_started(token, agent_id=agent, provider='claude', pid=os.getpid(), trace_id=trace)
        return token

    def staged(case: str) -> tuple[str, str]:
        """An agent, and the token the request presents, as the case has them."""
        agent = fresh('ivy')
        if case == 'the running turn':
            turn_lifecycle.open_turn(agent_id=agent, source='pwa', trace_id='trace-a')
            return agent, token_of(agent, 'trace-a')
        if case == 'no token':
            turn_lifecycle.open_turn(agent_id=agent, source='pwa', trace_id='trace-a')
            return agent, ''
        if case == 'a token that is none':
            turn_lifecycle.open_turn(agent_id=agent, source='pwa', trace_id='trace-a')
            return agent, 'not-a-token-of-this-host'
        if case == 'a token of a turn nobody has':
            turn_lifecycle.open_turn(agent_id=agent, source='pwa', trace_id='trace-a')
            return agent, provider_jobs.new_turn_token()
        if case == "another agent's token":
            other = fresh('oak')
            turn_lifecycle.open_turn(agent_id=other, source='pwa', trace_id='trace-o')
            turn_lifecycle.open_turn(agent_id=agent, source='pwa', trace_id='trace-a')
            return agent, token_of(other, 'trace-o')
        if case == 'the turn ended':
            turn_lifecycle.open_turn(agent_id=agent, source='pwa', trace_id='trace-a')
            token = token_of(agent, 'trace-a')
            turn_lifecycle.close_turns_for_trace(agent, 'trace-a')
            return agent, token
        if case == 'an older turn':
            turn_lifecycle.open_turn(agent_id=agent, source='pwa', trace_id='trace-a')
            token = token_of(agent, 'trace-a')
            turn_lifecycle.close_turns_for_trace(agent, 'trace-a')
            turn_lifecycle.open_turn(agent_id=agent, source='pwa', trace_id='trace-b')
            return agent, token
        if case == 'a running turn without a trace':
            turn_lifecycle.open_turn(agent_id=agent, source='pwa', trace_id='')
            return agent, token_of(agent, '')
        raise AssertionError(case)

    def published(agent: str, token: str, kind: str) -> dict:
        session = sessions[agent]
        trace = Request(token)._publishing_trace(session)
        if kind == 'document':
            return artifacts.create(session=session, type='document', title='A card',
                                    payload={'content': 'words'}, source_trace_id=trace)
        if kind == 'render_spec':
            return artifacts.create(session=session, type='render_spec', title='Build', payload=spec,
                                    source_trace_id=trace)
        return artifacts.create(session=session, type='html_form', title='A report',
                                payload={'content': '<p>a</p>', 'version': '1', 'read_only': True},
                                source_trace_id=trace)

    def named(trace: str) -> str:
        return 'none' if trace == '' else ('the trace' if trace == 'trace-a' else f'another: {trace}')

    vectors = []
    for case, how in CASES:
        for kind in KINDS:
            agent, token = staged(case)
            row = published(agent, token, kind)
            vectors.append({'case': case, 'how': how, 'kind': kind, 'shown': named(row['source_trace_id']),
                            'source_message_id': row['source_message_id']})

    # A later revision, by a later turn: what the first publication had stays.
    for kind in KINDS:
        agent, token = staged('the running turn')
        first = published(agent, token, kind)
        turn_lifecycle.close_turns_for_trace(agent, 'trace-a')
        turn_lifecycle.open_turn(agent_id=agent, source='pwa', trace_id='trace-b')
        later = token_of(agent, 'trace-b')
        session = sessions[agent]
        if kind == 'document':
            artifacts.update(first['artifact_id'], {'payload_patch': {'content': 'more words'}})
        elif kind == 'render_spec':
            revised = json.loads(json.dumps(spec))
            revised['version'] = f"{spec['version']}-again"
            trace = Request(later)._publishing_trace(session)
            artifacts.create(session=session, type='render_spec', title='Build', payload=revised,
                             artifact_id=first['artifact_id'], expected_version=spec['version'],
                             source_trace_id=trace)
        else:
            trace = Request(later)._publishing_trace(session)
            artifacts.create(session=session, type='html_form', title='A report', artifact_id=first['artifact_id'],
                             payload={'content': '<p>b</p>', 'version': '2', 'read_only': True},
                             expected_version='1', source_trace_id=trace)
        now = artifacts.get(first['artifact_id'])
        vectors.append({'case': 'a revision by a later turn', 'how': 'published in its turn that runs, then '
                        'revised in a later turn', 'kind': kind, 'shown': named(now['source_trace_id']),
                        'source_message_id': now['source_message_id']})

    # A lookup that fails is the retryable answer of the turn identity.
    agent, token = staged('the running turn')
    original = turn_identity.caller_turn
    turn_identity.caller_turn = lambda environ=None: (_ for _ in ()).throw(RuntimeError('unreadable'))
    try:
        Request(token)._publishing_trace(sessions[agent])
        failed = 'answered'
    except turn_identity.TurnIdentityUnavailable:
        failed = 'unavailable'
    finally:
        turn_identity.caller_turn = original
    vectors.append({'case': 'a lookup that fails', 'how': 'the turn cannot be read', 'kind': 'document',
                    'shown': failed, 'source_message_id': ''})

    # A request to the user does not pass that route: it names no turn.
    agent, token = staged('the running turn')
    asked = artifacts.create_decision(session=sessions[agent], title='Ship it?', question='Ship it?')
    if asked:
        vectors.append({'case': 'a request to the user', 'how': 'a decision, made by its own route in a running turn',
                        'kind': 'decision', 'shown': named(asked.get('source_trace_id', '')),
                        'source_message_id': asked.get('source_message_id', '')})

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        'about': 'Written by server-rs/tools/source-trace-vectors.py from server/lib/routes/artifacts.py '
                 '(_publishing_trace), server/lib/turn_identity.py and server/lib/artifacts.py. Do not edit by hand.',
        'vectors': vectors}, ensure_ascii=False, sort_keys=True, indent=1) + '\n')
    shown = {}
    for vector in vectors:
        shown[vector['shown']] = shown.get(vector['shown'], 0) + 1
    print(f'{OUT.relative_to(REPO)}: {len(vectors)} publications {dict(sorted(shown.items()))}, '
          f'{OUT.stat().st_size} bytes')
    # The library's connections are closed before its place goes: nothing of it is left.
    db.reset_for_tests()
    return 0


def main(argv: list[str]) -> int:
    if os.environ.get('PYTHONHASHSEED') != '0':
        os.execve(sys.executable, [sys.executable, *argv], {**os.environ, 'PYTHONHASHSEED': '0'})
    with tool_scratch.directory('clarp-source-trace-') as place:
        return generate(pathlib.Path(place))


if __name__ == '__main__':
    sys.exit(main(sys.argv))

#!/usr/bin/env python3
"""Reference argv and one-shot answers of Grok and Muse. No CLI is launched."""
import itertools
import json
import pathlib
import runpy
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = HERE.parent / 'crates/clarp-kernel/tests/fixtures/backend-grok-vectors.json'


def vectors():
    harness = runpy.run_path(str(ROOT / 'tests/replay/host.py'))
    with tempfile.TemporaryDirectory() as directory:
        harness['isolate'](pathlib.Path(directory))
        harness['loopback_only']()
        sys.path.insert(0, str(ROOT / 'server'))
        from lib.backend.registry import by_id
        rows = []
        for tool, sid, fresh, model, effort in itertools.product(
                ('grok', 'muse'), ('', 'synthetic-session'), (False, True),
                ('', 'synthetic-model'), ('', 'high')):
            backend = by_id(tool)
            options = dict(model=model, effort=effort)
            if tool == 'grok':
                options['is_new_session'] = fresh
            rows.append(dict(tool=tool, session=sid, fresh=fresh, model=model, effort=effort,
                             argv=backend.build_cmd(sid, **options)[1:],
                             once=backend.routing_cmd('--question', model=model, effort=effort)[1:]))
        answers = []
        for tool, events in [
                ('grok', [{'type': 'thought', 'data': 'private'}, {'type': 'text', 'data': 'A'},
                          {'event': 'assistant', 'role': 'user', 'content': 'skip'},
                          {'method': 'session/update', 'params': {'update': {
                              'sessionUpdate': 'agent_message_chunk', 'content': [{'text': 'B'}, {'text': 'C'}]}}}]),
                ('muse', [{'payload_type': 'run.output.delta', 'payload': {'text': 'A'}},
                          {'payload_type': 'run.output.delta', 'payload': {'text': 'B'}}]),
                ('muse', [{'payload_type': 'run.output.delta', 'payload': {'text': 'A'}},
                          {'payload_type': 'run.terminal.completed', 'payload': {'text': 'terminal'}}])]:
            printed = 'bad-json\n[]\n' + '\n'.join(map(json.dumps, events))
            answers.append(dict(tool=tool, printed=printed, answer=by_id(tool).routing_text(printed)))
        return dict(commands=rows, answers=answers)


if __name__ == '__main__':
    if sys.argv[1:] == ['--isolated']:
        OUT.write_text(json.dumps(vectors(), indent=1, ensure_ascii=True) + '\n')
    else:
        subprocess.run([sys.executable, '-I', '-B', __file__, '--isolated'], check=True,
                       env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})

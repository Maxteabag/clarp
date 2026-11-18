#!/usr/bin/env python3
"""Reference argv and one-shot answers of Grok and Muse. No CLI is launched."""
import ast
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
        from lib import grok_transcript, muse_transcript, provider_capabilities as capabilities
        # Load the recording function alone: the deployed interpreter has no pytest.
        path = ROOT / 'tests/unit/test_grok_runner.py'
        tree = ast.parse(path.read_text())
        tree.body = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in ('_chat_history_rows', '_stream_events')]
        grok = {'json': json, 'CALL': 'synthetic-call', 'SID': 'synthetic-session'}
        exec(compile(tree, str(path), 'exec'), grok)
        logs = [('grok', grok['_chat_history_rows']())]
        for name in ('session-tool-call', 'session-two-turns'):
            events = []
            for line in (ROOT / f'tests/fixtures/muse/{name}.jsonl').read_text().splitlines():
                original = json.loads(line)
                if original['payload_type'] == 'runtime.session' and original['payload'].get('kind') == 'run':
                    event = original['payload']['event']
                    if event.get('kind') in ('started', 'assistant_message_committed', 'assistant_tool_calls_committed',
                                              'tool_result_batch_committed', 'terminal'):
                        keys = ('kind', 'prompt', 'text', 'tool_calls', 'results', 'terminal', 'reason')
                        events.append(dict(recorded_at=1700000000000000+len(events),
                                           payload_type='runtime.session', payload=dict(
                                               kind='run', event={k: event[k] for k in keys if k in event})))
            logs.append(('muse', events))
        histories = []
        for tool, events in logs:
            log = '\n'.join(map(json.dumps, events))
            path = pathlib.Path(directory) / 'history'
            path.write_text(log)
            parser = grok_transcript if tool == 'grok' else muse_transcript
            histories.append(dict(tool=tool, log=log, turns=parser.parse_turns(path)))
        catalogs = []
        for tool, raw in [
                ('grok', 'You are signed in\nAvailable models:\n * synthetic-a (default)\n'
                         '- synthetic-b\nDefault model: synthetic-a\nsynthetic-a\n'),
                ('grok', '   \nmodels:\nAvailable\n'),
                ('muse', json.dumps(dict(rows=[
                    dict(model_id='test', display_label='Test', visibility='visible',
                         reasoning_effort_variants=[dict(tier=t) for t in ('high','low','high')]),
                    dict(model_id='test', visibility='visible'), dict(model_id='private', visibility='hidden'),
                    dict(model_id='plain', visibility='visible')]))),
                ('muse', '[]')]:
            parse = getattr(capabilities, 'parse_'+tool+'_models')
            catalogs.append(dict(tool=tool, raw=raw, models=parse(raw, observed_at='synthetic')))
        return dict(commands=rows, answers=answers, histories=histories, catalogs=catalogs)


if __name__ == '__main__':
    if sys.argv[1:] == ['--isolated']:
        OUT.write_text(json.dumps(vectors(), indent=1, ensure_ascii=True) + '\n')
    else:
        subprocess.run([sys.executable, '-I', '-B', __file__, '--isolated'], check=True,
                       env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})

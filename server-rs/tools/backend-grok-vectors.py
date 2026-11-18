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
        for kind, field, role in itertools.product(('assistant','agent_message','message','text','output_text'),
                                                    ('content','text','message','delta'), ('model','assistant','user','')):
            event = dict(event=kind,role=role)
            event[field] = {'content':['',{'text':'word'}, {'content':'after'}]}
            raw=json.dumps(event)
            answers.append(dict(tool='grok',printed=raw,answer=by_id('grok').routing_text(raw)))
        for event in [
                {'method':'session/update','params':{'update':{'sessionUpdate':'agent_message','content':'a'}}},
                {'method':'session/update','params':{'update':{'sessionUpdate':'tool','content':'private'}}},
                {'type':'assistant','content':{'text':'preferred','content':'other'},'delta':'later'},
                {'type':'assistant','content':{'content':7},'delta':'later'},
                {'type':'text','data':'','content':'unused'},
                {'type':'assistant','content':False,'text':{},'message':[],'delta':'last'}]:
            raw=json.dumps(event)
            answers.append(dict(tool='grok',printed=raw,answer=by_id('grok').routing_text(raw)))
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
        # Alternate spellings and every translated tool, including missing ids and
        # a result with no matching call. Only synthetic messages are imported.
        names = ['read_file', 'write', 'search_replace', 'grep', 'list_dir', 'todo_write',
                 'web_search', 'web_fetch', 'other']
        logs.append(('grok', [
            dict(type='user', content='ordinary-looking injected context', synthetic_reason='synthetic'),
            dict(role='user', content=dict(content=['a', {'text': 'b'}]), ts='then'),
            dict(type='tool_use', name='custom'),
            dict(type='assistant', content='', tool_calls=[
                dict(name=name, arguments='{"target_file":"f","target_directory":"d"}', id=str(n))
                for n, name in enumerate(names)] + [
                    dict(name='other', arguments='not-json'), dict(arguments=42)]),
            dict(type='tool_result', content='exit: -3\nfailed', tool_use_id='1'),
            dict(type='tool_result', content=['a', dict(text='b'), 2], tool_call_id='0'),
            dict(type='tool_result', content='orphan', tool_call_id='missing'),
            dict(type='backend_tool_call', kind=dict(tool_type='last')),
            dict(type='user', content='  '), dict(role='assistant', content='Done'),
            dict(type='tool_use', name='tail')]))
        def muse_event(kind, **fields):
            return dict(payload_type='runtime.session', recorded_at='1700000001000000',
                        payload=dict(kind='run', event=dict(kind=kind, **fields)))
        for terminal in ('failed', 'aborted', 'cancelled', 'interrupted', 'completed', ''):
            logs.append(('muse', [muse_event('started', prompt='ask'),
                muse_event('assistant_tool_calls_committed', tool_calls=[
                    dict(name=n, args='{"cmd":"c","path":"f"}', id=str(i)) for i,n in enumerate(
                        ('shell','exec_command','edit_file','apply_patch','write_file','grep',
                         'glob','list_dir','web_search','web_fetch','other'))]),
                muse_event('tool_result_batch_committed', results=[dict(tool_call_id='0', is_error=True,text='failed')]),
                muse_event('terminal', terminal=terminal, reason=' reason ')]))
        logs.append(('muse', [muse_event('started', prompt='ask'),muse_event('assistant_message_committed', text='one'),
            muse_event('assistant_message_committed', text='two'),
            muse_event('assistant_tool_calls_committed', tool_calls=[dict(name='read_file', call_id='later', args='{')]),
            muse_event('terminal', terminal='completed'),
            muse_event('assistant_tool_calls_committed', tool_calls=[dict(name='other', id='orphan', args=7)])]))
        # Read each event with the real turn fold. Only storage, speech and notifications
        # are replaced. These never reach the reference Host or a tool.
        from lib.backend import grok as grok_module, muse as muse_module
        from unittest.mock import patch
        from contextlib import ExitStack
        streams = []
        muse_events = []
        for line in (ROOT/'tests/fixtures/muse/exec-tool-call.jsonl').read_text().splitlines():
            r = json.loads(line)
            if r['payload_type'] not in ('run.lifecycle.started','run.model.configured','run.output.delta',
                                         'tool.result','run.usage','run.terminal.completed'):
                continue
            p = r['payload']
            fields = ('model_id','text','call_id','correlation_facts','models','allowance','terminal','reason')
            muse_events.append(dict(payload_type=r['payload_type'],payload={k:p[k] for k in fields if k in p}))
        stories = [('grok', grok['_stream_events']()), ('muse', muse_events)]
        for error in ('error','turn.failed','turn_failed'):
            stories.append(('grok',[dict(type=error,error=dict(message='failed'))]))
        for terminal in ('aborted','failed','cancelled','interrupted'):
            stories.append(('muse',[dict(payload_type='run.terminal.'+terminal,payload=dict(reason='refused'))]))
        kinds = ['tool_call','tool_started','tool_start','tool_use','item.started']
        for i, kind in enumerate(kinds):
            status = ('completed','failed','error','cancelled','in_progress')[i]
            stories.append(('grok', [
                dict(event='turn.started'), dict(event='thought'), dict(event='text', data='before'),
                dict(event=kind, toolCallId='call', **{('toolName','title','name','tool','ignored')[i]:'read_file'}),
                dict(event='tool_call_update', toolCallId='call', status=status), dict(event='text',data='after')]))
        for kind in ('turn_started','thought'):
            stories.append(('grok',[dict(type=kind)]))
        for field in ('usage', 'usage_update'):
            stories.append(('grok',[dict(type=field,usage=dict(input_tokens=x,output_tokens=y))
                                    for x,y in [(7,'2'),('bad',5),(False,True),(3.5,4),(0,None)]]
                            +[dict(type='end',usage={})]))
        stories.append(('muse',[dict(payload_type='run.usage', payload={'models':rows}) for rows in [
            [dict(input_tokens='5',output_tokens=2),3,dict(input_tokens=9,output_tokens='bad')],
            [dict(input_tokens='bad',output_tokens=6)], [dict(input_tokens=1,output_tokens=True)]]]))
        for tool, events in stories:
            adapter = by_id(tool)
            st = (grok_module if tool=='grok' else muse_module)._TurnState()
            steps = []
            with ExitStack() as stack:
                for name in ('_transition','_broadcast','_persist_live_text','_speak' if tool=='grok' else 'speak'):
                    stack.enter_context(patch.object(adapter,name,lambda *a,**kw: None))
                for ev in events:
                    adapter._handle_event(ev,st,agent_id='synthetic',session='synthetic',trace_id='trace',
                                          on_error=None,stream=None,enqueue=None)
                    steps.append(dict(event=ev,text=st.live_text,all_text=st.turn_text,
                                      phase=st.phase,usage=[st.tokens_in,st.tokens_out],error=st.failed_error,
                                      stopped=getattr(st,'stopped',False),model=getattr(st,'model','')))
            streams.append(dict(tool=tool,steps=steps))
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
        from lib.backend.long_prompt import fits_argv
        bounds = []
        for n, tail in [(0,''),(32767,'a'),(32768,''),(32768,'a')]:
            bounds.append(dict(pairs=n,tail=tail,inline=fits_argv(chr(0xdf)*n+tail)))
        return dict(bounds=bounds, commands=rows, answers=answers, histories=histories, catalogs=catalogs, streams=streams)


if __name__ == '__main__':
    if sys.argv[1:] == ['--isolated']:
        OUT.write_text(json.dumps(vectors(), indent=1, ensure_ascii=True) + '\n')
    else:
        subprocess.run([sys.executable, '-I', '-B', __file__, '--isolated'], check=True,
                       env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})

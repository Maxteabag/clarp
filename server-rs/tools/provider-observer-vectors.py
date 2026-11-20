#!/usr/bin/env python3
"""Record the Python provider observer's visits to synthetic bound runtimes."""
import importlib.util
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch as tool_scratch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'server-rs/crates/clarp-kernel/tests/fixtures/provider-observer.json'
NOW = 1800000000000
DAY = 86400000


def record(directory):
    spec = importlib.util.spec_from_file_location('isolated_host', ROOT / 'tests/replay/host.py')
    isolated = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(isolated)
    isolated.isolate(directory)
    isolated.loopback_only()
    sys.path.insert(0, str(ROOT / 'server'))
    from lib import agents, backends, db, provider_background_jobs as provider

    db.now_ms = lambda: NOW
    c = db.conn()
    owners = ['agent-a', 'agent-b', 'agent-c']
    for agent in owners:
        with patch.object(agents, '_new_agent_id', return_value=agent):
            agents.create_agent(persona='Fixture', voice_id='', cwd='/fixture', session=agent)
    actual = []
    def selected(owner, native, *, ended):
        actual.append([owner['agent_id'], native])
        return False

    cases = []
    ends = [None, 1, DAY-1, DAY, DAY+1]
    with patch.object(backends.by_id('claude'), 'find_transcript', return_value=None), \
         patch.object(provider, 'reconcile_if_needed', side_effect=selected), \
         patch.object(provider.time, 'monotonic', return_value=1):
        for mask, end, unfinished in itertools.product(range(8), ends, [False, True]):
            c.execute('delete from runtimes')
            c.execute('delete from background_jobs')
            rows = []
            for i, agent in enumerate(owners):
                if not mask & (1 << i):
                    continue
                # The reference asks whether this owner has any unfinished
                # native job, including one in another native session.
                if unfinished:
                    c.execute("""insert into background_jobs(job_id,agent_id,session,kind,title,status,started_at,updated_at)
                    values(?,?,?,'provider-task','Fixture','running',?,?)""",
                              ('test-'+agent, agent, agent, NOW, NOW))
                for j in range(i+1):
                    native = 'native-'+str(i)+'-'+str(j)
                    ended = NOW-end if end is not None and j==0 else None
                    c.execute('insert into runtimes(agent_id,session,backend_session_id,started_at,ended_at) values(?,?,?,?,?)',
                              (agent,agent,native,NOW-2*DAY,ended))
                    rows.append([agent,native,ended,unfinished])
            observer = provider.ProviderJobObserver()
            for after in [('', ''), ('agent-a','native-0-0'), ('agent-b','native-1-0'), ('zz','last')]:
                observer.after = after
                actual.clear()
                observer.poll_once()
                cases.append([rows,after,list(actual)])
    # Observe the actual files through the reference's cursor transaction.
    # Keep the bytes as strings so offsets can be compared directly in Rust.
    native = 'parent'
    parent = directory / 'project' / (native + '.jsonl')
    parent.parent.mkdir(exist_ok=True)
    stamp = '2027-01-15T08:00:00Z'
    def launches(start, count):
        content = [dict(type='tool_use', name='Bash', id='call-' + str(i),
                        input=dict(run_in_background=True)) for i in range(start,start+count)]
        return json.dumps(dict(type='assistant',timestamp=stamp,message=dict(content=content))) + '\n'
    first = launches(0,1)
    middle = launches(1,1)
    blocks = launches(2,130)
    writes = [('replace','bad JSON\n'+first), ('append',middle[:-1]), ('append',''),
              ('append','\n'), ('append',blocks), ('append',''), ('append',''),
              ('truncate',launches(200,1)), ('replace',launches(201,1)),
              ('append','null\n'), ('append','')]
    reads = []
    owner = dict(agent_id=owners[0],session=owners[0],bound_at=NOW)
    with patch.object(provider.time,'monotonic',return_value=1), \
         patch.object(provider,'apply_actions',wraps=provider.apply_actions) as apply:
        for operation, data in writes:
            if operation=='replace':
                fresh = parent.with_suffix('.new')
                fresh.write_bytes(data.encode())
                fresh.replace(parent)
            else:
                with parent.open('ab' if operation=='append' else 'wb') as f:
                    f.write(data.encode())
            apply.reset_mock()
            provider.observe(owner,native,parent)
            cursor=c.execute('select position,action_offset,discarding from provider_job_cursors where agent_id=? and native_id=?',
                             (owners[0],native)).fetchone()
            actions=[a['tool'] for call in apply.call_args_list for a in call.args[3]]
            reads.append(dict(operation=operation,data=data,cursor=list(cursor),tools=actions))
    return cases, reads


if __name__ == '__main__':
    if '--record' in sys.argv:
        with tool_scratch.directory() as root:
            print(json.dumps(record(Path(root))))
    else:
        data = json.loads(subprocess.check_output(
            [sys.executable,'-I',__file__,'--record'], env=tool_scratch.child_env({'PATH':os.defpath,'TZ':'UTC','LANG':'C.UTF-8'})))
        cases, reads = data
        for path, rows in [(OUT,cases),(OUT.with_name('provider-reader.json'),reads)]:
            path.write_text('[\n'+',\n'.join(json.dumps(row) for row in rows)+'\n]\n',encoding='ascii')
        print(len(cases), 'binding selections and', len(reads), 'file visits')

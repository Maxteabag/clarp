#!/usr/bin/env python3
"""Lifecycle refusals from the Python Host, with a disposable roster and credential directory."""
import json
from pathlib import Path
import runpy
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch as tool_scratch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT/'server-rs/crates/clarp-kernel/tests/fixtures/goal-delegation.json'


@tool_scratch.case('clarp-goal-delegation-')
def reference(directory):
    harness = runpy.run_path(str(ROOT/'tests/replay/host.py'))
    harness['isolate'](directory)
    harness['loopback_only']()
    sys.path.insert(0, str(ROOT/'server'))
    from lib import db, goal_ledger as ledger
    assert db.DB_PATH.resolve().is_relative_to(directory)
    principal = dict(agent_id='principal', role='agent', backend='claude')
    helper = dict(agent_id='helper', role='helper', parent_agent_id='principal', backend='grok')
    rows = []
    def enable(p, h, reason=' keep ', baseline=200):
        scheme = dict(principal=p, helper=h, reason=reason, baseline=baseline)
        try:
            row = ledger.enable(p, h, reason, baseline_messages=baseline)
        except ledger.LedgerError as error:
            scheme['error'] = str(error)
        else:
            scheme['reason_stored'] = row['created_reason']
            scheme['status'] = row['status']
            assert Path(row['token_path']).resolve().is_relative_to(directory)
            ledger.stop(row['delegation_id'], 'finished')
        rows.append(scheme)
    with patch.object(db,'now_ms',return_value=100):
        for n in [-1, 0, 1, 200, 2000, 2001, '1', True]:
            enable(principal, helper, baseline=n)
        for reason in ['', '  ', 'x'*500,'x'*501]:
            enable(principal,helper,reason)
        for change in [dict(agent_id='principal'),dict(parent_agent_id='other'),dict(role='agent'),
                       dict(archived_at=100),dict(deleted_at=100)]:
            enable(principal,dict(helper,**change))
        for backend in ['claude','codex','agy','grok','opencode','deepseek','muse','unknown']:
            enable(dict(principal,backend=backend),helper)
            enable(principal,dict(helper,backend=backend))
    from lib import agents
    actors=[('p','claude',None),('h','grok','p'),('child','claude','h'),('outsider','claude',None),
            ('no-identity','codex',None),('descendant-no-identity','codex','child')]
    for name,backend,parent in actors:
        with patch.object(agents,'_new_agent_id',return_value=name):
            agents.create_agent(persona=name,session=name,backend=backend,voice_id='',cwd=str(directory))
        if parent:
            db.conn().execute("UPDATE agents SET parent_agent_id=?,role='helper' WHERE agent_id=?",(parent,name))
    p,h = (agents.get_by_agent_id(name) for name in ('p','h'))
    checks=[]
    with patch.object(db,'now_ms',return_value=300), patch('secrets.token_hex',return_value='0123456789abcdef'):
        started=ledger.enable(p,h,'active test')
        did=started['delegation_id']
        for kind in ('owner','peer','user','system','legacy','delegate','unattributed'):
            for name,backend,_ in actors:
                for verified in (False,True):
                    who=ledger._attribute(dict(kind=kind,agent_id=name,verified=verified),'p')
                    why=None
                    try:
                        ledger.guard_principal_goal(db.conn(),'p',who)
                    except ledger.DelegationDenied as exc:
                        why=str(exc)
                    checks.append(dict(kind=who['kind'],verified=verified,
                                       descendant=ledger._descends_from(db.conn(),who['agent_id'],'h'),
                                       backend=agents.get_by_agent_id(who['agent_id'])['backend'],error=why))
        visible=ledger.delegation(did)
        finished=ledger.stop(did,'  finished  ')
        again=ledger.stop(did,'duplicate')
        assert finished==again
        assert all('token_hash' not in r for r in [visible,finished])
    return dict(guards=checks,active=visible,stopped=finished,written_by=f'goal-delegation-vectors.py with Python {sys.version_info.major}.{sys.version_info.minor}', enable=rows)


if __name__=='__main__':
    OUT.write_text(json.dumps(reference(),ensure_ascii=True,separators=(',',':'))+'\n')

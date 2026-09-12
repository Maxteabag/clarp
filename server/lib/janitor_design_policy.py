"""Host-owned candidate policy services. No credentials, inference or process control.

Settings use the existing Host transaction/database. Default behavior is unchanged.
"""
from __future__ import annotations
import hashlib
import json
import math
from . import db, janitors, settings_store, backends

KEY = 'janitors.design_policy.v1'

def configuration():
    return json.loads(settings_store.get_text(KEY, default='{"revision":0,"model_chain":[],"inherit_sessions":[]}'))

def configure(value, expected_revision):
    if not isinstance(value, dict) or set(value)-{'model_chain','inherit_sessions'}:
        raise ValueError('Unsupported policy fields')
    if 'inherit_sessions' in value:
        if not isinstance(value['inherit_sessions'],list) or any(not isinstance(s,str) or not janitors.get(s) for s in value['inherit_sessions']):raise ValueError('Expected existing Janitor sessions')
        if len(set(value['inherit_sessions']))!=len(value['inherit_sessions']):raise ValueError('Duplicate inherited session')
    if 'model_chain' in value:
        chain=value['model_chain']
        if not isinstance(chain,list) or not chain:raise ValueError('At least one model is required')
        for model in chain:
            if not isinstance(model,dict) or set(model)!={'provider','model'} or any(not isinstance(v,str) or not v.strip() or len(v)>200 for v in model.values()):raise ValueError('Each model needs provider and model identifiers')
        for model in chain:
            if model['provider'] != 'openai' and not backends.get(model['provider']):raise ValueError('Unsupported model provider')
            if model['provider'] != 'openai' and not backends.is_valid_model(model['provider'],model['model']):raise ValueError('Invalid provider model identifier')
        if len({json.dumps(v,sort_keys=True) for v in chain})!=len(chain):raise ValueError('Duplicate model')
    with janitors._write():
        current=configuration()
        if type(expected_revision) is not int or current['revision']!=expected_revision:raise janitors.JanitorError('Policy changed; refresh before saving',409,'revision_conflict')
        candidate={**current,**value}
        chain=candidate.get('model_chain',[])
        for session in candidate.get('inherit_sessions',[]):
            config=janitors.get(session)
            if not config or not chain:raise ValueError('Inherited Janitors require a primary model')
            if config.get('execution',{}).get('executor')=='deterministic' or config.get('execution',{}).get('provider')=='local':raise ValueError('Deterministic Janitors do not inherit models')
            supported=janitors.template(config['template_id']).get('supported_providers',[config['backend']])
            if any(v['provider'] not in supported for v in chain):raise ValueError('Model provider incompatible with '+session)
            if config['template_id']=='task-labels' and chain[0]['provider']!=config['backend']:raise ValueError('Managed primary provider must match the Janitor backend')
        current.update(value);current['revision']+=1
        settings_store.set_text(KEY,json.dumps(current,sort_keys=True))
    return current

def effective_chain(session):
    current=janitors.get(session)
    if not current:raise janitors.JanitorError('Janitor unavailable',404,'not_found')
    # Explicit existing model is preserved, never silently migrated to inheritance.
    if current.get('execution',{}).get('executor')=='deterministic' or current.get('execution',{}).get('provider')=='local':
        return {'source':'deterministic','chain':[],'revision':current['revision']}
    global_config=configuration()
    if session not in global_config.get('inherit_sessions',[]):
        return {'source':'explicit','chain':[{'provider':current.get('backend'),'model':current['model']}], 'revision':current['revision']}
    config=configuration()
    return {'source':'global','chain':config.get('model_chain',[]),'revision':config['revision']}

"""Bounded independent-scope demand execution using existing Host claims.

One serial partition per trusted target; computation never holds a writer lock.
A busy admission is returned as deferred, not silently dropped or resubmitted.
This adapter does not launch providers or retry unknown spend automatically.
"""
from concurrent.futures import ThreadPoolExecutor
from . import janitor_builtins

def execute_partitions(partitions, compute, *, workers=2):
    if type(workers) is not int or not 1 <= workers <= 8:
        raise ValueError('workers must be 1 through 8')
    targets=[p['target_agent_id'] for p in partitions]
    if len(set(targets))!=len(targets):raise ValueError('Targets must have one serial owner')
    def partition(part):
        results=[]
        for request in part['requests']:
            run=janitor_builtins.begin_run('tool-explainer',request['request_id'],context=request.get('context',{}),target_agent_id=part['target_agent_id'])
            if run is None:
                results.append({'request_id':request['request_id'],'status':'deferred'});continue
            if run.get('demand_result') is not None:
                results.append({'request_id':request['request_id'],'status':'receipt','result':run['demand_result']});continue
            if not janitor_builtins.claim_run(run['run_id']):
                results.append({'request_id':request['request_id'],'status':'claimed_elsewhere'});continue
            try:
                result=compute(request)
            except Exception:
                # Record bounded metadata; arbitrary provider exception text can contain secrets.
                janitor_builtins.complete_run(run['run_id'],'failed',error='Computation failed; inspect authorized diagnostics')
                results.append({'request_id':request['request_id'],'status':'failed'});continue
            accepted=janitor_builtins.complete_run(run['run_id'],result=result)
            results.append({'request_id':request['request_id'],'status':'accepted' if accepted else 'stale_rejected'})
        return results
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return [r for batch in pool.map(partition,partitions) for r in batch]

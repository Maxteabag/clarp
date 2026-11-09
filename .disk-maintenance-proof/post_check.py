"""Owner-side postcheck of hash-bound metadata captures; no SQL/status edits or sends."""
import hashlib
import json
import os
from pathlib import Path
import sys
import stat
import traceback
from writer_policy import validate_workers, classify_post, read_exit
from controller import Controller, SHARE
from roster import load_receipt


def capture_registry(observer, workers, protected):
    db=SHARE/'state.sqlite';before=db.stat()
    identity=(protected['state.sqlite']['dev'],protected['state.sqlite']['inode'])
    if (before.st_dev,before.st_ino)!=identity:raise SystemExit(11)
    ids=[w['job_id'] for w in workers.values()]
    if any(not isinstance(x,str) or not 0<len(x)<=512 for x in ids):raise SystemExit(11)
    literal=lambda x:"'"+x.replace("'","''")+"'"
    sql=("PRAGMA query_only=ON;BEGIN;SELECT job_id,session,generation,status,heartbeat_at,worker_pid,"
         "worker_start_token,terminal_reason FROM background_jobs WHERE job_id IN ("+','.join(literal(x) for x in ids)+");COMMIT;")
    text=observer.require(['/usr/bin/sqlite3','-batch','-bail','-readonly','-json','-cmd','.timeout 2000',str(db),sql],10)
    after=db.stat()
    if (after.st_dev,after.st_ino)!=identity:raise SystemExit(11)
    rows=json.loads(text)
    if len(rows)>len(workers) or len({r['job_id'] for r in rows})!=len(rows):raise SystemExit(11)
    return {r['job_id']:r for r in rows}


def main():
    if len(sys.argv) != 4:
        raise SystemExit('usage: POST_METADATA_JSON SHA256 NEW_OUTPUT')
    source = Path(sys.argv[1])
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > 1024 * 1024 or before.st_uid != os.getuid():
            raise SystemExit(11)
        raw = os.read(fd, 1024 * 1024 + 1)
        after = os.fstat(fd)
        stamp = lambda x: (x.st_dev,x.st_ino,x.st_size,x.st_mtime_ns,x.st_ctime_ns)
        if stamp(before) != stamp(after) or stamp(after) != stamp(source.stat(follow_symlinks=False)) or hashlib.sha256(raw).hexdigest() != sys.argv[2]:
            raise SystemExit(11)
    finally:
        os.close(fd)
    data = json.loads(raw)
    if data['schema'] != 'clarp.disk-writer-post-metadata/1': raise SystemExit(11)
    frozen=load_receipt(data['roster_receipt']['path'],data['roster_receipt']['sha256'])
    if data['original_binding_sha256']!=frozen['original_binding_sha256']:
        raise SystemExit('post_roster_authority_mismatch')
    binding=frozen['controller_binding']
    if data['pre_ms']!=frozen['end_epoch_ms']:raise SystemExit('post_roster_epoch_mismatch')
    workers = validate_workers(binding['tolerated_workers'],binding['auxiliary_units']+binding['timers']+['clarp.service','clarp-runtime.service'])
    native_root=Path(sys.argv[3]+'.native');native_root.mkdir(mode=0o700)
    observer=Controller(binding,native_root)
    registry=capture_registry(observer,workers,binding['protected_db_files'])
    results = []
    for name, expected in workers.items():
        row=registry.get(expected['job_id'],{})
        try:
            actual=observer.worker_snapshot(expected)
            receipt=read_exit(data['exit_bindings'][name],expected,data['pre_ms']) if data['has_exit_receipt'][name] else None
            results.append(classify_post(expected,actual,row,receipt,data['restart_ms']))
        except Exception as exc:
            diagnostic_traceback=traceback.format_exc()
            results.append({'unit':name,'job_id':expected['job_id'],'owner':expected['owner'],
                            'state':'UNKNOWN_REQUIRES_OWNER','error':type(exc).__name__+':'+str(exc),
                            'traceback':diagnostic_traceback,'last_heartbeat_at':row.get('heartbeat_at'),
                            'log_exit_path':expected.get('log_exit',{}).get('path'),
                            'automatic_status_edit':False})
    output = {'state':'ALL_RECONCILED' if all(r['state'] in ('ACTIVE_HEARTBEAT_RESUMED','FINISHED_LOG_EXIT_RECONCILED','PREFINISHED_RECONCILED') for r in results) else 'REPORT_LEAD_AND_OWNERS_NO_EDIT',
              'metadata_sha256':sys.argv[2], 'results':results, 'no_automatic_status_edit':True,
              'limits':'Owner must freshly join native unit/birth, readonly registry and exact LOG.exit provenance; hashes are not authority.'}
    fd = os.open(sys.argv[3], os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd,'w') as f:json.dump(output,f,indent=2);f.write('\n')
    return 0 if output['state']=='ALL_RECONCILED' else 11

if __name__=='__main__':raise SystemExit(main())

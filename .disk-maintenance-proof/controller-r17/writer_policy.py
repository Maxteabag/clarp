"""Exact tolerated-unit census and read-only post-result classification.

No DB writes, no signals or starts. Input receipts require native owner qualification.
"""
import os
from pathlib import Path
import stat
import hashlib

class WriterUnknown(RuntimeError):
    pass


def validate_workers(workers, stopped):
    if not isinstance(workers,list) or not 0<len(workers)<=64:
        raise WriterUnknown('current_active_collection_missing_or_over_cap')
    names = [w['unit'] for w in workers]
    if len(set(names)) != len(workers) or set(names) & set(stopped):
        raise WriterUnknown('tolerated_stop_overlap_or_duplicate')
    for w in workers:
        if not w['unit'].startswith('clarp-job-') or not w['unit'].endswith('.service'):
            raise WriterUnknown('not_classified_job_unit')
        if not (w['pid'] > 0 and w['start'] and w['invocation'] and w['cgroup'] and w['boot'] and w['job_id'] and w['owner']):
            raise WriterUnknown('incomplete_native_job_binding')
        if w.get('phase')!='ACTIVE':
            raise WriterUnknown('retired_history_not_current_tolerance')
        if w['uid'] != os.getuid():
            raise WriterUnknown('foreign_worker_uid')
    return {w['unit']: w for w in workers}


def require_same_worker(expected, actual):
    keys = ('unit', 'pid', 'start', 'boot', 'invocation', 'cgroup', 'uid')
    if any(expected[k] != actual.get(k) for k in keys) or actual.get('state') != 'active':
        raise WriterUnknown('worker_incarnation_changed')


def classify_fd(fd, workers, observed_units, protected):
    """Accept only an exact protected DB FD within a positively joined worker unit."""
    role = fd['role']
    expected_file = protected[role]
    if (fd['dev'], fd['inode']) != (expected_file['dev'], expected_file['inode']):
        raise WriterUnknown('protected_db_fd_rebound')
    matches = [w for w in workers.values() if fd['cgroup'] == w['cgroup'] or fd['cgroup'].startswith(w['cgroup'] + '/')]
    if len(matches) != 1:
        raise WriterUnknown('unlisted_db_fd')
    worker = matches[0]
    if worker.get('phase', 'ACTIVE') != 'ACTIVE':
        raise WriterUnknown('prefinished_writer_reappeared')
    require_same_worker(worker, observed_units[worker['unit']])
    if not (fd['uid'] == worker['uid'] and fd['boot'] == worker['boot'] and fd['pid'] > 0 and fd['start']):
        raise WriterUnknown('fd_process_identity_unknown')
    # A leader FD must itself carry the bound leader birth. Descendant FDs carry
    # their own before/after proc birth and the same unit subtree in native capture.
    if fd['pid'] == worker['pid'] and fd['start'] != worker['start']:
        raise WriterUnknown('fd_leader_birth_changed')
    return worker['unit']


def context_of(w):
    return {'job_id':w['job_id'],'generation':w['generation'],'owner':w['owner'],
            'worker_pid':w['pid'],'worker_start_token':w['boot']+':'+w['start']}


def version_of(st):
    return [st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns]


def read_exit(binding, expected, reference_ms):
    """Actual receipt reader enforces path/version/hash/generation and pre-window provenance."""
    if binding['path'] != expected['log_exit']['path'] or binding['context'] != context_of(expected):
        raise WriterUnknown('exit_context_or_path')
    path=Path(binding['path'])
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK)
    try:
        before=os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid!=os.getuid() or before.st_size>32:
            raise WriterUnknown('exit_receipt_type_owner_size')
        raw=os.read(fd,33);after=os.fstat(fd)
        digest=hashlib.sha256(raw).hexdigest()
        if version_of(before)!=version_of(after) or version_of(after)!=version_of(path.stat(follow_symlinks=False)):
            raise WriterUnknown('exit_receipt_rebound')
        if binding['version']!=version_of(after) or binding['sha256']!=digest:
            raise WriterUnknown('exit_version_hash_changed')
        if expected.get('phase','ACTIVE')=='ACTIVE':
            pre=expected['log_exit']
            if before.st_mtime_ns < reference_ms*1000000:
                raise WriterUnknown('exit_before_frozen_roster_epoch')
            if pre['pre_state']=='HISTORICAL':
                old=pre['historical']
                if old['context']!=context_of(expected) or old['path']!=str(path):
                    raise WriterUnknown('historical_exit_context')
                if version_of(before)==old['version'] or before.st_mtime_ns<=old['version'][3]:
                    raise WriterUnknown('unchanged_historical_exit_not_new_completion')
            elif pre['pre_state']!='ABSENT':
                raise WriterUnknown('exit_prestate_UNKNOWN')
        else:
            seal=expected['log_exit']['sealed']
            if seal['version']!=version_of(after) or seal['sha256']!=digest:
                raise WriterUnknown('prefinished_receipt_changed')
        value=raw.decode('ascii').strip()
        if not value or len(value)>5 or not value.lstrip('-').isdigit():raise WriterUnknown('exit_receipt_parse')
        return {'rc':int(value),'path':str(path),'sha256':digest,'version':version_of(after),
                'context':context_of(expected),'reader_verified':True}
    finally:os.close(fd)


def require_terminal(expected, actual):
    common=('unit','boot','invocation','cgroup','uid')
    if (any(expected[k]!=actual.get(k) for k in common) or actual.get('state') not in ('inactive','failed')
            or actual.get('pid')!=0 or actual.get('original_gone') is not True or actual.get('cgroup_empty') is not True):
        raise WriterUnknown('terminal_native_unknown_or_changed')


def classify_post(expected,actual,row,receipt,restart_ms):
    result={'unit':expected['unit'],'job_id':expected['job_id'],'owner':expected['owner'],
            'state':'UNKNOWN_REQUIRES_OWNER','automatic_status_edit':False,
            'last_heartbeat_at':row.get('heartbeat_at'),'log_exit_path':expected.get('log_exit',{}).get('path')}
    if (row.get('job_id')!=expected['job_id'] or row.get('session')!=expected['owner'] or row.get('generation')!=expected['generation']
            or row.get('worker_pid')!=expected['pid'] or row.get('worker_start_token')!=expected['boot']+':'+expected['start']):return result
    try:
        if expected.get('phase','ACTIVE')=='PREFINISHED':require_terminal(expected,actual)
        elif actual.get('state')=='active':require_same_worker(expected,actual)
        else:require_terminal(expected,actual)
    except WriterUnknown:return result
    if expected.get('phase','ACTIVE')=='ACTIVE' and actual.get('state')=='active' and row.get('status') in ('queued','running'):
        if row.get('heartbeat_at',0)>restart_ms:result['state']='ACTIVE_HEARTBEAT_RESUMED'
        return result
    if receipt is None or receipt.get('reader_verified') is not True or receipt.get('context')!=context_of(expected):return result
    correct='succeeded' if receipt['rc']==0 else 'failed'
    result.update(exit=receipt,recorded_status=row.get('status'),recorded_reason=row.get('terminal_reason'))
    inferred={'expired','heartbeat_expired','worker_vanished'}
    if row.get('status')==correct and row.get('terminal_reason') not in inferred:
        result['state']='PREFINISHED_RECONCILED' if expected.get('phase','ACTIVE')=='PREFINISHED' else 'FINISHED_LOG_EXIT_RECONCILED'
    else:result['state']='MIS_MARK_REPORT_LEAD_AND_OWNER'
    return result


def validate_heartbeat_age(workers,now_ms):
    for w in workers:
        if w.get('phase','ACTIVE')!='ACTIVE':continue
        row=w['pre_registry']
        if (row.get('job_id')!=w['job_id'] or row.get('generation')!=w['generation']
                or row.get('session')!=w['owner'] or row.get('worker_pid')!=w['pid']
                or row.get('worker_start_token')!=w['boot']+':'+w['start']
                or row.get('status') not in ('queued','running')
                or not 0<=now_ms-row.get('heartbeat_at',0)<=60000):
            raise WriterUnknown('pre_heartbeat_age_or_identity_unqualified')

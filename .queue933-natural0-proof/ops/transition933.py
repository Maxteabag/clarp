"""Attempt-agnostic 933 owner glue. SOURCE CANDIDATE: no selection file shipped.

No application imports, SQL implementation, migrations, repair or new send API.
Large parsing/hashing belongs to the bounded worker, never the R4 polling loop.
"""
from pathlib import Path
import hashlib
import importlib.util
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
import traceback

CHECKER_HASH = '6046c9b988eee6fb985dba676c2560074a7c022432bfa664633d3db5bcc513ca'
CHECKER_SOURCE = '31cc8110f2c8d30e99d3e5a447c3f6e8fc035beb'
WHOLE_SOURCE = '6245ab37003f0e2c854eebce9a30685d808bee9c'
CHECKER_MF = '89dd2ce00a18c7b883fe85317918e2a903459c5e7e18d9b3350f034e9f4820bb'
CAP = 64 * 1024 * 1024
SMALL = 65536
STAGES = ('pre', 'post', 'pre830', 'replay')


class Refused(RuntimeError):
    """Terminal pre-invocation refusal; NEVER BaselineRefused retry."""


class OwnershipUnknown(RuntimeError):
    """Owned process not reconciled, distinct from report-only DB UNKNOWN."""


def bounded_version(path, cap=SMALL, expected=None):
    path = Path(path)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError('noncanonical_path')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o022 or not 0 < s.st_size <= cap:
            raise ValueError('owned_regular_size_mode')
        identity = (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        body = bytearray()
        while len(body) <= cap:
            part = os.read(fd, min(65536, cap + 1 - len(body)))
            if not part:
                break
            body.extend(part)
        a = os.fstat(fd)
        n = os.stat(path, follow_symlinks=False)
        if identity != (a.st_dev, a.st_ino, a.st_size, a.st_mtime_ns, a.st_ctime_ns) or identity != (n.st_dev, n.st_ino, n.st_size, n.st_mtime_ns, n.st_ctime_ns):
            raise ValueError('rebound_input')
        digest = hashlib.sha256(body).hexdigest()
        binding = dict(path=str(path), dev=s.st_dev, inode=s.st_ino, size=s.st_size, sha256=digest)
        if len(body) != s.st_size or expected is not None and binding != expected:
            raise ValueError('binding_changed')
        # Stamp is derived from the SAME verified fd/version as the bytes/hash.
        # Never learn a fresh named-path version after this read has completed.
        stamp = dict(dev=s.st_dev, inode=s.st_ino, size=s.st_size, mtime_ns=s.st_mtime_ns, ctime_ns=s.st_ctime_ns)
        return bytes(body), binding, stamp
    finally:
        os.close(fd)


def bounded(path, cap=SMALL, expected=None):
    raw, binding, _stamp = bounded_version(path, cap, expected)
    return raw, binding


def read_json_version(path, cap=SMALL, expected=None):
    raw, binding, stamp = bounded_version(path, cap, expected)
    return json.loads(raw.decode('utf-8')), binding, stamp


def read_json(path, cap=SMALL, expected=None):
    raw, binding = bounded(path, cap, expected)
    return json.loads(raw.decode('utf-8')), binding


def exclusive(path, body):
    raw = (json.dumps(body, sort_keys=True, ensure_ascii=True) + '\n').encode()
    if len(raw) > SMALL:
        raise ValueError('envelope_size')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())


def directory(path):
    p = Path(path)
    s = p.stat(follow_symlinks=False)
    if not p.is_absolute() or p.resolve(strict=True) != p or not stat.S_ISDIR(s.st_mode) or s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) != 0o700:
        raise ValueError('private_root')
    return p


def digest_file(path, expected, cap=CAP):
    raw, b = bounded(Path(path), cap)
    if b['sha256'] != expected:
        raise ValueError('source_hash')
    return raw


def load_attempt(root):
    """No template/default capsule is installed. Future B review selects real bytes."""
    root = Path(root).resolve(strict=True)
    c, binding = read_json(root / 'ATTEMPT.json')
    if c.get('schema') != 'queue933.attempt/1' or c.get('selection') != 'FINAL_B_REVIEWED_CAPSULE':
        raise Refused('unselected_attempt')
    for key in ('sha', 'operator_source'):
        v = c.get(key, '')
        if not re.fullmatch('[0-9a-f]{40}', v) or len(set(v)) < 4:
            raise Refused('unbound_' + key)
    if not re.fullmatch(r'refactor-\d{8}-[1-9]\d*', c.get('tag', '')):
        raise Refused('unbound_tag')
    for k in ('previous_host', 'previous_runtime', 'predicted_runtime', 'job', 'native_session', 'goal'):
        if not isinstance(c.get(k), str) or not c[k] or any(x in c[k].upper() for x in ('UNSET', 'PLACEHOLDER', 'TODO', '<', '>')):
            raise Refused('unbound_' + k)
    if c['native_session'] != os.environ.get('CODEX_THREAD_ID') or c['goal'] != 'b149b798e5e23484:queue933-release:100699a1':
        raise Refused('native_goal')
    if not c['job'].startswith('rf-releasec-queue933-') or c['previous_runtime'] == c['predicted_runtime']:
        raise Refused('attempt_job_runtime')
    if (root / 'SOURCE_COMMIT').read_text().strip() != c['operator_source']:
        raise Refused('operator_source')
    mf = digest_file(root / 'SHA256SUMS', c['operator_manifest'], SMALL)
    for line in mf.decode().splitlines():
        h, name = line.split(maxsplit=1)
        p = root / name.lstrip('*')
        if p.resolve(strict=True).is_relative_to(root) is False:
            raise Refused('closure_escape')
        digest_file(p, h)
    required = {'plan', 'READY', 'operator_B', 'final_TestsCI', 'skill_gate', 'quiescence', 'r4_acceptance', 'owned_preflight'}
    if set(c['receipts']) != required:
        raise Refused('final_receipts_missing')
    for ref in c['receipts'].values():
        digest_file(ref['path'], ref['sha256'])
    plan, _ = read_json(c['receipts']['plan']['path'])
    if plan['sha'] != c['sha'] or plan['live_release'] != c['previous_host'] or plan['live_runtime_release_id'] != c['previous_runtime'] or plan['predicted']['runtime_release_id'] != c['predicted_runtime']:
        raise Refused('plan_binding')
    if type(c.get('deadline_epoch')) not in (int, float) or not time.time() < c['deadline_epoch'] < time.time() + 86400:
        raise Refused('deadline')
    db = Path(c['db']['path'])
    if not db.is_absolute() or db.resolve(strict=True) != db or not stat.S_ISREG(db.stat().st_mode):
        raise Refused('db_canonical_regular')
    ds = db.stat()
    if (ds.st_dev, ds.st_ino) != (c['db']['dev'], c['db']['inode']):
        raise Refused('db_incarnation')
    directory(c['evidence_root'])
    c['_binding'] = binding
    c['_root'] = str(root)
    return c


def selected(root):
    return load_attempt(Path(root).parent)


def checker_closure(root):
    root = Path(root)
    mf = digest_file(root / 'SHA256SUMS', CHECKER_MF, SMALL)
    for line in mf.decode().splitlines():
        h, name = line.split(maxsplit=1)
        p = root / name.lstrip('*')
        if not p.resolve(strict=True).is_relative_to(root):
            raise ValueError('checker_closure_escape')
        digest_file(p, h)
    digest_file(root / 'check.py', CHECKER_HASH)
    # These two markers belong to transport, not the frozen Git root.
    if (root / 'SOURCE_COMMIT').read_text().strip() != WHOLE_SOURCE or (root / 'CHECKER_IMPLEMENTATION_COMMIT').read_text().strip() != CHECKER_SOURCE:
        raise ValueError('transport_provenance')


def native_identity(pid):
    return dict(pid=pid, start=int(Path('/proc/' + str(pid) + '/stat').read_text().rsplit(')', 1)[1].split()[19]), boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip())


def group_members(pgid):
    """Bounded local /proc census, not an all-tree/escaped-descendant guarantee."""
    rows = []
    count = 0
    until = time.monotonic() + 0.2
    for p in Path('/proc').iterdir():
        count += 1
        if count > 8192 or time.monotonic() > until:
            raise OwnershipUnknown('census_budget')
        if not p.name.isdigit():
            continue
        try:
            s = (p / 'stat').read_text().rsplit(')', 1)[1].split()
        except FileNotFoundError:
            continue
        if int(s[2]) == pgid:
            rows.append(int(p.name))
            if len(rows) > 32:
                raise OwnershipUnknown('group_bound')
    return rows


class Owned:
    """Group leader is held WNOWAIT until final group signal; no PID whitelist."""
    def __init__(self, argv, out, seconds):
        self.out = open(out, 'xb', buffering=0)
        os.chmod(out, 0o600)
        try:
            self.p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=self.out, stderr=subprocess.STDOUT, start_new_session=True)
        except Exception:
            self.out.close()
            raise
        self.identity_error=None
        try:
            self.identity = native_identity(self.p.pid)
        except Exception as e:
            # Still retain the direct unreaped Popen child. Never discard its
            # ownership handle because an ancillary native receipt read failed.
            self.identity=dict(pid=self.p.pid,start=None,boot=None)
            self.identity_error=type(e).__name__
        self.end = time.monotonic() + (seconds if self.identity_error is None else 0)
        self.terminal = None
        self.kill_sent = False
        self.final_until = None

    def poll(self):
        if self.terminal is not None:
            if self.terminal['ownership']=='UNKNOWN':
                members=group_members(self.p.pid)
                self.terminal['group_remaining']=members
                if not members:self.terminal['ownership']='RECONCILED'
            return self.terminal
        done = os.waitid(os.P_PID, self.p.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        observed_late = time.monotonic() >= self.end
        expired = not done and observed_late
        if not done and not expired:
            return None
        if self.identity_error is None and native_identity(self.p.pid) != self.identity:
            raise OwnershipUnknown('owned_leader_changed')
        # P_PID/WNOWAIT reserves our original direct child, including when its
        # optional native receipt failed. Never signal after it is reaped.
        if os.getpgid(self.p.pid)!=self.p.pid:
            raise OwnershipUnknown('owned_private_group_changed')
        # Reserve the group leader until no further signals are needed. The worker
        # closure never daemonises; unrelated or escaped processes are NOT claimed.
        if not self.kill_sent:
            try:
                os.killpg(self.p.pid, signal.SIGKILL)
            except ProcessLookupError:
                if not done:
                    raise OwnershipUnknown('group_missing_live_leader')
            self.kill_sent = True
            self.final_until = time.monotonic() + 2
        done = os.waitid(os.P_PID, self.p.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if done:
            self.p.wait(timeout=0.1)
            self.out.close()
            # After reap never signal that numeric PID/group again.
            self.terminal = dict(returncode=self.p.returncode, expired=expired, observed_after_parent_deadline=observed_late, owned=self.identity, group_remaining=None, ownership='UNKNOWN', native_identity_error=self.identity_error)
            members = group_members(self.p.pid)
            self.terminal.update(group_remaining=members,ownership='UNKNOWN' if members else 'RECONCILED')
            return self.terminal
        if time.monotonic() > self.final_until:
            raise OwnershipUnknown('bounded_reap_unknown')
        return None


def row_contract(rows):
    if not isinstance(rows, dict) or type(rows.get('high_water')) is not int or not 0 <= rows['high_water'] <= 2**63-1 or rows.get('invalid_identities') != 0:
        raise ValueError('snapshot_shape')
    for key in ('candidates', 'prior', 'turns'):
        if not isinstance(rows.get(key), list):
            raise ValueError('rows_shape')
        seen = set()
        for r in rows[key]:
            if not isinstance(r, dict) or any(not isinstance(r.get(k), str) or not r[k].strip() for k in ('agent_id', 'trace_id')):
                raise ValueError('row_identity')
            v = r.get('queue_id' if key == 'candidates' else 'turn_id')
            if key == 'candidates':
                if not isinstance(v, str) or not v:
                    raise ValueError('queue_id')
            elif type(v) is not int or not 0 < v <= rows['high_water']:
                raise ValueError('turn_id')
            if v in seen:
                raise ValueError('duplicate_id')
            seen.add(v)


def validate_result(task, rc):
    """Only in the whole-bounded worker; no 64MiB parse in the R4 loop."""
    r, b = read_json(task['result'], CAP)
    if r.get('source_sha256') != CHECKER_HASH or r.get('stage') != task['stage']:
        raise ValueError('checker_stage_source')
    if r.get('state') == 'UNKNOWN':
        return dict(state='UNKNOWN', reason=r.get('reason_code'), receipt=b, checker_rc=rc)
    if rc != (10 if task['stage'] == 'pre' and r.get('state') == 'CANDIDATES' else 0):
        raise ValueError('checker_exit_state')
    if r.get('db') != task['db'] or r.get('sqlite_child', {}).get('state') != 'REAPED':
        raise ValueError('db_child')
    row_contract(r['snapshot'])
    rows = r['snapshot']
    if type(r.get('candidate_count')) is not int or r['candidate_count'] != len(rows['candidates']):
        raise ValueError('candidate_count')
    if r.get('raw_path') != task['result'] + '.sqlite.json':
        raise ValueError('raw_path')
    raw = digest_file(r['raw_path'], r['raw_sha256'], 32 * 1024 * 1024)
    if len(raw) != r['raw_bytes'] or json.loads(raw.decode()) != rows:
        raise ValueError('raw_snapshot')
    if r['state'] not in ('ZERO', 'CANDIDATES', 'NO_REPLAY_OBSERVED', 'REPLAY_INCIDENT'):
        raise ValueError('checker_state')
    if task['stage'] != 'replay' and r['state'] != ('CANDIDATES' if rows['candidates'] else 'ZERO'):
        raise ValueError('count_state')
    answer = dict(state=r['state'], receipt=b, checker_rc=rc, candidate_count=r['candidate_count'], high_water=rows['high_water'], start_epoch=r['start_epoch'], end_epoch=r['end_epoch'])
    if task['stage'] == 'replay':
        a, _ = read_json(task['anchor']['path'], CAP, task['anchor'])
        if a['stage'] != 'pre' or a['state'] != 'ZERO' or a['db'] != task['db'] or r.get('anchor_sha256') != task['anchor']['sha256'] or rows['high_water'] < a['snapshot']['high_water']:
            raise ValueError('anchor_replay_binding')
        pairs = {(x['agent_id'], x['trace_id']) for x in a['snapshot']['prior']}
        actual = [x for x in rows['turns'] if x['turn_id'] > a['snapshot']['high_water'] and (x['agent_id'], x['trace_id']) in pairs]
        if r.get('replays') != actual or r['state'] != ('REPLAY_INCIDENT' if actual else 'NO_REPLAY_OBSERVED'):
            raise ValueError('replay_rows_state')
        answer['replay_count'] = len(actual)
    return answer


def file_stamp(path):
    p=Path(path)
    x=p.stat(follow_symlinks=False)
    if p.resolve(strict=True)!=p or not stat.S_ISREG(x.st_mode) or x.st_uid!=os.getuid() or x.st_mode & 0o022:
        raise ValueError('anchor_name_type_owner')
    return dict(dev=x.st_dev,inode=x.st_ino,size=x.st_size,mtime_ns=x.st_mtime_ns,ctime_ns=x.st_ctime_ns)


def custody_worker(task_path):
    t,_=read_json(task_path)
    result=dict(state='UNKNOWN',stage='custody',task=t['context'])
    try:
        a,b,stamp=read_json_version(t['anchor']['path'],CAP,t['anchor'])
        if a.get('stage')!='pre' or a.get('state')!='ZERO' or a.get('source_sha256')!=CHECKER_HASH or a.get('db')!=t['db'] or a.get('candidate_count')!=0:
            raise ValueError('saved_PRE_source_DB_state')
        row_contract(a['snapshot'])
        if a['snapshot']['candidates'] or a.get('sqlite_child',{}).get('state')!='REAPED':
            raise ValueError('saved_PRE_snapshot_child')
        if a.get('raw_path')!=t['anchor']['path']+'.sqlite.json':
            raise ValueError('saved_PRE_raw_path')
        raw=digest_file(a['raw_path'],a['raw_sha256'],32*1024*1024)
        if len(raw)!=a['raw_bytes'] or json.loads(raw.decode())!=a['snapshot'] or file_stamp(t['anchor']['path'])!=stamp:
            raise ValueError('saved_PRE_raw_or_rebound')
        ds=Path(t['db']['path']).stat()
        if (ds.st_dev,ds.st_ino)!=(t['db']['dev'],t['db']['inode']):
            raise ValueError('saved_PRE_DB_changed')
        result.update(state='CUSTODY_OK',anchor=b,stamp=stamp)
    except Exception as e:
        result['reason']=type(e).__name__
    exclusive(t['summary'],result)


def qualify_natural(e,target):
    """Same-status attestation bound to manager and kernel on both sides."""
    try:
        a,b=e['status_before'],e['status_after']
        u,v=e['unit_before'],e['unit_after']
        n,m=e['native_before'],e['native_after']
        cg,dg=e['cgroup_before'],e['cgroup_after']
        if a!=b or u!=v or n!=m or cg!=dg or u['ActiveState']!='active':return None
        if a.get('release_id')!=target or a.get('draining') is not False or not re.fullmatch('[0-9a-f]{32}',a.get('instance','')):return None
        att=a.get('attestation')
        if not isinstance(att,dict) or set(att)!={'pid','start_ticks','boot_id','cgroup','invocation'}:return None
        if type(att['pid']) is not int or type(att['start_ticks']) is not int or att['pid']<=1 or att['start_ticks']<=0:return None
        if (att['pid'],att['start_ticks'],att['boot_id'],att['invocation'],att['cgroup'])!=(int(u['MainPID']),n['start'],n['boot'],u['InvocationID'],u['ControlGroup']):return None
        if n['pid']!=int(u['MainPID']) or cg!=u['ControlGroup'] or not cg.endswith('/clarp-runtime.service'):return None
        if not re.fullmatch('[0-9a-f]{32}',att['invocation']):return None
        return dict(runtime=target,healthy=True,native=dict(n,invocation=u['InvocationID']),route='joined_natural_status_attestation',join=e)
    except (KeyError,TypeError,ValueError):return None


def natural_worker(task_path):
    t,_=read_json(task_path)
    out=dict(state='UNKNOWN',stage='natural',task=t['context'])
    try:
        path=Path(__file__).resolve().parents[1]/'support/runtime_bridge931.py'
        digest_file(path,'6995a57d0eea07e8eb17e6056c47c02c94be7f5c94c66454d94345e1fb211c66')
        spec=importlib.util.spec_from_file_location('retained_runtime_bridge933',path)
        bridge=importlib.util.module_from_spec(spec);sys.modules[spec.name]=bridge;spec.loader.exec_module(bridge)
        client=bridge.RuntimeClient('/home/peter/.cache/clarp/runtime.sock',timeout=2)
        def unit():
            rc,raw=capture_command(['systemctl','--user','show','clarp-runtime.service','--property=MainPID,InvocationID,ControlGroup,ActiveState'],2,4096)
            if rc:raise ValueError('runtime_manager_read')
            return dict(line.split('=',1) for line in raw.decode().splitlines() if '=' in line)
        def cgroup(pid):
            text=Path('/proc/'+str(pid)+'/cgroup').read_text()
            if len(text)>4096:raise ValueError('runtime_cgroup_bound')
            lines=[x.split(':',2)[2] for x in text.splitlines() if x.startswith('0::')]
            if len(lines)!=1:raise ValueError('runtime_cgroup_shape')
            return lines[0]
        def fields(x):return {k:x.get(k) for k in ('release_id','instance','draining','attestation')}
        clock_before = dict(wall_before=time.time(), mono_before=time.monotonic())
        a=fields(client.status_within(2))
        u=unit();n=native_identity(int(u['MainPID']));cg=cgroup(n['pid'])
        b=fields(client.status_within(2))
        v=unit();m=native_identity(int(v['MainPID']));dg=cgroup(m['pid'])
        evidence=dict(status_before=a,status_after=b,unit_before=u,unit_after=v,native_before=n,native_after=m,cgroup_before=cg,cgroup_after=dg)
        evidence['healthy_clock'] = dict(clock_before, wall_after=time.time(), mono_after=time.monotonic())
        if qualify_natural(evidence,t['target']) is None:raise ValueError('natural_incarnation_join')
        out.update(state='NATURAL_JOINED',evidence=evidence)
    except Exception as e:
        out['reason']=type(e).__name__
        out['error']=dict(type=type(e).__name__,message=str(e),traceback=traceback.format_exc())
    exclusive(t['summary'],out)


def stage_worker(task_path):
    t, _ = read_json(task_path)
    result = dict(state='UNKNOWN', task=t['context'], stage=t['stage'],checker_process={'ownership':'NOT_STARTED'})
    nested=None
    try:
        checker_closure(t['checker_root'])
        argv = ['/usr/bin/bash', str(Path(t['checker_root']) / 'run-check.sh'), '--db', t['db']['path'], '--expected-dev', str(t['db']['dev']), '--expected-inode', str(t['db']['inode']), '--stage', t['stage'], '--out', t['result']]
        if t['stage'] == 'replay':
            a = t['anchor']
            argv += ['--anchor', a['path'], '--anchor-sha256', a['sha256'], '--anchor-dev', str(a['dev']), '--anchor-inode', str(a['inode']), '--anchor-size', str(a['size'])]
        # In the parent's private group: whole worker timeout owns the complete
        # nested command/parser envelope, including serialization and hashing.
        nested=Owned(argv,Path(t['result']).with_suffix('.command.log'),24)
        exclusive(Path(t['result']).with_suffix('.command.started.json'),dict(context=t['context'],owned=nested.identity,ownership='RUNNING_NOT_A_DRAIN'))
        terminal=None
        while terminal is None or terminal['ownership']!='RECONCILED':
            terminal=nested.poll()
            if time.monotonic()>nested.end+3:raise OwnershipUnknown('checker_group_not_reconciled')
            time.sleep(.02)
        result['checker_process']=terminal
        if terminal.get('native_identity_error') or terminal['expired']:raise OwnershipUnknown('checker_native_or_deadline')
        result.update(validate_result(t, terminal['returncode']))
    except Exception as e:
        result.update(state='UNKNOWN', reason=type(e).__name__)
        if nested is not None and result['checker_process']['ownership']=='NOT_STARTED':
            result['checker_process']=dict(ownership='UNKNOWN',owned=nested.identity,terminal=nested.terminal)
    exclusive(t['summary'], result)


def host_native(unit):
    n = native_identity(int(unit['MainPID']))
    n['invocation'] = unit['InvocationID']
    return n


def physical_worker(task_path):
    t, _ = read_json(task_path)
    result = dict(state='UNKNOWN', stage='physical', task=t['context'])
    try:
        n = t['host']['native']
        if native_identity(n['pid']) != {k: n[k] for k in ('pid', 'start', 'boot')}:
            raise ValueError('Host_native_changed')
        before_wall, before_mono = time.time(), time.monotonic()
        r = subprocess.run(['systemctl', '--user', 'show', 'clarp.service', '--property=MainPID,InvocationID,ExecMainStartTimestampMonotonic'], capture_output=True, text=True, timeout=10)
        after_mono, after_wall = time.monotonic(), time.time()
        if r.returncode or len(r.stdout) > SMALL:
            raise ValueError('unit_timestamp_unknown')
        unit = dict(x.split('=', 1) for x in r.stdout.splitlines() if '=' in x)
        if int(unit['MainPID']) != n['pid'] or unit['InvocationID'] != n['invocation']:
            raise ValueError('unit_incarnation')
        start_mono = int(unit['ExecMainStartTimestampMonotonic']) / 1000000
        # Unit process-start monotonic timestamp, not current-link or HTTP time.
        lo = before_wall - before_mono + start_mono
        hi = after_wall - after_mono + start_mono
        if abs((after_wall-before_wall)-(after_mono-before_mono)) > 0.1 or start_mono <= 0:
            raise ValueError('clock_offset_unknown')
        lo, hi = min(lo, hi)-0.002, max(lo, hi)+0.002
        j = subprocess.run(['journalctl', '--user', '-u', 'clarp.service', '--since', '@'+str(t['invocation_epoch']), '-n', '256', '--output=json', '--output-fields=__REALTIME_TIMESTAMP,_PID,_BOOT_ID,_SYSTEMD_INVOCATION_ID', '--no-pager'], capture_output=True, timeout=10)
        if j.returncode or len(j.stdout) > 1024*1024:
            raise ValueError('journal_metadata_bound')
        joined = []
        for line in j.stdout.splitlines():
            row = json.loads(line)
            if str(row.get('_PID')) == str(n['pid']) and row.get('_BOOT_ID', '').replace('-', '') == n['boot'].replace('-', '') and row.get('_SYSTEMD_INVOCATION_ID') == n['invocation']:
                joined.append(row)
        if not joined or native_identity(n['pid']) != {k: n[k] for k in ('pid', 'start', 'boot')}:
            raise ValueError('journal_native_join_missing')
        if lo < t['T_check'] or lo > t['host']['epoch'] or hi > time.time():
            raise ValueError('restart_bracket_invalid')
        raw_path = Path(t['summary']).with_suffix('.unit-journal.json')
        raw = dict(unit=unit, journal_metadata=joined, native=n, wall_mono_before=[before_wall,before_mono], wall_mono_after=[after_wall,after_mono])
        # Metadata only, no MESSAGE/payload/content fields; cap complete raw file.
        encoded=(json.dumps(raw, sort_keys=True)+'\n').encode()
        if len(encoded)>1024*1024:
            raise ValueError('joined_metadata_cap')
        fd=os.open(raw_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as f:
            f.write(encoded)
        result.update(state='JOINED_BRACKET', T_check=t['T_check'], physical_restart_epoch_bracket=[lo,hi], gap_seconds_bracket=[lo-t['T_check'],hi-t['T_check']], post_observation_delay_bracket=[t['host']['epoch']-hi,t['host']['epoch']-lo], raw_path=str(raw_path), raw_sha256=hashlib.sha256(encoded).hexdigest(), native=n, release_id=t['host']['release_id'], label='zero_at_T_check_NOT_zero_at_switch')
    except Exception as e:
        result['reason'] = type(e).__name__
    exclusive(t['summary'], result)


def union_worker(task_path):
    t,_=read_json(task_path)
    result=dict(state='UNKNOWN',stage='union',task=t['context'])
    try:
        summary=t['handover']
        root=Path(summary['helper_receipt_root'])
        expected=Path('/var/tmp/clarp-refactor-program/logs/releases/handover-resumes')/summary['runtime_invocation']
        if root!=expected or root.resolve(strict=True)!=root:
            raise ValueError('r3_receipt_root_binding')
        files=sorted(root.glob('*.json'))
        if len(files)>512:
            raise ValueError('receipt_inventory_cap')
        receipts=[];refs=[]
        for f in files:
            r,b=read_json(f)
            refs.append(b)
            if f.name!='pebble.json':
                receipts.append(r)
        q,_=read_json(t['quiescence']['path'])
        digest_file(t['quiescence']['path'],t['quiescence']['sha256'])
        if q.get('schema')!='queue933.quiescence/1' or not isinstance(q.get('continue_receipts'),list) or len(q['continue_receipts'])>512:
            raise ValueError('lead_quiescence_receipts_unbound')
        lead=[]
        for ref in q['continue_receipts']:
            raw=digest_file(ref['path'],ref['sha256'],SMALL)
            lead.append(json.loads(raw))
        union=continuation_union(receipts+lead)
        if len(receipts)!=summary['interrupted_current_boot'] or summary['resume_receipts'].get('unknown')!=0:
            raise ValueError('r3_interruption_count_unreconciled')
        result.update(state='UNION_UNKNOWN' if union['unknown'] else 'UNION_RECONCILED_METADATA',union=union,r3_refs=refs,scope='metadata_audit_only_no_send_no_state_preservation_claim')
    except Exception as e:
        result['reason']=type(e).__name__
    exclusive(t['summary'],result)


class Transition:
    def __init__(self, config):
        self.c = config
        self.root = directory(config['evidence_root'])
        self.context = {k: config[k] for k in ('sha', 'tag', 'job', 'native_session', 'goal')}
        self.context['capsule'] = config['_binding']
        self.states = {}
        self.children = {}
        self.anchor = None
        self.invoked = False
        self.notices = {}
        self.physical = None
        self.replay_due = False
        self.pre830_seen = False
        self.ownership_unknown = {}
        self.closing = False

    def start(self, stage, label=None):
        key = label or stage
        if key in self.states:
            return  # ONCE, including earlier UNKNOWN; never reuse/retry paths.
        if stage not in STAGES or stage == 'replay' and self.anchor is None:
            self.states[key] = dict(state='NOT_REACHED_NO_PRE_ANCHOR')
            return
        path = self.root / (key + '.task.json')
        result = self.root / (key + '.checker.json')
        summary = self.root / (key + '.summary.json')
        task = dict(context=self.context, db=self.c['db'], checker_root=self.c['checker_root'], stage=stage, result=str(result), summary=str(summary), anchor=self.anchor)
        try:
            exclusive(path, task)
            child = Owned(['/usr/bin/timeout', '--signal=TERM', '--kill-after=2s', '32s', '/usr/bin/python3', str(Path(__file__).resolve()), 'worker', str(path)], self.root / (key + '.worker.log'), 36)
            self.children[key] = (child, summary, stage)
            self.states[key] = dict(state='RUNNING', owned=child.identity)
        except Exception as e:
            self.states[key] = dict(state='UNKNOWN', reason=type(e).__name__)
            if stage != 'pre':
                self.incident(key)

    def poll(self):
        for key, (child, path, stage) in list(self.children.items()):
            try:
                terminal = child.poll()
            except Exception as e:
                self.ownership_unknown[key] = type(e).__name__
                self.states[key] = dict(state='OWNERSHIP_UNKNOWN', reason=type(e).__name__)
                continue
            if terminal is None:
                continue
            if terminal['ownership'] != 'RECONCILED':
                self.states[key] = dict(state='OWNERSHIP_UNKNOWN', process=terminal)
                self.ownership_unknown[key] = terminal
                continue
            self.ownership_unknown.pop(key,None)
            try:
                r, b = read_json(path)
                if r['task'] != self.context or r['stage'] != stage or terminal['returncode'] != 0 or terminal['expired'] or terminal.get('native_identity_error'):
                    raise ValueError('worker_summary_binding_exit')
                self.states[key] = dict(r, process=terminal, summary=b)
                if stage in STAGES and r.get('checker_process',{}).get('ownership') not in ('RECONCILED','NOT_STARTED'):
                    self.ownership_unknown[key]='nested_checker_not_reconciled'
                if stage == 'physical':
                    self.physical = r
                if stage=='natural' and r.get('state')=='NATURAL_JOINED':
                    self.healthy(dict(route='natural',join=r.get('evidence',{})))
                if stage == 'pre' and r['state'] == 'ZERO':
                    self.anchor = r['receipt']
            except Exception as e:
                self.states[key] = dict(state='UNKNOWN', reason=type(e).__name__, process=terminal)
                # Missing/terminated outer worker is not proof that its separately
                # owned frozen-timeout checker group is gone. Never assert a drain.
                if stage in STAGES:self.ownership_unknown[key]='nested_terminal_receipt_missing'
            del self.children[key]
            if stage not in ('pre','custody','natural') and self.states[key]['state'] not in ('ZERO', 'NO_REPLAY_OBSERVED', 'JOINED_BRACKET', 'UNION_RECONCILED_METADATA'):
                self.incident(key)
        self.poll_notices()

    def incident(self, key):
        """Fixed existing prompt API in a bounded child. Never blocks R4 or resends."""
        if key in self.notices:
            return
        self.notices[key] = dict(state='ATTEMPTED_UNKNOWN_NO_RESEND')
        if self.closing:
            self.notices[key]['state']='NOT_SENT_TERMINAL_CLEANUP'
            return
        try:
            intent = self.root / (key + '.notice.intent.json')
            # Complete IDs remain in the hashed checker receipt; do not inline
            # tens of thousands of identifiers in the notice/small envelope.
            exclusive(intent, dict(key=key, context=self.context, observation=self.states[key]))
            argv = ['/usr/bin/timeout', '--signal=TERM', '--kill-after=2s', '28s', '/usr/bin/python3', str(Path(__file__).resolve()), 'notice', str(intent)]
            self.notices[key]['child'] = Owned(argv, self.root / (key + '.notice.log'), 32)
        except Exception as e:
            self.notices[key]['reason'] = type(e).__name__

    def poll_notices(self):
        for key, n in self.notices.items():
            if 'child' not in n:
                continue
            try:
                t = n['child'].poll()
            except Exception as e:
                n['state'] = 'OWNERSHIP_UNKNOWN_NO_RESEND'
                self.ownership_unknown['notice:' + key] = type(e).__name__
                continue
            if t is None:
                continue
            if t['ownership']!='RECONCILED':
                self.ownership_unknown['notice:'+key]=t
                n['state']='OWNERSHIP_UNKNOWN_NO_RESEND'
                continue
            self.ownership_unknown.pop('notice:'+key,None)
            n['terminal'] = t
            del n['child']
            # The notice worker retains accepted trace only after real API rc0.
            try:
                receipt, _ = read_json(self.root / (key + '.notice.receipt.json'))
                if receipt['context'] != self.context or t['ownership'] != 'RECONCILED':
                    raise ValueError('notice_binding')
                n['state'] = receipt['state']
            except Exception:
                n['state'] = 'UNKNOWN_NO_RESEND'

    def start_auxiliary(self,key,mode,fields):
        if key in self.states:return
        path=self.root/(key+'.task.json');out=self.root/(key+'.summary.json')
        try:
            exclusive(path,dict(fields,context=self.context,summary=str(out)))
            child=Owned(['/usr/bin/timeout','--signal=TERM','--kill-after=2s','32s','/usr/bin/python3',str(Path(__file__).resolve()),mode,str(path)],self.root/(key+'.worker.log'),36)
            self.children[key]=(child,out,mode)
            self.states[key]=dict(state='RUNNING',owned=child.identity)
        except Exception as e:self.states[key]=dict(state='UNKNOWN',reason=type(e).__name__)

    def natural_observation(self,boundary):
        # Finite Host/830 boundaries plus the owner's one early native-admission
        # boundary. Same-key UNKNOWN never consumes replay or starts a retry loop.
        if boundary not in ('first_Host','Host_PASS','830_completion','native_admission'):raise ValueError('natural_boundary')
        self.start_auxiliary('natural_'+boundary,'natural',dict(target=self.c['predicted_runtime']))

    def revalidate_anchor(self):
        original=dict(self.anchor)
        self.start_auxiliary('pre_custody','custody',dict(anchor=original,db=self.c['db']))
        while 'pre_custody' in self.children:
            self.poll()
            if self.ownership_unknown:raise Refused('PRE_custody_process_UNKNOWN')
            time.sleep(.05)
        state=self.states['pre_custody']
        if state.get('state')!='CUSTODY_OK' or state.get('anchor')!=original or self.anchor!=original:
            raise Refused('saved_PRE_custody_UNKNOWN')
        # The large same-anchor hash/parse was whole-bounded above. Small final
        # name/stat check precedes native/age, never after Popen; no SQL rerun.
        if file_stamp(original['path'])!=state.get('stamp'):
            raise Refused('saved_PRE_changed_after_custody')

    def before_host(self, choice, native, command, shiplog):
        if self.invoked:raise Refused('already_invoked_no_second_effect')
        self.start('pre')
        while self.children:
            self.poll()
            if self.ownership_unknown:
                raise Refused('PRE_owned_process_UNKNOWN_no_invocation')
            time.sleep(0.05)
        r = self.states.get('pre', {})
        if r.get('state') != 'ZERO' or self.anchor is None or r.get('candidate_count') != 0:
            raise Refused('PRE_nonzero_or_UNKNOWN_no_invocation')
        self.revalidate_anchor()
        # Actual 906 decision follows PRE custody, immediately before actual Popen.
        read_json(self.c['_binding']['path'], expected=self.c['_binding'])
        if tuple(choice['identity']) != tuple(native()):
            raise Refused('baseline_native_changed_after_PRE')
        invoked = time.time()
        if not 0 <= invoked - choice['baseline']['end_epoch'] <= 120 or invoked >= self.c['deadline_epoch']:
            raise Refused('baseline_expired_after_PRE')
        # No I/O or second freshness gate after Popen has begun.
        child = subprocess.Popen(command, stdout=shiplog, stderr=subprocess.STDOUT, start_new_session=True)
        self.invoked = True
        self.states['invocation'] = dict(state='INVOKED_MAY_HAVE_EFFECTS', epoch=invoked, pid=child.pid)
        return child, invoked

    def observed_host(self, rid, native):
        if not self.invoked or not rid.startswith(self.c['sha'][:8] + '-') or not native:
            raise OwnershipUnknown('new_Host_observation_binding')
        if 'post' not in self.states:
            self.states['Host_observation'] = dict(release_id=rid, native=native, epoch=time.time())
            self.start('post')
            self.start_physical()

    def audit_continues(self, summary):
        key='union'
        if key in self.states:
            return
        path=self.root/'union.task.json';out=self.root/'union.summary.json'
        try:
            exclusive(path,dict(context=self.context,stage=key,summary=str(out),handover=summary,quiescence=self.c['receipts']['quiescence']))
            child=Owned(['/usr/bin/timeout','--signal=TERM','--kill-after=2s','32s','/usr/bin/python3',str(Path(__file__).resolve()),'union',str(path)],self.root/'union.worker.log',36)
            self.children[key]=(child,out,key)
            self.states[key]=dict(state='RUNNING',owned=child.identity)
        except Exception as e:
            self.states[key]=dict(state='UNKNOWN',reason=type(e).__name__)
            self.incident(key)

    def start_physical(self):
        key='physical'
        if key in self.states:
            return
        summary=self.root/'physical.summary.json'
        task=dict(context=self.context,host=self.states['Host_observation'],invocation_epoch=self.states['invocation']['epoch'],T_check=self.states['pre']['end_epoch'],summary=str(summary))
        try:
            path=self.root/'physical.task.json'
            exclusive(path,task)
            child=Owned(['/usr/bin/timeout','--signal=TERM','--kill-after=2s','28s','/usr/bin/python3',str(Path(__file__).resolve()),'physical',str(path)],self.root/'physical.worker.log',32)
            self.children[key]=(child,summary,key)
            self.states[key]=dict(state='RUNNING',owned=child.identity)
        except Exception as e:
            self.states[key]=dict(state='UNKNOWN',reason=type(e).__name__)
            self.physical=self.states[key]

    def before_runtime(self, natural_healthy):
        if self.ownership_unknown:
            self.states['pre830_timing']=dict(state='NOT_ATTEMPTED_OWNERSHIP_UNKNOWN',claim='existing_process_rule_not_DB_veto')
            return False
        self.pre830_seen = True
        self.start('pre830')
        self.states['pre830_timing'] = dict(natural_handover_already_healthy=True if natural_healthy is True else None, claim='before_explicit830_only')
        # Returning False means owned operation still running, NOT business veto.
        return self.states['pre830']['state'] != 'RUNNING' and not self.ownership_unknown

    def healthy(self, evidence):
        if evidence.get('route')=='natural':
            joined=qualify_natural(evidence.get('join',{}),self.c['predicted_runtime'])
        else:
            joined=None
            try:
                h,hr,u,n=evidence['health'],evidence['health_row'],evidence['unit'],evidence['native']
                q=h['probe']
                if (evidence.get('route')=='joined830_healthy' and h.get('runtime_on_installed') is True and q.get('runtime_available') is True and q.get('runtime_release_id')==self.c['predicted_runtime'] and hr['runtime']==self.c['predicted_runtime'] and hr['available'] is True and hr['unit_active']=='active' and hr['invocation']==u['InvocationID']==n['invocation'] and str(hr['runtime_pid'])==u['MainPID']==str(n['pid'])):
                    joined=dict(runtime=self.c['predicted_runtime'],healthy=True,native=n,route='joined830_healthy',join=evidence)
            except (KeyError,TypeError):pass
        if joined is None:
            self.states['health_join_last']=dict(state='UNKNOWN_UNJOINED_NOT_CONSUMED')
            return
        if 'replay' not in self.states:
            self.states['runtime_healthy'] = joined
            self.start('replay')

    def snapshot(self, reason):
        states = dict(self.states)
        for stage in STAGES:
            if stage not in states:
                states[stage] = dict(state='NOT_REACHED', reason=reason)
        return dict(context=self.context, states=states, physical_gap=self.physical or dict(state='UNKNOWN_NOT_CAPTURED'), pending_owned=list(self.children), ownership_unknown=self.ownership_unknown, notice_states={k: {a: b for a, b in v.items() if a != 'child'} for k, v in self.notices.items()}, coverage='new_turn_ID_above_PRE_HWM_on_PRE_ended_OR_settled_exact_agent_trace_only', residual='New/regressed/lost-transition/parked->queued receipts and old preselected work can evade snapshot. Risk-accepted detected transition; not a fence, atomic guarantee or all-replay detector.')

    def terminal_replay(self, reason):
        if self.invoked and self.anchor is not None and 'replay' not in self.states:
            # A terminal/control-abort or official rollback observation is not a
            # healthy-new-runtime result. Keep it separately labelled.
            self.start('replay', label='terminal_replay')
            until = time.monotonic() + 38
            while 'terminal_replay' in self.children and time.monotonic() < until and not self.ownership_unknown:
                self.poll()
                time.sleep(0.05)
            self.states['terminal_replay_scope'] = dict(reason=reason, not_successful_handover_proof=True)

    def finish(self, reason):
        """Bounded shutdown metadata, no service/effect/rollback authority here."""
        self.closing=True
        for child, _path, _stage in self.children.values():
            child.end = min(child.end, time.monotonic())
        for n in self.notices.values():
            if 'child' in n:
                n['child'].end = min(n['child'].end, time.monotonic())
        until = time.monotonic() + 3
        primary = None
        while (self.children or any('child' in n for n in self.notices.values())) and time.monotonic() < until:
            try:
                self.poll()
            except Exception as e:
                primary = type(e).__name__
                break
            time.sleep(0.05)
        record = self.snapshot(reason)
        record['cleanup_error'] = primary
        exclusive(self.root / 'terminal.json', record)
        return record


def continuation_union(receipts):
    """Metadata-only audit, NEVER a sender or replacement for approved r3."""
    accepted = {}
    pairs=set()
    unknown = []
    skipped = []
    for r in receipts:
        if r.get('state') == 'accepted':
            key = r.get('client_msg_id')
            rec=r.get('record',{})
            pair=(rec.get('agent'),rec.get('trace'))
            if not key or key in accepted or not isinstance(r.get('trace_id'),str) or not r['trace_id'] or not all(isinstance(v,str) and v for v in pair):
                unknown.append('missing_or_duplicate_client_key_or_identity')
            elif pair in pairs:
                unknown.append('same_interrupted_agent_trace_accepted_twice')
            else:
                pairs.add(pair)
                accepted[key] = r['trace_id']
        elif r.get('state') == 'skipped':
            skipped.append(r.get('reason'))
        else:
            unknown.append('receipt_unknown')
    return dict(accepted=len(accepted), skipped=len(skipped), unknown=unknown, sending_authority=False)


def capture_command(argv, seconds, cap):
    """Worker-local capped pipe. Outer Owned retains group ownership on faults."""
    p=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    data=bytearray();until=time.monotonic()+seconds
    try:
        with selectors.DefaultSelector() as sel:
            sel.register(p.stdout,selectors.EVENT_READ)
            while sel.get_map():
                left=until-time.monotonic()
                if left<=0:raise TimeoutError('command_deadline')
                for key,_ in sel.select(min(left,.1)):
                    b=os.read(key.fd,min(65536,cap+1-len(data)))
                    if not b:sel.unregister(key.fileobj)
                    else:
                        data.extend(b)
                        if len(data)>cap:raise ValueError('command_output_cap')
        rc=p.wait(timeout=max(.001,until-time.monotonic()))
        return rc,bytes(data)
    finally:
        if p.poll() is None:p.kill()
        p.wait(timeout=1)
        p.stdout.close()


def notice_worker(intent):
    v, _ = read_json(intent)
    result = dict(context=v['context'], state='UNKNOWN_NO_RESEND')
    exclusive(Path(intent).with_name(v['key']+'.notice.dispatch.json'),dict(context=v['context'],state='ATTEMPTED_NO_RESEND'))
    try:
        rc,output = capture_command(['clarp-admin', 'prompt', '--to', 'hobbles-18b0', '--from', 'rf-releasec-29c5', '--text', '933 TRANSITION INCIDENT: ' + json.dumps(v, sort_keys=True)],25,SMALL)
        a = json.loads(output)
        if rc == 0 and a.get('ok') is True and a.get('session') == 'hobbles-18b0' and a.get('trace_id'):
            result.update(state='ACCEPTED', trace_id=a['trace_id'])
    except Exception as e:
        result['error'] = type(e).__name__
    exclusive(Path(intent).with_name(v['key'] + '.notice.receipt.json'), result)


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] not in ('worker', 'notice', 'physical', 'union', 'custody', 'natural'):
        raise SystemExit('private fixed worker only')
    {'worker':stage_worker,'notice':notice_worker,'physical':physical_worker,'union':union_worker,'custody':custody_worker,'natural':natural_worker}[sys.argv[1]](sys.argv[2])

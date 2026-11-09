"""Rolling nightly controller. Existing replay.yml only; no product execution here.

Source proposal: install/seed/activate only after C review. No implicit recovery
of uncertain dispatches, no automatic heavy admission query, no branch checkout.
"""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import zipfile
import io
from zoneinfo import ZoneInfo

REPO = 'Maxteabag/clarp'
P = Path('/var/tmp/clarp-refactor-program')
ROOT = P / 'rf-harness/nightly-rolling'
REGISTRY = P / 'registers/differences.md'
A5 = 'a5fd6db9db6c12125e4d98389e22571f8f9bbb9a'
ROW = re.compile(r'^([0-9a-f]{12})  (?:UNEXPLAINED|INTENDED)\s+(.+?) / (.+?) / (.+?) / ([^:]+): (.*?) -> (.*?)$', re.M)


def sha(b):
    return hashlib.sha256(b).hexdigest()


def encode(o):
    return (json.dumps(o, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()


def read(p):
    return json.loads(p.read_bytes())


def persist(p, value, replace=False):
    """Exclusive immutable receipt first; pointer replacement only under outer lock."""
    data = encode(value)
    q = p.with_name(p.name + '.pending') if replace else p
    with q.open('xb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    if replace:
        os.replace(q, p)
    fd = os.open(p.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    if p.read_bytes() != data:
        raise RuntimeError('publication readback mismatch; HOLD')


def gh(args, payload=None, binary=False):
    # Never alter global account or persist a token. Child environment only.
    token = subprocess.check_output(['gh', 'auth', 'token', '-h', 'github.com', '-u', 'Maxteabag'], text=True).strip()
    env = dict(os.environ, GH_TOKEN=token)
    raw = subprocess.check_output(['gh', *args], input=payload, env=env)
    return raw if binary else json.loads(raw or b'null')


def api(path, payload=None, binary=False):
    if payload is not None and os.environ.get('CLARP_NIGHTLY_JOB_HANDLE'):
        subprocess.run(['clarp-agent-bg', 'rf-harnessc-4a4a', 'job-active',
                        os.environ['CLARP_NIGHTLY_JOB_HANDLE']], check=True, stdout=subprocess.DEVNULL)
    args = ['api', f'repos/{REPO}/{path}']
    if payload is not None:
        args += ['--method', 'POST', '--input', '-']
    return gh(args, encode(payload) if payload is not None else None, binary)


def pin(s):
    if type(s) is not str or not re.fullmatch('[0-9a-f]{40}', s):
        raise ValueError('exact SHA required')
    return s


def run_receipt(folder, lane, run, target):
    if (run['head_sha'] != target or run['event'] != 'workflow_dispatch'
            or run['path'] != '.github/workflows/replay.yml' or run['run_attempt'] != 1
            or run['status'] != 'completed' or not run['head_branch'].startswith('ci/nightly-')):
        raise ValueError('run identity/attempt/status mismatch')
    persist(folder / f'{lane}-run.json', run)
    artifacts = api(f"actions/runs/{run['id']}/artifacts")
    persist(folder / f'{lane}-artifacts.json', artifacts)
    items = artifacts['artifacts']
    if len(items) != 1 or items[0]['name'] != 'replay-report' or items[0]['expired']:
        raise ValueError('incomplete artifact inventory')
    z = api(f"actions/artifacts/{items[0]['id']}/zip", binary=True)
    if items[0].get('digest') != 'sha256:' + sha(z):
        raise ValueError('artifact digest mismatch/missing')
    (folder / f'{lane}.zip').write_bytes(z)
    with zipfile.ZipFile(io.BytesIO(z)) as archive:
        if archive.namelist() != ['report.txt'] or archive.getinfo('report.txt').file_size > 16_000_000:
            raise ValueError('unexpected report archive')
        report = archive.read('report.txt')
    (folder / f'{lane}-raw.txt').write_bytes(report)
    jobs = api(f"actions/runs/{run['id']}/jobs?per_page=100")
    persist(folder / f'{lane}-jobs.json', jobs)
    if jobs['total_count'] != 1 or len(jobs['jobs']) != 1:
        raise ValueError('unexpected jobs')
    steps = jobs['jobs'][0]['steps']
    bad = [s for s in steps if s['conclusion'] not in ('success', 'skipped')]
    if any(s['name'] != 'Replay' or s['conclusion'] != 'failure' for s in bad):
        raise ValueError('infrastructure/other step failure')
    text = report.decode()
    if re.search(r'^(RUN ERROR|BUILD ERROR|INTERNALERROR)', text, re.M):
        raise ValueError('replay error')
    return text, {'run': run['id'], 'url': run['html_url'], 'zip_sha256': sha(z),
                  'report_sha256': sha(report), 'conclusion': run['conclusion']}


def self_valid(text, target, conclusion):
    if conclusion != 'success' or f'target: {target}\n' not in text:
        raise ValueError('failed self-check/target')
    if len(re.findall(r'^unchanged .*: 0 differences  PASS$', text, re.M)) != 3:
        raise ValueError('unchanged/noise failure')
    declared = re.findall(r'^planted \((\d+)\): (.*)$', text, re.M)
    caught = re.findall(r'^planted ([\w-]+): \d+ differences, \d+ specific  CAUGHT$', text, re.M)
    if len(declared) != 1 or int(declared[0][0]) != 10 or caught != declared[0][1].split(','):
        raise ValueError('incomplete plant proof')
    if not re.search(r'^PROOF HOLDS$', text, re.M):
        raise ValueError('missing proof')


def disposition(text, run, base, head, registry):
    """Accept only exact run/pair/report + typed EACH-ID proof named in registry.

    A signed entry must contain run<ID>/attempt1, literal pins, report SHA,
    and registers/intended-ids/<name>.per-ID.json SHA256<digest>.
    No entry means unexplained. No ID-only or broad feature waiver.
    """
    if f'target: {base} -> {head}\n' not in text:
        raise ValueError('compare target mismatch')
    counts = re.findall(r'^differences: (\d+)  registered intended: (\d+)  unexplained: (\d+)$', text, re.M)
    rows = ROW.findall(text)
    if len(counts) != 1 or int(counts[0][0]) != len(rows) or len({r[0] for r in rows}) != len(rows):
        raise ValueError('incomplete/duplicate difference records')
    if not rows:
        if counts != [('0', '0', '0')]:
            raise ValueError('invalid empty counts')
        return {'intended': 0, 'unexplained': 0, 'proofs': []}
    expected = {r[0]: dict(zip(('id', 'scenario', 'channel', 'locator', 'kind', 'base', 'head'), r)) for r in rows}
    covered, proofs = {}, []
    for entry in registry.decode().splitlines():
        if not all(s in entry for s in (f'run{run}/attempt1', base, head, sha(text.encode()))):
            continue
        for rel, digest in re.findall(r'(registers/intended-ids/[\w.-]+\.per-ID\.json) SHA256([0-9a-f]{64})', entry):
            raw = (P / rel).read_bytes()
            if sha(raw) != digest:
                raise ValueError('registry proof bytes mismatch')
            proof = json.loads(raw)
            if (proof['base'], proof['head'], proof['run']) != (base, head, run):
                raise ValueError('proof pair/run mismatch')
            for row in proof['differences']:
                actual = {k: row[k] for k in ('id', 'scenario', 'channel', 'locator', 'kind', 'base', 'head')}
                if expected.get(row['id']) != actual:
                    raise ValueError('proof includes unrelated ID/value')
                covered[row['id']] = actual
            proofs.append({'path': rel, 'sha256': digest, 'entry_sha256': sha(entry.encode())})
    if covered != expected:
        raise ValueError(f'{len(expected.keys() - covered.keys())} unexplained; anchor unchanged')
    return {'intended': len(covered), 'unexplained': 0, 'proofs': proofs}


def advance(folder, old, current, receipt):
    receipt.update(previous=old, verified_main=pin(current))
    persist(folder / 'accepted.json', receipt)
    pointer = {'verified_main': current, 'receipt': str(folder / 'accepted.json'),
               'receipt_sha256': sha((folder / 'accepted.json').read_bytes())}
    persist(ROOT / 'state.json', pointer, replace=True)


def daily():
    day = dt.datetime.now(ZoneInfo('Europe/Oslo')).date().isoformat()
    if day <= '2026-11-09':
        raise ValueError('no Nov9 rerun')
    state = read(ROOT / 'state.json')
    receipt = Path(state['receipt']).read_bytes()
    if sha(receipt) != state['receipt_sha256'] or json.loads(receipt)['verified_main'] != state['verified_main']:
        raise ValueError('unverified anchor; no current-main fallback')
    base = pin(state['verified_main'])
    head = pin(api('git/ref/heads/main')['object']['sha'])
    folder = ROOT / day
    folder.mkdir()  # Existing/uncertain day is HOLD, never redispatch.
    registry = REGISTRY.read_bytes()
    (folder / 'differences.before.md').write_bytes(registry)
    persist(folder / 'frozen.json', {'base': base, 'head': head, 'executor': 'actions', 'registry_sha256': sha(registry)})
    lanes = {}
    for lane in ('self-check', 'compare'):
        branch = f'ci/nightly-{day}-{lane}-{head[:12]}'
        inputs = {'mode': lane, **({'ref': head} if lane == 'self-check' else {'base': base, 'head': head})}
        # Intent precedes both remote mutations. Any error stops; no retry today.
        persist(folder / f'{lane}-intent.json', {'branch': branch, 'sha': head, 'inputs': inputs})
        api('git/refs', {'ref': 'refs/heads/' + branch, 'sha': head})
        api('actions/workflows/replay.yml/dispatches', {'ref': branch, 'inputs': inputs})
        deadline = time.monotonic() + 1800
        while True:
            runs = api(f'actions/workflows/replay.yml/runs?branch={branch}&event=workflow_dispatch&per_page=100')
            if runs['total_count'] > 1:
                raise ValueError('multiple runs; HOLD')
            if runs['workflow_runs'] and runs['workflow_runs'][0]['status'] == 'completed':
                run = runs['workflow_runs'][0]
                break
            if time.monotonic() >= deadline:
                raise TimeoutError('uncertain/incomplete run; no redispatch')
            time.sleep(30)
        text, evidence = run_receipt(folder, lane, run, head)
        lanes[lane] = evidence
        if lane == 'self-check':
            # Record invalid self-check but still perform the ONE compare as requested.
            try:
                self_valid(text, head, evidence['conclusion'])
                self_ok = True
            except ValueError:
                self_ok = False
        else:
            registry = REGISTRY.read_bytes()
            (folder / 'differences.after.md').write_bytes(registry)
            classification = disposition(text, run['id'], base, head, registry)
            if evidence['conclusion'] not in ('success', 'failure') or (not classification['intended'] and evidence['conclusion'] != 'success'):
                raise ValueError('invalid compare conclusion')
    if not self_ok:
        raise ValueError('failed self-check; anchor unchanged')
    advance(folder, state, head, {'day': day, 'lanes': lanes, 'classification': classification,
                                'registry_sha256': sha(registry), 'executor': 'actions'})


def seed():
    """Bootstrap only from the exact retained Nov9 registration and GREEN proof."""
    evidence = P / 'rf-harness/evidence/nightly-20261109-actions'
    c = P / 'reviews/nightly-20261109-C'
    expected = {
        evidence / 'self-check-artifact/report.txt': '0e3010940e4ad2137ada621c815ba3b7af648793739ef1575f8fa0b2e0eb4ecb',
        evidence / 'self-check-replay-report.zip': '75224c46b208953edafff5e8e7cfe10a7144c55af845c6e67028a8c5dc0dfa5e',
        evidence / 'compare-artifact/report.txt': '15d47accbb66a81215ea586d309b0d8216fae99fda9141261508053c70e49901',
        evidence / 'compare-replay-report.zip': 'a345d06a90096827c1ef598fb57041f89504c1345b8e8d4d600b88453b361d97',
        c / 'per-ID-confirmation.json': '561937ec60485dd4eebeb2a283a6dad7086f08e47d240f53194ad67a897deace',
    }
    for path, digest in expected.items():
        if sha(path.read_bytes()) != digest:
            raise ValueError('seed retained bytes mismatch: ' + str(path))
    self_run = read(evidence / 'self-check-run.json')
    cmp_run = read(evidence / 'compare-run.json')
    if (self_run['id'], cmp_run['id'], self_run['head_sha'], cmp_run['head_sha']) != (42557140530, 42557146346, A5, A5):
        raise ValueError('seed run identity mismatch')
    self_valid((evidence / 'self-check-artifact/report.txt').read_text(), A5, self_run['conclusion'])
    registry = REGISTRY.read_bytes()
    entry = [line for line in registry.decode().splitlines() if 'HOTSEAT-931/A1 captain R1 exact nightly registration' in line]
    if len(entry) != 1 or sha(entry[0].encode()) != '4cbc2c72d6f377096b276327d09b12132cf5a8e4bf16c6eee6d521f5e21a5a88':
        raise ValueError('exact captain R1 missing/changed')
    result = disposition((evidence / 'compare-artifact/report.txt').read_text(), 42557146346,
                         A5, '780cd5022f87e512262add5340d0127590ce4dcd', registry)
    if result['intended'] != 186 or cmp_run['conclusion'] != 'failure':
        raise ValueError('seed disposition mismatch')
    if (ROOT / 'state.json').exists():
        raise ValueError('seed already exists; no overwrite')
    folder = ROOT / 'seed-2026-11-09'
    folder.mkdir()
    (folder / 'differences.md').write_bytes(registry)
    advance(folder, None, A5, {'day': '2026-11-09', 'kind': 'explicit-registered-seed',
            'raw_compare_exit': 1, 'classification': result,
            'evidence': {str(p): h for p, h in expected.items()},
            'self_run': self_run['html_url'], 'compare_run': cmp_run['html_url']})


def report():
    day = dt.datetime.now(ZoneInfo('Europe/Oslo')).date().isoformat()
    folder = ROOT / day
    frozen = read(folder / 'frozen.json') if (folder / 'frozen.json').exists() else {}
    accepted = read(folder / 'accepted.json') if (folder / 'accepted.json').exists() else None
    parts = [f"rf-harness NIGHTLY {day} " + ('VERIFIED' if accepted else 'HOLD; anchor unchanged unless publication requires reconciliation'),
             f"rolling main {frozen.get('base', 'UNKNOWN')} -> {frozen.get('head', 'UNKNOWN')}"]
    for lane in ('self-check', 'compare'):
        p = folder / f'{lane}-run.json'
        if p.exists():
            run = read(p)
            parts.append(f"{lane}: {run['conclusion']} {run['html_url']}")
        else:
            parts.append(lane + ': incomplete/unknown; no retry')
    if accepted:
        parts.append('intended ' + str(accepted['classification']['intended']) + '; unexplained 0')
    parts.append('Raw reports/Actions conclusions retained; TP-N3 separately not counted. Evidence: ' + str(folder))
    print('. '.join(parts))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['daily', 'seed', 'report'])
    args = parser.parse_args()
    if args.command == 'report':
        report()
        return
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (ROOT / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        seed() if args.command == 'seed' else daily()


if __name__ == '__main__':
    main()

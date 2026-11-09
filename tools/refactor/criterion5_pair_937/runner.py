"""937 matched experiment, SOURCE ONLY until exact workflow review and handoff.

No native worker/execnet observers. Raw pytest validity, actual health responses,
RSS/PSS and separate ordinary hygiene findings only. Never run on laptopstudio.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET


def write(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def terminate(proc):
    """Bounded ordinary owned-group cleanup; hygiene is separate from timing."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=3)
    try:
        os.killpg(proc.pid, 0)
    except ProcessLookupError:
        return {'group_survivors_observed': False}
    return {'group_survivors_observed': True, 'finding': 'separate hygiene finding; no native teardown certificate'}


def run(command, *, cwd, env, log, timeout, deadline):
    started = time.monotonic()
    with log.open('xb') as stream:
        proc = subprocess.Popen(command, cwd=cwd, env=env, stdout=stream,
                                stderr=subprocess.STDOUT, start_new_session=True)
        timed_out = False
        try:
            proc.wait(timeout=max(0.01, min(timeout, deadline - started)))
        except subprocess.TimeoutExpired:
            timed_out = True
        ended = time.monotonic()
        hygiene = terminate(proc)
    return {'argv': command, 'exit': proc.returncode, 'wall_s': ended - started,
            'timed_out': timed_out, 'hygiene': hygiene, 'log': str(log), 'log_sha256': digest(log)}


def suite(source, arm, cycle, out, python, plan, env, deadline):
    stem = out / f'suite-{cycle}-{arm}'
    junit, counts, log = [Path(str(stem) + suffix) for suffix in ('.xml', '.counts.json', '.log')]
    plugin = Path(__file__).parent
    env = {**env, 'PYTHONPATH': str(plugin)}
    command = [python, '-m', 'pytest', '-q', '-n', str(plan['workers']), '--max-worker-restart=0',
               '-p', 'counts', '--c937-counts', str(counts), '--junitxml', str(junit)]
    result = run(command, cwd=source, env=env, log=log, timeout=plan['suite_timeout_s'], deadline=deadline)
    errors = []
    text = log.read_text(errors='replace')
    if result['timed_out']:
        errors.append('pytest timeout')
    if re.search(r'(?im)INTERNALERROR|node down|crashed|replacing', text):
        errors.append('internal error or xdist node-down/crash/replacement line')
    try:
        root = ET.parse(junit).getroot()
        cases = list(root.iter('testcase'))
        declared = sum(int(s.attrib['tests']) for s in root.iter('testsuite'))
        collection = json.loads(counts.read_text())
        if (collection['collected'] != len(cases)
                or declared != len(cases) + collection['subtest_reports']):
            errors.append('junit collected/reported outcome count mismatch')
        failures = sorted(c.attrib.get('classname', '') + '::' + c.attrib.get('name', '')
                          for c in cases if c.find('failure') is not None or c.find('error') is not None)
        standing = sorted(plan['standing_failures'][arm])
        if failures != standing or result['exit'] != (1 if standing else 0):
            errors.append('exit/failing outcomes outside exact standing set')
        if collection['pytest_exit'] != result['exit']:
            errors.append('pytest exit receipt mismatch')
        reported = len(cases)
        result.update(outcomes=reported, subtest_reports=collection['subtest_reports'],
                      junit_declared_tests=declared, failures=failures, junit_sha256=digest(junit),
                      wall_ms_per_outcome=1000 * result['wall_s'] / reported if reported else None)
    except (OSError, ValueError, KeyError, ET.ParseError) as exc:
        errors.append('missing/invalid pytest evidence: ' + str(exc))
    summary = re.findall(r'(?m)^.*(?:passed|failed|skipped|error|xfailed).*\bin (\d+(?:\.\d+)?)s', text)
    if summary:
        result['pytest_s'] = float(summary[-1])
        result['pytest_ms_per_outcome'] = (1000 * result['pytest_s'] / result['outcomes']
                                          if result.get('outcomes') else None)
    else:
        errors.append('pytest summary elapsed missing')
    result.update(arm=arm, cycle=cycle, valid=not errors, invalid_reasons=errors)
    write(Path(str(stem) + '.json'), result)
    return result


def state_environment(root, source, port):
    env = {'PATH': os.environ['PATH'], 'LANG': 'C.UTF-8', 'TZ': 'UTC', 'PYTHONHASHSEED': '0',
           'PYTHONDONTWRITEBYTECODE': '1', 'HOME': str(root / 'home'), 'CLAUDE_PWA_ROOT': str(source),
           'CLAUDE_PWA_CONFIG': str(root / 'config.toml'), 'CLAUDE_PWA_DB': str(root / 'state.sqlite'),
           'CLAUDE_PWA_LOG_DIR': str(root / 'logs'), 'CLARP_TELEMETRY_DB': str(root / 'telemetry.sqlite'),
           'CLARP_CONFIG_DIR': str(root / 'config'), 'CLARP_CACHE_DIR': str(root / 'cache'),
           'CLARP_DATA_DIR': str(root / 'share'), 'CLARP_SHARE_DIR': str(root / 'share'),
           'XDG_CONFIG_HOME': str(root / 'xdg-config'), 'XDG_CACHE_HOME': str(root / 'xdg-cache'),
           'XDG_DATA_HOME': str(root / 'xdg-data'), 'XDG_STATE_HOME': str(root / 'xdg-state'),
           'CLARP_QA_PROVIDER_ROOT': str(root / 'provider'), 'CODEX_HOME': str(root / 'provider'),
           'CLAUDE_CONFIG_DIR': str(root / 'home/.claude'), 'TMPDIR': str(root / 'tmp'),
           'CLAUDE_PWA_AUDIO_DIR': str(root / 'audio'), 'CLAUDE_PWA_BIND': '127.0.0.1',
           'CLAUDE_PWA_PORT': str(port)}
    return env


def http_json(port, path):
    with urllib.request.urlopen('http://127.0.0.1:' + str(port) + path, timeout=0.5) as response:
        return json.loads(response.read(2 * 1024 * 1024))


def runtime_health(path):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        sock.connect(str(path))
        sock.sendall(b'{"version":1,"method":"status","params":{}}\n')
        raw = bytearray()
        while not raw.endswith(b'\n'):
            part = sock.recv(65536)
            if not part:
                break
            raw.extend(part)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError('runtime health response oversized')
        value = json.loads(raw)
        return value


def ready_process(source, role, env, python, out, plan, deadline, port, root):
    stream = (out / (role + '.log')).open('xb')
    started = time.monotonic()
    try:
        proc = subprocess.Popen([python, str(source / 'server' / (role + '.py'))], cwd=root, env=env,
                                stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
    except BaseException:
        stream.close()
        raise
    cap = min(deadline, started + plan[('runtime' if role == 'runtime' else 'host') + '_ready_timeout_s'])
    valid, error, health = False, None, None
    try:
        while time.monotonic() < cap and proc.poll() is None:
            try:
                health = (runtime_health(root / 'cache/runtime.sock') if role == 'runtime'
                          else http_json(port, plan['health_host']))
                valid = ((health.get('ok') is True and health.get('result', {}).get('protocol_version') == 1)
                         if role == 'runtime' else health.get('ready') is True)
            except (OSError, ValueError):
                valid = False
            if valid:
                break
            time.sleep(plan['poll_s'])
        if not valid:
            error = 'process exited or health readiness deadline failed'
        return proc, stream, {'valid': valid, 'startup_s': time.monotonic() - started,
                               'pid': proc.pid, 'error': error, 'health': health,
                               'process_start_mono': started, 'health_observed_mono': time.monotonic()}
    except BaseException:
        terminate(proc)
        stream.close()
        raise


def memory(pid):
    fields = {}
    for line in Path(f'/proc/{pid}/smaps_rollup').read_text().splitlines():
        if line.startswith(('Rss:', 'Pss:')):
            name, size, unit = line.split()
            if unit != 'kB':
                raise ValueError('unexpected process memory unit')
            fields[name[:-1]] = int(size) * 1024
    if 'Rss' not in fields:
        raise ValueError('process RSS unavailable')
    return fields


def native(source, arm, cycle, out, python, plan, seed, deadline):
    attempt = out / f'native-{cycle}-{arm}'
    attempt.mkdir()
    # Restore exactly one shared synthetic replay snapshot for every arm/cycle.
    active = out / 'state-active'
    if active.exists():
        shutil.rmtree(active)  # owned disposable fixture only, after preceding cleanup
    shutil.copytree(seed, active)
    (active / 'cache/runtime.sock').unlink(missing_ok=True)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = state_environment(active, source, port)
    children, streams, result = [], [], {'arm': arm, 'cycle': cycle, 'startup': {}, 'samples': []}
    try:
        for role in ('runtime', 'server'):
            proc, stream, observation = ready_process(source, role, env, python, attempt, plan,
                                                      deadline, port, active)
            children.append(proc); streams.append(stream)
            result['startup']['runtime' if role == 'runtime' else 'Host'] = observation
            if not observation['valid']:
                return result
        sample_start = time.monotonic()
        for tick in range(plan['plateau_s']):
            if time.monotonic() >= deadline:
                break
            time.sleep(max(0.0, min(sample_start + tick, deadline) - time.monotonic()))
            if time.monotonic() >= deadline:
                break
            for path in plan['memory_workload_gets']:
                http_json(port, path)
            result['samples'].append({'mono': time.monotonic(), 'Host': memory(children[1].pid),
                                      'runtime': memory(children[0].pid)})
        result['memory'] = {}
        for role in ('Host', 'runtime'):
            values = [s[role]['Rss'] for s in result['samples']]
            enough = len(values) == plan['plateau_s']
            first, last = statistics.median(values[:15]), statistics.median(values[-15:])
            plateau = enough and abs(last - first) <= first * plan['plateau_drift_fraction']
            result['memory'][role] = {'valid': plateau, 'RSS': statistics.median(values),
                                      'first15': first, 'last15': last}
            pss = [s[role]['Pss'] for s in result['samples'] if 'Pss' in s[role]]
            if len(pss) == len(values):
                result['memory'][role]['PSS'] = statistics.median(pss)
    except (OSError, ValueError, statistics.StatisticsError) as exc:
        result['invalid_reason'] = str(exc)
    finally:
        result['hygiene'] = [terminate(p) for p in reversed(children)]
        for stream in streams:
            stream.close()
        write(attempt / 'observations.json', result)
    return result


def compare(a, b, required, tolerance):
    if len(a) < required or len(b) < required:
        return {'verdict': 'UNKNOWN', 'reason': 'insufficient valid observations', 'A': a, 'B': b}
    ma, mb = statistics.median(a), statistics.median(b)
    return {'verdict': 'MET' if mb <= ma * (1 + tolerance) or min(a) <= mb <= max(a) else 'NOT MET',
            'A': {'n': len(a), 'median': ma, 'min': min(a), 'max': max(a)},
            'B': {'n': len(b), 'median': mb, 'min': min(b), 'max': max(b)},
            'gap': mb - ma, 'gap_percent': 100 * (mb / ma - 1) if ma else None}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', type=Path, required=True)
    args = parser.parse_args()
    if socket.gethostname() == 'laptopstudio' or os.environ.get('CI') != 'true':
        raise SystemExit('admitted contained CI/buildbox only; never laptopstudio')
    work = args.workspace.resolve()
    plan = json.loads((Path(__file__).parent / 'plan.json').read_text())
    out = work / 'measurement-results'
    out.mkdir()  # fresh only; no automatic rerun/reuse
    deadline = time.monotonic() + plan['runner_timeout_s']
    python = str(work / '.venv/bin/python')
    arms = {arm: work / 'measurement-arms' / arm for arm in ('A', 'B')}
    env = {k: v for k, v in os.environ.items() if k in ('HOME', 'PATH', 'LANG', 'CI', 'VIRTUAL_ENV', 'UV_OFFLINE', 'NPM_CONFIG_OFFLINE')}
    env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONHASHSEED='0', TZ='UTC')
    write(out / 'environment.json', {'pins': {arm: plan[arm] for arm in arms}, 'python': sys.version,
                                    'affinity': sorted(os.sched_getaffinity(0)), 'uname': list(os.uname()),
                                    'plan_sha256': digest(Path(__file__).parent / 'plan.json')})
    runs, startups = [], []
    try:
        for cycle in range(plan['suite_pairs']):
            for arm in ('A', 'B'):
                if time.monotonic() >= deadline:
                    raise TimeoutError('inclusive runner deadline')
                runs.append(suite(arms[arm], arm, cycle, out, python, plan, env, deadline))
        seed = out / 'seed'
        seeded = run([python, str(Path(__file__).parent / 'seed.py'), '--harness', str(work),
                      '--source', str(arms['A']), '--output', str(seed)], cwd=work, env=env,
                     log=out / 'seed.log', timeout=plan['seed_timeout_s'], deadline=deadline)
        write(out / 'seed-result.json', seeded)
        if seeded['exit'] != 0 or seeded['timed_out']:
            raise ValueError('synthetic replay seed invalid')
        for cycle in range(plan['startup_pairs']):
            for arm in ('A', 'B'):
                if time.monotonic() >= deadline:
                    raise TimeoutError('inclusive runner deadline')
                startups.append(native(arms[arm], arm, cycle, out, python, plan, seed, deadline))
    except (OSError, ValueError, TimeoutError, subprocess.SubprocessError) as exc:
        write(out / 'invalid-campaign.json', {'reason': str(exc), 'no_retry': True})
    finally:
        result = {'suite_runs': runs, 'native_runs': startups, 'targets': {},
                  'hotspot_coverage': 'INFORMATIONAL_NOT_COLLECTED', 'final_main_rerun_outstanding': True}
        for metric in ('pytest_s', 'wall_s', 'pytest_ms_per_outcome'):
            samples = {arm: [r[metric] for r in runs if r['arm'] == arm and r['valid']] for arm in arms}
            result['targets']['suite_' + metric] = compare(samples['A'], samples['B'], plan['suite_pairs'], plan['median_tolerance_fraction'])
        for role in ('Host', 'runtime'):
            samples = {arm: [r['startup'][role]['startup_s'] for r in startups if r['arm'] == arm
                             and r.get('startup', {}).get(role, {}).get('valid')] for arm in arms}
            result['targets'][role + '_startup'] = compare(samples['A'], samples['B'], plan['startup_pairs'], plan['median_tolerance_fraction'])
            samples = {arm: [r['memory'][role]['RSS'] for r in startups if r['arm'] == arm
                             and r.get('memory', {}).get(role, {}).get('valid')] for arm in arms}
            result['targets'][role + '_RSS'] = compare(samples['A'], samples['B'], plan['startup_pairs'], plan['median_tolerance_fraction'])
        write(out / 'report.json', result)
        hashes = {p.relative_to(out).as_posix(): digest(p) for p in out.rglob('*') if p.is_file()}
        write(out / 'SHA256.json', hashes)
    verdicts = {r['verdict'] for r in result['targets'].values()}
    return 2 if 'UNKNOWN' in verdicts else (1 if 'NOT MET' in verdicts else 0)


if __name__ == '__main__':
    raise SystemExit(main())

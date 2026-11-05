"""Exercise CI dependency setup without root, apt, downloads or system changes."""
from __future__ import annotations

import hashlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tarfile

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / '.github/scripts/install-test-ffmpeg.sh'


def _run(tmp_path, *, installed=False, apt_success=1, timeout_attempts=0,
         bad_hash=False, download_fails=False, baseline=False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    bindir = tmp_path / 'commands'
    bindir.mkdir()
    publish = tmp_path / 'published'
    publish.mkdir()
    state = tmp_path / 'state.json'
    events = tmp_path / 'events.jsonl'
    archive = tmp_path / 'asset.tar.xz'
    # Private test copy: real checksum/extraction/install, synthetic tiny payload.
    text = SCRIPT.read_text()
    bundle = next(line.split('=', 1)[1] for line in text.splitlines() if line.startswith('bundle='))
    binary = b'#!/usr/bin/env bash\necho ffmpeg-fixture-version\n'
    with tarfile.open(archive, 'w:xz') as tar:
        for name in ('ffmpeg', 'ffprobe'):
            info = tarfile.TarInfo(f'{bundle}/bin/{name}')
            info.size = len(binary)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(binary))
    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    original = next(line.split('=', 1)[1] for line in text.splitlines() if line.startswith('digest='))
    text = text.replace(original, '0' * 64 if bad_hash else actual)
    text = text.replace('/usr/bin/', str(publish) + '/')
    copy = tmp_path / 'setup.sh'
    copy.write_text(text)
    mock = f'#!{sys.executable}\n' + '''
import json,os,pathlib,shutil,subprocess,sys
name=pathlib.Path(sys.argv[0]).name
args=sys.argv[1:]
state=pathlib.Path(os.environ['PROBE_STATE'])
d=json.loads(state.read_text()) if state.exists() else {'attempt':0,'timeouts':0}
with open(os.environ['PROBE_EVENTS'],'a') as f:f.write(json.dumps([name,args])+'\\n')
if name=='sudo':sys.exit(subprocess.call(args))
if name=='sleep' or name=='dpkg':sys.exit(0)
if name=='timeout':
    if '120s' in args:
        d['timeouts']+=1;state.write_text(json.dumps(d))
        if d['timeouts']<=int(os.environ['PROBE_TIMEOUTS']):sys.exit(124)
    sys.exit(subprocess.call(['/usr/bin/timeout',*args]))
if name=='apt-get':
    if args[-1]=='update':
        d['attempt']+=1;state.write_text(json.dumps(d))
        sys.exit(0 if d['attempt']>=int(os.environ['PROBE_APT_SUCCESS']) else 100)
    p=pathlib.Path(os.environ['PROBE_PUBLISH'])/'ffmpeg'
    p.write_text('#!/usr/bin/env bash\\necho apt-fixture-version\\n');p.chmod(0o755)
    sys.exit(0)
if name=='curl':
    if os.environ['PROBE_DOWNLOAD_FAILS']=='1':sys.exit(22)
    shutil.copyfile(os.environ['PROBE_ARCHIVE'],args[args.index('--output')+1]);sys.exit(0)
raise SystemExit('unknown mock '+name)
'''
    for name in ('sudo', 'timeout', 'apt-get', 'dpkg', 'curl', 'sleep'):
        p = bindir / name
        p.write_text(mock)
        p.chmod(0o755)
    for name in ('bash', 'mktemp', 'rm', 'sha256sum', 'tar', 'install', 'xz'):
        target = shutil.which(name)
        assert target is not None, name
        (bindir / name).symlink_to(target)
    if installed:
        p = publish / 'ffmpeg'
        p.write_bytes(binary)
        p.chmod(0o755)
    env = dict(os.environ, PATH=f'{publish}:{bindir}', TMPDIR=str(tmp_path),
               PROBE_STATE=str(state), PROBE_EVENTS=str(events),
               PROBE_TIMEOUTS=str(timeout_attempts), PROBE_APT_SUCCESS=str(apt_success),
               PROBE_PUBLISH=str(publish), PROBE_ARCHIVE=str(archive),
               PROBE_DOWNLOAD_FAILS=str(int(download_fails)))
    argv = [str(bindir / 'bash'), '-c',
            'sudo apt-get update && sudo apt-get install -y --no-install-recommends ffmpeg'] if baseline else [str(bindir / 'bash'), str(copy)]
    result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=15)
    calls = [json.loads(line) for line in events.read_text().splitlines()] if events.exists() else []
    return result, calls, publish


def test_existing_ffmpeg_skips_all_privilege_and_network_commands(tmp_path):
    result, calls, _ = _run(tmp_path, installed=True)
    assert result.returncode == 0, result.stderr
    assert calls == []


@pytest.mark.parametrize('timeout_attempts', [0, 2])
def test_apt_success_or_timeout_retry_uses_one_bounded_attempt(tmp_path, timeout_attempts):
    result, calls, publish = _run(tmp_path, timeout_attempts=timeout_attempts)
    assert result.returncode == 0, result.stderr
    attempts = [args for name, args in calls if name == 'timeout']
    assert len(attempts) == timeout_attempts + 1
    assert all(args[:3] == ['--signal=TERM', '--kill-after=15s', '120s'] for args in attempts)
    assert not any(name == 'curl' for name, _ in calls)
    assert (publish / 'ffmpeg').exists()


def test_exhausted_apt_falls_back_to_checked_archive_visible_to_sandbox(tmp_path):
    old, _, _ = _run(tmp_path / 'old', apt_success=99, baseline=True)
    assert old.returncode != 0  # The former workflow has no mirror-independent recovery.
    result, calls, publish = _run(tmp_path / 'new', apt_success=99)
    assert result.returncode == 0, result.stderr
    assert sum(name == 'apt-get' for name, _ in calls) == 3
    timeout = [args for name, args in calls if name == 'timeout']
    assert [args[2] for args in timeout] == ['120s', '120s', '120s', '180s']
    urls = [args[-1] for name, args in calls if name == 'curl']
    assert len(urls) == 1 and '/releases/download/autobuild-' in urls[0]
    assert (publish / 'ffmpeg').exists() and (publish / 'ffprobe').exists()
    assert 'Pinned ffmpeg and ffprobe installed and verified' in result.stdout


@pytest.mark.parametrize('bad_hash,download_fails', [(True, False), (False, True)])
def test_untrusted_or_unavailable_download_never_publishes_a_binary(tmp_path, bad_hash, download_fails):
    result, calls, publish = _run(tmp_path, apt_success=99, bad_hash=bad_hash,
                                  download_fails=download_fails)
    assert result.returncode != 0
    assert not (publish / 'ffmpeg').exists() and not (publish / 'ffprobe').exists()
    assert not any(name == 'sudo' and args[:1] == ['install'] for name, args in calls)

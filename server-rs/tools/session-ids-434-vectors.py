#!/usr/bin/env python3
"""Compare native session names, transcript containment and complete rollout names with R70."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch as tool_scratch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'server-rs/crates/clarp-backend/tests/fixtures/session-ids-434-vectors.json'


def record(work):
    spec = importlib.util.spec_from_file_location('replay_host', ROOT / 'tests/replay/host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (work / 'scenario-config.toml').touch()
    host.isolate(work)
    host.loopback_only()
    sys.path.insert(0, str(ROOT / 'server'))
    from lib import codex_transcript, muse_transcript, transcript_paths as paths

    ids = [None, False, True, 0, 12, [], {}, '', 'A', 'z', '0', '9', '-', '.', '_', '..',
           'a.b-c_9', '_first', '-first', '.first', 'has space', ' left', 'right ', '/a', 'a/b',
           'a' + chr(92) + 'b', 'a' + chr(0), 'a\nb', 'a\n', chr(0xe9), 'a' + chr(0xdf),
           'a' * 7, 'a' * 8, 'a' * 63, 'a' * 64, 'a' * 65, 'a' * 127, 'a' * 128, 'a' * 129,
           'A9B0-Fef', '0123456g', '-123abcd', '0-------', 'abcdef.0', 'abcdef_0']
    for char in range(32, 127):
        ids.extend([chr(char), 'z' + chr(char)])
    names = []
    for value in ids:
        checked = paths.checked_id('claude', value)
        common = paths.session_id_ok(value)
        assert checked == (value if common else None)
        muse = common and muse_transcript._SESSION_ID.fullmatch(value) is not None
        names.append([value, common, muse])

    store = work / 'store'
    store.mkdir()
    (store / 'file').touch()
    sibling = work / 'store-sibling'
    sibling.mkdir()
    (sibling / 'file').touch()
    for name, target in [('in-link', 'file'), ('out-link', '../store-sibling/file'),
                         ('twice', 'in-link'), ('missing-link', 'absent'), ('loop', 'loop')]:
        (store / name).symlink_to(target)
    alias = work / 'alias'
    alias.symlink_to(store)
    locations = []
    for path in ['file', 'in-link', 'out-link', 'twice', 'loop', 'absent', 'missing-link', '.']:
        candidate = alias / path
        inside = paths.inside('claude', alias, candidate)
        locations.append([path, inside is not None and candidate.is_file()])

    # Each directory contains exactly one candidate, so no result can be due to a second file.
    ts = '2026-11-19T10-15-37'
    when = [ts, ts.replace('2026', '026'), ts.replace('11', '1'), ts.replace('19', '9'),
            ts.replace('T10', 'T0'), ts.replace('15', '5'), ts.replace('37', '7'),
            ts.replace('T', 't'), ts.replace('-', ':', 1), ts.replace('15-', '15:'),
            ts.replace('19', 'XX'), ts.replace('2026', chr(0x0662) * 4), 'copy']
    files = [f'rollout-{stamp}-a1.jsonl' for stamp in when]
    files += [f'{ts}-a1.jsonl', f'rollout-{ts}-ba1.jsonl', f'prefix-rollout-{ts}-a1.jsonl',
              f'rollout-{ts}-a1.jsonl.bak', f'rollout-{ts}-a1_jsonl', 'a1.jsonl']
    rollouts = []
    for i, filename in enumerate(files):
        directory = work / str(i)
        directory.mkdir()
        (directory / filename).touch()
        rollouts.append([filename, codex_transcript.find_latest_jsonl('a1', directory) is not None])
    return dict(names=names, locations=locations, rollouts=rollouts)


if __name__ == '__main__':
    if '--isolated' in sys.argv:
        with tool_scratch.directory() as tmp, contextlib.redirect_stderr(io.StringIO()):
            print(json.dumps(record(Path(tmp)), ensure_ascii=True))
    else:
        result = subprocess.check_output([sys.executable, '-I', __file__, '--isolated'],
                                         env=tool_scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}), text=True)
        rows = json.loads(result.splitlines()[-1])
        groups = [' "' + key + '": [\n' + ',\n'.join('  ' + json.dumps(row, ensure_ascii=True)
                  for row in data) + '\n ]' for key, data in rows.items()]
        OUT.write_text('{\n' + ',\n'.join(groups) + '\n}\n')

#!/usr/bin/env python3
"""The kept and discarded portrait rows selected by Python's published_by (PR 510)."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch as tool_scratch

REPO = pathlib.Path(__file__).resolve().parents[2]
OUT = REPO / 'server-rs/crates/clarp-kernel/tests/fixtures/portrait-media-vectors.json'


@tool_scratch.case('clarp-portrait-media-vectors-')
def run(root):
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests/replay/host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import agents, db, media_store, portrait_generation

    db.reset_for_tests(root / 'gallery.sqlite')
    names = iter(('oak-id', 'elm-id'))
    agents._new_agent_id = lambda: next(names)
    for session in ('oak', 'elm'):
        agents.create_agent(persona=session.capitalize(), voice_id='v', cwd=str(root),
                            session=session, backend='claude')
    prefix = portrait_generation.CREATED_BY_PREFIX
    now = 0
    db.now_ms = lambda: now
    png = bytes.fromhex('89504e470d0a1a0a0000000d4948445200000001000000010806000000')
    seeded = []

    for label, session, created, discarded, producer, mime in (
        ('early', 'oak', 9, None, prefix + 'openai', 'image/png'),
        ('before_run', 'oak', 9, 12, prefix + 'openai', 'image/png'),
        ('born_at_start', 'oak', 10, None, prefix + 'openai', 'image/png'),
        ('born_at_end', 'oak', 20, None, prefix + 'openai', 'image/png'),
        ('late', 'oak', 21, None, prefix + 'openai', 'image/png'),
        ('tombstone_at_start', 'oak', 10, 10, prefix + 'openai', 'image/png'),
        ('tombstone_after_end', 'oak', 11, 30, prefix + 'second', 'image/png'),
        ('tombstone_before_start', 'oak', 18, 9, prefix + 'openai', 'image/png'),
        ('same_a', 'oak', 14, None, prefix + 'openai', 'image/png'),
        ('same_b', 'oak', 14, None, prefix + 'openai', 'image/png'),
        ('other_producer', 'oak', 14, None, 'agent', 'image/png'),
        ('prefix_in_middle', 'oak', 14, None, 'other:' + prefix + 'openai', 'image/png'),
        ('other_chat', 'elm', 14, None, prefix + 'openai', 'image/png'),
        ('not_an_image', 'oak', 14, None, prefix + 'openai', 'text/plain'),
    ):
        now = created
        media_store.secrets = SimpleNamespace(token_urlsafe=lambda _: label)
        row = media_store.publish(session=session, source_name=label + ('.png' if mime == 'image/png' else '.txt'),
                                  blob=png + label.encode() if mime == 'image/png' else b'not an image',
                                  content_type=mime, created_by=producer, media_dir=root / 'media')
        id = row['asset_id']
        if discarded is not None:
            now = discarded
            media_store.delete(id, deleted_by='')
        seeded.append(dict(id=id, session=session, created_by=producer, mime=mime,
                           created_at=created, deleted_at=discarded))
    queries = []
    for session, producer, since, until in (
        ('oak', prefix, 10, 20), ('oak', prefix, 11, 19), ('oak', prefix, 10, 10),
        ('oak', prefix, 20, 20), ('oak', prefix, 21, 22), ('oak', prefix, 21, 20),
        ('oak', prefix + 'second', 10, 20), ('oak', prefix, 22, 100),
        ('elm', prefix, 10, 20), ('', prefix, 10, 20), ('absent', prefix, 10, 20),
        ('oak', 'agent', 10, 20), ('oak', '', 10, 20),
    ):
        kept, discarded = media_store.published_by(session, created_by_prefix=producer, since=since, until=until)
        queries.append(dict(session=session, producer=producer, since=since, until=until,
                            kept=[row['asset_id'] for row in kept],
                            discarded=[row['asset_id'] for row in discarded]))
    return dict(rows=seeded, queries=queries)


def main():
    if sys.argv[1:] == ['--run']:
        print(json.dumps(run()))
        return
    proc = subprocess.run(
        [sys.executable, '-I', __file__, '--run'],
        env=tool_scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}),
        text=True, check=True, capture_output=True)
    data = json.loads(proc.stdout.splitlines()[-1])
    OUT.write_text(json.dumps(data, ensure_ascii=True, indent=2) + '\n', encoding='ascii')


if __name__ == '__main__':
    main()

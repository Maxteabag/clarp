#!/usr/bin/env python3
"""Hold gallery state, order and cursors to the Python Host's PR 494, using its own store."""
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
OUT = REPO / 'server-rs/crates/clarp-kernel/tests/fixtures/gallery-vectors.json'


@tool_scratch.case('clarp-gallery-vectors-')
def run(root):
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests/replay/host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import agents, db, media_store as store

    db.reset_for_tests(root / 'gallery.sqlite')
    names = iter(('oak-id', 'elm-id'))
    agents._new_agent_id = lambda: next(names)
    for session in ('oak', 'elm'):
        agents.create_agent(persona=session.capitalize(), voice_id='v', cwd=str(root),
                            session=session, backend='claude')
    png = bytes.fromhex('89504e470d0a1a0a0000000d4948445200000001000000010806000000')
    now = 100
    db.now_ms = lambda: now
    steps = []

    def record(op, ask, thunk):
        try:
            answer = thunk()
        except store.Removed as error:
            answer = dict(error=str(error), deleted_at=error.row['deleted_at'])
        except store.MediaError as error:
            answer = dict(error=str(error))
        steps.append(dict(op=op, ask=ask, at=now, answer=answer))

    def seed(session, id, image=True):
        store.secrets = SimpleNamespace(token_urlsafe=lambda _: id)
        mime = 'image/png' if image else 'text/plain'
        row = store.publish(session=session, blob=png if image else b'gallery note',
                            source_name='image.png' if image else 'note.txt',
                            content_type=mime, media_dir=root / 'media')
        steps.append(dict(op='seed', ask=dict(id=row['asset_id'], session=session, mime=mime),
                          at=now, answer=public(row)))

    def public(row):
        return {key: row[key] for key in ('asset_id', 'favorite', 'favorite_at', 'used_as')}

    def favorite(id, value):
        def ask():
            row, _, changed = store.set_favorite(id, value)
            return dict(row=public(row), changed=changed is not None)
        record('favorite', dict(id=id, value=value), ask)

    def delete(id, by='operator'):
        def ask():
            answer, row, changed = store.delete(id, deleted_by=by)
            return dict(at=answer['deleted_at'], by=row['deleted_by'], changed=changed)
        record('delete', dict(id=id, by=by), ask)

    def page(session, limit, before='', favorites=False):
        def ask():
            result = store.list_page(session, limit=limit, before=before, favorites=favorites)
            return dict(result, assets=[public(row) for row in result['assets']])
        record('page', dict(session=session, limit=limit, before=before, favorites=favorites), ask)

    def deleted(session, since, limit):
        record('deleted', dict(session=session, since=since, limit=limit),
               lambda: store.deleted_since(session=session, since=since, limit=limit))

    for id in ('a', 'b', 'c'):
        seed('oak', id)
    seed('oak', 'text', image=False)
    seed('elm', 'd')
    page('oak', 1)
    page('oak', 1, 'c100.asset_c')
    page('oak', 2, 'c100.asset_b')
    page('oak', 2, 'f100.asset_c')
    for before in ('c-1.asset_c', 'c100.', 'c100.bad/id', 'c100.' + 'a' * 181):
        page('oak', 2, before)
    page('elm', 10)
    page('absent', 10)
    now = 110
    favorite('missing', True)
    favorite('asset_text', True)
    favorite('asset_a', False)
    favorite('asset_b', True)
    favorite('asset_c', True)
    now = 111
    favorite('asset_c', True)
    page('oak', 1, favorites=True)
    page('oak', 1, 'f110.asset_c', favorites=True)
    favorite('asset_c', False)
    favorite('asset_c', False)
    favorite('asset_c', True)
    page('oak', 3, favorites=True)
    now = 120
    delete('asset_b')
    delete('asset_d')
    now = 121
    delete('asset_c')
    delete('asset_b', 'other')
    delete('missing')
    delete('asset_text')
    favorite('asset_b', True)
    page('oak', 5)
    page('oak', 5, favorites=True)
    deleted('', 0, 1)
    deleted('', 120, 1)
    deleted('', 121, 1)
    deleted('oak', 0, 1)
    deleted('elm', 0, 1)
    deleted('absent', 0, 1)
    now = 122
    seed('oak', 'again')
    page('oak', 1)
    deleted('', 121, 1)
    return steps


def main():
    if sys.argv[1:] == ['--run']:
        print(json.dumps(run()))
        return
    completed = subprocess.run(
        [sys.executable, '-I', __file__, '--run'],
        env=tool_scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}),
        text=True, check=True, capture_output=True)
    steps = json.loads(completed.stdout.splitlines()[-1])
    rows = ',\n'.join(' ' + json.dumps(step, ensure_ascii=True) for step in steps)
    OUT.write_text('[\n' + rows + '\n]\n', encoding='ascii')


if __name__ == '__main__':
    main()

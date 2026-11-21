#!/usr/bin/env python3
"""Writes the vectors the Rust Host's export of a render spec is held to (R137, the Python Host's
PR 474).

`GET /artifacts/<id>/export` gives the administrator what a move to another Host needs of a render
spec, whatever the caller's display negotiation: `{kind: "render_spec", render_spec: {artifact,
stored, submission}}`, the public row, the payload as kept and the accepted submission or none
(`render_spec_store.export`); another kind is refused with its kind (`NotExportable`), an id
nobody has is none. This publishes a spec, exports it, takes an answer to it, exports it again,
and asks the same of a document and of an id nobody has, on a store of its own with the clock
and the agents' ids fixed, and writes what came out to
`crates/clarp-kernel/tests/fixtures/render-spec-export-vectors.json`.

    python3 server-rs/tools/render-spec-export-vectors.py

Nothing of a Host is started; the library writes only in a scratch place of its own
(`tools/scratch.py`), which is gone when it ends.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch as tool_scratch  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'render-spec-export-vectors.json'
T0 = 1_795_000_000_000


def isolate(place: pathlib.Path) -> None:
    """Points the library at a place of its own before it is imported."""
    os.environ.update({
        'CLAUDE_PWA_DB': str(place / 'state.sqlite'), 'CLAUDE_PWA_LOG_DIR': str(place / 'logs'),
        'CLARP_CONFIG_DIR': str(place / 'config'), 'CLARP_SHARE_DIR': str(place / 'share'),
        'CLARP_CACHE_DIR': str(place / 'cache'), 'CLAUDE_PWA_CONFIG': str(place / 'no-such-config.toml'),
        'CLARP_CODE_ROOT': str(REPO / 'server'),
    })
    sys.path.insert(0, str(REPO / 'server'))


def generate(place: pathlib.Path) -> int:
    isolate(place)
    from lib import agents as agents_db, apns, artifacts, clock, db, render_spec_store
    now = [T0]
    for module in [clock, db, *sys.modules.values()]:
        if getattr(module, '__name__', '').startswith('lib.') and hasattr(module, 'now_ms'):
            module.now_ms = lambda: now[0]
    db.reset_for_tests(place / 'export.sqlite')
    minted = iter(f'agent-{n}' for n in range(1, 100))
    agents_db._new_agent_id = lambda: next(minted)
    # A decision's push goes out from a thread of its own: nothing is pushed from here.
    apns.on_decision_created = lambda artifact: None
    apns.send_decision_created = lambda artifact: {}
    work = place / 'work'
    work.mkdir()
    agents_db.create_agent(persona='Theo', voice_id='v', cwd=str(work), session='theo')
    spec = json.loads((REPO / 'tests' / 'fixtures' / 'render_spec' / 'build-decision.json').read_text())

    def step() -> None:
        now[0] += 1000

    step()
    artifacts.create(session='theo', type='render_spec', title='Decide', payload=spec, artifact_id='spec-1')
    exports = [{'name': 'a spec without an answer', 'artifact_id': 'spec-1',
                'export': render_spec_store.export('spec-1')}]
    step()
    answer = {'submission_id': 'export-answer-01', 'version': spec['version'], 'synthesize_audio': False,
              'answers': {'intent': 'submit_decision', 'state': {'choice': 'send'}}}
    render_spec_store.submit('spec-1', answer, capable=True)
    exports.append({'name': 'a spec with its accepted answer', 'artifact_id': 'spec-1',
                    'export': render_spec_store.export('spec-1'), 'answer': answer})
    step()
    artifacts.create(session='theo', type='document', title='Notes', payload={'content': 'words'},
                     artifact_id='doc-1')
    try:
        render_spec_store.export('doc-1')
        refused = None
    except render_spec_store.NotExportable as kind:
        refused = str(kind)
    exports.append({'name': 'another kind', 'artifact_id': 'doc-1', 'refused_kind': refused})
    exports.append({'name': 'an id nobody has', 'artifact_id': 'no-such-artifact',
                    'export': render_spec_store.export('no-such-artifact')})

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        'about': 'Written by server-rs/tools/render-spec-export-vectors.py from server/lib/render_spec_store.py '
                 '(export). Do not edit by hand.',
        'spec': spec, 'exports': exports}, ensure_ascii=False, sort_keys=True, indent=1) + '\n')
    print(f'{OUT.relative_to(REPO)}: {len(exports)} exports, {OUT.stat().st_size} bytes')
    # The library's connections are closed, and whatever it started has ended, before its place
    # goes: nothing of it is left.
    import threading
    for thread in threading.enumerate():
        if thread is not threading.main_thread():
            thread.join(timeout=10)
    db.reset_for_tests()
    return 0


def main(argv: list[str]) -> int:
    if os.environ.get('PYTHONHASHSEED') != '0':
        os.execve(sys.executable, [sys.executable, *argv], {**os.environ, 'PYTHONHASHSEED': '0'})
    with tool_scratch.directory('clarp-render-spec-export-') as place:
        return generate(pathlib.Path(place))


if __name__ == '__main__':
    sys.exit(main(sys.argv))

#!/usr/bin/env python3
"""Writes the vectors the Rust Host's feed of deliveries is tested against.

A client that keeps a chat catches up on what changed of its sends with `GET /log?delivery_since=`
(feature `send_delivery_state`). The Python Host answers from its launch receipts: one page of the
agent's seq pointers (`launch_receipts.delivery_page`), each checked against its receipt and
projected, with a cursor that is valid only for the agent's user Stop generation it was taken
under; and the user rows of a page are given their delivery from one batched read
(`annotate_rows`). This drives the receipts through the Python Host's own transitions
(`record_pending`, `mark_attempted`, `fence_agent`, `prune`) on a store of its own, asks for pages
and annotations along the way, and writes, for each, what the rule was given (the agent's pointers
and their receipts as stored, the current seq, the prune watermark, the generation) and what it
answered, to `crates/clarp-kernel/tests/fixtures/delivery-feed-vectors.json`.

    python3 server-rs/tools/delivery-feed-vectors.py

Nothing of a Host is started; the library writes only under /var/tmp.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'delivery-feed-vectors.json'
SCRATCH = pathlib.Path('/var/tmp/clarp-delivery-feed-vectors')
T0 = 1_794_700_000_000
DAY = 86_400_000


def isolate() -> None:
    """Points the library at a place of its own before it is imported."""
    shutil.rmtree(SCRATCH, ignore_errors=True)
    SCRATCH.mkdir(parents=True)
    os.environ.update({
        'CLAUDE_PWA_DB': str(SCRATCH / 'state.sqlite'), 'CLAUDE_PWA_LOG_DIR': str(SCRATCH / 'logs'),
        'CLARP_CONFIG_DIR': str(SCRATCH / 'config'), 'CLARP_SHARE_DIR': str(SCRATCH / 'share'),
        'CLARP_CACHE_DIR': str(SCRATCH / 'cache'), 'CLAUDE_PWA_CONFIG': str(SCRATCH / 'no-such-config.toml'),
        'CLARP_CODE_ROOT': str(REPO / 'server'),
    })
    sys.path.insert(0, str(REPO / 'server'))


class World:
    def __init__(self):
        from lib import clock, db, launch_receipts, settings_store, turn_queue
        self.db, self.lr, self.store, self.queue = db, launch_receipts, settings_store, turn_queue
        self.now = T0
        for module in [clock, db, *sys.modules.values()]:
            if getattr(module, '__name__', '').startswith('lib.') and hasattr(module, 'now_ms'):
                module.now_ms = lambda: self.now
        db.reset_for_tests(SCRATCH / 'feed.sqlite')
        launch_receipts.reset_for_tests()
        self.pages: list[dict] = []
        self.rows: list[dict] = []

    def tick(self, ms: int = 1000) -> None:
        self.now += ms

    def send(self, agent: str, trace: str, cmid: str, *, origin: str = 'user', sender: str = '',
             supersedes: str = '') -> None:
        self.tick()
        self.lr.record_pending(agent_id=agent, trace_id=trace, client_msg_id=cmid, origin=origin,
                               sender_agent_id=sender, supersedes=supersedes,
                               user_stop_generation=self.queue.user_stop_generation(agent))

    def handed(self, trace: str, *, override: bool = False) -> None:
        self.tick()
        try:
            self.lr.mark_attempted(trace, override_fence=override)
        except self.lr.HandoffRefused:
            pass

    def user_stop(self, agent: str) -> None:
        """The user's Stop: the queue's pause moves the user Stop generation, then the fence."""
        self.tick()
        self.queue.set_paused(agent, True, user_stop=True)
        self.lr.note_stop(agent)
        self.lr.fence_agent(agent)

    def resume(self, agent: str) -> None:
        self.tick()
        self.queue.set_paused(agent, False)

    def prune(self) -> None:
        self.lr.prune(self.now)

    def plant(self, key: str, value: str) -> None:
        """A stored value the transitions would not write: a stale pointer, a broken receipt."""
        with self.store.transaction():
            self.store.set_text(key, value, updated_at=self.now)

    def state(self, agent: str) -> dict:
        """What the rule is given: the agent's pointers in key order, the receipts they name as
        stored (text, or null), the current seq, the watermark and the generation."""
        lr, store = self.lr, self.store
        prefix = f'{lr.POINTER_PREFIX}{agent}:'
        pointers = store.items_in_range(prefix, prefix[:-1] + ';', limit=100_000)
        raws = store.get_many([lr._key(trace) for _key, trace in pointers])
        return {'pointers': [[lr._pointer_seq(key), trace] for key, trace in pointers],
                'receipts': {trace: raws.get(lr._key(trace)) for _key, trace in pointers},
                'current': store.get_int(lr.SEQ_KEY), 'watermark': store.get_int(lr.PRUNE_SEQ_KEY),
                'generation': self.queue.user_stop_generation(agent)}

    def page(self, what: str, agent: str, *, since: int, until: int | None = None,
             stop_generation: int | None = None, bootstrap: bool = False, cap: int = 200) -> dict:
        given = self.state(agent)
        page = self.lr.delivery_page(agent, since=since, until=until, stop_generation=stop_generation,
                                     bootstrap=bootstrap, cap=cap)
        self.pages.append({'what': what, 'agent_id': agent, 'given': given, 'since': since, 'until': until,
                           'stop_generation': stop_generation, 'bootstrap': bootstrap, 'cap': cap,
                           'page': page})
        return page

    def bootstrap_all(self, what: str, agent: str, cap: int) -> None:
        """A whole bootstrap pass, page by page, as a client makes it."""
        first = self.page(f'{what}: first page', agent, since=0, bootstrap=True, cap=cap)
        cursor, until, generation, n = first['cursor'], first['until'], first['stop_generation'], 1
        more = first['more']
        while more and n < 50:
            n += 1
            page = self.page(f'{what}: page {n}', agent, since=cursor, until=until, stop_generation=generation,
                             bootstrap=True, cap=cap)
            cursor, more = page['cursor'], page['more']

    def annotate(self, what: str, agent: str, turns: list[dict]) -> None:
        lr, store = self.lr, self.store
        users = [t for t in turns if t.get('role') == 'user' and t.get('trace_id') and t.get('id')]
        raws = store.get_many([lr._key(t['trace_id']) for t in users]) if users else {}
        given = {'receipts': {t['trace_id']: raws.get(lr._key(t['trace_id'])) for t in users},
                 'generation': self.queue.user_stop_generation(agent)}
        rows = [dict(t) for t in turns]
        epoch = lr.annotate_rows(agent, rows)
        self.rows.append({'what': what, 'agent_id': agent, 'given': given, 'turns': turns, 'rows': rows,
                          'epoch': epoch})


def user(cmid: str, trace: str, **more) -> dict:
    return {'role': 'user', 'id': f'u-{cmid}', 'trace_id': trace, **more}


def story(w: World) -> None:
    lr = w.lr
    w.page('an agent with nothing', 'ag', since=0)
    w.page('nothing, bootstrap', 'ag', since=0, bootstrap=True)
    w.send('ag', 't1', 'm1')
    w.send('other', 'o1', 'x1')
    w.send('ag', 't2', 'm2')
    w.send('ag', 't3', 'm3', origin='agent', sender='helper')
    w.page('three of its own, one of another agent between', 'ag', since=0, stop_generation=0)
    w.page('from the first', 'ag', since=1, stop_generation=0)
    w.page('the other agent: its own pointer alone', 'other', since=0, stop_generation=0)
    w.page('generation missing', 'ag', since=1)
    w.page('generation from the future', 'ag', since=1, stop_generation=3)
    w.page('cursor ahead of the Host', 'ag', since=99, stop_generation=0)
    w.page('negative', 'ag', since=-1, stop_generation=0)
    w.page('cap one', 'ag', since=0, stop_generation=0, cap=1)
    w.page('cap two', 'ag', since=0, stop_generation=0, cap=2)
    w.page('cap three: exactly its pointers', 'ag', since=0, stop_generation=0, cap=3)
    w.handed('t1')
    w.annotate('one handed over, one pending, one of an agent, a reply between', 'ag', [
        user('m1', 't1'), {'role': 'assistant', 'id': 'r-1', 'trace_id': 't1'}, user('m2', 't2'),
        user('m3', 't3', origin='agent', sender_agent_id='helper'), user('m3', 't3'),
        user('m9', 't9'), {'role': 'user', 'id': 'u-m2', 'trace_id': ''}, {'role': 'user', 'id': '', 'trace_id': 't2'},
        user('m1', 't2'), {'role': 'user', 'id': 'm2', 'trace_id': 't2'}])
    w.page('after the handoff', 'ag', since=3, stop_generation=0)
    # The user stops the agent: the pending send is fenced, and the generation moves.
    w.user_stop('ag')
    w.page('a cursor of the old generation', 'ag', since=4, stop_generation=0)
    w.page('of the new one', 'ag', since=4, stop_generation=1)
    w.annotate('after the Stop', 'ag', [user('m1', 't1'), user('m2', 't2'), user('m3', 't3', origin='agent',
                                                                                   sender_agent_id='helper')])
    w.bootstrap_all('bootstrap after the Stop, one a page', 'ag', 1)
    w.bootstrap_all('bootstrap after the Stop, two a page', 'ag', 2)
    # A send after the Stop is pending under the new generation; the held one runs on resume.
    w.send('ag', 't4', 'm4')
    w.resume('ag')
    w.handed('t2', override=True)
    w.handed('t4')
    w.page('all since the Stop', 'ag', since=0, stop_generation=1)
    # A same-id retry of a send admitted and never launched: the old receipt is relaunched.
    w.send('ag', 't5', 'm5')
    w.send('ag', 't6', 'm5', supersedes='t5')
    w.annotate('the retried send, under its old and its new trace', 'ag', [user('m5', 't5'), user('m5', 't6')])
    w.page('with the relaunched one', 'ag', since=0, stop_generation=1)
    # A client whose ids carry the row's prefix already, and a row whose origin is empty.
    w.send('ag', 'tu', 'u-mu')
    w.annotate('an id with its prefix, an empty origin', 'ag', [
        {'role': 'user', 'id': 'u-mu', 'trace_id': 'tu'}, {'role': 'user', 'id': 'u-u-mu', 'trace_id': 'tu'},
        {'role': 'user', 'id': 'u-mu', 'trace_id': 'tu', 'origin': ''},
        {'role': 'user', 'id': 'u-mu', 'trace_id': 'tu', 'sender_agent_id': ''}])
    w.page('with the prefixed one', 'ag', since=13, stop_generation=1)
    # A second Stop with nothing pending moves the generation alone.
    w.user_stop('ag')
    w.page('a second Stop with nothing pending, the old generation', 'ag', since=0, stop_generation=1)
    w.annotate('after the second Stop', 'ag', [user('m5', 't6'), user('m4', 't4')])
    w.bootstrap_all('bootstrap, three a page', 'ag', 3)
    # A bootstrap pass interrupted by a Stop, and one interrupted by a new send.
    first = w.page('a pass begins', 'ag', since=0, bootstrap=True, cap=2)
    w.send('ag', 't7', 'm7')
    w.page('a send between its pages', 'ag', since=first['cursor'], until=first['until'],
           stop_generation=first['stop_generation'], bootstrap=True, cap=2)
    w.user_stop('ag')
    w.page('a Stop between its pages', 'ag', since=first['cursor'], until=first['until'],
           stop_generation=first['stop_generation'], bootstrap=True, cap=2)
    w.page('a later page with no until', 'ag', since=2, stop_generation=3, bootstrap=True)
    w.page('until beyond the Host', 'ag', since=0, until=999, stop_generation=3, bootstrap=True)
    w.page('since beyond until', 'ag', since=5, until=4, stop_generation=3, bootstrap=True)
    w.page('a first page that is not at the start', 'ag', since=3, bootstrap=True)
    # What the store holds that no transition wrote: skipped and counted.
    w.plant(lr.pointer_key('ag', 900), 't1')
    w.plant(lr.pointer_key('ag', 901), 'gone')
    w.plant(lr._key('broken'), '{')
    w.plant(lr.pointer_key('ag', 902), 'broken')
    w.plant(lr._key('elsewhere'), json.dumps({'state': 'pending', 'agent_id': 'other', 'trace_id': 'elsewhere',
                                              'client_msg_id': 'e', 'rev': 1, 'seq': 903, 'user_stop_gen': 0}))
    w.plant(lr.pointer_key('ag', 903), 'elsewhere')
    w.plant(lr._key('legacy'), json.dumps({'state': 'pending', 'agent_id': 'ag', 'trace_id': 'legacy',
                                           'client_msg_id': 'l'}))
    w.plant(lr.SEQ_KEY, '905')
    w.plant(lr.pointer_key('ag', 904), 'legacy')
    w.page('pointers that do not hold', 'ag', since=0, stop_generation=3)
    w.page('pointers that do not hold, cap one at the end', 'ag', since=899, stop_generation=3, cap=1)
    w.annotate('a legacy receipt', 'ag', [user('l', 'legacy'), user('e', 'elsewhere'), user('x', 'broken')])
    # A week later: the prune takes what is old and not fenced, and moves the watermark.
    w.tick(8 * DAY)
    w.send('ag', 't8', 'm8')
    w.prune()
    w.page('behind the watermark', 'ag', since=1, stop_generation=3)
    w.page('at zero, behind the watermark', 'ag', since=0, stop_generation=3)
    w.page('at the watermark', 'ag', since=w.state('ag')['watermark'], stop_generation=3)
    w.bootstrap_all('bootstrap after the prune', 'ag', 2)
    w.annotate('after the prune', 'ag', [user('m1', 't1'), user('m2', 't2'), user('m8', 't8')])
    w.page('the other agent, from its start', 'other', since=0, stop_generation=0)


def main(argv: list[str]) -> int:
    if os.environ.get('PYTHONHASHSEED') != '0':
        os.execve(sys.executable, [sys.executable, *argv], {**os.environ, 'PYTHONHASHSEED': '0'})
    isolate()
    world = World()
    story(world)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        'about': 'Written by server-rs/tools/delivery-feed-vectors.py from server/lib/launch_receipts.py '
                 '(delivery_page, annotate_rows, through record_pending, mark_attempted, fence_agent, prune). '
                 'Do not edit by hand.',
        'page_cap': world.lr.PAGE_CAP, 'pages': world.pages, 'rows': world.rows,
    }, ensure_ascii=False, sort_keys=True, indent=1) + '\n')
    expired = sum(1 for p in world.pages if p['page']['expired'])
    shown = sum(1 for r in world.rows for row in r['rows'] if 'delivery' in row)
    print(f'{OUT.relative_to(REPO)}: {len(world.pages)} pages ({expired} expired), {len(world.rows)} annotations '
          f'({shown} rows shown), {OUT.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))

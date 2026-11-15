#!/usr/bin/env python3
"""Writes the vectors the Rust Host's requests between agents are tested against.

One agent asks another for something that must come back: `clarp-admin request`, answered
with `clarp-admin reply`. What the two are told is worded by the Python Host in
`server/lib/peer_requests.py` (`request_text`, `reply_text`), which also names the messages
(`request_client_id`, the ids in `plan_message`) and the goal's dependency
(`dependency_key`). Agents read these words and act on them, so they are part of the
contract. This calls the pure functions for a set of inputs and writes what they said to
`crates/clarp-kernel/tests/fixtures/request-vectors.json`.

    python3 server-rs/tools/request-vectors.py

Nothing of a Host is touched or started: the library is pointed at an empty directory under
/var/tmp before it is imported, and only functions that build a text or a name are called.
Every text is synthetic.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'request-vectors.json'
SCRATCH = pathlib.Path('/var/tmp/clarp-request-vectors')

AGENTS = [{'persona': 'Vera', 'session': 'vera'}, {'persona': 'Omar the Second', 'session': 'omar-2'},
          {'persona': 'Ünïcödé', 'session': 'u'}]
TEXTS = ['Please add an event log to forms; tell me when it is live.', 'Two\nlines', '', '  padded  ']
# Longer than what a goal keeps of an answer: once is enough to see where it is cut.
LONG = 'x' * 8100
IDS = ['r0123456789ab', 'rffffffffffff']


def isolate() -> None:
    shutil.rmtree(SCRATCH, ignore_errors=True)
    SCRATCH.mkdir(parents=True)
    os.environ.update({
        'CLAUDE_PWA_DB': str(SCRATCH / 'state.sqlite'), 'CLAUDE_PWA_LOG_DIR': str(SCRATCH / 'logs'),
        'CLARP_CONFIG_DIR': str(SCRATCH / 'config'), 'CLARP_SHARE_DIR': str(SCRATCH / 'share'),
        'CLARP_CACHE_DIR': str(SCRATCH / 'cache'), 'CLAUDE_PWA_CONFIG': str(SCRATCH / 'no-such-config.toml'),
        'CLARP_CODE_ROOT': str(REPO / 'server'),
    })
    sys.path.insert(0, str(REPO / 'server'))


def main() -> int:
    isolate()
    from lib import peer_requests as pr

    requests = [{'request_id': request_id, 'requester': requester, 'text': text,
                 'said': pr.request_text(request_id, requester, text),
                 'client_id': pr.request_client_id(request_id), 'dependency': pr.dependency_key(request_id)}
                for request_id in IDS for requester in AGENTS for text in TEXTS]
    replies = []
    cases = [(request_id, replier, kind, text)
             for request_id in IDS for replier in AGENTS for kind in pr.KINDS for text in TEXTS]
    cases.append((IDS[0], AGENTS[0], 'result', LONG))
    for request_id, replier, kind, text in cases:
        if True:
            if True:
                if True:
                    said = pr.reply_text(request_id, replier, kind, text)
                    digest = hashlib.sha256(f'{kind}\0{text}'.encode()).hexdigest()[:12]
                    # The names `plan_message` gives a reply as a message: a result's first
                    # one, a result that differs from the first, and an update.
                    replies.append({
                        'request_id': request_id, 'replier': replier, 'kind': kind, 'text': text, 'said': said,
                        'evidence': said[:pr.MAX_EVIDENCE],
                        'client_id': (f'peer-res-{request_id}' if kind == 'result'
                                      else f'peer-{kind}-{request_id}-{digest}'),
                        'client_id_when_another_came_first': (f'peer-res-{request_id}-{digest}' if kind == 'result'
                                                               else f'peer-{kind}-{request_id}-{digest}')})
    ids = [{'given': given, 'is_one': bool(pr._ID.fullmatch(given))}
           for given in ['r0123456789ab', 'r0123456789AB', 'r0123456789a', 'r0123456789abc', 'x0123456789ab', '',
                         'r0123456789ab\n', ' r0123456789ab']]
    limits = {'kinds': list(pr.KINDS), 'max_updates': pr.MAX_UPDATES, 'max_deadline_s': pr.MAX_DEADLINE_S,
              'default_deadline_s': pr.DEFAULT_DEADLINE_S, 'max_evidence': pr.MAX_EVIDENCE,
              'not_delivered': pr.NOT_DELIVERED}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    vectors = {'about': 'Written by server-rs/tools/request-vectors.py from server/lib/peer_requests.py. '
                        'Do not edit by hand.',
               'limits': limits, 'requests': requests, 'replies': replies, 'ids': ids}
    OUT.write_text(json.dumps(vectors, ensure_ascii=False, indent=1) + '\n')
    print(f'{OUT.relative_to(REPO)}: {len(requests)} requests, {len(replies)} replies, {len(ids)} ids, '
          f'{OUT.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    sys.exit(main())

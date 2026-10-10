"""/attention/inbox reads only the page's payloads.

Every page request used to load every eligible artifact with its payload
(33.7 MB on the 2026-10-10 database), parse each one, and hash a JSON dump of
all of them for the cursor revision: 156-233 MB of allocation per 200-row
page, for an iOS client that pages through the whole inbox on each refresh.
The response must not change: current iOS renders and opens forms from the
inline content (Avana, 2026-10-10).
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
import tracemalloc

from lib import agents, artifacts, attention_index, countdown_attention, db, quiet_agents


def _reference_page(*, limit=100, cursor='', representation=''):
    """attention_index.page as it was before this change, verbatim, minus the
    revision (whose value may change format; its behaviour is tested apart)."""
    TYPES, bucket = attention_index.TYPES, attention_index.bucket
    limit = max(1, min(int(limit), 200))
    now_ms = int(time.time() * 1000)
    c = db.conn()
    marks = ','.join('?' for _ in TYPES)
    rows = c.execute(f'''SELECT a.*,g.persona AS agent_name FROM artifacts a
        JOIN agents g ON a.agent_id=g.agent_id WHERE a.deleted_at IS NULL
        AND {quiet_agents.loud_sql('g')}
        AND a.type IN ({marks}) ORDER BY a.created_at,a.artifact_id''', tuple(sorted(TYPES))).fetchall()
    canonical = {}
    for r in rows:
        key = ('artifact', r['artifact_id'])
        if r['type'] == 'workflow_run':
            try:
                p = json.loads(r['payload_json'] or '{}')
                if p.get('provider') and p.get('repository') and p.get('run_id'):
                    key = ('workflow', p['provider'], p['repository'], str(p['run_id']), str(p.get('run_attempt', 1)))
            except (ValueError, TypeError, AttributeError):
                pass
        old = canonical.get(key)
        if old is None or (r['updated_at'], r['artifact_id']) > (old['updated_at'], old['artifact_id']):
            canonical[key] = r
    selected = [(r, bucket(r, now_ms)) for r in canonical.values()]
    selected = [(r, b) for r, b in selected if b]
    ranks = {'blocking': 0, 'review': 1, 'working': 2}
    selected.sort(key=lambda rb: (ranks[rb[1]], rb[0]['created_at'], rb[0]['artifact_id']))
    offset = int(json.loads(base64.urlsafe_b64decode(cursor.encode()))['offset']) if cursor else 0
    result = []
    for r, b in selected[offset:offset + limit]:
        public = artifacts.response_representation(artifacts._public(r), representation)
        public['attention_bucket'] = b
        result.append(public)
    return {'artifacts': result, 'total': len(selected), 'end': offset + len(result)}


def _seed(tmp_path, *, big=0):
    agents.create_agent(persona='Mike', voice_id='V', cwd=str(tmp_path), session='mike')
    quiet = agents.create_agent(persona='Quiet', voice_id='V', cwd=str(tmp_path), session='quiet')
    db.conn().execute('UPDATE agents SET is_janitor=1 WHERE agent_id=?', (quiet,))
    made = []
    for i, (kind, status) in enumerate([
            ('document', 'ready'), ('html_form', 'completed'), ('research', 'failed'),
            ('data', 'active'), ('file', 'draft'), ('audio', 'cancelled'), ('video', 'expired'),
            ('code_change', 'ready'), ('release', 'completed'), ('directory', 'active'),
            ('document', 'ready'), ('document', 'failed')]):
        payload = {'content': f'body {i} ' + ('x' * big)}
        if i == 11:
            payload['attention_kind'] = 'janitor_failure'
        made.append(artifacts.create(session='mike', type='document', title=f'Result {i}', status=status,
                                     payload={'content': f'body {i}'})['artifact_id'])
        # Reports far larger than create() accepts exist live (up to 4.1 MB).
        db.conn().execute('UPDATE artifacts SET type=?, payload_json=? WHERE artifact_id=?',
                          (kind, json.dumps(payload), made[-1]))
    made.append(artifacts.create(session='mike', type='countdown', title='Due', status='ready',
                                 payload={'target_at': '2000-01-01T00:00:00Z', 'time_zone': 'UTC',
                                          'purpose': 'informational'})['artifact_id'])
    made.append(artifacts.create(session='mike', type='countdown', title='Broken', status='ready',
                                 payload={'target_at': '2000-01-01T00:00:00Z', 'time_zone': 'UTC',
                                          'purpose': 'informational'})['artifact_id'])
    db.conn().execute('UPDATE artifacts SET payload_json=? WHERE artifact_id=?',
                      ('{"target_at":"invalid"}', made[-1]))
    made.append(artifacts.create(session='quiet', type='document', title='Quiet', status='ready',
                                 payload={'content': 'janitor'})['artifact_id'])
    con = db.conn()
    # Payloads only a raw write could leave: not JSON, and JSON but not an object.
    con.execute('UPDATE artifacts SET payload_json=? WHERE artifact_id=?', ('{not json', made[7]))
    con.execute('UPDATE artifacts SET payload_json=? WHERE artifact_id=?', ('[1,2]', made[8]))
    con.execute('UPDATE artifacts SET deleted_at=1 WHERE artifact_id=?', (made[10],))
    con.execute('UPDATE artifacts SET archived_at=5, pinned_at=6 WHERE artifact_id=?', (made[0],))
    # Shared creation times make the artifact_id tie-break decide the order.
    con.execute('UPDATE artifacts SET created_at=1000 WHERE artifact_id IN (?,?,?)', tuple(made[1:4]))
    return made


def _walk(page, limit):
    pages, cursor = [], ''
    while True:
        result = page(limit=limit, cursor=cursor, representation='flat-v1')
        pages.append(result)
        cursor = result.get('next_cursor')
        if not cursor:
            return pages


def test_every_page_matches_the_previous_implementation(tmp_path):
    _seed(tmp_path)
    for limit in (1, 3, 200):
        new = _walk(attention_index.page, limit)
        reference, offset = [], 0
        for _ in new:
            cursor = base64.urlsafe_b64encode(json.dumps({'revision': '', 'offset': offset}).encode()).decode()
            ref = _reference_page(limit=limit, cursor=cursor if offset else '', representation='flat-v1')
            reference.append(ref)
            offset = ref['end']
        assert [p['artifacts'] for p in new] == [p['artifacts'] for p in reference], limit
        assert {p['total'] for p in new} == {reference[0]['total']}
        assert new[-1]['next_cursor'] is None
    buckets = {a['title']: a['attention_bucket'] for p in _walk(attention_index.page, 200) for a in p['artifacts']}
    assert buckets['Broken'] == 'blocking' and buckets['Due'] == 'review'
    assert buckets['Result 7'] == 'blocking' and buckets['Result 8'] == 'blocking', 'unreadable payloads block'
    assert 'Result 11' not in buckets and 'Quiet' not in buckets and 'Result 10' not in buckets


def test_the_revision_follows_what_the_page_shows(tmp_path):
    made = _seed(tmp_path)
    first = attention_index.page(limit=2)
    assert attention_index.page(limit=2)['snapshot_revision'] == first['snapshot_revision']
    second = attention_index.page(limit=2, cursor=first['next_cursor'])
    assert second['snapshot_revision'] == first['snapshot_revision']
    seen = {first['snapshot_revision']}
    changes = [
        lambda: artifacts.update(made[2], {'payload': {'content': 'edited body'}}),
        lambda: db.conn().execute('UPDATE artifacts SET pinned_at=99 WHERE artifact_id=?', (made[3],)),
        lambda: db.conn().execute("UPDATE agents SET persona='Michael' WHERE session='mike'"),
        lambda: db.conn().execute('UPDATE artifacts SET archived_at=7 WHERE artifact_id=?', (made[1],)),
    ]
    for change in changes:
        change()
        revision = attention_index.page(limit=2)['snapshot_revision']
        assert revision not in seen, "a change the page shows must restart pagination"
        seen.add(revision)


def test_a_page_reads_only_its_own_payloads(tmp_path):
    # 12 artifacts carry 1 MB each; a one-row page needs one of them.
    _seed(tmp_path, big=1_000_000)
    tracemalloc.start()
    try:
        result = attention_index.page(limit=1)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(result['artifacts']) == 1
    assert peak < 5_000_000, f"peak allocation {peak / 1e6:.1f} MB for a one-row page"

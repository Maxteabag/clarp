#!/usr/bin/env python3
"""Benchmark the pair-list projection on a private SQLite backup; print no messages."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import statistics
import sys
import tempfile
import time

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--database', type=Path, required=True)
p.add_argument('--runs', type=int, default=5)
p.add_argument('--clients', type=int, default=1)
p.add_argument('--cached', action='store_true', help='Exercise the shared HTTP response cache')
p.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[2])
a = p.parse_args()
if a.runs < 1 or a.clients < 1:
    p.error('--runs and --clients must be positive')
with tempfile.TemporaryDirectory(prefix='clarp-pair-benchmark-', dir='/var/tmp') as directory:
    root = Path(directory)
    with sqlite3.connect(f'{a.database.resolve().as_uri()}?mode=ro', uri=True) as source, sqlite3.connect(root/'state.sqlite') as target:
        source.backup(target)
    for key, name in {'CLAUDE_PWA_DB':'state.sqlite', 'CLARP_TELEMETRY_DB':'telemetry.sqlite',
                      'CLAUDE_PWA_CONFIG':'absent.toml', 'CLARP_CONFIG_DIR':'config',
                      'CLARP_DATA_DIR':'data', 'CLARP_CACHE_DIR':'cache',
                      'CLARP_RUNTIME_SOCKET':'absent.sock'}.items():
        os.environ[key] = str(root/name)
    sys.path.insert(0, str(a.repo.resolve()/'server'))
    from lib import agent_conversations, db
    # Resolve DB initialization outside the measured projection.
    db.conn()
    wall, cpu = [], []
    cache = agent_conversations.PairConversationListCache() if a.cached else None
    def fetch(_):
        if cache is not None:
            return json.loads(cache.get_payload())["conversations"]
        return agent_conversations.list_conversations()
    pool = ThreadPoolExecutor(max_workers=a.clients)
    for _ in range(a.runs):
        start, cpu_start = time.perf_counter(), time.process_time()
        results = list(pool.map(fetch, range(a.clients)))
        result = results[0]
        assert all(r == result for r in results), "inconsistent responses"
        cpu.append((time.process_time()-cpu_start)*1000)
        wall.append((time.perf_counter()-start)*1000)
    print(json.dumps({'runs':a.runs, 'clients':a.clients, 'cached':a.cached, 'rooms':len(result),
        'result_sha256':hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest(),
        'wall_ms':[round(x,2) for x in wall], 'cpu_ms':[round(x,2) for x in cpu],
        'median_wall_ms':round(statistics.median(wall),2),
        'median_cpu_ms':round(statistics.median(cpu),2)}))
    pool.shutdown()
    db.close_local()

#!/usr/bin/env python3
"""Record the Python Host's cumulative usage at each provider event."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch as tool_scratch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'server-rs/crates/clarp-kernel/tests/fixtures/oracle-live-usage-vectors.json'


def generate():
    spec = importlib.util.spec_from_file_location('host', ROOT / 'tests/replay/host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    with tool_scratch.directory() as work:
        root = Path(work)
        (root / 'scenario-config.toml').write_text('')
        host.isolate(root)
        host.loopback_only()
        sys.path.insert(0, str(ROOT / 'server'))
        from lib.oracle_live_usage import LiveUsage
        stories = []
        for mode in ('api', 'subscription', 'gemini'):
            for end in (None, 0, 31.25):
                usage = LiveUsage(mode)
                steps = []
                for event in [
                    {}, {'type': 'session.started', 'session': {'expires_at': 99}},
                    *[{'type': 'session.usage.updated', 'usage': {'seconds': n, 'voice_estimate_usd': n},
                       'context_window': {'usage_ratio': n}} for n in (0, 1, 1.1, 0.4, 42, 30, -1, True, '2', None)],
                    # Active and standby time, priced apart when the provider gives both (the reference's PR 479).
                    *[{'type': 'session.usage.updated', 'usage': {'seconds': 50 + n, **given}} for n, given in enumerate((
                        {'active_seconds': 20, 'paused_seconds': 30}, {'active_seconds': 21},
                        {'active_seconds': 1.5, 'paused_seconds': 0}, {'active_seconds': 2, 'paused_seconds': True},
                        {'active_seconds': -1, 'paused_seconds': 3}, {'active_seconds': 7.25, 'paused_seconds': 12.5}))],
                    {'type': 'session.usage.updated', 'usage': {'active_seconds': 1, 'paused_seconds': 1}},
                    {'type': 'session.closed', 'usage': {'seconds': end}, 'reason': 'r' * 125 if end else None},
                    {'type': 'session.usage.updated', 'usage': {'seconds': 100}},
                ]:
                    steps.append({'event': event, 'observed': usage.observe(event), 'snapshot': usage.snapshot()})
                stories.append({'mode': mode, 'steps': steps})
        return stories


if __name__ == '__main__':
    if sys.argv[1:] == ['--run']:
        print(json.dumps(generate(), indent=1))
    else:
        result = subprocess.check_output([sys.executable, '-I', __file__, '--run'],
                                         env=tool_scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}))
        OUT.write_bytes(result)

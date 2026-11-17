#!/usr/bin/env python3
"""What the Python Host makes of what Codex says of its account's limits
(`server/lib/backend_usage_codex.py`, `_codex_snapshot_from_payload`, with
`_select_rate_limit_payload` and `backend_usage_store._window_from_payload`): how much of each
window is used and when it begins anew. Its own functions are given made-up records of every
shape they read (the record Codex writes into its rollout with each count of tokens, the answer
of its usage endpoint, the notification of its app-server) and of shapes they refuse, and what
they answer is written to `crates/clarp-backend/tests/fixtures/codex-limits-vectors.json`, so
that the Rust Host's reading is held to it.

It runs in a process of its own whose every home, data, config and cache directory is a new empty
directory (the replay harness's own isolation, `tests/replay/host.py`), with no way out to any
network.

    nice "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/codex-limits-vectors.py
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-backend' / 'tests' / 'fixtures' / 'codex-limits-vectors.json'

# A window as Codex writes it into a rollout, and as its endpoint and its app-server say it.
FIVE_HOURS = {'used_percent': 37.5, 'window_minutes': 300, 'resets_at': 1_900_000_000}
WEEK = {'used_percent': 82.0, 'window_minutes': 10_080, 'resets_at': 1_900_400_000}


def rollout(**more):
    """The record of limits in a rollout's count of tokens."""
    return {'limit_id': 'codex', 'limit_name': None, 'primary': FIVE_HOURS, 'secondary': WEEK,
            'credits': {'has_credits': False, 'unlimited': False, 'balance': None},
            'individual_limit': None, 'spend_control_reached': None, 'plan_type': 'pro',
            'rate_limit_reached_type': None, **more}


WINDOWS = [
    FIVE_HOURS, WEEK, None, {}, 'a text', 7, [],
    {'used_percent': 0, 'window_minutes': 300, 'resets_at': 1_900_000_000},
    {'used_percent': 100.0, 'window_minutes': 300, 'resets_at': 1_900_000_000},
    {'used_percent': '41', 'window_minutes': 300, 'resets_at': 1_900_000_000},
    {'used_percent': 'much', 'window_minutes': 300, 'resets_at': 1_900_000_000},
    {'used_percent': None, 'window_minutes': 300, 'resets_at': 1_900_000_000},
    {'used_percent': 12.25},
    {'used_percent': 12.25, 'resets_at': None},
    {'used_percent': 12.25, 'resets_at': ''},
    {'used_percent': 12.25, 'resets_at': 0},
    {'used_percent': 12.25, 'resets_at': '2030-03-17T17:46:40Z'},
    {'used_percent': 12.25, 'resets_at': '2030-03-17T18:46:40+01:00'},
    {'used_percent': 12.25, 'resets_at': 'soon'},
    # A time given as a text that is a number is that many seconds.
    {'used_percent': 12.25, 'resets_at': '1900000000'},
    {'used_percent': 12.25, 'resets_at': ' 1900000000 '},
    {'used_percent': 12.25, 'resets_at': '1900000000.9'},
    {'used_percent': 12.25, 'resets_at': '0'},
    {'used_percent': 12.25, 'resets_at': '-5'},
    {'resets_at': '0'},
    {'used_percent': 12.25, 'resets_at': True},
    {'used_percent': 12.25, 'resets_at': False},
    {'used_percent': 12.25, 'resets_at': 1_900_000_000_000},
    {'used_percent': 12.25, 'resets_at': 1_900_000_000.75},
    {'resets_at': 1_900_000_000},
    {'window_minutes': 300},
    {'used_percentage': 55, 'window_minutes': 300, 'resets_at': 1_900_000_000},
    {'used_percentage': 55, 'used_percent': 66, 'usedPercent': 77},
    {'used_percent': 66, 'usedPercent': 77},
    {'usedPercent': 77, 'windowDurationMins': 300, 'resetsAt': 1_900_000_000},
    {'usedPercent': 77, 'window_minutes': 60, 'windowDurationMins': 300},
    {'used_percent': 5, 'reset_at': 1_900_000_000, 'resetsAt': 1_900_400_000},
    {'used_percent': 5, 'resets_at': 1_900_000_000, 'reset_at': 1_900_400_000},
    {'used_percent': 5, 'limit_window_seconds': 18_000},
    {'used_percent': 5, 'limit_window_seconds': '604800'},
    {'used_percent': 5, 'limit_window_seconds': 'a week'},
    {'used_percent': 5, 'limit_window_seconds': 90},
    {'used_percent': 5, 'window_minutes': None, 'limit_window_seconds': 18_000},
    {'used_percent': True},
    {'used_percent': -3.5, 'window_minutes': 300},
    {'used_percent': 250, 'window_minutes': 300},
    {'used_percent': float('nan'), 'window_minutes': 300} if False else {'used_percent': 1e30, 'window_minutes': 300},
]

PAYLOADS = [
    # As a rollout has it, given the name the endpoint gives it.
    {'rate_limit': rollout()},
    {'rate_limit': rollout(secondary=None)},
    {'rate_limit': rollout(primary=None)},
    {'rate_limit': rollout(primary=None, secondary=None)},
    {'rate_limit': rollout(primary={}, secondary={})},
    {'rate_limit': rollout(primary=WEEK, secondary=FIVE_HOURS)},
    {'rate_limit': {'primary_window': FIVE_HOURS, 'secondary_window': WEEK}},
    {'rate_limit': {'primary_window': FIVE_HOURS, 'primary': WEEK}},
    {'rate_limit': {'primary_window': None, 'primary': WEEK}},
    {'rate_limit': rollout(), 'spend_control': {'individual_limit': {'used_percent': 9, 'resets_at': 1_900_800_000}}},
    {'rate_limit': rollout(primary=None, secondary=None),
     'spend_control': {'individual_limit': {'used_percent': 9, 'resets_at': 1_900_800_000}}},
    {'rate_limit': rollout(), 'spend_control': 'none'},
    {'rate_limit': rollout(), 'spend_control': {'individual_limit': None}},
    # As the app-server says it.
    {'rateLimits': {'limitId': 'codex', 'primary': FIVE_HOURS, 'secondary': WEEK}},
    {'rateLimits': {'primary': FIVE_HOURS}},
    {'rateLimits': [{'limitId': 'other', 'primary': WEEK}, {'limitId': 'codex', 'primary': FIVE_HOURS}]},
    {'rateLimits': [{'limitId': 'other', 'primary': WEEK}, {'limitId': 'more', 'primary': WEEK}]},
    {'rateLimits': [{'limitId': 'other', 'primary': WEEK}, {'limitId': 'more', 'primary': FIVE_HOURS}]},
    {'rateLimits': [{'primary': FIVE_HOURS}, {'primary': FIVE_HOURS}]},
    {'rateLimits': [], 'rateLimitsByLimitId': {}},
    {'rateLimitsByLimitId': {'codex': {'primary': FIVE_HOURS, 'secondary': WEEK}, 'spark': {'primary': WEEK}}},
    {'rateLimitsByLimitId': {'Codex': {'primary': FIVE_HOURS}}},
    {'rateLimitsByLimitId': {'a': {'limitId': 'CODEX', 'primary': WEEK}, 'b': {'primary': FIVE_HOURS}}},
    {'rateLimitsByLimitId': {'a': {'primary': WEEK}, 'b': {'primary': FIVE_HOURS}}},
    {'rateLimitsByLimitId': {'a': {'primary': WEEK}, 'b': {'primary': WEEK}}},
    {'rateLimitsByLimitId': {'a': {'primary': WEEK}}},
    {'rateLimitsByLimitId': {'a': 'no limit'}},
    {'rateLimits': {'primary': WEEK}, 'rateLimitsByLimitId': {'codex': {'primary': FIVE_HOURS}}},
    {'rate_limit': rollout(), 'rateLimits': {'primary': WEEK}},
    {'rate_limit': 'none', 'rateLimits': {'primary': WEEK}},
    # And what is no record of limits.
    {}, {'rate_limit': None}, {'rate_limits': rollout()}, {'rateLimits': 'none'}, [], 'a text', None, 7,
]


def run() -> dict:
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    root = pathlib.Path(tempfile.mkdtemp(prefix='clarp-codex-limits-vectors-', dir='/var/tmp'))
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import backend_usage_codex as codex
    from lib import backend_usage_store as store

    def read(payload):
        try:
            return {'snapshot': codex._codex_snapshot_from_payload(payload)}
        except RuntimeError as refused:
            return {'refused': str(refused)}
        except (ValueError, OverflowError, OSError, TypeError, AttributeError) as broke:
            # Not a refusal: the reader itself fails on this record. Written down as that.
            return {'breaks': type(broke).__name__}

    def window(given):
        try:
            return {'window': store._window_from_payload(given)}
        except (ValueError, OverflowError, OSError, TypeError, AttributeError) as broke:
            return {'breaks': type(broke).__name__}

    return {
        'windows': [{'given': given, **window(given)} for given in WINDOWS],
        'records': [{'given': payload, **read(payload)} for payload in PAYLOADS],
    }


def main() -> int:
    vectors = run()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(vectors, indent=1, ensure_ascii=False, sort_keys=True) + '\n')
    read = sum(1 for record in vectors['records'] if 'snapshot' in record)
    refused = sum(1 for record in vectors['records'] if 'refused' in record)
    windows = sum(1 for window in vectors['windows'] if window.get('window') is not None)
    breaks = sum(1 for one in [*vectors['windows'], *vectors['records']] if 'breaks' in one)
    print(f"{len(vectors['windows'])} windows ({windows} read), {len(vectors['records'])} records ({read} read, "
          f"{refused} refused), {breaks} on which the reader itself breaks -> {OUT.relative_to(REPO)}")
    return 0


if __name__ == '__main__':
    sys.exit(main())

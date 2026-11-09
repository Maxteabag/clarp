"""Pytest's own collection/outcome counts; no worker/teardown instrumentation."""
import json
from pathlib import Path

_state = {'workers': {}, 'collection': None, 'subtests': 0}


def pytest_addoption(parser):
    parser.addoption('--c937-counts', required=False)


def pytest_collection_finish(session):
    if not hasattr(session.config, 'workerinput') and session.items:
        _state['collection'] = len(session.items)


def pytest_xdist_node_collection_finished(node, ids):
    _state['workers'][node.gateway.id] = len(ids)


def pytest_runtest_logreport(report):
    if type(report).__name__ in ('SubtestReport', 'SubTestReport'):
        _state['subtests'] += 1


def pytest_sessionfinish(session, exitstatus):
    if hasattr(session.config, 'workerinput'):
        return
    filename = session.config.getoption('--c937-counts')
    if filename:
        counts = list(_state['workers'].values())
        collected = counts[0] if counts and len(set(counts)) == 1 else _state['collection']
        Path(filename).write_text(json.dumps({'collected': collected, 'worker_counts': counts,
                                             'pytest_exit': int(exitstatus), 'subtest_reports': _state['subtests']}))

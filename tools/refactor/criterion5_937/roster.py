"""Pure exact-node-cohort contract; no collection/import/test execution here."""
import hashlib
import json
from pathlib import PurePosixPath


def checked(nodes):
    if type(nodes) is not list or not nodes:
        raise ValueError('missing complete collection')
    for node in nodes:
        if type(node) is not str or not node or any(c in node for c in '\x00\r\n'):
            raise ValueError('invalid node identity')
        filename = PurePosixPath(node.split('::', 1)[0])
        if filename.is_absolute() or '..' in filename.parts or filename.parts[0] != 'tests':
            raise ValueError('node outside frozen test source')
    if len(set(nodes)) != len(nodes):
        raise ValueError('duplicate exact node identity')
    return sorted(nodes)


def freeze(a_nodes, b_nodes, *, a_exit, b_exit):
    if type(a_exit) is not int or type(b_exit) is not int or a_exit != 0 or b_exit != 0:
        raise ValueError('collection incomplete; no omission inferred')
    a, b = checked(a_nodes), checked(b_nodes)
    common = sorted(set(a) & set(b))
    if not common:
        raise ValueError('empty common cohort')
    raw = (json.dumps(common, ensure_ascii=True, separators=(',', ':')) + '\n').encode()
    return {'common': common, 'sha256': hashlib.sha256(raw).hexdigest(),
            'A_only': sorted(set(a) - set(b)), 'B_only': sorted(set(b) - set(a)),
            'A_count': len(a), 'B_count': len(b), 'common_count': len(common),
            'order': ['A', 'B', 'B', 'A'],
            'scope': 'exact common node IDs; no rename equivalence/exclusions hidden'}


def select(collected, frozen):
    actual = checked(collected)
    wanted = checked(frozen['common'])
    raw = (json.dumps(wanted, ensure_ascii=True, separators=(',', ':')) + '\n').encode()
    if hashlib.sha256(raw).hexdigest() != frozen['sha256']:
        raise ValueError('cohort changed')
    if not set(wanted).issubset(actual):
        raise ValueError('collected selection drift')
    return wanted, sorted(set(actual) - set(wanted))

"""LOWER PURE wire-format controls SOURCE ONLY, UNEXECUTED.

Supplied graph-shaped records, not API/SQLite/trace/producer/publication proofs.
"""
from .coverage_graph_wire import FORMAT, B_PIN, BINDINGS, encode_graph, decode_graph
from .coverage_copy_read import _same_capture
from .suite_evidence import ROLES


def run_graph_wire_controls():
    # Deliberately LOWER supplied records. Minimal role dictionaries test wire
    # preservation ONLY; an actual outer semantic graph validator must refuse
    # them as incomplete captures. No manufactured semantic acceptance field.
    value = {'format': FORMAT,
             'bindings': {k: B_PIN if k == 'source_pin' else 'a' * 64 for k in BINDINGS},
             'role_captures': {r: {'negative_arc': (-7, -1), 'typed': [True, 1, False, 0],
                                  'contexts': {'1': [], 'named': [[1, -1]]}} for r in ROLES},
             'reference_union': {'all_contexts': ['', 'named', '\U0001f600'], 'empty': []},
             'closed_aggregate_capture': {'not_semantically_qualified': True}}
    limits = {'nodes': 10000, 'depth': 64, 'string_characters': 10000, 'wire_bytes': 200000}
    raw = encode_graph(value, limits=limits)
    restored = decode_graph(raw, limits=limits)
    assert _same_capture(restored, value)
    typed = restored['role_captures'][ROLES[0]]['typed']
    assert [type(v) for v in typed] == [bool, int, bool, int]
    assert restored['role_captures'][ROLES[0]]['negative_arc'] == [-7, -1]
    assert set(restored['role_captures']) == set(ROLES)  # no hash/content dedup

    def refusal(call):
        try:
            call()
        except ValueError:
            return
        raise AssertionError('malformed/partial wire accepted')

    # Decode boundary, including a duplicate key with otherwise equal value.
    refusal(lambda: decode_graph(raw.replace(b'{', b'{"format":"' + FORMAT.encode() + b'",', 1), limits=limits))
    refusal(lambda: decode_graph(raw.replace(b'true', b'1.0', 1), limits=limits))
    refusal(lambda: decode_graph(raw.replace(b'true', b'NaN', 1), limits=limits))
    refusal(lambda: decode_graph(raw[:-1], limits=limits))
    bad = dict(value); bad['role_captures'] = dict(value['role_captures'])
    bad['role_captures'].pop(ROLES[-1])
    refusal(lambda: encode_graph(bad, limits=limits))
    bad = dict(value); bad['reference_union'] = {1: 'coerced-key'}
    refusal(lambda: encode_graph(bad, limits=limits))
    cycle = []; cycle.append(cycle)
    bad = dict(value); bad['reference_union'] = {'cycle': cycle}
    refusal(lambda: encode_graph(bad, limits=limits))
    for cap in ('nodes', 'depth', 'string_characters', 'wire_bytes'):
        reduced = dict(limits); reduced[cap] = 1
        refusal(lambda: encode_graph(value, limits=reduced))
    return {'scope': 'LOWER_PURE_TYPED_WIRE_ONLY_NOT_GRAPH_SEMANTICS_OR_PUBLICATION',
            'outcomes': 12, 'actual_API_or_persistence_or_coverage_accepted': False}

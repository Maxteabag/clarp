"""937 complete-attribution wire component SOURCE ONLY, UNEXECUTED.

No filesystem/publication, semantic graph validation, producer authentication or
resource-fit verdict. The outer owner must supply full already qualified graphs
and authenticate/persist all raw inputs/bindings. No JSON report policy change.
"""
import json

from .coverage_copy_read import _same_capture
from .suite_evidence import ROLES

FORMAT = 'criterion5-937-full-attribution-v1'
B_PIN = '0073254687d22ac5a6c1d01b8af29fa678dcf4a2'
FIELDS = {'format', 'bindings', 'role_captures', 'reference_union', 'closed_aggregate_capture'}
BINDINGS = {'source_pin', 'source_catalog_sha256', 'config_sha256',
            'coverage_archive_sha256', 'run_receipts_sha256'}
LIMITS = {'nodes', 'depth', 'string_characters', 'wire_bytes'}


def _check(value, limits):
    """Pre-encode conservative ASCII-byte bound; NOT heap/CPU/fit proof.

    Count repeated aliases each time; only an active ancestry cycle refuses.
    Exact builtin types prevent key coercion or bool/int interchange. String
    character count is not UTF-8 bytes; ensure_ascii output uses <=12 per code
    point (including surrogate-pair escaping). Caller caps need qualification.
    """
    if (type(limits) is not dict or set(limits) != LIMITS
            or any(type(n) is not int or n <= 0 for n in limits.values())):
        raise ValueError('all graph wire limits must be explicit positive integers')
    count, characters, bound, active = 0, 0, 0, set()

    def walk(row, depth):
        nonlocal count, characters, bound
        count += 1
        if count > limits['nodes'] or depth > limits['depth']:
            raise ValueError('graph node/depth bound; no partial evidence')
        kind = type(row)
        if kind is str:
            characters += len(row)
            if characters > limits['string_characters']:
                raise ValueError('graph identity/text bound; no truncation')
            bound += 2 + 12 * len(row)
        elif kind is bool:
            bound += 5
        elif row is None:
            bound += 4
        elif kind is int:
            # Includes signed native arc units and unsigned observed identities.
            # Inner semantic validators still enforce each field's own domain.
            if not -2**63 <= row < 2**64:
                raise ValueError('graph integer outside fixed wire domain')
            bound += 20
        elif kind in (dict, list, tuple):
            identity = id(row)
            if identity in active:
                raise ValueError('cyclic graph cannot be retained')
            active.add(identity)
            try:
                bound += 2 + 2 * len(row)  # delimiters plus conservative commas/colons
                if kind is dict:
                    for key, item in row.items():
                        if type(key) is not str:
                            raise ValueError('graph dictionary keys must be exact strings')
                        walk(key, depth + 1)
                        walk(item, depth + 1)
                else:
                    for item in row:
                        walk(item, depth + 1)
            finally:
                active.remove(identity)
        else:
            raise ValueError('unsupported graph primitive; no conversion')
        if bound > limits['wire_bytes']:
            raise ValueError('graph conservative wire bound; no partial evidence')

    walk(value, 1)
    return {'nodes': count, 'string_characters': characters,
            'conservative_ASCII_wire_bytes': bound}


def _envelope(value):
    if type(value) is not dict or set(value) != FIELDS or value['format'] != FORMAT:
        raise ValueError('wrong full-attribution envelope')
    bindings = value['bindings']
    if (type(bindings) is not dict or set(bindings) != BINDINGS
            or bindings['source_pin'] != B_PIN):
        raise ValueError('exact source/config/tool/run binding declarations required')
    for key in BINDINGS - {'source_pin'}:
        digest = bindings[key]
        if (type(digest) is not str or len(digest) != 64
                or any(c not in '0123456789abcdef' for c in digest)):
            raise ValueError('invalid declared artifact digest')
    if (type(value['role_captures']) is not dict or set(value['role_captures']) != set(ROLES)
            or any(type(row) is not dict for row in value['role_captures'].values())
            or type(value['reference_union']) is not dict
            or type(value['closed_aggregate_capture']) is not dict):
        raise ValueError('full nine-role/reference/aggregate records required')
    # Do not prune keys/fields, deduplicate same-hash roles or substitute totals.
    # Shape alone is NOT complete/valid graph semantics; existing outer graph
    # validators and actual producer/custody binding remain mandatory.


def encode_graph(value, *, limits):
    costs = _check(value, limits)
    _envelope(value)
    raw = json.dumps(value, ensure_ascii=True, allow_nan=False,
                     separators=(',', ':'), sort_keys=True).encode('ascii')
    if len(raw) > costs['conservative_ASCII_wire_bytes']:
        raise ValueError('graph encoder exceeded precomputed bound')
    decoded = decode_graph(raw, limits=limits)
    if not _same_capture(decoded, value):
        raise ValueError('typed full graph changed across wire')
    return raw  # bytes only; no publication/acceptance receipt


def decode_graph(raw, *, limits):
    # Validate caller limits before parsing. JSON parse materialization remains
    # a separately qualified cost; length/depth refusal is not a heap bound.
    _check(None, limits)
    if type(raw) is not bytes or not raw or len(raw) > limits['wire_bytes']:
        raise ValueError('missing/oversized graph bytes')

    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError('duplicate graph dictionary key')
            result[key] = item
        return result

    def unsupported(token):
        raise ValueError('noninteger or nonfinite graph number')

    value = json.loads(raw.decode('ascii'), object_pairs_hook=pairs,
                       parse_float=unsupported, parse_constant=unsupported)
    _check(value, limits)
    _envelope(value)
    return value  # untrusted typed records, NOT actual graph/custody acceptance

"""Controls over ONE actual native S INITIAL capture, SOURCE ONLY/UNEXECUTED.

Not a fixture generator or native persistence certificate. The qualified outer
owner must provide the authentic complete captured product after native setup.
No new seed/root/workload is launched here and no SQL or product code is called.
No command-line entrypoint; inclusion in an admitted setup selector remains owed.
"""
import hashlib
import json
import socket

from .seed_membership import audit_initial_membership
from .seed_paths import A_PIN


def run_membership_controls(products, *, root):
    if socket.gethostname() == 'laptopstudio':
        raise RuntimeError('BUILDBOX_ONLY; no local control authority')
    def digest():
        # Outside measured startup/RSS. Hashing/temporary bytes cost must be
        # charged and bounded by the complete package, not subtracted later.
        raw = json.dumps(products, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
        return hashlib.sha256(raw).hexdigest()
    original_hash = digest()
    # Do not fabricate a passing native output from the oracle's own recipe.
    positive = audit_initial_membership(products, root=root, size='S', pin=A_PIN, mode='initial_seed')
    results = [{'case': 'actual_native_S_initial', 'outcome': 'ACCEPTED', 'receipt': positive}]

    def locate(table, **values):
        body = products['tables'][table]
        offsets = {field: body['columns'].index(field) for field in values}
        found = [index for index, row in enumerate(body['rows'])
                 if all(row[offsets[field]] == ['text', wanted] for field, wanted in values.items())]
        if len(found) != 1:
            raise ValueError('control setup needs one actual native row, not invented input')
        return found[0]

    user = locate('messages', agent_id='synthetic-0', message_id='u-0-0')
    reply = locate('messages', agent_id='synthetic-0', trace_id='trace-0-0', role='assistant')
    future = locate('turns', agent_id='synthetic-0', trace_id='trace-0-1005')
    settled = locate('turns', agent_id='synthetic-0', trace_id='trace-0-0')
    summary = products['tables']['messages']['rows'][reply][
        products['tables']['messages']['columns'].index('turn_json')]
    cases = [
        ('trace_cross_owner', 'messages', reply, 'trace_id', ['text', 'trace-1-0'], 'trace belongs'),
        ('different_session', 'messages', reply, 'backend_session_id', ['text', 'other'], 'identity/session/path/slot'),
        ('different_source', 'messages', reply, 'source_file', ['text', '/other/source'], 'identity/session/path/slot'),
        ('different_slot', 'messages', reply, 'seq', ['integer', 2], 'identity/session/path/slot'),
        ('wrong_native_message_id', 'messages', reply, 'message_id', ['text', 'msg-wrong'], 'identity/session/path/slot'),
        ('wrong_durable_client_id', 'messages', user, 'message_id', ['text', 'u-wrong'], 'identity/session/path/slot'),
        ('turn_settled_at_changed', 'turns', settled, 'settled_at', ['integer', 1767225602001], 'turn membership/outcome/time'),
        ('turn_outcome_changed', 'turns', settled, 'outcome', ['text', 'interrupted'], 'turn membership/outcome/time'),
        ('request_summary_holder', 'messages', user, 'turn_json', list(summary), 'last-row summary'),
        ('missing_reply_summary', 'messages', reply, 'turn_json', ['null'], 'last-row summary'),
        ('changed_phase', 'messages', reply, 'phase', ['text', 'commentary'], 'payload/provenance/phase'),
        ('changed_origin', 'messages', reply, 'origin', ['text', 'peer'], 'payload/provenance/phase'),
        ('changed_native_clock', 'messages', reply, 'updated_at', ['integer', 1767225600001], 'summary/revision/time'),
        ('client_revision_changed', 'messages', user, 'revision', ['integer', 2], 'summary/revision/time'),
        ('wrong_storage_type', 'messages', reply, 'revision', ['real', '0x1p+0'], 'native text/integer/null'),
    ]
    for name, table, index, field, changed_cell, reason in cases:
        # Copy just the affected table's row vector and one typed row. Keep ALL
        # other native tables/fields present; never mutate the original capture.
        altered = dict(products, tables=dict(products['tables']))
        body = products['tables'][table]
        changed = dict(body, rows=list(body['rows']))
        changed['rows'][index] = [list(cell) for cell in body['rows'][index]]
        changed['rows'][index][body['columns'].index(field)] = changed_cell
        altered['tables'][table] = changed
        try:
            audit_initial_membership(altered, root=root, size='S', pin=A_PIN, mode='initial_seed')
        except ValueError as error:
            if reason not in str(error):
                raise AssertionError(name + ': wrong refusal/setup failure') from error
            results.append({'case': name, 'outcome': 'REFUSED_AT_CONTRACT', 'reason': str(error)})
        else:
            raise AssertionError(name + ': wrong native product accepted')

    altered = dict(products, tables=dict(products['tables']))
    body = products['tables']['turns']
    changed = dict(body, rows=list(body['rows']))
    changed['rows'].pop(future)
    altered['tables']['turns'] = changed
    try:
        audit_initial_membership(altered, root=root, size='S', pin=A_PIN, mode='initial_seed')
    except ValueError as error:
        if 'membership missing' not in str(error):
            raise AssertionError('missing_future_turn: wrong refusal/setup failure') from error
        results.append({'case': 'missing_future_turn', 'outcome': 'REFUSED_AT_CONTRACT', 'reason': str(error)})
    else:
        raise AssertionError('missing_future_turn accepted')
    if digest() != original_hash:
        raise AssertionError('control mutated original native capture')
    return {'cases': results, 'original_raw_sha256': original_hash,
            'scope': 'native snapshot consumer controls only; no replay/mutation/capacity/metric verdict'}

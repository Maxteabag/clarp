"""Plain INITIAL seed row/turn predicates, SOURCE ONLY / UNEXECUTED.

Not a parser, mutation recorder, schema/reference oracle or readiness verdict.
No SQL, product import, filesystem access or raw rewriting. Use only after the
outer owner authenticates the complete native A INITIAL snapshot and recipe.
Measured/post-ready/native-clock products MUST NOT use this INITIAL predicate.
"""
import hashlib
import json

from .recipe import APPEND_EXCHANGES, COUNTS, record
from .seed_paths import A_PIN, transcript_paths

EPOCH_MS = 1767225600000
TURN_FIELDS = ('agent_id', 'trace_id', 'source', 'started_at', 'settled_at', 'outcome')
MESSAGE_FIELDS = ('agent_id', 'message_id', 'backend_session_id', 'source_file', 'seq',
                  'role', 'timestamp', 'text', 'kind', 'tool_name', 'tools_json',
                  'display_cells_json', 'origin', 'sender_agent_id', 'trace_id',
                  'phase', 'turn_json', 'revision', 'updated_at', 'prompt_admission_id')


def _value(raw):
    if raw == ['null']:
        return None
    if type(raw) is list and len(raw) == 2:
        if raw[0] == 'text' and type(raw[1]) is str:
            return raw[1]
        if raw[0] == 'integer' and type(raw[1]) is int:
            return raw[1]
    raise ValueError('INITIAL membership requires exact native text/integer/null storage')


def _rows(products, table, fields, owners):
    body = products['tables'][table]
    columns = body['columns']
    if len(columns) != len(set(columns)) or not set(fields) <= set(columns):
        raise ValueError('missing/ambiguous INITIAL membership columns')
    offsets = {name: columns.index(name) for name in fields}
    for raw in body['rows']:
        if len(raw) != len(columns):
            raise ValueError('short INITIAL membership raw row')
        agent = _value(raw[offsets['agent_id']])
        if agent not in owners:
            continue
        yield {name: _value(raw[index]) for name, index in offsets.items()}


def _number(trace, owner_index, bound):
    prefix = f'trace-{owner_index}-'
    if type(trace) is not str or not trace.startswith(prefix):
        raise ValueError('INITIAL trace belongs to another owner/domain')
    suffix = trace[len(prefix):]
    if not suffix.isascii() or not suffix.isdecimal():
        raise ValueError('INITIAL trace ordinal is not literal decimal')
    # Bound the string before conversion; no arbitrary-size untrusted int.
    if len(suffix) > 5:
        raise ValueError('INITIAL trace ordinal exceeds frozen recipe')
    number = int(suffix)
    if str(number) != suffix or not 0 <= number < bound:
        raise ValueError('INITIAL trace ordinal exceeds frozen recipe')
    return number


def audit_initial_membership(products, *, root, size, pin, mode):
    """Refuse any mismatch to the declared plain A seed row/summary contract.

    The accepted result only establishes the listed predicates on supplied raw
    data. It cannot authenticate a producer or prove future A/B replay is inert.
    All additional raw fields/tables remain inputs to the separate full oracle.
    """
    if pin != A_PIN or mode != 'initial_seed' or size not in COUNTS:
        raise ValueError('INITIAL A seed membership operation required')
    if products['format'] != 'criterion5-937-typed-raw-v1' or products['capture']['exclusions'] != []:
        raise ValueError('complete unexcluded typed raw INITIAL snapshot required')
    paths = transcript_paths(root)
    owners = {agent: (index, COUNTS[size][index]) for index, agent in enumerate(paths)}
    turns = {agent: set() for agent in owners}
    messages = {agent: set() for agent in owners}
    identities = set()
    for row in _rows(products, 'turns', TURN_FIELDS, owners):
        agent = row['agent_id']
        index, count = owners[agent]
        number = _number(row['trace_id'], index, count + APPEND_EXCHANGES)
        start = EPOCH_MS + 60000 * number
        if (number in turns[agent] or row['source'] != 'local'
                or type(row['started_at']) is not int or row['started_at'] != start
                or type(row['settled_at']) is not int or row['settled_at'] != start + 2000
                or row['outcome'] != 'completed'):
            raise ValueError('INITIAL turn membership/outcome/time differs or duplicates')
        turns[agent].add(number)
    for row in _rows(products, 'messages', MESSAGE_FIELDS, owners):
        agent = row['agent_id']
        index, count = owners[agent]
        number = _number(row['trace_id'], index, count)
        role = row['role']
        if role not in ('user', 'assistant'):
            raise ValueError('INITIAL message role differs')
        key = (number, role)
        if key in messages[agent]:
            raise ValueError('duplicate INITIAL turn-role membership')
        is_client = role == 'user' and number % 10 == 0
        seq = -number - 1 if is_client else 2 * number + (role == 'assistant')
        backend = paths[agent]['backend_session_id']
        if is_client:
            message_id = f'u-{index}-{number}'
            source = 'client:synthetic'
        else:
            identity = f'{agent}\0{backend}\0{seq}'
            message_id = 'msg-' + hashlib.sha1(identity.encode('utf-8')).hexdigest()[:20]
            source = paths[agent]['source_file']
        if (type(row['seq']) is not int or row['seq'] != seq
                or row['message_id'] != message_id or message_id in identities
                or row['backend_session_id'] != backend or row['source_file'] != source):
            raise ValueError('INITIAL message identity/session/path/slot differs')
        recipe = record(agent, number, role)
        content = recipe['message']['content']
        expected_text = content if role == 'user' else content[0]['text']
        if (row['text'] != expected_text or row['timestamp'] != recipe['timestamp']
                or row['kind'] is not None or row['tool_name'] is not None
                or row['tools_json'] != '[]' or row['display_cells_json'] != '[]'
                or row['origin'] != 'user' or row['prompt_admission_id'] is not None
                or row['sender_agent_id'] != (None if is_client else '')
                or row['phase'] != ('final' if role == 'assistant' else None)):
            raise ValueError('INITIAL message payload/provenance/phase differs')
        summary = None
        if role == 'assistant':
            start = EPOCH_MS + 60000 * number
            summary = json.dumps({'turn_id': row['trace_id'], 'status': 'completed',
                                  'started_at_ms': start, 'ended_at_ms': start + 2000,
                                  'worked_ms': 2000, 'tool_count': 0}, separators=(',', ':'))
        if (row['turn_json'] != summary or type(row['revision']) is not int or row['revision'] < 1
                or type(row['updated_at']) is not int
                or row['updated_at'] != (1 if is_client else EPOCH_MS)
                or (is_client and row['revision'] != 1)):
            raise ValueError('INITIAL last-row summary/revision/time differs')
        messages[agent].add(key)
        identities.add(message_id)
    result = {}
    for agent, (_, count) in owners.items():
        if len(turns[agent]) != count + APPEND_EXCHANGES or len(messages[agent]) != 2 * count:
            raise ValueError('INITIAL turn/message membership missing')
        # With exact slot/role/time checks, each populated trace has one request
        # and its +2s reply; no equal timestamp+seq tie or extra session can
        # choose a different last summary row within this selected owner domain.
        result[agent] = {'turns': len(turns[agent]), 'message_rows': len(messages[agent]),
                         'durable_client_rows': count // 10, 'future_turns_without_messages': APPEND_EXCHANGES}
    return {'initial_membership': result,
            'scope': 'listed INITIAL A recipe predicates only; no replay/counter/head/CP/actor/resource proof'}

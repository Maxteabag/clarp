"""Literal populated-seed source-path evidence, SOURCE ONLY / UNEXECUTED.

Consumes already captured native rows; no SQL, filesystem resolution, product
import, mutation or normalization. This proves neither receipt authenticity nor
actor drain, schema fidelity, row equivalence or recorder capacity.
"""
from pathlib import PurePosixPath


def literal_root(value):
    if (type(value) is not str or not value.startswith('/') or value.startswith('//')
            or value == '/' or len(value) > 4096
            or '\x00' in value or str(PurePosixPath(value)) != value
            or any(part in ('.', '..') for part in value.split('/'))):
        raise ValueError('literal absolute private workspace required; no path normalization')
    return value


def transcript_paths(root):
    root = literal_root(root)
    return {f'synthetic-{i}': {
        'backend_session_id': f'bs-synthetic-{i}',
        'source_file': root + f'/home/.claude/projects/project/bs-synthetic-{i}.jsonl',
    } for i in range(3)}


def require_same_workspace(*, initial_root, operation_root):
    """Before an eventual root launch, reject even equivalent-looking aliases.

    A returned literal is NOT permission to reuse/remove/restore that workspace.
    Qualified prior root/actor exit and physical-copy ownership remain mandatory.
    """
    if literal_root(initial_root) != literal_root(operation_root):
        raise ValueError('initial construction and operation source paths differ')
    return transcript_paths(initial_root)


def _selected_rows(products, name, fields, owners):
    if (products['format'] != 'criterion5-937-typed-raw-v1'
            or products['capture']['exclusions'] != []):
        raise ValueError('unqualified raw snapshot format/exclusions')
    table = products['tables'][name]
    columns = table['columns']
    if (len(columns) != len(set(columns)) or not set(fields) <= set(columns)):
        raise ValueError('missing/ambiguous native source-path columns')
    offsets = {field: columns.index(field) for field in fields}
    for row in table['rows']:
        if len(row) != len(columns):
            raise ValueError('incomplete native raw row')
        agent = row[offsets['agent_id']]
        if (type(agent) is not list or len(agent) != 2 or agent[0] != 'text'
                or type(agent[1]) is not str):
            raise ValueError('native agent identity is not text')
        if agent[1] not in owners:
            continue
        selected = {}
        for field, index in offsets.items():
            value = row[index]
            if (type(value) is not list or len(value) != 2 or value[0] != 'text'
                    or type(value[1]) is not str):
                raise ValueError('source-path identity is not native text')
            selected[field] = value[1]
        yield selected


def audit_initial_paths(products, *, root):
    """Check actual native source_file identities for the three fixture owners.

    Durable client rows retain client:synthetic, rather than being rewritten to
    the transcript path. Counts are observations ONLY, never mutation bounds.
    All unrelated rows/tables remain in the raw product and the full oracle.
    """
    expected = transcript_paths(root)
    census = {agent: {'transcript_rows': 0, 'client_rows': 0, 'checkpoint_rows': 0}
              for agent in expected}
    for table in ('messages', 'transcript_import_checkpoints'):
        for row in _selected_rows(products, table,
                                  ('agent_id', 'backend_session_id', 'source_file'), expected):
            agent = row['agent_id']
            if agent not in expected:
                continue
            wanted = expected[agent]
            if row['backend_session_id'] != wanted['backend_session_id']:
                raise ValueError('fixture owner has an unexpected backend session')
            if row['source_file'] == wanted['source_file']:
                key = 'transcript_rows' if table == 'messages' else 'checkpoint_rows'
            elif table == 'messages' and row['source_file'] == 'client:synthetic':
                key = 'client_rows'
            else:
                raise ValueError('native fixture source_file differs from initial logical path')
            census[agent][key] += 1
    for counts in census.values():
        if counts['transcript_rows'] < 1 or counts['checkpoint_rows'] != 1:
            raise ValueError('populated native transcript/checkpoint path evidence missing/duplicate')
    return {'logical_root': literal_root(root), 'sources': expected, 'census': census,
            'scope': 'literal native path census only; NOT replay equality, drain or capacity'}

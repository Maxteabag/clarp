"""Literal populated-seed source-path evidence, SOURCE ONLY / UNEXECUTED.

Consumes already captured native rows; no SQL, filesystem resolution, product
import, mutation or normalization. This proves neither receipt authenticity nor
actor drain, schema fidelity, row equivalence or recorder capacity.
"""
from pathlib import PurePosixPath

A_PIN = '4f33cdc9287eafcddd0adfb17b51487c84ace28b'
# These are native source bindings, NOT a runtime loaded-code certificate.
A_POLICY_SOURCES = {
    'server/lib/config.py': '84f8c7a8a2b91095045da26919acc65e8ed4d78464bcb06939d3a9283b7850f2',
    'server/lib/transcript_incremental.py': '3c3c7e6d6591c2c1ffa00ee5b1edca6d43abab6a5c996e5e71249a4a098384f6',
}
DISABLED_A_INITIAL = 'A_INITIAL_DEFAULT_DISABLED'
REQUIRED_PUBLICATION = 'REQUIRED_NATIVE_PUBLICATION'


def a_initial_checkpoint_policy(*, pin, mode, root, source_files, native_imports):
    """Bind zero CPs to the specific fresh A/default-disabled native route.

    The constructor records actual config-file absence, native raw config and
    native settings on both sides of each real call, plus its untouched return.
    This validates that receipt's shape/route, not its producer authenticity.
    No expected count is inferred from the captured checkpoint rows.
    """
    if (pin != A_PIN or mode != 'initial_seed'
            or any(source_files.get(key) != value for key, value in A_POLICY_SOURCES.items())
            or type(native_imports) is not list or len(native_imports) != 3):
        raise ValueError('A INITIAL checkpoint policy source/operation binding differs')
    expected = transcript_paths(root)
    for agent, item in zip(expected, native_imports, strict=True):
        if (item['agent'] != agent or item['source_file'] != expected[agent]['source_file']
                or item['backend_session_id'] != expected[agent]['backend_session_id']
                or item['native_return']['shadow'] != 'disabled'):
            raise ValueError('A INITIAL native disabled import result missing/different')
        for position in ('before', 'after'):
            observed = item['native_shadow_' + position]
            if (observed['config_file'] != root + '/config/config.toml'
                    or observed['config_file_state'] != 'ABSENT'
                    or observed['raw_transcript_shadow'] != {}
                    or observed['native_settings']['enabled'] is not False):
                raise ValueError('A INITIAL native default-disabled configuration differs')
    return {'route': DISABLED_A_INITIAL, 'pin': pin, 'mode': mode,
            'source_hashes': dict(A_POLICY_SOURCES),
            'native_imports': native_imports,
            'scope': 'source/config/native-return binding; authenticity/lifecycle separate'}


def _checkpoint_count(policy):
    """Closed policy names: a missing CP never selects the zero-count route.

    REQUIRED_PUBLICATION is consumer support for a separately source-qualified
    operation, NOT a currently implemented producer/reference/launch route.
    The outer owner must supply its own source/config/native-publication proof.
    """
    if policy == DISABLED_A_INITIAL:
        return 0
    if policy == REQUIRED_PUBLICATION:
        return 1
    raise ValueError('explicit source-qualified checkpoint route required')


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


def audit_initial_paths(products, *, root, checkpoint_route):
    """Check actual native source_file identities for the three fixture owners.

    Durable client rows retain client:synthetic, rather than being rewritten to
    the transcript path. Counts are observations ONLY, never mutation bounds.
    All unrelated rows/tables remain in the raw product and the full oracle.
    """
    checkpoint_count = _checkpoint_count(checkpoint_route)
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
        if counts['transcript_rows'] < 1 or counts['checkpoint_rows'] != checkpoint_count:
            raise ValueError('populated native transcript/checkpoint path evidence missing/duplicate')
    return {'logical_root': literal_root(root), 'sources': expected, 'census': census,
            'checkpoint_route': checkpoint_route,
            'scope': 'literal native path census only; NOT replay equality, drain or capacity'}

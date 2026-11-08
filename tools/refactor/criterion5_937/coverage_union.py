"""Pure nine-role reference union SOURCE; no API/data/producer acceptance.

Consumes full unchanged run receipts and per-role capture observations. All
identity/custody fields remain declarations until authenticated by the outer
owner. No filesystem, coverage/SQL, remapping, report or actual update call.
"""
from pathlib import PurePosixPath

from .roster import checked
from .suite_evidence import ROLES, coverage_inputs
from .coverage_sets import check_limits


def reference_union(config_bytes, starts, finishes, *, actual_exit, outer_seconds,
                    cohort, captures, limits):
    check_limits(limits)
    candidates = coverage_inputs(config_bytes, starts, finishes, actual_exit=actual_exit,
                                 outer_seconds=outer_seconds, cohort=cohort)
    wanted = checked(cohort)
    # Coverage is the complete current-B suite, unlike the common timing
    # cohort. No accounted-but-hidden B-only deselection accepted here.
    for role in ROLES[1:]:
        if checked(finishes[role]['nodes_before']) != wanted or finishes[role]['deselected'] != []:
            raise ValueError('coverage not full declared B selection; retain omissions/PARTIAL')
    if type(captures) is not dict or set(captures) != set(ROLES):
        raise ValueError('exact nine role captures required, no missing/hash dedup')
    counts = {'pairs': 0, 'items': 0, 'total_name_bytes': 0}
    identities, paths, all_contexts, merged, inputs = set(), set(), set(), {}, []
    hash_views = {}

    def name(value, *, file=False):
        if type(value) is not str:
            raise ValueError('invalid exact identity type')
        n = len(value.encode('utf-8'))
        counts['total_name_bytes'] += n
        if n > limits['name_bytes'] or counts['total_name_bytes'] > limits['total_name_bytes']:
            raise ValueError('reference identity limit, no truncation')
        if file:
            p = PurePosixPath(value)
            if (not value or p.is_absolute() or str(p) != value or '..' in p.parts
                    or not p.parts or p.parts[0] != 'server' or p.suffix != '.py'):
                raise ValueError('unsupported/noncanonical file identity')
        return value

    def arcs(raw):
        if type(raw) not in (list, tuple):
            raise ValueError('invalid arc sequence')
        counts['items'] += len(raw)
        if counts['items'] > limits['items']:
            raise ValueError('reference item limit, no omitted arcs')
        result = set()
        for edge in raw:
            if (type(edge) not in (list, tuple) or len(edge) != 2
                    or any(type(n) is not int or not -2**63 <= n < 2**63 for n in edge)):
                raise ValueError('invalid exact typed native arc')
            edge = tuple(edge)
            if edge in result:
                raise ValueError('duplicate arc within one captured set')
            result.add(edge)
        return result

    for role in ROLES:
        candidate, capture = candidates['coverage_candidates'][role], captures[role]
        if (capture['qualification'] != 'SAME_VERSION_COPY_API_CAPTURE_ONLY_NOT_INPUT_ACCEPTANCE'
                or capture['source_producer_schema_valid_empty_or_union_accepted'] is not False):
            raise ValueError('wrong capture tier')
        receipt, snapshot = capture['read_close_receipt'], capture['accessor_sets']
        if (any(receipt.get(k) is not True for k in ('public_read_returned', 'public_close_returned',
                                                    'copy_bytes_unchanged', 'original_bytes_unchanged'))
                or receipt['artifact_sha256'] != candidate['sha256']
                or type(receipt['artifact_bytes']) is not int or receipt['artifact_bytes'] != candidate['bytes']
                or receipt['schema_valid_empty_union_or_producer_accepted'] is not False):
            raise ValueError('role/capture declaration mismatch')
        for kind in ('original_observation', 'copy_observation'):
            observed = receipt[kind]
            p = PurePosixPath(observed['path'])
            if (type(observed['path']) is not str or not p.is_absolute() or str(p) != observed['path']
                    or '..' in p.parts or observed['path'] in paths
                    or type(observed['dev']) is not int or observed['dev'] < 0
                    or type(observed['ino']) is not int or observed['ino'] <= 0
                    or type(observed['bytes']) is not int or observed['bytes'] != candidate['bytes']
                    or observed['sha256'] != candidate['sha256']):
                raise ValueError('changed/duplicate observed artifact declaration')
            if kind == 'original_observation' and observed['path'] != candidate['path']:
                raise ValueError('capture source not exact role candidate path')
            identity = (observed['dev'], observed['ino'])
            if identity in identities:
                raise ValueError('original/copy/cross-role inode alias declaration')
            identities.add(identity)
            paths.add(observed['path'])
        if (snapshot['mode'] != 'branch'
                or snapshot['qualification'] != 'ACCESSOR_SETS_ONLY_NOT_INPUT_OR_COVERAGE_ACCEPTANCE'
                or snapshot['valid_empty_or_source_producer_custody_accepted'] is not False):
            raise ValueError('nonbranch/unresolved mode cannot enter frozen B branch union')
        contexts = snapshot['contexts']
        if type(contexts) is not list or len(set(contexts)) != len(contexts):
            raise ValueError('invalid captured global contexts')
        context_set = {name(c) for c in contexts}
        all_contexts.update(context_set)
        if len(all_contexts) > limits['contexts'] or type(snapshot['files']) is not dict:
            raise ValueError('reference context limit or file map shape')
        role_files = {}
        for filename, row in snapshot['files'].items():
            name(filename, file=True)
            if row['tracer'] != '' or type(row['contexts']) is not dict:
                raise ValueError('unsupported tracer or context relation')
            values = arcs(row['values'])
            lines = {n for edge in values for n in edge if n > 0}
            if (type(row['lines']) not in (list, tuple)
                    or any(type(n) is not int or n <= 0 for n in row['lines'])
                    or len(set(row['lines'])) != len(row['lines']) or set(row['lines']) != lines):
                raise ValueError('captured line/arc mismatch')
            counts['items'] += len(row['lines'])
            if counts['items'] > limits['items']:
                raise ValueError('reference line item limit')
            keys = set(row['contexts'])
            exhaustive = any(a <= 0 and b <= 0 for a, b in values)
            if (type(row['exhaustive_context_query']) is not bool
                    or row['exhaustive_context_query'] != exhaustive
                    or not keys.issubset(context_set) or (exhaustive and keys != context_set)):
                raise ValueError('missing/extra captured attribution context')
            counts['pairs'] += len(keys)
            if counts['pairs'] > limits['pairs']:
                raise ValueError('reference pair limit, no dropped context')
            target = merged.setdefault(filename, {'values': set(), 'contexts': {}})
            if len(merged) > limits['files']:
                raise ValueError('reference file limit, no dropped file')
            local_union, role_contexts = set(), {}
            for context, raw in row['contexts'].items():
                current = arcs(raw)
                if not exhaustive and not current:
                    raise ValueError('unexpected empty sparse context relation')
                local_union.update(current)
                role_contexts[context] = tuple(sorted(current))
                target['contexts'].setdefault(context, set()).update(current)
            if local_union != values:
                raise ValueError('one role context union does not match unfiltered arcs')
            target['values'].update(values)
            role_files[filename] = {'values': tuple(sorted(values)), 'lines': tuple(sorted(lines)),
                                    'contexts': role_contexts, 'exhaustive': exhaustive}
        view = {'contexts': sorted(context_set), 'files': role_files}
        digest = candidate['sha256']
        if digest in hash_views and hash_views[digest] != view:
            raise ValueError('identical source bytes have incompatible accessor declarations')
        hash_views[digest] = view
        # Record every role even if its bytes/hash/data sets match another role.
        inputs.append({'role': role, **candidate})
    files = {}
    output_pairs, output_names, output_items = 0, sum(len(c.encode('utf-8')) for c in all_contexts), 0
    for file, row in sorted(merged.items()):
        exhaustive = any(a <= 0 and b <= 0 for a, b in row['values'])
        keys = all_contexts if exhaustive else row['contexts'].keys()
        output_pairs += len(keys)
        output_names += len(file.encode('utf-8')) + sum(len(c.encode('utf-8')) for c in keys)
        output_items += len(row['values']) + len({n for e in row['values'] for n in e if n > 0})
        output_items += sum(len(v) for v in row['contexts'].values())
        if (output_pairs > limits['pairs'] or output_names > limits['total_name_bytes']
                or output_items > limits['items']):
            raise ValueError('expanded reference output limit; no partial union')
        if exhaustive:
            # The aggregate accessor will query every global context for such
            # a file. Missing contributions are exact EMPTY sets, never arcs.
            for context in all_contexts:
                row['contexts'].setdefault(context, set())
        files[file] = {'tracer': '', 'values': tuple(sorted(row['values'])),
                       'lines': tuple(sorted({n for e in row['values'] for n in e if n > 0})),
                       'contexts': {c: tuple(sorted(a)) for c, a in sorted(row['contexts'].items())},
                       'exhaustive_context_query': exhaustive}
    return {'inputs': inputs, 'mode': 'branch', 'contexts': sorted(all_contexts), 'files': files,
            'cost_counts': counts, 'output_counts': {'pairs': output_pairs,
                                                     'name_bytes': output_names, 'items': output_items},
            'qualification': 'REFERENCE_UNION_ONLY_NOT_ACTUAL_UPDATE_OR_INPUT_ACCEPTANCE',
            'actual_data_source_producer_or_denominator_accepted': False,
            'scope': 'full declared B pytest selection only; actual identities/custody/data/provenance still external'}

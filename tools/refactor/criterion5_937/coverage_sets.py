"""Public coverage accessor-set reader SOURCE, UNEXECUTED and unwired.

The actual owner must supply a source-qualified, strictly validated private-copy
CoverageData within its read/close/custody envelope. This component does not
open, authenticate or close an input and cannot bless schema/empty/provenance.
"""
from pathlib import PurePosixPath

from .coverage_copy_read import _retain

LIMITS = {'files', 'contexts', 'pairs', 'items', 'name_bytes', 'total_name_bytes'}


def capture_sets(data, *, limits):
    if (type(limits) is not dict or set(limits) != LIMITS
            or any(type(v) is not int or v <= 0 for v in limits.values())):
        raise ValueError('all finite positive limits must be frozen by qualified owner')
    costs = {'pairs': 0, 'items': 0, 'total_name_bytes': 0}

    def name(value, *, file=False):
        if type(value) is not str:
            raise ValueError('non-string coverage identity')
        size = len(value.encode('utf-8'))
        if size > limits['name_bytes']:
            raise ValueError('coverage identity cap; no truncation')
        costs['total_name_bytes'] += size
        if costs['total_name_bytes'] > limits['total_name_bytes']:
            raise ValueError('total identity cap; no omitted identities')
        if file:
            p = PurePosixPath(value)
            if (not value or p.is_absolute() or str(p) != value or '..' in p.parts
                    or not p.parts or p.parts[0] != 'server' or p.suffix != '.py'):
                raise ValueError('noncanonical supported relative server Python path')
        return value

    def values(raw, *, arcs):
        if type(raw) is not list:
            raise ValueError('measured file missing or accessor shape changed')
        costs['items'] += len(raw)
        if costs['items'] > limits['items']:
            raise ValueError('accessor item cap; no partial accepted set')
        if arcs:
            if any(type(v) is not tuple or len(v) != 2
                   or any(type(n) is not int or not -2**63 <= n < 2**63 for n in v) for v in raw):
                raise ValueError('invalid typed native arc')
        elif any(type(v) is not int or v <= 0 for v in raw):
            raise ValueError('invalid typed executed line')
        if len(set(raw)) != len(raw):
            raise ValueError('duplicate accessor set member')
        return set(raw)

    def lines_of(arcs):
        return {n for edge in arcs for n in edge if n > 0}

    result, primary = None, None
    try:
        data.set_query_contexts(None)  # explicit reset, never a regex selection
        branch = data.has_arcs()
        if type(branch) is not bool:
            raise ValueError('unknown data mode')
        raw_files, raw_contexts = data.measured_files(), data.measured_contexts()
        if (type(raw_files) is not set or len(raw_files) > limits['files']
                or type(raw_contexts) is not set or len(raw_contexts) > limits['contexts']):
            raise ValueError('file/context inventory cap or shape changed')
        files = sorted(name(f, file=True) for f in raw_files)
        contexts = sorted(name(c) for c in raw_contexts)  # empty context name is valid
        captured = {}
        for file in files:
            data.set_query_contexts(None)
            tracer = data.file_tracer(file)
            if tracer != '':
                raise ValueError('missing/plugin tracer outside frozen no-plugin profile')
            all_values = values(data.arcs(file) if branch else data.lines(file), arcs=branch)
            lines = values(data.lines(file), arcs=False) if branch else all_values
            if branch and lines != lines_of(all_values):
                raise ValueError('unfiltered native arc/line union mismatch')
            index = data.contexts_by_lineno(file)
            if type(index) is not dict or set(index) != lines:
                raise ValueError('context index/line inventory mismatch')
            selected = set()
            for line, names in index.items():
                if (type(line) is not int or line <= 0 or type(names) is not list
                        or any(type(c) is not str for c in names)
                        or len(set(names)) != len(names) or not set(names).issubset(raw_contexts)):
                    raise ValueError('invalid context index relation')
                selected.update(names)
            # Pinned contexts_by_lineno emits positive endpoints ONLY. Any arc
            # without one may carry an otherwise invisible context: query ALL
            # declared contexts for that file. No inference from union counts.
            exhaustive = branch and any(a <= 0 and b <= 0 for a, b in all_values)
            if exhaustive:
                selected = set(contexts)
            if costs['pairs'] + len(selected) > limits['pairs']:
                raise ValueError('context/file pair cap; no dropped attribution')
            per_context, union, derived_index = {}, set(), {}
            for context in sorted(selected):
                costs['pairs'] += 1
                data.set_query_context(context)  # exact equality API, NOT regex
                current = values(data.arcs(file) if branch else data.lines(file), arcs=branch)
                per_context[context] = tuple(sorted(current))
                union.update(current)
                current_lines = lines_of(current) if branch else current
                for line in current_lines:
                    derived_index.setdefault(line, set()).add(context)
            if union != all_values or derived_index != {k: set(v) for k, v in index.items()}:
                raise ValueError('context attribution/independent unfiltered union mismatch')
            captured[file] = {'tracer': tracer, 'values': tuple(sorted(all_values)),
                              'lines': tuple(sorted(lines)), 'contexts': per_context,
                              'exhaustive_context_query': exhaustive}
        # False has_arcs with no positive units is not positive line-mode
        # evidence for an empty producer. Keep that classification unresolved.
        mode = ('branch' if branch else 'line' if any(r['values'] for r in captured.values())
                else 'UNRESOLVED_EMPTY_MODE')
        result = {'mode': mode, 'contexts': contexts,
                  'files': captured, 'accessor_cost_counts': costs,
                  'qualification': 'ACCESSOR_SETS_ONLY_NOT_INPUT_OR_COVERAGE_ACCEPTANCE',
                  'valid_empty_or_source_producer_custody_accepted': False}
    except BaseException as error:
        primary = error
    finally:
        try:
            # Required query-state restoration even on failure/control. Actual
            # connection close remains the outer API owner, not a background job.
            data.set_query_contexts(None)
        except BaseException as cleanup:
            primary = _retain(primary, cleanup)
    if primary is not None:
        raise primary
    return result

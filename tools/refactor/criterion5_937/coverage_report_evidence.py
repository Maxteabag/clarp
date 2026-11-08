"""Pure report/analysis consistency SOURCE only; no library or source execution.

Requires complete declared source inventory and supplied public analysis2 sets.
It does NOT authenticate those inputs or establish actual source/coverage/MET.
"""
from pathlib import PurePosixPath

B_PIN = '0073254687d22ac5a6c1d01b8af29fa678dcf4a2'
COUNTS = ('covered_lines', 'num_statements', 'missing_lines', 'excluded_lines',
          'num_branches', 'covered_branches', 'missing_branches', 'num_partial_branches')


def reconcile_report(report, analyses, *, inventory):
    if (inventory['pin'] != B_PIN or type(inventory['files']) is not dict
            or not inventory['files']):
        raise ValueError('complete frozen current B source inventory required')
    expected = set(inventory['files'])
    if (type(report['files']) is not dict or set(report['files']) != expected
            or type(analyses) is not dict or set(analyses) != expected):
        raise ValueError('missing/extra report or public-analysis source file; no disappearing denominator')
    meta = report['meta']
    if (type(meta['format']) is not int or meta['format'] != 3 or meta['version'] != '7.17.0'
            or meta['branch_coverage'] is not True or meta['show_contexts'] is not True):
        raise ValueError('wrong frozen report format/version/branch/context profile')
    totals = {k: 0 for k in COUNTS}
    files = {}
    for name, source in inventory['files'].items():
        p = PurePosixPath(name)
        if (not name or p.is_absolute() or str(p) != name or '..' in p.parts
                or not p.parts or p.parts[0] != 'server' or p.suffix != '.py'
                or type(source['physical_lines']) is not int or source['physical_lines'] < 0
                or type(source['sha256']) is not str or len(source['sha256']) != 64
                or any(c not in '0123456789abcdef' for c in source['sha256'])):
            raise ValueError('invalid source inventory declaration')

        def lines(raw):
            if (type(raw) is not list or any(type(n) is not int or not 0 < n <= source['physical_lines'] for n in raw)
                    or len(set(raw)) != len(raw)):
                raise ValueError('invalid typed duplicate-free source line units')
            return set(raw)

        # Supplied record must come from the supported analysis2 API on this
        # exact source under the same unfiltered report owner. Not AST counts.
        analysis, row = analyses[name], report['files'][name]
        if type(analysis['source_sha256']) is not str or analysis['source_sha256'] != source['sha256']:
            raise ValueError('analysis/source hash declaration mismatch')
        statements, excluded, missing = [lines(analysis[k]) for k in ('statements', 'excluded', 'missing')]
        executed = lines(row['executed_lines'])
        if (excluded & statements or not executed.issubset(statements) or not missing.issubset(statements)
                or missing != statements - executed
                or lines(row['missing_lines']) != missing or lines(row['excluded_lines']) != excluded):
            raise ValueError('JSON/public-analysis statement or exclusion mismatch')
        counts = row['summary']
        if any(type(counts[k]) is not int or counts[k] < 0 for k in COUNTS):
            raise ValueError('invalid exact integer summary count')
        if (counts['num_statements'] != len(statements) or counts['covered_lines'] != len(statements - missing)
                or counts['missing_lines'] != len(missing) or counts['excluded_lines'] != len(excluded)
                or counts['covered_lines'] + counts['missing_lines'] != counts['num_statements']
                or counts['covered_branches'] + counts['missing_branches'] != counts['num_branches']
                or counts['num_partial_branches'] > counts['missing_branches']):
            raise ValueError('summary disagrees with exact units')

        def branches(raw):
            if type(raw) is not list:
                raise ValueError('invalid branch unit list')
            result = set()
            for edge in raw:
                if (type(edge) not in (tuple, list) or len(edge) != 2
                        or type(edge[0]) is not int or not 0 < edge[0] <= source['physical_lines']
                        or type(edge[1]) is not int or edge[1] == 0 or abs(edge[1]) > source['physical_lines']):
                    raise ValueError('invalid typed branch unit')
                pair = tuple(edge)
                if pair in result:
                    raise ValueError('duplicate branch unit')
                result.add(pair)
            return result

        covered_branches = branches(row['executed_branches'])
        missing_branches = branches(row['missing_branches'])
        counted_executed = {e for e in covered_branches if e[1] not in excluded}
        # Public branch_stats uses the tool's no-branch exclusion semantics;
        # covered_branches can exceed literally observed executed branch arcs.
        stats = analysis['branch_stats']
        if type(stats) is not list:
            raise ValueError('public branch statistics required')
        by_line = {}
        for item in stats:
            if (type(item) is not dict or set(item) != {'line', 'total', 'taken'}
                    or any(type(item[k]) is not int for k in item)
                    or not 0 < item['line'] <= source['physical_lines']
                    or item['total'] <= 1 or not 0 <= item['taken'] <= item['total']
                    or item['line'] in by_line):
                raise ValueError('invalid typed public branch statistics')
            by_line[item['line']] = item
        if (covered_branches & missing_branches
                or any(e[0] not in by_line for e in covered_branches | missing_branches)
                or sum(v['total'] for v in stats) != counts['num_branches']
                or sum(v['taken'] for v in stats) != counts['covered_branches']
                or any(b in excluded for a, b in missing_branches)
                or len(missing_branches) != counts['missing_branches']
                or sum(1 for a, b in missing_branches if a not in missing) != counts['num_partial_branches']
                or any(sum(1 for a, b in missing_branches if a == line) != v['total'] - v['taken']
                       or sum(1 for a, b in counted_executed if a == line) > v['taken']
                       for line, v in by_line.items())):
            raise ValueError('JSON branch sets/public statistics disagree')
        if type(row['contexts']) is not dict:
            raise ValueError('missing explicit per-file line-context map')
        for line, contexts in row['contexts'].items():
            if (type(line) is not str or not line.isascii() or not line.isdecimal()
                    or str(int(line)) != line or not 0 < int(line) <= source['physical_lines']
                    or type(contexts) is not list or any(type(c) is not str for c in contexts)
                    or len(set(contexts)) != len(contexts)):
                raise ValueError('invalid exact source line/context identity')
        for key in COUNTS:
            totals[key] += counts[key]
        files[name] = {'source_sha256': source['sha256'], 'statement_units': sorted(statements),
                       'missing_statement_units': sorted(missing), 'excluded_units': sorted(excluded),
                       'counts': {k: counts[k] for k in COUNTS},
                       'statement_covered_percent': (None if not statements else
                                                     100 * len(statements - missing) / len(statements)),
                       'branch_covered_percent': (None if not counts['num_branches'] else
                                                  100 * counts['covered_branches'] / counts['num_branches']),
                       'empty_statement_denominator': not statements,
                       'literal_executed_branch_arcs': len(covered_branches),
                       'observed_arcs_to_excluded_destinations': len(covered_branches - counted_executed),
                       'tool_credited_unobserved_branches': counts['covered_branches'] - len(counted_executed)}
    if any(type(report['totals'][k]) is not int or report['totals'][k] != totals[k] for k in COUNTS):
        raise ValueError('whole report totals omit/double-count file units')
    return {'files': files, 'totals': totals,
            'source_file_count': len(expected),
            'qualification': 'DECLARED_REPORT_ANALYSIS_CONSISTENCY_ONLY_NOT_MEASUREMENT_ACCEPTANCE',
            'source_input_process_coverage_or_no_worse_accepted': False,
            'scope': 'current B declared Python denominator only; no historical mapping, branch baseline or child coverage'}

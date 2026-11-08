"""Pure finite-sample operational rules, not physical accuracy or leak proofs.

No process reads/signals, imports, server startup or measurements occur here.
Samplers and lifecycle adapters must be source-qualified separately.
"""
import math
import statistics

STEADY_SECONDS = 60.0
SAMPLE_OFFSETS = tuple(i + .5 for i in range(60))
MAX_SLOT_MIDPOINT_JITTER = .25
MAX_READ_BRACKET = .25


def finite(value):
    if type(value) not in (int, float):
        raise ValueError('untyped number')
    try:
        result = float(value)
    except OverflowError as error:
        raise ValueError('overflowed number') from error
    if not math.isfinite(result):
        raise ValueError('nonfinite number')
    return result


def ready_interval(spawn, probes):
    """Last negative→first positive bracket; no negative means spawn lower bound.

    Producer must independently prove actual source/instance readiness and pass
    only that predicate here; a socket/home-page alone is not positive readiness.
    """
    start = finite(spawn)
    previous = start
    lower = start
    for row in probes:
        lo, hi = finite(row['before']), finite(row['after'])
        if lo < previous or hi < lo or type(row['positive']) is not bool:
            raise ValueError('unordered/unknown readiness observation')
        if row['positive']:
            return {'lower_seconds': lower - start, 'upper_seconds': hi - start,
                    'positive_read_bracket_seconds': hi - lo,
                    'qualification': 'observed readiness bracket, not exact transition'}
        lower = lo
        previous = hi
    raise ValueError('readiness censored; no zero substituted')


def plateau(samples, *, start, kernel_page_bytes):
    """Exactly60 predeclared half-second-offset samples in ONE60s window.

    Each root sample needs an independently checked PID/start/boot/smaps rollup
    identity. No missing, replacement, adaptive-window or false coverage allowed.
    RSS/PSS reported units are NOT a bound on true kernel measurement accuracy.
    """
    origin, page = finite(start), finite(kernel_page_bytes)
    if type(kernel_page_bytes) is not int or kernel_page_bytes <= 0 or kernel_page_bytes & (kernel_page_bytes - 1):
        raise ValueError('invalid kernel page size')
    if page <= 0 or type(samples) is not list or len(samples) != 60:
        raise ValueError('missing steady samples or page size')
    times, rss, pss = [], [], []
    previous_end = origin
    for slot, row in enumerate(samples):
        before, after = finite(row['before']), finite(row['after'])
        mid = (before + after) / 2
        if row.get('slot') != slot or type(row.get('slot')) is not int:
            raise ValueError('missing/duplicate/out-of-order slot')
        if row.get('identity_valid') is not True or row.get('all_required_work_covered') is not True:
            raise ValueError('identity/work coverage unknown; not plateau')
        if row.get('pending_or_deferred') is not False:
            raise ValueError('deferred work is not steady')
        if (before < previous_end or after < before or after - before > MAX_READ_BRACKET
                or abs(mid - origin - SAMPLE_OFFSETS[slot]) > MAX_SLOT_MIDPOINT_JITTER
                or before < origin or after > origin + STEADY_SECONDS):
            raise ValueError('sample missing/late/overlapping/outside fixed steady interval')
        previous_end = after
        times.append(mid)
        for field, values in (('rss_bytes', rss), ('pss_bytes', pss)):
            value = finite(row[field])
            if value < 0:
                raise ValueError('negative memory')
            values.append(value)
    time_mean = statistics.mean(times)
    denominator = sum((t - time_mean) ** 2 for t in times)
    if denominator <= 0:
        raise ValueError('no observed sample span')
    summary = {}
    for name, values, quantum in [('RSS', rss, page), ('PSS', pss, 1024.0)]:
        mean = statistics.mean(values)
        slope = sum((t - time_mean) * (v - mean) for t, v in zip(times, values)) / denominator
        median_change = statistics.median(values[-10:]) - statistics.median(values[:10])
        growth = slope * (times[-1] - times[0])
        summary[name] = {'median_bytes': statistics.median(values),
                         'p95_nearest_rank_bytes': sorted(values)[math.ceil(.95 * len(values)) - 1],
                         'min_bytes': min(values), 'max_bytes': max(values),
                         'first10_last10_median_change_bytes': median_change,
                         'ols_bytes_per_second': slope, 'ols_growth_over_observed_span_bytes': growth,
                         'operational_quantum_bytes': quantum,
                         'operational_flat': median_change <= quantum and growth <= quantum}
    return {'sample_count': 60, 'planned_window_seconds': STEADY_SECONDS,
            'observed_sample_span_seconds': times[-1] - times[0], 'memory': summary,
            'operational_plateau': all(v['operational_flat'] for v in summary.values()),
            'qualification': 'strict finite operational censor; NOT measurement-error/leak proof'}


def no_worse(a_intervals, b_intervals):
    """Reviewed finite bound rule on supplied, independently justified bounds.

    This does NOT turn display quantization into a true memory-error interval or
    samples within a cell into independent repetitions.
    """
    if len(a_intervals) != 2 or len(b_intervals) != 2:
        raise ValueError('two independent valid cells per arm required')
    cells = [*a_intervals, *b_intervals]
    ids = [row.get('cell_id') for row in cells]
    if any(type(value) is not str or not value for value in ids) or len(set(ids)) != 4:
        raise ValueError('independent cell identities required')
    if (any(type(row.get('metric')) is not str or not row['metric'] or row.get('size') not in ('S', 'L') for row in cells)
            or len({row.get('metric') for row in cells}) != 1 or len({row.get('size') for row in cells}) != 1):
        raise ValueError('mixed primary metrics or data sizes')
    for row in cells:
        lo, hi = finite(row['lower']), finite(row['upper'])
        if lo < 0 or hi < lo or row.get('valid') is not True:
            raise ValueError('invalid/censored cell bound')
    best_a = min(finite(row['lower']) for row in a_intervals)
    worst_b = max(finite(row['upper']) for row in b_intervals)
    return {'observed_no_worse': worst_b <= best_a,
            'B_worst_upper_minus_A_best_lower': worst_b - best_a,
            'policy_margin_percent': 0,
            'qualification': 'observed finite bound rule only; no population guarantee'}

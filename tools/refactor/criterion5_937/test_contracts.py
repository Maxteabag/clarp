"""937 finite helper controls — SOURCE ONLY; execute only after admission.

No Clarp imports. Targets independent input bytes/selection/bounds/plateau rules;
these controls cannot certify native seed, actual roots or observer accuracy.
"""
import copy
import json

import pytest

from . import metrics, recipe, roster


def samples():
    return [{'slot': i, 'before': 100 + i + .45, 'after': 100 + i + .55,
             'identity_valid': True, 'all_required_work_covered': True,
             'pending_or_deferred': False, 'rss_bytes': 8 * 1024 * 1024,
             'pss_bytes': 4 * 1024 * 1024} for i in range(60)]


def test_frozen_native_record_shape_and_independent_byte_totals(tmp_path):
    for size, expected in [('S', 2686680), ('L', 15286680)]:
        output = tmp_path / size
        manifest = recipe.emit(output, size)
        assert sum(row['bytes'] for row in manifest['files']) == expected
        assert [row['exchanges'] for row in manifest['files']] == (
            [1000, 1000, 1000] if size == 'S' else [1000, 6000, 10000])
        line_count = 0
        for row in manifest['files']:
            with (output / row['file']).open('rb') as stream:
                line_count += sum(1 for _ in stream)
        assert line_count == (6000 if size == 'S' else 34000)
        for row in manifest['files']:
            with (output / row['file']).open('rb') as stream:
                first = json.loads(stream.readline())
                assistant = json.loads(stream.readline())
            assert first['type'] == 'user' and first['uuid'].endswith('-0-user')
            assert assistant['type'] == 'assistant'
            assert assistant['message']['content'][0]['text'].endswith('x' * 512)
        # Appends are a later EVENT-ALIGNED treatment, not pre-ready seed bytes.
        assert manifest['required_final_bytes'] == (2702880 if size == 'S' else 15302904)


def test_recipe_never_overwrites_or_accepts_unfrozen_size(tmp_path):
    old = tmp_path / 'already-owned'
    old.mkdir()
    marker = old / 'keep'
    marker.write_text('preserve')
    with pytest.raises(FileExistsError):
        recipe.emit(old, 'S')
    assert marker.read_text() == 'preserve'
    with pytest.raises(ValueError):
        recipe.emit(tmp_path / 'unfrozen', 'tiny-after-failure')
    assert not (tmp_path / 'unfrozen').exists()


def test_exact_node_cohort_preserves_all_omissions_and_refuses_drift():
    a = ['tests/test_one.py::test_a', 'tests/test_one.py::test_old']
    b = ['tests/test_one.py::test_new', 'tests/test_one.py::test_a']
    frozen = roster.freeze(a, b, a_exit=0, b_exit=0)
    assert frozen['common'] == ['tests/test_one.py::test_a']
    assert frozen['A_only'] == ['tests/test_one.py::test_old']
    assert frozen['B_only'] == ['tests/test_one.py::test_new']
    assert frozen['order'] == ['A', 'B', 'B', 'A']
    assert roster.select(b, frozen) == (['tests/test_one.py::test_a'], ['tests/test_one.py::test_new'])
    for wrong in (b + b, [], ['tests/test_one.py::test_missing']):
        with pytest.raises(ValueError):
            roster.select(wrong, frozen)
    changed = copy.deepcopy(frozen)
    changed['common'] = ['tests/test_one.py::test_new']
    with pytest.raises(ValueError, match='cohort changed'):
        roster.select(b, changed)
    for rc in (1, None, False):
        with pytest.raises(ValueError, match='incomplete'):
            roster.freeze(a, b, a_exit=rc, b_exit=0)


@pytest.mark.parametrize('nodes', [[], ['other/test_a.py::test_x'], ['../tests/test_a.py::test_x'],
                                  ['tests/test_a.py::test_x\nsecret'], [None],
                                  ['tests/test_a.py::test_x', 'tests/test_a.py::test_x']])
def test_invalid_collection_is_not_a_free_omission(nodes):
    with pytest.raises(ValueError):
        roster.checked(nodes)


def test_readiness_uses_probe_brackets_and_source_predicate_not_poll_nominal():
    probes = [{'before': 11, 'after': 11.2, 'positive': False},
              {'before': 12, 'after': 12.1, 'positive': True}]
    result = metrics.ready_interval(10, probes)
    assert result['lower_seconds'] == 1
    assert result['upper_seconds'] == pytest.approx(2.1)
    first = metrics.ready_interval(10, [{'before': 11, 'after': 11.5, 'positive': True}])
    assert first['lower_seconds'] == 0  # no negative => honest spawn lower bound
    with pytest.raises(ValueError, match='censored'):
        metrics.ready_interval(10, probes[:1])
    with pytest.raises(ValueError):
        metrics.ready_interval(10, [{'before': 11, 'after': 10, 'positive': True}])


def test_fixed_window_flat_rule_and_growth_are_independent_contracts():
    rows = samples()
    result = metrics.plateau(rows, start=100, kernel_page_bytes=4096)
    assert result['operational_plateau'] is True
    assert result['sample_count'] == 60 and result['observed_sample_span_seconds'] == 59
    assert result['memory']['RSS']['median_bytes'] == 8388608
    rising = samples()
    for i, row in enumerate(rising):
        row['rss_bytes'] += i * 4096
    assert metrics.plateau(rising, start=100, kernel_page_bytes=4096)['operational_plateau'] is False
    # End medians alone look flat; an internal sustained trend must still censor.
    hidden = samples()
    for row in hidden[10:30]:
        row['rss_bytes'] -= 65536
    for row in hidden[30:50]:
        row['rss_bytes'] += 65536
    result = metrics.plateau(hidden, start=100, kernel_page_bytes=4096)
    assert result['memory']['RSS']['first10_last10_median_change_bytes'] == 0
    assert result['memory']['RSS']['ols_growth_over_observed_span_bytes'] > 4096
    assert result['operational_plateau'] is False


@pytest.mark.parametrize('fault', ['missing', 'duplicate-slot', 'late', 'overlap', 'pending',
                                  'unknown-identity', 'not-covered', 'nonfinite'])
def test_no_adaptive_window_or_missing_work_can_be_plateau(fault):
    rows = samples()
    if fault == 'missing':
        rows.pop()
    elif fault == 'duplicate-slot':
        rows[1]['slot'] = 0
    elif fault == 'late':
        rows[1].update(before=102, after=102.1)
    elif fault == 'overlap':
        rows[1].update(before=100.5, after=100.6)
    elif fault == 'pending':
        rows[20]['pending_or_deferred'] = True
    elif fault == 'unknown-identity':
        rows[20]['identity_valid'] = None
    elif fault == 'not-covered':
        rows[20]['all_required_work_covered'] = False
    else:
        rows[20]['pss_bytes'] = float('nan')
    with pytest.raises(ValueError):
        metrics.plateau(rows, start=100, kernel_page_bytes=4096)


def cell(name, lo, hi):
    return {'cell_id': name, 'size': 'S', 'metric': 'runtime_boot',
            'lower': lo, 'upper': hi, 'valid': True}


def test_no_worse_conservative_zero_margin_not_average_or_repeated_samples():
    a = [cell('A1', 10, 12), cell('A2', 11, 13)]
    b = [cell('B1', 8, 9), cell('B2', 9, 10)]
    assert metrics.no_worse(a, b)['observed_no_worse'] is True
    b[1] = cell('B2', 10, 10.01)
    assert metrics.no_worse(a, b)['observed_no_worse'] is False  # no hidden tolerance
    with pytest.raises(ValueError, match='independent'):
        metrics.no_worse(a, [b[0], b[0]])
    with pytest.raises(ValueError, match='two independent'):
        metrics.no_worse(a[:1], b)
    b[1]['valid'] = False
    with pytest.raises(ValueError, match='censored'):
        metrics.no_worse(a, b)

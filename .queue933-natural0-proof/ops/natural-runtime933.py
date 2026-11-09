"""Natural handover composition only; frozen R4 algorithms/constants stay separate.

No service command, restart witness, planned-boot exclusion or sending authority.
The caller supplies actual observation inputs; missing joins remain UNKNOWN.
"""
from pathlib import Path
import json
import math
import os
import traceback


class NaturalUnknown(RuntimeError):
    pass


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def completion_kind(summary):
    value = summary.get('forward_restart_attempts')
    if type(value) is not int or value not in (0, 1):
        raise NaturalUnknown('completion_restart_count')
    return value


def current_native(unit, native_reader, unit_reader):
    result = native_reader(unit)
    def group(pid):
        path = Path('/proc') / str(pid) / 'cgroup'
        with path.open() as stream:
            text = stream.read(4097)
        groups = [line.split(':', 2)[2] for line in text.splitlines() if line.startswith('0::')]
        if len(text) > 4096 or len(groups) != 1:
            raise NaturalUnknown('current_cgroup_unknown')
        return groups[0]
    first = group(result['pid'])
    after_unit = unit_reader()
    after_native = native_reader(after_unit)
    if (any(unit.get(k) != after_unit.get(k) for k in ('ActiveState','MainPID','InvocationID','NRestarts'))
            or result != after_native or first != group(after_native['pid'])):
        raise NaturalUnknown('current_native_bracket_changed')
    result['cgroup'] = first
    return result


class NaturalRuntime:
    def __init__(self, target, host, host_native):
        self.target, self.host, self.host_native = target, host, host_native
        self.anchor = None
        self.native = None
        self.unit_identity = None
        self.last_refusal = None

    def unknown(self, reason, **facts):
        self.last_refusal = dict(state='UNKNOWN', reason=reason, **facts)
        return False

    def admit(self, observation, unit, native, row, now, mono, qualify, availability):
        """One positive target admission; never rebind a later foreign incarnation."""
        if self.anchor is not None:
            return True if self.native == native else self.unknown('admitted_incarnation_changed', expected=self.native, observed=native)
        if not isinstance(observation, dict) or observation.get('route') != 'joined_natural_status_attestation':
            return self.unknown('positive_natural_attestation_absent')
        joined = qualify(observation.get('join', {}), self.target)
        if joined is None:
            return self.unknown('natural_attestation_join_rejected')
        e = joined['join']; clock = e.get('healthy_clock', {})
        if not isinstance(clock, dict):
            return self.unknown('healthy_clock_shape')
        values = [clock.get(k) for k in ('wall_before', 'mono_before', 'wall_after', 'mono_after')]
        if not all(finite(x) for x in values + [now, mono, row.get('epoch'), row.get('mono')]):
            return self.unknown('healthy_clock_values')
        w0, m0, w1, m1 = values
        if (not 0 <= m1-m0 <= 30 or not 0 <= w1-w0 <= 30
                or abs((w1-w0)-(m1-m0)) > 5 or not 0 <= mono-m1 <= 30
                or not 0 <= now-w1 <= 30 or abs((now-w1)-(mono-m1)) > 5
                or not 0 <= now-row['epoch'] <= 30 or not 0 <= mono-row['mono'] <= 30):
            return self.unknown('healthy_clock_age_or_domain', observation_clock=clock, now=now, mono=mono)
        expected = dict(joined['native'], cgroup=e['cgroup_after'])
        if (native != expected or unit.get('ActiveState') != 'active'
                or str(native['pid']) != unit.get('MainPID')
                or native['invocation'] != unit.get('InvocationID')
                or row.get('host_code') != 200 or row.get('host_ready') is not True
                or row.get('runtime_available') is not True or row.get('runtime_id') != self.target
                or (row.get('pid'), row.get('process_start'), row.get('boot')) !=
                   tuple(self.host_native[k] for k in ('pid', 'start', 'boot'))):
            return self.unknown('current_native_or_Host_health_join', expected_native=expected, observed_native=native)
        # This method only binds the positively observed incarnation. It grants
        # NO supervised=True interval; ordinary availability applies immediately.
        availability.supervised_boot_complete(unit)
        self.native = dict(native)
        self.unit_identity = availability.expected
        self.anchor = dict(state='NATURAL_NO_EXPLICIT_RESTART', healthy_epoch=now,
                           healthy_mono=mono, observation_clock=clock, join=e, native=dict(native),
                           runtime=self.target, host=self.host, planned_exclusions=False)
        self.last_refusal = None
        return True

    def clock(self, now, mono):
        if self.anchor is None:
            raise NaturalUnknown('natural_positive_join_missing')
        a = self.anchor
        if (not finite(now) or not finite(mono) or now < a['healthy_epoch'] or mono < a['healthy_mono']
                or abs((now-a['healthy_epoch'])-(mono-a['healthy_mono'])) > 5):
            raise NaturalUnknown('natural_healthy_clock_unknown')
        return dict(a, elapsed_seconds=mono-a['healthy_mono'])

    def availability(self, guard, row, unit, mono, direct, unit_after, identity, *, supervised=False):
        # An unjoined active replacement is UNKNOWN, not an authorized natural
        # admission and not the old outside-supervision identity rollback.
        # Inactive units and all observations after a positive binding still use
        # the unchanged guard, including later foreign replacements.
        if self.anchor is None and unit.get('ActiveState') == 'active' and identity(unit) != guard.expected and not supervised:
            guard.unavailable_since = guard.last_sample = guard.confirmed_episode = None
            return dict(rollback=None, unknown='natural_replacement_not_positively_joined')
        return guard.sample(row, unit, mono, direct, unit_after, supervised=supervised)

    def finish_zero(self, summary, unit, native, row, now, mono, logs, tag, read_json, union):
        if completion_kind(summary) != 0:
            raise NaturalUnknown('not_natural_zero')
        anchor = self.clock(now, mono)
        if (not finite(row.get('epoch')) or not finite(row.get('mono'))
                or not 0 <= now-row['epoch'] <= 30 or not 0 <= mono-row['mono'] <= 30
                or row.get('host_code') != 200
                or (row.get('pid'), row.get('process_start'), row.get('boot')) !=
                   tuple(self.host_native[k] for k in ('pid', 'start', 'boot'))):
            raise NaturalUnknown('natural_completion_current_Host_sample')
        if native != self.native or (int(unit['MainPID']), unit['InvocationID'], int(unit['NRestarts'])) != self.unit_identity:
            raise NaturalUnknown('natural_completion_incarnation_changed')
        if (summary.get('result') != 'runtime_activation_health_pass' or summary.get('rolled_back') is not False
                or summary.get('tag') != tag or summary.get('release_id') != self.host
                or summary.get('runtime_id') != self.target or summary.get('runtime_invocation') != native['invocation']
                or row.get('host_ready') is not True or row.get('runtime_available') is not True
                or row.get('runtime_id') != self.target):
            raise NaturalUnknown('natural_completion_summary_health')
        logs = Path(logs)
        if os.path.lexists(logs / (tag+'.runtime830.restart.requested.json')):
            raise NaturalUnknown('request_conflicts_with_zero')
        refs = {}; data = {}
        for key, suffix in [('summary', '.summary.json'), ('healthy', '.healthy.json'), ('events', '.forward.interrupted.json'), ('helper', '.helper.json')]:
            value, binding = read_json(logs / (tag+'.runtime830'+suffix))
            data[key] = value; refs[key] = binding
        if data['summary'] != summary:
            raise NaturalUnknown('natural_summary_rebound')
        h = data['healthy']; q = h.get('probe', {})
        if (h.get('host_on_installed') is not True or h.get('runtime_on_installed') is not True
                or q.get('release_id') != self.host or q.get('runtime_release_id') != self.target
                or q.get('runtime_available') is not True or q.get('health_ready') is not True):
            raise NaturalUnknown('natural_healthy_receipt')
        helper = data['helper']; events = data['events']; counts = summary.get('resume_receipts', {})
        total = summary.get('interrupted_current_boot')
        if (type(helper.get('exit')) is not int or helper['exit'] != 0
                or not isinstance(events.get('events'), list) or len(events['events']) > 512
                or type(events.get('restart_interrupt_failures')) is not int or events['restart_interrupt_failures'] != 0
                or not finite(events.get('start_epoch')) or events['start_epoch'] > now
                or type(total) is not int or total < 0
                or any(type(counts.get(k)) is not int or counts[k] < 0 for k in ('accepted','skipped','unknown'))
                or counts['unknown'] != 0 or counts['accepted']+counts['skipped'] != total):
            raise NaturalUnknown('natural_helper_journal_accounting')
        for event in events['events']:
            stamp = event.get('journal_usec')
            fields = event.get('fields', {})
            if (event.get('invocation') != native['invocation'] or str(event.get('pid')) != str(native['pid'])
                    or not str(stamp).isdigit() or not events['start_epoch'] <= int(stamp)/1000000 <= now
                    or not isinstance(fields.get('agent'), str) or not fields['agent']
                    or not isinstance(fields.get('session'), str) or not fields['session']):
                raise NaturalUnknown('natural_forward_journal_identity')
        output = json.loads(helper['stdout'])
        expected = logs / 'handover-resumes' / native['invocation']
        if (output.get('invocation') != native['invocation'] or output.get('interrupted') != total
                or output.get('receipt_root') != str(expected) or summary.get('helper_receipt_root') != str(expected)
                or expected.resolve(strict=True) != expected
                or union.get('state') != 'UNION_RECONCILED_METADATA'
                or union.get('union', {}).get('accepted') != counts['accepted']
                or union.get('union', {}).get('skipped') != counts['skipped'] or union.get('union', {}).get('unknown') != []):
            raise NaturalUnknown('natural_helper_union_binding')
        return dict(state='NATURAL_ZERO_COMPLETED', healthy_clock=anchor, native=native,
                    receipts=refs, helper_union=union, supervised_registry_write=False,
                    planned_restart_exclusion=False, journal_scope='retained since830 supervisor start; helper union covers actual current boot')


def natural_partition(policy, intervals, since, until):
    """Same bounded raw producer contract; ALL natural-route events stay eligible."""
    raw = {}; eligible = {}; excluded = {}; faults = {}
    for category in (policy.REPORT_ONLY,) + policy.CATEGORIES:
        try:
            seen = set(); total = 0
            for interval in intervals:
                a, b = interval['since_epoch'], interval['until_epoch']
                if not finite(a) or not finite(b) or b < a or b > until:
                    raise NaturalUnknown('interval_schema')
                events = interval['raw_records'].get(category)
                legacy = interval.get('legacy_counts', {}).get(category)
                if not isinstance(events, list) or type(legacy) is not int or legacy < 0 or len(events) != legacy:
                    raise NaturalUnknown('raw_producer_count_mismatch')
                for event in events:
                    if (type(event.get('at_us')) is not int or event.get('code') != category
                            or not isinstance(event.get('id'), str) or not event['id']):
                        raise NaturalUnknown('event_schema')
                    lo, hi = ((int(a*1000)*1000, int(b*1000)*1000) if category == 'http_5xx' else (policy.usec(a), policy.usec(b)))
                    if not lo <= event['at_us'] < hi:
                        raise NaturalUnknown('event_outside_producer_interval')
                    if not policy.usec(since) <= event['at_us'] < policy.usec(until):
                        continue
                    if event['id'] in seen:
                        raise NaturalUnknown('event_duplicate')
                    seen.add(event['id']); total += 1
            raw[category] = total
            if category in policy.CATEGORIES:
                eligible[category] = total; excluded[category] = 0
        except (KeyError, TypeError, AttributeError, ValueError, NaturalUnknown) as error:
            raw[category] = None
            faults[category] = dict(type=type(error).__name__, message=str(error), traceback=traceback.format_exc())
            if category in policy.CATEGORIES:
                eligible[category] = excluded[category] = None
    return dict(raw=raw, eligible=eligible, excluded=excluded, capture_unknown=faults,
                boot_state='NATURAL_NO_EXPLICIT_RESTART', state='PARTITION_PARTIAL_UNKNOWN' if faults else 'NATURAL_NO_PLANNED_EXCLUSIONS',
                window_since=since, window_until=until)


def apply_errors(policy, natural, row, decision, intervals, baseline, now, mono):
    anchor = natural.clock(now, mono)
    result = dict(triggers=[k for k in decision['triggers'] if k not in policy.CATEGORIES+(policy.REPORT_ONLY,)],
                  unknown=[k for k in decision['unknown'] if k not in policy.CATEGORIES])
    switch = row.get('actual_Host_switch_epoch')
    if not finite(switch) or now < switch:
        raise NaturalUnknown('Host_switch_clock')
    parts = natural_partition(policy, intervals, max(switch, now-1800), now)
    mature = anchor['elapsed_seconds'] >= 2700
    for category in policy.CATEGORIES:
        count = parts['eligible'][category]; before = baseline.get('baseline_error_counts', {}).get(category)
        if type(count) is not int or count < 0:
            result['unknown'].append(category)
        elif mature:
            if type(before) is not int or before < 0:
                result['unknown'].append(category)
            elif count > policy.FLOORS[category] and count > 3*before:
                result['triggers'].append(category)
        elif count > policy.CATCHUP[category]:
            result['triggers'].append(category)
    row['error_policy927'] = dict(phase='MATURE_AFTER_HEALTHY45' if mature else 'CATCHUP_HEALTHY45',
                                 boot_window=anchor, partition=parts, db_locked='REPORT_ONLY_NEVER_ALONE_ROLLBACK',
                                 raw_legacy_post_counts=dict(row.get('post_error_counts', {})), baseline_fixed=True)
    return result


def apply_rss(natural, row, decision, guard):
    episode = guard.sample(row, row.get('mono'), False)
    result = dict(triggers=[x for x in decision['triggers'] if x != 'rss'], unknown=list(decision['unknown']))
    if episode['trigger']:
        result['triggers'].append('rss')
    if episode['state'] == 'UNKNOWN':
        result['unknown'].append('rss_continuity')
    row['rss_r2_911'] = dict(episode, route='NATURAL_NO_BOOT_SUPPRESSION')
    return result

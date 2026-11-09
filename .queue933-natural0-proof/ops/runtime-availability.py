"""892 runtime availability ownership guard. RPC timeout samples alone never roll back."""
import collections
import json
import re
import time


class RuntimeObservationUnknown(RuntimeError):
    pass


def identity(unit):
    if not isinstance(unit, dict) or unit.get('ActiveState') not in (
            'active', 'inactive', 'failed', 'activating', 'deactivating', 'reloading'):
        raise RuntimeObservationUnknown('runtime_unit_state_unknown')
    try:
        pid, restarts = int(unit['MainPID']), int(unit['NRestarts'])
        invocation = unit['InvocationID']
    except (KeyError, ValueError, TypeError) as error:
        raise RuntimeObservationUnknown('runtime_unit_identity_unknown') from error
    if pid < 0 or restarts < 0 or not isinstance(invocation, str):
        raise RuntimeObservationUnknown('runtime_unit_identity_unknown')
    if unit['ActiveState']=='active' and (pid<=0 or not re.fullmatch(r'[0-9a-f]{32}',invocation)):
        raise RuntimeObservationUnknown('runtime_active_unit_identity_malformed')
    if invocation and not re.fullmatch(r'[0-9a-f]{32}',invocation):
        raise RuntimeObservationUnknown('runtime_unit_invocation_malformed')
    return pid, invocation, restarts


class RuntimeAvailability:
    """Own the exact admitted unit identity; authorize only the proven803/830 new boot.

    Every sample must be available=False continuously with no >30s coverage gap.
    Missing/unknown observations break the episode. A120s episode admits exactly one
    direct20s confirmation for that episode. Failure is never inferred from UNKNOWN.
    """
    def __init__(self, expected_unit):
        self.expected = identity(expected_unit)
        self.last_observed = self.expected
        if expected_unit['ActiveState'] != 'active' or self.expected[0] <= 0 or not self.expected[1]:
            raise RuntimeObservationUnknown('runtime_initial_unit_not_active')
        self.unavailable_since = None
        self.last_sample = None
        self.confirmed_episode = None

    def supervised_boot_complete(self, expected_unit):
        new = identity(expected_unit)
        if expected_unit['ActiveState'] != 'active' or new[0] <= 0 or not new[1]:
            raise RuntimeObservationUnknown('runtime_supervised_unit_not_active')
        self.expected = new
        self.unavailable_since = self.last_sample = self.confirmed_episode = None

    def sample(self, sample, unit, now, direct_probe, unit_after, *, supervised=False):
        if supervised:
            # Caller830 retains its independent300s boot budget and BOTH rollback.
            self.unavailable_since = self.last_sample = self.confirmed_episode = None
            return {'rollback': None, 'scope': '803_830_supervised_boot_owns_availability'}
        try:
            observed = identity(unit)
        except Exception:
            self.unavailable_since = self.last_sample = self.confirmed_episode = None
            raise
        self.last_observed = observed
        if unit['ActiveState'] != 'active':
            return {'rollback': 'runtime_unit_not_active'}
        if observed != self.expected:
            return {'rollback': 'runtime_unit_identity_changed_outside_supervision'}
        value = sample.get('runtime_available')
        if value is True:
            self.unavailable_since = self.confirmed_episode = None
            self.last_sample = now
            return {'rollback': None, 'unavailable_seconds': 0}
        if value is not False:
            self.unavailable_since = self.confirmed_episode = None
            self.last_sample = now
            return {'rollback': None, 'unknown': 'runtime_rpc_sample_unknown'}
        if self.last_sample is None or now - self.last_sample > 30 or now < self.last_sample:
            self.unavailable_since = now
            self.confirmed_episode = None
        elif self.unavailable_since is None:
            self.unavailable_since = now
        self.last_sample = now
        elapsed = now - self.unavailable_since
        if elapsed < 120:
            return {'rollback': None, 'unavailable_seconds': elapsed}
        # One bounded direct confirmation on the same120s episode; a positive breaks it.
        self.confirmed_episode = self.unavailable_since
        try:
            confirmation = direct_probe(20.0)
            latest = unit_after()
            latest_id = identity(latest)
        except Exception:
            self.unavailable_since = self.last_sample = self.confirmed_episode = None
            raise
        self.last_observed = latest_id
        if latest['ActiveState'] != 'active':
            return {'rollback': 'runtime_unit_not_active_after_confirmation'}
        if latest_id != self.expected:
            return {'rollback': 'runtime_unit_identity_changed_during_confirmation'}
        if not isinstance(confirmation, dict):
            raise RuntimeObservationUnknown('runtime_confirmation_shape')
        if confirmation.get('late') is True:
            return {'rollback': 'runtime_continuously_unavailable120s_and_direct20s_failed',
                    'confirmation': 'BOUNDED_PROBE_DEADLINE', 'unavailable_seconds': elapsed}
        if confirmation.get('runtime_available') is True:
            self.unavailable_since = self.confirmed_episode = None
            self.last_sample = now
            return {'rollback': None, 'confirmation': 'AVAILABLE', 'unavailable_seconds': elapsed}
        if confirmation.get('runtime_available') is False:
            return {'rollback': 'runtime_continuously_unavailable120s_and_direct20s_failed',
                    'confirmation': 'UNAVAILABLE', 'unavailable_seconds': elapsed}
        # A transport timeout is a definite failed bounded probe, while malformed/unknown
        # status data never supplies positive or negative runtime availability evidence.
        if (confirmation.get('status_code') == 0 and
                str(confirmation.get('error', '')).startswith('TimeoutError')):
            return {'rollback': 'runtime_continuously_unavailable120s_and_direct20s_failed',
                    'confirmation': 'BOUNDED_PROBE_TIMEOUT', 'unavailable_seconds': elapsed}
        self.unavailable_since = self.confirmed_episode = None
        return {'rollback': None, 'unknown': 'runtime_confirmation_unknown'}


def rpc_timeout_count(run, since, until):
    result = run(['journalctl', '--user', '-u', 'clarp.service', '--since', '@'+str(since),
                  '--until', '@'+str(until), '--grep', 'runtimeStatusUnavailable',
                  '--output=json', '--no-pager', '-n', '1000'], 20)
    if result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip():
        return 0
    if result.returncode:
        raise RuntimeObservationUnknown('runtime_timeout_journal_unknown')
    rows = result.stdout.splitlines()
    if len(rows) >= 1000:
        raise RuntimeObservationUnknown('runtime_timeout_journal_truncated')
    import re
    count = 0
    for row in rows:
        message = json.loads(row).get('MESSAGE')
        if not isinstance(message, str):
            raise RuntimeObservationUnknown('runtime_timeout_journal_shape')
        count += bool(re.search(r'\bruntimeStatusUnavailable\b.*\breason=timeout\b', message))
    return count


def direct_runtime_probe(bridge, socket_path, budget, expected_release):
    """Native whole-call RPC with a validated envelope and positive IO cause.

    The exact native support remains unchanged. This read-only subclass validates
    response schema before native _status can conflate malformed/negative JSON
    with RuntimeUnavailable. Neither exception text nor private status escapes.
    """
    class ValidatedRuntimeClient(bridge.RuntimeClient):
        @staticmethod
        def _status(response):
            if not isinstance(response, dict) or type(response.get('ok')) is not bool:
                raise bridge.RuntimeProtocolError('runtime_status_envelope_unknown')
            if response['ok'] is not True:
                raise bridge.RuntimeProtocolError('runtime_application_negative_unproven')
            result = response.get('result')
            if (not isinstance(result, dict) or not isinstance(result.get('release_id'), str)
                    or not result['release_id']):
                raise bridge.RuntimeProtocolError('runtime_status_result_unknown')
            return result
    client = ValidatedRuntimeClient(socket_path, timeout=budget)
    try:
        status = client.status_within(budget)
    except bridge.RuntimeUnavailable as error:
        if isinstance(error.__cause__, OSError):
            return {'runtime_available': False, 'failure_class': type(error.__cause__).__name__}
        return {'unknown': 'runtime_unavailability_cause_unproven'}
    except bridge.RuntimeProtocolError:
        return {'unknown': 'runtime_protocol_unknown'}
    if status['release_id'] != expected_release:
        return {'unknown': 'runtime_direct_release_mismatch'}
    return {'runtime_available': True, 'runtime_release_id': status['release_id']}

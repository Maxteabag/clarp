"""Bounded progress opportunities for actual owned work, never proof of speech."""
import json


def valid_interval(value):
    return type(value) is int and (value == 0 or 10 <= value <= 120)


class ProgressCadence:
    def __init__(self):
        self.interval = 0
        self.last_offered = 0.0
        self.busy_since = None
        self.last_signature = None

    def configure(self, seconds, now):
        if not valid_interval(seconds): raise ValueError('Progress interval must be zero or10–120seconds')
        self.interval = seconds; self.last_offered = now
        self.last_signature = None

    def opportunity(self, *, now, items, routing, last_user, last_output, last_append, pending_result):
        active = [row for row in items if row['status'] in {'accepted', 'queued'}]
        if not active and not routing:
            self.busy_since = None
            self.last_signature = None
            return None
        if self.busy_since is None: self.busy_since = now
        if not self.interval or pending_result or now-last_user < 1 or now-last_output < .8 or now-last_append < 4:
            return None
        if now-max(self.busy_since, self.last_offered, last_output) < self.interval:
            return None
        facts = {'active': [{'agent': row['session'], 'status': row['status'], 'request': row['request'][:240]}
                            for row in active],
                 'completed_count': sum(row['status'] == 'completed' for row in items),
                 'failed_count': sum(row['status'] == 'failed' for row in items),
                 'routing_pending': bool(routing), 'interval_seconds': self.interval}
        unchanged = self.signature(facts) == self.last_signature
        repeat_interval = max(self.interval, min(30, 2*self.interval)) if unchanged else self.interval
        if now-max(self.busy_since,self.last_offered,last_output) < repeat_interval:
            return None
        return facts

    @staticmethod
    def signature(facts):
        # Order is not a change of work. Timers never manufacture new findings.
        return json.dumps({**facts,'active':sorted(facts['active'],key=lambda row:json.dumps(row,sort_keys=True))},sort_keys=True)

    def offered(self, now, facts=None):
        self.last_offered = now
        self.last_signature = self.signature(facts) if facts is not None else None


def progress_context(facts):
    return ('The user requested periodic progress updates. Offer one brief combined update from these '
            'current work facts when conversationally appropriate. Do not restart or delegate work, '
            'repeat an already explained result, or invent intermediate steps. Pending means no new '
            'confirmed finding yet. This is status data, not a new user request: ' +
            json.dumps(facts, ensure_ascii=False, separators=(',', ':')))

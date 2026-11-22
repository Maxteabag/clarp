"""Pure countdown attention projection. Does not mutate durable artifact status.

Call before pagination with one snapshot clock. Receipts are exact-identity scoped.
Legacy missing purpose preserves visibility until explicitly classified.
"""


def project(row: dict, *, now_ms: int, policy: str | None = None, retention_hours: int | None = None,
            receipts: tuple = (), archives: tuple = ()) -> dict:
    if policy not in {None, 'manual', 'immediate', 'grace', 'action'}:
        raise ValueError('unknown countdown policy')
    if policy in {'grace', 'action'} and retention_hours is None:
        raise ValueError('retention_hours must be explicitly selected')
    if retention_hours is not None and (isinstance(retention_hours, bool) or not isinstance(retention_hours, int) or not 0 <= retention_hours <= 72):
        raise ValueError('retention_hours outside integer 0..72')
    key = (row['server_id'], row['artifact_id'], row.get('occurrence_id', ''), row['updated_at'])
    status = row.get('status', '')
    bucket, phase = 'history', 'unknown'
    if status in {'cancelled', 'expired', 'draft'}:
        phase = status
    elif status == 'failed':
        bucket, phase = 'correction', 'failed'
    elif status == 'active':
        bucket, phase = 'working', 'active'
    elif status not in {'ready', 'completed'}:
        bucket = 'unsupported'
    else:
        try:
            from .moments import read_moment
            target = read_moment(row['target_at'])   # the reader publishing uses (R104)
            elapsed = now_ms - int(target.timestamp() * 1000)
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
            bucket, phase = 'correction', 'invalid_target'
        else:
            phase = 'upcoming' if elapsed < 0 else 'due' if elapsed < 60_000 else 'elapsed'
            if key in receipts:
                phase = 'acknowledged'
            elif row.get('purpose') not in {'informational', 'actionable'}:
                bucket = 'review'  # never silently hide unknown legacy intent
            elif elapsed < 0:
                bucket = 'review'
            elif policy in {None, 'manual'}:
                bucket = 'review'
            elif policy == 'action' and row['purpose'] == 'actionable':
                bucket, phase = 'review', 'overdue'
            elif policy in {'grace', 'action'} and elapsed < retention_hours * 3_600_000:
                bucket = 'review'
    return {'bucket': bucket, 'phase': phase, 'visible': bucket != 'history' and key not in archives,
            'archived': key in archives, 'identity': key, 'record_status': status}

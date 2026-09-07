"""Pure, per-task admission rules for the Janitor; no I/O or model calls."""

IDLE_STATES = frozenset(('idle', 'done', 'error', 'spawned'))
RECHECK_SECONDS = 30
SAME_TASK_DELAYS = (120, 300, 900)
ELIGIBILITY_OUTCOMES = frozenset(('protected', 'busy', 'unavailable'))


def record_review(previous, context, outcome, label, now):
    """Build a durable receipt; the guarded writer owns storing it atomically."""
    if outcome not in ('changed', 'same_task', 'insufficient_context', 'error') + tuple(ELIGIBILITY_OUTCOMES):
        raise ValueError('Unknown review outcome')
    previous = previous or {}
    same_task = previous.get('task_key') == context.get('task_key')
    previous_streak = int(previous.get('unchanged_streak', 0)) if same_task else 0
    streak = previous_streak
    if outcome == 'changed':
        streak, next_eligible = 0, now + 60
    elif outcome == 'same_task':
        streak = previous_streak + 1
        next_eligible = now + SAME_TASK_DELAYS[min(streak - 1, 2)]
    elif outcome == 'error' or outcome in ELIGIBILITY_OUTCOMES:
        next_eligible = now + RECHECK_SECONDS
    else:
        # No elapsed time alone can make the same sparse evidence sufficient.
        next_eligible = None
    return {
        'task_key': context.get('task_key'), 'change_key': context.get('change_key'),
        'fingerprint': context.get('fingerprint'), 'label': label or '',
        'outcome': outcome, 'unchanged_streak': streak,
        'reviewed_at': now, 'next_eligible_at': next_eligible,
    }


def admit(context, review, owned_label, now):
    """Return allow/drop/defer using evidence, ownership and this task's receipt."""
    context, review = context or {}, review or {}

    def result(decision, reason, eligible_at=None):
        return {'decision': decision, 'reason': reason, 'eligible_at': eligible_at}

    def deferred(reason):
        return result('defer', reason, now + RECHECK_SECONDS)

    if context.get('deleted_at') or context.get('archived_at'):
        return deferred('unavailable')
    if context.get('busy') or context.get('state') not in IDLE_STATES:
        return deferred('busy_or_unavailable')
    current = context.get('current_status') or ''
    if current and current != (owned_label or ''):
        return deferred('protected_label')
    if not context.get('has_context') or not context.get('fingerprint'):
        return deferred('missing_context')
    if not review:
        return result('allow', 'first_review')
    if review.get('outcome') in ELIGIBILITY_OUTCOMES:
        return result('allow', 'eligibility_restored')
    if review.get('task_key') != context.get('task_key'):
        return result('allow', 'new_task')
    if review.get('change_key') != context.get('change_key'):
        return result('allow', 'meaningful_change')
    same_fingerprint = review.get('fingerprint') == context['fingerprint']
    same_label = (review.get('label') or '') == current
    if review.get('outcome') == 'insufficient_context' and same_fingerprint:
        return deferred('insufficient_context_unchanged')
    # Retry failures on their own clock, never as successful unchanged reviews.
    if review.get('outcome') == 'error' and same_fingerprint:
        retry_at = review.get('next_eligible_at') or 0
        if now < retry_at:
            return result('defer', 'error_retry', retry_at)
        return result('allow', 'error_retry_due')
    if same_fingerprint and same_label:
        return result('drop', 'already_reviewed')
    # Once formerly insufficient evidence changes, let the model judge it again.
    if review.get('outcome') == 'insufficient_context':
        return result('allow', 'new_context')
    next_eligible = review.get('next_eligible_at') or 0
    if now < next_eligible:
        return result('defer', 'task_cooldown', next_eligible)
    return result('allow', 'changed_input')

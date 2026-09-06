import unittest

from lib.janitor_policy import admit, record_review


def context(**updates):
    value = dict(session='worker', state='done', current_status='Clarp audio',
                 task_key='audio-task', change_key='investigation', fingerprint='fp1', has_context=True)
    value.update(updates)
    return value


class ReviewPolicyTests(unittest.TestCase):
    def test_same_task_progression_and_error_does_not_increment(self):
        receipt = None
        for i, delay in enumerate((120, 300, 900, 900), start=1):
            receipt = record_review(receipt, context(), 'same_task', 'Clarp audio', 1000)
            self.assertEqual(receipt['next_eligible_at'], 1000 + delay)
            self.assertEqual(receipt['unchanged_streak'], i)
        error = record_review(receipt, context(), 'error', 'Clarp audio', 2000)
        self.assertEqual(error['unchanged_streak'], 4)
        self.assertEqual(error['next_eligible_at'], 2030)
        changed = record_review(error, context(), 'changed', 'iPhone sound', 2100)
        self.assertEqual(changed['unchanged_streak'], 0)
        self.assertEqual(changed['next_eligible_at'], 2160)

    def test_new_task_resets_unchanged_streak(self):
        previous = record_review(None, context(), 'same_task', 'Clarp audio', 0)
        updated = record_review(previous, context(task_key='new'), 'same_task', 'Clarp audio', 100)
        self.assertEqual(updated['unchanged_streak'], 1)

    def test_identical_input_is_dropped_even_after_deadline(self):
        review = record_review(None, context(), 'same_task', 'Clarp audio', 1000)
        for now in (1001, 5000):
            result = admit(context(), review, 'Clarp audio', now)
            self.assertEqual(result['decision'], 'drop')
            self.assertEqual(result['reason'], 'already_reviewed')

    def test_meaningful_task_and_phase_changes_bypass_cooldown(self):
        review = record_review(None, context(), 'same_task', 'Clarp audio', 1000)
        for changed in (context(task_key='other', fingerprint='fp2'),
                        context(change_key='shipping', fingerprint='fp2')):
            self.assertEqual(admit(changed, review, 'Clarp audio', 1001)['decision'], 'allow')

    def test_ordinary_changed_input_waits_for_task_cooldown(self):
        review = record_review(None, context(), 'same_task', 'Clarp audio', 1000)
        candidate = context(fingerprint='fp2')
        self.assertEqual(admit(candidate, review, 'Clarp audio', 1001),
                         dict(decision='defer', reason='task_cooldown', eligible_at=1120))
        self.assertEqual(admit(candidate, review, 'Clarp audio', 1120)['decision'], 'allow')

    def test_same_wording_on_new_task_or_phase_still_bypasses_suppression(self):
        for outcome in ('same_task', 'insufficient_context', 'error'):
            review = record_review(None, context(), outcome, 'Clarp audio', 1000)
            for candidate in (context(task_key='other-task'), context(change_key='new-step')):
                self.assertEqual(admit(candidate, review, 'Clarp audio', 1001)['decision'], 'allow')

    def test_protected_busy_missing_and_unavailable_do_not_admit(self):
        for candidate, label in ((context(state='thinking'), 'Clarp audio'),
                                 (context(busy=True), 'Clarp audio'),
                                 (context(deleted_at=1), 'Clarp audio'),
                                 (context(archived_at=1), 'Clarp audio'),
                                 (context(current_status='User caption'), 'Clarp audio'),
                                 (context(has_context=False), 'Clarp audio'),
                                 (context(state=None), 'Clarp audio')):
            self.assertEqual(admit(candidate, None, label, 1000)['decision'], 'defer')

    def test_insufficient_context_never_retries_just_on_time(self):
        review = record_review(None, context(), 'insufficient_context', 'Clarp audio', 1000)
        self.assertIsNone(review['next_eligible_at'])
        self.assertEqual(admit(context(), review, 'Clarp audio', 100000)['decision'], 'defer')
        self.assertEqual(admit(context(fingerprint='fp2'), review, 'Clarp audio', 1001)['decision'], 'allow')

    def test_error_uses_separate_retry_clock_for_identical_input(self):
        review = record_review(None, context(), 'error', 'Clarp audio', 1000)
        self.assertEqual(admit(context(), review, 'Clarp audio', 1029)['decision'], 'defer')
        self.assertEqual(admit(context(), review, 'Clarp audio', 1030)['decision'], 'allow')

    def test_cleared_label_is_not_identical_even_with_same_fingerprint(self):
        review = record_review(None, context(), 'changed', 'Clarp audio', 1000)
        result = admit(context(current_status=''), review, 'Clarp audio', 1060)
        self.assertEqual(result['decision'], 'allow')

    def test_eligibility_receipt_is_not_a_semantic_review(self):
        previous = record_review(None, context(), 'same_task', 'Clarp audio', 1000)
        for outcome in ('protected', 'busy', 'unavailable'):
            review = record_review(previous, context(), outcome, 'Clarp audio', 1100)
            self.assertEqual(review['unchanged_streak'], 1)
            self.assertEqual(admit(context(), review, 'Clarp audio', 1101)['decision'], 'allow')

    def test_bad_outcome_rejected(self):
        with self.assertRaises(ValueError):
            record_review(None, context(), 'invented', '', 0)


if __name__ == '__main__':
    unittest.main()

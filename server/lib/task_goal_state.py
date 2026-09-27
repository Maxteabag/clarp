"""Goal lifecycle facade; task_plans remains the sole table writer."""

from .task_plans import (
    _goal_check_revision as check_revision,
    _goal_save as _save,
    _goal_initialize as initialize,
    _goal_record as record,
    _goal_require_completion as require_completion,
    _goal_mutate as mutate,
)

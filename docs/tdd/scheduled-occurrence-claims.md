# Durable scheduled occurrence admission

Draft TDD slice from umbrella audit #25, rebased onto `eeecc72`.
These are reproductions and an implementation contract, **not completed fixes**.
Keep this PR draft until the implementation and its positive/negative controls pass.
Do not merge red tests into main.

## Accepted reproductions

- **R30** `tests/regression/test_restart_heartbeat_scheduler_failures.py::test_failed_scheduled_dispatch_remains_due_for_retry`: Schedule advance commits before dispatch; a known synchronous failure loses the occurrence. Preserve a retryable occurrence with a stable identity, and handle ambiguous runtime acknowledgement without duplicate execution.
- **R31** `tests/regression/test_restart_heartbeat_scheduler_failures.py::test_two_scheduler_workers_cannot_dispatch_the_same_due_run_twice`: Read then advance is not an atomic occurrence claim. Overlapping workers can both dispatch the same row. Use a durable conditional claim, not a process-local lock or a transaction held across backend I/O.
- **R32** `tests/regression/test_restart_heartbeat_scheduler_failures.py::test_schedule_disabled_after_due_read_cannot_still_dispatch`: A row disabled after the due snapshot is still dispatched. Revalidate the same occurrence and configuration when claiming; define the race boundary once admission has already happened.
- **R33** `tests/regression/test_restart_heartbeat_scheduler_failures.py::test_deleted_agent_cannot_leave_an_enabled_schedule_behind`: Soft deletion leaves recurring schedule rows enabled. These rows continue to be selected, and a reused session may receive stale work. Disable by stable agent identity at deletion.
- **R35** `tests/regression/test_restart_heartbeat_scheduler_failures.py::test_leap_day_schedule_searches_far_enough_to_find_its_next_run`: A valid February 29 schedule cannot find 2028 from March 2025 because the search stops at 366 days. Extend a bounded calendar search without changing existing day-of-week semantics accidentally.

## Implementation and verification

Use a conditional durable occurrence claim with a stable dispatch identity. Recheck enabled/configuration/agent identity at admission, without holding a SQLite write transaction during runtime I/O. Known dispatch failure must remain retryable; ambiguous acknowledgement must not double-execute. Disable schedules on agent deletion. Add bounded leap-day search without silently changing cron day semantics.

## Qualified or excluded claims

None in this slice.

# Maintenance attention

`lib.janitor_attention.reconcile()` runs after a Janitor runner tick and after
configuration/pause/remove commits. Call it outside an existing transaction. It
returns the number of changed artifacts; repeated polls do not rewrite stable
versions. The Host may use that count to refresh the existing attention view.
The function does not dispatch an agent, mark chat unread, send push, or speak.

Two consecutive terminal failed/error runs in the current configuration create
one `document` diagnostic artifact with status `failed`. The artifact and its
reference are deterministic per agent, configuration generation and failure
episode. A successful terminal `changed`/`same_task` run with an actual accepted
review ends the episode. Skipped or insufficient-context reviews break the
consecutive failure streak but do not assert successful recovery. Pause, removal
and configuration changes resolve the prior warning. A new episode can warn
again after two failures.

Additional failures update the same artifact's concise diagnostic and count.
Archived or discarded episodes remain dismissed even when more failures arrive;
their existing artifact markers/tombstones suppress recreation. Recovery can
complete archived warnings without restoring them. No extra schema is needed.

`pending(include_archived=False)` is a read-only projection for the existing
`GET /attention` response's `janitor_items` array:

```json
{
  "artifact_id": "janitor-alert-HASH", "agent_id": "STABLE_ID",
  "session": "sam", "agent_name": "Sam", "type": "document",
  "status": "failed", "title": "Sam needs attention",
  "summary": "Task labels need attention after 2 failed reviews. Review the configuration or pause maintenance.",
  "created_at": 1788700000000, "updated_at": 1788700000000,
  "archived_at": null, "reference_id": "janitor-failure:STABLE_ID:2:initial",
  "attention_kind": "janitor_failure", "configuration_generation": 2,
  "latest_run_id": "RUN_ID", "failure_count": 2
}
```

All timestamps use Unix milliseconds. Archive/discard uses the existing artifact
endpoint with `expected_updated_at`, not a decision revision. Native Updates
opens the dedicated Janitor inspector without selecting/focusing its chat.
The artifact payload also carries `attention_kind: janitor_failure`, so native
artifact lists can suppress duplicate cards. Ordinary decision contracts remain
unchanged. Verify with `tests/unit/test_janitor_attention.py` and the existing
artifact lifecycle tests.

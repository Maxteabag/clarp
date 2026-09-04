# HTTP authentication availability and error diagnostics

Draft TDD slice from umbrella audit #25, rebased onto `eeecc72`.
These are reproductions and an implementation contract, **not completed fixes**.
Keep this PR draft until the implementation and its positive/negative controls pass.
Do not merge red tests into main.

## Accepted reproductions

- **R04** `tests/integration/test_server_di.py::test_access_log_redacts_query_credentials`: Access logging interpolates raw request URLs, so query credentials enter logs. Redact credential values before logging; preserve useful route diagnostics.
- **R05** `tests/integration/test_server_di.py::test_transcription_provider_errors_remain_valid_json`: A provider error containing quotes/newlines produces invalid application/json. Serialize the response; keep the HTTP status and original error text.
- **R13** `tests/regression/test_database_availability_failures.py::test_paired_device_authentication_remains_readable_during_writer_contention`: Valid paired authentication requires an unrelated last-seen write and fails under a WAL writer lock. Make telemetry best-effort without hiding revoked tokens or unrelated database corruption.

## Implementation and verification

Make last-seen telemetry unable to block valid WAL-readable authentication; continue checking revocation. Redact URL credential values in access logs. Serialize provider errors with JSON encoding. Add tests for revoked/invalid credentials, writer contention, sensitive query values, and quote/newline errors.

## Qualified or excluded claims

None in this slice.

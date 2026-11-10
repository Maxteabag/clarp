# Hotseat status shared wire fixtures (clarp.hotseat-status/1)

One fixture set for both clients: Arnold's iOS decode/policy tests (matrix F1–F16 in `ARNOLD-AGREEMENT-v2.md`) and the Host builder tests (`tests/unit/test_hotseat_status.py::test_shared_wire_fixture`, with a byte-identical copy in `tests/fixtures/hotseat_status/` of the Host repo).

Contract: `/var/tmp/hotseat-ui/CONTRACT-v2-root.md`.
- Canonical contract `CONTRACT-v2-root.md` sha256 `0b8c0d2d2cd9b3a585e6a8b8b6cef4eeb8885551896389dc771e7c298ca5042b` (joint C/D draft 504d7a7e folded in). The wire schema and route in every file are exactly v2.
- That is the only citation. The pre-C/D v2 bytes are kept on disk as `CONTRACT-v2-root-before-CD-1e82defe.md`.

The files are static and hand-written: no code produced them. The two F11/F12 files share one hand-written template that differs only in mode, switched and kind, and the three S02 files share one that differs only in `served_at` and `stale`. All data is synthetic (`example.invalid`, `leak-example.test`, made-up tokens and paths).

## Format
- `<case>.input.json`: the synthetic Hotseat CLI input and Host events, applied in order to an empty store.
  - `{"op":"read","provider","received_at","payload"}`: the raw `hotseat list --json` (claude) or `hotseat codex --json` (codex) output that `read_accounts` parsed, passed to `record_read(provider, payload, received_at)`.
  - `{"op":"action","provider","decision","mode","switched","now","floor_pct","cadence_s","next_due_at"}`: the switcher decision after a successful read, passed to `record_action`.
  - `{"op":"failure","provider","error","now"}`: a failed read, passed to `record_failure`. The `error` value stands for the exception the Host maps: timeout = subprocess.TimeoutExpired, cli_failed = RuntimeError or OSError, parse_failed = ValueError, provider_error = any other exception.
  - `janitor_enabled`: whether an enabled `account-hotseat` subscriber exists at serve time. `served_at`: the serve clock in ms.
- `<case>.wire.json`: the exact `GET /hotseat/status` body. Compare after JSON parsing; key order and `1.0` against `1` do not matter.
- `F16-capabilities-absent.server-info.json`: a `/server-info` excerpt with no wire or input file.

Clock: T0 = 1800000000000 ms. Hotseat float seconds become epoch ms (1800010800.25 s becomes 1800010800250).

## Rules these fixtures follow (cited, not restated)
- Canonical contract sha256 `0b8c0d2d2cd9b3a585e6a8b8b6cef4eeb8885551896389dc771e7c298ca5042b` (joint C/D draft 504d7a7e folded in): sections C (action reason codes; no receipt for a read failure, a disabled Janitor or a begin/claim failure) and D (unique window kind, eligibility, precedence; on a full tie hotseat-cli comes before usage-worker, then label; the client rule for duplicates).
- Server-side decision (addendum on no_reset_reason): a Codex account with a stored no-reset reason stays exhausted and its windows are served with `resets_at: null`. The reason itself is internal (S05).

Where each rule is exercised:
| Rule | Fixtures | Host unit tests |
|---|---|---|
| C, six reason codes | F11, F12, S03a–c, F17 (client-negative) | `test_every_emitted_reason_code_is_in_the_set_of_six`, `test_actions_are_closed_codes` |
| C, read failure is not a receipt | F05, F19, S03c | `test_a_read_failure_after_a_real_action_leaves_actions_identical`, `test_read_failures_and_disabled_are_error_codes_never_receipts` |
| C, disabled or begin/claim failure records nothing | F10 | `test_a_disabled_switcher_records_nothing`, `test_a_begin_or_claim_failure_records_nothing` |
| D, identity binding | S01a, S01b | `test_last_good_values_follow_the_identity_not_the_alias`, `test_a_concurrent_switch_never_relabels_an_old_read` |
| D, precedence and dedup, partial | F06, F18, F20, F18c (client-negative) | `test_duplicate_and_unknown_windows_map_to_unique_known_kinds`, `test_a_newer_sample_of_the_same_identity_wins_over_a_more_constraining_older_one`, `test_a_newer_null_keeps_the_last_good_value_with_its_own_freshness`, `test_a_full_tie_keeps_hotseat_cli_before_usage_worker_then_label`, `test_the_full_precedence_chain_with_a_synthetic_usage_worker_sample` |
| D2, no observed_at | none (the Host always stamps samples) | `test_a_sample_without_observed_at_is_served_null_and_stale` |
| Window kinds and labels, unknown dropped | F14, S05 | `test_duplicate_and_unknown_windows_map_to_unique_known_kinds` |

## Arnold's matrix (F)
| File | Matrix | Shows |
|---|---|---|
| F01-happy | F1 | Claude ok with 2 accounts. The active 5h window has used 0.42, remaining 0.58; the 7d window has resets in the future. Fresh, with a noop receipt. |
| F02-exhausted | F2 | Claude limited/rejected and Codex blocked are exhausted, with remaining_ratio 0.0 given. |
| F03-unknown | F3 | A free-text error is unknown/provider_error with no windows. A row with no numbers is unknown, with windows whose ratios are null (never 0). |
| F04-auth-error-structured | F4 | auth_error only from the typed field `error_code:"auth_error"` (Claude and Codex). A "401 … sign in" message in free text stays unknown/provider_error. |
| F05-stale-last-good | F5 | A timeout after a good read: last-good values are stale, `last_good_observed_at` is set, error_code is `timeout`, and the earlier receipt is unchanged. |
| F06-partial | F6 | Claude partial because of a dropped duplicate `model:opus` window. Codex's read failed (cli_failed, its window stale) while Claude's windows keep their own freshness. |
| F07-empty-inventory | F7 | A successful read with zero accounts: inventory `empty`. |
| F08-missing-snapshot | F8 | No data yet: every provider is `unavailable`/`never` with observed_at null. Distinct from F7. |
| F09-muse-unavailable | F9 | Muse is always `unavailable`/`never`, never empty. |
| F10-janitor-disabled | F10 | The switcher is off: enabled=false, lane next_due_at null, samples stale, error_code `disabled`. There are no usage-worker windows: the UsageRefreshWorker cache is not bound to a Hotseat account, so the Host never emits one. |
| F11-notify-would-switch | F11 | lanes.mode `notify`; actions.kind `would_switch` from a to b, reason `below_floor`. "a" is still the active default. |
| F12-switch-receipt | F12 | actions.kind `switched` from a to b with at and reason `below_floor`. The read before it stays as it was observed. |
| F13-past-reset | F13 | A 5h reset passed with no newer sample: ratios are null and stale (unknown since the reset). The exhausted account turns unknown. |
| F14-model-window-label-and-unknown-family | F14 | `model:opus` "Opus weekly" and `model:spark` "Spark weekly" with server labels. The unknown families `claude-mystery-9` and `zebra weekly` are dropped by the Host. |
| F15-sentinels | F15 | The email alias arrives masked as `s…@l…`. Path, token-like and overlong aliases arrive as `account`, `account (2)` and `account (3)`. An email-like label, a token-like plan and a URL label become null. `preview`, `stderr`, `token`, `extra_usage_reason` and the error path never appear. The receipt aliases are masked. |
| F16-capabilities-absent.server-info.json | F16 | A capabilities example without `accounts_hotseat`: the client hides the section. |
| F18-duplicate-window-kind | F18 | Same-read ties: two Claude scoped windows map to model:opus and two Codex `codex primary weekly` windows map to 7d. The highest used_ratio is kept and partial=true. A newer sample of the same exact identity (a Claude read 60 s later) wins for 5h and model:opus even though the older values were more constraining, while its null 7d keeps the older 7d sample with that sample's own observed_at. Only one source can be emitted today (the usage-worker is unbound), so the newer sample comes from a newer hotseat-cli read; the rule does not depend on the source. |
| F19-read-failure-keeps-prior-action | F19 | A real switch receipt, then a parse_failed read. Actions and lane are byte-identical, read_state is `failed` with error_code `parse_failed`, and the retained samples are stale. |
| F20-stale-vs-fresh-duplicate | F20 | The eligible fresh samples (5h, and model:opus deduped in the same read, partial=true) win over the older, more constraining samples, which never displace them. A newer null 7d keeps the older 7d sample with its own observed_at, stale=true. |

## Client-negative files (the Host can never produce these)
These have no `.input.json` and are named `*-CLIENT-NEGATIVE.wire.json`, and each carries a top-level `client_negative` string saying what the client must do. They exist only for Arnold's fail-closed tests. The Host test checks that they are labelled, have no input, and really break the rule; it also checks that every positive wire file keeps the closed reason codes and unique kinds.
| File | Matrix | What the client must do |
|---|---|---|
| F17-unknown-reason-code-CLIENT-NEGATIVE.wire.json | F17 | **CLIENT-NEGATIVE.** `reason_code: "moon_phase"` is outside the six codes of contract C. Render "Reason not given" and never show the raw code. |
| F18c-duplicate-kind-CLIENT-NEGATIVE.wire.json | F18c | **CLIENT-NEGATIVE.** Account "a" has two `5h` windows with different values. Canonical client behaviour (`0b8c0d2d` D): suppress the conflicting values and show "Data inconsistent" for that account, never picking, overwriting or merging. Other accounts render normally. The server still guarantees uniqueness, so it never sends this. |

## Host matrix (S, server-only)
| File | Shows |
|---|---|
| S01a-concurrent-switch-identity | A read older than the stored one, which finished after the switch, is ignored. "a" stays active_default, and the switch receipt reads a→b. |
| S01b-concurrent-switch-identity-alias-rebound | Alias "a" now points at another account (other email) with an error, so it does not inherit the first account's last-good values. |
| S02a/b/c-freshness-boundaries-300-900 | Claude cadence 300 s gives a 600 s limit (the floor); Codex cadence 900 s gives 1800 s. a: both at their limit (fresh). b: Claude 1 ms past (stale), Codex fresh. c: both stale. |
| S03a-receipts-noop-exhausted | noop `already_ok`; exhausted `no_better_account`. |
| S03b-receipts-skipped | skipped `no_active_account`; skipped `quota_unknown`. |
| S03c-receipts-failed-and-read-failure | failed `switch_failed`. A later Codex read failure writes no receipt (the noop stays) and only read_state and error_code change. |
| S04-claude-mixed | Claude mix: ok, limited (exhausted), a free-text error with a path (unknown/provider_error, path dropped), an email alias masked, a token-like label rejected. |
| S05-codex-mixed | Codex mix: blocked is exhausted; `usable:false` without blocked is unknown; windows 7d, 5h and model:spark with resets. `no_reset_reason` "no credits" (known) and an unknown value (stored as `other`) keep a blocked account exhausted and serve its windows with `resets_at: null` (a reset that will not unblock it is never advertised; null means unknown). A blocked account without a reason turns unknown once its reset passes. The reason itself stays internal: v2 has no field for it. |

Integrity: `SHA256SUMS` covers every file here.

## Name lookup
The F01–F16 and S01–S05 names are final (addendum 3 rename, confirmed by Hobbles). This table maps the first-set numbering to the current files.

| First-set name | Current file(s) |
|---|---|
| 01-claude-mixed | S04-claude-mixed |
| 02-codex-mixed | S05-codex-mixed |
| 03-empty-inventory | F07-empty-inventory |
| 04-never-read | F08-missing-snapshot |
| 05-muse-unavailable | F09-muse-unavailable |
| 06-failure-after-good | F05-stale-last-good |
| 07-janitor-disabled | F10-janitor-disabled |
| 08a / 08b / 08c-freshness | S02a / S02b / S02c-freshness-boundaries-300-900 |
| 09-past-reset-no-fresh-sample | F13-past-reset |
| 10a-concurrent-switch | S01a-concurrent-switch-identity |
| 10b-identity-not-alias | S01b-concurrent-switch-identity-alias-rebound |
| 11a-actions-switched-would-switch | F11-notify-would-switch and F12-switch-receipt (split) |
| 11b-actions-noop-exhausted | S03a-receipts-noop-exhausted |
| 11c-actions-skipped | S03b-receipts-skipped |
| 11d-actions-failed-and-read-failure | S03c-receipts-failed-and-read-failure |
| 12-auth-error-structured | F04-auth-error-structured |

F19 is its own pair, F19-read-failure-keeps-prior-action: a real switched receipt, then a parse_failed read on the same provider, with the action and lane byte-identical. S03c shows the same rule only across providers (Claude's failed switch beside Codex's noop followed by a Codex read failure), so it does not replace F19.

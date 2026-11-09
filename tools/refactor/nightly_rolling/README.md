# Rolling nightly proposal

Authorized by lead Nov9; exact C source review and activation are still pending.
No workflow or product changes. Do not run the controller on Nov9 except the
reviewed `seed` operation after qualification; `daily` refuses that date.

The anchor is an explicit verified-main pointer, never inferred from today's
main. `seed` checks the exact retained Nov9 GREEN self-check, both archive/report
hashes, exact captain R1 entry hash, and all typed EACH186 rows before writing
an immutable seed receipt then atomic pointer. It preserves raw compare exit1.
The registered reverse integration delta is usable only for that run/pair.

`daily` takes one process lock, checks the pointer's immutable receipt, freezes
current main once, and exclusively creates the date directory. It dispatches
one unchanged replay.yml self-check and one compare from verified main to frozen
main through distinct immutable branches. Intent is persisted before mutation.
An existing date directory, uncertain dispatch or publication, missing artifacts,
unexplained difference, noise, error or invalid self-check stops advancement.
Recovery is manual readback of the same attempt, never a second dispatch.
Failed self-check evidence still permits the single compare, but never advancement.

Each compare difference requires an exact run/attempt/pair/raw-report entry in
registers/differences.md pointing to a hash-bound typed per-ID proof. All seven
fields must equal raw report records. Broad feature entries and ID-only lists
are insufficient. Missing new registration leaves the anchor unchanged for
review; retain raw1 separately from any later signed disposition. No automatic
retry after later registration: reconciliation must explicitly review and
publish the same retained attempt. No dropping scenarios or TP-N3 reclassification.

Success writes immutable accepted.json first, then replaces state.json under
flock, fsyncs its directory and reads back exact bytes. If replacement/fsync is
uncertain, stop and reconcile the receipt/pointer; never guess or rerun replay.

Proposed activation after C review:

1. Copy this exact committed directory to a fresh versioned private install;
   verify every file hash. Bind stable `nightly-rolling-source` to that version.
2. Verify existing owned unit/PID/source and retain its current script/logs.
   Stop ONLY `clarp-job-rf-harnessc-nightly-2-1793948105-10846.service`; verify it
   terminal. No Host/runtime restart. If ownership changed, stop activation.
3. Execute reviewed seed once; retain output/state/seed receipt readback.
4. Install the supplied user service/timer (no Persistent catch-up), enable
   timer only. Verify source hash, timer next wake Nov10 04:30 Europe/Oslo,
   no service started and no new Actions dispatch. Retain activation receipt.
5. Routine execution is an inspectable, cancellable Clarp job per night. Actions
   remains selected until explicit heavy-restoration confirmation and route
   update; no heavy status query/admission attempt here.

Open before activation: C exact source review, verified installation/seed/timer readbacks. Managed worker log/result notification
is authored and unexecuted; it delivers one nightly result, retains uncertain
delivery without resend, and checks job ownership before GitHub mutations. No
activation receipt exists yet. Syntax/AST checks alone do not qualify runtime.

## C findings repair (source only)

F1: retain the full actual job log and actual jobs/step receipt, not stdout ZIP
alone. Failure requires the sole runner error to be exact Process-completed
exit1; any other exit/error/traceback/noise is HOLD. Success requires completed
runner Replay-shell success (the runner's exit0 result) plus no error markers.
The unchanged pipefail workflow stays unchanged. Both require the native final
`run directory` line printed after `run.release` returns, so a report followed
by a cleanup exception cannot qualify. Step/run/log/artifact identity and hashes
are bound in evidence. No explicit exit0 marker is invented: GitHub does not emit
one for a successful shell step; this exit origin is named in the receipt.

F2: all ZIP/raw/job-log/registry/proof and metadata files use exclusive bounded
write+file fsync+directory fsync+exact readback. accepted.json binds the retained
inventory hash/size and is written only after all those publications succeed.
Seed copies its exact reviewed historical bytes/receipts into its own durable
inventory before pointer publication. Incomplete writes stop without retry.

F3: unknown ownership preserves the SAME handle and pauses with bounded backoff;
it never means cancellation or a new generation. Controller checks before each
remote mutation and immediately before pointer replacement. Report VERIFIED
requires actual controller rc0 AND exact pointer/accepted/inventory/full-evidence
agreement. A receipt alone, failed pointer publication or unreadable state is
HOLD/publication UNKNOWN. The worker retains its own log and single-result policy.
All repairs are source only; no replay/helper/seed/job/timer activation executed.

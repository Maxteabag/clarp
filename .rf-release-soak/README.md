# Big merge #1 synthetic release harness (800/803/805)

WIP, UNEXECUTED. No D_F pin, allowance installation, slot or release GO is implied.

The compute admission binds one immutable manifest, source script hashes, exact
D_F/base B exports, selected v8n2-r1 manifest cad642ea, owner rf-releasec-29c5 and
job rf-releasec-bigmerge1-soak-800. Maximum owned envelope 30000 seconds plus
660 seconds tail in the cumulative 70 NOK pool. No retry/resume or normal-pool spill.

Ordering: exact four-case canary, final clean D_F activation, six uninterrupted
hours (21600 seconds), then forward -> back -> D_F on the SAME soaked synthetic
DB. The six-hour monotonic clock includes planned container runtime drills;
it never pauses, shrinks or resumes across preemption. Incomplete job is no gate.

The native adapter derives from approved v8n2-r1 native canary, with isolated
owned-container naming/labels and retained OS image; Host observe-mode watch,
then ONE supervised container runtime restart with a strict 300-second target
health check. Boot failure uses the previous release's official interpreter/admin
rollback and restores its runtime. No real Host/config/database paths are mounted.

Rehearsal must exercise actual boundaries remotely and be explicitly labelled
short synthetic rehearsal, never substituted for the exact D_F six-hour receipt.
Live big-merge shipping is a separate release-owner operation under 803: after
Host watch PASS, one deliberate runtime restart is needed because D_F restructures
runtime code; target boot <=300s, preauthorized official BOTH rollback on failure,
then journal/current-admission deduplicated continues. Host-only N1/APNS do NOT restart.

Admission interop (805): real full runs require HEAVY_SOAK_ALLOWANCE=bigmerge1-800
and wrapper-provided HEAVY_SOAK_NONCE, HEAVY_SOAK_REMOTE_JOB_ID,
HEAVY_SOAK_BOOT_ID, HEAVY_SOAK_DEADLINE_UTC,
HEAVY_SOAK_REMAINING_OWNED_SECONDS. Boot ID must equal the current VM boot;
all deadlines are minimized and120 seconds of cleanup is inside the owned envelope.
Containers carry owner/job/nonce; cleanup verifies all three and removes the
immutable ID, retaining any same-name replacement. Every phase/terminal proof
binds nonce/job/boot. Exit0 alone is not a soak gate; the wrapper validates
canary-before, full21600 seconds, compatibility-after/sameDB and cleanup.

Source exports are streamed to private source tar files, never buffered as a
whole archive on the laptop. Packed capsules contain only Git source and the
approved cached bundle/tool files; database/env/transcript-shaped test fixtures
are omitted explicitly, with any data-shaped product path a refusal. Capsule
hashes and per-file hashes bind the remote payload, independent of carrierHEAD.

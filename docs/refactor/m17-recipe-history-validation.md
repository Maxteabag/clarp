# Merge-17: validate historical families with historical recipe data

Design before code, lead 696/700. Tool owner S4; reviewer B for infrastructure,
A3 for S4 preservation; S9 coordinated and will not edit these functions in
parallel; its historical-loader/provenance constraints are incorporated below. No split_apply or s3_apply semantic change is proposed.

Failure: prospective main 57d241ff, integration a7331017, common main
55c37eaf71522dac8e46ae233892f525a7731f15. Reviewed recipe 0743d86b names
_alive and two Claim helpers introduced only by Friday source ad454ee7.
Current pre-A reconstruction applies that recipe to 55's pre-stage-one source
and refuses _alive. The old integration is valid; relaxing definition or drift
checks would hide unrelated integration edits and is forbidden.

## Separate the two eras

Historical validation gets split/repoint/method recipe DATA from the exact
merge-base Git tree. It never reads historical metadata from integration,
untracked files or the merged checkout, and never executes historical tools.
Resolve the supplied baseline to a FULL immutable commit once before any
history read. Enumerate recipes from that exact Git tree with checked exit
status, canonical data paths and blob IDs; record the commit and recipe blobs
in diagnostics. A successful empty listing can prove directory/path absence;
a Git/object/read failure cannot. Source lookup must likewise distinguish a
proven absent path from a failed read, rather than returning None for both.
Historical split, method, collision and repoint checks run through the same
strict current parsers. Capture data only, never checkout/import old Python.
Use the current reviewed loaders/generators unchanged; incompatible historical
schema or a refused historical reconstruction remains a hard refusal.

Reconstruct baseline source with baseline recipes, in the same sequence as
before: split order, repoints, then successive applied method prefixes. Compare
facade, every part, every method source and mixin byte-for-byte against the old
integration. Retain partial-family, ordered-prefix, repoint alternative and
source-cycle/refusal checks. Already-applied baseline parts retain the existing
strict behavior; no general missing-definition or missing-file fallback.

Current pinned-main recipe data still controls new-generation enumeration,
cleanup and strict stages D/E on current main text. Historical metadata NEVER
feeds new generation. Current-only names can be absent from old source only
because the pinned old recipe did not request them; old required names remain
required. This is independent of whether a name happens to be called _alive.

Validate the union of old and new declared paths. A current-only generated path
must be absent in the old integration; its old source, if present, must equal
baseline. A first-introduction path with no old recipe cannot claim a generated
family already exists without a justified historical reconstruction. Missing
or ambiguous historical provenance refuses explicitly. No arbitrary generator
exception is converted into acceptance.

Historical generated paths must remain accounted for by current cleanup. For
this bounded repair, refuse removed split/method sources, removed output paths,
retired repoint targets or changed source/class/output topology unless there
is an explicit reviewed migration mechanism. Member-list additions/changes,
new groups/parts and first-introduction declarations are evaluated with their
respective eras, not a filtered current recipe. Preserve relative historical
order; unsupported topology changes fail closed rather than leaving stale
output. Reviewer B settles this migration boundary before implementation.

## One pre-A path and provenance

The shell's combined S9/method block should use this same reconstruction even
when methods are empty, provided the pinned-main orchestrator is available;
do not maintain a second current-recipe-only validator for split-only families.
Existing CLI/exit attribution, current-main tool/recipe checkout and checked
cleanup remain intact. Historical recipe capture is read-only Git data.

S9 owns split_apply. S4 will keep its strict names/refusals unchanged and ask
S9/B before any discovered change to that API boundary. No manual generated
source, patch whitelist, silent-except change, route or runtime change.

## Required evidence before a packet

B's acceptance cases are requested before implementation. A3's criteria read:
reviews/s4-baseline-validator-acceptance.md. Required matrix:

- Actual 55/a733/current0743 case: old tool RED on missing _alive; repaired
  validator GREEN on identical old bytes, current ad454/current recipe strict.
- New module function plus new/changed class method, generic split and non-split
  sources, and split-only entry with no methods. Historical members/source
  omissions still refuse; new generation cannot lose new names.
- Old facade/state/command/stop/mixin drift still refuses. Explicit Claim-body
  and state-binding controls fail for intended mismatch; extra/missing outputs,
  non-prefix methods, partial splits and repoints/unstarted splits stay covered.
- Integration-only/malformed recipes/tools and failed cleanup cannot influence
  either era. Missing/unreadable history cannot look like an empty recipe set.
- Retired/renamed topology refuses, or follows a separately B-approved rule;
  an applied group is not accepted merely by a matching inherited class name.

All execution on heavy-remote, true exit/log read. Reuse exact reviewed current
0743 generation/G0 evidence on unchanged source/recipe/tool inputs; do not repeat
completed G0/G1/G2 to restate it. New final tools need their applicable focused
proof and gates. Captain owns real merge17; no push before the 18:00 ship and
no hand-made resolution. This repair must be ready before Sunday 12:00 freeze.

## Next queue

After this blocker, S4 designs step 0 of explicit-lifecycles-plan.md with A2/B:
state set, legal-event table, generation-checked owner write hook, logged/counted
refusal, waits with named exits and table-walking test helper generalized from
turn_lifecycle.py. U09 stays parked behind these priorities; U10/TP-02 owner
agreement remains accepted/design-only; TP-N4 remains parked.

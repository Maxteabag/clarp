# Bounded fit proposal — requires exact B source review

Existing360s cap lacks a defensible fit rationale. Captain's same-rec7 request
for reconciliation is accepted; do not force that known-unjustified trial.
This proposal stays within the100min/about17NOK authority and preserves every
suite/test/pair/native workload/sample. No timing probe or prior execution of
this driver has occurred. Source approval alone is not measured fit assurance.

Retained exact-B1ca Tests42561521868 used CPython3.14.4, two xdist workers, four
eligible CPUs and16GB RAM. It passed the full suite in850s. Its independently
hash-verified JUnit testcase elapsed sum is1632.818s. The retained elapsed minus
sum/2 is33.591s. This confirms the old360s cap cannot be justified by citing that
run or silently assuming a Python3.12 speedup. These are retained observations,
not a new run, and the testcase sum is elapsed occupancy, not measured CPU work.

Propose FOUR pytest workers, fixed identically for A and B on the same runner,
and a480s per-suite refusal. This uses the four CPUs observed in the comparable
Actions machine without adding a job/machine or reducing scope. Refuse before
measurement if the actual common runner's CPU affinity is smaller than the
reviewed worker count. Keep all outcomes/JUnit/summary/xdist-error validity and
standing-failure sets unchanged; no skip, quarantine, retry or load-failure waiver.
Counts and A/B order/pins stay3 suite pairs and5 native pairs on the same machine.

Conditional scheduling worksheet:1632.818/4 +33.591 =441.7955s, leaving38.2045s
against480. This estimates only what redistributing the retained work could do
IF per-test elapsed and nonparallel overhead remained similar. It does NOT
establish four-worker speedup, CPU/IO contention, A timing, new interpreter
performance or eventual validity. In particular it is not a license to assume
that the3.12 environment makes tests faster. More workers may increase cost or
expose load-sensitive errors; those remain invalid and cannot be retried/waived.
The complete retained source references and arithmetic are in the adjacent
external FIT-REVISION-SOURCE-FACTS.json packet, with original artifact hashes.
Reviewer must assess this explicit conditional rationale before publication;
if insufficient, keep the existing request held and return a source disposition,
not a trial or declaration of platform impossibility.

Hard whole-job allocation (minutes): setup8 (checkout1+uv1+node1+prepare5),
experiment wrapper89, upload2, margin1 =100. The experiment wrapper is88m30s
plus30s kill bound; internal runner is88m/5280s. Runner phase ceilings:
6*480 suite =2880s;10*(runtime90+Host60+plateau60) =2100s; seed120s;
remaining180s for copies/HTTP overhead/hygiene/log/index/report and other runner
work. All share one deadline; no reset or extra tail. Faster phases leave their
unused allowance shared, never authorize exceeding the absolute cap. Setup is
bounded by its own step; failure leaves partial evidence rather than a blind
rerun. The timeout margins are refusal bounds, not a prediction or guarantee.

This changes only worker count/per-suite/internal/wrapper/setup-upload allocation
and the minimal CPU-affinity preflight, plus explanatory protocol. Method,
thresholds, baseline/target, fixture/corpora/tests, readiness/plateau/data collection,
cleanup and no-other-workflow policy remain. Exact B source review and a new
qualified driver handoff must replace bfab in SAMErec7; no second execution
request, publication, branch creation remotely, allocation or priority clearance.
933I4/DISK15/readylandings still precede937. If main moves, explicit source rebind
and review, never autonomous retarget. Final-main rerun remains separate and not
preapproved. Retired native/execnet/coverage certification stays retired. Herald
HOLD; no FUND/preempt/delay/corpus shrink/cap extension beyond reviewed authority.

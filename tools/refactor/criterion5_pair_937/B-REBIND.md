# Explicit SOURCE-ONLY B/main rebind after936 landing

Reviewed driver parent ff97e6d111d17dc4370bc1e71ac8e2000ceeef37.
Prior B a5fd6db9db6c12125e4d98389e22571f8f9bbb9a is stale and must refuse.
New proposed B 1ca5a5a69cba322e311968f20298c1db36f31eac is actualmain
at this readback. A4f33, sample counts, synthetic workload, validity predicates,
cleanup, dependency policy, budgets, thresholds and workflow remain unchanged.
Only plan.B and explanatory protocol binding change. B re-review required;
source_reviewed/execution_admitted remain false declarations. No autonomous
retarget or native retry/healing. If main moves again before setup, refuse.

This isolated driver derives from exactff97, not later nightly HEAD. All driver
code/workflow is unchanged from B356196 source closure. The actual product delta
from A5 consists of reconcile.py/turn_lifecycle.py and five test files for936.
It adds the busy-repair temporal grace/observation map and expected-state guard;
those real changes may affect suite/startup/memory and remain part of target B.
No product edits or copied metrics occur here. Frozen dependency files, existing
native entries, containment and replay source bindings compared byte-for-byte
in source facts; equality is not runtime compatibility or a measured result.

SAME captain request rec7c513cb879 stays open and ONEpush remains UNSPENT.
No new execution request, branch publication, dispatch, probe or job. After
exact B rebind review, captain alone reconciles replacement driver/branch with
freshmain/source/trigger/priorrun and LOWEST priority behind933I4, DISK15 and
readylandings. Source rebind does not clear that priority blocker. Inclusive
100min/about17NOK/noFUNDpreemptdelay; finalprogrammain rerun separatelyrequired,
not preapproved here. Retired native/execnet/coverage certification barriers
stay retired under lead rf1c82b9c1d49. Herald HOLD and historical raw evidence
stay unchanged. Nightly activation is separate and not part of this driver.

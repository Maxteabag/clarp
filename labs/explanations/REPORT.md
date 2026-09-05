# Professor Felix's explanation lab — first findings

**Verdict:** prioritize failure recovery and focused-activity scheduling. A warm
translator is promising, but changing prompts alone did not reliably improve
speed or meaning. No production changes were deployed.

## Scope and evidence

- 2026-09-05; base Host commit `ae266cfa13ed7331987416f0209718b89c09d1eb`.
- Codex CLI `0.153.4`; benchmark runner Python `3.14.2`.
- Model fixed at `gpt-5.3-codex-spark`, effort **low**, audience **Plain English**.
- **31 recorded benchmark calls**, plus one instruction-sanity check excluded
  from comparisons. All 31 benchmark responses passed ID/JSON/160-character
  checks. Format validity does **not** mean the explanations were good.
- Eight synthetic development cases and five transfer cases. The weather and
  retention cases were new; invoice counting also resembles a prompt example.
- No private chat history was used. Described commands were never executed.
  Native testing was headless, against a local fake HTTP server.
- [Raw samples](results/), [reproduction commands](README.md), and
  [machine-readable summary](results/summary.json) are retained beside this report.

## 1. Startup is not the main delay; teardown also costs time

| Current exec worker | Samples | Median total | Observed range |
|---|---:|---:|---:|
| One explanation | 3 | 3.37 s | 3.32–3.42 s |
| Four explanations together | 3 | 3.98 s | 3.93–5.58 s |
| Eight explanations together | 3 | 4.84 s | 4.23–6.68 s |

The normal eight-item baseline reported thread startup at a median **209 ms**.
Another median **512 ms** elapsed after `turn.completed` before the worker
returned. Most time was inside the model/API turn phase, not process launch.
These events do not separate provider queueing, networking and model computation.

The worker additionally has a 180 ms batching delay; the desktop polls every
600 ms. Those are outside the isolated exec timings above. Push notifications
could remove polling delay, but cannot remove a multi-second model turn.

Batching helps **throughput**, not necessarily the first visible answer. All
eight current explanations wait for the completed batch; dividing 4.84 seconds
by eight would misrepresent the user's latency. Urgent single rows and historical
backfill probably deserve different batch policies.

## 2. A warm process works, but the comparison needs controls

The prototype uses one private app-server and a **new ephemeral conversation
for every sample**, so no previous conversation answers enter the next prompt.
It follows the official [Codex app-server protocol](https://learn.chatgpt.com/docs/app-server),
with read-only sandboxing, disabled action integrations, and owned-process cleanup.

| Eight-item baseline prompt | Samples | Median, including startup when needed |
|---|---:|---:|
| Current production-shaped exec | 3 | 4.84 s |
| Exec with a private config profile | 3 | 5.53 s |
| Fresh app-server for each sample | 3 | 3.74 s |
| One app-server reused, fresh threads | 3 | 3.38 s |

The two reused-thread turns took 3.38 and 3.31 seconds; creating their new threads
took only 7–9 ms. The first process also paid initialization and first-thread costs.

**Important confound:** normal exec used about 5,904 input tokens, private-profile
exec about 3,642, and app-server about 3,505. Private configuration/instruction
delivery changes context size; the apparent improvement cannot all be attributed
to keeping a process alive. The fresh-vs-reused RPC comparison controls that
better, but has only three samples and uncontrolled provider latency.

Conclusion: worth a limited prototype rollout, not a guaranteed percentage win.
The local model catalog confirms **low is already Spark's lowest supported
effort**; I did not silently substitute an unsupported lower setting.

## 3. Focused activity can get stuck behind old history

Using the **real shipping scheduler** with a fake 20 ms translator:

1. Enqueue 64 historical activities.
2. Hold the first batch in progress, then enqueue a newly focused live activity.
3. Release the running batch and observe its scheduling position.

| Policy | Batch containing the live activity | Synthetic wait |
|---|---:|---:|
| Current FIFO queue | 9th | 343 ms |
| Lab-only promotion of focused activity | 2nd | 61 ms |

The meaningful result is **ninth batch versus second**, not a production claim
of 61 ms. Real model batches take seconds. The prototype leaves the current
batch alone. Production needs aging/fairness, cross-audience handling and bounded
queues before promotion becomes a real scheduling policy.

Completed-cache lookups in this synthetic probe were about 0.01–0.02 ms without
HTTP. This is an answer-cache hit, not a fresh inference or provider prefix cache.

## 4. The C++ failure latch is real and reproducible

A headless test used the **actual ApiClient and ToolNarrator**:

- The fake Host returned HTTP 503 once.
- The narrator became unavailable.
- Requesting a different activity produced **no second HTTP request**.
- Explicitly resetting the narrator restored requests and a successful answer.

That can feel like "slow explanations" when narration is actually stopped.
The characterization test is retained as
`labTransientHostFailureStopsNewExplanationsUntilReset`; it demonstrates a defect,
not behavior to preserve.

Recommended fix: retry transient transport failures with bounded backoff while
preserving completed answers. Do not loop on authentication failures or unsupported
Host capabilities. Keep Host/session/audience generation checks so old replies
cannot reappear after navigation. Neither the fix nor a retry policy was deployed.

## 5. Better-looking prompts did not reliably generalize

On the same eight examples, three trials each:

| Prompt | Median batch time | Explicit uncertainty for the opaque script |
|---|---:|---:|
| Current baseline | 4.84 s | 1 / 3 |
| Shorter instructions | 4.38 s | 1 / 3 |
| More explicit grounding rules | 4.53 s | 0 / 3 |
| Concrete examples + short labels | 4.38 s | 3 / 3 |

The last version produced exactly the kind of text we want in some trials:

> Find beef products and compare prices per kilogram.

> Read the code that searches for and compares beef prices.

> Run a script whose purpose is not known yet.

But another trial regressed to:

> Run the meat_search.js script.

And on one new weather example, it said the purpose was unknown **despite source
that clearly fetched a forecast and filtered rainy days**. The baseline identified
the weather operation in both transfer trials. The example-driven prompt was
also slower on that transfer set: 4.28 s median versus 3.90 s, two samples each.

All tested prompts resisted the synthetic payment/billing instruction comments,
and retained the broad distinction between reading code and running it. Remaining
problems were incidental filenames/languages, vague "predefined workflow" filler,
and occasionally dropping useful source evidence. Lexical flags in the summary
are not a substitute for this semantic inspection.

Conclusion: do **not** ship a prompt based on its prettiest answer. Keep accurate
operation/source metadata, retain unknown-purpose examples in the evaluation set,
and consider deterministic wording for truly known simple operations. A broader
fixture set is required before choosing a new default prompt.

## What I would ship first

1. **Recover from transient Host failures** without globally stopping narration.
2. **Prioritize visible/focused activity**, with fairness for history and other panes.
3. **Test a warm transport behind an opt-in flag**, measuring startup, turn time,
   post-turn overhead, cache hits and queue delay separately.
4. **Push ready explanations** instead of polling; keep retry/reconnect safeguards.
5. **Improve the evidence contract**, then evaluate prompt changes on unseen cases.

This round did not benchmark desktop frame rate, typing latency, other audience
levels, sustained provider load, or production reliability of a long-lived worker.
No claimed P95, SLA, universal speedup, or "perfect prompt" follows from this pilot.

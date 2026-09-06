# Grandma refinement: useful wording improvement, not a speed win

Six fresh trials, three per variant, eight identical synthetic activities,
Spark low, audience level 4. No production settings, code or services changed.

The candidate extends the audience-specific examples with plain-language meanings
of technical terms. It requires preserving the operation, unknown purpose and
numeric restrictions. Exact instructions and every answer are in
`results/grandma-refinement.jsonl`. These are still familiar development cases,
not an independent generalization evaluation.

## Actual paired examples

Repetition zero, without polishing:

| Activity | Current | Refined examples |
|---|---|---|
| Search login errors | Searches server files for expired-token messages and authorization failure strings. | Search server files for messages about denied access or login that may have expired. |
| Shorten timeout | Reduces the network request timeout setting from 60 seconds to 30 seconds. | Lower a network wait limit from 60 to 30 seconds in the request setup. |
| Unknown task | Runs an unspecified local task automation script to perform a predefined batch action. | Start a task whose purpose is not yet clear. |
| Push changes | Uploads the local feature-branch work to the remote repository branch. | Try to send local prepared changes to the shared project for a specific update. |

One stronger second-repetition answer was:

> Change the maximum waiting time from sixty to thirty seconds.

But its third repetition regressed to:

> Update network wait limit in code from 60,000 to 30,000 milliseconds.

## Findings

- Candidate retained explicit unknown purpose in 3/3 trials. Current was explicit
  in 1/3; the other two invented a predefined batch action or configured checks.
- The candidate avoided the listed jargon words in these 24 outputs, but still
  used awkward language such as "file head", "network wait limit", "self-instructions"
  and milliseconds. Passing a forbidden-word check is not the same as being clear.
- Both versions retained reading-versus-running for the source inspection cases.
- Candidate generally preserved the weather operation, though sometimes dropped
  Oslo or the exact 80% cutoff. Baseline also omitted those details in some trials.
- Baseline's second login-search answer added timeout errors not in the command.
  Candidate's third added "from prior attempts", which the evidence did not establish.
  Neither should be treated as an authoritative record of results.
- All six batches passed format/length checks. Current median was **4.02 seconds**;
  refined median **4.93 seconds**. This small pilot is not evidence of a speed win.

## Recommendation

The direction improves readability and honest uncertainty, but do not deploy this
prompt as a universal replacement yet. Test on fresh cases and explicitly evaluate
preservation of numbers, conditions and action scope. Keep Technical unchanged.
For performance, the already reproduced recovery and scheduling defects remain
more actionable than further stylistic prompt tuning.

## Reproduce

```sh
python3 labs/explanations/levels.py --grandma-refinement --output /tmp/grandma.jsonl
# Explicitly opt in to six real model calls:
python3 labs/explanations/levels.py --grandma-refinement --live --output /tmp/grandma.jsonl
```

Output uses exclusive creation to preserve previous evidence. Compare prompt
hashes as well as labels when aggregating experiments; the name "examples" alone
does not identify identical instructions across lab revisions.

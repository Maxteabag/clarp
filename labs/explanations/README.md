# Explanation performance and prompting lab

An experimental branch, not a deployed worker change. Fixtures are synthetic;
their commands are **data**, never executed. Keep the lab in its own worktree.
No app launch, Host restart, or iOS release is part of these scripts.

## Reproduce

From the repository root:

```sh
# Dry-run the planned matrix: zero model calls.
python3 labs/explanations/run.py
uv run --group dev pytest tests/unit/test_explanation_lab.py tests/unit/test_tool_explanations.py

# Explicit opt-in: consumes the existing Codex account's model allowance.
# Output paths must not already exist; the scripts refuse to overwrite evidence.
python3 labs/explanations/run.py --live --rounds 3 --prompts baseline compact grounded --output /tmp/prompt-lab.jsonl
python3 labs/explanations/run.py --live --rounds 3 --prompts baseline --batch-sizes 1 4 --output /tmp/batch-lab.jsonl
python3 labs/explanations/run.py --live --rounds 3 --prompts fewshot --output /tmp/fewshot-lab.jsonl
python3 labs/explanations/run.py --live --holdout --rounds 2 --prompts baseline fewshot --output /tmp/transfer-lab.jsonl

# Warm transport: fresh ephemeral thread each time, no shared conversation history.
python3 labs/explanations/warm.py --live --rounds 3 --output /tmp/warm-lab.jsonl
python3 labs/explanations/warm.py --live --cold --rounds 3 --output /tmp/cold-rpc-lab.jsonl
# Control for configuration isolation separately from transport.
python3 labs/explanations/run.py --live --private-profile --rounds 3 --prompts baseline --output /tmp/private-exec-lab.jsonl

# No model calls: real scheduler, synthetic 20ms model, 64 history rows then one live row.
python3 labs/explanations/scheduler.py

# No model calls or visible UI: characterize the real C++ HTTP-failure latch.
cmake --preset release -S desktop
cmake --build desktop/build/release --target clarp-tool-narrator-tests -j 2
desktop/build/release/tests/clarp-tool-narrator-tests labTransientHostFailureStopsNewExplanationsUntilReset

python3 labs/explanations/analyze.py labs/explanations/results
```

## Boundaries and interpretation

- Model stays `gpt-5.3-codex-spark`, effort `low`, audience Plain English (3).
- Use standalone processes. The exec tracer temporarily patches Python's
  subprocess constructor and the shipping prompt; never import this live runner
  into a production Host process.
- `run.py` invokes the shipping `_run_codex` implementation, retaining its
  schema, timeout, read-only sandbox and disabled action integrations. The
  stdout collector records event types/timing/usage, not reasoning content.
- `warm.py` uses a separate private Codex configuration directory because
  app-server lacks exec's `--ignore-user-config`. It links existing file-based
  login credentials without copying them into artifacts. It changes neither
  the user's configuration nor the running Clarp service. Keyring-only logins
  may require a separate supported authentication path; do not extract secrets
  or change production auth just to make a benchmark run.
- Every RPC trial uses an ephemeral thread and read-only sandbox. Unexpected
  tools/approval requests fail the experiment. The owned subprocess group is
  killed on completion/error; temporary profiles are removed.
- Exec's `turn.started` marks a turn phase, not model first-token time.
  Its JSON stream generally emits completed messages. Only the RPC probe's
  `first_delta_ms` measures receipt of the first agent-message delta.
- End-to-end exec time includes process exit and cleanup. RPC reports turn
  time separately and explicitly includes initialization for fresh processes
  in `elapsed_including_startup_ms`. Older warm samples can derive it by adding
  `startup_ms` only to repetition zero.
- The private-profile control matters: instruction/configuration delivery can
  alter input-token counts. Do not attribute that difference to process reuse.
- Provider prefix-cache tokens are distinct from Clarp's completed-answer cache.
- Small samples support hypotheses, not P95/SLA claims. Report sample counts,
  ranges, output failures, and bad examples alongside good examples.
- `holdout.json` is a small transfer set: weather and retention examples were
  not in prompt development; invoice counting also serves as a positive control
  resembling a prompt example. Do not call all five wholly unseen tasks.
- `analyze.py` flags selected jargon/filenames and explicit uncertainty. These
  are limited lexical checks, **not** automated semantic quality judgments.
- The C++ characterization test deliberately proves current undesirable
  behavior. Change its expectation when implementing recovery; do not preserve
  the latch as a desired production contract.

## References

The warm prototype follows the official [Codex app-server protocol](https://learn.chatgpt.com/docs/app-server):
initialize once, create threads, start turns with an output schema, and consume
turn-completion events. Verify the installed CLI's generated schema before
reusing experimental fields:

```sh
codex app-server generate-json-schema --experimental --out /tmp/clarp-lab-schema
```

See [REPORT.md](REPORT.md) for measured results and limitations.

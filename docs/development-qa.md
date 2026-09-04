# Reproducible verification

Create an owned worktree with
`python3 scripts/worktree.py create /exact/path --owner task-name --branch branch-name`.
Run `uv sync --frozen --group dev` and `npm ci` there. Never copy a `.venv` from
another path: console-script shebangs contain absolute interpreter paths.
`make py` uses `python -m pytest` so a relocated launcher cannot select another
environment. Source changes stay in the checkout; production deployment stays
in a permanent installation.

`python3 scripts/qa.py` runs the Python and JavaScript suites plus a synthetic
snapshot benchmark. `--full` adds disposable Docker and browser checks. Each
run retains command lines, exits, timings, source SHA and change hashes under
`.qa/`. Changes during verification invalidate the result. The Docker publish
workflow depends on the complete test gate.

`tests/integration/test_qa_turns.py` drives a real Host and deterministic Codex
provider process through retry/idempotency, two-host isolation and restart of a
durable queue. `tests/qa/host.py --state-dir /owned/disposable/path` exposes the
same harness for client QA. It binds only loopback; both state and telemetry
databases must stay inside the marked directory. Nothing in tests/qa is installed
into a production runtime.

## Debug evidence

Use `scripts/debug_bundle.py --state-db PATH --telemetry-db PATH --trace ID
--output NEW_FILE.json` to collect a bounded metadata-only timeline of requests,
client diagnostic events, turns, message revisions and clip/SSE identities.
The command opens databases read-only and omits prompt text, raw payloads and
diagnostic detail. Retain build manifests alongside it. Missing telemetry means
the corresponding category was disabled or aged out; it is not evidence that
the operation never happened.

## Closeout

After integration, run `scripts/worktree.py check /exact/path --owner task-name`
from a permanent checkout. It fetches main and checks merge containment,
cleanliness, ownership, process paths, Docker mounts/Compose labels (including
stopped containers), and systemd unit paths. `remove` repeats the checks before
using non-forced git worktree removal and pruning. Inventory failures block
removal. Redeploy dependencies from a permanent checkout first. The automated
workload inventory currently supports Linux; follow the repository's manual
closeout rules on other systems.

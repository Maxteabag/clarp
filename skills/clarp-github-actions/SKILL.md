---
name: clarp-github-actions
description: Publish and track a GitHub Actions workflow run.
---
# GitHub Actions
Use `clarp-github-workflow-artifact start "$CLAUDE_PWA_SESSION" OWNER/REPO RUN_ID`. The watcher updates one artifact until the run finishes; never create one artifact per poll.

Workflow-run artifacts show in the conversation, not in Updates: a CI result
is not something the user can act on there. When a failure actually blocks
your work, diagnose it first, then ask one `clarp-decisions` question naming
what is blocked and the options (retry, fix, skip). Withdraw it if you fix the
failure yourself.

When a merge must wait for CI, verify final check results explicitly before
merging. `gh pr merge --auto` can merge immediately when the repository has no
required checks; it is not a substitute for waiting on the workflow. Read back
the PR's state and merge SHA, and distinguish local test results from CI results.

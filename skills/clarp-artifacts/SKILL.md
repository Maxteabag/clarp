---
name: clarp-artifacts
description: Shared low-level transport for Clarp artifact skills. Use a type-specific artifact skill instead.
---

# Clarp Artifact Runtime

This core skill supplies the common authenticated helper and lifecycle rules.
Do not select an artifact type from this file. Use the independently managed
type skill installed on this computer. Images and galleries use `clarp-media`
and must never be created as artifacts; plans use `clarp-tasks`.

Create an artifact only for a durable deliverable the user may reopen, inspect,
download, share, or act on. Do not publish routine replies, logs, intermediate
scratch files, or every tool call.

Use the installed helper:

```bash
clarp-agent-artifacts \
  create "$CLAUDE_PWA_SESSION" research "Title" "Summary" '{"content":"..."}'
```

Supported types: `document`, `research`, `code_change`, `data`, `audio`,
`video`, `file`, `release`, `directory`, and
`workflow_run`. Plans are created by the task skill automatically.

When the user's explicit authorization is required, create a decision instead of
asking Yes/No only in prose:

```bash
clarp-agent-artifacts \
  decision "$CLAUDE_PWA_SESSION" "Send email" "Send this draft to Anna?" \
  Yes No '{"linked_type":"email_draft"}'
```

Stop before the protected action. A decision resolution is delivered back to
the same agent as a durable Clarp message. Revalidate recipients, content,
permissions, cost, and external state after approval, then act once. Rejection
does not authorize a modified action; revise and create a new decision.

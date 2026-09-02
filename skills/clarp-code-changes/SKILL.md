---
name: clarp-code-changes
description: Publish a code change, diff summary, pull request, or review.
---
# Code changes
Publish with `clarp-agent-artifacts create "$CLAUDE_PWA_SESSION" code_change TITLE SUMMARY JSON_PAYLOAD`.
Create type `code_change` with `repository`, plus relevant `branch`, `commit`, `source_url`, `files_changed`, `additions`, `deletions`, and `content`.

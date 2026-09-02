---
name: clarp-files
description: Publish a previewable and downloadable file.
---
# Files
Publish with `clarp-agent-artifacts create "$CLAUDE_PWA_SESSION" file TITLE SUMMARY JSON_PAYLOAD`.
Upload with `clarp-media-publish --session "$CLAUDE_PWA_SESSION" --json FILE`, then create type `file` with the returned `asset.url`, `mime_type`, `file_name`, and optional `size_bytes`.

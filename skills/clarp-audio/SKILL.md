---
name: clarp-audio
description: Publish an existing playable audio file as a durable deliverable.
---
# Audio
Publish with `clarp-agent-artifacts create "$CLAUDE_PWA_SESSION" audio TITLE SUMMARY JSON_PAYLOAD`.
Upload with `clarp-media-publish --session "$CLAUDE_PWA_SESSION" --json FILE`, then create type `audio` with the returned `asset.url`, `mime_type`, `file_name`, and optional `duration_ms`.

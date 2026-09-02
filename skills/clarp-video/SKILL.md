---
name: clarp-video
description: Publish an existing video with inline playback and a thumbnail.
---
# Video
Publish with `clarp-agent-artifacts create "$CLAUDE_PWA_SESSION" video TITLE SUMMARY JSON_PAYLOAD`.
Upload with `clarp-media-publish --session "$CLAUDE_PWA_SESSION" --json FILE`, then create type `video` with the returned `asset.url`, `mime_type`, `file_name`, and optional `thumbnail_url` and `duration_ms`.

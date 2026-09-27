---
name: clarp-files
description: Publish a previewable and downloadable file.
---
# Files
Publish with `clarp-agent-artifacts create "$CLAUDE_PWA_SESSION" file TITLE SUMMARY JSON_PAYLOAD`.
For a report, findings, audit or write-up, publish self-contained HTML with
`clarp-agent-artifacts create-report "$CLAUDE_PWA_SESSION" TITLE report.html --summary S`
instead of a file. Use a file only for data or when the user asks for a specific
format. Never rename a file to get it past the upload filter; `.md` is accepted.
Make the HTML self-contained: inline CSS and scripts, data-URL images, no external
requests. Keep it readable on a phone in light and dark mode (`prefers-color-scheme`).

Upload with `clarp-media-publish --session "$CLAUDE_PWA_SESSION" --json FILE`, then create type `file` with the returned `asset.url`, `mime_type`, `file_name`, and optional `size_bytes`.

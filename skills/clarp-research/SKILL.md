---
name: clarp-research
description: Publish a sourced research brief with findings and references.
---
# Research
Publish with `clarp-agent-artifacts create "$CLAUDE_PWA_SESSION" research TITLE SUMMARY JSON_PAYLOAD`.
Create type `research` with `content` and `sources`, an array of `{title,url}` objects. Preserve conclusions and source attribution.

Keep the `research` card for short sourced briefs. For anything longer, publish an
HTML report instead, with the sources linked inside it:
`clarp-agent-artifacts create-report "$CLAUDE_PWA_SESSION" TITLE report.html --summary S`.
Make the HTML self-contained: inline CSS and scripts, data-URL images, no external
requests. Keep it readable on a phone in light and dark mode (`prefers-color-scheme`).

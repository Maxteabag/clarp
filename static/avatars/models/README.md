# Model portraits

Variants of a bundled persona portrait, drawn for the model an Agent runs
on. When the Computer's "Model avatars" preference is on, an Agent still
wearing its bundled portrait wears the variant for its model instead.

Name each file `<persona-slug>.<family>.png`, 512×512, framed like the base
portrait in `../` so the two are interchangeable at avatar size. The slug is
the persona name lowercased with everything outside `[a-z0-9_-]` removed.

Families come from the model id, not the CLI: `opus`, `fable`, `sonnet`,
`haiku`, `gemini`, `grok`, `codex`. An Agent that pins no model falls back to
this Computer's configured default for its CLI, and then to the family the
CLI itself names — `codex` for Codex, `gemini` for Antigravity, `grok` for
Grok. The Claude CLI spans four families and so names none; a Claude Agent
with no model keeps its ordinary portrait.

A persona with no file for its family simply keeps its ordinary portrait.
Nothing here is generated at runtime.

See `server/lib/model_avatars.py`.

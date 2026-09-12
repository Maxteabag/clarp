# Preserving an older preview window

`adopt_preview.py` prepares a private copy of an older window's settings and
opens the current preview beside it only when explicitly invoked with `--launch`.
It never closes or signals the original, copies all persisted drafts and attachment
metadata, and opens the exact existing session without creating an agent. Future
changes in the two copies are independent; finish work in the adopted window.

```sh
python desktop/tools/adopt_preview.py --from-pid PID --session EXACT_SESSION \
  --host http://HOST:PORT --expect-title 'Unique persona — Clarp'
```

The default prepares a receipt only. Once the user is ready, repeat the command
with `--launch`. Resolve current PID/title from Hyprland and session from the Host.
The helper verifies the configured Host, authenticates a read-only roster request,
and requires exactly one persona/session matching that window title. Generic or
duplicate titles fail closed. It rechecks process start time/hash, window identity,
settings hash and installed binary hash before execution. It rejects changed drafts
instead of replacing them with an older snapshot. It preserves the source window's
workspace and requests `noinitialfocus`, but the tested Qt launch still takes
keyboard focus. Invoke it only when ready to switch to the new window.
No model, transcript, token or draft content is placed in command arguments or Git.

This preserves persisted data; it cannot recover an old binary's unpersisted
selection, cursor position, expanded tool cards or transient recording/upload state.
Pause typing and finish local recording/uploads before invoking it. The original
window remains available with those transient states intact. Do not treat general
repair authorization as permission to open a visible adopted window: prepare and
test in isolation first, then provide the concrete command for the user's action.

Run `python desktop/tests/test_preview_adoption.py`. The Qt screenshot-only
`CLARP_ADOPTION_TRACE` receipt checks restored session, Host, draft hash and attachment
count without exposing draft text. Use private settings and offscreen rendering.

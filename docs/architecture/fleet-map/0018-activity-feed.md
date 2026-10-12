# 0018 — Bottom-left activity feed

Status: Accepted. Owner request, 2026-09-11.

Add a compact, collapsible feed of the recorded Clarp state stream, including
tool calls and non-tool activity. Stable agent IDs determine colors; names stay
visible. Use an existing explanation only when recorded directly or matched to
the exact normalized action in the ready cache. Never generate explanations as
a side effect of watching the map. Redact command/detail snippets with Clarp's
existing redaction helper. Expired or future cached explanations are withheld.

Transport polls the durable state stream once a second. Forward cursors page in
bursts of 200; the UI retains 400 rows. Earlier pages remain accessible through
Earlier, which pauses the feed. Scrolling or inspecting a row pauses updates;
Resume returns to the latest records. The feed respects the map's live/replay
timeline. Demo activity is explicitly labeled synthetic. The panel starts open
on desktop and collapsed on small screens, and pauses polling during recap.

This reads recorded state, not an assertion that unrecorded events or complete
tool outputs exist. Raw details are expandable, and no state changes, agent
prompts or provider calls are issued by the feed.

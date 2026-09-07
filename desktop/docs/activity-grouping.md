# Tool activity presentation

Chats → Tool activity has three saved modes. Enter cycles the choices; Left and
Right select the adjacent choice. Existing Tool details preferences migrate to
Grouped (off) or Always visible (on).

- Grouped: collapse historical tool-only runs and tools attached to messages.
- Always visible: show individual cards.
- Group old: collapse completed history on entry; keep activity observed live
  during the visit visible after completion. A fresh visit clears expansion state.

Answer streaming remains a separate preference. Explanation audience never
forces collapsed cards open. Collapsed groups do not instantiate card delegates
or acquire explanation demand. Expanding fetches available lazy details and
mounts the cards; their existing viewport/dwell policy controls explanation work.

Tool-only runs are separated by messages. Their count uses available activity
metadata and their optional elapsed span uses transcript timestamps, not summed
tool durations (see below). Missing or zero spans are omitted rather than invented.

Checks: `oldActivityGroupsAreLazyAndVisitScoped`, the lazy-card QML regression,
and `clarp-desktop-ready-reply` (historical group beside observed live activity).

## Sender identity and elapsed labels

Cross-agent messages use right-aligned blue bubbles with the sender's portrait,
not a TEAM heading. Resolve the stable sender agent ID to its current session;
older messages without an ID may use their recorded sender session. Missing
portraits retain an initial, and hovering the avatar identifies the sender.
Human messages remain purple; automation retains its separate provenance label.

Collapsed activity, including tools attached to a prose message, shows its count
and available elapsed wall-clock time. The interval starts at the first activity
message timestamp and ends at the next assistant message, or the last grouped
activity timestamp if no closing reply exists. Never include the wait for a user
or teammate. This is not summed per-tool runtime: parallel calls and thinking
may overlap the interval. Invalid/missing timestamps omit timing, not invent it.
Timing does not fetch tool details or explanations. Expansion remains lazy.

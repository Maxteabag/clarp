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
metadata and their optional elapsed span is first-to-last timestamp, not summed
tool durations. Missing or zero spans are omitted rather than invented.

Checks: `oldActivityGroupsAreLazyAndVisitScoped`, the lazy-card QML regression,
and `clarp-desktop-ready-reply` (historical group beside observed live activity).

# iOS behavior parity for native desktop

The current clarp-ios origin/main is the behavioral source of truth for this
program. The Qt client adapts those contracts to a persistent
keyboard-and-pointer desktop rather than copying the phone layout literally.
Car Mode and CarPlay are explicitly out of scope.

## Desktop adaptations

- Keep the multi-pane workspace, command palette, native notifications, tray,
  MPRIS, filesystem access, and terminal launching.
- Use one local Host connection instead of reproducing the phone's
  multi-Computer connection manager.
- Present iOS's Chats, Updates, Teams, and Settings destinations in a permanent
  desktop rail.
- Keep a composer visible in every pane. Only the active pane owns keyboard,
  voice, and destructive actions; clicking another composer activates it.
- Use drag/drop, clipboard, and native file pickers instead of phone-only
  camera and location-first attachment flows.

## Parity matrix

| Surface | iOS contract to preserve | Native state |
| --- | --- | --- |
| Conversation sync | Stable IDs, revision-safe in-place updates, conversation identity fencing, optimistic confirmation | Implemented |
| Streaming | Provisional live row, incomplete voice-markup shielding, final/live de-duplication, no structural reset per token | Implemented |
| Activity | Semantic match key, running-to-terminal in-place update, Claude tools and Codex display cells share one presentation | Implemented |
| Scrolling | Follow only while pinned, preserve history anchor, surface new content below, prepend older history | Implemented |
| Composer | Always visible, per-Host/per-conversation durable draft, queue-if-busy, multi-file picker/drag-drop, MIME-aware durable attachment chips, send without losing focus | Implemented |
| Message rendering | Incremental-safe streaming text, selectable final Markdown, code/tables/links, authenticated inline media, lazy tool details, spoken-markup shielding, provenance and timestamps | Implemented |
| Chat list | Stale-state rejection, unread semantics, stable diff updates, recent-activity ordering, preview, search | Implemented |
| Agent profile | Identity, prompt history, media, artifacts, heartbeat history, teams, context/compact, task plan, queue, model/effort/MCP, schedules and autonomy | Implemented |
| Updates | Attention/decisions with pending feedback, active/recent jobs with progress, artifacts, action refresh and stale-response fencing | Implemented |
| Teams | Team list/messages, members, leader, create/edit/delete and independent refresh fencing | Implemented |
| Settings | Chat/voice preferences, Host auth entry point, orchestrator, live diagnostics, transcription/TTS status and keyboard reference | Implemented for desktop scope |
| Voice | Tap recording, background transcription ownership, playback/mute/stop and explicit state | Partial |
| New session | Where to Session to Run, resume/fork/new, backend/model/effort/MCP | Implemented |
| Files and terminal | Per-pane actions open the local agent folder and a terminal in its working directory | Implemented |
| Reliability | Cache-first restore, monotonic revisions, stale endpoint/request fencing, coalesced transcript refresh, reconnect ownership | Implemented |

## Required evidence

- Pure C++ tests cover timeline identity, in-place streaming/activity updates,
  stale state/replies, ordering, cache restore, drafts, attachments, Updates,
  Teams, queue, profile, avatar auth, and mutation requests.
- QML lint, warnings-as-errors release build, native tests, AppStream validation,
  and address/undefined-behavior sanitizers pass.
- Offscreen captures cover loading, incomplete streaming, long history,
  activity, 4x4 panes, Updates, populated/empty Teams, profile, queue, Settings,
  and narrow-window states.
- The live worktree preview is bound to Super+Alt+A without replacing the
  installed stable client.
- Direct release, sanitizer, native, QML, browser, and visual gates must pass
  before the branch is proposed for merge.

## Deliberate non-copies

- Car Mode and CarPlay remain absent.
- The desktop opens the Host's local files and terminal directly instead of
  embedding iOS's remote file browser and touch terminal.
- Phone-only subscription, iPhone action, camera, location, and multi-Host
  navigation screens are not copied into this single-Host desktop shell.
- Native file picking uses a direct local path only with the explicit shared
  filesystem preference; every other connection uses the authenticated upload
  contract, including containers and SSH tunnels on loopback URLs.

# Clarp Native Desktop Rewrite Plan

## Objective

Replace the browser-based desktop client with a native Linux desktop
application under `desktop/`, built with Qt 6.11, C++20, Qt Quick/QML, Qt
Network, and Qt Multimedia. The server remains the source of truth and the
existing PWA remains a separate browser client.

The native application must not embed or depend on Qt WebEngine, a WebView,
HTML, CSS, JavaScript assets, or the PWA runtime. QML is used only as Qt's
native declarative presentation language; state, protocol, networking, media,
and persistence live in C++.

## Existing behavior to preserve

The rewrite is pinned to `docs/protocol.md`, `contract/schemas/`, and
`contract/fixtures/`, rather than to incidental Svelte implementation details.

- Discover and render the live agent roster from `/agents/snapshot`.
- Select and follow an agent, including server-wide focus updates.
- Load tail history, paginate older messages, and apply revision deltas.
- Replace a conversation when its `conversation_id` changes or the server
  returns `replace_required`.
- Merge growing messages by id and revision without duplicating turns.
- Open one SSE stream, resume with `Last-Event-ID`, reconnect on silence or
  failure, and ignore unknown additive fields/events.
- Send idempotent messages with an optimistic `u-<client_msg_id>` row and keep
  delivery pending until that id appears in `/log`.
- Stop a running turn and represent queued, waiting, interrupted, and active
  states accurately.
- Record microphone PCM, upload it to `/transcribe`, and carry the returned
  trace/transcription ids into `/send`.
- Play announced clips, select sources by protocol precedence, and acknowledge
  queued/start/success/failure states.
- Treat `user-notification` as the only unread/desktop-notification decision.
- Create/relaunch/fork/release agents and expose the desktop overview,
  voice-selection, and orchestrator settings workflows.
- Preserve the desktop pane workspace, collapsible agent rail, keyboard-driven
  navigation, quick switcher, tool visibility, and scroll-to-latest behavior.

## Architecture

```text
QML views and components
        |
        v
AppController / focused view models
        |
        +-- AgentListModel
        +-- ConversationModel per open session
        +-- PaneTreeModel
        +-- DeliveryTracker
        |
        +-- ApiClient (REST, bearer auth, absolute URLs)
        +-- SseClient (stream parser, replay cursor, reconnect watchdog)
        +-- AudioController (capture, upload, playback, clip acknowledgements)
        +-- SettingsStore / CredentialStore
```

Design constraints:

- QML may format and animate already-derived state, but it must not issue
  network requests, parse JSON, own timers for protocol semantics, or contain
  business rules.
- Models expose typed Qt roles and invokable intent methods. They never expose
  mutable `QJsonObject` instances directly to QML.
- The server remains authoritative. Local persistence is limited to connection
  profiles, UI preferences, the last SSE id, and revocable device credentials.
- Network and media work is asynchronous. The GUI thread never blocks.
- Protocol objects tolerate unknown fields. Required-field failures become
  visible connection errors rather than crashes.
- Native transcript rendering uses Qt text/layout primitives and dedicated
  delegates for prose, code, tools, activity, and delivery state.

## Delivery phases

### 1. Build and quality foundation

- CMake/Ninja project with an executable, QML module, Qt Test targets, install
  rules, warnings-as-errors in CI, clang-tidy, clang-format, `qmllint`, ASan,
  UBSan, and optional coverage.
- Runtime guard that refuses to initialize Qt WebEngine if it is accidentally
  linked in the future.
- Developer presets for debug, release, and sanitizer builds.

### 2. Protocol core

- URL construction and bearer authentication.
- Typed agent/message/activity/audio structures.
- SSE block parser and streaming client.
- Snapshot and conversation synchronization reducers.
- Delivery tracker and clip-source selection.
- Qt Test coverage using the repository's golden contract fixtures.

### 3. Native desktop shell

- Branded application window, collapsible rail, agent status rows, header,
  transcript list, composer, connection/error states, and keyboard shortcuts.
- Native connection setup for server URL and paired-device token.
- Single-pane behavior complete before recursive pane splitting is added.

### 4. Full desktop workflows

- Recursive split panes, active/hovered pane semantics, resize/equalize/zoom,
  quick switching, and persisted layout.
- Agent overview, creation/relaunch/fork/release, voice picker, and orchestrator
  settings.
- Tool/activity presentation, older-history loading, unread badges, native
  desktop notifications, system tray, and single-instance behavior.

### 5. Native media

- Qt Multimedia microphone/device selection and permission/error states.
- Deterministic PCM/WAV upload path for `/transcribe`.
- Authenticated MP3, raw PCM, and finalized HLS/fMP4 playback with clip
  acknowledgement, duplicate suppression, and recoverable-clip support.
- MPRIS/media-key integration on Linux.

### 6. Distribution

- Install rules, AppStream metadata, desktop entry, icons, and MIME/URL scheme
  declarations.
- Flatpak manifest as the primary update/distribution channel.
- AppImage recipe for portable releases.
- AUR `PKGBUILD` for Arch/Omarchy installations.
- Reproducible release CI inputs and documented signing/checksum procedure.

## Verification gates

Completion requires all of the following evidence:

- `cmake --preset dev` configures with Qt 6.11 and Ninja.
- Debug and release builds complete without warnings.
- Unit and contract tests pass under normal and sanitizer presets.
- `clang-tidy` and `qmllint` pass with warnings treated as failures.
- No desktop target links Qt WebEngine/WebChannel or ships HTML/CSS/JS.
- Automated fake-server integration proves server info, snapshot, history,
  SSE, send delivery, authentication, model catalog, paths, and schedules.
- Before a public release, a disposable-server smoke test must additionally
  prove stop, microphone transcription, and audio acknowledgement end to end.
- The application is visually inspected under native Wayland and XWayland,
  including fractional scaling, keyboard-only use, long transcripts, and
  window resize behavior.
- Flatpak, AppImage, and AUR artifacts or recipes build successfully in clean
  environments.

## Known risks

- Rich Markdown and syntax-highlighted code need careful native layout and
  virtualization to avoid recreating a browser-shaped renderer.
- Qt Multimedia codec availability varies by Linux distribution; packaging
  must carry the required FFmpeg/GStreamer support and retain protocol
  fallbacks.
- QML permits JavaScript expressions. Project review and linting must keep
  imperative application logic out of QML.
- The open-source Qt license path requires a documented module/license audit
  and dynamic-linking compliance before public distribution.

# Slint parity

The whole desktop UI rebuilt in Slint (no Qt, no QML), goal `slint-ui`.
Each QML file maps to its Slint implementation; a row is **done** only with
the Slint code and a check (headless test or probe) named here.

## Architecture decision

The QML app's logic lives in the cxx-qt bridge (`app/src/bridge/controller.rs`
and models), which is Qt-bound. The Slint app gets a new UI-neutral crate,
`engine/`, ported from that bridge: plain Rust state, commands and change
notifications on top of `clarp-core`/`clarp-net`, with no toolkit types. The
Qt bridge stays as it is (PR #201 must not break); moving it onto `engine/`
later removes the duplication. The Slint app (`slint-app/`) is a thin view
over `engine/`, so its behaviour tests run against the engine without a UI.

## QML components (62 files, 11688 lines)

| QML file | Lines | Slint target | Status |
|---|---|---|---|
| `qml/Main.qml` | 874 | `slint-app/ui/app.slint` + `slint-app/src/main.rs` | wip: window, header with connection state, error banner, sidebar/transcript/composer layout on `clarp-engine`; e2e `slint-app/tests/run-e2e.sh` (roster, send, streaming, second chat) passes |
| `qml/PreviewVersionWindow.qml` | 42 | preview versions window | todo |
| `qml/components/ActivityExplanation.qml` | 134 | ui/transcript.slint | todo |
| `qml/components/ActivitySweep.qml` | 15 | ui/sidebar.slint | todo |
| `qml/components/AgentAvatar.qml` | 81 | ui/sidebar.slint | todo |
| `qml/components/AgentOverview.qml` | 499 | ui/panels/agentoverview.slint | todo |
| `qml/components/AgentProfilePanel.qml` | 653 | ui/panels/agentprofilepanel.slint | todo |
| `qml/components/AgentRail.qml` | 52 | ui/sidebar.slint | todo |
| `qml/components/ArtifactSummaryCard.qml` | 70 | ui/transcript.slint | todo |
| `qml/components/AssignAgentDialog.qml` | 146 | ui/panels/assignagentdialog.slint | todo |
| `qml/components/AttachFileDialog.qml` | 12 | ui/composer.slint | todo |
| `qml/components/AvatarActivity.qml` | 38 | ui/sidebar.slint | todo |
| `qml/components/ChatList.qml` | 456 | `slint-app/ui/sidebar.slint` (search, All/Unread, pair rooms and archive rows, nesting via `clarp_core::sidebar`) | wip: in the e2e; keyboard navigation, hide/collapse, restore from archive and a search/scope check pending |
| `qml/components/ChatRow.qml` | 301 | `slint-app/ui/sidebar.slint` `Row` (name, stamp, preview, activity line, queue, muted, unread, busy ring, helper depth) | wip: portraits, process glyph, done-helpers lines pending |
| `qml/components/Composer.qml` | 461 | ui/composer.slint | todo |
| `qml/components/ConnectionPage.qml` | 151 | ui/panels/connectionpage.slint | todo |
| `qml/components/ConversationPane.qml` | 575 | ui/workspace.slint | todo |
| `qml/components/DeferredPanel.qml` | 74 | ui/workspace.slint | todo |
| `qml/components/DisplayCellCard.qml` | 200 | ui/transcript.slint | todo |
| `qml/components/HeaderContext.qml` | 115 | ui/workspace.slint | todo |
| `qml/components/KeyboardMap.qml` | 146 | ui/workspace.slint | todo |
| `qml/components/KeymapEditor.qml` | 32 | ui/workspace.slint | todo |
| `qml/components/LaunchDirectoryPicker.qml` | 113 | ui/panels/launchdirectorypicker.slint | todo |
| `qml/components/MediaGallery.qml` | 163 | ui/panels/mediagallery.slint | todo |
| `qml/components/MessageDelegate.qml` | 538 | ui/transcript.slint | todo |
| `qml/components/NavRail.qml` | 145 | `slint-app/ui/navrail.slint` (same SVG icons, attention badge, mute, settings, Host initial) | wip: surfaces other than chats pending |
| `qml/components/NewSessionHub.qml` | 458 | ui/panels/newsessionhub.slint | todo |
| `qml/components/OrchestratorDialog.qml` | 193 | ui/panels/orchestratordialog.slint | todo |
| `qml/components/PairRow.qml` | 117 | `slint-app/ui/sidebar.slint` room rows + `engine` rooms (unread from seen revisions; `engine/tests/host_flow.rs::pair_rooms_load_unread_and_are_read_once_opened`) | wip: participants' portraits pending |
| `qml/components/PaneLeaf.qml` | 69 | ui/workspace.slint | todo |
| `qml/components/PreviewVersionPanel.qml` | 114 | ui/panels/previewversionpanel.slint | todo |
| `qml/components/ProcessGlyph.qml` | 118 | ui/sidebar.slint | todo |
| `qml/components/ProcessIndicator.qml` | 85 | ui/sidebar.slint | todo |
| `qml/components/ProcessPopover.qml` | 236 | ui/sidebar.slint | todo |
| `qml/components/QueueDialog.qml` | 167 | ui/panels/queuedialog.slint | todo |
| `qml/components/QuickSwitcher.qml` | 440 | ui/switcher.slint (prototype exists) | todo |
| `qml/components/RenameAgentDialog.qml` | 99 | ui/panels/renameagentdialog.slint | todo |
| `qml/components/ReportView.qml` | 154 | ui/panels/reportview.slint | todo |
| `qml/components/SettingsPanel.qml` | 625 | ui/panels/settingspanel.slint | todo |
| `qml/components/ShortcutBar.qml` | 50 | ui/workspace.slint | todo |
| `qml/components/StartAgentDialog.qml` | 368 | ui/panels/startagentdialog.slint | todo |
| `qml/components/StatusPill.qml` | 49 | ui/sidebar.slint | todo |
| `qml/components/TeamsPanel.qml` | 564 | ui/panels/teamspanel.slint | todo |
| `qml/components/Theme.qml` | 47 | ui/widgets.slint (shared controls, theme global) | todo |
| `qml/components/ThemedComboBox.qml` | 31 | ui/widgets.slint (shared controls, theme global) | todo |
| `qml/components/ToolCard.qml` | 150 | ui/transcript.slint | todo |
| `qml/components/TranscriptList.qml` | 478 | ui/transcript.slint | todo |
| `qml/components/TranscriptView.qml` | 177 | ui/transcript.slint | todo |
| `qml/components/TuiBusyIndicator.qml` | 25 | ui/widgets.slint (shared controls, theme global) | todo |
| `qml/components/TuiButton.qml` | 14 | ui/widgets.slint (shared controls, theme global) | todo |
| `qml/components/TuiCheckBox.qml` | 20 | ui/widgets.slint (shared controls, theme global) | todo |
| `qml/components/TuiLabel.qml` | 3 | ui/widgets.slint (shared controls, theme global) | todo |
| `qml/components/TuiRadioButton.qml` | 20 | ui/widgets.slint (shared controls, theme global) | todo |
| `qml/components/TuiSwitch.qml` | 20 | ui/widgets.slint (shared controls, theme global) | todo |
| `qml/components/TuiText.qml` | 2 | ui/widgets.slint (shared controls, theme global) | todo |
| `qml/components/TuiTextArea.qml` | 14 | ui/widgets.slint (shared controls, theme global) | todo |
| `qml/components/TuiTextField.qml` | 14 | ui/widgets.slint (shared controls, theme global) | todo |
| `qml/components/TuiToolButton.qml` | 14 | ui/widgets.slint (shared controls, theme global) | todo |
| `qml/components/TypingIndicator.qml` | 42 | ui/transcript.slint | todo |
| `qml/components/UpdatesPanel.qml` | 331 | ui/panels/updatespanel.slint | todo |
| `qml/components/VoiceDialog.qml` | 123 | ui/panels/voicedialog.slint | todo |
| `qml/components/Workspace.qml` | 171 | ui/workspace.slint | todo |

## REWRITE_PLAN behaviours (against the Slint app)

| Behaviour | Slint check | Status |
|---|---|---|
| Discover and render the live agent roster from `/agents/snapshot`. |  | todo |
| Select and follow an agent, including server-wide focus updates. |  | todo |
| Load tail history, paginate older messages, and apply revision deltas. |  | todo |
| Replace a conversation when its `conversation_id` changes or the server returns `replace_required`. |  | todo |
| Merge growing messages by id and revision without duplicating turns. |  | todo |
| Open one SSE stream, resume with `Last-Event-ID`, reconnect on silence or failure, and ignore unknown additive fields/events. |  | todo |
| Send idempotent messages with an optimistic `u-<client_msg_id>` row and keep delivery pending until that id appears in `/log`. |  | todo |
| Stop a running turn and represent queued, waiting, interrupted, and active states accurately. |  | todo |
| Record microphone PCM, upload it to `/transcribe`, and carry the returned trace/transcription ids into `/send`. |  | todo |
| Play announced clips, select sources by protocol precedence, and acknowledge queued/start/success/failure states. |  | todo |
| Treat `user-notification` as the only unread/desktop-notification decision. |  | todo |
| Create/relaunch/fork/release agents and expose the desktop overview, voice-selection, and orchestrator settings workflows. |  | todo |
| Preserve the desktop pane workspace, collapsible agent rail, keyboard-driven navigation, quick switcher, tool visibility, and scroll-to-latest behavior. |  | todo |

## End-to-end

`slint-app/tests/run-e2e.sh` runs the Slint app against `tests/qa/host.py` in a
loopback-only network namespace (isolated HOME, system PATH), headless on
Slint's software renderer. The driver (`slint-app/src/driver.rs`) types and
sends through the real composer, sees the reply stream and complete, and opens
a second chat. Screenshots: `slint-app/docs/e2e/`.

# Contextual keyboard navigation

The native client follows sqlit's `core/state_base.py` and
`domains/shell/state/machine.py` pattern: a focused leaf inherits parent actions,
a child can override an action, and a modal stops workspace inheritance. This
is an adaptation to Qt focus ownership, not a port of Textual or Vim editing.

`qml/components/KeyboardMap.qml` is the binding registry. The window creates
shortcuts from `shortcuts`; `ShortcutBar.qml` displays `hints` from the same
resolved entries. Guards apply to both. Native editor/settings actions are
marked `native` so their hints do not install a competing shortcut. Add a key
there rather than adding a second window Shortcut or hard-coded footer hint.

Hierarchy:

- Main: command picker and modified application shortcuts.
- Workspace: pane operations, release, voice, and Escape.
- Navigation: workspace plus letter navigation and Tab between areas.
- Conversation and sidebar: navigation children, with sidebar row actions.
- Composer and sidebar search: workspace children; plain keys stay text.
- Settings, updates, and teams: main children with screen-specific hints.
- Modal: close only. Settings dialogs block all window dispatch and let their
  own controls and popups consume keys.

From typing, Escape enters conversation navigation. `e` opens and focuses the
sidebar at the current agent, `c` focuses the conversation, and `i` resumes the
composer. Tab/Shift+Tab toggle the two navigation areas. In the sidebar,
`j`/Down and `k`/Up move the highlighted row without switching the conversation;
Enter opens it. `/` focuses search. Escape from search restores the current
agent; a filter hiding that agent is cleared. Enter and Shift+Enter in the
composer retain send/new-line behavior. Space opens commands only in navigation.
Ctrl+B continues to show/hide the sidebar; Ctrl+K works while typing.

The sidebar uses session identity via `AgentFilterModel::indexOfSession`, not a
stale source row number or the first visible item. Returning to the sidebar
reveals the current agent if search/unread/archive filters hid it.

Verification:

```sh
cmake --build desktop/build/release
QT_FORCE_STDERR_LOGGING=1 ctest --test-dir desktop/build/release --output-on-failure
cmake --build desktop/build/release --target all_qmllint
```

`clarp-desktop-activity-layout` includes hierarchy, guard, duplicate-key and
text-isolation tests. `clarp-desktop-context-keyboard` sends actual key events to
an isolated offscreen app with two fixture agents, checks draft preservation,
current-agent anchoring, browse versus activation, Tab, picker return, hidden
sidebar recovery, and a search that excludes the selected agent. It never
injects keys into the visible desktop or sends/releases a live agent.

`n` in navigation (or Ctrl+J while typing) opens the next other agent with an
unread reply, a waiting state, or a pending attention item. It cycles in roster
order and skips unknown/archived sessions. Visiting an unread agent clears its
unread flag through the normal selection path; pending questions are not
answered or dismissed. The footer hint disappears when no other target exists.

The toolbar binds to AppController's notified `nextAttentionTarget` property.
Its notifications cover roster changes, attention updates, and selection. Do
not replace this with an opaque invokable call and assumed QML dependencies:
the real-key fixture caught a stale availability hint with that approach.


Ctrl+K also changes local settings directly. Search for timestamps, streaming,
tool details, phone/iPhone notifications, shared filesystem, voice replies, or
an explanation audience (Developer through Grandma), then press Enter. Boolean
entries show the current and next state; audience entries mark the current
level and whether AI is used. Existing controller setters persist the changes.
Escape cancels and typing focus is restored after a direct setting action.
Connection and orchestrator entries still open their dedicated configuration
screens for fields that require more than a toggle.

Minimal UI (Settings → Appearance, or Ctrl+K → Minimal UI) only hides the sidebar
chevron. It defaults off and persists independently of sidebar visibility; Ctrl+B
still works. Headless screenshots with `CLARP_SCREENSHOT_MINIMAL_UI=0` or `1`
assert the chevron's visibility without changing the user's saved setting.

`tst_palette_settings.qml` covers actual Enter selection, query aliases, state
labels, exact detail-level selection, Escape cancellation and focus return.
For a screenshot, set `CLARP_SCREENSHOT_QUERY=streaming` and use the existing
screenshot helper with `quickSwitcher` as its view argument.

Ctrl+Shift+N opens the name-only New contact & chat dialog. Enter a new name and
press Enter to create a fresh agent using the saved workspace and quick-start
backend. The dialog rejects empty/offline submissions, prevents duplicate
submission while pending, and retains the name on error. Ctrl+N keeps the full
configuration form; Ctrl+Alt+N selects an existing idle contact. These are distinct
operations. `tst_quick_new_agent.qml` covers Enter, cancellation, failure/retry,
saved defaults and pending submission.

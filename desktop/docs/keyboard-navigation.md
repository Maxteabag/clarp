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

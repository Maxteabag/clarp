# Transcript view

The transcript is Clarp's own virtualized list. It replaces `ListView` for the
conversation because `ListView` places rows it has not created from an
*estimated* height. Chat rows range from one line to 140-row tables and
500 KB messages, so the estimate swings (one 1,542-row chat was estimated at
14.7 million pixels) and every correction moved the view: blank panes, jumps
while reading, a following view left short of its end. Those were patched one
by one in `TranscriptList.qml`; this design removes the cause.

## Principle

Positions come from heights the view knows, never from an average.

- Every row has a height record: *measured* (its delegate existed at this
  width) or *estimated* (not measured yet). Estimates are per row, from the
  row's own text length and kind, not from the loaded average.
- A row's top is the sum of the heights above it, kept in a Fenwick tree, so
  a height change updates every position below in O(log n) and a position ->
  row lookup is O(log n).
- The viewport is described by an anchor: `(row, offset)`, the row at the top
  of the viewport and how far into it the viewport starts. `contentY` is
  derived from the anchor after every height change, so a row above the
  viewport growing, shrinking, being measured, inserted or removed never moves
  what the reader sees.
- Following is a mode, not a position: while following, the anchor is "the
  end", and `contentY = total - height` after every change. The last rows
  are always measured because they are on screen.
- Estimates only ever affect the scrollbar thumb, never the content on screen.

## Pieces

- `TranscriptLayout` (C++, `QQuickItem`): owns the height records and the
  Fenwick tree, creates delegates for the visible range plus a small margin
  (by pixels, not rows), positions them, pools them for reuse, measures them
  when they report their height, and exposes `contentHeight`,
  `positionOf(row)`, `rowAt(y)`, `itemAt(row)`.
- `TranscriptView.qml`: a `Flickable` whose content item holds the layout.
  It owns scrolling input (wheel, touchpad, keys, scrollbar), the anchor and
  follow logic, and keeps the API of `TranscriptList.qml` that the pane and
  the smoke checks use (`followLatest`, `atYEnd`, `distanceFromBottom`,
  `userInteracting`, `count`, `scrollToLatest()`, `pauseFollowing()`,
  `resetForConversation()`, `itemAtIndex()`, `indexAt()`,
  `positionViewAtIndex()`, `originY` fixed at 0).
- Sections (day labels) and the "load earlier" header are rows of the layout,
  so they are measured like any other row.
- Background measurement: when idle, the layout measures a few not-yet-
  measured rows near the viewport per frame (bounded time per frame) so the
  scrollbar converges without affecting what is on screen.

## Rollout

Behind `CLARP_TRANSCRIPT_VIEW=2` (and a settings toggle) next to
`TranscriptList`. It becomes the default only when it passes every rig
scenario (streaming follow, switch storm, wheel storms, resize, huge chats,
older history prepended, the 15-minute session) with no regression against
`TranscriptList`; then `TranscriptList` is removed.

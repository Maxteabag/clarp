# Streaming text rendering

Qt's [performance guide](https://doc.qt.io/qt-6/qtquick-performance.html)
recommends limiting delegate work, avoiding unnecessary binding reevaluation,
and reducing text layout cost. Its [ListView documentation](https://doc.qt.io/qt-6/qml-qtquick-listview.html)
describes delegate reuse and variable-height estimation. Keep the existing
`TranscriptList` follow-intent and message-anchor protections when optimizing;
changing scroll physics without tracing rendering can hide the actual cost.

The native message renderer previously bound a Repeater to a freshly returned
string array. Each live text update replaced that model and destroyed/recreated
its TextEdit. The regression fixture counts `itemAdded`: 80 streamed updates
created 80 replacement editors before the fix and zero afterward. This measures
object churn, not GPU frame rate or device smoothness.

The Repeater now uses block count and looks up each retained delegate's text by
index. Appending/removing blocks changes the delegate count; replacing text in
an existing block does not. Live text stays PlainText until completion, preserving
the existing protection against incomplete Markdown fences and tags. Long
messages use available bubble width without a second full-message TextMetrics
layout. ConversationModel publishes only changed roles, avoiding tool-array
invalidation on text-only deltas and notifications for unchanged rows.

Run the checks from the repository root:

```sh
QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software /usr/lib/qt6/bin/qmltestrunner \
  -input desktop/tests/qml/tst_streaming_render.qml
QT_FORCE_STDERR_LOGGING=1 ctest --test-dir desktop/build/release --output-on-failure
```

The full lane also covers reader position during streamed growth, queued follow
cancellation, prepend/reset anchoring, live-to-final row retirement, and role
notifications. Further performance claims require a frame/timing profile on the
renderer/device experiencing the lag. Do not infer a network bottleneck from
scrolling symptoms or use test runtime as a frame-rate benchmark.

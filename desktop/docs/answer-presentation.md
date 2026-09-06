# Answer presentation

Settings → Chats → Show when ready is a saved, global desktop preference. The
same toggle appears in each conversation's menu. Default is streaming, and
switching modes does not discard transcript rows or change agent execution.

The final-reply rule matches iOS `AnswerPresentationPolicy`: provisional
assistant rows explicitly marked `kind=live` are withheld until the Host
publishes an authoritative final row. Do not guess completion from punctuation,
a quiet stream, or a timer. Final messages and errors stay visible even if the
agent goes on to do more work. User messages remain immediate.

The desktop quiet presentation additionally hides tool/activity rows and tool
metadata and shows three animated typing dots while the agent is thinking,
using a tool, or compacting. Waiting, interruption, completion, and disconnection
stop that indicator. Unclassified sidebar snapshot previews are suppressed in
this mode because they cannot distinguish partial from finalized text.

`ConversationPresentationModel` is a proxy over the unchanged canonical model.
It retains message-ID lookup for scroll anchoring and only signals visible
appends. Conversation identity changes explicitly reset follow intent, while
refreshes within a conversation preserve the anchor. Hidden live rows do not
create text delegates or emit visible text changes.

Verification:

```sh
QT_FORCE_STDERR_LOGGING=1 ctest --test-dir desktop/build/release \
  -R 'core|ready-reply|settings-keyboard|activity-layout' --output-on-failure
```

The model test checks hidden live updates, returning to streaming, final
publication, activity filtering, canonical retention and settings persistence.
The isolated `clarp-desktop-ready-reply` lane checks actual TextEdit content,
visible typing dots, and final reply arrival. It writes `capture.png.typing.png`
before completion and `capture.png` afterward, under
`desktop/build/release/tests/ready-reply/`. The fixture never contacts a real Host.

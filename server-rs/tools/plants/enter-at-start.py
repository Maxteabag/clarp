#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

Issue 491, part A: a message enters the conversation when its turn starts, whoever sent it. The journal's one rule
of whether a message is in the conversation at once, the row its turn's start writes with the send time, the
receipt a waiting send keeps, the rewording of what waits, a log an older build wrote, and what the actor tells.
"""
import runner

RUNS = {
    'kernel': ['test', '-p', 'clarp-kernel', '--lib'],
    'actor': ['test', '-p', 'clarp-actor', '--test', 'actor'],
}
J = 'clarp-kernel/src/journal.rs'
A = 'clarp-kernel/src/agent.rs'
C = 'clarp-kernel/src/conversation.rs'
L = 'clarp-actor/src/lib.rs'
OUTSIDE = '        let outside = !self.agent.starts_at_once(message.admission);\n'
AT_ONCE = '        self.live.is_none() && !(self.paused && admission == Admission::Background)\n'
SENT_AT = '                    let sent_at = sent_at.map_or(stored.ts_ms, |queued| queued.accepted_at_ms);\n'
ROW = '                    let mut item = NewItem::new(kind::MESSAGE, now_ms, body).in_turn(turn.0);\n'
KEYED = '                    item.key = stored.key;\n                    self.items.push(item);\n'
# NOT PLANTED, each with its reason:
#   that a turn accepted in the very batch that starts it is no waiting item (`if message_seq >
#     self.recorder.last_seq`, in `Batch::enter`) as `>=`: a waiting item is always followed in its write by the
#     event that accepts it, so it is never the log's last item when a later write starts its turn.
#   that a body which is no JSON object is kept as it is (`sent`): every message the Host writes is one; the kernel's
#     own tests write bare words, and the rule only keeps them from failing a start.
#   the words of the edge's documentation and the 409 refusal, which this piece does not change.
PLANTS = [
    # The one rule: a message is in the conversation at once exactly when its turn starts at once.
    (J, 'kernel', 'every message is in the conversation at once', OUTSIDE, '        let outside = false;\n'),
    (J, 'kernel', 'every message waits outside, even one that starts at once', OUTSIDE, '        let outside = true;\n'),
    (J, 'kernel', 'only what is left for later waits outside (the rule before issue 491)', OUTSIDE,
     '        let outside =\n            message.admission.left_for_later() && !self.agent.starts_at_once(message.admission);\n'),
    (J, 'kernel', "only the user's word waits outside", OUTSIDE,
     '        let outside = message.origin.is_user() && !self.agent.starts_at_once(message.admission);\n'),
    (J, 'kernel', 'a trigger\'s word is in the conversation at once', OUTSIDE,
     '        let outside = !matches!(message.origin, Origin::Trigger { .. })\n'
     '            && !self.agent.starts_at_once(message.admission);\n'),
    (J, 'kernel', 'a direct message is in the conversation at once', OUTSIDE,
     '        let outside = message.admission != Admission::Direct && !self.agent.starts_at_once(message.admission);\n'),
    (J, 'kernel', 'held background work is in the conversation at once', OUTSIDE,
     '        let outside = message.admission != Admission::Background\n'
     '            && !self.agent.starts_at_once(message.admission);\n'),
    (J, 'kernel', 'a message is said to have entered whether or not it did', '            entered: !outside,\n',
     '            entered: true,\n'),
    (J, 'kernel', 'no message is said to have entered', '            entered: !outside,\n', '            entered: false,\n'),
    # Whether a message starts at once.
    (A, 'kernel', 'held background work starts at once', AT_ONCE, '        self.live.is_none()\n'),
    (A, 'kernel', 'nothing starts at once on a held agent', AT_ONCE, '        self.live.is_none() && !self.paused\n'),
    (A, 'kernel', 'a busy agent starts a message at once', AT_ONCE,
     '        !(self.paused && admission == Admission::Background)\n'),
    (A, 'kernel', 'a stopping turn lets a message start at once', AT_ONCE,
     '        (self.live.is_none() || self.stopping.is_some())\n'
     '            && !(self.paused && admission == Admission::Background)\n'),
    (A, 'kernel', 'a parked turn lets a message start at once', AT_ONCE,
     '        (self.live.is_none() || matches!(self.live, Some(Turn::Parked(_))))\n'
     '            && !(self.paused && admission == Admission::Background)\n'),
    # The row its turn's start writes.
    (J, 'kernel', 'the send time is when the waiting copy was last written', SENT_AT,
     '                    let sent_at = sent_at.map_or(stored.ts_ms, |_| stored.ts_ms);\n'),
    (J, 'kernel', 'the send time is when the turn starts', SENT_AT,
     '                    let sent_at = sent_at.map_or(stored.ts_ms, |_| now_ms);\n'),
    (J, 'kernel', 'the row keeps no send time', '                    let body = sent(stored.body, sent_at);\n',
     '                    let body = {\n                        let _ = sent_at;\n                        stored.body\n                    };\n'),
    (J, 'kernel', 'the row stands at its send time', ROW,
     '                    let mut item = NewItem::new(kind::MESSAGE, sent_at, body).in_turn(turn.0);\n'),
    (J, 'kernel', 'the row is in no turn', ROW,
     '                    let mut item = NewItem::new(kind::MESSAGE, now_ms, body);\n'),
    (J, 'kernel', 'the row has no id', KEYED, '                    self.items.push(item);\n'),
    (J, 'kernel', 'the row is not written', KEYED, '                    item.key = stored.key;\n                    let _ = item;\n'),
    (J, 'kernel', 'the send time under another key',
     '    message.insert(crate::conversation::SENT_AT.to_string(), at_ms.into());\n',
     '    message.insert("sent_at".to_string(), at_ms.into());\n'),
    (C, 'kernel', "the log's key named otherwise", 'pub const SENT_AT: &str = "sent_at_ms";\n',
     'pub const SENT_AT: &str = "sent_at";\n'),
    (C, 'kernel', 'a message that started at once is written with an empty send time',
     '    #[serde(default, skip_serializing_if = "Option::is_none")]\n    pub sent_at_ms: Option<i64>,\n',
     '    #[serde(default)]\n    pub sent_at_ms: Option<i64>,\n'),
    # What a waiting send keeps, and its words.
    (A, 'kernel', 'a plain send keeps no receipt while it waits',
     '                if origin.is_user() && matches!(admission, Admission::Queue | Admission::Direct) {\n',
     '                if origin.is_user() && matches!(admission, Admission::Direct) {\n'),
    (J, 'kernel', 'what waits outside is refused a rewording as if it were said',
     '                .read_after(message_seq.saturating_sub(1), Kinds::only(kind::WAITING), 1)?;\n',
     '                .read_after(message_seq.saturating_sub(1), Kinds::only(kind::MESSAGE), 1)?;\n'),
    # A log an older build wrote.
    (J, 'kernel', 'a message an older build wrote into the conversation is said not to wait',
     '            return Ok(Reworded::InConversation);\n', '            return Ok(Reworded::NotWaiting);\n'),
    (J, 'kernel', 'a start anew moves what waits outside into the conversation',
     '            let kept = self.log.read_after(from, Kinds::only(kind::MESSAGE), 1)?;\n',
     '            let kept = self.log.read_after(from, Kinds::only(kind::WAITING), 1)?;\n'),
    # What the actor tells.
    (L, 'actor', 'a waiting send is told as a change of the conversation', '            conversation: accepted.entered,\n',
     '            conversation: true,\n'),
]

if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))

"""Wrong rules for the owner's coordination settings and filtered chat pages."""
import runner

RUNS = {
    'vectors': ['test', '--test', 'hidden_coordination_vectors'],
    'actor': ['test', '-p', 'clarp-actor', '--test', 'actor',
              'a_turn_accepted_without_a_note_capability'],
    'counts': ['test', '-p', 'clarp-actor', '--lib', 'counted_runs_match'],
    'host': ['test', '--test', 'host', '--', 'a_user_can_hide_coordination'],
}
K = 'clarp-kernel/src/hidden_coordination.rs'
C = 'clarp-actor/src/conversation.rs'
H = 'clarp-host/src/hidden_coordination.rs'
E = 'clarp-host/src/edge.rs'
F = 'clarp-host/src/fleet.rs'

PLANTS = [
    (K, 'vectors', 'auto ignores a chat override',
     '(_, Some(visibility)) => (visibility, "chat"),',
     '(_, Some(_)) => (global, "global"),'),
    (K, 'vectors', 'explicit policy is labeled global',
     '(Self::Request(visibility), _) => (visibility, "request")',
     '(Self::Request(visibility), _) => (visibility, "global")'),
    (K, 'vectors', 'an absent override follows hide all the time',
     '_ => (global, "global"),', '_ => (Visibility::Hide, "global"),'),
    (K, 'vectors', 'show tells the caller its rows are hidden',
     'hidden: visibility == Visibility::Hide', 'hidden: visibility == Visibility::Show'),
    (K, 'vectors', 'a user cannot clear a chat override',
     'eq_ignore_ascii_case("default")', 'eq_ignore_ascii_case("never")'),
    (K, 'host', 'unknown policy query silently defaults to show',
     '_ => Err(MODE_ERROR),', '_ => Ok(None),'),
    (K, 'host', 'a delta forgets the cache was read under show',
     '!read.eq_ignore_ascii_case(self.visibility.word())',
     'read.eq_ignore_ascii_case(self.visibility.word())'),
    ('clarp-host/src/coordination.rs', 'host', 'a request from a peer can set visibility',
     '[crate::turn_identity::HEADER, PEER_HEADER]', '[crate::turn_identity::HEADER, "x-clarp-unrelated"]'),
    ('clarp-host/src/coordination.rs', 'host', 'a request from a turn can set visibility',
     '[crate::turn_identity::HEADER, PEER_HEADER]', '["x-clarp-unrelated", PEER_HEADER]'),
    (F, 'host', 'a settings write emits no roster change',
     'self.works.hub.publish(event, self.now_ms());\n        Ok(effective)',
     'let _ = event;\n        Ok(effective)'),
    (E, 'host', 'the log echoes no policy',
     'answer["coordination"] = json!(policy);', 'let _ = policy;'),
    (E, 'host', 'hide leaves the original reader in place',
     'which.hide_coordination()', 'which'),
    (E, 'host', 'a snapshot shows the global instead of the effective policy',
     'row["coordination_visibility"] = json!(visibility);',
     'row["coordination_visibility"] = json!(global);'),
    (E, 'host', 'a snapshot shows an empty string for a default override',
     'row["coordination_override"] = json!(choice);',
     'row["coordination_override"] = json!(choice.map_or("", |choice| choice.word()));'),
    ('clarp-actor/src/lib.rs', 'actor', 'a turn without notes can become hidden later',
     'coordination: if self.profile.hides_coordination {',
     'coordination: if true {'),
    (C, 'host', 'a note from a marked turn is hidden',
     'if matches!(entry, Entry::Note(_)) {',
     'if matches!(entry, Entry::Note(_)) && item.turn.is_none() {'),
    (C, 'counts', 'the message cap reads a hundred and one',
     'const COUNT_CAP: usize = 100;', 'const COUNT_CAP: usize = 101;'),
    (C, 'counts', 'the row cap reads a hundred fewer',
     'const COUNT_ROW_CAP: usize = 400;', 'const COUNT_ROW_CAP: usize = 300;'),
    (C, 'counts', 'a capped tool row makes the run look longer',
     'self.run.capped |= counted;', 'self.run.capped |= counted || self.rows == COUNT_ROW_CAP;'),
    (C, 'counts', 'a counted message beyond the row cap does not say capped',
     'self.run.capped |= counted;', 'self.run.capped |= counted && self.rows < COUNT_ROW_CAP;'),
    (C, 'counts', 'the exchange count forgets the turn keys',
     'self.run.exchanges = self.turns.len();', 'self.run.exchanges = self.run.messages;'),
    (C, 'counts', 'the sender set excludes the known sender',
     'if !sender.is_empty() {', 'if sender.is_empty() {'),
    (C, 'host', 'only delta pages have tail counts',
     'let tail = before == Seq::MAX;', 'let tail = before != Seq::MAX;'),
    (H, 'host', 'a visible note carries no count for the gap',
     'row["hidden_before"] = public(run);', 'let _ = run;'),
    (H, 'host', 'an answer does not describe the hidden tail',
     'answer["hidden_after"] = public(run);', 'let _ = run;'),
]

if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))

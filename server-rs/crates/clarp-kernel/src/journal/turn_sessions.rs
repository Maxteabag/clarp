//! Runs of turns with the same native session. A queued turn can start out
//! of numeric order, and a retry can bind that same turn to a different session.
use crate::{agent::AgentEvent, turn::TurnEvent};
use std::collections::BTreeMap;

pub(super) fn affected(event: &AgentEvent) -> Option<u64> {
    match event {
        AgentEvent::Turn {
            turn,
            change: TurnEvent::Accepted { .. } | TurnEvent::Started { .. } | TurnEvent::Bound { .. },
        } => Some(turn.0),
        _ => None,
    }
}

#[derive(Default)]
pub(super) struct TurnSessions(BTreeMap<u64, Option<String>>);

impl TurnSessions {
    pub(super) fn get(&self, turn: u64) -> Option<&str> {
        self.0
            .range(..=turn)
            .next_back()
            .and_then(|(_, s)| s.as_deref())
    }

    pub(super) fn set(&mut self, turn: u64, session: Option<&str>) {
        if self.get(turn) == session {
            return;
        }
        // Preserve the following turn even on a late rebind. Adjacent turns
        // sharing a session keep just one start and one end in the index.
        if let Some(next) = turn.checked_add(1) {
            let following = self.get(next);
            if following == session {
                self.0.remove(&next);
            } else {
                let value = following.map(str::to_owned);
                self.0.entry(next).or_insert(value);
            }
        }
        let previous = self
            .0
            .range(..turn)
            .next_back()
            .and_then(|(_, s)| s.as_deref());
        if previous == session {
            self.0.remove(&turn);
        } else {
            self.0.insert(turn, session.map(str::to_owned));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn a_late_native_binding_updates_only_its_own_turn() {
        let mut history = TurnSessions::default();
        for n in 1..=1000 {
            history.set(n, Some("first"));
        }
        assert_eq!(history.0.len(), 2);
        history.set(300, Some("retry"));
        assert_eq!(history.get(299), Some("first"));
        assert_eq!(history.get(300), Some("retry"));
        assert_eq!(history.get(301), Some("first"));
        assert_eq!(history.get(1001), None);
        history.set(300, Some("first"));
        assert_eq!(history.0.len(), 2);
    }
}

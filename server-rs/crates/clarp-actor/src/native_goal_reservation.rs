//! Admit the next autonomous Codex execution without inventing a chat message.

use clarp_kernel::conversation::Message;
use clarp_kernel::turn::{Admission, Origin};

use super::native_goal::NativeGoal;
use super::{Actor, ActorError, Marks, Outputs, Sent};

impl Actor {
    /// Background admission respects a held queue and never claims a turn
    /// ahead of a message that was already waiting. Account admission still
    /// applies to the resulting launch order.
    pub fn reserve_native_goal(
        &mut self,
        native: &NativeGoal,
        now_ms: i64,
    ) -> Result<Option<(Sent, Outputs)>, ActorError> {
        self.require_here()?;
        let agent = self.recorder.agent();
        if agent.live().is_some() || agent.is_paused() || agent.waiting().next().is_some() {
            return Ok(None);
        }
        let turn = agent.next_turn().0;
        let id = format!("native-goal-{turn}");
        let message = Message {
            id: id.clone(),
            origin: Origin::NativeGoal,
            trace_id: Some(id),
            unminted: true,
            ..Message::default()
        };
        self.take(
            &message,
            Admission::Background,
            vec![super::native_goal::item(native, now_ms)?],
            None,
            &Marks::default(),
            now_ms,
        )
        .map(Some)
    }
}

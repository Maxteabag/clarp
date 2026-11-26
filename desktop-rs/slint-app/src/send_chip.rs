//! The chip just above the message field while the agent works with
//! messages waiting (feature `send_while_busy`, #920): "1 waiting · Steer
//! now", or the Host's line for a send it handled otherwise for a few
//! seconds. Its state is the engine's (`send_chip`, `send_notice`), read
//! from the loaded turn queue; its button is the queue's own Steer now and
//! take over. While it shows, it stands in for the queue strip: one place
//! says what waits.

use clarp_core::send_while_busy::ChipAction;
use clarp_engine::Engine;
use slint::{ModelRc, VecModel};

use crate::PaneView;

pub fn fill(engine: &Engine, view: &mut PaneView, session: &str) {
    let chip = engine.send_chip(session);
    let (count, action, id) = match chip.as_ref().map(|c| (c.count, c.action.clone())) {
        Some((count, Some(ChipAction::Steer(id)))) => (count, "steer", id),
        Some((count, Some(ChipAction::TakeOver(id)))) => (count, "take-over", id),
        Some((count, None)) => (count, "", String::new()),
        None => (0, "", String::new()),
    };
    view.send_chip_count = count as i32;
    view.send_chip_action = action.into();
    view.send_chip_id = id.into();
    view.send_notice = engine.send_notice(session).into();
    // The chip says what waits: the strip's list stays away meanwhile.
    if count > 0 {
        view.waiting = ModelRc::new(VecModel::from(Vec::<crate::WaitingTurn>::new()));
    }
}

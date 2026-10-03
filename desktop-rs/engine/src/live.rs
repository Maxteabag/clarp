//! Live items (docs/live-items.md §7) on the engine: the `live_items`
//! feature, the `/events?live=` subscription, `GET /live` snapshots and the
//! `live` events, one [`LiveView`] per chat.

use clarp_core::live::LiveView;

use crate::Engine;

impl Engine {
    /// The Host sends live items (`live_items` in `/server-info`).
    pub fn live_items(&self) -> bool {
        false
    }

    /// The chat's live state, once the feature is on and it was fetched.
    pub fn live_view(&self, _session: &str) -> Option<&LiveView> {
        None
    }
}

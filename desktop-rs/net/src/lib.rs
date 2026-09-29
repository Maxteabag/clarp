//! Network clients for the Clarp Host: tagged JSON/bytes requests and the
//! single `/events` stream. Results go to a caller-supplied sink so the Qt
//! layer can queue them onto its own thread; nothing here depends on Qt.

mod api;
mod sse;

pub use api::{ApiClient, ApiReply};
pub use sse::{SseClient, SseSignal};

pub const USER_AGENT: &str = "ClarpNativeDesktop/0.1";

//! Network clients for the Clarp Host: tagged JSON/bytes requests, the
//! single `/events` stream, and the desktop keyring that holds device tokens. Results go to a caller-supplied sink so the Qt
//! layer can queue them onto its own thread; nothing here depends on Qt.

mod api;
pub mod credentials;
pub mod pairing;
mod sse;

pub use api::{ApiClient, ApiReply};
pub use sse::{SseClient, SseSignal};

pub const USER_AGENT: &str = "ClarpNativeDesktop/0.1";

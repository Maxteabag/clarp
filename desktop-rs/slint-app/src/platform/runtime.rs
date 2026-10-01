//! The tokio runtime the platform services run on: the engine's, handed
//! over at startup. Results come back to the UI thread through
//! `slint::invoke_from_event_loop`.

use std::sync::OnceLock;
use std::time::Duration;

use tokio::runtime::Handle;

static HANDLE: OnceLock<Handle> = OnceLock::new();

pub fn set(handle: Handle) {
    if HANDLE.set(handle).is_err() {
        eprintln!("clarp-slint: the platform runtime was already set");
    }
}

pub fn handle() -> Handle {
    HANDLE.get().expect("the platform runtime is set at startup").clone()
}

/// Run `work` after `delay` on the runtime (it must queue itself to the UI).
pub fn after(delay: Duration, work: impl FnOnce() + Send + 'static) {
    handle().spawn(async move {
        tokio::time::sleep(delay).await;
        work();
    });
}

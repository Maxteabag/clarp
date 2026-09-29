//! The one tokio runtime behind every network client and timer. Results are
//! queued back onto the Qt thread by the object that started the work.

use std::sync::OnceLock;
use std::time::Duration;

use tokio::runtime::{Handle, Runtime};

pub fn handle() -> Handle {
    static RUNTIME: OnceLock<Runtime> = OnceLock::new();
    RUNTIME
        .get_or_init(|| {
            tokio::runtime::Builder::new_multi_thread()
                .worker_threads(2)
                .thread_name("clarp-net")
                .enable_all()
                .build()
                .expect("the network runtime starts")
        })
        .handle()
        .clone()
}

/// Run `work` after `delay` on the runtime (it must queue itself to Qt).
pub fn after(delay: Duration, work: impl FnOnce() + Send + 'static) {
    handle().spawn(async move {
        tokio::time::sleep(delay).await;
        work();
    });
}

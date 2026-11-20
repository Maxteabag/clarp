//! One explicit request to fetch an immutable model snapshot. The Host supplies the
//! catalog coordinates and an open staging directory; HTTP inputs never name a source or path.

use std::fs::File;
use std::io;

pub struct DownloadRequest {
    pub repository: &'static str,
    pub revision: &'static str,
    /// The Host has opened and checked this model's own staging directory.
    pub staging: File,
    /// Total staged payload, including bytes retained from an interrupted attempt.
    pub maximum_bytes: u64,
}

/// A run owns its child process group until it has exited. Dropping a live run must
/// terminate and reap it. Success means all bytes have been verified in staging;
/// publication and job state remain the Host's responsibility.
pub trait DownloadRun: Send {
    /// Pending, complete, or failed with a public diagnostic.
    fn try_complete(&mut self) -> io::Result<Option<()>>;
    fn cancel(&mut self) -> io::Result<()>;
}

pub trait ModelDownloader: Send + Sync {
    fn start(&self, request: DownloadRequest) -> io::Result<Box<dyn DownloadRun>>;
}

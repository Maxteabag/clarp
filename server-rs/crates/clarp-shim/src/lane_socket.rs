//! Private, reconnectable Unix sockets for a lane and its admitted turn, whatever tool the lane
//! keeps: each request read is handed to the lane's one loop as the lane's own input.

use std::fs;
use std::io::{self, BufRead, BufReader, Read, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::SyncSender;
use std::time::Duration;

use serde::Serialize;
use serde::de::DeserializeOwned;

use clarp_shim::{Reply, Request as TurnRequest};

use clarp_shim::lane::Claim;

const FRAME_MOST: u64 = 8 * 1024 * 1024;
const TIME: Duration = Duration::from_secs(3);

/// Stop the accept thread with a private connection before removing the path.
/// Its connections are owned by the supervisor and can outlive this listener.
pub(super) struct Socket {
    path: PathBuf,
    closing: Arc<AtomicBool>,
}

/// What came on an admitted attempt's socket.
pub(super) enum TurnAsk {
    Follow,
    /// A Stop, with how long the tool has to end the turn itself.
    Stop(u64),
    /// Steered words, under the id their reading is told by.
    Steer(String, String),
}

impl Socket {
    /// The lane's control socket: each request, as the lane's driver reads it, goes to its loop.
    pub(super) fn control<R, I>(
        path: &Path,
        input: SyncSender<I>,
        wrap: fn(R, UnixStream) -> I,
    ) -> io::Result<Self>
    where
        R: DeserializeOwned + 'static,
        I: Send + 'static,
    {
        Self::listen(path, move |stream| {
            if let Ok(request) = read::<R>(&stream) {
                let _ = input.send(wrap(request, stream));
            }
        })
    }

    /// The socket of the attempt `claim` names, for as long as the lane runs it.
    pub(super) fn turn<I: Send + 'static>(
        claim: &Claim,
        input: SyncSender<I>,
        wrap: fn((u64, u32), TurnAsk, UnixStream) -> I,
    ) -> io::Result<Self> {
        let attempt = (claim.turn, claim.attempt);
        Self::listen(&claim.socket, move |mut stream| {
            let ask = match read::<TurnRequest>(&stream) {
                Ok(TurnRequest::Follow) => TurnAsk::Follow,
                Ok(TurnRequest::Stop { grace_ms }) => TurnAsk::Stop(grace_ms),
                Ok(TurnRequest::Steer { id, text }) => TurnAsk::Steer(id, text),
                _ => {
                    let _ = write(&mut stream, &Reply::failed("Unsupported lane turn request"));
                    return;
                }
            };
            let _ = input.send(wrap(attempt, ask, stream));
        })
    }

    fn listen(
        path: &Path,
        receive: impl Fn(UnixStream) + Send + Sync + 'static,
    ) -> io::Result<Self> {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        let listener = UnixListener::bind(path)?;
        let socket = Self {
            path: path.to_owned(),
            closing: Arc::new(AtomicBool::new(false)),
        };
        fs::set_permissions(path, fs::Permissions::from_mode(0o600))?;
        let closing = socket.closing.clone();
        let receive = Arc::new(receive);
        std::thread::Builder::new()
            .name("lane-listener".into())
            .spawn(move || {
                for stream in listener.incoming() {
                    if closing.load(Ordering::Acquire) {
                        break;
                    }
                    let Ok(stream) = stream else { break };
                    let receive = receive.clone();
                    let _ = std::thread::Builder::new()
                        .name("lane-request".into())
                        .spawn(move || {
                            let _ = stream.set_read_timeout(Some(TIME));
                            let _ = stream.set_write_timeout(Some(TIME));
                            receive(stream);
                        });
                }
            })?;
        Ok(socket)
    }
}

impl Drop for Socket {
    fn drop(&mut self) {
        self.closing.store(true, Ordering::Release);
        let _ = UnixStream::connect(&self.path);
        let _ = fs::remove_file(&self.path);
    }
}

fn read<T: DeserializeOwned>(stream: &UnixStream) -> io::Result<T> {
    let mut line = Vec::new();
    BufReader::new(stream)
        .take(FRAME_MOST)
        .read_until(b'\n', &mut line)?;
    if line.last() != Some(&b'\n') {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "Incomplete lane request",
        ));
    }
    serde_json::from_slice(&line).map_err(io::Error::other)
}

pub(super) fn write(stream: &mut UnixStream, value: &impl Serialize) -> io::Result<()> {
    let mut frame = serde_json::to_vec(value).map_err(io::Error::other)?;
    frame.push(b'\n');
    stream.write_all(&frame)
}

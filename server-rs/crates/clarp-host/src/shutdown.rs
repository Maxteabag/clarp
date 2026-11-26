//! Serve HTTP with a bounded connection drain when this process stops.

use std::future::IntoFuture;
use std::io::{self, IoSlice};
use std::net::SocketAddr;
use std::pin::Pin;
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::task::{Context, Poll};
use std::time::Duration;

use axum::Router;
use axum::serve::{Listener, ListenerExt};
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::oneshot;

const DRAIN: Duration = Duration::from_secs(5);

/// `hub`: told first when the Host is asked to stop, so that every event stream ends before the
/// drain begins. A stream never ends by itself, and kept the drain open to its end: apps came
/// back 6.8 s after a stop, against 1.9 s after a kill (issue 880).
pub(super) async fn serve(
    listener: TcpListener,
    router: Router,
    hub: Arc<clarp_host::hub::Hub>,
) -> io::Result<()> {
    let open = Arc::new(AtomicUsize::new(0));
    let listener = CountingListener {
        listener,
        open: open.clone(),
    };
    let service = router.into_make_service_with_connect_info::<SocketAddr>();
    let (stop, stopping) = oneshot::channel();
    // Axum provides SocketAddr connect info for custom listeners through TapIo.
    let server = axum::serve(listener.tap_io(|_| {}), service)
        .with_graceful_shutdown(async move {
            let _ = stopping.await;
        })
        .into_future();
    tokio::pin!(server);

    tokio::select! {
        biased;
        () = told_to_stop() => {
            let deadline = tokio::time::Instant::now() + DRAIN;
            hub.close_streams();
            let _ = stop.send(());
            match tokio::time::timeout_at(deadline, server).await {
                Ok(result) => result,
                Err(_) => {
                    eprintln!(
                        "clarp-host: HTTP shutdown deadline reached with {} open connections",
                        open.load(Ordering::Relaxed)
                    );
                    Ok(())
                }
            }
        }
        result = &mut server => result,
    }
}

/// Count the lifetime of each accepted socket, including when it carries an upgrade.
struct CountingListener {
    listener: TcpListener,
    open: Arc<AtomicUsize>,
}

impl Listener for CountingListener {
    type Io = Connection;
    type Addr = SocketAddr;

    async fn accept(&mut self) -> (Connection, SocketAddr) {
        let (stream, addr) = Listener::accept(&mut self.listener).await;
        self.open.fetch_add(1, Ordering::Relaxed);
        let connection = Connection {
            stream,
            open: self.open.clone(),
        };
        (connection, addr)
    }

    fn local_addr(&self) -> io::Result<SocketAddr> {
        self.listener.local_addr()
    }
}

struct Connection {
    stream: TcpStream,
    open: Arc<AtomicUsize>,
}

impl Drop for Connection {
    fn drop(&mut self) {
        self.open.fetch_sub(1, Ordering::Relaxed);
    }
}

impl AsyncRead for Connection {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        Pin::new(&mut self.stream).poll_read(cx, buf)
    }
}

impl AsyncWrite for Connection {
    fn poll_write(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<io::Result<usize>> {
        Pin::new(&mut self.stream).poll_write(cx, buf)
    }

    fn poll_write_vectored(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        bufs: &[IoSlice<'_>],
    ) -> Poll<io::Result<usize>> {
        Pin::new(&mut self.stream).poll_write_vectored(cx, bufs)
    }

    fn is_write_vectored(&self) -> bool {
        self.stream.is_write_vectored()
    }

    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.stream).poll_flush(cx)
    }

    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.stream).poll_shutdown(cx)
    }
}

/// Resolves when the Host is asked to stop. The turns that run are not asked: they go on.
async fn told_to_stop() {
    use tokio::signal::unix::{SignalKind, signal};
    let mut terminate = signal(SignalKind::terminate()).ok();
    let terminated = async {
        match terminate.as_mut() {
            Some(terminate) => {
                terminate.recv().await;
            }
            None => std::future::pending::<()>().await,
        }
    };
    tokio::select! {
        _ = tokio::signal::ctrl_c() => {}
        () = terminated => {}
    }
}

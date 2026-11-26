//! Where the Host listens: on the socket its socket unit holds, when it is started with one, or
//! on a port it binds itself.
//!
//! A release stops one Host and then starts the next (issue 935). A port the Host binds itself is
//! closed from the moment the old Host stops until the new one binds it, and refused all that time:
//! Tailscale Serve answered 502 to the apps in that gap. Held by `clarp-host.socket`, the port
//! stays open across the switch: what comes in between waits in the socket's queue, and the next
//! Host answers it. systemd hands the socket over as the Host's standard input
//! (`StandardInput=socket`), which the Host takes without unsafe code.

use std::io;
use std::net::SocketAddr;
use std::os::fd::AsFd;

/// The listening socket the Host was handed as its standard input, when it was handed one.
///
/// Taken first thing at the start, before the Host starts any other process: from then on its
/// standard input is `/dev/null`, so nothing the Host starts inherits the socket, and a turn
/// that outlives the Host never keeps its port open. A socket that listens elsewhere than
/// `--listen` says is refused: the Host does not answer on a port it was not told of.
pub(super) fn inherited(configured: SocketAddr) -> Result<Option<std::net::TcpListener>, String> {
    let stdin = io::stdin();
    // A terminal, a pipe, a file or `/dev/null` is no listening socket: the Host binds its port.
    if !rustix::net::sockopt::socket_acceptconn(stdin.as_fd()).unwrap_or(false) {
        return Ok(None);
    }
    // The copy is closed on exec: no process the Host starts has it.
    let owned = stdin
        .as_fd()
        .try_clone_to_owned()
        .map_err(|error| format!("the socket on standard input cannot be taken: {error}"))?;
    let listener = std::net::TcpListener::from(owned);
    let at = listener
        .local_addr()
        .map_err(|error| format!("the socket on standard input is no TCP socket: {error}"))?;
    if at != configured {
        return Err(format!(
            "the socket on standard input listens on {at}, not on {configured} as --listen says"
        ));
    }
    let null = std::fs::File::open("/dev/null")
        .map_err(|error| format!("/dev/null cannot be opened: {error}"))?;
    rustix::stdio::dup2_stdin(&null)
        .map_err(|error| format!("standard input cannot be let go of: {error}"))?;
    listener
        .set_nonblocking(true)
        .map_err(|error| format!("the socket on standard input: {error}"))?;
    Ok(Some(listener))
}

/// The socket the Host listens on: the one it was handed, else its own port bound now.
pub(super) async fn listen(
    configured: SocketAddr,
    inherited: Option<std::net::TcpListener>,
) -> Result<tokio::net::TcpListener, String> {
    match inherited {
        Some(listener) => tokio::net::TcpListener::from_std(listener)
            .map_err(|error| format!("{configured}: {error}")),
        None => tokio::net::TcpListener::bind(configured)
            .await
            .map_err(|error| format!("{configured}: {error}")),
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    /// What `inherited` judges, on a socket given to it rather than on standard input.
    fn judged(fd: std::os::fd::BorrowedFd<'_>) -> bool {
        rustix::net::sockopt::socket_acceptconn(fd).unwrap_or(false)
    }

    #[test]
    fn only_a_listening_socket_is_taken_for_one() {
        let listening = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        assert!(judged(listening.as_fd()));
        let connected = std::net::TcpStream::connect(listening.local_addr().unwrap()).unwrap();
        assert!(!judged(connected.as_fd()));
        let file = std::fs::File::open("/dev/null").unwrap();
        assert!(!judged(file.as_fd()));
    }
}

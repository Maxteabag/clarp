use std::net::{TcpStream, ToSocketAddrs};
use std::time::{Duration, Instant};

pub(crate) fn open(
    request: tungstenite::handshake::client::Request,
    timeout: Duration,
) -> Result<tungstenite::WebSocket<tungstenite::stream::MaybeTlsStream<TcpStream>>, &'static str> {
    let uri = request.uri();
    let host = uri
        .host()
        .ok_or("missing speech endpoint host")?
        .trim_matches(['[', ']']);
    let port = uri.port_u16().unwrap_or(if uri.scheme_str() == Some("ws") {
        80
    } else {
        443
    });
    let until = Instant::now() + timeout;
    let socket = (host, port)
        .to_socket_addrs()
        .map_err(|_| "could not resolve speech endpoint")?
        .find_map(|address| {
            TcpStream::connect_timeout(&address, until.saturating_duration_since(Instant::now()))
                .ok()
        })
        .ok_or("WS connect failed")?;
    socket
        .set_read_timeout(Some(timeout))
        .and_then(|()| socket.set_write_timeout(Some(timeout)))
        .map_err(|_| "WS timeout configuration failed")?;
    tungstenite::client_tls(request, socket)
        .map(|(ws, _)| ws)
        .map_err(|_| "WS connect failed")
}

//! `clarp-hook NAME`: a tool's hook reports what it saw, through the shim, and gets out of the way.
//!
//! The tool runs this for each of its hook events with the event as JSON on standard input. It
//! passes the event to the shim named by `CLARP_SHIM_SOCKET`, which appends it to the spool in
//! order with the tool's own output. It always exits 0 and prints nothing: a hook is a sensor,
//! and a sensor that fails must never stop or change the turn it watches.

use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::net::UnixStream;
use std::time::Duration;

use clarp_shim::{Request, SOCKET_ENV};

fn report() -> Option<()> {
    let name = std::env::args().nth(1)?;
    let socket = std::env::var_os(SOCKET_ENV)?;
    let mut input = String::new();
    std::io::stdin().read_to_string(&mut input).ok()?;
    let data = serde_json::from_str(&input).unwrap_or(serde_json::Value::String(input));
    let mut line = serde_json::to_string(&Request::Hook { name, data }).ok()?;
    line.push('\n');
    let mut stream = UnixStream::connect(socket).ok()?;
    stream
        .set_write_timeout(Some(Duration::from_secs(2)))
        .ok()?;
    stream.set_read_timeout(Some(Duration::from_secs(2))).ok()?;
    stream.write_all(line.as_bytes()).ok()?;
    // Wait for the shim's answer so the record is in the spool before the tool moves on.
    let mut reply = String::new();
    BufReader::new(stream).read_line(&mut reply).ok()?;
    Some(())
}

fn main() {
    let _ = report();
}

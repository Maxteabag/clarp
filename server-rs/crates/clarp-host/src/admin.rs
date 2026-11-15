//! What the administrator does from the command line, by asking the Host that is running.
//!
//! The Host is the one writer of its own tables, so a command never touches them: it asks the
//! Host over the same door the apps use, with the administrator's token, from this machine.

use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::Duration;

use serde_json::{Value, json};

pub const PAIR_USAGE: &str =
    "usage: clarp-host pair --url URL [--name DEVICE] [--scope full|limited] \
[--ttl SECONDS] [--host ADDR:PORT]
URL is the address the device will reach this Host at. The administrator's token is read from
CLARP_HOST_TOKEN.";

/// Sends one request to the Host at `address` and returns the status with the body as JSON.
pub fn post(address: &str, path: &str, token: &str, body: &Value) -> Result<(u16, Value), String> {
    let mut stream = TcpStream::connect(address)
        .map_err(|error| format!("no Host answers at {address}: {error}"))?;
    let timeout = Some(Duration::from_secs(20));
    stream
        .set_read_timeout(timeout)
        .and_then(|()| stream.set_write_timeout(timeout))
        .map_err(|error| error.to_string())?;
    let body = body.to_string();
    let authorization = if token.is_empty() {
        String::new()
    } else {
        format!("Authorization: Bearer {token}\r\n")
    };
    let request = format!(
        "POST {path} HTTP/1.1\r\nHost: {address}\r\nConnection: close\r\n{authorization}Content-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}",
        body.len()
    );
    stream
        .write_all(request.as_bytes())
        .map_err(|error| error.to_string())?;
    let mut answer = String::new();
    stream
        .read_to_string(&mut answer)
        .map_err(|error| error.to_string())?;
    let (head, body) = answer.split_once("\r\n\r\n").unwrap_or((&answer, ""));
    let status = head
        .split_whitespace()
        .nth(1)
        .and_then(|code| code.parse().ok())
        .ok_or("the Host's answer was not an HTTP response")?;
    Ok((status, serde_json::from_str(body).unwrap_or(Value::Null)))
}

/// `clarp-host pair`: has the running Host issue a pairing code, and returns what to show.
pub fn pair(mut args: impl Iterator<Item = String>, token: &str) -> Result<String, String> {
    let (mut url, mut name, mut scope, mut ttl) =
        (None, "iPhone".to_string(), "full".to_string(), 600i64);
    let mut host = "127.0.0.1:7690".to_string();
    while let Some(arg) = args.next() {
        let mut value = |what: &str| args.next().ok_or(format!("{arg} needs {what}"));
        match arg.as_str() {
            "--url" => url = Some(value("an address")?),
            "--name" => name = value("a name")?,
            "--scope" => scope = value("full or limited")?,
            "--ttl" => {
                ttl = value("a number of seconds")?
                    .parse()
                    .map_err(|_| "--ttl needs a number of seconds".to_string())?;
            }
            "--host" => host = value("an address")?,
            other => return Err(format!("unknown argument {other}")),
        }
    }
    let url = url.ok_or("pairing needs --url: the address the device will reach this Host at")?;
    let asked = json!({ "device_name": name, "scope": scope, "ttl_seconds": ttl, "url": url });
    let (status, answer) = post(&host, "/pairing/issue", token, &asked)?;
    if status != 200 {
        let why = answer["error"].as_str().unwrap_or("no reason given");
        return Err(format!("the Host refused ({status}): {why}"));
    }
    let uri = answer["uri"]
        .as_str()
        .ok_or("the Host's answer had no pairing link")?;
    Ok(format!(
        "{uri}\nOpen this link on the device. It expires in {ttl} seconds and works once."
    ))
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    fn words(text: &str) -> impl Iterator<Item = String> + use<> {
        text.split_whitespace()
            .map(str::to_string)
            .collect::<Vec<_>>()
            .into_iter()
    }

    #[test]
    fn a_pairing_needs_an_address_and_a_host_that_answers() {
        assert!(pair(words(""), "").unwrap_err().contains("--url"));
        assert!(
            pair(words("--ttl soon --url http://x"), "")
                .unwrap_err()
                .contains("seconds")
        );
        assert!(
            pair(words("--what"), "")
                .unwrap_err()
                .contains("unknown argument")
        );
        // Nothing listens on port 1.
        let nobody = pair(words("--url http://x --host 127.0.0.1:1"), "");
        assert!(nobody.unwrap_err().contains("no Host answers"));
    }
}

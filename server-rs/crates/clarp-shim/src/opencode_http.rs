//! The HTTP side of an `opencode serve` on loopback: where it says it listens, the requests a
//! turn makes of it, and its event stream. Used by the per-turn driver and by the OpenCode lane,
//! the only two processes that know a server's password.
use std::io::{self, BufReader, Read};
use std::sync::mpsc;
use std::time::Duration;

use base64::{Engine as _, engine::general_purpose::STANDARD};
use serde_json::{Value, json};

pub(crate) type Error = Box<dyn std::error::Error + Send + Sync>;
pub(crate) type Result<T> = std::result::Result<T, Error>;

// An announcement is two whitespace-delimited tokens: "on" follows "listening",
// then the loopback URL. A chunk boundary never ends a token.
pub(crate) fn port(stdout: impl Read + Send + 'static) -> Option<u16> {
    let (send, receive) = mpsc::sync_channel(1);
    std::thread::spawn(move || {
        let mut input = BufReader::new(stdout);
        let mut token = Vec::new();
        let mut stage = 0;
        let mut chunk = [0u8; 4096];
        while let Ok(n) = input.read(&mut chunk) {
            for b in chunk[..n].iter().copied().chain((n == 0).then_some(b' ')) {
                if !b.is_ascii_whitespace() {
                    if token.len() <= 8192 {
                        token.push(b);
                    }
                    continue;
                }
                if token.is_empty() {
                    continue;
                }
                let word = std::str::from_utf8(&token).unwrap_or_default();
                if stage == 2
                    && token.len() <= 8192
                    && let Some(port) = word
                        .strip_prefix("http://127.0.0.1:")
                        .and_then(|s| s.trim_end_matches('/').parse::<u16>().ok())
                        .filter(|n| *n != 0)
                {
                    let _ = send.send(port);
                    let _ = io::copy(&mut input, &mut io::sink());
                    return;
                }
                stage = match (stage, word) {
                    (_, "listening") => 1,
                    (1, "on") => 2,
                    _ => 0,
                };
                token.clear();
            }
            if n == 0 {
                break;
            }
        }
    });
    receive.recv_timeout(Duration::from_secs(30)).ok()
}

#[derive(Clone)]
pub(crate) struct Client {
    base: String,
    auth: String,
    directory: String,
}

pub(crate) fn component(value: &str) -> String {
    let mut out = String::new();
    for b in value.bytes() {
        if b.is_ascii_alphanumeric() || b"-_.~".contains(&b) {
            out.push(b as char);
        } else {
            use std::fmt::Write;
            let _ = write!(out, "%{b:02X}");
        }
    }
    out
}

impl Client {
    /// The client of a server on loopback `port`, behind `password`, for the agent's `directory`.
    pub(crate) fn new(port: u16, password: &str, directory: String) -> Self {
        Self {
            base: format!("http://127.0.0.1:{port}"),
            directory,
            auth: format!(
                "Basic {}",
                STANDARD.encode(format!("opencode:{password}").as_bytes())
            ),
        }
    }

    pub(crate) fn get(&self, path: &str, seconds: u64) -> Result<Value> {
        Self::json(
            self.request(minreq::Method::Get, path)
                .with_timeout(seconds),
        )
    }

    /// Ends what the session runs now. OpenCode answers once the session's loop was told.
    pub(crate) fn abort(&self, session: &str, seconds: u64) -> Result<Value> {
        self.post(
            &format!("/session/{}/abort", component(session)),
            None,
            seconds,
        )
    }

    fn url(&self, path: &str) -> String {
        let joined = if path.contains('?') { '&' } else { '?' };
        format!(
            "{}{path}{joined}directory={}",
            self.base,
            component(&self.directory)
        )
    }

    fn request(&self, method: minreq::Method, path: &str) -> minreq::Request {
        minreq::Request::new(method, self.url(path))
            .with_follow_redirects(false)
            .with_max_headers_size(64 * 1024)
            .with_max_status_line_length(4096)
            .with_header("Authorization", &self.auth)
    }

    fn response(req: minreq::Request) -> Result<minreq::ResponseLazy> {
        let reply = req.send_lazy()?;
        if !(200..300).contains(&reply.status_code) {
            return Err(format!("OpenCode server returned HTTP {}", reply.status_code).into());
        }
        Ok(reply)
    }

    fn json(req: minreq::Request) -> Result<Value> {
        const LIMIT: u64 = 10 * 1024 * 1024;
        let mut bytes = Vec::new();
        let reply = Self::response(req)?;
        let length = reply
            .headers
            .get("content-length")
            .map(|value| value.parse::<u64>())
            .transpose()?;
        if length.is_some_and(|n| n > LIMIT) {
            return Err("OpenCode response exceeds ten MiB".into());
        }
        Read::take(reply, LIMIT + 1).read_to_end(&mut bytes)?;
        if length.is_some_and(|n| n != bytes.len() as u64) {
            return Err("incomplete OpenCode response".into());
        }
        if bytes.len() as u64 > LIMIT {
            return Err("OpenCode response exceeds ten MiB".into());
        }
        if bytes.iter().all(u8::is_ascii_whitespace) {
            Ok(Value::Null)
        } else {
            Ok(serde_json::from_slice(&bytes)?)
        }
    }

    pub(crate) fn post(&self, path: &str, body: Option<&Value>, seconds: u64) -> Result<Value> {
        let mut req = self
            .request(minreq::Method::Post, path)
            .with_timeout(seconds);
        if let Some(body) = body {
            req = req
                .with_header("Content-Type", "application/json")
                .with_body(serde_json::to_vec(body)?);
        }
        Self::json(req)
    }

    /// Asks the server to delete what `path` names; its answer's status, whatever it is.
    pub(crate) fn delete(&self, path: &str, seconds: u64) -> Result<i32> {
        let reply = self
            .request(minreq::Method::Delete, path)
            .with_timeout(seconds)
            .send()?;
        Ok(reply.status_code)
    }

    pub(crate) fn subscribe(&self) -> Result<mpsc::Receiver<Value>> {
        let req = self
            .request(minreq::Method::Get, "/event")
            .with_header("Accept", "text/event-stream");
        let (opened, ready) = mpsc::channel();
        let (send, receive) = mpsc::sync_channel(8);
        std::thread::spawn(move || match Self::response(req) {
            Ok(stream) => {
                if opened.send(Ok(())).is_ok() {
                    events(stream, send);
                }
            }
            Err(err) => {
                let _ = opened.send(Err(err));
            }
        });
        ready.recv_timeout(Duration::from_secs(30))??;
        Ok(receive)
    }

    pub(crate) fn open_session(&self, old: &str) -> Result<String> {
        if old.is_empty() {
            return self.create_session(&["question", "plan_enter", "plan_exit"]);
        }
        if !clarp_tool_args::native_session_name(old) {
            return Err(format!("OpenCode session {old} not found").into());
        }
        let path = format!("/session/{}", component(old));
        let result = Self::json(self.request(minreq::Method::Get, &path).with_timeout(30))
            .map_err(|_| format!("OpenCode session {old} not found"))?;
        if result["id"].as_str() != Some(old) {
            return Err(format!("OpenCode session {old} not found").into());
        }
        Ok(old.to_string())
    }

    pub(crate) fn open_conversation(&self) -> Result<String> {
        self.create_session(&["*"])
    }

    fn create_session(&self, deny: &[&str]) -> Result<String> {
        let rules: Vec<_> = deny
            .iter()
            .map(|rule| json!({"permission":rule,"pattern":"*","action":"deny"}))
            .collect();
        let result = self.post("/session", Some(&json!({"permission":rules})), 30)?;
        result["id"]
            .as_str()
            .filter(|id| clarp_tool_args::native_session_name(id))
            .map(str::to_owned)
            .ok_or_else(|| "OpenCode did not create a session".into())
    }
}

// ResponseLazy decodes HTTP chunking incrementally. Its Read implementation waits
// to fill the caller's buffer, so take decoded bytes directly: the next event
// must be delivered even when the peer is now waiting for a permission reply.
pub(crate) fn events(stream: minreq::ResponseLazy, send: mpsc::SyncSender<Value>) {
    const LIMIT: usize = 4 * 1024 * 1024;
    let mut line = Vec::new();
    let mut data = Vec::new();
    for byte in stream {
        let Ok((byte, _)) = byte else { break };
        if byte != b'\n' {
            if line.len() >= LIMIT {
                break;
            }
            line.push(byte);
            continue;
        }
        let payload = line.strip_suffix(b"\r").unwrap_or(&line);
        if payload.is_empty() {
            if let Ok(event) = serde_json::from_slice(&data)
                && send.send(event).is_err()
            {
                break;
            }
            data.clear();
        } else if let Some(text) = payload.strip_prefix(b"data:") {
            let text = text.trim_ascii_start();
            if data.len() + text.len() + 1 > LIMIT {
                break;
            }
            data.extend_from_slice(text);
            data.push(b'\n');
        }
        line.clear();
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn announcements_are_loopback_only_bounded_and_complete() {
        let ok = "listening on http://127.0.0.1:12345";
        for (data, want) in [
            (ok.to_owned(), Some(12345)),
            (format!("{} {ok}/\nnoise", "x".repeat(40000)), Some(12345)),
            ("listening on http://192.0.2.9:10".to_owned(), None),
            ("listening on http://127.0.0.1:65536".to_owned(), None),
            ("listening on http://127.0.0.1:0".to_owned(), None),
            ("listening on http://127.0.0.1:12@outside".to_owned(), None),
            (format!("{ok}{}\n{ok}", "/".repeat(8192)), Some(12345)),
            (format!("{ok}{}", "/".repeat(8192)), None),
            (format!("{} {ok}", "x".repeat(4096 - 12)), Some(12345)),
            (String::new(), None),
        ] {
            assert_eq!(port(io::Cursor::new(data.into_bytes())), want);
        }
    }

    // A loopback server that captures the request and answers once. Redirects,
    // bad statuses and truncated bodies are tested without any provider.
    fn answering(reply: Vec<u8>) -> (Client, std::thread::JoinHandle<String>) {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = std::thread::spawn(move || {
            use std::io::BufRead;
            let (mut socket, _) = listener.accept().unwrap();
            socket
                .set_read_timeout(Some(Duration::from_secs(3)))
                .unwrap();
            let mut reader = BufReader::new(&socket);
            let mut request = String::new();
            loop {
                let mut line = String::new();
                if reader.read_line(&mut line).unwrap() == 0 || line == "\r\n" {
                    break;
                }
                request.push_str(&line);
            }
            let length = request
                .lines()
                .find_map(|line| {
                    let (name, value) = line.split_once(':')?;
                    name.eq_ignore_ascii_case("content-length")
                        .then(|| value.trim().parse::<usize>().unwrap())
                })
                .unwrap_or(0);
            let mut body = vec![0; length];
            reader.read_exact(&mut body).unwrap();
            request.push_str("\r\n");
            request.push_str(std::str::from_utf8(&body).unwrap());
            let _ = socket.write_all(&reply);
            request
        });
        (
            Client {
                base: format!("http://127.0.0.1:{port}"),
                auth: "Basic synthetic".into(),
                directory: "/a b".into(),
            },
            server,
        )
    }

    #[test]
    fn a_conversation_creates_a_fresh_session_that_denies_model_tools() {
        let body = r#"{"id":"ses_conversation"}"#;
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\n\r\n{body}",
            body.len()
        );
        let (client, server) = answering(response.into_bytes());
        assert_eq!(client.open_conversation().unwrap(), "ses_conversation");
        let request = server.join().unwrap();
        assert!(request.starts_with("POST /session?directory="));
        let (_, json) = request.split_once("\r\n\r\n").unwrap();
        let asked: Value = serde_json::from_str(json).unwrap();
        assert_eq!(
            asked,
            json!({"permission":[{"permission":"*","pattern":"*","action":"deny"}]})
        );
    }

    #[test]
    fn loopback_http_reads_chunked_responses_and_refuses_redirects_and_errors() {
        for (response, wanted) in [
            (
                "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{\"\r\n5\r\na\":1}\r\n0\r\n\r\n",
                Ok(json!({"a":1})),
            ),
            ("HTTP/1.1 204 No Content\r\n\r\n", Ok(Value::Null)),
            (
                "HTTP/1.1 302 Found\r\nLocation: /elsewhere\r\nContent-Length: 0\r\n\r\n",
                Err("302"),
            ),
            (
                "HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\n{}",
                Err("incomplete"),
            ),
            (
                "HTTP/1.1 200 OK\r\nContent-Length: 10485761\r\n\r\n",
                Err("ten MiB"),
            ),
            (
                "HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n",
                Err("401"),
            ),
        ] {
            let (client, server) = answering(response.as_bytes().to_vec());
            let result = client.post("/session", None, 3);
            match wanted {
                Ok(body) => assert_eq!(result.unwrap(), body),
                Err(code) => assert!(result.unwrap_err().to_string().contains(code)),
            }
            let headers = server.join().unwrap().to_ascii_lowercase();
            assert!(headers.starts_with("post /session?directory=%2fa%20b http/1.1\r\n"));
            assert!(headers.contains("authorization: basic synthetic\r\n"));
            assert!(!headers.contains("accept-encoding:"));
        }
    }
}

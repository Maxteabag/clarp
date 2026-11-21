//! A private `opencode serve` for one sign-in or one question about sign-in methods: on
//! 127.0.0.1, on a port of its choosing, behind a password of its own, in a process group of
//! its own. Everything here blocks: it runs on a thread of the desk's, never on the runtime.

use std::io::{BufReader, Read};
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::{Child, Stdio};
use std::sync::mpsc;
use std::time::Duration;

use clarp_kernel::harness_providers as rules;
use clarp_kernel::python;
use nix::sys::signal::{Signal, killpg};
use nix::unistd::Pid;
use serde_json::Value;

/// How long OpenCode has to say where it listens.
const STARTING: Duration = Duration::from_secs(30);
/// How long a server that was asked to end may take before it is killed.
const ENDING: Duration = Duration::from_secs(10);
/// The most of an answer that is read.
const MOST: u64 = 10 * 1024 * 1024;

/// What a request to the server needs: where it listens, its password, the directory.
#[derive(Clone)]
pub(crate) struct Client {
    base: String,
    authorization: String,
    directory: String,
}

/// A running server and the way to it. Ended when dropped.
pub(crate) struct Serve {
    child: Option<Child>,
    pub(crate) client: Client,
}

impl Serve {
    /// Starts OpenCode's server in the home of whoever runs this Host. `Err`: it did not
    /// start or did not say where it listens.
    pub(crate) fn start(program: &str, home: &Path) -> Result<Self, String> {
        let password = crate::ids::hex(24);
        let mut child = crate::child::command(program)
            .args(["serve", "--hostname", "127.0.0.1", "--port", "0"])
            .env("OPENCODE_SERVER_PASSWORD", &password)
            .current_dir(home)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .process_group(0)
            .spawn()
            .map_err(|_| rules::NOT_STARTED.to_string())?;
        let port = child.stdout.take().and_then(port);
        let mut serve = Self {
            child: Some(child),
            client: Client {
                base: String::new(),
                authorization: format!(
                    "Basic {}",
                    python::b64encode(format!("opencode:{password}").as_bytes())
                ),
                directory: home.display().to_string(),
            },
        };
        let Some(port) = port else {
            serve.close();
            return Err(rules::NOT_STARTED.to_string());
        };
        serve.client.base = format!("http://127.0.0.1:{port}");
        Ok(serve)
    }

    /// Ends the server: asked politely, then killed. Waits until it is gone.
    pub(crate) fn close(&mut self) {
        if let Some(child) = self.child.take() {
            end(child);
        }
    }
}

impl Drop for Serve {
    fn drop(&mut self) {
        self.close();
    }
}

/// Asks a process group to end, and kills it when it has not within [`ENDING`].
fn end(mut child: Child) {
    let group = i32::try_from(child.id()).map(Pid::from_raw);
    if let Ok(group) = group {
        let _ = killpg(group, Signal::SIGTERM);
    }
    let (ended, waited) = mpsc::channel();
    let waiter = std::thread::spawn(move || {
        let _ = child.wait();
        let _ = ended.send(());
    });
    if waited.recv_timeout(ENDING).is_err()
        && let Ok(group) = group
    {
        let _ = killpg(group, Signal::SIGKILL);
    }
    let _ = waiter.join();
}

/// The port of OpenCode's announcement (`... listening on http://127.0.0.1:PORT`), read in
/// bounded pieces; what follows is read and dropped so the server never blocks on its output.
fn port(out: impl Read + Send + 'static) -> Option<u16> {
    let (found, heard) = mpsc::sync_channel(1);
    std::thread::spawn(move || {
        let mut input = BufReader::new(out);
        let mut word = Vec::new();
        let mut stage = 0;
        let mut chunk = [0u8; 4096];
        loop {
            let n = input.read(&mut chunk).unwrap_or(0);
            for sign in chunk[..n].iter().copied().chain((n == 0).then_some(b' ')) {
                if !sign.is_ascii_whitespace() {
                    if word.len() <= 8192 {
                        word.push(sign);
                    }
                    continue;
                }
                if word.is_empty() {
                    continue;
                }
                let said = std::str::from_utf8(&word).unwrap_or_default();
                if stage == 2
                    && word.len() <= 8192
                    && let Some(port) = said
                        .strip_prefix("http://127.0.0.1:")
                        .and_then(|p| p.trim_end_matches('/').parse::<u16>().ok())
                        .filter(|p| *p != 0)
                {
                    let _ = found.send(port);
                    let _ = std::io::copy(&mut input, &mut std::io::sink());
                    return;
                }
                stage = match (stage, said) {
                    (_, "listening") => 1,
                    (1, "on") => 2,
                    _ => 0,
                };
                word.clear();
            }
            if n == 0 {
                return;
            }
        }
    });
    heard.recv_timeout(STARTING).ok()
}

/// A text as a part of a query.
fn component(value: &str) -> String {
    let mut out = String::new();
    for byte in value.bytes() {
        if byte.is_ascii_alphanumeric() || b"-_.~".contains(&byte) {
            out.push(char::from(byte));
        } else {
            out.push_str(&format!("%{byte:02X}"));
        }
    }
    out
}

impl Client {
    /// One request. Its answer as JSON, `Null` for none, the text where it is not JSON. An
    /// answer of 400 or above is an error that says the request and the answer's start.
    pub(crate) fn request(
        &self,
        method: &str,
        path: &str,
        body: Option<&Value>,
        seconds: u64,
    ) -> Result<Value, String> {
        let url = format!(
            "{}{path}?directory={}",
            self.base,
            component(&self.directory)
        );
        let agent: ureq::Agent = ureq::Agent::config_builder()
            .proxy(None)
            .max_redirects(0)
            .http_status_as_error(false)
            .timeout_global(Some(Duration::from_secs(seconds)))
            .build()
            .into();
        let sent = match body {
            Some(body) => agent
                .post(&url)
                .header("Authorization", &self.authorization)
                .header("Content-Type", "application/json")
                .send(body.to_string().as_bytes()),
            None => agent
                .get(&url)
                .header("Authorization", &self.authorization)
                .call(),
        };
        let mut reply = sent.map_err(|e| e.to_string())?;
        let status = reply.status().as_u16();
        let mut bytes = Vec::new();
        reply
            .body_mut()
            .as_reader()
            .take(MOST)
            .read_to_end(&mut bytes)
            .map_err(|e| e.to_string())?;
        let raw = String::from_utf8_lossy(&bytes);
        if status >= 400 {
            return Err(format!(
                "{method} {path}: HTTP {status} {}",
                python::first(&raw, 300)
            ));
        }
        if python::strip(&raw).is_empty() {
            return Ok(Value::Null);
        }
        Ok(serde_json::from_str(&raw).unwrap_or_else(|_| Value::String(raw.into_owned())))
    }
}

/// What OpenCode says its version is, in a hundred signs at most; nothing when it does not
/// answer within ten seconds.
pub(crate) fn version(program: &str) -> String {
    let child = crate::child::command(program)
        .arg("--version")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .process_group(0)
        .spawn();
    let Ok(mut child) = child else {
        return String::new();
    };
    let Some(mut out) = child.stdout.take() else {
        end(child);
        return String::new();
    };
    let (read, heard) = mpsc::channel();
    std::thread::spawn(move || {
        let mut said = Vec::new();
        let _ = (&mut out).take(64 * 1024).read_to_end(&mut said);
        let _ = read.send(said);
    });
    let said = heard
        .recv_timeout(Duration::from_secs(10))
        .unwrap_or_default();
    end(child);
    python::first(python::strip(&String::from_utf8_lossy(&said)), 100).to_string()
}

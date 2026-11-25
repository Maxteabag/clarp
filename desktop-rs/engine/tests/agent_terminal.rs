//! An agent's terminal on its Host (`clarp_net::terminal`), the loop the
//! desktop's `--agent-terminal` runs, against the fake Host's
//! `/terminal/<session>`, on pipes instead of a real terminal.

mod common;

use std::io::Write;
use std::time::Duration;

use clarp_net::terminal::{self, Ended};
use common::Host;
use serde_json::json;
use tokio::io::AsyncReadExt;

fn runtime() -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_multi_thread().worker_threads(2).enable_all().build().unwrap()
}

/// Reads the screen until `wanted` shows.
async fn shown(screen: &mut tokio::io::DuplexStream, wanted: &[u8]) -> Vec<u8> {
    let mut seen = Vec::new();
    let mut chunk = [0u8; 4096];
    let reading = async {
        while !seen.windows(wanted.len()).any(|w| w == wanted) {
            let read = screen.read(&mut chunk).await.unwrap();
            assert!(read > 0, "the screen closed before {:?} showed", String::from_utf8_lossy(wanted));
            seen.extend_from_slice(&chunk[..read]);
        }
    };
    tokio::time::timeout(Duration::from_secs(10), reading).await.expect("the Host's output shows");
    seen
}

fn until(what: &str, done: impl Fn() -> bool) {
    let deadline = std::time::Instant::now() + Duration::from_secs(10);
    while !done() {
        assert!(std::time::Instant::now() < deadline, "timed out waiting for {what}");
        std::thread::sleep(Duration::from_millis(20));
    }
}

#[test]
fn keys_and_output_cross_the_hosts_terminal_until_the_cli_ends_it() {
    let host = Host::start("agent-terminal");
    let runtime = runtime();
    let (reader, mut writer) = std::io::pipe().unwrap();
    let mut keys = terminal::read_into_channel(reader);
    let (resize, sizes) = terminal::sizes();
    let (mut screen, mut output) = tokio::io::duplex(64 * 1024);
    let Ok(opened) = runtime.block_on(terminal::open(&host.base, "rachel", "probe-token")) else { panic!("the terminal opens") };
    let carrying = runtime.spawn(async move { terminal::carry(opened, &mut keys, &mut output, (100, 30), sizes).await });

    writer.write_all(b"hello").unwrap();
    runtime.block_on(shown(&mut screen, b"echo:hello"));
    let frames = || host.requests("WS", "/terminal/rachel");
    assert_eq!(frames()[0]["text"], json!({"resize": {"cols": 100, "rows": 30}}), "the size goes first");

    resize.send((132, 43)).unwrap();
    until("the new size", || frames().iter().any(|f| f["text"] == json!({"resize": {"cols": 132, "rows": 43}})));
    writer.write_all("ünïcode".as_bytes()).unwrap();
    runtime.block_on(shown(&mut screen, "echo:ünïcode".as_bytes()));

    // Ctrl-D: the fake CLI exits, and the Host closes the terminal normally.
    writer.write_all(b"\x04").unwrap();
    let ended = runtime.block_on(async { tokio::time::timeout(Duration::from_secs(10), carrying).await }).expect("it ends").unwrap();
    assert_eq!(ended, Ended::Closed("The agent's terminal closed.".into()));
    assert!(!ended.holds());
    let keys: Vec<String> = frames().iter().filter_map(|f| f["keys"].as_str().map(str::to_owned)).collect();
    assert_eq!(keys, ["hello", "ünïcode", "\u{4}"]);
}

#[test]
fn a_refusal_is_the_hosts_own_sentence() {
    let host = Host::start("agent-terminal-refused");
    let runtime = runtime();
    let refused = |session: &str, token: &str| runtime.block_on(terminal::open(&host.base, session, token)).err();

    let turn = "Rachel is in a turn; her terminal opens when it ends.";
    host.control("/__control/terminal", json!({"status": 409, "body": turn}));
    assert_eq!(refused("rachel", "probe-token").as_deref(), Some(turn), "plain text, sent after the headers");
    host.control("/__control/terminal", json!({"status": 409, "body": {"error": turn}}));
    assert_eq!(refused("rachel", "probe-token").as_deref(), Some(turn), "JSON error");
    host.control("/__control/terminal", json!({"status": 423, "body": {"error": {"code": "agent_turn_running", "message": turn}}}));
    assert_eq!(refused("rachel", "probe-token").as_deref(), Some(turn), "JSON error object");

    host.control("/__control/terminal", json!({}));
    assert_eq!(refused("nobody", "probe-token").as_deref(), Some("no such agent"));
    assert_eq!(refused("rachel", "wrong-token").as_deref(), Some("unauthorized"));

    // A close with a reason right after the upgrade is a refusal too.
    host.control("/__control/terminal", json!({"close_reason": "The Host took the terminal back."}));
    let Ok(opened) = runtime.block_on(terminal::open(&host.base, "rachel", "probe-token")) else { panic!("the upgrade is accepted") };
    let (_writer_kept, keys) = tokio::sync::mpsc::unbounded_channel();
    let (_resize, sizes) = terminal::sizes();
    let ended = runtime.block_on(async move {
        let (mut keys, mut sink) = (keys, tokio::io::sink());
        terminal::carry(opened, &mut keys, &mut sink, (80, 24), sizes).await
    });
    assert_eq!(ended, Ended::Refused("The Host took the terminal back.".into()));
    assert!(ended.holds(), "a refusal stays until it is read");

    let unreachable = runtime.block_on(terminal::open("http://127.0.0.1:9", "rachel", "probe-token")).err().unwrap_or_default();
    assert!(unreachable.starts_with("Could not reach the Host at 127.0.0.1:9"), "{unreachable}");
}

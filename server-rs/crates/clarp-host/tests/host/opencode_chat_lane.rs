//! Issue 763. With the owner's `opencode_chat_lane` on, an OpenCode agent's first turn runs on
//! its own server while the lane starts beside it, and the turns after run on the lane's kept
//! server: no new server, each turn its own identity, a Stop that keeps the server, a turn
//! that lives through a Host restart, a server that dies under a turn, and a changed
//! configuration that starts a new lane.

use super::*;

/// A stand-in `opencode serve`, for the per-turn driver and the lane alike. Sessions are kept
/// in a file beside it, as OpenCode keeps them in its database, so a lane's server sees the
/// session another server made. A word `ask-<x>` is answered `said-ask-<x>` at once; `hold-<x>`
/// once the file `release-<x>` exists, unless the session is aborted first; `die-<x>` ends the
/// server under the turn. What it was asked is noted in files beside it.
const SERVE: &str = r#"#!/usr/bin/env python3
import base64, json, os, queue, re, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

root = Path(__file__).resolve().parent
def note(name, text):
    with (root / name).open("a") as f:
        f.write(text + "\n")
if "--version" in sys.argv:
    print("1.18.33"); sys.exit(0)
assert sys.argv[1:] == ["serve", "--hostname", "127.0.0.1", "--port", "0"], sys.argv
pid = os.getpid()
note("starts", str(pid))
PASSWORD = os.environ["OPENCODE_SERVER_PASSWORD"]
sessions_file = root / "sessions"
listeners = []
lock = threading.Lock()
aborted = threading.Event()
count = [0]

def sessions():
    return sessions_file.read_text().split() if sessions_file.exists() else []

def emit(event):
    with lock:
        for q in listeners:
            q.put(event)

def part(sid, mid, **fields):
    return {"type": "message.part.updated", "properties": {"part": {"sessionID": sid, "messageID": mid, **fields}}}

def status(sid, kind):
    return {"type": "session.status", "properties": {"sessionID": sid, "status": {"type": kind}}}

def run(sid, word):
    count[0] += 1
    mid = f"msg_{pid}_{count[0]}"
    emit(status(sid, "busy"))
    emit(part(sid, mid, id=mid + "s", type="step-start"))
    if word.startswith("die-"):
        note("died", word)
        os._exit(3)
    if word.startswith("hold-"):
        (root / ("holding-" + word[5:])).write_text("")
        end = time.monotonic() + 60
        while not (root / ("release-" + word[5:])).exists() and time.monotonic() < end:
            if aborted.wait(0.02):
                emit(status(sid, "idle"))
                return
    emit(part(sid, mid, id=mid + "t", type="text", text="said-" + word, time={"end": 1}))
    emit(part(sid, mid, id=mid + "f", type="step-finish", reason="stop", cost=0, tokens={"input": 1, "output": 1}))
    note("answered", word)
    emit(status(sid, "idle"))

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a):
        pass
    def _auth(self):
        want = "Basic " + base64.b64encode(f"opencode:{PASSWORD}".encode()).decode()
        if self.headers.get("Authorization") != want:
            self.send_response(401); self.send_header("Content-Length", "0"); self.end_headers()
            return False
        return True
    def _json(self, code, value):
        raw = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
    def do_GET(self):
        if not self._auth():
            return
        path = self.path.split("?")[0]
        if path == "/global/health":
            version = (root / "version").read_text().strip() if (root / "version").exists() else "1.18.33"
            return self._json(200, {"healthy": True, "version": version})
        if path == "/session":
            return self._json(200, [{"id": s} for s in sessions()])
        if path == "/event":
            q = queue.Queue()
            with lock:
                listeners.append(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            def send(event):
                raw = ("data: " + json.dumps(event) + "\n\n").encode()
                self.wfile.write(b"%x\r\n%s\r\n" % (len(raw), raw))
                self.wfile.flush()
            send({"type": "server.connected", "properties": {}})
            while True:
                try:
                    send(q.get(timeout=0.5))
                except queue.Empty:
                    send({"type": "server.heartbeat", "properties": {}})
        if path.startswith("/session/"):
            sid = path.split("/")[2]
            if sid in sessions():
                return self._json(200, {"id": sid})
            return self._json(404, {"name": "NotFoundError"})
        self._json(404, {})
    def do_POST(self):
        if not self._auth():
            return
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else None
        path = self.path.split("?")[0]
        if path == "/session":
            sid = f"ses_{pid}"
            with sessions_file.open("a") as f:
                f.write(sid + "\n")
            return self._json(200, {"id": sid})
        if path.endswith("/abort"):
            note("aborts", str(pid))
            aborted.set()
            return self._json(200, True)
        if path.endswith("/prompt_async"):
            sid = path.split("/")[2]
            text = body["parts"][0]["text"]
            word = re.search(r"\b(?:ask|hold|die)-[a-z0-9]+", text).group(0)
            token_file = os.environ.get("CLARP_PROVIDER_TURN_FILE", "")
            token = os.environ.get("CLARP_PROVIDER_TURN") or (Path(token_file).read_text() if token_file else "")
            note("turns", f"{pid} {sid} {word}")
            note("tokens", token)
            aborted.clear()
            threading.Thread(target=run, args=(sid, word), daemon=True).start()
            self.send_response(204); self.send_header("Content-Length", "0"); self.end_headers()
            return
        self._json(200, True)

server = ThreadingHTTPServer(("127.0.0.1", 0), H)
print(f"opencode server listening on http://127.0.0.1:{server.server_address[1]}", flush=True)
server.serve_forever()
"#;

struct World {
    place: Place,
    world: Vec<(String, String)>,
}

fn world(name: &str, settings: &str) -> World {
    let place = Place::new(name);
    let root = place.root.clone();
    let program_path = root.join("opencode");
    program(&program_path, SERVE);
    let config = root.join("config");
    std::fs::create_dir_all(config.join("clarp-host")).unwrap();
    std::fs::write(
        config.join("clarp-host/config.toml"),
        format!("opencode_chat_lane = true\n{settings}"),
    )
    .unwrap();
    let text = |path: &std::path::Path| path.to_string_lossy().into_owned();
    let world = vec![
        ("CLARP_OPENCODE_PROGRAM".to_owned(), text(&program_path)),
        ("CLARP_OPENCODE_LIVE".to_owned(), "1".to_owned()),
        ("XDG_CONFIG_HOME".to_owned(), text(&config)),
        ("XDG_DATA_HOME".to_owned(), text(&root.join("data"))),
    ];
    World { place, world }
}

impl World {
    fn host(&self) -> Host {
        let world: Vec<(&str, &str)> = self
            .world
            .iter()
            .map(|(key, value)| (key.as_str(), value.as_str()))
            .collect();
        self.place.start_in("", &[], &world)
    }

    fn noted(&self, name: &str) -> String {
        std::fs::read_to_string(self.place.root.join(name)).unwrap_or_default()
    }

    fn lines(&self, name: &str) -> Vec<String> {
        self.noted(name).lines().map(str::to_owned).collect()
    }

    /// The server each turn ran on, in order.
    fn servers(&self) -> Vec<String> {
        self.lines("turns")
            .iter()
            .map(|line| line.split(' ').next().unwrap_or_default().to_owned())
            .collect()
    }

    fn setting(&self, text: &str) {
        std::fs::write(self.place.root.join("config/clarp-host/config.toml"), text).unwrap();
    }

    /// A new agent of `backend`, and the socket its lane answers on.
    fn agent(&self, host: &Host, session: &str, backend: &str) -> std::path::PathBuf {
        let made = host.post(
            "/agents",
            json!({"name": session, "session": session, "backend": backend, "cwd": self.place.root.join("work")}),
        );
        assert_eq!(made["session"], session, "{made}");
        let id = host.get("/agents/snapshot")["agents"]
            .as_array()
            .unwrap()
            .iter()
            .find(|agent| agent["session"] == session)
            .unwrap()["agent_id"]
            .as_str()
            .unwrap()
            .to_owned();
        self.place.root.join("run").join(format!("{id}-ocl.sock"))
    }
}

/// What a lane answers one request with, or `None` when no lane answers on the socket.
fn lane_said(socket: &std::path::Path, op: &str) -> Option<Value> {
    let mut stream = std::os::unix::net::UnixStream::connect(socket).ok()?;
    stream.set_read_timeout(Some(Duration::from_secs(5))).ok()?;
    writeln!(stream, "{}", json!({ "op": op })).ok()?;
    let mut line = String::new();
    BufReader::new(stream).read_line(&mut line).ok()?;
    serde_json::from_str(&line).ok()
}

fn lane_state(socket: &std::path::Path) -> Option<Value> {
    lane_said(socket, "inspect")
        .filter(|said| said["ok"] == true)
        .map(|said| said["state"].clone())
}

/// Asks every lane of a place to leave when the test ends.
struct Lanes(std::path::PathBuf);

impl Drop for Lanes {
    fn drop(&mut self) {
        let Ok(entries) = std::fs::read_dir(self.0.join("run")) else {
            return;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.to_string_lossy().ends_with("-ocl.sock") {
                let _ = lane_said(&path, "retire");
            }
        }
    }
}

fn answers(host: &Host, session: &str, text: &str) -> usize {
    host.conversation(session)
        .iter()
        .filter(|(role, said)| role == "assistant" && said == text)
        .count()
}

/// Sends `word` and waits for its answer and the end of its turn.
fn asked(host: &Host, session: &str, word: &str) {
    host.send(session, word, word);
    let answer = format!("said-{word}");
    wait_for(&answer, || answers(host, session, &answer) == 1);
    wait_for("the turn to end", || host.state(session) == "done");
}

/// The agent's lane is up and holds no claim.
fn lane_ready(socket: &std::path::Path) {
    wait_for("the lane to be ready", || {
        lane_state(socket).is_some_and(|state| state["available"] == true)
    });
}

#[test]
fn opencode_turns_after_the_first_run_on_one_kept_server() {
    for backend in ["opencode", "deepseek"] {
        let world = world(&format!("opencode-lane-{backend}"), "");
        let _lanes = Lanes(world.place.root.clone());
        std::fs::create_dir_all(world.place.root.join("work")).unwrap();
        let host = world.host();
        let socket = world.agent(&host, "keeper", backend);

        // No turn waits for a lane: the first runs on its own server, the lane starts beside it.
        asked(&host, "keeper", "ask-one");
        lane_ready(&socket);
        let lane_pid = lane_state(&socket).unwrap()["pid"].to_string();
        assert_eq!(
            world.lines("starts").len(),
            2,
            "the turn's server and the lane's"
        );
        let first = world.servers()[0].clone();
        assert_ne!(first, lane_pid, "the first turn ran on its own server");

        // The turns after run on the lane's server, in the same session, each with its own
        // identity; no server is started for them.
        asked(&host, "keeper", "ask-two");
        asked(&host, "keeper", "ask-three");
        assert_eq!(world.lines("starts").len(), 2);
        let servers = world.servers();
        assert_eq!(
            servers[1..],
            [lane_pid.clone(), lane_pid.clone()],
            "{servers:?}"
        );
        let sessions: Vec<_> = world
            .lines("turns")
            .iter()
            .map(|line| line.split(' ').nth(1).unwrap().to_owned())
            .collect();
        assert!(sessions.iter().all(|s| *s == sessions[0]), "{sessions:?}");
        let tokens = world.lines("tokens");
        assert_eq!(tokens.len(), 3, "{tokens:?}");
        assert!(tokens.iter().all(|t| !t.is_empty()), "{tokens:?}");
        assert_ne!(tokens[1], tokens[2], "each lane turn has its own identity");
        let state = lane_state(&socket).unwrap();
        assert_eq!(state["thread"], sessions[0].as_str());
        assert!(state["memory_bytes"].as_u64().unwrap_or(0) > 0, "{state}");

        // The agent's own configuration changed in its words: the next turn runs on its own
        // server while a new lane starts, and the one after on the new lane.
        std::fs::write(world.place.root.join("work/opencode.json"), "{\"a\":1}").unwrap();
        asked(&host, "keeper", "ask-four");
        lane_ready(&socket);
        let new_pid = lane_state(&socket).unwrap()["pid"].to_string();
        assert_ne!(new_pid, lane_pid, "a new lane for the new configuration");
        asked(&host, "keeper", "ask-five");
        assert_eq!(world.servers().last().unwrap(), &new_pid);

        // Switched off: the turn runs on its own server, and the lane lets the session go.
        world.setting("opencode_chat_lane = false\n");
        asked(&host, "keeper", "ask-six");
        let last = world.servers().last().unwrap().clone();
        assert_ne!(last, new_pid);
        assert_eq!(answers(&host, "keeper", "said-ask-six"), 1);
    }
}

#[test]
fn a_stop_on_the_lane_aborts_the_turn_and_keeps_the_server() {
    let world = world("opencode-lane-stop", "");
    let _lanes = Lanes(world.place.root.clone());
    std::fs::create_dir_all(world.place.root.join("work")).unwrap();
    let host = world.host();
    let socket = world.agent(&host, "stopper", "opencode");
    asked(&host, "stopper", "ask-first");
    lane_ready(&socket);
    let lane_pid = lane_state(&socket).unwrap()["pid"].to_string();

    host.send("stopper", "hold-stop", "hold");
    wait_for("the held turn", || {
        world.place.root.join("holding-stop").exists()
    });
    let began = Instant::now();
    host.post("/stop", json!({"session": "stopper"}));
    wait_for("the turn to end interrupted", || {
        host.state("stopper") == "interrupted"
    });
    assert!(
        began.elapsed() < Duration::from_secs(4),
        "{:?}",
        began.elapsed()
    );
    assert_eq!(world.lines("aborts"), [lane_pid.as_str()]);
    assert_eq!(answers(&host, "stopper", "said-hold-stop"), 0);
    assert_eq!(
        lane_state(&socket).unwrap()["pid"].to_string(),
        lane_pid,
        "the server is kept"
    );

    asked(&host, "stopper", "ask-after");
    assert_eq!(world.servers().last().unwrap(), &lane_pid);
    assert_eq!(world.lines("starts").len(), 2);
}

#[test]
fn a_lane_turn_lives_through_a_host_restart_and_is_answered_once() {
    let world = world("opencode-lane-restart", "");
    let _lanes = Lanes(world.place.root.clone());
    std::fs::create_dir_all(world.place.root.join("work")).unwrap();
    let host = world.host();
    let socket = world.agent(&host, "keeper", "opencode");
    asked(&host, "keeper", "ask-first");
    lane_ready(&socket);
    let lane_pid = lane_state(&socket).unwrap()["pid"].to_string();

    host.send("keeper", "hold-up", "up");
    wait_for("the held turn", || {
        world.place.root.join("holding-up").exists()
    });
    host.kill();
    let host = world.host();
    wait_for("the turn rejoined", || host.state("keeper") == "thinking");
    std::fs::write(world.place.root.join("release-up"), "").unwrap();
    wait_for("the answer given after the restart", || {
        answers(&host, "keeper", "said-hold-up") == 1
    });
    wait_for("that turn to end", || host.state("keeper") == "done");

    // The held turn ran on the lane. The new Host listens elsewhere, so what the lane's server
    // was started with names the old Host: the next turn runs on its own server while a lane
    // for the new Host starts, and nothing came twice.
    asked(&host, "keeper", "ask-last");
    assert_eq!(answers(&host, "keeper", "said-hold-up"), 1);
    assert_eq!(world.servers()[1], lane_pid);
    assert_eq!(
        world
            .lines("answered")
            .iter()
            .filter(|w| *w == "hold-up")
            .count(),
        1
    );
}

#[test]
fn a_server_that_dies_under_a_lane_turn_fails_that_turn_once_and_the_next_runs() {
    let world = world("opencode-lane-died", "");
    let _lanes = Lanes(world.place.root.clone());
    std::fs::create_dir_all(world.place.root.join("work")).unwrap();
    let host = world.host();
    let socket = world.agent(&host, "fragile", "opencode");
    asked(&host, "fragile", "ask-first");
    lane_ready(&socket);

    host.send("fragile", "die-now", "die");
    wait_for("the server to die", || {
        world.noted("died").contains("die-now")
    });
    wait_for("the turn to end", || {
        host.state("fragile") != "thinking" && host.state("fragile") != "tool"
    });
    let failed = || {
        host.turns("fragile")
            .into_iter()
            .filter(|row| row["turn"]["status"] == "failed")
            .count()
    };
    wait_for("the turn to fail", || failed() == 1);
    wait_for("the lane to have left", || lane_state(&socket).is_none());

    // The next turn runs on its own server and is answered; a new lane starts beside it.
    asked(&host, "fragile", "ask-after");
    assert_eq!(failed(), 1, "{:?}", host.turns("fragile"));
    assert_eq!(
        world
            .lines("turns")
            .iter()
            .filter(|l| l.ends_with("die-now"))
            .count(),
        1
    );
    lane_ready(&socket);
}

#[test]
fn a_server_of_another_line_is_never_kept() {
    let world = world("opencode-lane-version", "");
    let _lanes = Lanes(world.place.root.clone());
    std::fs::create_dir_all(world.place.root.join("work")).unwrap();
    std::fs::write(world.place.root.join("version"), "2.1.2").unwrap();
    let host = world.host();
    let socket = world.agent(&host, "old", "opencode");
    asked(&host, "old", "ask-one");
    asked(&host, "old", "ask-two");
    assert!(lane_state(&socket).is_none(), "no lane for a 2.x server");
    // Every turn ran on its own server.
    let servers = world.servers();
    assert_ne!(servers[0], servers[1]);
}

#[test]
fn a_turn_whose_lane_cannot_be_worked_out_runs_on_its_own_server() {
    let world = world("opencode-lane-unread", "");
    let _lanes = Lanes(world.place.root.clone());
    // More of a project's OpenCode folder than the lane reads at each turn: no lane is kept for
    // the agent, and every turn runs on its own server, answered as before (review of #783).
    let folder = world.place.root.join("work/.opencode/agents");
    std::fs::create_dir_all(&folder).unwrap();
    for n in 0..501 {
        std::fs::write(folder.join(format!("a{n}.md")), "x").unwrap();
    }
    let host = world.host();
    let socket = world.agent(&host, "unread", "opencode");
    asked(&host, "unread", "ask-one");
    asked(&host, "unread", "ask-two");
    assert!(
        lane_state(&socket).is_none(),
        "no lane for an unread folder"
    );
    let servers = world.servers();
    assert_eq!(servers.len(), 2, "{servers:?}");
    assert_ne!(servers[0], servers[1]);
}

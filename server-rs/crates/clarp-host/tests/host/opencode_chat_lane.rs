//! Issue 763. With the owner's `opencode_chat_lane` on, an OpenCode agent's first turn runs on
//! its own server while the lane starts beside it, and the turns after run on the lane's kept
//! server: no new server, each turn its own identity, a Stop that keeps the server, a turn
//! that lives through a Host restart, a server that dies under a turn, and a changed
//! configuration that starts a new lane. Issue 784: words sent while a lane turn runs are read
//! into it; words it has not read when it ends, or is stopped, run as a turn of their own.

use super::*;

/// A stand-in `opencode serve`, for the per-turn driver and the lane alike. Sessions are kept
/// in a file beside it, as OpenCode keeps them in its database, so a lane's server sees the
/// session another server made. A word `ask-<x>` is answered `said-ask-<x>` at once; `hold-<x>`
/// once the file `release-<x>` exists, unless the session is aborted first; a prompt of several
/// such words (messages given to a turn together) is answered word by word; `die-<x>` ends the
/// server under the turn. The first prompt any server is given writes OpenCode's own files into
/// the global configuration folder, as OpenCode does on its first start. A prompt's user message is named in OpenCode's ascending form. A prompt
/// that names its `messageID` is a steer: it is stored, and the steers stored since the last
/// answer are read together at the turn's next step, as OpenCode reads them: one answer, whose
/// parent is the last of them, says `said-<word>+<word>...`. Not so a word `late-<x>`, which the
/// turn never reads, or `race-<x>`, which reached it as its run ended: as OpenCode does, it starts
/// a run of its own for that right after the run's idle, so that idle is stale when the lane reads
/// it, and it refuses to delete a message while a run goes on; `done-<x>` is the same, but that
/// run has answered before the lane reads the stale idle, and only its events are still to come;
/// `skew-<x>` is never read, and a message stored before it has an id that sorts after it, with an
/// answer (a clock that stepped back between them); `lost-<x>` has such a message before it too,
/// and is read and answered, but the event of its answer's start is lost; `slow-<x>` comes as the run ends, with an order as wrong as
/// skew's: the session goes idle, asks a permission whose answer takes 6.5 s, and starts a run for
/// it, which stores its answer only after that; `flaky-<x>` is `lost-<x>` whose session fails the
/// second read of its messages after it (the first recheck's), `broken-<x>` is `skew-<x>` whose
/// session fails the second and third. The session lists its stored messages in the order it stored
/// them. It tells the sessions that run (`/session/status`) and a session's answers, each with the
/// message it answers. While the file `slow-delete` exists a delete takes two seconds. What it
/// was asked is noted in files beside it.
const SERVE: &str = r#"#!/usr/bin/env python3
import base64, json, os, queue, random, re, string, sys, threading, time
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
# The steers stored and not read yet: their message and their word.
stored = []
running = [False]
# The session's messages as it stores them, in order: users' and answers, each answer with the
# message it answers.
history = []
# Reads of a session's messages since a flaky- or broken- steer was stored, and which of them fail.
reads = [0, set()]
named = [0, 0]

def ascending():
    """A message id in OpenCode's ascending form, as it names a prompt's user message."""
    ms = int(time.time() * 1000)
    if ms != named[0]:
        named[0], named[1] = ms, 0
    named[1] += 1
    tail = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(14))
    return "msg_%012x%s" % ((ms * 4096 + named[1]) & 0xFFFFFFFFFFFF, tail)

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

def idle(sid):
    running[0] = False
    emit(status(sid, "idle"))

def step(sid, word, parent, kept=False, quiet=False):
    """One step of the turn, answering `word`: False when the session was aborted in it. `kept`:
    the answer is in the session already, and only told now. `quiet`: the answer's start is never
    told (its event is lost)."""
    count[0] += 1
    mid = f"msg_{pid}_{count[0]}"
    if not kept:
        history.append({"id": mid, "role": "assistant", "parentID": parent})
    if not quiet:
        emit({"type": "message.updated", "properties": {"info": {"id": mid, "sessionID": sid, "role": "assistant", "parentID": parent}}})
    emit(part(sid, mid, id=mid + "s", type="step-start"))
    if word.startswith("die-"):
        note("died", word)
        os._exit(3)
    if word.startswith("hold-"):
        (root / ("holding-" + word[5:])).write_text("")
        end = time.monotonic() + 60
        while not (root / ("release-" + word[5:])).exists() and time.monotonic() < end:
            if aborted.wait(0.02):
                return False
    emit(part(sid, mid, id=mid + "t", type="text", text="said-" + word, time={"end": 1}))
    emit(part(sid, mid, id=mid + "f", type="step-finish", reason="stop", cost=0, tokens={"input": 1, "output": 1}))
    note("answered", word)
    return True

def together(group):
    """What one answer to stored messages answers, and the last of them, its parent."""
    return "+".join(word for _, word in group), group[-1][0]

def run(sid, words, user):
    emit(status(sid, "busy"))
    # Messages the Host gives a turn together are answered one after another.
    for word in words:
        if not step(sid, word, user):
            return idle(sid)
    # All words stored meanwhile are read together at the next step, as OpenCode reads every
    # user message since its last answer: one answer, whose parent is the last of them.
    while True:
        with lock:
            ready = [s for s in stored if not s[1].startswith(("late-", "race-", "done-", "skew-", "slow-", "broken-"))]
            if not ready:
                break
            for s in ready:
                stored.remove(s)
        quiet = any(word.startswith(("lost-", "flaky-")) for _, word in ready)
        if not step(sid, *together(ready), quiet=quiet):
            return idle(sid)
    with lock:
        slow = [s for s in stored if s[1].startswith("slow-")]
        for s in slow:
            stored.remove(s)
    if slow:
        # Words that came as the run ended, on which message order is wrong (an id stored
        # before them sorts after them). The run's idle goes out with the session idle; then a
        # permission the lane must answer, which takes 6.5 s, with the new run's busy behind it;
        # the answer itself is stored only after that.
        running[0] = False
        emit(status(sid, "idle"))
        emit({"type": "permission.asked", "properties": {"sessionID": sid, "id": "per_slow"}})
        emit(status(sid, "busy"))
        time.sleep(8)
        running[0] = True
        step(sid, *together(slow))
        return idle(sid)
    with lock:
        racing = [s for s in stored if s[1].startswith("race-")]
        done = [s for s in stored if s[1].startswith("done-")]
        for s in racing + done:
            stored.remove(s)
    if done:
        # Words that came as the run ended, which a run of their own answered before the lane
        # reads this run's idle: the session holds that answer, and its events come after.
        word, parent = together(done)
        history.append({"id": f"msg_{pid}_done", "role": "assistant", "parentID": parent})
        running[0] = False
        emit(status(sid, "idle"))
        emit(status(sid, "busy"))
        step(sid, word, parent, kept=True)
        return emit(status(sid, "idle"))
    # The run ends; words that came as it ended start a run of their own at once.
    emit(status(sid, "idle"))
    if racing:
        emit(status(sid, "busy"))
        time.sleep(0.3)
        step(sid, *together(racing))
    idle(sid)

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
        if path == "/session/status":
            return self._json(200, {s: {"type": "busy"} for s in sessions()} if running[0] else {})
        if path.startswith("/session/") and path.endswith("/message"):
            reads[0] += 1
            note("reads", "failed" if reads[0] in reads[1] else "read")
            if reads[0] in reads[1]:
                return self._json(500, {"name": "UnknownError"})
            return self._json(200, [{"info": m} for m in history[-50:]])
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
        if path == "/permission/per_slow/reply":
            time.sleep(6.5)
            return self._json(200, True)
        if path.endswith("/abort"):
            note("aborts", str(pid))
            aborted.set()
            return self._json(200, True)
        if path.endswith("/prompt_async"):
            sid = path.split("/")[2]
            text = body["parts"][0]["text"]
            words = list(dict.fromkeys(re.findall(r"\b(?:ask|hold|die|late|race|done|skew|lost|slow|flaky|broken)-[a-z0-9]+", text)))
            word = words[0]
            if "messageID" in body:
                note("steered", f"{pid} {body['messageID']} {word} {'busy' if running[0] else 'idle'}")
                with lock:
                    if word.startswith(("flaky-", "broken-")):
                        # The idle's read goes through; the recheck's (and for broken-, the one
                        # after it too) fails.
                        reads[0], reads[1] = 0, {2} if word.startswith("flaky-") else {2, 3}
                    if word.startswith(("skew-", "slow-", "lost-", "broken-")):
                        # A message stored before these words with an id that sorts after them,
                        # and its answer: what a clock that stepped back between them makes.
                        ahead = "msg_ffffffffffff" + "".join(random.choice(string.ascii_letters) for _ in range(14))
                        history.append({"id": ahead, "role": "user"})
                        history.append({"id": f"msg_{pid}_ahead", "role": "assistant", "parentID": ahead})
                    history.append({"id": body["messageID"], "role": "user"})
                    stored.append((body["messageID"], word))
                self.send_response(204); self.send_header("Content-Length", "0"); self.end_headers()
                return
            # As OpenCode does on its first start: its default configuration, its plugins'
            # package file and its .gitignore, in the global configuration folder.
            config = os.environ.get("XDG_CONFIG_HOME", "")
            own = Path(config) / "opencode"
            if config and not (own / "package.json").exists():
                own.mkdir(parents=True, exist_ok=True)
                (own / "opencode.jsonc").write_text('{\n  "$schema": "https://opencode.ai/config.json"\n}')
                (own / "package.json").write_text('{"dependencies": {"@opencode-ai/plugin": "1.18.33"}}')
                (own / ".gitignore").write_text("node_modules\npackage.json\npackage-lock.json\nbun.lock\n.gitignore\n")
            token_file = os.environ.get("CLARP_PROVIDER_TURN_FILE", "")
            token = os.environ.get("CLARP_PROVIDER_TURN") or (Path(token_file).read_text() if token_file else "")
            for each in words:
                note("turns", f"{pid} {sid} {each}")
            note("tokens", token)
            aborted.clear()
            running[0] = True
            user = ascending()
            with lock:
                history.append({"id": user, "role": "user"})
            threading.Thread(target=run, args=(sid, words, user), daemon=True).start()
            self.send_response(204); self.send_header("Content-Length", "0"); self.end_headers()
            return
        self._json(200, True)
    def do_DELETE(self):
        if not self._auth():
            return
        parts = self.path.split("?")[0].split("/")
        if len(parts) != 5 or parts[3] != "message":
            return self._json(404, {})
        note("deleting", parts[4])
        if (root / "slow-delete").exists():
            time.sleep(2)
        if running[0]:
            return self._json(409, {"name": "SessionBusyError"})
        with lock:
            found = [s for s in stored if s[0] == parts[4]]
            for s in found:
                stored.remove(s)
            history[:] = [m for m in history if m["id"] != parts[4]]
        if not found:
            return self._json(404, {"name": "NotFoundError"})
        note("deleted", parts[4])
        return self._json(200, True)

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

/// The lane lines of each of the agent's turn spools that are there now: the agent is told by its
/// lane's socket (`<agent>-ocl.sock`).
fn lane_lines(world: &World, socket: &std::path::Path) -> Vec<Vec<Value>> {
    let name = socket.file_name().unwrap().to_string_lossy();
    let agent = name.strip_suffix("-ocl.sock").unwrap();
    let spools = world
        .place
        .root
        .join("data/agents")
        .join(agent)
        .join("spool");
    std::fs::read_dir(spools)
        .into_iter()
        .flatten()
        .flatten()
        .map(|file| file.path())
        .filter(|path| path.extension().is_some_and(|ext| ext == "jsonl"))
        .filter_map(|path| std::fs::read_to_string(path).ok())
        .map(|text| {
            text.lines()
                .filter_map(|row| serde_json::from_str::<Value>(row).ok())
                .filter(|row| row["s"] == "out")
                .filter_map(|row| serde_json::from_str::<Value>(row["d"].as_str()?).ok())
                .filter(|line| {
                    line["type"]
                        .as_str()
                        .is_some_and(|kind| kind.starts_with("steer.") || kind.starts_with("turn."))
                })
                .collect()
        })
        .collect()
}

/// The steers the server stored: its pid, the message the lane named, the word, and whether the
/// session was busy then.
fn steers(world: &World) -> Vec<Vec<String>> {
    world
        .lines("steered")
        .iter()
        .map(|line| line.split(' ').map(str::to_owned).collect())
        .collect()
}

/// The words each prompt of a turn of its own asked, in order.
fn prompted(world: &World) -> Vec<String> {
    world
        .lines("turns")
        .iter()
        .map(|line| line.rsplit(' ').next().unwrap_or_default().to_owned())
        .collect()
}

/// A message the lane named in OpenCode's ascending form: `msg_`, twelve hex digits, fourteen
/// letters and digits.
fn lane_named(message: &str) -> bool {
    message.strip_prefix("msg_").is_some_and(|rest| {
        rest.len() == 26
            && rest[..12].bytes().all(|b| b.is_ascii_hexdigit())
            && rest[12..].bytes().all(|b| b.is_ascii_alphanumeric())
    })
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

/// The turns that started OpenCode without a lane in the last hour, as the apps read them.
fn cold(host: &Host) -> u64 {
    host.get("/lane-settings")["cold_starts_last_hour"]["tools"]["opencode"]
        .as_u64()
        .unwrap_or(0)
}

impl World {
    /// OpenCode's login file, as a seat switch or a refreshed token writes it.
    fn login(&self, said: &Value) {
        let dir = self.place.root.join("data/opencode");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("auth.json"), said.to_string()).unwrap();
    }

    /// An agent whose second turn ran on its lane: the lane's server, and the socket.
    fn on_lane(&self, host: &Host, session: &str) -> (String, std::path::PathBuf) {
        let socket = self.agent(host, session, "opencode");
        asked(host, session, "ask-one");
        lane_ready(&socket);
        asked(host, session, "ask-two");
        let lane = lane_state(&socket).unwrap()["pid"].to_string();
        assert_eq!(
            self.servers().last(),
            Some(&lane),
            "the second turn ran on the lane"
        );
        assert_eq!(
            self.lines("starts").len(),
            2,
            "the first turn's server and the lane's"
        );
        (lane, socket)
    }
}

/// Issue 857. A seat switch, or a refreshed token, rewrites `auth.json`, and the lane's digest
/// moved with it: the next turn ran on its own server, a cold start. Where the login alone
/// changed, the turn now starts the new lane itself and runs on it: one server started, the
/// lane's, and no turn without a lane. Any other change runs the turn on its own server, as
/// before.
#[test]
fn a_changed_login_starts_the_new_lane_as_the_turn() {
    let world = world("opencode-login-swap", "");
    let _lanes = Lanes(world.place.root.clone());
    std::fs::create_dir_all(world.place.root.join("work")).unwrap();
    world.login(&json!({"openai": {"type": "api", "key": "k1"}}));
    let host = world.host();
    let (first, socket) = world.on_lane(&host, "swapper");
    let cold_before = cold(&host);

    // Another account.
    world.login(&json!({"anthropic": {"type": "oauth", "refresh": "r2", "access": "a2"}}));
    asked(&host, "swapper", "ask-three");
    assert_eq!(
        world.lines("starts").len(),
        3,
        "one server started: the lane's"
    );
    let second = lane_state(&socket).unwrap()["pid"].to_string();
    assert_ne!(second, first, "a lane on the new login");
    assert_eq!(world.servers().last(), Some(&second), "the turn ran on it");
    assert_eq!(cold(&host), cold_before, "no turn without a lane");

    // A refreshed token of the same account: the same.
    world.login(&json!({"anthropic": {"type": "oauth", "refresh": "r2", "access": "a3"}}));
    asked(&host, "swapper", "ask-four");
    assert_eq!(world.lines("starts").len(), 4);
    let third = lane_state(&socket).unwrap()["pid"].to_string();
    assert_ne!(third, second);
    assert_eq!(world.servers().last(), Some(&third));
    assert_eq!(cold(&host), cold_before);
    let sessions: Vec<_> = world
        .lines("turns")
        .iter()
        .map(|line| line.split(' ').nth(1).unwrap().to_owned())
        .collect();
    assert!(sessions.iter().all(|s| *s == sessions[0]), "{sessions:?}");

    // The agent's configuration changed, not the login: the turn runs on its own server and
    // the lane is started beside it, as before.
    std::fs::write(world.place.root.join("work/opencode.json"), "{\"a\":1}").unwrap();
    asked(&host, "swapper", "ask-five");
    let own = world.servers().last().unwrap().clone();
    assert_ne!(own, third, "the turn ran on its own server");
    assert_eq!(cold(&host), cold_before + 1);
    lane_ready(&socket);
    let beside = lane_state(&socket).unwrap()["pid"].to_string();
    assert_ne!(beside, own);
    assert_ne!(beside, third);
}

/// Issue 857. Where no lane may start under the new login (no place within the limits), the
/// turn runs on its own server, as before: tried once, never again.
#[test]
fn a_changed_login_with_no_place_for_a_lane_runs_the_turn_on_its_own_server() {
    let world = world("opencode-login-swap-full", "");
    let _lanes = Lanes(world.place.root.clone());
    std::fs::create_dir_all(world.place.root.join("work")).unwrap();
    world.login(&json!({"openai": {"type": "api", "key": "k1"}}));
    let host = world.host();
    let (lane, socket) = world.on_lane(&host, "crowded");
    let cold_before = cold(&host);
    // Too little memory for any lane: none may start now.
    world.setting("opencode_chat_lane = true\nlane_limits = \"fixed\"\nlane_memory_mb = 1\n");
    world.login(&json!({"openai": {"type": "api", "key": "k2"}}));
    asked(&host, "crowded", "ask-three");
    assert_eq!(
        world.lines("starts").len(),
        3,
        "the turn's own server alone"
    );
    let own = world.servers().last().unwrap().clone();
    assert_ne!(own, lane, "the turn ran on its own server");
    assert_eq!(cold(&host), cold_before + 1);
    assert!(lane_state(&socket).is_none(), "no lane started");
}

/// Issue 784. A message sent while a turn runs on its own server waits for a turn of its own, as
/// before. One sent while a lane turn runs its tool is posted to the busy session, read at the
/// turn's next step and answered there: one turn, the answer after the message, and the
/// steering evidence the apps read for a Codex lane.
#[test]
fn a_message_sent_while_a_lane_turn_runs_is_read_into_it_and_answered_there() {
    let world = world("opencode-lane-steer", "");
    let _lanes = Lanes(world.place.root.clone());
    std::fs::create_dir_all(world.place.root.join("work")).unwrap();
    let host = world.host();
    let socket = world.agent(&host, "steer", "opencode");

    // The first turn runs on its own server, which takes no steered words: the message sent
    // meanwhile waits, and runs as a turn of its own once the first is over.
    host.send("steer", "hold-own", "own");
    wait_for("the first turn", || {
        world.place.root.join("holding-own").exists()
    });
    let sent = host.send("steer", "ask-next", "next");
    assert_eq!(sent["queued"], true, "{sent}");
    std::fs::write(world.place.root.join("release-own"), "").unwrap();
    wait_for("both answered", || {
        answers(&host, "steer", "said-hold-own") == 1
            && answers(&host, "steer", "said-ask-next") == 1
    });
    wait_for("those turns to end", || host.state("steer") == "done");
    // Never read into the turn on its own server: no steer, no reading of it in that turn, and
    // it is the prompt of a turn of its own, after the first, answered once.
    assert!(steers(&world).is_empty(), "{:?}", steers(&world));
    assert_eq!(prompted(&world), ["hold-own", "ask-next"]);
    let rows = host.turns("steer");
    let row = |id: &str| rows.iter().find(|row| row["id"] == id).cloned().unwrap();
    let (own, next) = (row("u-own"), row("u-next"));
    assert!(next.get("read_into").is_none(), "{next}");
    assert_ne!(own["trace_id"], next["trace_id"], "{own} {next}");
    let said: Vec<String> = host
        .conversation("steer")
        .into_iter()
        .map(|(_, text)| text)
        .collect();
    assert_eq!(
        said,
        ["hold-own", "said-hold-own", "ask-next", "said-ask-next"]
    );
    lane_ready(&socket);
    let lane_pid = lane_state(&socket).unwrap()["pid"].to_string();

    // A lane turn runs its tool; the message sent meanwhile goes to the busy session.
    host.send("steer", "hold-tool", "tool");
    wait_for("the lane turn's tool", || {
        world.place.root.join("holding-tool").exists()
    });
    let sent = host.send("steer", "hold-more", "more");
    assert_eq!(sent["queued"], true, "{sent}");
    wait_for("the steer", || !steers(&world).is_empty());
    let steer = steers(&world)[0].clone();
    assert_eq!(steer[0], lane_pid, "{steer:?}");
    assert!(lane_named(&steer[1]), "{steer:?}");
    assert_eq!(steer[2..], ["hold-more", "busy"], "{steer:?}");
    // Stored, not read yet: the message is no row of the conversation.
    assert!(
        !host.turns("steer").iter().any(|row| row["id"] == "u-more"),
        "{:?}",
        host.turns("steer")
    );

    // The tool ends; the next step names the message as the one it answers, and holds.
    std::fs::write(world.place.root.join("release-tool"), "").unwrap();
    wait_for("the steered words' own step", || {
        world.place.root.join("holding-more").exists()
    });
    wait_for("the steered words read into the turn", || {
        host.turns("steer").iter().any(|row| row["id"] == "u-more")
    });
    let live = host.get("/live?session=steer");
    let read = live["turn"]["read_rows"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    assert!(read.iter().any(|row| row == "u-more"), "{live}");
    let rows = host.turns("steer");
    let row = rows.iter().find(|row| row["id"] == "u-more").unwrap();
    assert_eq!(
        row["read_into"]["trace_id"], live["turn"]["turn_id"],
        "{row}"
    );
    assert!(row["read_into"]["turn"].is_u64(), "{row}");
    let lines = lane_lines(&world, &socket)
        .into_iter()
        .find(|lines| lines.iter().any(|line| line["type"] == "steer.read"))
        .unwrap();
    let kinds: Vec<&str> = lines.iter().map(|l| l["type"].as_str().unwrap()).collect();
    assert_eq!(kinds, ["steer.open", "steer.read"], "{lines:?}");
    assert!(
        lines[1]["id"].as_str().is_some_and(|id| !id.is_empty()),
        "{lines:?}"
    );

    std::fs::write(world.place.root.join("release-more"), "").unwrap();
    wait_for("the answer to the steered words", || {
        answers(&host, "steer", "said-hold-more") == 1
    });
    wait_for("the turn to end", || host.state("steer") == "done");
    // One turn: the steered words were never a prompt of their own, and their answer came after
    // them, after what the turn said before it read them.
    assert_eq!(prompted(&world), ["hold-own", "ask-next", "hold-tool"]);
    let said: Vec<String> = host
        .conversation("steer")
        .into_iter()
        .map(|(_, text)| text)
        .filter(|text| text.contains("-tool") || text.contains("-more"))
        .collect();
    assert_eq!(
        said,
        ["hold-tool", "said-hold-tool", "hold-more", "said-hold-more"]
    );
    assert!(world.lines("deleted").is_empty());

    // The lane is kept for the next turn.
    asked(&host, "steer", "ask-after");
    assert_eq!(world.servers().last().unwrap(), &lane_pid);
    assert_eq!(answers(&host, "steer", "said-ask-next"), 1);
}

/// Issue 784. Words the lane turn stored but never read run as a turn of their own: those it
/// took after its last step, and those a Stop caught. The lane takes them out of the session
/// first, so OpenCode never meets them twice.
#[test]
fn steered_words_a_lane_turn_never_read_run_as_a_turn_of_their_own() {
    let world = world("opencode-lane-unread-steer", "");
    let _lanes = Lanes(world.place.root.clone());
    std::fs::create_dir_all(world.place.root.join("work")).unwrap();
    let host = world.host();
    let socket = world.agent(&host, "late", "opencode");
    asked(&host, "late", "ask-first");
    lane_ready(&socket);

    // Stored after the turn's last step: the turn ends without them.
    host.send("late", "hold-a", "a");
    wait_for("the held turn", || {
        world.place.root.join("holding-a").exists()
    });
    let sent = host.send("late", "late-b", "b");
    assert_eq!(sent["queued"], true, "{sent}");
    wait_for("the steer", || steers(&world).len() == 1);
    let late = steers(&world)[0][1].clone();
    std::fs::write(world.place.root.join("release-a"), "").unwrap();
    wait_for("the unread words answered in a turn of their own", || {
        answers(&host, "late", "said-late-b") == 1
    });
    wait_for("that turn to end", || host.state("late") == "done");
    assert_eq!(world.lines("deleted"), [late.as_str()]);
    assert_eq!(prompted(&world), ["ask-first", "hold-a", "late-b"]);
    assert_eq!(answers(&host, "late", "said-hold-a"), 1);

    // Caught by a Stop: OpenCode aborts the turn before its next step, and the words are taken
    // out of the session and run on their own.
    host.send("late", "hold-c", "c");
    wait_for("the held turn", || {
        world.place.root.join("holding-c").exists()
    });
    let sent = host.send("late", "ask-d", "d");
    assert_eq!(sent["queued"], true, "{sent}");
    wait_for("the steer", || steers(&world).len() == 2);
    let caught = steers(&world)[1][1].clone();
    host.post("/stop", json!({"session": "late"}));
    wait_for("the turn to end interrupted", || {
        world.lines("deleted").len() == 2
    });
    assert_eq!(world.lines("deleted")[1], caught);
    // A Stop holds what waits until the user's next message; then both run, each once.
    asked(&host, "late", "ask-e");
    wait_for("the caught words answered", || {
        answers(&host, "late", "said-ask-d") == 1
    });
    wait_for("the last turn to end", || host.state("late") == "done");
    assert_eq!(answers(&host, "late", "said-hold-c"), 0);
    assert_eq!(answers(&host, "late", "said-ask-e"), 1);
    let own = prompted(&world);
    assert_eq!(own.iter().filter(|w| *w == "ask-d").count(), 1, "{own:?}");
    assert_eq!(steers(&world).len(), 2);
}

/// Issue 784. Words that reach OpenCode as its run ends start a run of their own there (OpenCode
/// 1.18: `effect/runner.ts` starts a run for a prompt that finds the session idle), right after
/// the run's idle: the idle the lane reads is stale. The lane asks the session, finds it running
/// again, and keeps the turn open: the words are read into it and answered there, once, and
/// nothing is deleted.
#[test]
fn a_stale_idle_keeps_the_turn_open_for_words_opencode_runs_again() {
    let world = world("opencode-lane-race-steer", "");
    let _lanes = Lanes(world.place.root.clone());
    std::fs::create_dir_all(world.place.root.join("work")).unwrap();
    let host = world.host();
    let socket = world.agent(&host, "racer", "opencode");
    asked(&host, "racer", "ask-first");
    lane_ready(&socket);

    host.send("racer", "hold-a", "a");
    wait_for("the held turn", || {
        world.place.root.join("holding-a").exists()
    });
    let sent = host.send("racer", "race-b", "b");
    assert_eq!(sent["queued"], true, "{sent}");
    wait_for("the steer", || steers(&world).len() == 1);
    std::fs::write(world.place.root.join("release-a"), "").unwrap();
    wait_for("the words answered in the turn", || {
        answers(&host, "racer", "said-race-b") == 1
    });
    wait_for("the turn to end", || host.state("racer") == "done");
    assert!(
        world.lines("deleting").is_empty(),
        "{:?}",
        world.lines("deleting")
    );
    let rows = host.turns("racer");
    let row = rows.iter().find(|row| row["id"] == "u-b").unwrap();
    assert!(row["read_into"]["turn"].is_u64(), "{row}");
    let said: Vec<String> = host
        .conversation("racer")
        .into_iter()
        .map(|(_, text)| text)
        .filter(|text| text.ends_with("-a") || text.ends_with("-b"))
        .collect();
    assert_eq!(said, ["hold-a", "said-hold-a", "race-b", "said-race-b"]);
    asked(&host, "racer", "ask-after");
    assert_eq!(prompted(&world), ["ask-first", "hold-a", "ask-after"]);
    assert_eq!(answers(&host, "racer", "said-race-b"), 1);
    assert_eq!(
        world
            .lines("answered")
            .iter()
            .filter(|w| *w == "race-b")
            .count(),
        1
    );
}

/// Issue 784. A message the Host gives the turn after OpenCode's run went idle, while the lane
/// still ends that turn, is refused by the lane: it runs as a turn of its own, once.
#[test]
fn a_steer_the_lane_refuses_after_the_idle_runs_as_its_own_turn_once() {
    let world = world("opencode-lane-refused-steer", "");
    let _lanes = Lanes(world.place.root.clone());
    std::fs::create_dir_all(world.place.root.join("work")).unwrap();
    let host = world.host();
    let socket = world.agent(&host, "after", "opencode");
    asked(&host, "after", "ask-first");
    lane_ready(&socket);

    // Words the turn never reads hold the lane in its delete, after the idle, for two seconds.
    std::fs::write(world.place.root.join("slow-delete"), "").unwrap();
    host.send("after", "hold-a", "a");
    wait_for("the held turn", || {
        world.place.root.join("holding-a").exists()
    });
    host.send("after", "late-b", "b");
    wait_for("the steer", || steers(&world).len() == 1);
    std::fs::write(world.place.root.join("release-a"), "").unwrap();
    wait_for("the lane in its delete", || {
        !world.lines("deleting").is_empty()
    });

    // The Host still sees the turn run and gives it the message; the lane has ended the turn.
    let sent = host.send("after", "ask-c", "c");
    assert_eq!(sent["queued"], true, "{sent}");
    let status = || {
        host.get("/turn-queue?session=after")["items"]
            .as_array()
            .unwrap()
            .iter()
            .find(|row| row["id"] == "c")
            .and_then(|row| row["status"].as_str().map(str::to_owned))
    };
    assert_eq!(
        status().as_deref(),
        Some("started"),
        "given to the running turn"
    );
    wait_for("both answered", || {
        answers(&host, "after", "said-late-b") == 1 && answers(&host, "after", "said-ask-c") == 1
    });
    wait_for("the last turn to end", || host.state("after") == "done");
    assert_eq!(steers(&world).len(), 1, "{:?}", steers(&world));
    let own = prompted(&world);
    assert_eq!(own.iter().filter(|w| *w == "ask-c").count(), 1, "{own:?}");
    assert_eq!(own.iter().filter(|w| *w == "late-b").count(), 1, "{own:?}");
    let rows = host.turns("after");
    // Never read into the turn as a steer: it waited, and the next turn took it at its start
    // (with the words that turn never read, which waited too).
    let row = rows.iter().find(|row| row["id"] == "u-c").unwrap();
    assert_ne!(row["read_into"]["at"], "steer", "{row}");
    assert_eq!(answers(&host, "after", "said-ask-c"), 1);
}

/// The conversation's words, in order, of the rows that end in one of `ends`.
fn said_of(host: &Host, session: &str, ends: &[&str]) -> Vec<String> {
    host.conversation(session)
        .into_iter()
        .map(|(_, text)| text)
        .filter(|text| ends.iter().any(|end| text.ends_with(end)))
        .collect()
}

/// How a message's row says it was read into a turn (`read_into.at`), if it was.
fn read_at(host: &Host, session: &str, row: &str) -> Value {
    host.turns(session)
        .into_iter()
        .find(|turn| turn["id"] == row)
        .map_or(Value::Null, |turn| turn["read_into"]["at"].clone())
}

/// Issue 862. Two steers stored while a tool runs are read by OpenCode together, and answered
/// once: the answer names the last of them. Both are read into the turn, as steers, and neither
/// is deleted or run again.
#[test]
fn steers_opencode_reads_together_are_all_read_and_answered_once() {
    let world = world("opencode-lane-together", "");
    let _lanes = Lanes(world.place.root.clone());
    std::fs::create_dir_all(world.place.root.join("work")).unwrap();
    let host = world.host();
    let socket = world.agent(&host, "both", "opencode");
    asked(&host, "both", "ask-first");
    lane_ready(&socket);

    host.send("both", "hold-a", "a");
    wait_for("the held turn", || {
        world.place.root.join("holding-a").exists()
    });
    assert_eq!(host.send("both", "ask-p", "p")["queued"], true);
    wait_for("the first steer", || steers(&world).len() == 1);
    assert_eq!(host.send("both", "ask-q", "q")["queued"], true);
    wait_for("the second steer", || steers(&world).len() == 2);
    std::fs::write(world.place.root.join("release-a"), "").unwrap();
    wait_for("one answer to both", || {
        answers(&host, "both", "said-ask-p+ask-q") == 1
    });
    wait_for("the turn to end", || host.state("both") == "done");
    assert_eq!(read_at(&host, "both", "u-p"), "steer");
    assert_eq!(read_at(&host, "both", "u-q"), "steer");
    assert!(
        world.lines("deleting").is_empty(),
        "{:?}",
        world.lines("deleting")
    );
    assert_eq!(
        said_of(&host, "both", &["-a", "-p", "-q"]),
        [
            "hold-a",
            "said-hold-a",
            "ask-p",
            "ask-q",
            "said-ask-p+ask-q"
        ]
    );
    asked(&host, "both", "ask-after");
    assert_eq!(prompted(&world), ["ask-first", "hold-a", "ask-after"]);
    assert_eq!(answers(&host, "both", "said-ask-p+ask-q"), 1);
}

/// Issue 862. Two steers that reached OpenCode as its run ended, which a run of their own
/// answered together before the lane read that run's stale idle: the session's answer names the
/// later one only. The lane counts both as answered and keeps the turn open; both are read into
/// it, answered once, and nothing is deleted.
#[test]
fn at_a_stale_idle_words_answered_with_later_ones_count_as_answered() {
    let world = world("opencode-lane-done-steer", "");
    let _lanes = Lanes(world.place.root.clone());
    std::fs::create_dir_all(world.place.root.join("work")).unwrap();
    let host = world.host();
    let socket = world.agent(&host, "done", "opencode");
    asked(&host, "done", "ask-first");
    lane_ready(&socket);

    host.send("done", "hold-a", "a");
    wait_for("the held turn", || {
        world.place.root.join("holding-a").exists()
    });
    host.send("done", "done-b", "b");
    wait_for("the first steer", || steers(&world).len() == 1);
    host.send("done", "done-c", "c");
    wait_for("the second steer", || steers(&world).len() == 2);
    std::fs::write(world.place.root.join("release-a"), "").unwrap();
    wait_for("the answer to both", || {
        answers(&host, "done", "said-done-b+done-c") == 1
    });
    wait_for("the turn to end", || host.state("done") == "done");
    assert!(
        world.lines("deleting").is_empty(),
        "{:?}",
        world.lines("deleting")
    );
    assert_eq!(read_at(&host, "done", "u-b"), "steer");
    assert_eq!(read_at(&host, "done", "u-c"), "steer");
    asked(&host, "done", "ask-after");
    assert_eq!(prompted(&world), ["ask-first", "hold-a", "ask-after"]);
    assert_eq!(answers(&host, "done", "said-done-b+done-c"), 1);
}

/// Issue 862. A steer posted after the message the last answer answered was not taken in by it:
/// it stays unread, is deleted at the idle, and runs once as a turn of its own.
#[test]
fn a_steer_after_the_last_answers_parent_runs_once_as_its_own_turn() {
    let world = world("opencode-lane-after-parent", "");
    let _lanes = Lanes(world.place.root.clone());
    std::fs::create_dir_all(world.place.root.join("work")).unwrap();
    let host = world.host();
    let socket = world.agent(&host, "after", "opencode");
    asked(&host, "after", "ask-first");
    lane_ready(&socket);

    host.send("after", "hold-a", "a");
    wait_for("the held turn", || {
        world.place.root.join("holding-a").exists()
    });
    host.send("after", "hold-p", "p");
    wait_for("the steer", || steers(&world).len() == 1);
    std::fs::write(world.place.root.join("release-a"), "").unwrap();
    // The steer is read and its answer holds; a message after it is posted then.
    wait_for("the steer's answer", || {
        world.place.root.join("holding-p").exists()
    });
    wait_for("the steer read", || {
        read_at(&host, "after", "u-p") == "steer"
    });
    host.send("after", "late-r", "r");
    wait_for("the second steer", || steers(&world).len() == 2);
    let late = steers(&world)[1][1].clone();
    std::fs::write(world.place.root.join("release-p"), "").unwrap();
    wait_for("the late words answered on their own", || {
        answers(&host, "after", "said-late-r") == 1
    });
    wait_for("the last turn to end", || host.state("after") == "done");
    assert_eq!(world.lines("deleted"), [late.as_str()]);
    assert_eq!(prompted(&world), ["ask-first", "hold-a", "late-r"]);
    assert_ne!(read_at(&host, "after", "u-r"), "steer");
    assert_eq!(answers(&host, "after", "said-hold-p"), 1);
    assert_eq!(answers(&host, "after", "said-late-r"), 1);
}

/// Issue 864, the skew case. An older answer's parent is a message stored before the steer, not
/// one of this turn's steers, and a clock step makes the steer sort before it: message order
/// says the steer was taken in, but no run follows. The lane holds the turn open for a recheck
/// only. There no answer's parent is the steer or a later steer of this turn's own, so it is not
/// read: the turn ends as the idle would have, and the steer is deleted and run once as a turn of
/// its own, never marked read and never lost. Nothing hangs.
#[test]
fn a_turn_held_open_by_message_order_alone_ends_at_the_recheck() {
    let world = world("opencode-lane-skew", "");
    let _lanes = Lanes(world.place.root.clone());
    std::fs::create_dir_all(world.place.root.join("work")).unwrap();
    let host = world.host();
    let socket = world.agent(&host, "skew", "opencode");
    asked(&host, "skew", "ask-first");
    lane_ready(&socket);

    host.send("skew", "hold-a", "a");
    wait_for("the held turn", || {
        world.place.root.join("holding-a").exists()
    });
    host.send("skew", "skew-b", "b");
    wait_for("the steer", || steers(&world).len() == 1);
    let skewed = steers(&world)[0][1].clone();
    std::fs::write(world.place.root.join("release-a"), "").unwrap();
    wait_for("the held turn's answer", || {
        answers(&host, "skew", "said-hold-a") == 1
    });
    // Held open for the recheck: the words are not taken back at the idle.
    std::thread::sleep(Duration::from_secs(2));
    assert!(
        world.lines("deleting").is_empty(),
        "{:?}",
        world.lines("deleting")
    );
    let began = Instant::now();
    wait_for("the words taken back at the recheck", || {
        world.lines("deleted") == [skewed.as_str()]
    });
    assert!(
        began.elapsed() < Duration::from_secs(10),
        "{:?}",
        began.elapsed()
    );
    wait_for("the words answered on their own", || {
        answers(&host, "skew", "said-skew-b") == 1
    });
    wait_for("the last turn to end", || host.state("skew") == "done");
    assert_eq!(prompted(&world), ["ask-first", "hold-a", "skew-b"]);
    assert_ne!(read_at(&host, "skew", "u-b"), "steer");
    assert_eq!(answers(&host, "skew", "said-skew-b"), 1);
}

/// The lanes live now, as the apps read them: `(lanes, busy)` of OpenCode's.
fn live(host: &Host) -> (u64, u64) {
    let tool = &host.get("/lane-settings")["live"]["tools"]["opencode"];
    (
        tool["lanes"].as_u64().unwrap_or(0),
        tool["busy"].as_u64().unwrap_or(0),
    )
}

/// Issue 864's warm lane. OpenCode writes its own files into its configuration folder during the
/// first turn (its default configuration, its plugins' package file), after the lane beside that
/// turn was started. They are no part of how a server is started: the second turn runs on the
/// warm lane, which is busy with it, and nothing starts cold.
#[test]
fn the_second_turn_runs_on_the_lane_warmed_beside_the_first() {
    let world = world("opencode-lane-warm", "");
    let _lanes = Lanes(world.place.root.clone());
    std::fs::create_dir_all(world.place.root.join("work")).unwrap();
    let host = world.host();
    let socket = world.agent(&host, "warm", "opencode");
    asked(&host, "warm", "ask-first");
    assert!(
        world
            .place
            .root
            .join("config/opencode/package.json")
            .exists(),
        "OpenCode's own files were written during the first turn"
    );
    lane_ready(&socket);
    let lane_pid = lane_state(&socket).unwrap()["pid"].to_string();
    assert_eq!(live(&host), (1, 0));
    let cold_before = cold(&host);

    host.send("warm", "hold-second", "second");
    wait_for("the second turn", || {
        world.place.root.join("holding-second").exists()
    });
    assert_eq!(live(&host), (1, 1), "the warm lane runs the second turn");
    std::fs::write(world.place.root.join("release-second"), "").unwrap();
    wait_for("its answer", || {
        answers(&host, "warm", "said-hold-second") == 1
    });
    wait_for("the turn to end", || host.state("warm") == "done");
    assert_eq!(world.servers().last().unwrap(), &lane_pid);
    assert_eq!(cold(&host), cold_before, "nothing started cold");
    assert_eq!(
        world.lines("starts").len(),
        2,
        "the first turn's server and the lane's"
    );
}

/// Issue 864. The event of the answer that took two steered words in is lost, so the lane never
/// writes their reading as it happens, and the ids around them are skewed (a message stored
/// before them sorts after them). At the idle, message order holds the turn open; at the recheck
/// the session's stored list holds the answer, whose parent is the later of the two: both are
/// read into the turn then, the first proven by the later steer of the turn's own, whatever the
/// ids say. Nothing is deleted, and they are answered exactly once.
#[test]
fn words_whose_answer_was_never_told_are_read_by_the_stored_list_at_the_recheck() {
    let world = world("opencode-lane-lost-answer", "");
    let _lanes = Lanes(world.place.root.clone());
    std::fs::create_dir_all(world.place.root.join("work")).unwrap();
    let host = world.host();
    let socket = world.agent(&host, "lost", "opencode");
    asked(&host, "lost", "ask-first");
    lane_ready(&socket);

    host.send("lost", "hold-a", "a");
    wait_for("the held turn", || {
        world.place.root.join("holding-a").exists()
    });
    host.send("lost", "lost-b", "b");
    wait_for("the first steer", || steers(&world).len() == 1);
    host.send("lost", "lost-c", "c");
    wait_for("the second steer", || steers(&world).len() == 2);
    std::fs::write(world.place.root.join("release-a"), "").unwrap();
    wait_for("the answer to the words", || {
        answers(&host, "lost", "said-lost-b+lost-c") == 1
    });
    // The answer names the later steer: both were taken in, the first proven by the second.
    wait_for("the words read into the turn at the recheck", || {
        read_at(&host, "lost", "u-b") == "steer" && read_at(&host, "lost", "u-c") == "steer"
    });
    wait_for("the turn to end", || host.state("lost") == "done");
    assert!(
        world.lines("deleting").is_empty(),
        "{:?}",
        world.lines("deleting")
    );
    asked(&host, "lost", "ask-after");
    assert_eq!(prompted(&world), ["ask-first", "hold-a", "ask-after"]);
    assert_eq!(answers(&host, "lost", "said-lost-b+lost-c"), 1);
    assert_eq!(
        world
            .lines("answered")
            .iter()
            .filter(|w| *w == "lost-b+lost-c")
            .count(),
        1
    );
}

/// Issue 864. The lane's loop falls more than five seconds behind (here a permission it answers
/// takes 6.5 s) right after an idle that message order alone held open, with the new run's busy
/// event already waiting. The recheck fires only once nothing waits: the busy event is taken
/// first and cancels it, so the words are read into the turn and answered once, and nothing is
/// deleted.
#[test]
fn a_late_loop_takes_the_waiting_busy_event_before_its_recheck() {
    let world = world("opencode-lane-slow-loop", "");
    let _lanes = Lanes(world.place.root.clone());
    std::fs::create_dir_all(world.place.root.join("work")).unwrap();
    let host = world.host();
    let socket = world.agent(&host, "slow", "opencode");
    asked(&host, "slow", "ask-first");
    lane_ready(&socket);

    host.send("slow", "hold-a", "a");
    wait_for("the held turn", || {
        world.place.root.join("holding-a").exists()
    });
    host.send("slow", "slow-b", "b");
    wait_for("the steer", || steers(&world).len() == 1);
    let began = Instant::now();
    std::fs::write(world.place.root.join("release-a"), "").unwrap();
    wait_for("the answer to the words", || {
        answers(&host, "slow", "said-slow-b") == 1
    });
    wait_for("the turn to end", || host.state("slow") == "done");
    assert!(
        began.elapsed() > Duration::from_secs(6),
        "{:?}",
        began.elapsed()
    );
    assert!(
        world.lines("deleting").is_empty(),
        "{:?}",
        world.lines("deleting")
    );
    assert_eq!(read_at(&host, "slow", "u-b"), "steer");
    asked(&host, "slow", "ask-after");
    assert_eq!(prompted(&world), ["ask-first", "hold-a", "ask-after"]);
    assert_eq!(answers(&host, "slow", "said-slow-b"), 1);
}

/// Issue 864. The recheck's read of the session's messages fails: the turn stays open for one more
/// recheck, whose read succeeds and decides. The words' answer is there (its events were lost),
/// with the steer as its parent: they are read into the turn, nothing is deleted, and they are
/// answered once.
#[test]
fn a_failed_read_at_the_recheck_is_tried_once_more_and_that_read_decides() {
    let world = world("opencode-lane-flaky-read", "");
    let _lanes = Lanes(world.place.root.clone());
    std::fs::create_dir_all(world.place.root.join("work")).unwrap();
    let host = world.host();
    let socket = world.agent(&host, "flaky", "opencode");
    asked(&host, "flaky", "ask-first");
    lane_ready(&socket);

    host.send("flaky", "hold-a", "a");
    wait_for("the held turn", || {
        world.place.root.join("holding-a").exists()
    });
    host.send("flaky", "flaky-b", "b");
    wait_for("the steer", || steers(&world).len() == 1);
    std::fs::write(world.place.root.join("release-a"), "").unwrap();
    wait_for("the words read into the turn at the second recheck", || {
        read_at(&host, "flaky", "u-b") == "steer"
    });
    wait_for("the turn to end", || host.state("flaky") == "done");
    assert_eq!(world.lines("reads"), ["read", "failed", "read"]);
    assert!(
        world.lines("deleting").is_empty(),
        "{:?}",
        world.lines("deleting")
    );
    asked(&host, "flaky", "ask-after");
    assert_eq!(prompted(&world), ["ask-first", "hold-a", "ask-after"]);
    assert_eq!(answers(&host, "flaky", "said-flaky-b"), 1);
}

/// Issue 864. Both rechecks' reads of the session's messages fail: after the second the turn
/// ends as its idle would have, once, without the list. The words, which no run took in, are
/// deleted and run once as a turn of their own.
#[test]
fn two_failed_reads_end_the_turn_once_without_the_list() {
    let world = world("opencode-lane-broken-read", "");
    let _lanes = Lanes(world.place.root.clone());
    std::fs::create_dir_all(world.place.root.join("work")).unwrap();
    let host = world.host();
    let socket = world.agent(&host, "broken", "opencode");
    asked(&host, "broken", "ask-first");
    lane_ready(&socket);

    host.send("broken", "hold-a", "a");
    wait_for("the held turn", || {
        world.place.root.join("holding-a").exists()
    });
    host.send("broken", "broken-b", "b");
    wait_for("the steer", || steers(&world).len() == 1);
    let steered = steers(&world)[0][1].clone();
    std::fs::write(world.place.root.join("release-a"), "").unwrap();
    wait_for("the words taken back", || {
        world.lines("deleted") == [steered.as_str()]
    });
    assert_eq!(world.lines("reads"), ["read", "failed", "failed"]);
    wait_for("the words answered on their own", || {
        answers(&host, "broken", "said-broken-b") == 1
    });
    wait_for("the last turn to end", || host.state("broken") == "done");
    assert_eq!(prompted(&world), ["ask-first", "hold-a", "broken-b"]);
    assert_ne!(read_at(&host, "broken", "u-b"), "steer");
    assert_eq!(answers(&host, "broken", "said-hold-a"), 1);
    assert_eq!(answers(&host, "broken", "said-broken-b"), 1);
}

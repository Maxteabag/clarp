#!/usr/bin/env python3
import base64, json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pathlib, signal, socket, struct, subprocess
root = pathlib.Path(os.environ["FAKE_ROOT"])
LOG = str(root / "requests")
session = "ses_" + os.environ.get("CLAUDE_PWA_SESSION", "once")
if sys.argv[1:] == ["seed-history"]:
    import sqlite3
    store = root / "opencode"
    store.mkdir(exist_ok=True)
    (store / "models.json").write_text(json.dumps({"fake":{"name":"Local Fake","models":{"DeepSeek":{}}},
                                                  "other":{"name":"Other","models":{"other":{}}}}))
    (store / "auth.json").write_text(json.dumps({"fake":{"type":"oauth"},"other":{"type":"api"}}))
    with sqlite3.connect(store / "opencode.db") as db:
        db.executescript("""CREATE TABLE session(id text, directory text, title text, time_updated integer,time_archived integer,parent_id text);
          CREATE TABLE message(id text, session_id text, time_created integer,data text);
          CREATE TABLE part(id text, session_id text, message_id text,data text);""")
        for sid,parent,archived in [("ses_stored",None,None),("ses_sub","ses_stored",None),("ses_archived",None,123)]:
            db.execute("INSERT INTO session VALUES(?,?,?,?,?,?)",(sid,"/work","Native title",1789544308000,archived,parent))
        for mid,role,parts in [("u","user",[{"type":"text","text":"first real prompt"}]),
                              ("a","assistant",[{"type":"tool","callID":"z","tool":"bash","state":{"input":{"command":"make check"},"status":"completed","output":"ok"}},
                                                {"type":"text","text":"checks passed"}])]:
            db.execute("INSERT INTO message VALUES(?,?,?,?)",(mid,"ses_stored",1 if role=="user" else 2,json.dumps({"role":role})))
            for i,part in enumerate(parts):
                db.execute("INSERT INTO part VALUES(?,?,?,?)",(mid+str(i),"ses_stored",mid,json.dumps(part)))
    sys.exit(0)
if "--version" in sys.argv:
    print("9.9");sys.exit(0)
if sys.argv[1:]==["models","--verbose"]:
    print("fake/Other\nfake/org/DeepSeek");sys.exit(0)

if sys.argv[1] == "run":
    with open(LOG, "a") as f:
        f.write(json.dumps({"run": sys.argv[1:]}) + "\n")
    print(json.dumps({"type": "text", "sessionID": session,
                      "part": {"text": "from run"}}))
    sys.exit(0)
assert sys.argv[1:] == ["serve", "--hostname", "127.0.0.1", "--port", "0"]
dependent = None
def terminate_server(*_):
    (root/"server-term").write_text(str(os.getpid()))
    if (root/"resist-term").exists():
        return
    if dependent is not None:
        dependent.terminate()
        dependent.wait(timeout=2)
        (root/"dependent-reaped").write_text(str(dependent.pid))
    sys.exit(0)

signal.signal(signal.SIGTERM, terminate_server)
if (root/"before-subscription").exists():
    # A local language-server stand-in keeps the turn's stderr until shutdown.
    dependent = subprocess.Popen([sys.executable,"-c","import time;time.sleep(12)"],
                                 stdout=subprocess.DEVNULL, start_new_session=True)
if (root/"fallback").exists():
    os.close(1)
    signal.alarm(12)
    while True: signal.pause()
with open(root/"pid", "w") as f:
    f.write(str(os.getpid()))
def _events(sid="ses_new"):
    def part(**fields):
        return {"type": "message.part.updated", "properties": {"part": {
            "sessionID": sid, "messageID": "msg_a", **fields}}}
    return [
        {"type":"session.idle","properties":{"sessionID":sid}},
        {"type":"session.status","properties":{"sessionID":"other","status":{"type":"busy"}}},
        {"type":"permission.asked","properties":{"sessionID":"other","id":"wrong"}},
        {"type":"question.asked","properties":{"sessionID":"other","id":"wrong"}},
        {"type":"session.created","properties":{"info":{"id":"child","parentID":sid}}},
        {"type":"question.asked","properties":{"sessionID":"child","id":"q1"}},
        {"type":"permission.asked","properties":{"sessionID":"child","id":"p2"}},
        # The prompt's own part, before any message.updated names its role
        # (the order a real server uses).
        {"type": "message.part.updated", "properties": {"part": {
            "id": "prt_u", "sessionID": sid, "messageID": "msg_u", "type": "text",
            "text": "<speak>the prompt</speak>", "time": {"end": 1}}}},
        {"type": "session.status", "properties": {"sessionID": sid,
                                                  "status": {"type": "busy"}}},
        part(id="prt_s1", type="step-start"),
        part(id="prt_r",type="reasoning",text=""),
        {"type":"message.part.delta","properties":{"sessionID":sid,"partID":"prt_r","field":"text","delta":"Check this"}},
        part(id="prt_r",type="reasoning",text="Check this.",time={"end":2}),
        part(id="hidden1",type="text",text="synthetic",synthetic=True),
        part(id="hidden2",type="text",text="private annotation",ignored=True),
        part(id="prt_t1", type="text", text=""),
        {"type": "message.part.delta", "properties": {
            "sessionID": sid, "messageID": "msg_a", "partID": "prt_t1",
            "field": "text", "delta": "<speak>On it.</speak>"}},
        {"type": "message.part.delta", "properties": {
            "sessionID": sid, "messageID": "msg_a", "partID": "prt_t1",
            "field": "text", "delta": " Checking."}},
        part(id="prt_b", type="tool", tool="bash", callID="c1",
             state={"status": "pending", "input": {}}),
        part(id="prt_b", type="tool", tool="bash", callID="c1",
             state={"status": "running", "input": {"command": "make test"}}),
        {"type": "permission.asked", "properties": {"id": "per_1", "sessionID": sid}},
        part(id="prt_b", type="tool", tool="bash", callID="c1",
             state={"status": "completed", "input": {"command": "make test"},
                    "output": "ok"}),
        part(id="prt_f1", type="step-finish", reason="tool-calls", cost=0.1,
             tokens={"input": 5, "output": 2, "cache": {"read": 50, "write": 0}}),
        part(id="prt_s2", type="step-start"),
        part(id="prt_t2", type="text", text="All green.", time={"end": 1}),
        part(id="prt_f2", type="step-finish", reason="stop", cost=0.1,
             tokens={"input": 5, "output": 2, "cache": {"read": 50, "write": 0}}),
        {"type": "session.status", "properties": {"sessionID": sid,
                                                  "status": {"type": "idle"}}},
    ]
EVENTS = _events(session)
PASSWORD = os.environ["OPENCODE_SERVER_PASSWORD"]
prompted = threading.Event()

def record(entry):
    with open(LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")

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
    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"null") if n else None
    def do_GET(self):
        if not self._auth():
            return
        path = self.path.split("?")[0]
        record({"method": "GET", "path": self.path})
        if path == "/event":
            while (root / "before-subscription").exists():
                time.sleep(0.02)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            def send(event):
                raw = ("data: " + json.dumps(event) + "\n\n").encode()
                self.wfile.write(b"%x\r\n%s\r\n" % (len(raw), raw))
                self.wfile.flush()
            send({"type": "server.connected", "properties": {}})
            prompted.wait(10)
            while (root/"during-restart").exists(): time.sleep(0.02)
            for event in EVENTS:
                if event == "HANG":
                    while True:
                        time.sleep(0.2)
                send(event)
            while True:
                time.sleep(0.5)
                send({"type": "server.heartbeat", "properties": {}})
        if path.startswith("/session/"):
            sid = path.split("/")[2]
            if (root/"wrong").exists(): return self._json(200, {"id": "different"})
            if sid == session and not (root/"gone").exists():
                return self._json(200, {"id": sid})
            return self._json(404, {"name": "NotFoundError"})
        self._json(404, {})
    def do_POST(self):
        if not self._auth():
            return
        body = self._body()
        path = self.path.split("?")[0]
        record({"method": "POST", "path": self.path, "body": body})
        if path == "/session":
            return self._json(200, {"id": session})
        if path.endswith("/prompt_async"):
            if (root/"signal-shim").exists():
                with socket.socket(socket.AF_UNIX) as peer:
                    peer.connect(os.environ["CLARP_SHIM_SOCKET"])
                    shim_pid, _, _ = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
                    argv = pathlib.Path(f"/proc/{shim_pid}/cmdline").read_bytes().split(b"\0")
                    # Keep this test's view of the inode across normal settled-turn cleanup.
                    os.link(argv[argv.index(b"--spool") + 1], root / "signalled-spool")
                    os.kill(shim_pid, signal.SIGTERM)
            if (root/"hang").exists():
                EVENTS[:] = [e for e in EVENTS if e["type"]=="session.status" and e["properties"]["sessionID"]==session][:1]+["HANG"]
            if (root/"fail").exists(): EVENTS[:] = [{"type":"session.status","properties":{"sessionID":session,"status":{"type":"busy"}}}, {"type":"session.error","properties":{"sessionID":session,"error":{"data":{"message":"synthetic refusal"}}}}, EVENTS[-1]]
            prompted.set()
            self.send_response(204); self.send_header("Content-Length", "0"); self.end_headers()
            return
        self._json(200, True)

server = ThreadingHTTPServer(("127.0.0.1", 0), H)
print(f"opencode server listening on http://127.0.0.1:{server.server_address[1]}", flush=True)
server.serve_forever()

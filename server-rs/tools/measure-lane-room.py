"""How long an agent's turn takes to start when the lanes are full and when room is free (issue
908), for one Host binary or two compared turn about.

A disposable release Host runs agents on lanes with a stand-in for the tool. Claude Code's
stand-in costs a fixed time to start (`STARTUP_S`, as a real one reads its conversation) and
answers each message at once; OpenCode's is the stand-in server of the Host's OpenCode lane
tests. Every turn is timed from the send to the moment the stand-in reads the message (its tool
start) and to the moment the Host shows the answer (its first output). A Claude turn's first
output is split into its parts: until a Claude Code started for it was born (none for a warm
lane), from then until it read the message (its start, `STARTUP_S` of it), and from its answer
to the Host showing it. Cases, each on a Host of its own:

  owner-full    the lanes over a limit no idle lane leaving can reach (one busy lane, four idle,
                the count fixed at one), an owner's agent's turns 1 and 2
  helper-full   the same, a helper's agent's turns 1 and 2 (sent by another agent); and which
                idle lanes left: never one of an agent the owner talks with
  room-free     room within the limits, turns 1 and 2
  busy-only     the count fixed at one, held by a busy lane: no idle lane may leave, so every
                turn runs in a process of its own, and the busy lane is never asked to leave
  swap-exit     owner-full, with every stand-in taking EXIT_S to end once its input closes, as
                Claude Code may: what a lane that leaves for another's place costs the new one
  contention    per round, one idle helper lane over a count of one, and an owner's agent and a
                helper's agent starting their first turns at the same moment: which took the
                place, and what the helper's start waited
  oc-healthy    OpenCode: turn 1 runs on its own server while the lane starts beside it; how
                long until the lane is ready, and whether turns 2 and 3 run on it
  oc-locked     the same, with the lane's first server ending before it listens (its database
                locked by the turn's own server): started once more, or no lane

Run through `tools/build lane-room-read` on the Host binary of the checkout it runs in, or
directly in private namespaces with up to four binaries (each with its clarp-shim beside it):
they run each case turn about, the order turned by one each round (ROUNDS, default 1), so all see
the same machine. N agents per case (default 5). With OUT set, every turn's numbers are written
there as JSON lines.
"""
import http.client
import json
import os
from pathlib import Path
import re
import signal
import socket
import statistics
import subprocess
import sys
import threading
import time
import uuid

N = int(os.environ.get("N", "5"))
ROUNDS = int(os.environ.get("ROUNDS", "1"))
STARTUP_S = 1.5
EXIT_S = 2.0
OC_STARTUP_S = 0.5

# The stand-in: Claude Code's stream-json, for the lane (many messages) and a turn of its own
# (one message, then its input closes). What it does is noted beside it, with the time.
STANDIN = r'''
import json, os, pathlib, re, sys, time
born = time.time()
root = pathlib.Path(__file__).resolve().parent
if "--version" in sys.argv:
    print("2.1.321 (Claude Code)"); sys.exit(0)
flag = "--resume" if "--resume" in sys.argv else "--session-id"
session = sys.argv[sys.argv.index(flag) + 1]
def note(name, text):
    with (root / name).open("a") as f:
        f.write(text + "\n")
def read(name, default):
    try:
        return float((root / name).read_text())
    except (OSError, ValueError):
        return default
own = "set" if os.environ.get("CLARP_PROVIDER_TURN") else "lane"
note("born", f"{born} {own} {session}")
time.sleep(read("startup-s", 0.0))
note("launches", f"{time.time()} {own} {flag}")
def say(kind, **fields):
    print(json.dumps({"type": kind, "session_id": session, **fields}), flush=True)
for line in sys.stdin:
    record = json.loads(line)
    if record.get("type") == "control_request":
        say("control_response", response={"subtype": "success",
            "request_id": record["request_id"], "response": {}})
        continue
    uid = record["uuid"]
    text = record["message"]["content"][0]["text"]
    found = re.search(r"\b(?:ask|hold)-[a-z0-9]+", text)
    word = found.group(0) if found else "none"
    note("reads", f"{time.time()} {word} {own}")
    say("system", subtype="init")
    say("user", isReplay=True, uuid=uid, message={"role": "user", "content": []})
    if word.startswith("hold-"):
        while not (root / ("release-" + word)).exists():
            time.sleep(0.05)
    note("prints", f"{time.time()} {word}")
    say("assistant", message={"id": "msg_" + word, "role": "assistant",
        "content": [{"type": "text", "text": "said-" + word}]})
    say("result", subtype="success", is_error=False, result="said-" + word,
        user_message_uuids=[uid])
time.sleep(read("exit-s", 0.0))
note("exits", f"{time.time()} {session}")
'''


def opencode_serve():
    """The stand-in `opencode serve` of the Host's OpenCode lane tests, taken from them."""
    tests = Path(__file__).resolve().parent.parent / "crates/clarp-host/tests/host/opencode_chat_lane.rs"
    found = re.search(r'const SERVE: &str = r#"(.*?)\n"#;', tests.read_text(), re.S)
    if not found:
        raise RuntimeError(f"no SERVE stand-in in {tests}")
    return found.group(1)


def private_namespace():
    """Whether this process is the first of a PID namespace of its own: then every other process
    in /proc is one it started, and may be ended."""
    return os.getpid() == 1


class Host:
    def __init__(self, binary, root, exit_s=0.0, backend="claude"):
        self.root = root
        self.backend = backend
        (root / "config/clarp-host").mkdir(parents=True)
        (root / "home/.claude").mkdir(parents=True)
        (root / "work").mkdir()
        env = {"HOME": str(root / "home"), "TMPDIR": str(root), "PATH": "/usr/bin:/bin",
               "XDG_CONFIG_HOME": str(root / "config"), "XDG_DATA_HOME": str(root / "xdg-data"),
               "CLARP_HEARTBEATS_DISABLED": "1"}
        if backend == "claude":
            standin = root / "claude-standin"
            standin.write_text(f"#!{sys.executable}\n{STANDIN}")
            standin.chmod(0o755)
            (root / "startup-s").write_text(str(STARTUP_S))
            (root / "exit-s").write_text(str(exit_s))
            env["CLARP_CLAUDE_PROGRAM"] = str(standin)
        else:
            serve = root / "opencode-serve.py"
            serve.write_text(opencode_serve())
            # As a real server takes a moment to open its database before it listens.
            program = root / "opencode"
            program.write_text(f"#!/bin/sh\ncase \"$1\" in serve) sleep {OC_STARTUP_S};; esac\n"
                               f"exec {sys.executable} {serve} \"$@\"\n")
            program.chmod(0o755)
            env["CLARP_OPENCODE_PROGRAM"] = str(program)
            env["CLARP_OPENCODE_LIVE"] = "1"
        self.most(8)
        cmd = [binary, "--listen", "127.0.0.1:0", "--data", root / "data", "--run", root / "run",
               "--cwd", root / "work", "--engine", "sqlite", "--accounts", "none",
               "--config", root / "config/clarp-host", "--no-lane-pressure"]
        self.process = subprocess.Popen(cmd, cwd=root, env=env, stderr=subprocess.PIPE, text=True)
        prefix = "clarp-host listens on "
        while True:
            line = self.process.stderr.readline()
            if not line:
                raise RuntimeError("scratch Host failed to listen")
            if line.startswith(prefix):
                break
        self.log = []
        threading.Thread(target=lambda: [self.log.append(l) for l in self.process.stderr],
                         daemon=True).start()
        host, port = line[len(prefix):].strip().rsplit(":", 1)
        self.address = (host.removeprefix("http://"), int(port))

    def most(self, most):
        lane = "claude_chat_lane" if self.backend == "claude" else "opencode_chat_lane"
        (self.root / "config/clarp-host/config.toml").write_text(
            f"{lane} = true\ncodex_lane_idle_minutes = 60\n"
            f"lane_limits = \"fixed\"\ncodex_lane_most = {most}\n")

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection(*self.address, timeout=30)
        data = json.dumps(body).encode() if body is not None else None
        connection.request(method, path, body=data,
                           headers={"Content-Type": "application/json"} if data else {})
        answer = connection.getresponse()
        text = answer.read()
        connection.close()
        return answer.status, json.loads(text) if text else None

    def agent(self, session):
        status, made = self.request("POST", "/agents",
                                    {"name": session, "session": session,
                                     "backend": self.backend, "cwd": str(self.root / "work")})
        if status not in (200, 201):
            raise RuntimeError(f"POST /agents {status}: {made}")
        _, snapshot = self.request("GET", "/agents/snapshot")
        return next(a["agent_id"] for a in snapshot["agents"] if a["session"] == session)

    def settled(self, session):
        """Whether the agent's turn has ended with nothing queued behind it: the snapshot row's
        `busy`, `queued_turn_count` and `latest_state`."""
        _, snapshot = self.request("GET", "/agents/snapshot")
        row = next(a for a in snapshot["agents"] if a["session"] == session)
        return (not row.get("busy") and not row.get("queued_turn_count")
                and row.get("latest_state") == "done")

    def send(self, session, word, helper=False):
        body = {"session": session, "text": word, "client_msg_id": str(uuid.uuid4())}
        if helper:
            body["origin"] = "agent"
        sent = time.time()
        status, answer = self.request("POST", "/send", body)
        if status not in (200, 201, 202):
            raise RuntimeError(f"POST /send {status}: {answer}")
        return sent

    def answered(self, session, word, timeout=60):
        until = time.time() + timeout
        while time.time() < until:
            _, log = self.request("GET", f"/log?session={session}&limit=50")
            if any(t.get("role") == "assistant" and t.get("text") == f"said-{word}"
                   for t in log.get("turns", [])):
                return time.time()
            time.sleep(0.01)
        raise RuntimeError(f"no answer to {word}")

    def done(self, session, timeout=60):
        until = time.time() + timeout
        while time.time() < until:
            if self.settled(session):
                return
            time.sleep(0.05)
        raise RuntimeError(f"{session} did not finish")

    def turn(self, session, word, helper=False):
        """A turn's numbers: `tool` (send to the stand-in reading it) and `first` (send to the
        Host showing the answer); for Claude also `born` (send to a Claude Code started for it,
        None for a warm lane) and `shown` (the stand-in's answer to the Host showing it)."""
        sent = self.send(session, word, helper)
        first = self.answered(session, word)
        self.done(session)
        timed = {"first": first - sent}
        if self.backend == "claude":
            read = next(float(l.split()[0]) for l in self.noted("reads") if l.split()[1] == word)
            printed = next(float(l.split()[0]) for l in self.noted("prints")
                           if l.split()[1] == word)
            born = [float(l.split()[0]) for l in self.noted("born")
                    if sent <= float(l.split()[0]) <= read]
            timed.update(tool=read - sent, born=(max(born) - sent) if born else None,
                         shown=first - printed)
        return timed

    def noted(self, name):
        try:
            return (self.root / name).read_text().splitlines()
        except OSError:
            return []

    def cold(self):
        _, said = self.request("GET", "/lane-settings")
        return said["cold_starts_last_hour"]["tools"].get(self.backend, 0)

    def inspect(self, agent_id):
        suffix = "cll" if self.backend == "claude" else "ocl"
        path = self.root / "run" / f"{agent_id}-{suffix}.sock"
        try:
            with socket.socket(socket.AF_UNIX) as s:
                s.settimeout(5)
                s.connect(str(path))
                s.sendall(b'{"op": "inspect"}\n')
                said = s.makefile("rb").readline()
            return json.loads(said).get("state")
        except (OSError, ValueError):
            return None

    def lane(self, agent_id):
        return self.inspect(agent_id) is not None

    def close(self):
        """Ends the Host and everything it left running, and waits for them, so nothing writes
        into its scratch folder once it is removed: a lane, and the stand-in it was asked to
        retire, outlive the Host by design, and a retired stand-in notes its exit EXIT_S later
        (what failed run8). What was still running is said."""
        for path in (self.root / "run").glob("*.sock"):
            try:
                with socket.socket(socket.AF_UNIX) as s:
                    s.settimeout(2)
                    s.connect(str(path))
                    s.sendall(b'{"op": "retire"}\n')
            except OSError:
                pass
        self.process.kill()
        self.process.wait()
        self.left = end_the_rest()


def end_the_rest(bound=10.0):
    """In a PID namespace of its own, every process but this one is one the case left: each is
    killed and reaped. Their names, as they were found."""
    if not private_namespace():
        return []
    me = os.getpid()
    found = {}
    until = time.time() + bound
    while time.time() < until:
        live = []
        for entry in os.listdir("/proc"):
            if not entry.isdigit() or int(entry) == me:
                continue
            try:
                state = Path(f"/proc/{entry}/stat").read_text()
            except OSError:
                continue
            name = state[state.index("(") + 1:state.rindex(")")]
            found.setdefault(int(entry), name)
            if state[state.rindex(")") + 2] != "Z":
                live.append(int(entry))
                try:
                    os.kill(int(entry), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if pid == 0:
                break
        if not live and not any(e.isdigit() and int(e) != me for e in os.listdir("/proc")):
            return sorted(set(found.values()))
        time.sleep(0.05)
    raise RuntimeError(f"processes left after the case: {found}")


def wait_noted(host, name, word, timeout=30):
    until = time.time() + timeout
    while not any(line.split()[1] == word for line in host.noted(name)):
        if time.time() > until:
            raise RuntimeError(f"{word} never in {name}")
        time.sleep(0.05)


def full(host):
    """One busy lane and four idle ones (two of agents the owner talks with, two of helpers'),
    then a count of one: no idle lane leaving brings the lanes within it."""
    ids = {}
    for name, helper in (("ownerone", False), ("ownertwo", False),
                         ("helperone", True), ("helpertwo", True), ("busy", False)):
        ids[name] = host.agent(name)
        host.turn(name, f"ask-{name}", helper)
    host.send("busy", "hold-busy")
    wait_noted(host, "reads", "hold-busy")
    host.most(1)
    return ids


def busy_only(host):
    """One busy lane and no idle one, the count fixed at one."""
    ids = {"busy": host.agent("busy")}
    host.turn("busy", "ask-busy")
    host.send("busy", "hold-busy")
    wait_noted(host, "reads", "hold-busy")
    host.most(1)
    return ids


def scratch_case(binary, name, body, exit_s=0.0, backend="claude"):
    import scratch
    with scratch.directory(f"clarp-lane-room-{name}-") as path:
        host = Host(binary, Path(path), exit_s, backend)
        try:
            result = body(host)
        finally:
            host.close()
        result["left"] = host.left
        result["log"] = [l.strip()[:200] for l in host.log if "lane" in l][-8:]
        return result


def claude_case(binary, name, exit_s, setup, helper):
    def body(host):
        ids = setup(host)
        cold = host.cold()
        turns = []
        for n in range(N):
            session = f"{name.replace('-', '')}{n}"
            host.agent(session)
            turns.append({"turn": 1, **host.turn(session, f"ask-{session}a", helper)})
            turns.append({"turn": 2, **host.turn(session, f"ask-{session}b", helper)})
        cold = host.cold() - cold
        kept = {k: host.lane(v) for k, v in ids.items()}
        if "busy" in ids:
            busy_reads = sum(1 for l in host.noted("reads") if l.split()[1] == "hold-busy")
            kept["busy-still-holding"] = kept["busy"] and busy_reads == 1
            (host.root / "release-hold-busy").write_text("")
        return {"turns": turns, "cold": cold, "kept": kept}
    return scratch_case(binary, name, body, exit_s)


def contention(binary):
    """Per round: a helper's idle lane over a count of one (with a busy lane), then an owner's
    agent and a helper's agent start their first turns at the same moment."""
    def body(host):
        busy = host.agent("busy")
        host.turn("busy", "ask-busy")
        host.send("busy", "hold-busy")
        wait_noted(host, "reads", "hold-busy")
        turns, rounds = [], []
        owners = {}
        for n in range(N):
            host.most(8)
            spare = f"spare{n}"
            spare_id = host.agent(spare)
            host.turn(spare, f"ask-{spare}", helper=True)
            host.most(1)
            ours, theirs = f"owner{n}", f"helper{n}"
            ids = {ours: host.agent(ours), theirs: host.agent(theirs)}
            cold = host.cold()
            got = {}
            go = threading.Barrier(2)

            def run(session, helper):
                go.wait()
                got[session] = host.turn(session, f"ask-{session}", helper)
            threads = [threading.Thread(target=run, args=(ours, False)),
                       threading.Thread(target=run, args=(theirs, True))]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            turns.append({"who": "owner", **got[ours]})
            turns.append({"who": "helper", **got[theirs]})
            rounds.append({"owner-lane": host.lane(ids[ours]),
                           "helper-lane": host.lane(ids[theirs]),
                           "spare-lane": host.lane(spare_id),
                           "earlier-owner-lanes-kept": all(host.lane(v) for v in owners.values()),
                           "cold": host.cold() - cold})
            owners[ours] = ids[ours]
        busy_kept = host.lane(busy)
        (host.root / "release-hold-busy").write_text("")
        return {"turns": turns, "rounds": rounds, "kept": {"busy": busy_kept}}
    return scratch_case(binary, "contention", body)


def opencode_case(binary, name, locked):
    def body(host):
        turns, agents = [], []
        for n in range(N):
            session = f"{name.replace('-', '')}{n}"
            agent_id = host.agent(session)
            if locked:
                (host.root / "locked-once").write_text("")
            t1 = host.turn(session, f"ask-{session}a")
            # How long after turn 1 was sent until the lane is ready, 10 s at most.
            sent = time.time() - t1["first"]
            ready, until = None, time.time() + 10
            while time.time() < until:
                state = host.inspect(agent_id)
                if state and state.get("available"):
                    ready = time.time() - sent
                    break
                time.sleep(0.01)
            lane_pid = str((host.inspect(agent_id) or {}).get("pid", ""))
            t2 = host.turn(session, f"ask-{session}b")
            time.sleep(1.0)  # A lane that ends after its start ends within this.
            t3 = host.turn(session, f"ask-{session}c")
            ran = {l.split()[2]: l.split()[0] for l in host.noted("turns")}
            state3 = host.inspect(agent_id) or {}
            agents.append({"lane-ready": ready,
                           "locked-gone": not (host.root / "locked-once").exists(),
                           "turn2-on-lane": bool(lane_pid) and ran.get(f"ask-{session}b") == lane_pid,
                           "turn3-on-lane": ran.get(f"ask-{session}c") == str(state3.get("pid", "-")),
                           "lane-after": bool(state3)})
            for k, t in ((1, t1), (2, t2), (3, t3)):
                turns.append({"turn": k, **t})
            (host.root / "locked-once").unlink(missing_ok=True)
        return {"turns": turns, "agents": agents, "servers": len(host.noted("starts"))}
    return scratch_case(binary, name, body, backend="opencode")


CASES = {
    "owner-full": lambda b: claude_case(b, "owner-full", 0.0, full, False),
    "helper-full": lambda b: claude_case(b, "helper-full", 0.0, full, True),
    "room-free": lambda b: claude_case(b, "room-free", 0.0, lambda host: {}, False),
    "busy-only": lambda b: claude_case(b, "busy-only", 0.0, busy_only, False),
    "swap-exit": lambda b: claude_case(b, "swap-exit", EXIT_S, full, False),
    "contention": contention,
    "oc-healthy": lambda b: opencode_case(b, "oc-healthy", False),
    "oc-locked": lambda b: opencode_case(b, "oc-locked", True),
}


def median(values):
    values = [v for v in values if v is not None]
    return f"{statistics.median(values):.3f}" if values else "-"


def summary(result):
    groups = {}
    for t in result["turns"]:
        groups.setdefault(t.get("who") or f"t{t['turn']}", []).append(t)
    parts = []
    for key, ts in groups.items():
        line = f"{key} first {median(t['first'] for t in ts)}"
        if "tool" in ts[0]:
            line += (f" tool {median(t['tool'] for t in ts)} born {median(t['born'] for t in ts)}"
                     f" shown {median(t['shown'] for t in ts)}")
        parts.append(line)
    extra = {k: result[k] for k in ("cold", "kept", "servers") if k in result}
    if "agents" in result:
        a = result["agents"]
        extra["lane-ready"] = median(x["lane-ready"] for x in a)
        extra["lanes-ready"] = sum(x["lane-ready"] is not None for x in a)
        for k in ("locked-gone", "turn2-on-lane", "turn3-on-lane", "lane-after"):
            extra[k] = sum(x[k] for x in a)
    if "rounds" in result:
        r = result["rounds"]
        for k in ("owner-lane", "helper-lane", "spare-lane", "earlier-owner-lanes-kept"):
            extra[k] = sum(x[k] for x in r)
        extra["cold"] = sum(x["cold"] for x in r)
    return "; ".join(parts) + " | " + json.dumps(extra)


def main(binaries):
    arms = dict(zip("ABCD", binaries))
    names = os.environ.get("CASES", " ".join(CASES)).split()
    print(f"arms {arms}; N={N}; ROUNDS={ROUNDS}; stand-in start {STARTUP_S} s; cases {names}",
          flush=True)
    out = open(os.environ["OUT"], "a") if os.environ.get("OUT") else None
    every = {}
    for r in range(ROUNDS):
        for name in names:
            arm_names = list(arms)
            order = arm_names[r % len(arm_names):] + arm_names[:r % len(arm_names)]
            for arm in order:
                result = CASES[name](arms[arm])
                load = os.getloadavg()[0]
                print(f"round {r} {name} {arm} load {load:.0f}: {summary(result)}", flush=True)
                print(f"    left at close: {result['left']}", flush=True)
                for line in result["log"][-3:]:
                    print(f"    log: {line[:160]}", flush=True)
                every.setdefault((name, arm), []).append(result)
                if out:
                    out.write(json.dumps({"round": r, "case": name, "arm": arm, "load": load,
                                          **{k: v for k, v in result.items() if k != "log"}})
                              + "\n")
                    out.flush()
    print("== all rounds", flush=True)
    for (name, arm), results in every.items():
        merged = {"turns": [t for x in results for t in x["turns"]]}
        for key in ("agents", "rounds"):
            if key in results[0]:
                merged[key] = [a for x in results for a in x[key]]
        if "cold" in results[0] and "rounds" not in results[0]:
            merged["cold"] = sum(x["cold"] for x in results)
        if "servers" in results[0]:
            merged["servers"] = sum(x["servers"] for x in results)
        print(f"ALL {name} {arm} n={len(merged['turns'])}: {summary(merged)}", flush=True)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    if os.getpid() != 1:
        os.execvp("unshare", ["unshare", "--mount", "--pid", "--fork", "--kill-child",
                             "--mount-proc", "sh", "-ec",
                             'mount -t tmpfs tmpfs /run; exec "$@"', "lane-room-read",
                             sys.executable, __file__, *sys.argv[1:]])
    if socket.if_nameindex() != [(1, "lo")]:
        raise SystemExit("run through tools/build lane-room-read in its private namespaces")
    main([Path(b).resolve() for b in sys.argv[1:5]])

"""How long GET /agents/snapshot takes on an isolated release Host with thirty agents.

The snapshot is asked for often, and since the voice profile it resolves every agent's voice and
hearing from one view of the Host. This makes thirty agents of the scripted backend, asks for the
snapshot ten times to warm it, then times a hundred and prints the median, the 90th percentile
and the slowest, with the size of the answer. Run through `tools/build snapshot-read`.

Then what the apps download (issue 805): it grows the fleet to 310 agents, publishes a hundred
documents of about 80 KB, and for the snapshot and the artifact list prints the size of the answer
plain and gzipped, the time of an answer and the Host's CPU time per answer (its user and system
time from /proc), for a client that does not take gzip, one that does, and one that sends back
the tag it holds (feature `list_etags`). The documents are words
drawn from a small vocabulary, so their gzip ratio is near that of prose, not of repeated bytes.
"""
import gzip
import http.client
import json
import os
from pathlib import Path
import random
import socket
import statistics
import subprocess
import sys
import time

import scratch

# What a client asks with to be sent an answer gzipped also from this machine: a Host answers a
# client on loopback that takes plain with plain (issue 932).
GZIP_ONLY = "gzip, identity;q=0"
AGENTS = 30
WARM = 10
TIMED = 100


FLEET = 310
DOCUMENTS = 100
DOCUMENT_BYTES = 80_000
WIRE_TIMED = 30


def request(address, method, path, body=None, headers=None, full=False):
    conn = http.client.HTTPConnection(address, timeout=60)
    try:
        start = time.monotonic_ns()
        conn.request(method, path, body, {"Content-Type": "application/json", **(headers or {})})
        response = conn.getresponse()
        data = response.read()
        elapsed_ms = (time.monotonic_ns() - start) / 1_000_000
        if response.status not in (200, 201):
            raise RuntimeError(f"{method} {path} returned {response.status}")
        if full:
            return data, elapsed_ms, response
        return data, elapsed_ms
    finally:
        conn.close()


def request_any(address, path, headers):
    """A GET whose answer may be a 304: its body, its time and the answer."""
    conn = http.client.HTTPConnection(address, timeout=60)
    try:
        start = time.monotonic_ns()
        conn.request("GET", path, None, headers)
        response = conn.getresponse()
        data = response.read()
        return data, (time.monotonic_ns() - start) / 1_000_000, response
    finally:
        conn.close()


def cpu_ms(pid):
    """The process's user and system time so far, in milliseconds."""
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) * 1000 / os.sysconf("SC_CLK_TCK")


def document(rng, words):
    parts, size = [], 0
    while size < DOCUMENT_BYTES:
        word = rng.choice(words)
        parts.append(word)
        size += len(word) + 1
    return " ".join(parts)


def wire(address, pid):
    for n in range(AGENTS, FLEET):
        request(address, "POST", "/agents", json.dumps({"name": f"Agent{n:03d}", "backend": "scripted"}))
    data, _ = request(address, "GET", "/agents/snapshot")
    sessions = [agent["session"] for agent in json.loads(data)["agents"]]
    rng = random.Random(805)
    words = ["".join(rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(rng.randint(2, 10)))
             for _ in range(600)]
    for n in range(DOCUMENTS):
        body = {"session": sessions[n % len(sessions)], "type": "document", "title": f"Report {n}",
                "summary": "What it found", "payload": {"content": document(rng, words)}}
        request(address, "POST", "/artifacts", json.dumps(body))
    for path in ("/agents/snapshot", "/artifacts"):
        plain, _ = request(address, "GET", path)
        packed, _, answer = request(address, "GET", path, headers={"Accept-Encoding": GZIP_ONLY}, full=True)
        encoding = answer.getheader("Content-Encoding")
        if encoding != "gzip" or gzip.decompress(packed) != plain:
            raise RuntimeError(f"{path}: Content-Encoding {encoding!r}, or its body decodes to another")
        line = [f"{path}: {len(plain)} bytes plain, {len(packed)} gzipped ({len(packed) / len(plain):.1%})"]
        for name, headers in (("plain", {}), ("gzip", {"Accept-Encoding": GZIP_ONLY})):
            for _ in range(3):
                request(address, "GET", path, headers=headers)
            before = cpu_ms(pid)
            times = sorted(request(address, "GET", path, headers=headers)[1] for _ in range(WIRE_TIMED))
            spent = (cpu_ms(pid) - before) / WIRE_TIMED
            line.append(f"{name}: median {statistics.median(times):.1f} ms, "
                        f"p90 {times[int(WIRE_TIMED * 0.9) - 1]:.1f} ms, Host CPU {spent:.1f} ms an answer")
        tag = answer.getheader("ETag")
        if tag:
            held = {"Accept-Encoding": "gzip", "If-None-Match": tag}
            body, _, again = request_any(address, path, held)
            before = cpu_ms(pid)
            times = sorted(request_any(address, path, held)[1] for _ in range(WIRE_TIMED))
            spent = (cpu_ms(pid) - before) / WIRE_TIMED
            line.append(f"held tag: {again.status} with {len(body)} bytes, "
                        f"median {statistics.median(times):.1f} ms, Host CPU {spent:.1f} ms an answer")
        print("; ".join(line), flush=True)


def measure(address):
    for n in range(AGENTS):
        request(address, "POST", "/agents", json.dumps({"name": f"Agent{n:02d}", "backend": "scripted"}))
    data, _ = request(address, "GET", "/agents/snapshot")
    agents = json.loads(data)["agents"]
    if len(agents) < AGENTS:
        raise RuntimeError(f"{len(agents)} agents in the snapshot")
    for _ in range(WARM):
        request(address, "GET", "/agents/snapshot")
    times = sorted(request(address, "GET", "/agents/snapshot")[1] for _ in range(TIMED))
    profiled = sum(1 for agent in agents if "voice_profile" in agent)
    print(f"{len(agents)} rows ({profiled} with a voice profile), {len(data)} bytes: "
          f"median {statistics.median(times):.2f} ms, p90 {times[int(TIMED * 0.9) - 1]:.2f} ms, "
          f"slowest {times[-1]:.2f} ms over {TIMED}", flush=True)


def main(binary):
    with scratch.directory("clarp-snapshot-read-") as name:
        root = Path(name)
        cmd = [binary, "--listen", "127.0.0.1:0", "--data", root / "data",
               "--run", root / "run", "--cwd", root, "--engine", "sqlite", "--accounts", "none"]
        with subprocess.Popen(cmd, cwd=root, env={"HOME": name, "TMPDIR": name,
                              "PATH": str(root / "no-programs"), "CLARP_HEARTBEATS_DISABLED": "1"},
                              stderr=subprocess.PIPE, text=True) as host:
            try:
                prefix = "clarp-host listens on "
                startup = host.stderr.readline().strip()
                if not startup.startswith(prefix):
                    raise RuntimeError("scratch Host failed to listen")
                measure(startup[len(prefix):])
                wire(startup[len(prefix):], host.pid)
            finally:
                host.kill()


if __name__ == "__main__":
    # The runner creates the network namespace. Hide the manager sockets under /run
    # and other processes under /proc before starting even the disposable Host.
    if os.getpid() != 1:
        os.execvp("unshare", ["unshare", "--mount", "--pid", "--fork", "--kill-child",
                             "--mount-proc", "sh", "-ec",
                             'mount -t tmpfs tmpfs /run; exec "$@"', "snapshot-read",
                             sys.executable, __file__, *sys.argv[1:]])
    if socket.if_nameindex() != [(1, "lo")] or any(Path("/run").iterdir()):
        raise SystemExit("run through tools/build snapshot-read in its private namespaces")
    main(Path(sys.argv[1]).resolve())

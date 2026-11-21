"""How long GET /agents/snapshot takes on an isolated release Host with thirty agents.

The snapshot is asked for often, and since the voice profile it resolves every agent's voice and
hearing from one view of the Host. This makes thirty agents of the scripted backend, asks for the
snapshot ten times to warm it, then times a hundred and prints the median, the 90th percentile
and the slowest, with the size of the answer. Run through `tools/build snapshot-read`.
"""
import http.client
import json
import os
from pathlib import Path
import socket
import statistics
import subprocess
import sys
import time

import scratch

AGENTS = 30
WARM = 10
TIMED = 100


def request(address, method, path, body=None):
    conn = http.client.HTTPConnection(address, timeout=60)
    try:
        start = time.monotonic_ns()
        conn.request(method, path, body, {"Content-Type": "application/json"})
        response = conn.getresponse()
        data = response.read()
        elapsed_ms = (time.monotonic_ns() - start) / 1_000_000
        if response.status != 200:
            raise RuntimeError(f"{method} {path} returned {response.status}")
        return data, elapsed_ms
    finally:
        conn.close()


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

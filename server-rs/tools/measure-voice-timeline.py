"""One loopback GET per history size on an isolated release Host, after synthetic POSTs."""
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import scratch


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
        return json.loads(data), elapsed_ms
    finally:
        conn.close()


def measure(address):
    batch = json.dumps({"source": "ios", "events": [{"event": "level", "level_db": -24}] * 500})
    count = 0
    for target in (100_000, 1_000_000):
        deadline = time.monotonic() + 60
        before = count
        while count < target and time.monotonic() < deadline:
            posted, _ = request(address, "POST", "/voice-events", batch)
            if posted["stored"] != 500:
                raise RuntimeError("incomplete seed batch")
            count += 500
        if count == before:
            raise RuntimeError("no rows added")
        answer, elapsed = request(address, "GET", "/voice-events?limit=50")
        ids = [row["event_id"] for row in answer["events"]]
        if ids != list(range(count - 49, count + 1)):
            raise RuntimeError("GET did not return the newest fifty events in order")
        print(f"{count} rows: {elapsed:.1f} ms", flush=True)
        if count < target:
            break


def main(binary):
    with scratch.directory("clarp-voice-read-") as name:
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
                             'mount -t tmpfs tmpfs /run; exec "$@"', "voice-read",
                             sys.executable, __file__, *sys.argv[1:]])
    if socket.if_nameindex() != [(1, "lo")] or any(Path("/run").iterdir()):
        raise SystemExit("run through tools/build voice-timeline-read in its private namespaces")
    main(Path(sys.argv[1]).resolve())

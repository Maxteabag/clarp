"""Prove the CI test containment (scripts/ci_contained.sh) before any test runs; exit non-zero on
the first gap. Standard library only, no network use: every probe is zero-packet or loopback.

    python scripts/ci_containment_selfcheck.py <runner_home> <runner_temp> <workspace> [ignored...]
"""
from __future__ import annotations

import errno
import os
import pathlib
import socket
import stat
import subprocess
import sys

ENV_ALLOWED = {"HOME", "PATH", "LANG", "CI", "VIRTUAL_ENV", "UV_OFFLINE", "NPM_CONFIG_OFFLINE", "PWD", "SHLVL", "_"}


def fail(message: str) -> None:
    print(f"ci containment self-check FAILED: {message}", file=sys.stderr)
    sys.exit(3)


def check(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def main(runner_home: str, runner_temp: str, workspace: str) -> None:
    # Unprivileged, and unable to re-elevate.
    check(os.geteuid() != 0 and os.getegid() != 0, "running as root")
    check(os.getgroups() in ([], [os.getegid()]), f"supplementary groups kept: {os.getgroups()}")
    status = pathlib.Path("/proc/self/status").read_text()
    check("NoNewPrivs:\t1" in status, "no_new_privs is not set")
    check("CapBnd:\t0000000000000000" in status, "capability bounding set not empty")
    try:
        sudo = subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=10)
        check(sudo.returncode != 0, "sudo -n true succeeded")
    except FileNotFoundError:
        pass

    # Network: loopback only, no route, no resolution.
    names = {name for _, name in socket.if_nameindex()}
    check(names == {"lo"}, f"interfaces {sorted(names)}")
    check(pathlib.Path("/proc/net/route").read_text().splitlines()[1:] == [], "an IPv4 route exists")
    # A UDP connect sends no packet; it fails with ENETUNREACH only when there is no route at all,
    # which a host WITH egress would never report for these public resolvers.
    for family, address in ((socket.AF_INET, ("1.1.1.1", 53)), (socket.AF_INET6, ("2606:4700:4700::1111", 53))):
        try:
            probe = socket.socket(family, socket.SOCK_DGRAM)
        except OSError as error:
            check(family == socket.AF_INET6 and error.errno == errno.EAFNOSUPPORT, f"socket error {error!r}")
            continue
        try:
            probe.connect(address)
            fail(f"a route to {address[0]} exists")
        except OSError as error:
            check(error.errno == errno.ENETUNREACH, f"unexpected connect error to {address[0]}: {error!r}")
        finally:
            probe.close()
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    client = socket.create_connection(server.getsockname(), timeout=2)
    accepted, _ = server.accept()
    client.sendall(b"ok")
    check(accepted.recv(2) == b"ok", "loopback round-trip failed")
    for sock in (accepted, client, server):
        sock.close()

    # Docker is masked: the socket path is now the /dev/null character device.
    for path in ("/var/run/docker.sock", "/run/docker.sock"):
        if os.path.lexists(path):
            check(stat.S_ISCHR(os.stat(path).st_mode), f"{path} is not masked (not a character device)")

    # Only this process tree is visible; the runner's processes are not.
    pids = [p for p in os.listdir("/proc") if p.isdigit()]
    check(len(pids) < 50, f"{len(pids)} processes visible")
    for pid in pids:
        try:
            comm = pathlib.Path(f"/proc/{pid}/comm").read_text().strip()
        except OSError:
            continue
        check(not comm.startswith(("Runner.", "provjobd", "hosted-compute")), f"runner process visible: {comm}")

    # Credentials: exact env, empty HOME, the runner's home holds only the workspace path and the
    # interpreter, its temp dir is empty, and git has no stored auth header.
    extra = set(os.environ) - ENV_ALLOWED
    check(not extra, f"unexpected environment keys {sorted(extra)}")
    home = pathlib.Path(os.environ["HOME"])
    check(list(home.iterdir()) == [], "HOME is not empty")
    check(os.path.realpath(home) != os.path.realpath(runner_home), "HOME is the runner's home")
    kept = {pathlib.Path(workspace).relative_to(runner_home).parts[0]} if workspace.startswith(runner_home + "/") else set()
    interpreter = os.path.realpath(sys.executable)
    if interpreter.startswith(runner_home + "/"):
        kept.add(pathlib.Path(interpreter).relative_to(runner_home).parts[0])
    visible = {p.name for p in pathlib.Path(runner_home).iterdir()}
    check(visible <= kept, f"runner home still shows {sorted(visible - kept)}")
    if runner_temp and os.path.isdir(runner_temp):
        check(list(pathlib.Path(runner_temp).iterdir()) == [], "RUNNER_TEMP is not empty")
    header = subprocess.run(["git", "-C", workspace, "config", "--get-regexp", r"http\..*extraheader"],
                            capture_output=True, text=True)
    check(header.stdout.strip() == "", "git still holds an http extraheader (checkout credentials)")
    print("ci containment self-check passed", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) < 4:
        fail("usage: ci_containment_selfcheck.py <runner_home> <runner_temp> <workspace>")
    main(sys.argv[1], sys.argv[2], sys.argv[3])

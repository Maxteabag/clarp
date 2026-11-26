"""install-host restarts only the Host where its socket unit holds the port on the address its unit
says now, in the run of the socket (InvocationID) start_host last started; anything else starts the
socket again (issue 935).

A unit written with --files-only is on disk and not in effect: the next install must not take it
for the port the socket holds. start_host and port_free are run here alone, taken from the script,
with a stand-in systemctl that records each call: no unit is touched and no Host runs.
"""
import os
import pathlib
import socket
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[3]
INSTALL_HOST = pathlib.Path(os.environ.get("INSTALL_HOST_UNDER_TEST", ROOT / "server-rs/tools/install-host"))

SYSTEMCTL = """#!/bin/sh
# Stand-in systemctl: records each call; a unit is active while $STANDIN/active-<unit> exists; the
# socket's run is $STANDIN/invocation, a new one at every start of the socket (none: empty).
echo "$*" >> "$STANDIN/calls"
case "$*" in
    *"is-active --quiet "*) for unit; do :; done; [ -e "$STANDIN/active-$unit" ] ;;
    *"show clarp-host.socket -p InvocationID --value"*) cat "$STANDIN/invocation" 2>/dev/null || true ;;
    *"restart clarp-host.socket"*|*"start clarp-host.socket"*)
        touch "$STANDIN/active-clarp-host.socket"
        [ -e "$STANDIN/no-invocation" ] || echo "run$(date +%s%N)" > "$STANDIN/invocation" ;;
    *) exit 0 ;;
esac
"""


def free_address():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return f"127.0.0.1:{probe.getsockname()[1]}"


def machine(tmp_path, listen, recorded, active, run="run1"):
    standin, units, releases = tmp_path / "standin", tmp_path / "units", tmp_path / "releases"
    for d in (standin, units, releases):
        d.mkdir()
    (standin / "systemctl").write_text(SYSTEMCTL)
    (standin / "systemctl").chmod(0o755)
    (standin / "journalctl").write_text("#!/bin/sh\nexit 0\n")
    (standin / "journalctl").chmod(0o755)
    (units / "clarp-host.service").write_text("[Service]\nStandardInput=socket\n")
    (units / "clarp-host.socket").write_text(f"[Socket]\nListenStream={listen}\n")
    if recorded:
        (releases / "socket-listening").write_text(recorded + "\n")
    if active:
        (standin / "active-clarp-host.socket").touch()
    if run:
        (standin / "invocation").write_text(run + "\n")
    else:
        (standin / "no-invocation").touch()
    return standin, units, releases


def start_host(tmp_path, standin, units, releases, arg):
    script = ('set -u; eval "$(sed -n -e \'/^start_host() {/,/^}/p\' -e \'/^port_free() {/,/^}/p\' "$1")"; '
              f'units={units}; host_unit=clarp-host.service; socket_unit=clarp-host.socket; '
              f'releases={releases}; socket_listening=$releases/socket-listening; start_host {arg}')
    env = dict(os.environ, PATH=f"{standin}:{os.environ['PATH']}", STANDIN=str(standin))
    done = subprocess.run(["bash", "-c", script, "start_host", str(INSTALL_HOST)],
                          capture_output=True, text=True, env=env, timeout=60)
    calls = (standin / "calls").read_text().splitlines() if (standin / "calls").exists() else []
    return done, calls


def test_the_socket_in_effect_on_the_address_written_restarts_only_the_host(tmp_path):
    listen = free_address()
    standin, units, releases = machine(tmp_path, listen, recorded=f"{listen} run1", active=True)
    done, calls = start_host(tmp_path, standin, units, releases, 0)
    assert done.returncode == 0, done.stderr
    assert "--user restart clarp-host.service" in calls, calls
    assert not any("clarp-host.socket" in c and ("restart" in c or "stop" in c) for c in calls), calls


def test_a_listen_written_with_files_only_starts_the_socket_again(tmp_path):
    # The socket still holds the address it was last started on; the unit on disk says another.
    old, new = free_address(), free_address()
    standin, units, releases = machine(tmp_path, new, recorded=f"{old} run1", active=True)
    done, calls = start_host(tmp_path, standin, units, releases, 0)
    assert done.returncode == 0, done.stderr
    assert "--user restart clarp-host.socket" in calls, calls
    assert "--user start clarp-host.service" in calls, calls
    recorded = (releases / "socket-listening").read_text().split()
    assert recorded[0] == new and recorded[1] != "run1", recorded


def test_a_matching_address_from_an_earlier_run_of_the_socket_starts_it_again(tmp_path):
    # Recorded: address A in run1. Someone started the socket again since (run2, perhaps on B),
    # then A was staged again: the address matches, the run does not.
    listen = free_address()
    standin, units, releases = machine(tmp_path, listen, recorded=f"{listen} run1", active=True, run="run2")
    done, calls = start_host(tmp_path, standin, units, releases, 0)
    assert done.returncode == 0, done.stderr
    assert "--user restart clarp-host.socket" in calls, calls
    assert "--user restart clarp-host.service" not in calls, calls


def test_a_socket_whose_run_cannot_be_told_is_never_trusted_and_not_recorded(tmp_path):
    listen = free_address()
    standin, units, releases = machine(tmp_path, listen, recorded=f"{listen} run1", active=True, run=None)
    done, calls = start_host(tmp_path, standin, units, releases, 0)
    assert done.returncode == 0, done.stderr
    assert "--user restart clarp-host.socket" in calls, calls
    assert not (releases / "socket-listening").exists()


def test_a_socket_never_started_by_start_host_is_started_again(tmp_path):
    listen = free_address()
    standin, units, releases = machine(tmp_path, listen, recorded=None, active=True)
    done, calls = start_host(tmp_path, standin, units, releases, 0)
    assert done.returncode == 0, done.stderr
    assert "--user restart clarp-host.socket" in calls, calls
    assert (releases / "socket-listening").read_text().split()[0] == listen

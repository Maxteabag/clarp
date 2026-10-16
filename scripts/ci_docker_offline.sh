# Sourced by scripts/test_docker_node.sh and scripts/test_e2e_docker.sh (CI and local test runs only).
#
# Test containers of the built image run on a Docker network created --internal by the caller, so
# nothing in them (the Host, its janitors and heartbeats, the bundled claude and codex CLIs) has a
# route out. The runner reaches the container directly on that network's bridge; no port is
# published and the default bridge is never used. Every container also gets a resolv.conf whose
# only nameserver is the container's own loopback, so name resolution cannot be forwarded either.
# There is no fallback: if the network cannot be proven internal, or the container cannot be
# reached on it, the test stops.

CI_OFFLINE_NET=""
CI_OFFLINE_RESOLV=""
CI_OFFLINE_FORWARD_PID=""
CI_OFFLINE_URL=""

ci_offline_create() {
    # $1: a name unique to this run. Creates the network and the resolv.conf the containers mount.
    # Engines before 26.0.0 forwarded DNS for --internal networks from the host namespace
    # (CVE-2024-29018); require a patched engine so the embedded resolver forwards nothing.
    local engine
    engine="$(docker version -f '{{.Server.Version}}')"
    [[ "${engine%%.*}" =~ ^[0-9]+$ && "${engine%%.*}" -ge 26 ]] || {
        echo "docker engine $engine is older than 26.0.0 (internal-network DNS forwarding)" >&2; exit 1; }
    # The name is recorded as ours only after the create succeeded, so cleanup never removes a
    # network this run did not create.
    docker network create --internal --driver bridge "$1" >/dev/null
    CI_OFFLINE_NET="$1"
    [[ "$(docker network inspect -f '{{.Internal}}' "$CI_OFFLINE_NET")" == true ]] || {
        echo "network $CI_OFFLINE_NET is not internal" >&2; exit 1; }
    [[ "$(docker network inspect -f '{{.EnableIPv6}}' "$CI_OFFLINE_NET")" == false ]] || {
        echo "network $CI_OFFLINE_NET has IPv6 enabled" >&2; exit 1; }
    CI_OFFLINE_RESOLV="$(mktemp)"
    printf 'nameserver 127.0.0.1\noptions attempts:1 timeout:1\n' >"$CI_OFFLINE_RESOLV"
    chmod 0644 "$CI_OFFLINE_RESOLV"
}

ci_offline_cleanup() {
    # Only what this run created: its forwarder, the network (after its containers are gone) and
    # the resolv.conf.
    ci_offline_forward_stop
    [[ -n "$CI_OFFLINE_NET" ]] && docker network rm "$CI_OFFLINE_NET" >/dev/null 2>&1 || true
    [[ -n "$CI_OFFLINE_RESOLV" ]] && rm -f "$CI_OFFLINE_RESOLV" || true
}

ci_offline_run_args() {
    # The docker run arguments every test container of the image gets.
    printf '%s\n' --network "$CI_OFFLINE_NET" -v "$CI_OFFLINE_RESOLV:/etc/resolv.conf:ro" \
        -e CLARP_HEARTBEATS_DISABLED=1
}

ci_offline_preflight() {
    # $1: image. Proves, BEFORE any Host container starts, that a container of this image on this
    # network has no way out: no default route (IPv4 or IPv6), a
    # zero-packet UDP connect to public resolvers fails with ENETUNREACH, and resolv.conf names
    # only the container's own loopback, where nothing answers. The Host never runs in this probe.
    local -a args
    mapfile -t args < <(ci_offline_run_args)
    docker run --rm "${args[@]}" --entrypoint python3 "$1" -c '
import errno, os, socket
assert "lo" in {n for _, n in socket.if_nameindex()}   # the routes and probes below are the proof
routes = open("/proc/net/route").read().splitlines()[1:]
assert not [r for r in routes if r.split()[1] == "00000000"], routes          # no IPv4 default route
if os.path.exists("/proc/net/ipv6_route"):
    v6 = open("/proc/net/ipv6_route").read().split("\n")
    assert not [r for r in v6 if r.startswith("0" * 32 + " 00")], v6          # no IPv6 default route
for family, address in ((socket.AF_INET, ("1.1.1.1", 53)), (socket.AF_INET6, ("2606:4700:4700::1111", 53))):
    try:
        probe = socket.socket(family, socket.SOCK_DGRAM)
    except OSError as error:
        assert family == socket.AF_INET6 and error.errno == errno.EAFNOSUPPORT, error
        continue
    try:
        probe.connect(address)          # sends no packet
        raise AssertionError(f"route to {address[0]}")
    except OSError as error:
        assert error.errno in (errno.ENETUNREACH, errno.EADDRNOTAVAIL), (address, error)
    finally:
        probe.close()
servers = [l.split()[1] for l in open("/etc/resolv.conf") if l.startswith("nameserver")]
assert servers == ["127.0.0.1"], servers
quiet = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)                      # loopback-only fixture
quiet.settimeout(1)
quiet.sendto(b"\x00\x01\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x07invalid\x00\x00\x01\x00\x01",
             ("127.0.0.1", 53))
try:
    quiet.recv(512)
    raise AssertionError("something answers DNS on the container loopback")
except (ConnectionRefusedError, socket.timeout):
    pass
try:                                    # resolution of a public name must fail inside (no egress:
    socket.getaddrinfo("example.com", 443)   # resolv.conf names only the dead container loopback)
    raise AssertionError("a public name resolves inside the test container")
except socket.gaierror:
    pass
print("ci offline network preflight passed")
'
}

ci_offline_address() {
    # $1: container. Its IPv4 address on the owned internal network, or a hard failure.
    local ip
    ip="$(docker inspect -f "{{(index .NetworkSettings.Networks \"$CI_OFFLINE_NET\").IPAddress}}" "$1")"
    [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
        echo "container $1 has no address on $CI_OFFLINE_NET" >&2; exit 1; }
    printf '%s\n' "$ip"
}

ci_offline_assert_inside() {
    # $1: running container. The same no-egress facts, from inside the running Host's container.
    docker exec "$1" python3 -c '
import errno, socket
for family, address in ((socket.AF_INET, ("1.1.1.1", 53)), (socket.AF_INET6, ("2606:4700:4700::1111", 53))):
    try:
        probe = socket.socket(family, socket.SOCK_DGRAM)
    except OSError as error:
        assert family == socket.AF_INET6 and error.errno == errno.EAFNOSUPPORT, error
        continue
    try:
        probe.connect(address)          # sends no packet
        raise AssertionError(f"route out of the test container to {address[0]}")
    except OSError as error:
        assert error.errno in (errno.ENETUNREACH, errno.EADDRNOTAVAIL), (address, error)
    finally:
        probe.close()
servers = [l.split()[1] for l in open("/etc/resolv.conf") if l.startswith("nameserver")]
assert servers == ["127.0.0.1"], servers
try:
    socket.getaddrinfo("example.com", 443)
    raise AssertionError("a public name resolves inside the test container")
except socket.gaierror:
    pass
'
}

ci_offline_forward_stop() {
    [[ -n "$CI_OFFLINE_FORWARD_PID" ]] && kill "$CI_OFFLINE_FORWARD_PID" 2>/dev/null || true
    CI_OFFLINE_FORWARD_PID=""
}

ci_offline_forward() {
    # $1: container. Sets CI_OFFLINE_URL to http://127.0.0.1:<port> of a loopback forwarder (owned
    # by this run) to the container's address on the internal network; calling it again replaces
    # the forwarder and re-resolves the address. Call it directly, not in $(...): the forwarder's
    # PID must stay in this shell so cleanup can stop it.
    local ip portfile
    ci_offline_forward_stop
    ip="$(ci_offline_address "$1")"
    portfile="$(mktemp)"
    python3 "$(dirname "${BASH_SOURCE[0]}")/ci_loopback_forward.py" "$ip" 7682 "$portfile" \
        >/dev/null 2>"$portfile.err" &
    CI_OFFLINE_FORWARD_PID=$!
    for _ in $(seq 1 50); do [[ -s "$portfile" ]] && break; sleep 0.1; done
    if [[ ! -s "$portfile" ]]; then
        cat "$portfile.err" >&2
        rm -f "$portfile" "$portfile.err"
        echo "loopback forwarder to $ip did not start" >&2
        exit 1
    fi
    CI_OFFLINE_URL="http://127.0.0.1:$(cat "$portfile")"
    rm -f "$portfile" "$portfile.err"
}

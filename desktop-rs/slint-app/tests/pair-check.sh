#!/usr/bin/env bash
# `clarp-slint --pair` against the fake Host, with no window or display:
# slint-app/tests/pair-check.sh [OUT] (check.sh pair runs it). A good code
# is exchanged, the device token kept in the keyring and read back, and the
# Host saved; an expired code, a reply without a token and a URL that is not
# one fail with the reason and save nothing, as does a keyring that cannot be
# written, before the code is spent. The keyring is a throwaway
# gnome-keyring on a private bus on Linux, and on macOS the default keychain
# only with CLARP_TEST_KEYCHAIN=1 (CI makes it throwaway: clarp-test.keychain,
# password "probe", which the check locks once); otherwise the
# token is not kept (CLARP_KEYRING=off). Portable to macOS's bash 3.2.
set -uo pipefail
cd "$(dirname "$0")/../.."
out=${1:-slint-app/docs/checks}; mkdir -p "$out"; out=$(cd "$out" && pwd)
scratch=$(mktemp -d /var/tmp/clarp-pair-check.XXXXXX)
host=""
cleanup() {
    [ -n "$host" ] && { kill "$host" 2>/dev/null; wait "$host" 2>/dev/null; }
    [ "$(uname)" = Darwin ] && [ "${keyring:-off}" = keychain ] && [ -n "${url:-}" ] &&
        security delete-generic-password -s com.maxteabag.Clarp -a "$url" >/dev/null 2>&1
    rm -rf "$scratch"
}
trap cleanup EXIT
mkdir -p "$scratch/home" && mkdir -m 700 "$scratch/run"
python3 tests/fake_host.py --port-file "$scratch/port" --log "$scratch/host.log" 2> "$out/host.stderr.txt" &
host=$!
for _ in $(seq 600); do [ -s "$scratch/port" ] && break; sleep 0.1; done
if [ ! -s "$scratch/port" ]; then
    echo "FAIL the fake Host did not start:"; cat "$out/host.stderr.txt"; echo E2E_FAIL; exit 1
fi
url="http://127.0.0.1:$(cat "$scratch/port")"
binary="$PWD/target/${CLARP_CHECK_PROFILE:-debug}/clarp-slint"

keyring=off
if [ "$(uname)" = Darwin ]; then
    [ "${CLARP_TEST_KEYCHAIN:-}" = 1 ] && keyring=keychain
elif command -v gnome-keyring-daemon >/dev/null && command -v dbus-run-session >/dev/null; then
    keyring=secret-service
fi
echo "keyring: $keyring"

# pair NAME URL CODE: one run with its own settings file; output in NAME.*.
pair() {
    local name=$1; shift
    local environment=(HOME="$scratch/home" XDG_RUNTIME_DIR="$scratch/run" XDG_CONFIG_HOME="$scratch/c"
        XDG_CACHE_HOME="$scratch/k" XDG_DATA_HOME="$scratch/d" XDG_STATE_HOME="$scratch/s"
        CLARP_SETTINGS="$scratch/$name.settings.json")
    case $keyring in
        off) env "${environment[@]}" CLARP_KEYRING=off "$binary" --pair "$@" ;;
        # The real HOME: in a scratch one `security` has no default keychain.
        keychain) env "${environment[@]}" HOME="$HOME" "$binary" --pair "$@" ;;
        secret-service) env -u DBUS_SESSION_BUS_ADDRESS "${environment[@]}" \
            dbus-run-session --config-file="$PWD/tests/private-bus.conf" -- sh -c '
                printf probe-pass | gnome-keyring-daemon --unlock --components=secrets --daemonize >/dev/null
                exec "$@"' sh "$binary" --pair "$@" ;;
        # A keyring that cannot be written: no daemon on the bus, or (macOS)
        # the default keychain locked, as over SSH.
        unwritable) if [ "$(uname)" = Darwin ]; then
                security lock-keychain clarp-test.keychain
                # In a GUI session a locked keychain may ask for its password
                # instead of failing: never wait on that dialog.
                perl -e 'alarm 60; exec @ARGV' env "${environment[@]}" HOME="$HOME" "$binary" --pair "$@"
                local paired=$?
                security unlock-keychain -p probe clarp-test.keychain
                (exit $paired)
            else
                env -u DBUS_SESSION_BUS_ADDRESS "${environment[@]}" \
                    dbus-run-session --config-file="$PWD/tests/private-bus.conf" -- "$binary" --pair "$@"
            fi ;;
    esac > "$scratch/$name.out" 2> "$scratch/$name.err"
    echo $? > "$scratch/$name.status"
    cp "$scratch/$name.out" "$out/$name.stdout.txt"; cp "$scratch/$name.err" "$out/$name.stderr.txt"
}

failed=0
expect() {  # expect DESCRIPTION COMMAND...
    local description=$1; shift
    if "$@"; then echo "ok $description"; else echo "FAIL $description"; failed=1; fi
}
status() { [ "$(cat "$scratch/$1.status")" = "$2" ]; }
said() { grep -qF -- "$3" "$scratch/$1.$2"; }
saved() { grep -qF "\"connection/baseUrl\": \"$url\"" "$scratch/$1.settings.json" 2>/dev/null; }
unsaved() { [ ! -e "$scratch/$1.settings.json" ]; }
exchanged() { grep -F '"/pairing/exchange"' "$scratch/host.log" | grep -qF "\"code\": \"$1\""; }

pair good "$url/" " 123456 "
expect "a good code pairs and exits 0" status good 0
expect "it says it paired with the Host" said good out "Paired with $url:"
expect "the code went to /pairing/exchange, trimmed" exchanged 123456
expect "the Host is saved in the settings" saved good
if [ "$keyring" = off ]; then
    expect "without a keyring it says the token is not kept" said good out "the device token is not kept"
else
    expect "the device token is kept (and read back)" said good out "the device token is in "
fi

if [ "$keyring" != off ]; then
    before=$(grep -cF '"/pairing/exchange"' "$scratch/host.log")
    saved_keyring=$keyring; keyring=unwritable
    pair locked "$url" 123456
    keyring=$saved_keyring
    expect "an unwritable keyring fails before the exchange" status locked 1
    expect "and says the code was not used" said locked err "The code was not used"
    expect "the Host never saw the code" [ "$(grep -cF '"/pairing/exchange"' "$scratch/host.log")" = "$before" ]
    expect "and saves nothing" unsaved locked
fi

pair expired "$url" 999999
expect "an expired code fails" status expired 1
expect "with the Host's reason" said expired err "pairing code expired"
expect "and saves nothing" unsaved expired

pair tokenless "$url" 000000
expect "a reply without a token fails" status tokenless 1
expect "and says so" said tokenless err "did not contain a device credential"
expect "and saves nothing" unsaved tokenless

pair hostless laptopstudio 123456
expect "a URL that is not one fails" status hostless 1
expect "and names it" said hostless err "is not a Clarp server URL"

pair codeless "$url"
expect "a missing code prints the usage" said codeless err "usage: clarp-slint --pair"

cp "$scratch/host.log" "$out/host.log"
if [ "$failed" = 0 ]; then echo E2E_PASS; else echo E2E_FAIL; exit 1; fi

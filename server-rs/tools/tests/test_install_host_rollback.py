"""install-host keeps the last release that ran steady and rolls back to it (issue 853).

Every run here installs into a disposable home of its own. systemctl, journalctl, curl and
coredumpctl are stand-ins on the front of the path, so no live install, unit or data is touched:
each test checks that before it runs install-host.
"""
import hashlib
import os
import pathlib
import shutil
import subprocess
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
INSTALL_HOST = ROOT / "server-rs/tools/install-host"

SYSTEMCTL = """#!/bin/sh
# Stand-in systemctl: records each call, answers `show` from files in $STANDIN.
echo "$*" >> "$STANDIN/systemctl.log"
case "$*" in
    *"show clarp-host.service -p NRestarts --value"*) cat "$STANDIN/restarts" ;;
    *"show clarp-host.service -p ActiveState --value"*) echo active ;;
    *"-p LoadState"*) echo not-found ;;
    *is-active*) echo inactive ;;
    *is-enabled*) echo disabled ;;
esac
exit 0
"""
CURL = """#!/bin/sh
# Stand-in curl: the Host answers, and has opened every agent.
echo '{"opening": 0}'
"""
COREDUMPCTL = """#!/bin/sh
# Stand-in coredumpctl: lists what $STANDIN/cores holds.
echo "$*" >> "$STANDIN/coredumpctl.log"
[ -s "$STANDIN/cores" ] || exit 1
cat "$STANDIN/cores"
"""
CLAUDE = """#!/bin/sh
echo "--thinking-display: expected one of summarized, omitted"
"""


class Machine:
    """A disposable home with stand-ins for systemd, and builds to install from."""

    def __init__(self, base: pathlib.Path):
        self.base = base
        self.home = base / "home"
        self.standin = base / "standin"
        self.share = self.home / ".local/share/clarp-host"
        self.units = self.home / ".config/systemd/user"
        self.releases = self.share / "releases"
        for path in (self.home, self.standin):
            path.mkdir(parents=True)
        for name, text in (("systemctl", SYSTEMCTL), ("journalctl", "#!/bin/sh\nexit 0\n"),
                           ("curl", CURL), ("coredumpctl", COREDUMPCTL), ("claude", CLAUDE)):
            (self.standin / name).write_text(text)
            (self.standin / name).chmod(0o755)
        (self.standin / "restarts").write_text("0\n")
        skills = base / "skills"
        skills.mkdir()
        (skills / "manifest.json").write_text("{}\n")
        self.skills = skills
        self.env = {
            "HOME": str(self.home),
            "XDG_DATA_HOME": str(self.home / ".local/share"),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "PATH": f"{self.standin}:/usr/local/bin:/usr/bin:/bin",
            "STANDIN": str(self.standin),
            "LANG": "C.UTF-8",
        }

    def build(self, letter: str) -> str:
        """A build whose binaries say which one they are; returns its version."""
        commit = letter * 40
        dist = self.base / f"dist-{letter}"
        dist.mkdir()
        sums = []
        for binary in ("clarp-host", "clarp-shim"):
            text = f"#!/bin/sh\necho {binary} {letter}\n".encode()
            (dist / binary).write_bytes(text)
            (dist / binary).chmod(0o755)
            sums.append(f"{hashlib.sha256(text).hexdigest()}  {binary}\n")
        (dist / "SHA256SUMS").write_text("".join(sums))
        (dist / "COMMIT").write_text(commit + "\n")
        return commit[:12]

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        # Nothing runs unless every systemd command it can reach is a stand-in and every path is here.
        for name in ("systemctl", "journalctl", "curl", "coredumpctl"):
            assert shutil.which(name, path=self.env["PATH"]) == str(self.standin / name)
        for key in ("HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME"):
            assert self.env[key].startswith(str(self.base))
        done = subprocess.run(["bash", str(INSTALL_HOST), *args], env=self.env, capture_output=True,
                              text=True, timeout=120)
        if check:
            assert done.returncode == 0, done.stdout + done.stderr
        return done

    def install(self, letter: str) -> subprocess.CompletedProcess:
        return self.run("--from", str(self.base / f"dist-{letter}"), "--skills-from", str(self.skills),
                        "--claude", str(self.standin / "claude"),
                        "--seats-tool", "off", "--whisper", "off", "--speech-keys", "off",
                        "--push-keys", "off", "--push-relay", "off", "--start")

    def backdate(self, minutes: int):
        """The release installed now has been current for this many minutes."""
        current = self.releases / "current"
        lines = current.read_text().splitlines()
        since = int(time.time()) - minutes * 60
        current.write_text("\n".join(f"since={since}" if line.startswith("since=") else line
                                     for line in lines) + "\n")

    def kept(self):
        return sorted(p.name for p in self.releases.iterdir() if p.is_dir())

    def steady(self):
        record = self.releases / "steady"
        if not record.exists():
            return None
        return next(line[8:] for line in record.read_text().splitlines() if line.startswith("version="))

    def running_binary(self) -> str:
        return (self.share / "bin/clarp-host").read_text()


@pytest.fixture
def machine(tmp_path):
    return Machine(tmp_path)


def builds(machine, letters):
    return {letter: machine.build(letter) for letter in letters}


def test_the_steady_release_survives_two_installs_and_the_rollback_starts_it(machine):
    v = builds(machine, "abcd")
    machine.install("a")
    machine.backdate(31)
    said = machine.install("b").stdout
    assert f"{v['a']} ran steady for 31 minutes" in said
    assert machine.steady() == v["a"]

    # Two installs after it, neither of which ran long enough to be judged: a is kept all along.
    machine.install("c")
    assert machine.kept() == sorted([v["a"], v["b"], v["c"]])
    said = machine.install("d").stdout
    assert "not judged yet" in said
    assert machine.kept() == sorted([v["a"], v["c"], v["d"]]), "b is neither current, previous nor steady"
    assert machine.steady() == v["a"]
    assert "echo clarp-host d" in machine.running_binary()

    calls_before = (machine.standin / "systemctl.log").read_text().count("restart clarp-host.service")
    said = machine.run("--rollback").stdout
    assert f"rolled back: left {v['d']}, started {v['a']}" in said
    assert "echo clarp-host a" in machine.running_binary()
    assert "echo clarp-shim a" in (machine.share / "bin/clarp-shim").read_text()
    assert (machine.share / "data/DEPLOYED_VERSION").read_text().strip() == v["a"]
    unit = (machine.units / "clarp-host.service").read_text()
    assert unit == (machine.releases / v["a"] / "clarp-host.service").read_text()
    assert f"releases/{v['a']}/skills" in unit
    log = (machine.standin / "systemctl.log").read_text()
    assert log.count("restart clarp-host.service") == calls_before + 1
    # The release started is the one on record as current now.
    assert f"version={v['a']}" in (machine.releases / "current").read_text()

    # The copy every install leaves rolls back as well: here, a is installed and steady, so it refuses.
    copy = machine.share / "libexec/install-host"
    assert copy.exists()
    done = subprocess.run(["bash", str(copy), "--rollback"], env=machine.env, capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 1
    assert "is the one installed now" in done.stderr


def test_a_rollback_with_no_steady_release_refuses_and_changes_nothing(machine):
    builds(machine, "ab")
    machine.install("a")
    machine.install("b")
    before = machine.running_binary()
    restarts = (machine.standin / "systemctl.log").read_text().count("restart")
    done = machine.run("--rollback", check=False)
    assert done.returncode == 1
    assert "no release is known to have run steady" in done.stderr
    assert "Nothing was changed" in done.stderr
    assert machine.running_binary() == before
    assert (machine.standin / "systemctl.log").read_text().count("restart") == restarts


def test_a_release_that_restarted_is_not_steady(machine):
    v = builds(machine, "ab")
    machine.install("a")
    machine.backdate(45)
    (machine.standin / "restarts").write_text("3\n")
    said = machine.install("b").stdout
    assert f"{v['a']} did not run steady" in said
    assert "restarts 0 at its start and 3 now" in said
    assert machine.steady() is None


def test_a_release_with_a_core_dump_is_not_steady(machine):
    v = builds(machine, "ab")
    machine.install("a")
    machine.backdate(45)
    (machine.standin / "cores").write_text(
        "Wed 2026-11-25 16:55:25 CET 2867829 1000 1000 SIGBUS present /x/clarp-host 262.3M\n")
    said = machine.install("b").stdout
    assert f"{v['a']} did not run steady" in said
    assert "core dumps since: 1" in said
    assert "COREDUMP_USER_UNIT=clarp-host.service" in (machine.standin / "coredumpctl.log").read_text()
    assert machine.steady() is None


def test_judge_says_how_long_is_left(machine):
    v = builds(machine, "a")
    machine.install("a")
    machine.backdate(10)
    assert f"{v['a']} has run 10 of 30 minutes: not judged yet" in machine.run("--judge").stdout
    machine.backdate(30)
    assert f"{v['a']} ran steady for 30 minutes" in machine.run("--judge").stdout
    record = (machine.releases / "steady").read_text()
    assert "restarts_at_start=0" in record and "core_dumps=0" in record
    assert os.stat(machine.releases / "steady").st_mode & 0o077 == 0

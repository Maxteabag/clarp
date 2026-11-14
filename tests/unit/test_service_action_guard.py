"""Who may act on clarp.service and clarp-runtime.service (Clarp issue 322).

The units have fixed names and every process of the user shares one service
manager, so a disposable Host that restarted "clarp.service" restarted the
LIVE Host. service_manager now acts only for a Host that runs inside the unit,
or for the admin path of the installation the definitions name. Every test
here uses the module's runner seam: no service-manager command is real.
"""
from __future__ import annotations

import importlib.util
import pathlib
import plistlib
import subprocess

import pytest

from lib import service_manager

ROOT = pathlib.Path(__file__).resolve().parents[2]
SLICE = "/user.slice/user-{uid}.slice/user@{uid}.service/app.slice/"


class Manager:
    """The runner seam: records every command, answers `show` like systemd."""

    def __init__(self, loaded: dict[str, str] | None = None, *, show_rc: int = 0):
        self.calls: list[list[str]] = []
        self.loaded = loaded if loaded is not None else {}
        self.show_rc = show_rc

    def __call__(self, argv, **_kwargs):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:3] == ["systemctl", "--user", "show"]:
            blocks = []
            for unit in (service_manager.SERVER_UNIT, service_manager.RUNTIME_UNIT):
                path = self.loaded.get(unit, "")
                blocks.append(f"Id={unit}\nLoadState={'loaded' if path else 'not-found'}\n"
                              f"FragmentPath={path}\n")
            return subprocess.CompletedProcess(argv, self.show_rc, "\n".join(blocks), "")
        if argv[:2] == ["launchctl", "print"]:
            unit = next(unit for unit, label in service_manager._LAUNCHD_LABELS
                        if argv[2].endswith("/" + label))
            path = self.loaded.get(unit, "")
            return subprocess.CompletedProcess(
                argv, 0 if path else 113, f"{argv[2]} = {{\n\tpath = {path}\n}}\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    @property
    def acted(self) -> list[list[str]]:
        """Commands that change something: everything but the standing question."""
        return [call for call in self.calls
                if call[:3] != ["systemctl", "--user", "show"]
                and call[:2] != ["launchctl", "print"]]


def _unit_text(share: pathlib.Path, database: pathlib.Path | None = None) -> str:
    return (f'[Service]\nEnvironment="PATH=/usr/bin"\n'
            f'Environment="CLARP_SHARE_DIR={share}"\n'
            f'Environment="CLAUDE_PWA_DB={database or share / "state.sqlite"}"\n'
            "ExecStart=/usr/bin/true\n")


class Installation:
    """An installed Clarp in a temporary home, and this process as its Host."""

    def __init__(self, tmp_path, monkeypatch, *, platform: str = "linux"):
        self.monkeypatch = monkeypatch
        self.home = tmp_path / "home"
        self.share = self.home / ".local/share/clarp"
        self.share.mkdir(parents=True)
        monkeypatch.setenv("CLARP_PLATFORM_OVERRIDE", platform)
        monkeypatch.setenv("HOME", str(self.home))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(self.home / ".config"))
        monkeypatch.setenv("XDG_DATA_HOME", str(self.home / ".local/share"))
        monkeypatch.setenv("CLARP_SHARE_DIR", str(self.share))
        monkeypatch.setenv("CLAUDE_PWA_DB", str(self.share / "state.sqlite"))
        monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
        self.server = service_manager.definition_path()
        self.runtime = service_manager.runtime_definition_path()
        assert self.home in self.server.parents
        self.write_definitions()
        self.cgroup(service_manager.SERVER_UNIT)
        if platform == "macos":
            monkeypatch.setenv("XPC_SERVICE_NAME", service_manager.LAUNCHD_LABEL)

    def write_definitions(self, share: pathlib.Path | None = None,
                          database: pathlib.Path | None = None):
        share = share or self.share
        self.server.parent.mkdir(parents=True, exist_ok=True)
        for path in (self.server, self.runtime):
            if path.suffix == ".plist":
                path.write_bytes(plistlib.dumps({"EnvironmentVariables": {
                    "CLARP_SHARE_DIR": str(share),
                    "CLAUDE_PWA_DB": str(database or share / "state.sqlite")}}))
            else:
                path.write_text(_unit_text(share, database))

    def cgroup(self, unit: str, *, uid: int | None = None, prefix: str | None = None):
        import os
        uid = os.getuid() if uid is None else uid
        path = (SLICE.format(uid=uid) if prefix is None else prefix) + unit
        self.monkeypatch.setattr(service_manager, "_own_cgroup", lambda: path)

    def manager(self) -> Manager:
        """A service manager that loaded both units from this installation."""
        return Manager({service_manager.SERVER_UNIT: str(self.server),
                        service_manager.RUNTIME_UNIT: str(self.runtime)})


@pytest.fixture
def installed(tmp_path, monkeypatch):
    return Installation(tmp_path, monkeypatch)


@pytest.fixture
def refusals(monkeypatch):
    """Every refusal the module logged, as (action, reason)."""
    from lib import log as log_module
    seen = []
    real = log_module.log

    def recording(event, detail=""):
        if event == "serviceActionRefused":
            fields = dict(part.split("=", 1) for part in detail.split()[:2])
            seen.append((fields["action"], fields["reason"]))
        return real(event, detail)
    monkeypatch.setattr(log_module, "log", recording)
    return seen


# Every function of the module that acts on a unit, as a Host would reach it.
ACTIONS = {
    "restart": lambda runner: service_manager.restart(runner=runner),
    "restart-unchecked": lambda runner: service_manager.restart(runner=runner, check=False),
    "restart-admin": lambda runner: service_manager.restart(runner=runner, admin=True),
    "install": lambda runner: service_manager.install_and_restart(runner=runner),
    "restore": lambda runner: service_manager.restore_after_failed_install(
        had_previous=True, runner=runner),
    "restore-first-install": lambda runner: service_manager.restore_after_failed_install(
        had_previous=False, had_runtime_previous=False, runner=runner),
    "stop": lambda runner: service_manager.stop_and_disable(runner=runner),
    "remove": lambda runner: service_manager.remove_definitions(runner=runner),
}
ADMIN_ACTIONS = [name for name in ACTIONS if name not in ("restart", "restart-unchecked")]


def test_every_function_that_runs_a_unit_command_is_listed_here():
    """A new unit action must be added to ACTIONS (and so to the matrix below)."""
    import ast
    tree = ast.parse((ROOT / "server/lib/service_manager.py").read_text())
    acting = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for call in ast.walk(node):
            if (isinstance(call, ast.Call) and getattr(call.func, "id", "") == "_run"
                    and call.args and getattr(call.args[0], "value", "") in ("systemctl", "launchctl")):
                acting.add(node.name)
    # Questions, not actions: they change nothing.
    acting -= {"is_active", "is_runtime_active", "_loaded_definitions"}
    assert acting == {"install_and_restart", "restart", "restore_after_failed_install",
                      "stop_and_disable", "remove_definitions"}


def _foreign_share(installed, tmp_path):
    installed.monkeypatch.setenv("CLARP_SHARE_DIR", str(tmp_path / "qa/share"))


def _foreign_database(installed, tmp_path):
    installed.monkeypatch.setenv("CLAUDE_PWA_DB", str(tmp_path / "qa/state.sqlite"))


def _foreign_config_home(installed, tmp_path):
    installed.monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "qa/xdg-config"))


def _qa_host_environment(installed, tmp_path):
    """What tests/qa/host.py gives its process."""
    root = tmp_path / "qa"
    for key, suffix in {"CLAUDE_PWA_DB": "state.sqlite", "CLARP_SHARE_DIR": "share",
                        "CLARP_CONFIG_DIR": "config", "XDG_CONFIG_HOME": "xdg-config",
                        "XDG_DATA_HOME": "xdg-data"}.items():
        installed.monkeypatch.setenv(key, str(root / suffix))


FOREIGN = {"share": _foreign_share, "database": _foreign_database,
           "config-home": _foreign_config_home, "qa-host": _qa_host_environment}


@pytest.mark.parametrize("action", sorted(ACTIONS))
@pytest.mark.parametrize("foreign", sorted(FOREIGN))
@pytest.mark.parametrize("inside_unit", [True, False])
def test_a_host_with_foreign_directories_never_reaches_the_service_manager(
        installed, tmp_path, refusals, action, foreign, inside_unit):
    """Not even to ask: the directory test needs no command. Inside the live
    unit's cgroup or not (a QA Host started by an agent runs in the runtime's)."""
    FOREIGN[foreign](installed, tmp_path)
    if not inside_unit:
        installed.cgroup(service_manager.RUNTIME_UNIT)
    manager = installed.manager()

    if action == "remove" and foreign in ("config-home", "qa-host"):
        ACTIONS[action](manager)          # nothing of its own to remove: a no-op
    else:
        with pytest.raises(service_manager.ServiceActionRefused) as refused:
            ACTIONS[action](manager)
        assert refused.value.reason in service_manager.REFUSAL_REASONS
        assert refusals and refusals[-1][1] == refused.value.reason      # logged, not silent

    assert manager.calls == []
    assert installed.server.is_file() and installed.runtime.is_file()


@pytest.mark.parametrize("action", ["restart", "restart-unchecked"])
@pytest.mark.parametrize("where", [
    "runtime-unit", "terminal", "unknown", "other-user", "system-unit", "lookalike"])
def test_a_host_outside_the_unit_does_not_restart_it(installed, refusals, action, where):
    """The installation's own directories are not enough: a Host started by
    hand beside the live one restarts nothing."""
    import os
    if where == "runtime-unit":
        installed.cgroup(service_manager.RUNTIME_UNIT)
    elif where == "terminal":
        installed.cgroup("app-kitty-1234.scope")
    elif where == "unknown":
        installed.monkeypatch.setattr(service_manager, "_own_cgroup", lambda: "")
    elif where == "other-user":
        installed.cgroup(service_manager.SERVER_UNIT, uid=os.getuid() + 1)
    elif where == "system-unit":
        installed.cgroup(service_manager.SERVER_UNIT, prefix="/system.slice/")
    else:
        installed.cgroup("not-clarp.service")
    manager = installed.manager()

    with pytest.raises(service_manager.ServiceActionRefused) as refused:
        ACTIONS[action](manager)

    assert refused.value.reason == "not_in_unit"
    assert refusals == [("restart", "not_in_unit")]
    assert manager.calls == []


def test_the_installed_host_restarts_its_own_unit(installed, refusals):
    manager = installed.manager()

    service_manager.restart(runner=manager, check=False)

    assert manager.calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "restart", "clarp.service"],
    ]
    assert refusals == []
    assert service_manager.host_refusal("restart") is None


def test_a_host_in_a_sub_cgroup_of_its_unit_is_inside_it(installed):
    installed.cgroup(service_manager.SERVER_UNIT + "/worker")
    assert service_manager.runs_in_unit(service_manager.SERVER_UNIT)
    assert not service_manager.runs_in_unit(service_manager.RUNTIME_UNIT)


def test_a_host_whose_unit_has_no_definition_is_refused(installed, refusals):
    installed.server.unlink()
    manager = installed.manager()
    with pytest.raises(service_manager.ServiceActionRefused) as refused:
        service_manager.restart(runner=manager)
    assert refused.value.reason == "no_definition"
    assert manager.calls == []


@pytest.mark.parametrize("admin", [False, True])
def test_a_definition_that_names_no_share_directory_proves_nothing(installed, admin):
    """A hand-written or very old unit: nothing in it says whose it is. The
    admin has no database in its environment here, so only the share decides."""
    for path in (installed.server, installed.runtime):
        path.write_text("[Service]\nExecStart=/usr/bin/true\n")
    if admin:
        installed.monkeypatch.delenv("CLAUDE_PWA_DB")
    manager = installed.manager()
    with pytest.raises(service_manager.ServiceActionRefused) as refused:
        service_manager.restart(runner=manager, admin=admin)
    assert refused.value.reason == "foreign_directories"
    assert "share directory (none)" in refused.value.detail
    assert manager.calls == []


def test_a_host_database_defaults_like_lib_db(installed, tmp_path):
    """Without CLAUDE_PWA_DB a Host opens <data dir>/state.sqlite (lib.db), not
    <share>/state.sqlite: a share given alone does not make the database ours."""
    elsewhere = tmp_path / "elsewhere"
    installed.write_definitions(share=elsewhere)
    installed.monkeypatch.setenv("CLARP_SHARE_DIR", str(elsewhere))
    installed.monkeypatch.delenv("CLAUDE_PWA_DB")
    manager = installed.manager()
    with pytest.raises(service_manager.ServiceActionRefused) as refused:
        service_manager.restart(runner=manager)
    assert refused.value.reason == "foreign_directories"
    assert "database" in refused.value.detail
    assert manager.calls == []


def test_directories_are_compared_as_real_paths(installed, tmp_path):
    link = tmp_path / "share-link"
    link.symlink_to(installed.share, target_is_directory=True)
    installed.monkeypatch.setenv("CLARP_SHARE_DIR", str(link))
    installed.monkeypatch.setenv("CLAUDE_PWA_DB", str(link / "state.sqlite"))
    assert service_manager.host_refusal("restart") is None


# ---- the admin path: install.sh and clarp-admin ----------------------------

@pytest.mark.parametrize("where", ["runtime-unit", "update-unit", "terminal"])
def test_the_admin_path_acts_from_any_cgroup(installed, refusals, where):
    """clarp-admin run by an agent sits in the runtime's cgroup, the Host's own
    update in clarp-update.service, the owner's in a terminal scope."""
    installed.cgroup({"runtime-unit": service_manager.RUNTIME_UNIT,
                      "update-unit": "clarp-update.service",
                      "terminal": "app-kitty-1234.scope"}[where])
    manager = installed.manager()

    service_manager.restart(runner=manager, admin=True)

    assert manager.calls[0][:3] == ["systemctl", "--user", "show"]
    assert manager.acted == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "restart", "clarp.service"],
    ]
    assert refusals == []


def test_the_installer_installs_and_starts_both_units(installed):
    manager = installed.manager()
    service_manager.install_and_restart(runner=manager)
    assert manager.acted == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "is-active", "--quiet", "clarp-runtime.service"],
        ["systemctl", "--user", "enable", "--now", "clarp-runtime.service"],
        ["systemctl", "--user", "enable", "clarp.service"],
        ["systemctl", "--user", "restart", "clarp.service"],
    ]


def test_a_first_installation_meets_a_manager_that_knows_no_unit(installed):
    manager = Manager({})                       # LoadState=not-found for both
    service_manager.install_and_restart(runner=manager)
    assert ["systemctl", "--user", "restart", "clarp.service"] in manager.acted


def test_rollback_restores_the_previous_services(installed):
    manager = installed.manager()
    service_manager.restore_after_failed_install(had_previous=True, runner=manager)
    assert ["systemctl", "--user", "restart", "clarp.service"] in manager.acted


def test_a_failed_first_installation_cleans_up_while_its_definitions_exist(installed, refusals):
    manager = installed.manager()
    service_manager.restore_after_failed_install(
        had_previous=False, had_runtime_previous=False, runner=manager)
    assert manager.acted[1:] == [
        ["systemctl", "--user", "disable", "--now", "clarp.service"],
        ["systemctl", "--user", "disable", "--now", "clarp-runtime.service"],
    ]
    # With the definitions already deleted nothing says whose services they are.
    installed.server.unlink()
    installed.runtime.unlink()
    again = installed.manager()
    with pytest.raises(service_manager.ServiceActionRefused) as refused:
        service_manager.restore_after_failed_install(had_previous=False, runner=again)
    assert refused.value.reason == "no_definition"
    assert again.calls == []
    assert refusals == [("restore", "no_definition")]


def test_uninstall_stops_the_services_then_removes_their_definitions(installed):
    manager = installed.manager()
    service_manager.stop_and_disable(runner=manager)
    service_manager.remove_definitions(runner=manager)
    assert manager.acted == [
        ["systemctl", "--user", "disable", "--now", "clarp.service"],
        ["systemctl", "--user", "disable", "--now", "clarp-runtime.service"],
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "daemon-reload"],
    ]
    assert not installed.server.exists() and not installed.runtime.exists()
    # A second uninstall finds nothing of its own and runs nothing.
    again = installed.manager()
    service_manager.remove_definitions(runner=again)
    assert again.calls == []


@pytest.mark.parametrize("action", ADMIN_ACTIONS)
def test_a_sandbox_installation_does_not_touch_the_live_units(
        installed, tmp_path, refusals, action):
    """A test or scratch installation writes its own definitions under its own
    config home, and they name its own share. The service manager's units are
    the live Host's: it is asked which file they come from, and nothing else."""
    sandbox = tmp_path / "sandbox"
    live = installed.manager()                      # units loaded from the live files
    installed.monkeypatch.setenv("XDG_CONFIG_HOME", str(sandbox / ".config"))
    installed.monkeypatch.setenv("CLARP_SHARE_DIR", str(sandbox / "share"))
    installed.monkeypatch.setenv("CLAUDE_PWA_DB", str(sandbox / "share/state.sqlite"))
    for path in (service_manager.definition_path(), service_manager.runtime_definition_path()):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_unit_text(sandbox / "share"))

    with pytest.raises(service_manager.ServiceActionRefused) as refused:
        ACTIONS[action](live)

    assert refused.value.reason == "foreign_definition"
    assert live.acted == []
    assert len(live.calls) == 1 and live.calls[0][:3] == ["systemctl", "--user", "show"]
    assert service_manager.definition_path().is_file()       # `remove` deleted nothing
    assert refusals[-1][1] == "foreign_definition"


@pytest.mark.parametrize("answer", ["fails", "empty", "missing-binary", "transient", "masked"])
def test_the_admin_path_needs_the_managers_answer(installed, answer):
    manager = installed.manager()
    reason = "manager_unavailable"
    if answer == "fails":
        manager.show_rc = 1
    elif answer == "empty":
        manager = Manager()
        manager.__class__ = type("Silent", (Manager,), {
            "__call__": lambda self, argv, **_k: (
                self.calls.append(list(argv)),
                subprocess.CompletedProcess(argv, 0, "", ""))[1]})
    elif answer == "missing-binary":
        class Missing(Manager):
            def __call__(self, argv, **_kwargs):
                self.calls.append(list(argv))
                raise FileNotFoundError("systemctl")
        manager = Missing()
    else:
        class Odd(Manager):
            def __call__(self, argv, **_kwargs):
                self.calls.append(list(argv))
                fragment = "" if answer == "transient" else "/dev/null"
                state = "loaded" if answer == "transient" else "masked"
                text = "\n".join(f"Id={unit}\nLoadState={state}\nFragmentPath={fragment}\n"
                                 for unit in (service_manager.SERVER_UNIT,
                                              service_manager.RUNTIME_UNIT))
                return subprocess.CompletedProcess(argv, 0, text, "")
        manager, reason = Odd(), "foreign_definition"

    with pytest.raises(service_manager.ServiceActionRefused) as refused:
        service_manager.restart(runner=manager, admin=True)

    assert refused.value.reason == reason
    assert manager.acted == []


def test_an_admin_command_given_a_foreign_database_is_refused(installed, tmp_path):
    installed.monkeypatch.setenv("CLAUDE_PWA_DB", str(tmp_path / "scratch.sqlite"))
    manager = installed.manager()
    with pytest.raises(service_manager.ServiceActionRefused) as refused:
        service_manager.restart(runner=manager, admin=True)
    assert refused.value.reason == "foreign_directories"
    assert manager.calls == []


def test_an_admin_command_without_a_database_in_its_environment_still_acts(installed):
    """install.sh run from a shell has no CLAUDE_PWA_DB; it opens no database."""
    installed.monkeypatch.delenv("CLAUDE_PWA_DB")
    manager = installed.manager()
    service_manager.restart(runner=manager, admin=True)
    assert ["systemctl", "--user", "restart", "clarp.service"] in manager.acted


def test_a_runtime_definition_of_another_installation_refuses_the_admin(installed, tmp_path):
    installed.runtime.write_text(_unit_text(tmp_path / "other/share"))
    manager = installed.manager()
    with pytest.raises(service_manager.ServiceActionRefused) as refused:
        service_manager.install_and_restart(runner=manager)
    assert refused.value.reason == "foreign_directories"
    assert manager.calls == []


def test_the_rendered_definitions_satisfy_the_guard(tmp_path, monkeypatch):
    """The real templates, rendered by write_definition as the installer does."""
    installed = Installation(tmp_path, monkeypatch)
    installed.server.unlink()
    installed.runtime.unlink()
    release = installed.share / "current"
    (release / "systemd").mkdir(parents=True)
    for name in ("clarp.service", "clarp-runtime.service"):
        (release / "systemd" / name).write_text((ROOT / "systemd" / name).read_text())
    (release / "runtime.py").touch()
    monkeypatch.setenv("CLARP_CONFIG_DIR", str(installed.home / ".config/clarp"))
    monkeypatch.setenv("CLARP_CACHE_DIR", str(installed.home / ".cache/clarp"))

    service_manager.write_definition(
        python=pathlib.Path("/usr/bin/python3"), share=installed.share,
        service_path="/usr/bin", home=installed.home, inherited_environment={})

    environment = service_manager._definition_environment(installed.server)
    assert environment["CLARP_SHARE_DIR"] == str(installed.share)
    assert environment["CLAUDE_PWA_DB"] == str(installed.share / "state.sqlite")
    assert service_manager.host_refusal("restart") is None
    manager = installed.manager()
    service_manager.install_and_restart(runner=manager)
    assert ["systemctl", "--user", "restart", "clarp.service"] in manager.acted


# ---- the kernel's word for where this process runs -------------------------

@pytest.mark.parametrize("text, expected", [
    ("0::/user.slice/user-1000.slice/user@1000.service/app.slice/clarp.service\n",
     "/user.slice/user-1000.slice/user@1000.service/app.slice/clarp.service"),
    ("12:pids:/user.slice\n1:name=systemd:/user.slice/user-1000.slice/user@1000.service/clarp.service\n",
     "/user.slice/user-1000.slice/user@1000.service/clarp.service"),
    ("12:pids:/user.slice/clarp.service\n", ""),
    ("", ""),
])
def test_own_cgroup_reads_the_systemd_hierarchy(monkeypatch, tmp_path, text, expected):
    source = tmp_path / "cgroup"
    source.write_text(text)
    real = pathlib.Path

    def path(value, *rest):
        return real(source) if value == "/proc/self/cgroup" else real(value, *rest)
    monkeypatch.setattr(service_manager, "Path", path)
    assert service_manager._own_cgroup() == expected


def test_an_unreadable_cgroup_is_unknown(monkeypatch, tmp_path):
    real = pathlib.Path
    monkeypatch.setattr(service_manager, "Path", lambda value, *rest: (
        real(tmp_path / "absent") if value == "/proc/self/cgroup" else real(value, *rest)))
    assert service_manager._own_cgroup() == ""


def test_the_environment_alone_does_not_put_a_process_in_the_unit(installed, monkeypatch):
    """INVOCATION_ID is inherited by every child and can be copied; on Linux the
    cgroup decides."""
    installed.cgroup("app-kitty-1234.scope")
    monkeypatch.setenv("INVOCATION_ID", "f4123ba2eba14f9e9052e10b8870b8b2")
    monkeypatch.setenv("XPC_SERVICE_NAME", service_manager.LAUNCHD_LABEL)
    assert service_manager.host_refusal("restart").reason == "not_in_unit"


# ---- macOS ------------------------------------------------------------------

@pytest.fixture
def installed_mac(tmp_path, monkeypatch):
    return Installation(tmp_path, monkeypatch, platform="macos")


def test_macos_host_restarts_only_the_job_launchd_started_it_as(installed_mac, refusals):
    manager = installed_mac.manager()
    service_manager.restart(runner=manager, check=False)
    assert manager.calls == [["launchctl", "kickstart", "-k",
                              f"{service_manager.launchd_domain()}/com.maxteabag.clarp.server"]]

    for name in ("0", service_manager.RUNTIME_LAUNCHD_LABEL, ""):
        installed_mac.monkeypatch.setenv("XPC_SERVICE_NAME", name)
        outside = installed_mac.manager()
        with pytest.raises(service_manager.ServiceActionRefused) as refused:
            service_manager.restart(runner=outside)
        assert refused.value.reason == "not_in_unit" and outside.calls == []


def test_macos_foreign_host_and_sandbox_admin_are_refused(installed_mac, tmp_path):
    live = installed_mac.manager()
    installed_mac.monkeypatch.setenv("CLARP_SHARE_DIR", str(tmp_path / "qa/share"))
    for action in sorted(ACTIONS):
        with pytest.raises(service_manager.ServiceActionRefused) as refused:
            ACTIONS[action](live)
        assert refused.value.reason == "foreign_directories"
    assert live.calls == []

    # A sandbox home with its own plists, against launchd's live jobs.
    sandbox = tmp_path / "sandbox-home"
    installed_mac.monkeypatch.setenv("HOME", str(sandbox))
    installed_mac.monkeypatch.setenv("CLARP_SHARE_DIR", str(sandbox / "share"))
    installed_mac.monkeypatch.setenv("CLAUDE_PWA_DB", str(sandbox / "share/state.sqlite"))
    for path in (service_manager.definition_path(), service_manager.runtime_definition_path()):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(plistlib.dumps({"EnvironmentVariables": {
            "CLARP_SHARE_DIR": str(sandbox / "share")}}))
    with pytest.raises(service_manager.ServiceActionRefused) as refused:
        service_manager.install_and_restart(runner=live)
    assert refused.value.reason == "foreign_definition"
    assert live.acted == []


def test_macos_admin_path_acts_for_its_own_jobs_and_for_a_first_installation(installed_mac):
    installed_mac.monkeypatch.setenv("XPC_SERVICE_NAME", "0")       # a terminal
    manager = installed_mac.manager()
    service_manager.restart(runner=manager, admin=True)
    assert manager.acted == [["launchctl", "kickstart", "-k",
                              f"{service_manager.launchd_domain()}/com.maxteabag.clarp.server"]]
    first = Manager({})                                              # nothing loaded
    service_manager.stop_and_disable(runner=first)
    assert [call[:2] for call in first.acted] == [["launchctl", "bootout"]] * 2


# ---- tests/qa/host.py -------------------------------------------------------

def _qa_host_module():
    spec = importlib.util.spec_from_file_location("qa_host_under_test", ROOT / "tests/qa/host.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_qa_host_refuses_every_service_action_outright(installed, monkeypatch, refusals):
    """Even with every fact of the installed Host in its favour."""
    for name in ("_run", "host_refusal"):
        monkeypatch.setattr(service_manager, name, getattr(service_manager, name))
    manager = installed.manager()
    assert service_manager.host_refusal("restart") is None        # the Host it imitates may

    _qa_host_module().refuse_service_actions(service_manager)

    for action in sorted(ACTIONS):
        with pytest.raises(service_manager.ServiceActionRefused) as refused:
            ACTIONS[action](manager)
        assert refused.value.reason == "disposable_host"
    for question in (service_manager.is_active, service_manager.is_runtime_active):
        with pytest.raises(service_manager.ServiceActionRefused):
            question(runner=manager)
    with pytest.raises(service_manager.ServiceActionRefused):
        service_manager.launch_detached(["/bin/true"], unit="clarp-qa", runner=manager)
    assert service_manager.host_refusal("update").reason == "disposable_host"
    assert manager.calls == []
    assert len(refusals) == len(ACTIONS) + 4


def test_the_qa_host_installs_its_refusal_before_it_builds_the_server():
    source = (ROOT / "tests/qa/host.py").read_text()
    assert source.index("refuse_service_actions(service_manager)\n") < source.index(
        "spec.loader.exec_module(module)")


# ---- the Host's own update --------------------------------------------------

def test_a_host_outside_the_unit_does_not_launch_the_update(installed, monkeypatch, refusals):
    from lib import server_update
    installed.cgroup(service_manager.RUNTIME_UNIT)
    monkeypatch.setattr(server_update, "get_update_status", lambda: {
        "update_method": "managed", "update_command": "clarp-admin update",
        "update_available": True, "latest_version": "next"})
    commands = []
    monkeypatch.setattr(server_update.subprocess, "run",
                        lambda command, **_k: commands.append(command))
    monkeypatch.setattr(server_update.subprocess, "Popen",
                        lambda command, **_k: commands.append(command))

    code, result = server_update.request_update("mike")

    assert code == 409 and result["ok"] is False
    assert "not the installed Clarp service" in result["message"]
    assert commands == []
    assert refusals == [("update", "not_in_unit")]

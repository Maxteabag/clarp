"""Structure of lib.routes.registry (refactor slice S3).

Handler.do_<METHOD> runs its gate prelude and then registry.dispatch, so the
dispatch golden (test_route_goldens.py) is the parity check for every method,
path and principal. These tests keep the registry itself honest: no dead
route, unique names, server.py's dispatch only through the registry.
"""
from __future__ import annotations

import json

import pytest

from tests.integration import route_probe
from tests.integration.test_route_goldens import DISPATCH_GOLDEN, server_module  # noqa: F401 - fixture
from tests.integration.test_server_di import (  # noqa: F401 - fixtures
    _model_catalog_never_probes_installed_clis, fake_ctx)


def test_every_route_is_the_first_match_for_some_golden_key():
    from lib.routes import registry
    golden = json.loads(DISPATCH_GOLDEN.read_text())["routes"]
    used = set()
    for key in golden:
        method, path = key.split(" ", 1)
        bare = path.split("?", 1)[0]
        for route in registry.ROUTES[method]:
            if route.match(bare):
                used.add((method, route.name))
                if route.name not in ("exact", "root-static", "decision", "clip-hls"):
                    break
    declared = {(method, route.name) for method, routes in registry.ROUTES.items()
                for route in routes}
    assert declared - used == set()


@pytest.mark.parametrize("method", route_probe.METHODS)
def test_route_names_are_unique_per_method(method):
    from lib.routes import registry
    names = [route.name for route in registry.ROUTES[method]]
    assert len(names) == len(set(names))


@pytest.mark.parametrize("method", route_probe.METHODS)
def test_handler_dispatches_only_through_the_registry(server_module, method):
    """After the gate prelude each do_<METHOD> hands the bare path to the
    registry; no route is matched in server.py itself."""
    import inspect
    source = inspect.getsource(getattr(server_module.Handler, "do_" + method))
    assert source.rstrip().endswith(f'return route_registry.dispatch(self, "{method}", path)')
    assert "startswith" not in source and "_dispatch_exact" not in source


SERVER_PY = route_probe.SERVER_DIR / "server.py"
ROUTES_DIR = route_probe.SERVER_DIR / "lib" / "routes"


@pytest.mark.parametrize("alias", ["claude_pwa_server", "clarp_route_probe_server", "server"])
def test_boot_config_is_the_modules_own_import_time_config(alias, fake_ctx, monkeypatch):
    """Composition-root state reaches handler code through the server object:
    every ContextHTTPServer (build_server's, a directly built one, the LAN
    listener's subclass) carries exactly its module's _CFG, never a reload."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(alias, SERVER_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # config.load caches, so a reload would return the same object; make any
    # load after import a fresh one, so only the import-time object passes.
    import copy
    from lib import config
    real_load = config.load

    def fresh_load(*args, **kwargs):
        return copy.copy(real_load(*args, **kwargs))
    monkeypatch.setattr(config, "load", fresh_load)
    monkeypatch.setattr(module, "load_config", fresh_load)
    direct = module.ContextHTTPServer(("127.0.0.1", 0), module.Handler, fake_ctx)
    subclass = type("LanServer", (type(direct),), {})(("127.0.0.1", 0), module.Handler, fake_ctx)
    built = module.build_server(fake_ctx, 0, bind_addr="127.0.0.1")
    try:
        for server in (direct, subclass, built):
            assert server.boot_config is module._CFG
    finally:
        for server in (direct, subclass, built):
            server.server_close()


def _imports(path):
    import ast
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            yield base
            yield from (f"{base}.{alias.name}" if base else alias.name for alias in node.names)


def test_lib_routes_never_imports_the_composition_root():
    for path in sorted(ROUTES_DIR.glob("*.py")):
        names = set(_imports(path))
        assert not names & {"server", "__main__"}, path.name
        assert not any(name.startswith(("server.", "__main__.")) for name in names), path.name


def test_only_server_py_imports_lib_routes():
    lib = route_probe.SERVER_DIR / "lib"
    offenders = []
    for path in sorted(lib.rglob("*.py")):
        if ROUTES_DIR in path.parents:
            continue
        names = set(_imports(path))
        if any(name == "routes" or name.startswith(("lib.routes", ".routes", "routes."))
               for name in names):
            offenders.append(str(path.relative_to(lib)))
    assert offenders == []

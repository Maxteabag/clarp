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

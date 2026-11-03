"""Parity of lib.routes.registry with server.py's dispatch (refactor slice S3).

Until the seam commit switches Handler.do_<METHOD> to registry.dispatch, this
test runs a probe whose do_<METHOD> keep today's gate prelude (copied from
server.py) and then call registry.dispatch, and requires the same answer as
the dispatch golden for every method, path and principal. After the seam
commit the golden test itself is the parity check and the copied preludes go.
"""
from __future__ import annotations

import json

import pytest

from tests.integration import route_probe
from tests.integration.test_route_goldens import (
    DISPATCH_GOLDEN, PRINCIPAL_ORDER, _device_tokens, server_module)  # noqa: F401 - fixture


def _registry_probe(probe_cls):
    from lib.routes import registry

    class RegistryProbe(probe_cls):
        def do_GET(self):
            if not self._authorized():
                return self._reject_unauthorized()
            path = self.path.split("?", 1)[0]
            if self._oracle_auth_missing(path):
                return self._json_error(401, "Oracle requires authenticated full-device access")
            if self._device_forbidden(path, "GET"):
                return self._json_error(403, "full device access required")
            return registry.dispatch(self, "GET", path)

        def do_POST(self):
            if not self._authorized():
                return self._reject_unauthorized()
            path = self.path.split("?", 1)[0]
            if self._oracle_auth_missing(path):
                return self._json_error(401, "Oracle requires authenticated full-device access")
            if self._device_forbidden(path, "POST"):
                return self._json_error(403, "full device access required")
            return registry.dispatch(self, "POST", path)

        def do_PUT(self):
            if not self._authorized():
                return self._reject_unauthorized()
            path = self.path.split("?", 1)[0]
            if self._device_forbidden(path, "PUT"):
                return self._json_error(403, "full device access required")
            return registry.dispatch(self, "PUT", path)

        def do_DELETE(self):
            if not self._authorized():
                return self._reject_unauthorized()
            path = self.path.split("?", 1)[0]
            if self._device_forbidden(path, "DELETE"):
                return self._json_error(403, "full device access required")
            return registry.dispatch(self, "DELETE", path)

        def do_PATCH(self):
            if not self._authorized():
                return self._reject_unauthorized()
            path = self.path.split("?", 1)[0]
            return registry.dispatch(self, "PATCH", path)

    return RegistryProbe


def test_registry_reproduces_the_dispatch_golden(server_module, monkeypatch, tmp_path):
    (tmp_path / "static").mkdir()
    (tmp_path / "audio").mkdir()
    route_probe.patch_lib_targets(monkeypatch)
    probe = _registry_probe(route_probe.make_probe_class(server_module.Handler))
    principals = route_probe.principals(*_device_tokens())
    golden = json.loads(DISPATCH_GOLDEN.read_text())["routes"]
    differences = []
    for key, entry in sorted(golden.items()):
        method, path = key.split(" ", 1)
        for name in PRINCIPAL_ORDER:
            expected = entry["others"].get(name, entry["target"])
            actual = route_probe.resolve(probe, tmp_path, method, path, *principals[name])
            if actual != expected:
                differences.append((key, name, expected, actual))
    assert not differences, f"{len(differences)} differences, first: {differences[:5]}"


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

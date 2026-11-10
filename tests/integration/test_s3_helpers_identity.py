"""Every name the S3 [helpers] step moved is one object: server.<name> is
lib.routes.helpers.<name> (route-registry.md section 11, lead condition 2).
Skipped where the helpers step is not applied (main)."""
from __future__ import annotations

import pathlib
import tomllib

import pytest

from tests.integration.test_server_di import server_module  # noqa: F401 - the loaded server.py

_RECIPE = pathlib.Path(__file__).resolve().parents[2] / "tools" / "refactor" / "s3_moves.toml"
NAMES = tomllib.loads(_RECIPE.read_text()).get("helpers", {}).get("names", [])


@pytest.mark.skipif(not NAMES, reason="the recipe moves no helpers")
@pytest.mark.parametrize("name", NAMES)
def test_moved_helper_is_the_same_object_on_both_modules(name):
    helpers = pytest.importorskip("lib.routes.helpers", reason="helpers step not applied here")
    assert getattr(server_module, name) is getattr(helpers, name)

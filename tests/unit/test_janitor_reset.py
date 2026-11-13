import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from lib import janitor_reset


def test_reset_adapter_is_disabled_without_explicit_command():
    with patch.object(janitor_reset.config, "load", return_value=SimpleNamespace()), patch.object(janitor_reset.subprocess, "run") as run:
        assert janitor_reset.attempt("claude_reset_command", ["Fable"]) == {"status": "disabled", "verified": False}
        run.assert_not_called()


@pytest.mark.parametrize("code,receipt,want", [
    (0, {"status":"redeemed","account":"ai2","verified":True}, True),
    (0, {"status":"reconciled","account":"ai15","verified":True}, True),
    (1, {"status":"redeemed","account":"ai2","verified":True}, False),
    (0, {"status":"redeemed","account":"ai2","verified":False}, False),
    (0, {"status":"redeemed","account":"ecit","verified":True}, False),
    (0, {"available":True}, False),
])
def test_reset_adapter_requires_structured_verified_receipt(code, receipt, want):
    with patch.object(janitor_reset.config,"load",return_value=SimpleNamespace(claude_reset_command=("broker",))), patch.object(janitor_reset.subprocess,"run",return_value=SimpleNamespace(returncode=code,stdout=json.dumps(receipt))) as run:
        result=janitor_reset.attempt("claude_reset_command",["claude-fable-5-2"])
        assert result["verified"] is want
        assert run.call_args.kwargs["input"]==json.dumps({"models":["claude-fable-5-2"]})
        assert "shell" not in run.call_args.kwargs

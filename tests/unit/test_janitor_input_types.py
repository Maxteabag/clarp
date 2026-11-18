"""A janitor's input of the wrong type is refused with the 400 the handler uses (R29)."""
from __future__ import annotations

import pytest

from lib import janitors


@pytest.mark.parametrize("field,value", [("cron", 5), ("cron", None), ("cron", ["0 9 * * *"]),
                                         ("timezone", 3), ("timezone", ["UTC"])])
def test_a_schedule_that_is_not_text_is_refused_as_a_bad_value(field, value):
    config = {"cron": "0 9 * * *", "timezone": "UTC", field: value}
    with pytest.raises(janitors.JanitorError) as refused:
        janitors._attachment_values([{"trigger_id": "schedule", "config": config}], "agent-1", "scheduled-prompt")
    assert (str(refused.value), refused.value.status) == ("Use a valid five-field schedule and IANA timezone", 400)


@pytest.mark.parametrize("provider", [5, None, ["agy"], {"id": "agy"}])
@pytest.mark.parametrize("isolated,message", [(False, "Demand jobs require an ephemeral executor"),
                                              (True, "Isolated execution is an Antigravity option")])
def test_an_execution_provider_that_is_not_text_is_refused_as_a_bad_value(provider, isolated, message):
    value = {"executor": "ephemeral", "provider": provider, "isolated": isolated}
    with pytest.raises(janitors.JanitorError) as refused:
        janitors._execution("goal-keeper", value, "agy")
    assert (str(refused.value), refused.value.status) == (message, 400)


def test_a_text_schedule_and_provider_still_pass():
    [out] = janitors._attachment_values(
        [{"trigger_id": "schedule", "config": {"cron": "0 9 * * *", "timezone": "Europe/Oslo"}}],
        "agent-1", "scheduled-prompt")
    assert out["config"]["cron"] == "0 9 * * *"
    assert janitors._execution("goal-keeper", {"provider": "agy", "isolated": True}, "agy")["provider"] == "agy"

import uuid
import pytest
from lib import application_activity as activity, db


def test_activity_lease_and_input_idle_are_independent_of_phone_alerts(monkeypatch):
    now = [100_000]
    monkeypatch.setattr(db, "now_ms", lambda: now[0])
    instance = str(uuid.uuid4())
    assert not activity.active(300)
    activity.report("administrator", instance, 1, True, 0, now[0])
    assert activity.active(300)
    now[0] += 40_000
    activity.report("administrator", instance, 2, True, 301_000, now[0])
    assert not activity.active(300)
    assert activity.active(600)
    activity.report("administrator", instance, 3, False, 0, now[0])
    assert not activity.active(600)
    assert not activity.report("administrator", instance, 2, True, 0, now[0])["accepted"]
    assert not activity.active(600)
    activity.report("administrator", instance, 4, True, 0, now[0])
    now[0] += 45_000
    assert not activity.active(600)


@pytest.mark.parametrize("field,value", [("sequence", True), ("foreground", "true"), ("input_age_ms", -1), ("sent_at_ms", 0)])
def test_invalid_or_stale_reports_do_not_activate(field, value):
    args = dict(principal="administrator", instance_id=str(uuid.uuid4()), sequence=1,
                foreground=True, input_age_ms=0, sent_at_ms=db.now_ms())
    args[field] = value
    with pytest.raises(ValueError): activity.report(**args)
    assert not activity.active(300)

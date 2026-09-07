import uuid
import pytest
from lib import db, desktop_presence


def put(instance, seq, active=True, principal='administrator'):
    return desktop_presence.update(principal=principal, instance_id=instance, sequence=seq, active=active, sent_at_ms=db.now_ms())


def test_lease_expiry_and_out_of_order_release(monkeypatch):
    now = [100000]
    monkeypatch.setattr(db, 'now_ms', lambda: now[0])
    instance = str(uuid.uuid4())
    assert not desktop_presence.active()
    put(instance, 1)
    assert desktop_presence.active()
    put(instance, 3, False)
    assert not put(instance, 2)['accepted']
    assert not desktop_presence.active()
    put(instance, 4)
    now[0] += desktop_presence.LEASE_MS
    assert not desktop_presence.active()
    put(instance, 5)
    assert desktop_presence.active()
    now[0] -= 1  # Backward clock movement must not retain a stale lease.
    assert not desktop_presence.active()


def test_multiple_desktops_do_not_release_each_other():
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    put(first, 1); put(second, 1)
    put(first, 2, False)
    assert desktop_presence.active()
    put(second, 2, False)
    assert not desktop_presence.active()


def test_principals_are_isolated_and_revoked_devices_cannot_suppress():
    instance = str(uuid.uuid4())
    put(instance, 1)
    put(instance, 100, False, principal='different-device')
    assert desktop_presence.active()
    put(instance, 2, False)
    put(instance, 101, principal='different-device')
    assert not desktop_presence.active()


@pytest.mark.parametrize('sequence,active', [(True, True), (0, True), (2**54, True), (1, 'true')])
def test_invalid_updates_do_not_create_presence(sequence, active):
    with pytest.raises(ValueError):
        put(str(uuid.uuid4()), sequence, active)
    assert not desktop_presence.active()


def test_lease_is_shared_through_sqlite_and_revocation_takes_effect():
    import importlib
    from lib import device_pairing
    paired = device_pairing.exchange(device_pairing.issue(scope='full')['code'])
    put(str(uuid.uuid4()), 1, principal=paired['device_id'])
    assert importlib.reload(desktop_presence).active()
    db.conn().execute('UPDATE paired_devices SET revoked_at=? WHERE device_id=?',
                      (db.now_ms(), paired['device_id']))
    assert not desktop_presence.active()


def test_delayed_active_report_cannot_resurrect_expired_activity(monkeypatch):
    now = db.now_ms()
    with pytest.raises(ValueError):
        desktop_presence.update(principal='administrator', instance_id=str(uuid.uuid4()),
            sequence=1, active=True, sent_at_ms=now - desktop_presence.LEASE_MS - 1000)
    assert not desktop_presence.active()

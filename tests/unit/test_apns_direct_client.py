"""AUT-19: the direct-to-APNs path shares one HTTP/2 client per process.
One batch's failed send used to close it under every concurrent batch, and
each of those, failing in turn, closed the replacement too."""
from __future__ import annotations

import httpx
import pytest

from lib import apns
from tests.unit.test_apns import _apns_config, _FakeResp

TOKEN = "ab" * 32


@pytest.fixture
def clients(monkeypatch):
    made = []

    class Client:
        def __init__(self, *args, **kwargs):
            self.closed = False
            made.append(self)

        def post(self, url, headers=None, content=None):
            if self.closed:
                raise RuntimeError("Cannot send a request, as the client has been closed.")
            return _FakeResp(200, apns_id="x")

        def close(self):
            self.closed = True
    monkeypatch.setattr(httpx, "Client", Client)
    apns._reset_pooled_client()
    yield made
    apns._reset_pooled_client()


def test_a_batch_holding_the_old_client_does_not_close_the_replacement(tmp_path, clients):
    cfg = _apns_config(tmp_path)
    first, second = apns._DirectTransport(cfg), apns._DirectTransport(cfg)
    first.reset()                    # one batch's send failed: the shared client is replaced
    second.reset()                   # another batch, still on the old client, resets too
    assert not clients[-1].closed
    assert first.send(TOKEN, "production", {"aps": {}})[0] == 200


def test_a_send_whose_client_another_batch_closed_is_retried_on_the_live_one(tmp_path, clients):
    cfg = _apns_config(tmp_path)
    first, second = apns._DirectTransport(cfg), apns._DirectTransport(cfg)
    first.reset()                    # closes the client the second batch still holds
    assert second.send(TOKEN, "production", {"aps": {}})[0] == 200

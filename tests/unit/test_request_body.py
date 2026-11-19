"""The body cap is one table (issue 360, R81); its behaviour is pinned in
tests/integration/test_request_body_cap.py and tests/unit/test_relay_body_cap.py."""
from __future__ import annotations

from lib import request_body

MiB = 1024 * 1024


def test_the_cap_is_one_table():
    assert request_body.limit("POST", "/artifacts") == 16 * MiB
    assert request_body.limit("PUT", "/settings") == 16 * MiB
    assert request_body.limit("POST", "/pairing/exchange") == 64 * 1024
    assert request_body.limit("GET", "/artifacts") is None
    assert {request_body.limit("POST", p) for p in ("/media", "/upload", "/transcribe")} == {None}
    assert request_body.family("/artifacts/12/revisions") == "/artifacts"

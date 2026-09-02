import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib.http_utils import redact_query_secrets  # noqa: E402


def test_redacts_token_query_values_without_dropping_other_params():
    out = redact_query_secrets("/events?token=secret&x=1")

    assert out == "/events?token=REDACTED&x=1"
    assert "secret" not in out


def test_redacts_token_after_other_query_params():
    out = redact_query_secrets("/events?_=123&token=abc")

    assert out == "/events?_=123&token=REDACTED"


def test_leaves_url_without_secrets_unchanged():
    assert redact_query_secrets("/agents/snapshot?x=1") == "/agents/snapshot?x=1"

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib import error_classify as ec


def test_connection_errors():
    for msg in [
        "API Error: The socket connection was closed unexpectedly.",
        "read ECONNRESET",
        "fetch failed",
        "Connection error.",
        "stream disconnected",
        "write EPIPE",
    ]:
        assert ec.classify_error(msg) == ec.CONNECTION, msg


def test_transient_api_errors():
    for msg in [
        "overloaded_error: Overloaded",
        "rate_limit_error: rate limited",
        "HTTP 429 Too Many Requests",
        "503 Service Unavailable",
        "502 Bad Gateway",
    ]:
        assert ec.classify_error(msg) == ec.TRANSIENT, msg


def test_usage_limit_errors_are_user_visible_not_transient():
    for msg in [
        "out of usage",
        "Usage limit reached for this account",
        "quota exceeded",
        "insufficient credits",
        "credit balance depleted",
        "Your workspace is out of credits. Ask your workspace owner to refill in order to continue.",
        "RESOURCE_EXHAUSTED: exceeded your current quota",
        "monthly limit reached",
        "You've hit your session limit · resets 3:20pm (Europe/Oslo)",
        # Grok Build: an exhausted balance arrives as an HTTP 402 blob. Before
        # this was recognised the turn classified UNKNOWN, which flips the
        # agent to IDLE — the chat just went silent with no badge at all.
        'Internal error: {\n  "message": "API error (status 402 Payment '
        'Required): Grok Build usage balance exhausted",\n  '
        '"http_status": 402\n}',
        "Grok Build usage balance exhausted",
        "API error (status 402 Payment Required)",
        "account balance exhausted",
    ]:
        assert ec.classify_error(msg) == ec.USAGE_LIMIT, msg


def test_stray_402_is_not_a_usage_limit():
    # A bare number must not trip the billing rule; only the HTTP status pair.
    assert ec.classify_error("wrote 402 tokens to the log") == ec.UNKNOWN


def test_runner_exit_errors_are_user_visible():
    for msg in [
        "codex exited rc=1",
        "clarp exited rc=2",
        "agy exited rc=1",
        "process exited code 1",
        "exit status 1",
    ]:
        assert ec.classify_error(msg) == ec.RUNNER_EXIT, msg


def test_interruptions_win_over_connection_noise():
    # A SIGTERM'd turn often also prints a broken-pipe message as it dies;
    # it must classify as INTERRUPTED, never auto-retried.
    assert ec.classify_error("turn_aborted reason=interrupted") == ec.INTERRUPTED
    assert ec.classify_error(
        "The user interrupted the previous turn; socket hang up"
    ) == ec.INTERRUPTED
    assert ec.classify_error("killed by signal SIGTERM") == ec.INTERRUPTED


def test_unknown_and_empty():
    assert ec.classify_error("") == ec.UNKNOWN
    assert ec.classify_error(None) == ec.UNKNOWN
    assert ec.classify_error("some unexpected parser crash") == ec.UNKNOWN


def test_clean_result():
    assert ec.classify_result({"duration_ms": 10}) == ec.CLEAN
    assert ec.classify_result({"subtype": "success", "is_error": False}) == ec.CLEAN
    assert ec.classify_result("not a dict") == ec.CLEAN


def test_error_result_classified_from_text():
    assert ec.classify_result(
        {"is_error": True,
         "result": "API Error: The socket connection was closed unexpectedly."}
    ) == ec.CONNECTION
    assert ec.classify_result(
        {"subtype": "error_during_execution", "error": "Overloaded"}
    ) == ec.TRANSIENT


def test_error_result_without_recognisable_text_defaults_to_transient():
    # An error-result we can't parse should still notify, not be ignored.
    assert ec.classify_result(
        {"is_error": True, "result": "???"}
    ) == ec.TRANSIENT


def test_expired_sign_in_is_auth_not_transient():
    # The exact CLI text from the 2026-09-19 reboot, when twelve agents were
    # dropped as "transient" and never retried.
    assert ec.classify_error("Failed to authenticate: OAuth session expired and could not be refreshed") == ec.AUTH
    assert ec.classify_error("Not logged in. Please run /login") == ec.AUTH
    assert ec.classify_error("token refresh failed: invalid_grant") == ec.AUTH
    # A usage limit that also mentions a token still counts as a usage limit.
    assert ec.classify_error("You've hit your usage limit; token expired") == ec.USAGE_LIMIT
    assert ec.AUTH in ec.NOTIFY

"""Synchronous response delivery for Codex transport stubs."""
from functools import wraps


def result_reply(reply):
    """A stub response runs the transport callback before returning to its caller."""
    @wraps(reply)
    def request(*args, on_result=None, **kwargs):
        result = reply(*args, **kwargs)
        if on_result is not None:
            on_result(result)
        return result
    return request

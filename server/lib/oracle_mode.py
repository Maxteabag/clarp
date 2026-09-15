"""Explicit per-connection opt-in; unknown/missing selections retain stable v2."""
from urllib.parse import parse_qs, urlparse


def tinkered_requested(path):
    return parse_qs(urlparse(path).query).get('tinkered') == ['1']


def voice_handler(path):
    if tinkered_requested(path):
        from .oracle_live import serve
    else:
        from .oracle_live_stable import serve
    return serve

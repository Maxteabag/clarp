#!/usr/bin/env python3
"""Writes the vectors the Rust Host's reading of a failed turn is tested against.

How a turn ended, beyond "clean", decides what the Host does next: try again in silence (a
dropped connection), wait for an account (a usage limit, a sign-in that expired, a plan without
the model), or tell the user. The Python Host reads that off the tool's own words with the
pure functions of `server/lib/error_classify.py`. This calls them for every text its own tests
use, for the wordings its comments record from real failures, and for variations of both, and
writes what they said to `crates/clarp-kernel/tests/fixtures/failure-vectors.json`.

    python3 server-rs/tools/failure-vectors.py

Nothing of a Host is touched or started: pure functions are called.
"""
from __future__ import annotations

import ast
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'failure-vectors.json'

# Wordings the module's comments record, and others of the same kinds: what tools really said.
SAID = [
    '', '   ', 'something else entirely', 'ok',
    'Claude usage limit reached', "You've hit your session limit · resets 3pm", 'You are out of usage',
    'usage limit reached. Your limit will reset at 3pm (Europe/Oslo)', 'Usage cap exceeded', 'usage quota',
    'You have hit your weekly limit, it resets Monday', 'hit your limit',
    'Subscription quota exhausted. Your usage window resets at 14:00', 'quota exhausted',
    'Subscription quota exhausted. Your credits reset at 14:00', 'Your trial quota resets in 3 days',
    'Grok Build usage balance exhausted', 'insufficient credit balance', 'You exceeded your current quota',
    'Your workspace is out of credits', 'billing hard limit reached', 'Monthly limit reached',
    'Account acme is suspended, possibly due to reaching the monthly spending limit',
    'free tier can only be used from within OpenCode', 'Payment required', 'status code 402', 'error 402 payment',
    'room 402', 'RESOURCE_EXHAUSTED', 'no credits remaining', 'Your AI credits balance is too low to continue.',
    'The model is not supported when using Codex with a ChatGPT account', 'gpt-6 is not available on your plan',
    'Your current plan does not include this model', 'not included in your current plan',
    'Failed to authenticate: OAuth session expired', 'OAuth token could not be refreshed', 'Not logged in',
    'invalid_grant', 'token has expired', 'token expired', '401 Unauthorized', 'unauthorised (401)',
    'Please run /login', 'run `claude login`', 'Request had insufficient authentication scopes.. Please log out '
    '(/logout) and log back in (/login).',
    'socket connection was closed unexpectedly', 'socket hang up', 'ECONNRESET', 'EPIPE', 'read ETIMEDOUT',
    'getaddrinfo ETIMEOUT api.example', 'EAI_AGAIN', 'ENOTFOUND', 'fetch failed', 'network error',
    'network timeout', 'stream disconnected before completion', 'Connection error.', 'premature close',
    'terminated unexpectedly', 'connection reset by peer',
    'Overloaded', 'rate limit exceeded', 'rate_limit_error', 'Too Many Requests', 'HTTP 429', 'error 503',
    'Internal Server Error', 'Service Unavailable', 'Bad Gateway', 'gateway timeout', 'Gateway Time-out',
    'API error: please retry', 'api timeout, temporarily failing', 'temporarily unavailable',
    'upstream request failed', 'The endpoint is unavailable', 'port 5000 is busy', 'took 500ms',
    'turn_aborted', 'turn aborted', 'deliberately interrupted', 'user interrupted the run', 'received SIGTERM',
    'SIGINT', 'killed by signal 15', 'aborted by user', 'You interrupted the previous turn',
    'The operation was aborted', 'SIGTERM: socket connection was closed',
    'turn timed out', 'watchdog killed the turn (SIGTERM)', 'idle timeout', 'no output for 600 s',
    'timeout waiting for response',
    'claude exited rc=1', 'codex exited rc=137', 'agent exited rc=2', 'opencode exited rc=1', 'exit status 3',
    'exited with code 1', 'process exited unexpectedly, code=9', 'process exited with rc 1', 'clarp exited rc=0',
    'muse_session_locked: another turn holds the session', 'muse_session_locked',
    'Codex could not complete this request. Try again',
    'Waiting for a claude account with available usage', 'Waiting for a codex account with available usage',
    'Waiting for a claude account that serves claude-opus-5-5; the active one does not right now',
    'Waiting for a claude account that serves this model; the active one does not right now',
    'Waiting for a claude account', 'waiting for a claude account with available usage',
    'Waiting for a claude account with available usage.', 'Waiting for an agy account with available usage',
    'stream error: {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}',
    'unexpected status 429: {"error": {"message": "You exceeded your current quota, please check your plan"}}',
    'request failed: {"message": "first"} then {"message": "the \\"last\\" one\\nwins"}',
    'prefix only {"detail": "no message here"}', '{"message": "bare body"}', '{"message": "bad \\x escape"}',
    'no body at all', '   padded text   ',
]

RESULTS = [
    None, 'text', 7, [], {},
    {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'done'},
    {'type': 'result', 'subtype': 'success', 'is_error': True, 'result': 'Claude usage limit reached'},
    {'type': 'result', 'subtype': 'error_during_execution'},
    {'type': 'result', 'subtype': 'error_max_turns', 'result': ''},
    {'type': 'result', 'is_error': True},
    {'type': 'result', 'is_error': True, 'error': 'ECONNRESET'},
    {'type': 'result', 'is_error': True, 'message': 'Failed to authenticate: OAuth session expired'},
    {'type': 'result', 'is_error': 1, 'result': 'Overloaded'},
    {'type': 'result', 'is_error': True, 'result': None, 'error': None, 'message': 'turn_aborted'},
    {'type': 'result', 'subtype': 'Error', 'result': 'something nobody has a word for'},
    {'type': 'result', 'is_error': 'yes', 'result': 'not available on your plan'},
]

WAITS = [('claude', ''), ('codex', ''), ('claude', 'claude-opus-5-5'), ('claude', None), ('agy', 'gemini-4'), ('', '')]


def strings_of(path: pathlib.Path) -> list[str]:
    """Every text the Python Host's own tests of the module use."""
    found = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value.strip()) >= 3:
            found.append(node.value)
    return found


def main() -> int:
    sys.path.insert(0, str(REPO / 'server'))
    from lib import error_classify as rules

    texts = []
    for text in [*SAID, *strings_of(REPO / 'tests' / 'unit' / 'test_error_classify.py')]:
        # As it came, shouted, and behind what a runner puts in front of it.
        for variant in (text, text.upper(), f'claude exited rc=1: {text}' if text.strip() else text):
            if variant not in texts and len(variant) <= 2000:
                texts.append(variant)
    read = [{'text': text, 'kind': rules.classify_error(text), 'resets': rules.usage_limit_resets(text),
             'explained': rules.explain(text), 'provider_message': rules.provider_message(text),
             'account_wait': rules.is_account_wait_message(text)} for text in texts]
    absent = {'kind': rules.classify_error(None), 'resets': rules.usage_limit_resets(None),
              'explained': rules.explain(None), 'provider_message': rules.provider_message(None),
              'account_wait': rules.is_account_wait_message(None)}
    results = [{'event': event, 'kind': rules.classify_result(event)} for event in RESULTS]
    waits = [{'backend': backend, 'model': model, 'message': rules.account_wait_message(backend, model)}
             for backend, model in WAITS]
    vectors = {
        'kinds': sorted({rules.CLEAN, rules.CONNECTION, rules.TRANSIENT, rules.INTERRUPTED, rules.USAGE_LIMIT,
                         rules.AUTH, rules.ACCOUNT_PLAN, rules.RUNNER_EXIT, rules.TIMEOUT, rules.NOT_STARTED,
                         rules.UNKNOWN, rules.MUSE_SESSION_LOCKED}),
        'notify': sorted(rules.NOTIFY),
        'quota_unconfirmed': rules.QUOTA_UNCONFIRMED_MESSAGE,
        'texts': read, 'absent': absent, 'results': results, 'waits': waits,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(vectors, indent=1, ensure_ascii=False, sort_keys=True) + '\n')
    by_kind = {}
    for vector in read:
        by_kind[vector['kind']] = by_kind.get(vector['kind'], 0) + 1
    print(f'{len(read)} texts, {len(results)} results, {len(waits)} waits -> {OUT.relative_to(REPO)}')
    print(', '.join(f'{kind}: {count}' for kind, count in sorted(by_kind.items())))
    return 0


if __name__ == '__main__':
    sys.exit(main())

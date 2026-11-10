"""Cartesia Realtime STT over the websocket, which is the only place it
accepts vocabulary.

The batch endpoint takes no biasing at all, so Ink-Whisper has always
transcribed blind while every other provider was handed the glossary. These
tests pin the streaming contract that fixes that: keyterms ride on the
connection URL, and only Ink-2 honours them.
"""
from __future__ import annotations

import json

import pytest

from lib import cartesia_stt_ws as ws


class FakeSocket:
    """Records what was sent and replays a scripted server transcript."""

    def __init__(self, script: list[dict]):
        self.script = list(script)
        self.binary: list[bytes] = []
        self.text: list[str] = []
        self.closed = False

    def send_binary(self, payload: bytes) -> None:
        self.binary.append(payload)

    def send(self, payload: str) -> None:
        self.text.append(payload)

    def recv(self) -> str:
        if not self.script:
            return json.dumps({"type": "done", "request_id": "r"})
        return json.dumps(self.script.pop(0))

    def close(self) -> None:
        self.closed = True


# --- connection URL -------------------------------------------------------

def test_url_carries_the_contract_parameters():
    url = ws.connection_url(model="ink-2", sample_rate=16_000, keyterms=[])
    assert url.startswith("wss://api.cartesia.ai/stt/websocket?")
    assert "model=ink-2" in url
    assert "encoding=pcm_s16le" in url
    assert "sample_rate=16000" in url
    assert f"cartesia_version={ws.API_VERSION}" in url


def test_each_keyterm_is_its_own_repeated_parameter():
    url = ws.connection_url(
        model="ink-2", sample_rate=16_000, keyterms=["Tripletex", "Omarchy"])
    assert url.count("keyterm=") == 2
    assert "keyterm=Tripletex" in url
    assert "keyterm=Omarchy" in url


def test_ink_whisper_is_never_sent_keyterms():
    """Cartesia ignores them with a warning; sending them is a lie to the user."""
    url = ws.connection_url(
        model="ink-whisper", sample_rate=16_000, keyterms=["Tripletex"])
    assert "keyterm=" not in url


@pytest.mark.parametrize("model", ["ink-2", "ink-preview"])
def test_biasing_models_accept_keyterms(model):
    url = ws.connection_url(model=model, sample_rate=16_000, keyterms=["ECIT"])
    assert "keyterm=ECIT" in url


# --- the documented limits ------------------------------------------------

def test_keyterms_are_capped_at_one_hundred():
    kept = ws.fit_keyterms([f"term{i}" for i in range(250)])
    assert len(kept) == ws.MAX_KEYTERMS == 100


def test_keyterms_are_capped_at_twelve_hundred_characters():
    kept = ws.fit_keyterms(["x" * 100] * 50)
    assert sum(len(t) for t in kept) <= ws.MAX_KEYTERM_CHARS == 1200


def test_fitting_keeps_earlier_terms_first():
    """Ranking upstream already put the most valuable terms first."""
    kept = ws.fit_keyterms(["first", "second", *[f"pad{i}" for i in range(200)]])
    assert kept[0] == "first"
    assert kept[1] == "second"


def test_blank_terms_never_reach_the_wire():
    assert ws.fit_keyterms(["  ", "", "real"]) == ["real"]


# --- the exchange ---------------------------------------------------------

def test_audio_is_sent_as_binary_chunks_then_finalized_and_closed():
    sock = FakeSocket([
        {"type": "transcript", "is_final": True, "text": "hello there"},
        {"type": "flush_done", "request_id": "r"},
        {"type": "done", "request_id": "r"},
    ])
    text = ws.transcribe_pcm(sock, pcm=b"\x00\x01" * 8000, chunk_bytes=4000)
    assert text == "hello there"
    assert sock.binary, "audio must be sent as binary frames"
    assert all(len(c) <= 4000 for c in sock.binary)
    assert b"".join(sock.binary) == b"\x00\x01" * 8000
    assert "finalize" in sock.text
    assert "close" in sock.text
    assert sock.closed


def test_final_transcripts_are_joined_and_partials_ignored():
    """`text` is a delta from the last final chunk, so only finals accumulate."""
    sock = FakeSocket([
        {"type": "transcript", "is_final": False, "text": "Trip"},
        {"type": "transcript", "is_final": True, "text": "Tripletex"},
        {"type": "transcript", "is_final": False, "text": "and Omar"},
        {"type": "transcript", "is_final": True, "text": " and Omarchy"},
        {"type": "done", "request_id": "r"},
    ])
    assert ws.transcribe_pcm(sock, pcm=b"\x00" * 100) == "Tripletex and Omarchy"


def test_a_server_error_is_raised_with_its_message():
    sock = FakeSocket([
        {"type": "error", "status_code": 402, "message": "quota exhausted"},
    ])
    with pytest.raises(ws.CartesiaSTTStreamError, match="quota exhausted"):
        ws.transcribe_pcm(sock, pcm=b"\x00" * 100)


def test_silence_yields_empty_text_rather_than_an_error():
    sock = FakeSocket([{"type": "done", "request_id": "r"}])
    assert ws.transcribe_pcm(sock, pcm=b"\x00" * 100) == ""


# --- wiring ---------------------------------------------------------------

def test_ink_2_is_offered_and_declares_keyterm_biasing():
    from lib import stt_providers
    row = next(m for d in stt_providers.CATALOG if d["id"] == "cartesia"
               for m in d["models"] if m["id"] == "cartesia:ink-2")
    assert row["biasing"] == "keyterms"


def test_ink_whisper_is_kept_as_the_latency_option():
    from lib import stt_providers
    ids = {m["id"] for d in stt_providers.CATALOG if d["id"] == "cartesia"
           for m in d["models"]}
    assert {"cartesia:ink-2", "cartesia:ink-whisper"} <= ids


def test_cartesia_budget_now_matches_what_ink_2_accepts():
    from lib.vocab_compile import budget_for
    b = budget_for("cartesia", "ink-2")
    assert b.capacity == 100
    assert "term" in str(b.unit).lower()


def test_only_the_biasing_models_take_the_socket_route():
    from lib import stt_providers
    assert "ink-2" in stt_providers._CARTESIA_SOCKET_MODELS
    assert "ink-whisper" not in stt_providers._CARTESIA_SOCKET_MODELS


# 937: cleanup diagnostics must never replace the prior transcript/exception.
_CLOSE_SECRET = 'SYNTHETIC_CLOSE_SECRET_937'


class MediaCloseFault(RuntimeError):
    def __str__(self):
        raise AssertionError('close fault message must not be formatted')

    def __repr__(self):
        raise AssertionError('close fault repr must not be formatted')


class ClosingSocket(FakeSocket):
    def __init__(self, script, *, close_fails=True, receive_error=None):
        import threading
        super().__init__(script)
        self.lock, self.readlock = threading.RLock(), threading.RLock()
        self.close_fails, self.receive_error = close_fails, receive_error
        self.close_calls, self.recv_calls = 0, 0
        self.primary_at_close = None

    def recv(self):
        with self.readlock:
            self.recv_calls += 1
            if self.receive_error is not None:
                raise self.receive_error
            return super().recv()

    def close(self):
        import sys
        with self.lock:
            self.close_calls += 1
            self.primary_at_close = sys.exception()
            self.closed = True
            if self.close_fails:
                raise MediaCloseFault(_CLOSE_SECRET)


@pytest.fixture
def close_faults(monkeypatch):
    import io
    import threading
    from types import SimpleNamespace
    from lib import eventlog, log as logger
    stderr, records, raw, locks, checks, clock = io.StringIO(), [], [], [], [], [100.0]
    def record(*args, **kwargs):
        acquired = []
        def inspect():
            for lock in locks:
                ok = lock.acquire(blocking=False)
                acquired.append(ok)
                if ok:
                    lock.release()
        worker = threading.Thread(target=inspect)
        worker.start()
        worker.join(timeout=2)
        checks.append((not worker.is_alive(), tuple(acquired)))
        records.append((args, kwargs))
    monkeypatch.setattr(logger.sys, 'stderr', stderr)
    monkeypatch.setattr(logger, 'time', SimpleNamespace(monotonic=lambda: clock[0], strftime=logger.time.strftime))
    monkeypatch.setattr(eventlog, 'emit', record)
    monkeypatch.setattr(logger.os, 'write', lambda fd, data: raw.append((fd, data)) or len(data))
    logger._FAULT_LOG_THROTTLE.clear()
    logger._FAULT_LOG_COUNTS.clear()
    yield SimpleNamespace(stderr=stderr, records=records, raw=raw, locks=locks,
                          checks=checks, clock=clock, logger=logger)
    logger._FAULT_LOG_THROTTLE.clear()
    logger._FAULT_LOG_COUNTS.clear()


@pytest.mark.parametrize('text', ['retained transcript', ''])
def test_failed_close_preserves_text_once_and_reports_after_locks_release(close_faults, text):
    f = close_faults
    script = ([{'type': 'transcript', 'is_final': True, 'text': text}] if text else [])
    sock = ClosingSocket(script + [{'type': 'done'}])
    f.locks.extend([sock.lock, sock.readlock])
    assert ws.transcribe_pcm(sock, pcm=b'\x00\x01' * 5) == text
    assert sock.close_calls == 1 and sock.recv_calls == len(script) + 1
    assert sock.text == ['finalize', 'close'] and b''.join(sock.binary) == b'\x00\x01' * 5
    assert len(f.records) == 1 and f.checks == [(True, (True, True))]
    assert f.records[0][0] == ('server', 'cartesiaSTTSocketCloseFailed')
    assert f.records[0][1]['detail']['error_type'] == 'MediaCloseFault'
    assert _CLOSE_SECRET not in f.stderr.getvalue() + repr(f.records) + repr(f.raw)


@pytest.mark.parametrize('protocol_error', [False, True])
def test_failed_close_preserves_primary_exception_identity_and_origin(close_faults, protocol_error):
    f = close_faults
    primary = RuntimeError('existing receive failure')
    sock = ClosingSocket([{'type': 'error', 'message': 'existing protocol failure'}],
                         receive_error=None if protocol_error else primary)
    f.locks.extend([sock.lock, sock.readlock])
    expected = ws.CartesiaSTTStreamError if protocol_error else RuntimeError
    with pytest.raises(expected) as raised:
        ws.transcribe_pcm(sock, pcm=b'\x00\x00')
    assert raised.value is sock.primary_at_close
    if protocol_error:
        assert str(raised.value) == 'existing protocol failure'
    else:
        assert raised.value is primary and str(raised.value) == 'existing receive failure'
    tb = raised.value.__traceback__
    while tb.tb_next is not None:
        tb = tb.tb_next
    assert tb.tb_frame.f_code.co_name == ('transcribe_pcm' if protocol_error else 'recv')
    assert sock.close_calls == 1 and sock.recv_calls == 1
    assert f.checks == [(True, (True, True))]
    assert _CLOSE_SECRET not in f.stderr.getvalue() + repr(f.records)


def test_successful_close_has_no_new_fault(close_faults):
    sock = ClosingSocket([{'type': 'done'}], close_fails=False)
    assert ws.transcribe_pcm(sock, pcm=b'\x00\x00') == ''
    assert sock.close_calls == 1 and close_faults.records == []
    assert close_faults.stderr.getvalue() == ''


@pytest.mark.parametrize('stage', ['binary', 'finalize', 'close'])
def test_send_failure_before_receive_try_still_does_not_attempt_close(close_faults, stage):
    primary = RuntimeError('existing send failure')
    class SendFailure(ClosingSocket):
        def send_binary(self, payload):
            if stage == 'binary':
                raise primary
            super().send_binary(payload)
        def send(self, payload):
            if payload == stage:
                raise primary
            super().send(payload)
    sock = SendFailure([])
    with pytest.raises(RuntimeError) as raised:
        ws.transcribe_pcm(sock, pcm=b'\x00\x00')
    assert raised.value is primary and sock.close_calls == sock.recv_calls == 0
    assert close_faults.records == []


def test_close_fault_throttle_does_not_skip_closes(close_faults):
    f = close_faults
    sockets = [ClosingSocket([{'type': 'done'}]) for _ in range(4)]
    for sock in sockets[:3]:
        assert ws.transcribe_pcm(sock, pcm=b'\x00\x00') == ''
    assert [s.close_calls for s in sockets[:3]] == [1, 1, 1] and len(f.records) == 1
    f.clock[0] += 60
    assert ws.transcribe_pcm(sockets[3], pcm=b'\x00\x00') == ''
    assert sockets[3].close_calls == 1 and len(f.records) == 2
    assert f.records[-1][1]['detail']['suppressed'] == 2


@pytest.mark.parametrize('primary_fails', [False, True])
def test_all_diagnostic_sinks_failing_preserves_return_or_primary(close_faults, monkeypatch, primary_fails):
    from lib import eventlog
    f = close_faults
    class BrokenStream:
        def write(self, data):
            raise OSError(_CLOSE_SECRET)
        def flush(self):
            raise OSError(_CLOSE_SECRET)
    def fail(*args, **kwargs):
        raise OSError(_CLOSE_SECRET)
    monkeypatch.setattr(f.logger.sys, 'stderr', BrokenStream())
    monkeypatch.setattr(eventlog, 'emit', fail)
    monkeypatch.setattr(f.logger.os, 'write', fail)
    primary = RuntimeError('preserved receive failure') if primary_fails else None
    sock = ClosingSocket([{'type': 'done'}], receive_error=primary)
    if primary_fails:
        with pytest.raises(RuntimeError) as raised:
            ws.transcribe_pcm(sock, pcm=b'\x00\x00')
        assert raised.value is primary is sock.primary_at_close
    else:
        assert ws.transcribe_pcm(sock, pcm=b'\x00\x00') == ''
    assert sock.close_calls == 1 and f.logger._FAULT_LOG_COUNTS['dropped'] == 1

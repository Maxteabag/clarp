"""Remote-only source controls for bounded REFUSAL evidence, UNEXECUTED.

Primary contract: exhausted/partially published main storage cannot erase the
refusal or turn a retained prefix into a complete native-history certificate.
These tests do not attest root/process lifetime, kernel sharing or persistence.
"""
import socket
import struct

import pytest

from .packed_columns import Bank
from .terminal_channel import Terminal, SIZE


@pytest.fixture(autouse=True)
def remote_only():
    if socket.gethostname() == 'laptopstudio':
        pytest.fail('BUILDBOX_ONLY; controls not admitted locally')


def test_empty_and_valid_refusal_are_never_success():
    raw = bytearray(SIZE); channel = Terminal(raw)
    assert channel.read()['state'] == 'UNRECORDED' and channel.read()['qualified'] is False
    channel.mark('APPEND_INCOMPLETE',bank=7,published=(5,2,19))
    result = channel.read()
    assert result['state'] == 'PARTIAL' and result['qualified'] is False
    assert result['last_published'] == {'rows':5,'dictionary_entries':2,'dictionary_bytes':19}
    assert result['triggering_event_missing'] and not result['suffix_qualified']
    before = bytes(raw)
    channel.mark('EXPORT_INCOMPLETE',bank=99,published=(99,99,99))
    assert bytes(raw) == before, 'first triggering condition must remain immutable'
    with pytest.raises(ValueError,match='not fresh'):
        Terminal(raw)


@pytest.mark.parametrize('damage',('body','digest','padding','uncommitted_digest'))
def test_torn_or_corrupt_refusal_is_unknown(damage):
    raw=bytearray(SIZE);channel=Terminal(raw)
    channel.mark('DECODE_INCOMPLETE',bank=0,published=(1,1,3),prefix_integrity_unknown=True)
    if damage=='body':raw[20]^=1
    elif damage=='digest':raw[80]^=1
    elif damage=='padding':raw[127]=1
    else:raw[80:112]=bytes(32)
    with pytest.raises(ValueError,match='UNKNOWN'):
        channel.read()


def test_exhaustion_retains_exact_published_prefix_and_gap():
    raw=bytearray(SIZE)
    value=Bank(schema=(('revision','i64'),('key','key')),rows=1,
               dictionary_entries=1,dictionary_bytes=128,storage_limit_bytes=1024,
               domain=b'CONTROL_ONLY_NOT_A_PRODUCER_ATTESTATION',terminal_buffer=raw,bank_id=4)
    value.append({'revision':8,'key':('a',)})
    with pytest.raises(ValueError,match='row capacity'):
        value.append({'revision':9,'key':('b',)})
    retained=value.partial_segments()
    assert retained['qualified'] is False and retained['suffix_qualified'] is False
    assert retained['terminal']['code']=='APPEND_INCOMPLETE'
    assert retained['terminal']['bank']==4 and retained['last_published'][0]==1
    assert bytes(retained['segments']['column-0.bin'])==struct.pack('<q',8)
    assert retained['segments']['column-0.bin'].readonly
    assert retained['terminal']['triggering_event_missing'] is True
    assert all(v.readonly for v in retained['segments'].values())
    with pytest.raises(ValueError,match='incomplete/refused'):
        value.seal()


def test_lost_terminal_is_unknown_not_a_complete_prefix():
    raw=bytearray(SIZE)
    value=Bank(schema=(('revision','i64'),),rows=1,dictionary_entries=0,
               dictionary_bytes=0,storage_limit_bytes=1024,domain=b'CONTROL',terminal_buffer=raw)
    value.append({'revision':8})
    with pytest.raises(ValueError):value.append({'revision':9})
    raw[80]^=1
    assert value.partial_segments()['terminal']=={'state':'UNKNOWN','qualified':False}



def test_second_writer_cannot_overwrite_first_refusal():
    raw=bytearray(SIZE); first=Terminal(raw); other=Terminal(raw)
    first.mark('NATIVE_FAILURE',bank=1,published=(2,3,4))
    saved=bytes(raw)
    with pytest.raises(ValueError,match='already changed'):
        other.mark('TIMEOUT',bank=2,published=(5,6,7))
    assert bytes(raw)==saved and first.read()['code']=='NATIVE_FAILURE'

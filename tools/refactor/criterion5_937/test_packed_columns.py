"""Remote-only authored controls for STORAGE contract, not native transactions.

Primary risk: a malformed/partially published compact fact qualifies as complete
or silently changes typed identity. The native component controls own native
behavior; these source-only cases require no new product seam. UNEXECUTED.
"""
import hashlib
import json
from pathlib import Path
import socket
import struct

import pytest

from .packed_columns import Bank, decode_identity, identity


@pytest.fixture(autouse=True)
def remote_only():
    if socket.gethostname() == 'laptopstudio':
        pytest.fail('BUILDBOX_ONLY; no control execution admitted locally')


def bank(schema, **options):
    config = dict(schema=schema, rows=4, dictionary_entries=16,
                  dictionary_bytes=4096, storage_limit_bytes=8192,
                  domain=b'EXPLICIT_SYNTHETIC_CONTROL_DOMAIN_NOT_KERNEL_PROOF')
    config.update(options)
    return Bank(**config)


def test_exact_signed_optional_state_and_lossless_identity(tmp_path):
    value = bank((('birth','u64'),('revision','i64'),('parent','optional_u64'),
                  ('wall','optional_i64'),('before','native_state'),('source','text'),
                  ('key','key'),('ancestors','ids')))
    first = dict(birth=2**64-1, revision=-2**63, parent=None, wall=0,
                 before=False, source='A\x00é/../\udcff', key=('msg-1','A'), ancestors=(0,2**64-1))
    second = dict(first, birth=0, revision=2**63-1, parent=0, wall=None, before=None)
    assert value.append(first) == 0 and value.append(second) == 1
    # This index certifies append order ONLY. No assertion of native order.
    with pytest.raises(ValueError, match='unsealed'):
        value.row(0)
    value.seal()
    assert value.row(0) == first and value.row(1) == second
    output = tmp_path/'bank'; output.mkdir()
    manifest = value.export(output)
    assert manifest['used_rows'] == 2 and manifest['structurally_sealed'] is True
    raw = (output/'column-0.bin').read_bytes()
    assert raw == struct.pack('<QQ', 2**64-1, 0)  # independent typed wire expectation
    assert (output/'column-1.bin').read_bytes() == struct.pack('<qq', -2**63, 2**63-1)
    for name, receipt in manifest['files'].items():
        data = (output/name).read_bytes()
        assert len(data) == receipt['bytes'] and hashlib.sha256(data).hexdigest() == receipt['sha256']
    assert json.loads((output/'manifest.json').read_bytes())['domain_hex'] == value.domain.hex()


@pytest.mark.parametrize('kind,bad', [('u64',-1),('u64',2**64),('u64',True),
                                     ('i64',2**63),('i64',-2**63-1),
                                     ('optional_i64',False),('native_state',1),
                                     ('text',b'bytes'),('key',('one',2)),('ids',(True,))])
def test_invalid_type_or_width_refuses_whole_bank(kind, bad):
    value = bank((('field',kind),))
    with pytest.raises(ValueError):
        value.append({'field':bad})
    assert value.state == 'REFUSED' and value.used == 0
    with pytest.raises(ValueError, match='incomplete/refused'):
        value.seal()


def test_reserve_all_columns_dictionary_and_row_bytes_before_publish():
    value = bank((('first','text'),('second','text')),dictionary_entries=1)
    with pytest.raises(ValueError, match='dictionary capacity'):
        value.append({'first':'one','second':'two'})
    assert value.used == value.entries == value.bytes_used == 0
    value = bank((('first','text'),),dictionary_bytes=2)
    with pytest.raises(ValueError, match='dictionary capacity'):
        value.append({'first':'four'})
    assert value.used == 0
    value = bank((('first','u64'),),rows=1)
    value.append({'first':7})
    with pytest.raises(ValueError, match='row capacity'):
        value.append({'first':8})
    assert value.used == 1 and value.state == 'REFUSED'
    with pytest.raises(ValueError):
        value.seal()  # the retained valid prefix is NOT a complete bank
    with pytest.raises(ValueError, match='allocation budget'):
        bank((('first','u64'),),rows=1000,storage_limit_bytes=10)


def test_typed_dictionary_corruption_is_not_a_valid_identity():
    assert identity('text','\x00é') == b'T\x00\xc3\xa9'
    assert identity('ids',(1,)) == b'I\x01' + struct.pack('<Q',1)
    assert identity('key',('a','b')) == b'K\x02\x01\x00\x00\x00a\x01\x00\x00\x00b'
    with pytest.raises(ValueError, match='type/tag'):
        decode_identity('key',identity('text','key'))
    with pytest.raises(ValueError, match='tuple wire'):
        decode_identity('ids',b'I\x02'+struct.pack('<Q',1))
    with pytest.raises(ValueError, match='trailing'):
        decode_identity('key',identity('key',('a',))+b'x')
    value = bank((('parent','optional_u64'),('key','key')))
    value.append({'parent':None,'key':('a',)}); value.seal()
    value.columns['parent'][1] = 1
    with pytest.raises(ValueError, match='tag/padding'):
        value.row(0)
    value = bank((('key','key'),)); value.append({'key':('a',)}); value.seal()
    struct.pack_into('<I',value.offsets,4,value.bytes_used+1)
    with pytest.raises(ValueError, match='offsets'):
        value.row(0)


def test_export_failure_retains_artifacts_and_refuses_bank(tmp_path,monkeypatch):
    value = bank((('revision','i64'),)); value.append({'revision':9}); value.seal()
    output = tmp_path/'export'; output.mkdir()
    native_open = Path.open
    primary = OSError('owned export interruption')
    def interrupted(path,*args,**kwargs):
        if path.name == 'manifest.pending':
            raise primary
        return native_open(path,*args,**kwargs)
    monkeypatch.setattr(Path,'open',interrupted)
    with pytest.raises(OSError) as raised:
        value.export(output)
    assert raised.value is primary and value.state == 'REFUSED'
    assert (output/'column-0.bin').read_bytes() == struct.pack('<q',9)
    assert not (output/'manifest.json').exists()
    with pytest.raises(ValueError, match='complete sealed'):
        value.export(output)  # no retry after partial export


def test_partial_column_append_cannot_seal_even_with_valid_prefix():
    value = bank((('first','u64'),('second','u64')))
    primary = MemoryError('owned second-column write failure')
    class BrokenColumn(bytearray):
        def __setitem__(self,key,data):
            raise primary
    value.columns['second'] = BrokenColumn(value.columns['second'])
    with pytest.raises(MemoryError) as raised:
        value.append({'first':31,'second':41})
    assert raised.value is primary and value.used == 0 and value.state == 'REFUSED'
    assert bytes(value.columns['first'][:8]) == struct.pack('<Q',31)
    with pytest.raises(ValueError, match='incomplete/refused'):
        value.seal()

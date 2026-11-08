"""Remote-only finite SQLite snapshot contracts, SOURCE ONLY / NOT EXECUTED.

Protect raw storage type/duplicate/schema completeness at the actual SQLite
boundary. Pure mocked rows cannot prove the read transaction selects all tables.
No product seam; no live DB or corpus. Bounds controls must refuse, not truncate.
"""
from copy import deepcopy
import socket
import sqlite3

import pytest

from . import raw_products


def fixture_db(tmp_path):
    if socket.gethostname() == 'laptopstudio':
        pytest.fail('BUILDBOX_ONLY; no local measurement controls authorized')
    path = tmp_path / 'synthetic.sqlite'
    with sqlite3.connect(path) as con:
        con.execute('CREATE TABLE products(id INTEGER PRIMARY KEY, value)')
        con.execute('CREATE TABLE duplicates(value)')
        con.execute('CREATE TABLE empty_table(value)')
        con.executemany('INSERT INTO products(value) VALUES(?)',
                        [(None,), (7,), (7.5,), ('["blob","78"]',), (b'x',)])
        con.executemany('INSERT INTO duplicates VALUES(?)', [('same',), ('same',)])
    return path


def test_actual_sqlite_keeps_all_types_duplicate_rows_empty_tables_and_no_data_write(tmp_path):
    path = fixture_db(tmp_path)
    before = path.read_bytes()
    value = raw_products.capture(path, owned_root=tmp_path)
    assert path.read_bytes() == before
    assert set(value['tables']) == {'products', 'duplicates', 'empty_table'}
    assert value['capture']['row_count'] == 7 and value['capture']['exclusions'] == []
    assert value['tables']['empty_table']['rows'] == []
    assert [row[1][0] for row in value['tables']['products']['rows']] == ['null', 'integer', 'real', 'text', 'blob']
    assert value['tables']['products']['primary_key'] == ['id']
    assert raw_products.exact_diff(value, raw_products.capture(path, owned_root=tmp_path))['changes'] == []
    with sqlite3.connect(path) as con:
        con.execute('DELETE FROM duplicates WHERE rowid=1')
        con.execute('ALTER TABLE products ADD COLUMN updated_at INTEGER')
        con.execute('UPDATE products SET updated_at=42 WHERE id=1')
    changed = raw_products.exact_diff(value, raw_products.capture(path, owned_root=tmp_path))
    assert changed['classified'] == 0 and changed['unresolved'] > 0
    assert any(row['kind'] == 'schema' for row in changed['changes'])
    assert any(row.get('table') == 'duplicates' and row.get('before') == 2 and row.get('after') == 1
               for row in changed['changes'])
    assert any(row.get('table') == 'products' and row['kind'] == 'row_multiplicity'
               and row['row'][-1] == ['integer', 42] for row in changed['changes'])


def test_schema_and_all_rows_share_snapshot_despite_independent_wal_commit(tmp_path, monkeypatch):
    path = fixture_db(tmp_path)
    native_connect = sqlite3.connect
    with native_connect(path) as con:
        assert con.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
    committed = []
    def observe_connect(*args, **kwargs):
        reader = native_connect(*args, **kwargs)
        def after_schema(statement):
            if statement.startswith('PRAGMA main.table_xinfo') and not committed:
                with native_connect(path) as writer:
                    writer.execute('UPDATE products SET value=99 WHERE id=2')
                committed.append(True)
        reader.set_trace_callback(after_schema)
        return reader
    with monkeypatch.context() as patch:
        patch.setattr(raw_products.sqlite3, 'connect', observe_connect)
        snapshot = raw_products.capture(path, owned_root=tmp_path)
    assert committed == [True]  # actual second connection committed during capture
    old = next(row for row in snapshot['tables']['products']['rows'] if row[0] == ['integer', 2])
    assert old[1] == ['integer', 7]
    fresh = raw_products.capture(path, owned_root=tmp_path)
    new = next(row for row in fresh['tables']['products']['rows'] if row[0] == ['integer', 2])
    assert new[1] == ['integer', 99]


@pytest.mark.parametrize('fault', ['row-limit', 'byte-limit', 'table-omitted', 'row-omitted', 'symlink', 'outside', 'nonfinite'])
def test_missing_products_and_unsafe_paths_never_become_unchanged(tmp_path, monkeypatch, fault):
    path = fixture_db(tmp_path)
    if fault.endswith('limit'):
        monkeypatch.setattr(raw_products, 'MAX_ROWS' if fault == 'row-limit' else 'MAX_ENCODED_BYTES', 1)
        with pytest.raises(ValueError, match='snapshot bound exceeded'):
            raw_products.capture(path, owned_root=tmp_path)
    elif fault == 'nonfinite':
        with sqlite3.connect(path) as con:
            con.execute('UPDATE products SET value=? WHERE id=2', (float('inf'),))
        with pytest.raises(ValueError, match='nonfinite SQLite cell'):
            raw_products.capture(path, owned_root=tmp_path)
    elif fault in ('symlink', 'outside'):
        if fault == 'symlink':
            link = tmp_path / 'alias.sqlite'
            link.symlink_to(path)
            path = link
        else:
            (tmp_path / 'other').mkdir()
        with pytest.raises(ValueError, match='owned physical root'):
            raw_products.capture(path, owned_root=tmp_path if fault == 'symlink' else tmp_path / 'other')
    else:
        original = raw_products.capture(path, owned_root=tmp_path)
        value = deepcopy(original)
        if fault == 'table-omitted':
            del value['tables']['duplicates']
        else:
            value['tables']['duplicates']['rows'].pop()
        with pytest.raises(ValueError, match='inventory'):
            raw_products.exact_diff(original, value)

"""All-table synthetic SQLite products; source preparation, NOT EXECUTED.

Only an externally qualified, private campaign database may be passed here.
No discovery of live paths, import/healing, schema changes or volatile masks.
Read-only SQL does not assert SQLite never touches a WAL shared-memory file.
Capture overhead is charged separately, outside the fixed steady interval.
"""
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time

MAX_ROWS = 250000
MAX_ENCODED_BYTES = 256 * 1024 * 1024


def encoded(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('ascii')


def cell(value):
    """Storage types cannot collide with a text value shaped like a blob tag."""
    if value is None:
        return ['null']
    if type(value) is int:
        return ['integer', value]
    if type(value) is float and math.isfinite(value):
        return ['real', value.hex()]
    if type(value) is str:
        return ['text', value]
    if type(value) is bytes:
        return ['blob', value.hex()]
    raise ValueError('unsupported/nonfinite SQLite cell; never substitute a value')


def private_database(path, root):
    path, root = Path(path), Path(root)
    if (not root.is_absolute() or not path.is_absolute() or root.is_symlink()
            or path.is_symlink() or root.resolve(strict=True) != root
            or path.resolve(strict=True) != path or not path.is_file()
            or path == root or not path.is_relative_to(root)):
        raise ValueError('database is not a plain file under exact owned physical root')
    # Ownership/native process-drain/source certificates are separate required
    # runner evidence. A supplied pathname alone does not establish ownership.
    return path


def capture(path, *, owned_root):
    path = private_database(path, owned_root)
    identity = lambda stat: (stat.st_dev, stat.st_ino)
    before = path.stat()
    started = time.monotonic()
    cpu = time.process_time()
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True,
                                 timeout=2.0, isolation_level=None)
    try:
        connection.execute('PRAGMA query_only=ON')
        def authorize(action, first, second, database, trigger):
            if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_TRANSACTION):
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_PRAGMA and first == 'table_xinfo':
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        connection.set_authorizer(authorize)
        connection.execute('BEGIN')
        # The first read establishes one coherent snapshot for schema and rows.
        schema = [list(row) for row in connection.execute(
            'SELECT type,name,tbl_name,sql FROM main.sqlite_master ORDER BY type,name')]
        tables, total_rows, total_bytes = {}, 0, len(encoded(schema))
        if total_bytes > MAX_ENCODED_BYTES:
            raise ValueError('complete snapshot bound exceeded; no truncated success')
        for kind, name, owner, sql in schema:
            if kind != 'table':
                continue
            quoted = '"' + name.replace('"', '""') + '"'
            info = [list(row) for row in connection.execute('PRAGMA main.table_xinfo(' + quoted + ')')]
            total_bytes += len(encoded(info))
            if total_bytes > MAX_ENCODED_BYTES:
                raise ValueError('complete snapshot bound exceeded; no truncated success')
            columns = [row[1] for row in info]
            if not columns or len(set(columns)) != len(columns):
                raise ValueError('missing/ambiguous table columns')
            projection = ','.join('"' + col.replace('"', '""') + '"' for col in columns)
            cursor = connection.execute('SELECT ' + projection + ' FROM main.' + quoted)
            if [entry[0] for entry in cursor.description] != columns:
                raise ValueError('table column inventory changed')
            rows = []
            for record in cursor:
                row = [cell(value) for value in record]
                total_rows += 1
                total_bytes += len(encoded(row))
                if total_rows > MAX_ROWS or total_bytes > MAX_ENCODED_BYTES:
                    raise ValueError('complete snapshot bound exceeded; no truncated success')
                rows.append(row)
            rows.sort(key=encoded)
            tables[name] = {'columns': columns, 'column_info': info,
                            'primary_key': [row[1] for row in sorted(info, key=lambda r: r[5]) if row[5] > 0],
                            'rows': rows}
        connection.execute('ROLLBACK')
    finally:
        connection.close()
    if identity(path.stat()) != identity(before):
        raise ValueError('database identity changed during read snapshot')
    return {'format': 'criterion5-937-typed-raw-v1', 'schema': schema, 'tables': tables,
            'capture': {'database': str(path), 'file_device_inode': list(identity(before)),
                        'started_mono': started, 'ended_mono': time.monotonic(),
                        'cpu_seconds': time.process_time() - cpu, 'row_count': total_rows,
                        'exclusions': [], 'scope': 'one coherent read transaction; no field rewrites'}}


def exact_diff(before, after):
    """Every row/multiplicity/schema change stays visible, initially UNRESOLVED.

    This is NOT the final native-time correspondence oracle. Callers must add
    source/writer-specific proofs; the raw inputs and this diff stay immutable.
    """
    for value in (before, after):
        if (value['format'] != 'criterion5-937-typed-raw-v1'
                or value['capture']['exclusions'] != []):
            raise ValueError('unknown or excluded raw snapshot')
        expected = {row[1] for row in value['schema'] if row[0] == 'table'}
        if expected != set(value['tables']):
            raise ValueError('table omitted from schema inventory')
        actual_rows = sum(len(table['rows']) for table in value['tables'].values())
        if actual_rows != value['capture']['row_count']:
            raise ValueError('raw row inventory incomplete')
    changes = []
    if before['schema'] != after['schema']:
        changes.append({'kind': 'schema', 'before': before['schema'], 'after': after['schema']})
    for name in sorted(set(before['tables']) | set(after['tables'])):
        a, b = before['tables'].get(name), after['tables'].get(name)
        if a is None or b is None:
            changes.append({'kind': 'table', 'table': name, 'before': a, 'after': b})
            continue
        for field in ('columns', 'column_info', 'primary_key'):
            if a[field] != b[field]:
                changes.append({'kind': 'table_metadata', 'table': name, 'field': field,
                                'before': a[field], 'after': b[field]})
        ac = Counter(encoded(row) for row in a['rows'])
        bc = Counter(encoded(row) for row in b['rows'])
        for key in sorted(ac.keys() | bc.keys()):
            if ac[key] != bc[key]:
                changes.append({'kind': 'row_multiplicity', 'table': name,
                                'row': json.loads(key), 'before': ac[key], 'after': bc[key]})
    return {'changes': changes, 'unresolved': len(changes), 'classified': 0,
            'before_products_sha256': hashlib.sha256(encoded({'schema': before['schema'], 'tables': before['tables']})).hexdigest(),
            'after_products_sha256': hashlib.sha256(encoded({'schema': after['schema'], 'tables': after['tables']})).hexdigest(),
            'claim': 'exact raw products only; no timestamp/ID/revision exclusions or performance verdict'}

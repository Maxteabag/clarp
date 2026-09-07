#!/usr/bin/env python3
"""Rehearse a Clarp migration on a new private SQLite backup, preserving source."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import importlib
import json
import os
from pathlib import Path
import pickle
import sqlite3
import sys


def quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def fingerprint(connection, table, columns):
    info = connection.execute(f"PRAGMA table_info({quoted(table)})").fetchall()
    keys = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
    order = ','.join(map(quoted, keys)) if keys else 'rowid'
    query = f"SELECT {','.join(map(quoted, columns))} FROM {quoted(table)} ORDER BY {order}"
    digest, count = hashlib.sha256(), 0
    for row in connection.execute(query):
        encoded = pickle.dumps(tuple(row), protocol=4)
        digest.update(len(encoded).to_bytes(8, 'big'))
        digest.update(encoded)
        count += 1
    return count, digest.hexdigest()


def row_multiset(connection, table, columns):
    query = f"SELECT {','.join(map(quoted, columns))} FROM {quoted(table)}"
    # Serialized keys preserve SQLite value types as well as duplicate counts;
    # Python tuple equality alone would conflate INTEGER 1 and REAL 1.0.
    return Counter(pickle.dumps(tuple(row), protocol=4)
                   for row in connection.execute(query))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--server-root', required=True, type=Path,
                        help='Candidate server directory containing lib/db.py')
    parser.add_argument('--output', required=True, type=Path,
                        help='New private rehearsal database; never overwritten')
    parser.add_argument('--allow-added-rows', action='append', default=[], metavar='TABLE',
                        help='Allow insertions into this existing table while preserving '
                             'all original rows and duplicate counts; repeat per table')
    args = parser.parse_args()
    source, output = args.source.resolve(strict=True), args.output.absolute()
    server = args.server_root.resolve(strict=True)
    if not (server / 'lib/db.py').is_file():
        parser.error('server-root must contain lib/db.py')
    # Exclusive creation also refuses a pre-existing symlink. The source is
    # opened read-only; only this new backup is handed to migration code.
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    original = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
    rehearsal = sqlite3.connect(output, isolation_level=None)
    try:
        original.backup(rehearsal)
        before_version = rehearsal.execute('PRAGMA user_version').fetchone()[0]
        tables = [row[0] for row in rehearsal.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        allowed = set(args.allow_added_rows)
        unknown = allowed - set(tables)
        if unknown:
            parser.error('--allow-added-rows: unknown table(s): ' + ', '.join(sorted(unknown)))
        before = {}
        for table in tables:
            pragma = 'table_xinfo' if table in allowed else 'table_info'
            columns = [row[1] for row in rehearsal.execute(f'PRAGMA {pragma}({quoted(table)})')]
            snapshot = row_multiset if table in allowed else fingerprint
            before[table] = (columns, snapshot(rehearsal, table, columns))
        os.environ['CLAUDE_PWA_DB'] = str(output)
        sys.path.insert(0, str(server))
        db = importlib.import_module('lib.db')
        db._migrate(rehearsal)
        changes = []
        additions = {}
        after_tables = {row[0] for row in rehearsal.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")} if allowed else set()
        for table, (columns, expected) in before.items():
            if table in allowed:
                after_columns = {row[1] for row in rehearsal.execute(
                    f'PRAGMA table_xinfo({quoted(table)})')}
                # SQLite may interpret a missing double-quoted column as a
                # string literal. Check schema membership before selecting it.
                if table not in after_tables or not set(columns) <= after_columns:
                    changes.append(table)
                    additions[table] = {'before': sum(expected.values()),
                                        'error': 'table or original columns missing'}
                    continue
                actual = row_multiset(rehearsal, table, columns)
                missing = sum((expected - actual).values())
                additions[table] = {'before': sum(expected.values()),
                                    'after': sum(actual.values()),
                                    'added': sum((actual - expected).values()),
                                    'missing': missing}
                if missing:
                    changes.append(table)
                continue
            if fingerprint(rehearsal, table, columns) != expected:
                changes.append(table)
        integrity = rehearsal.execute('PRAGMA quick_check').fetchone()[0]
        after_version = rehearsal.execute('PRAGMA user_version').fetchone()[0]
        result = {'source': str(source), 'rehearsal': str(output),
                  'before_version': before_version, 'after_version': after_version,
                  'candidate_version': db._SCHEMA_VERSION,
                  'tables_checked': len(before), 'changed_existing_data': changes,
                  'quick_check': integrity,
                  'verified_additive': not changes and integrity == 'ok'
                      and after_version == db._SCHEMA_VERSION}
        if allowed:
            result['allowed_added_rows'] = additions
        print(json.dumps(result, indent=2))
        return 0 if result['verified_additive'] else 1
    finally:
        rehearsal.close()
        original.close()


if __name__ == '__main__':
    raise SystemExit(main())

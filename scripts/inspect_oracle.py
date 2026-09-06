#!/usr/bin/env python3
"""Inspect private Host Oracle journals without opening a voice session."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from lib import xdg


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=xdg.data_dir() / 'oracle-diagnostics')
    parser.add_argument('--session', help='Exact journal session UUID')
    parser.add_argument('--include-text', action='store_true', help='Include private transcript/tool content')
    args = parser.parse_args()
    files = sorted(args.directory.glob('*.jsonl'))
    if args.session:
        files = [p for p in files if p.stem == args.session]
        if not files:
            parser.error('Session not found')
    output = []
    for file in files:
        rows = []
        invalid_lines = 0
        for line in file.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                invalid_lines += 1
        counts = Counter(row.get('event', 'unknown') for row in rows)
        result = {'session_id': file.stem, 'events': dict(counts), 'invalid_lines': invalid_lines,
                  'closed': bool(counts.get('session.close'))}
        if args.include_text:
            result['timeline'] = rows
        output.append(result)
    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()

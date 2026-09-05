#!/usr/bin/env python3
"""Scan the corpus read-only; optionally learn rules on the separate cold path."""
import argparse
import json
import pathlib
import resource
import sqlite3
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server'))
from lib import viz_corpus, viz_normalize, viz_rule_author, viz_library


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db', type=pathlib.Path,
                   default=pathlib.Path.home() / '.local/share/clarp/state.sqlite')
    p.add_argument('--page-size', type=int, default=128)
    p.add_argument('--learn', action='store_true', help='Invoke models and apply decisions')
    p.add_argument('--limit', type=int, default=5, help='Maximum novel tools to learn')
    p.add_argument('--library', type=pathlib.Path, help='Use an isolated JSON library for a model smoke test')
    a = p.parse_args()
    if a.library:
        viz_library.path = lambda: a.library
    con = sqlite3.connect(a.db.resolve().as_uri() + '?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    started = time.monotonic()
    stats = {'rows': 0, 'eligible': 0, 'matched': 0}

    def rows():
        for row in viz_corpus.tool_rows(con, page_size=a.page_size):
            stats['rows'] += 1
            try:
                data = json.loads(row['detail'])
            except (TypeError, ValueError):
                continue
            if not isinstance(data, dict) or data.get('phase') == 'tool_finished':
                continue
            stats['eligible'] += 1
            if viz_normalize.classify(str(data.get('tool') or ''),
                                     data.get('input') or {}, data.get('file_path') or ''):
                stats['matched'] += 1
            yield row

    clusters = viz_normalize.unmatched_clusters(rows())
    con.close()
    stats.update(coverage_pct=round(100 * stats['matched'] / max(1, stats['eligible']), 2),
                 seconds=round(time.monotonic() - started, 3),
                 max_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                 clusters=clusters)
    if a.learn:
        stats['learning'] = viz_rule_author.learn(clusters, limit=a.limit)
    print(json.dumps(stats, indent=2))


if __name__ == '__main__':
    main()

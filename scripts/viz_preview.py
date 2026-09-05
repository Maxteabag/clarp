#!/usr/bin/env python3
"""Read-only fleet canvas preview: live corpus, no model calls or runtime boot."""
import argparse
import functools
import pathlib
import sqlite3
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
from lib import db, viz_learning, viz_library
from server import Handler as ProductionHandler


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db', type=pathlib.Path, required=True)
    p.add_argument('--port', type=int, default=7699)
    p.add_argument('--library', type=pathlib.Path)
    a = p.parse_args()
    # Only this process sees these DI overrides. No migrate() or server workers.
    def connection():
        con = getattr(db._LOCAL, 'conn', None)
        if con is None:
            con = sqlite3.connect(a.db.resolve().as_uri() + '?mode=ro', uri=True)
            con.row_factory = sqlite3.Row
            db._LOCAL.conn = con
        return con
    db.conn = connection
    viz_learning.offer = lambda clusters: {'designing': '', 'queued': []}
    if a.library:
        viz_library.path = lambda: a.library
    else:
        viz_library.load = viz_library.seed

    class Handler(SimpleHTTPRequestHandler):
        def _send(self, status, body, content_type='text/plain'):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.split('?')[0] == '/viz/events':
                try:
                    ProductionHandler._handle_viz_events(self)
                finally:
                    db.close_local()
            elif self.path == '/viz':
                self._send(200, (ROOT / 'static/viz.html').read_bytes(), 'text/html')
            elif self.path.startswith('/static/'):
                super().do_GET()
            else:
                self.send_error(404)

    server = ThreadingHTTPServer(('127.0.0.1', a.port), functools.partial(Handler, directory=str(ROOT)))
    print(f'Preview http://127.0.0.1:{server.server_port}/viz', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()

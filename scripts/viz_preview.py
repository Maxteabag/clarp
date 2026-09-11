#!/usr/bin/env python3
"""Read-only fleet canvas preview: live corpus, no model calls or runtime boot."""
import argparse
import functools
import pathlib
import json
import re
import mimetypes
import threading
import time
import sqlite3
import sys
from urllib.parse import unquote, urlsplit
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
from lib import db, viz_learning, viz_library
from server import Handler as ProductionHandler
from viz_reload import revisions


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db', type=pathlib.Path, required=True)
    p.add_argument('--port', type=int, default=7699)
    p.add_argument('--library', type=pathlib.Path)
    p.add_argument('--reload', action='store_true', help='Auto-refresh frontend edits and use checkout renderer sources (development only)')
    p.add_argument("--learn", action="store_true", help="Enable autonomous source development; requires --library")
    a = p.parse_args()
    if a.learn and not a.library:
        p.error("--learn requires an explicit persistent --library path")
    if a.reload and a.learn:
        p.error('--reload uses checkout source and cannot be combined with --learn')
    # Only this process sees these DI overrides. No migrate() or server workers.
    def connection():
        con = getattr(db._LOCAL, 'conn', None)
        if con is None:
            con = sqlite3.connect(a.db.resolve().as_uri() + '?mode=ro', uri=True)
            con.row_factory = sqlite3.Row
            db._LOCAL.conn = con
        return con
    db.conn = connection
    if not a.learn:
        viz_learning.offer = lambda clusters: {'designing': '', 'queued': []}
        viz_learning.offer_scene = lambda *a, **k: {'designing':'','queued':[]}
        viz_learning.status = lambda: {'enabled':False,'designing':'','queued':[]}
    if a.library:
        viz_library.path = lambda: a.library
    else:
        viz_library.load = viz_library.seed

    if a.reload:
        read_library = viz_library.load
        def source_library():
            library = dict(read_library())
            for key in ('program', 'previous_program', 'view_programs'):
                library.pop(key, None)
            return library
        viz_library.load = source_library

    class Handler(SimpleHTTPRequestHandler):
        def end_headers(self):
            if a.reload:
                self.send_header('Cache-Control', 'no-store')
            super().end_headers()
        def _send(self, status, body, content_type='text/plain'):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0 < size <= 16000:return None
                return json.loads(self.rfile.read(size))
            except (ValueError,OSError):return None

        def do_POST(self):
            if not a.learn or urlsplit(self.path).path != '/viz/supersede':
                return self.send_error(404)
            if self.headers.get('Content-Type','').split(';')[0] != 'application/json':
                return self.send_error(415)
            origin=self.headers.get('Origin')
            if origin and urlsplit(origin).netloc != self.headers.get('Host'):
                return self.send_error(403)
            try:
                ProductionHandler._handle_viz_supersede(self)
            finally:
                db.close_local()

        def do_HEAD(self):
            self.send_error(405)

        def do_GET(self):
            if a.reload and 'If-Modified-Since' in self.headers:
                del self.headers['If-Modified-Since']
            if urlsplit(self.path).path == '/viz/dev-revision' and a.reload:
                self._send(200,json.dumps(revisions(ROOT)).encode(),'application/json')
            elif urlsplit(self.path).path == '/viz/activity':
                try:ProductionHandler._handle_viz_activity(self)
                finally:db.close_local()
            elif urlsplit(self.path).path in ('/viz/recap', '/viz/recap/artifact'):
                try:
                    handler = ProductionHandler._handle_viz_recap_artifact if urlsplit(self.path).path.endswith('/artifact') else ProductionHandler._handle_viz_recap
                    handler(self)
                finally:
                    db.close_local()
            elif self.path.split('?')[0] == '/viz/events':
                try:
                    ProductionHandler._handle_viz_events(self)
                finally:
                    db.close_local()
            elif urlsplit(self.path).path.startswith('/viz/owner-avatar/'):
                from lib.viz_owner_avatar import portrait
                login=unquote(urlsplit(self.path).path[len('/viz/owner-avatar/'):])
                result=portrait(login)
                if not result:return self.send_error(404)
                self._send(200,*result)
            elif urlsplit(self.path).path.startswith('/media/'):
                # Recorded media assets back artifact previews. Only the asset
                # row's own storage path is served, read-only, like production.
                asset_id=unquote(urlsplit(self.path).path[len('/media/'):]).strip('/')
                if not re.fullmatch(r'[A-Za-z0-9_-]{4,80}',asset_id):return self.send_error(400)
                try:
                    row=db.conn().execute('SELECT storage_path,mime_type FROM media_assets WHERE asset_id=? AND deleted_at IS NULL',(asset_id,)).fetchone()
                    path=pathlib.Path(str(row['storage_path'])).resolve() if row else None
                    root=(pathlib.Path.home()/'.local/share/clarp/media').resolve()
                    if not path or not path.is_file() or root not in path.parents:return self.send_error(404)
                    size=path.stat().st_size;start=0;end=size-1;status=200
                    requested=self.headers.get('Range')
                    if requested:
                        match=re.fullmatch(r'bytes=(\d*)-(\d*)',requested)
                        if not match or not any(match.groups()):return self.send_error(416)
                        left,right=match.groups()
                        if left:start=int(left);end=min(end,int(right)) if right else end
                        else:start=max(0,size-int(right))
                        if start>end or start>=size:return self.send_error(416)
                        status=206
                    self.send_response(status);self.send_header('Content-Type',str(row['mime_type']))
                    self.send_header('Accept-Ranges','bytes');self.send_header('Content-Length',str(max(0,end-start+1)))
                    if status==206:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
                    self.end_headers()
                    with path.open('rb') as source:
                        source.seek(start);remaining=end-start+1
                        while remaining>0:
                            chunk=source.read(min(65536,remaining))
                            if not chunk:break
                            self.wfile.write(chunk);remaining-=len(chunk)
                finally:
                    db.close_local()
            elif urlsplit(self.path).path.startswith('/avatars/'):
                # Same exact-agent portrait resolution as the main app. Never
                # accept a filesystem path from the URL.
                identity=unquote(urlsplit(self.path).path[len('/avatars/'):])
                try:
                    row=db.conn().execute('SELECT avatar_path FROM agents WHERE agent_id=?',(identity,)).fetchone()
                    path=pathlib.Path(str(row['avatar_path'] or '')) if row else None
                    if not path or not path.is_file():return self.send_error(404)
                    self._send(200,path.read_bytes(),mimetypes.guess_type(str(path))[0] or 'application/octet-stream')
                finally:
                    db.close_local()
            elif urlsplit(self.path).path == '/viz':
                revision=revisions(ROOT) if a.reload else None
                body=(ROOT / 'static/viz.html').read_bytes()
                if a.reload:
                    body += ('<script type="application/json" id="viz-dev-revision">'+json.dumps(revision)+'</script><script type="module" src="/static/lib/viz-dev-reload.js"></script>').encode()
                self._send(200, body, 'text/html')
            elif self.path.startswith('/static/'):
                target=(ROOT/unquote(urlsplit(self.path).path).lstrip('/')).resolve()
                if not target.is_relative_to((ROOT/'static').resolve()) or not target.is_file():
                    return self.send_error(404)
                super().do_GET()
            else:
                self.send_error(404)

    server = ThreadingHTTPServer(('127.0.0.1', a.port), functools.partial(Handler, directory=str(ROOT)))
    stop=threading.Event()
    def observe():
        from lib import viz_normalize
        while not stop.is_set():
            try:
                world=viz_normalize.build_fleet_map(int(time.time()*1000)-3600000)['world']
                viz_learning.offer_scene(world)
            except Exception as error:
                print('Fleet observer:', error, flush=True)
            finally:
                db.close_local()
            stop.wait(15)
    if a.learn:
        threading.Thread(target=observe,name='fleet-observer',daemon=True).start()
    print(f'Preview http://127.0.0.1:{server.server_port}/viz learning={a.learn}', flush=True)
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()


if __name__ == '__main__':
    main()

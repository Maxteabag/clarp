import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, URL } from 'node:url';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const repo = fileURLToPath(new URL('.', import.meta.url));
const staticDir = path.join(repo, 'static');

// The dev server proxies the API to a running Clarp, so it has to
// authenticate like any other client. Read the token the same way the server
// does rather than making the developer export one.
function authToken() {
  const cfg = path.join(process.env.HOME || '', '.config/clarp/config.toml');
  try {
    const m = fs.readFileSync(cfg, 'utf8').match(/^\s*auth_token\s*=\s*"([^"]*)"/m);
    return m ? m[1] : '';
  } catch {
    return '';
  }
}

// Everything Vite does not own is API, and gets proxied. Written as an
// exclusion rather than a list of routes because server.py has 70+ top-level
// paths, and adding one there should not mean editing this file.
//
// The `\?` in the exclusion matters: this is matched against req.url, which
// carries the query string, so `$` alone leaves the root excluded only when it
// is bare. The desktop shell opens `/?token=...`, which without this would be
// proxied to the Python server — the app would then boot from the built bundle
// with no Vite client attached, and nothing would ever hot-reload.
const API = String.raw`^/(?!$|\?|src/|@|node_modules/|static/|styles\.css|manifest\.json|icon\.png|sw\.js)`;

const MIME = {
  '.js': 'application/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.ico': 'image/x-icon',
  '.woff': 'font/woff', '.woff2': 'font/woff2', '.ttf': 'font/ttf',
};

// Serve the repo's static/ during dev so editing styles.css or a vendored lib
// is live without a build. publicDir cannot do this: it maps to the URL root,
// and these assets need the `/static` prefix they have in production.
function serveRepoStatic() {
  return {
    name: 'clarp-dev-static',
    apply: 'serve',
    configureServer(server) {
      // static/ sits outside Vite's root, so nothing here is watched and an
      // edit to styles.css or a vendored lib would sit there until the next
      // manual restart. Watch it and ask the page to reload.
      server.watcher.add(staticDir);
      server.watcher.on('change', (file) => {
        if (!file.startsWith(staticDir)) return;
        // The bundle is build output landing in the same tree; reloading on it
        // would fight `npm run build` rather than help.
        if (file.startsWith(path.join(staticDir, 'app'))) return;
        server.ws.send({ type: 'full-reload', path: '*' });
      });

      server.middlewares.use((req, res, next) => {
        const url = (req.url || '').split('?')[0];

        // A service worker in dev fights HMR — a cached shell keeps serving
        // the previous bundle after every hot update, which is exactly how a
        // desktop-shell session ends up stuck on stale JS. Hand back the same
        // self-destructing worker the server ships, so any worker still
        // installed against this origin removes itself.
        if (url === '/sw.js') {
          res.setHeader('Content-Type', MIME['.js']);
          res.setHeader('Cache-Control', 'no-store');
          res.end(fs.readFileSync(path.join(staticDir, 'sw.js')));
          return;
        }

        const rel = url === '/styles.css' || url === '/manifest.json' || url === '/icon.png'
          ? url.slice(1)
          : url.startsWith('/static/') ? url.slice('/static/'.length) : '';
        if (!rel) return next();

        const target = path.join(staticDir, rel);
        if (!target.startsWith(staticDir)) { res.statusCode = 403; res.end(); return; }
        fs.readFile(target, (err, data) => {
          if (err) return next();
          res.setHeader('Content-Type',
            MIME[path.extname(target).toLowerCase()] || 'application/octet-stream');
          res.setHeader('Cache-Control', 'no-store');
          res.end(data);
        });
      });
    },
  };
}

const proxy = {
  [API]: {
    target: process.env.CLARP_UPSTREAM || 'http://127.0.0.1:7682',
    changeOrigin: true,
    // /events is Server-Sent Events: it must not be buffered or timed out.
    timeout: 0,
    proxyTimeout: 0,
    configure(p) {
      const token = authToken();
      p.on('proxyReq', (req) => {
        if (token) req.setHeader('Authorization', 'Bearer ' + token);
      });
      p.on('error', (err, _req, res) => {
        if (res && typeof res.writeHead === 'function' && !res.headersSent) {
          res.writeHead(502, { 'Content-Type': 'text/plain' });
          res.end('upstream unreachable: ' + err.message);
        }
      });
    },
  },
};

// The Python server owns asset routing and it is not going to change for a
// bundler: `/` serves static/index.html, `/static/*` serves static/*, and a
// handful of root paths (/manifest.json, /icon.png, /sw.js) are special-cased
// in server.py. Anything else falls through to the API router and 404s.
//
// So the build writes *into* static/ with `base: '/static/'` and the bundle
// under `static/app/` — every emitted URL is then `/static/app/...`, which the
// existing route already serves. emptyOutDir is off because static/ is not
// ours alone: fonts, avatars, img, css, sw.js and the vendored libs live there
// and are not build output.
export default ({ command }) => ({
  root: 'web',
  // Production serves the app from `/` with its bundle under `/static/app/`,
  // which is what `base` has to describe for a build. In dev there is no
  // Python server in front, so the app is simply at `/` — using the built
  // base here would move the dev app to /static/ and stop the URLs matching
  // what the real thing serves.
  base: command === 'build' ? '/static/' : '/',
  // Off: static/ is the build target, and letting Vite also treat it as a
  // public directory would make it copy the whole tree over itself.
  publicDir: false,
  plugins: [svelte(), serveRepoStatic()],
  resolve: {
    alias: {
      // The tested, framework-free modules stay where they are: the vitest
      // suite imports them from static/lib and they are the one source of
      // truth for protocol constants, the state machine and audio.
      '@core': path.join(staticDir, 'lib'),
    },
  },
  build: {
    outDir: '../static',
    emptyOutDir: false,
    assetsDir: 'app',
    target: 'es2022',
    // One chunk, and no content hash in the filename. Hashing would buy
    // nothing here — the server sends Cache-Control: no-store on every file
    // and the service worker's cache name is already derived from the newest
    // static mtime — while a hashed name cannot be listed in sw.js's offline
    // shell, which has to be a literal path.
    rollupOptions: {
      output: {
        inlineDynamicImports: true,
        entryFileNames: 'app/bundle.js',
        chunkFileNames: 'app/bundle.js',
        assetFileNames: 'app/[name][extname]',
      },
    },
  },
  // Bind explicitly: Vite's default resolves to ::1 only on this host, and
  // everything else here (the server, the tests, curl) speaks 127.0.0.1.
  server: { host: '127.0.0.1', port: 5174, proxy },
  preview: { host: '127.0.0.1', port: 5175, proxy },
});

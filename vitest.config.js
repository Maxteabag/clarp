import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const root = fileURLToPath(new URL('.', import.meta.url));

export default {
  // The Svelte plugin lets tests import the rune-based stores under
  // web/src/stores/*.svelte.js. It only compiles .svelte / .svelte.js files,
  // so the plain static/lib modules are untouched.
  plugins: [svelte({ hot: false })],
  resolve: {
    alias: {
      '@core': path.join(root, 'static', 'lib'),
    },
    // Svelte 5 stores are plain modules; resolve the browser build so
    // $state works in node just like it does in the bundle.
    conditions: ['browser'],
  },
  test: {
    include: ['tests/state/**/*.test.js', 'tests/sim-e2e/**/*.test.js', 'tests/contract/**/*.test.js'],
    environment: 'node',
    testTimeout: 30000,
    hookTimeout: 20000,
  },
};

import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('.', import.meta.url));

export default {
  resolve: {
    alias: {
      '@core': path.join(root, 'static', 'lib'),
    },
  },
  test: {
    include: ['tests/state/**/*.test.js', 'tests/sim-e2e/**/*.test.js'],
    environment: 'node',
    testTimeout: 30000,
    hookTimeout: 20000,
  },
};

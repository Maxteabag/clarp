import { mount } from 'svelte';
import App from './App.svelte';
import { bootstrapAuth, installAuthFetch, onAuthChange, setClogFlushInterval } from './lib/net.js';
import { Timing } from '@core/protocol.js';
import { removeServiceWorker } from './lib/sw.js';

import { app, agentSnapshot, setSession } from './stores/app.svelte.js';
import { conversation, conversations, reload } from './stores/conversations.svelte.js';
import { audio } from './stores/audio.svelte.js';
import { mic } from './stores/mic.svelte.js';
import { composerRef } from './stores/composer.svelte.js';

// Order matters: the token has to be read out of the URL and the fetch
// wrapper installed before any component mounts and starts making requests.
bootstrapAuth();
installAuthFetch();
onAuthChange((rejected) => { app.authRejected = rejected; });
setClogFlushInterval(Timing.CLIENT_LOG_FLUSH_MS);
removeServiceWorker();

// Expose live Svelte 5 reactive stores on window for DevTools inspection
if (typeof window !== 'undefined') {
  window.__CLARP__ = {
    app,
    conversations,
    audio,
    mic,
    composerRef,
    agentSnapshot,
    setSession,
    reload,
    inspect() {
      const c = conversation(app.session);
      return {
        session: app.session,
        conn: app.conn,
        status: c.status,
        turns: c.turns.length,
        revision: c.latestRevision,
        conversationId: c.conversationId,
        availableSessions: app.availableSessions,
      };
    },
  };
}

mount(App, { target: document.getElementById('app') });

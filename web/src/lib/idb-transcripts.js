// IndexedDB storage for @core/transcript-cache.js: one record per chat,
// keyed by session. Null when the browser has no IndexedDB (or refuses it),
// in which case chats simply open from the network as before.

const DB = 'clarp-transcripts';
const STORE = 'chats';

export function idbTranscriptStorage() {
  if (typeof indexedDB === 'undefined') return null;
  let dbp = null;
  const db = () => {
    if (!dbp) {
      dbp = new Promise((resolve, reject) => {
        const req = indexedDB.open(DB, 1);
        req.onupgradeneeded = () => req.result.createObjectStore(STORE, { keyPath: 'session' });
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
      });
      dbp.catch(() => { dbp = null; });
    }
    return dbp;
  };
  const run = (mode, op) => db().then(d => new Promise((resolve, reject) => {
    const tx = d.transaction(STORE, mode);
    const req = op(tx.objectStore(STORE));
    tx.oncomplete = () => resolve(req.result);
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error);
  }));
  return {
    get: session => run('readonly', s => s.get(session)).then(r => r || null),
    put: record => run('readwrite', s => s.put(record)),
    delete: session => run('readwrite', s => s.delete(session)),
  };
}

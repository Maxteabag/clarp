// Cache-first chat open (T3 Code's pattern): the newest page of each chat is
// kept on the device with its revision cursor, so opening a chat paints from
// it at once and asks /log only for what changed after the cursor.
//
// Writing is throttled per chat: the first write 500 ms after a change, then
// at most one per 10 s, and none while the agent works (a streaming turn
// changes the chat many times a second). The storage is injected: IndexedDB
// in the browser, a Map in tests.

export const CACHE_TURNS = 100;
export const FIRST_WRITE_MS = 500;
export const WRITE_COOLDOWN_MS = 10000;
const BUSY_RECHECK_MS = 2000;

/** The record kept for a chat: the reducer's cursor and its newest durable turns. */
export function cacheRecord(session, { conversationId, cursor, latestTs, hasMore, cwd, turns }) {
  const durable = (turns || []).filter(t => t && t.id && !t.optimistic && t.kind !== 'live');
  const kept = durable.slice(-CACHE_TURNS);
  return {
    session,
    conversation_id: conversationId || '',
    latest_revision: Number(cursor) || 0,
    latest_ts: latestTs || '',
    // Older turns than the kept page exist when the page was cut here.
    has_more: !!hasMore || durable.length > kept.length,
    cwd: cwd || '',
    turns: kept,
    saved_at: Date.now(),
  };
}

export function createTranscriptCache({ storage, isBusy = () => false } = {}) {
  const pending = new Map();   // session → { read, timer }
  const lastWrite = new Map(); // session → ms

  function arm(session, delay) {
    const entry = pending.get(session);
    if (!entry || entry.timer) return;
    entry.timer = setTimeout(() => flush(session), Math.max(0, delay));
  }

  async function flush(session) {
    const entry = pending.get(session);
    if (!entry) return;
    entry.timer = null;
    if (isBusy(session)) { arm(session, BUSY_RECHECK_MS); return; }
    pending.delete(session);
    lastWrite.set(session, Date.now());
    try {
      await storage.put(cacheRecord(session, entry.read()));
    } catch (_) {
      // Quota or a private-mode refusal: the cache is an optimisation.
    }
  }

  return {
    /** The chat changed; `read` returns the state to keep when the write happens. */
    changed(session, read) {
      if (!storage || !session) return;
      const entry = pending.get(session);
      if (entry) { entry.read = read; return; }
      pending.set(session, { read, timer: null });
      const since = Date.now() - (lastWrite.get(session) || -Infinity);
      arm(session, Math.max(FIRST_WRITE_MS, WRITE_COOLDOWN_MS - since));
    },
    async load(session) {
      if (!storage || !session) return null;
      try {
        const rec = await storage.get(session);
        return rec && Array.isArray(rec.turns) ? rec : null;
      } catch (_) {
        return null;
      }
    },
    async forget(session) {
      const entry = pending.get(session);
      if (entry && entry.timer) clearTimeout(entry.timer);
      pending.delete(session);
      try { await storage?.delete?.(session); } catch (_) {}
    },
  };
}

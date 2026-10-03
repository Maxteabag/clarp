// The unread badge: agents (other than the open one, and only those listed)
// with a user notification newer than the user's last visit. It is counted
// on every agent status change across the whole fleet, so it is linear and
// works on plain values (not reactive proxies).

export function countUnread(sessions, { available = [], current = '', seen = {}, notifications = {} } = {}) {
  const listed = available instanceof Set ? available : new Set(available);
  let count = 0;
  for (const sid of sessions) {
    if (sid === current || !listed.has(sid)) continue;
    if ((notifications[sid] || 0) > (seen[sid] || 0)) count++;
  }
  return count;
}

/**
 * Reads a JSON object from storage, parsing it again only when the stored
 * text changed. The returned object is shared: treat it as read-only.
 */
export function createJsonReader(getItem, parse = JSON.parse) {
  const cache = new Map();   // key → { raw, value }
  return key => {
    let raw = null;
    try { raw = getItem(key); } catch (_) {}
    const hit = cache.get(key);
    if (hit && hit.raw === raw) return hit.value;
    let value = {};
    try { value = raw ? parse(raw) || {} : {}; } catch (_) { value = {}; }
    cache.set(key, { raw, value });
    return value;
  };
}
